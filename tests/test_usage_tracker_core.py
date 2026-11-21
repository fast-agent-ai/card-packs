"""Core usage-tracker modules: pricing, catalog updates, storage, and session sweeps."""

from __future__ import annotations

import contextlib
import copy
import gzip
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugins" / "usage-tracker"
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from _usage_tracker import perf, pricing, pricing_updater, remotes, store, sweep  # noqa: E402

BUNDLED = json.loads(pricing.BUNDLED_CATALOG_PATH.read_text(encoding="utf-8"))
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)  # a Friday


def _catalog_payload(**overrides):
    payload = copy.deepcopy(BUNDLED)
    payload.update(overrides)
    return payload


def _test_catalog(*, version="2026-01-01.1", with_12h=True, extra_rules=()):
    rates_12h = {"cache_write_12h": "5.00"} if with_12h else {}
    rules = [
        {
            "id": "claude-test",
            "match": {"model": {"values": ["claude-test"]}, "providers": ["anthropic"]},
            "rates": {
                "input": "3.00",
                "cache_read": "0.30",
                "cache_write": "3.75",
                "cache_write_1h": "6.00",
                "output": "15.00",
            },
        },
        {
            "id": "gpt-test",
            "match": {"model": {"values": ["gpt-test"]}, "providers": ["openai"]},
            "rates": {
                "input": "2.00",
                "cache_read": "0.10",
                "cache_write": "2.50",
                **rates_12h,
                "output": "10.00",
            },
        },
        *extra_rules,
    ]
    return pricing.parse_catalog(
        {
            "schema": "fast-agent.pricing/v1",
            "catalog_version": version,
            "currency": "USD",
            "unit": "usd_per_million_tokens",
            "rules": rules,
        }
    )


def _attempt(
    model="claude-test",
    *,
    timestamp=None,
    provider="anthropic",
    prompt=1_000,
    output=100,
    cache_read=0,
    cache_write=0,
    uncached=None,
    reasoning=None,
    duration_ms=None,
    ttft_ms=None,
    raw_usage=None,
    cost_usd=None,
    service_tier=None,
):
    return pricing.UsageAttempt(
        provider=provider,
        usage_schema="anthropic" if provider == "anthropic" else "openai-responses",
        model=model,
        prompt=pricing.PromptUsage(
            total=prompt, uncached=uncached, cache_read=cache_read, cache_write=cache_write
        ),
        completion=pricing.CompletionUsage(total=output, reasoning=reasoning),
        timestamp=NOW.timestamp() if timestamp is None else timestamp,
        timing=(
            pricing.AttemptTiming(duration_ms=duration_ms, ttft_ms=ttft_ms)
            if duration_ms is not None
            else None
        ),
        raw_usage=raw_usage,
        cost_usd=cost_usd,
        service_tier=service_tier,
    )


def _stored_fields(attempt):
    one_hour, twelve_hour = pricing.extract_cache_write_splits(attempt)
    return {
        "provider": pricing.provider_name(attempt),
        "upstream_provider": attempt.upstream_provider,
        "usage_schema": attempt.usage_schema,
        "model": attempt.model,
        "service_tier": pricing.normalized_service_tier(attempt),
        "timestamp": attempt.timestamp,
        "input_tokens": attempt.prompt.total,
        "uncached_tokens": attempt.prompt.uncached,
        "cache_read_tokens": attempt.prompt.cache_read,
        "cache_write_tokens": attempt.prompt.cache_write,
        "cache_write_1h_tokens": one_hour,
        "cache_write_12h_tokens": twelve_hour,
        "output_tokens": attempt.completion.total,
    }


class _TempDirTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.db = self.tmp / "usage" / "usage.sqlite3"

    def tearDown(self):
        self._tmp.cleanup()


# =========================================================================== pricing


class PricingCatalogTests(unittest.TestCase):
    def test_bundled_catalog_loads_from_any_path(self):
        catalog = pricing.load_catalog(pricing.BUNDLED_CATALOG_PATH)
        self.assertEqual(catalog.catalog_version, BUNDLED["catalog_version"])
        self.assertEqual(catalog.version, catalog.catalog_version)
        self.assertEqual(catalog.schema, "fast-agent.pricing/v1")
        self.assertIsNone(catalog.min_plugin_version)
        self.assertEqual(len(catalog.rules), len(BUNDLED["rules"]))

    def test_min_plugin_version_is_accepted(self):
        catalog = pricing.parse_catalog(_catalog_payload(min_plugin_version="0.2.0"))
        self.assertEqual(catalog.min_plugin_version, "0.2.0")

    def test_rejects_unknown_fields_unsupported_schema_and_overlaps(self):
        with self.assertRaisesRegex(ValueError, "unknown fields"):
            pricing.parse_catalog(_catalog_payload(surprise=True))
        with self.assertRaisesRegex(ValueError, "schema"):
            pricing.parse_catalog(_catalog_payload(schema="fast-agent.pricing/v9"))
        rules = copy.deepcopy(BUNDLED["rules"])
        duplicate = copy.deepcopy(rules[0])
        duplicate["id"] = "duplicate-rule"
        with self.assertRaisesRegex(ValueError, "Ambiguous pricing rules"):
            pricing.parse_catalog(_catalog_payload(rules=[*rules, duplicate]))

    def test_anthropic_one_hour_cache_writes_price_live_and_stored(self):
        catalog = _test_catalog()
        attempt = _attempt(
            prompt=13_500,
            cache_read=10_000,
            cache_write=3_000,
            uncached=500,
            output=200,
            raw_usage={
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 1_000,
                    "ephemeral_1h_input_tokens": 2_000,
                }
            },
        )
        expected = (500 * 3 + 10_000 * 0.3 + 1_000 * 3.75 + 2_000 * 6 + 200 * 15) / 1e6
        self.assertAlmostEqual(pricing.call_cost(attempt, catalog=catalog), expected)
        self.assertEqual(pricing.extract_cache_write_splits(attempt), (2_000, None))
        stored = _stored_fields(attempt)
        self.assertAlmostEqual(pricing.price_stored_call(**stored, catalog=catalog), expected)
        # Without the split every write would be (wrongly) priced at the 5m rate.
        cheaper = pricing.price_stored_call(
            **{**stored, "cache_write_1h_tokens": None}, catalog=catalog
        )
        self.assertLess(cheaper, expected)

    def test_openai_twelve_hour_cache_writes_price_live_and_stored(self):
        catalog = _test_catalog()
        attempt = _attempt(
            "gpt-test",
            provider="openai",
            prompt=1_400,
            cache_read=1_000,
            cache_write=400,
            output=50,
            raw_usage={
                "input_tokens_details": {
                    "cache_write_tokens_details": {"ttl_30m_tokens": 100, "ttl_12h_tokens": 300}
                }
            },
        )
        expected = (1_000 * 0.1 + 100 * 2.5 + 300 * 5 + 50 * 10) / 1e6
        self.assertAlmostEqual(pricing.call_cost(attempt, catalog=catalog), expected)
        self.assertEqual(pricing.extract_cache_write_splits(attempt), (None, 300))
        self.assertAlmostEqual(
            pricing.price_stored_call(**_stored_fields(attempt), catalog=catalog), expected
        )

        # A known 12h write never falls back to a cheaper tariff, but a newer
        # catalog with the rate could still price it.
        no_12h = _test_catalog(with_12h=False)
        priced = pricing.price_call(attempt, catalog=no_12h)
        self.assertIsNone(priced.cost_usd)
        self.assertTrue(priced.repriceable)
        self.assertIsNone(pricing.price_stored_call(**_stored_fields(attempt), catalog=no_12h))

    def test_invalid_openai_lifetimes_are_unpriceable(self):
        attempt = _attempt(
            "gpt-test",
            provider="openai",
            prompt=1_400,
            cache_read=1_000,
            cache_write=400,
            raw_usage={
                "input_tokens_details": {
                    "cache_write_tokens_details": {"ttl_30m_tokens": 1, "ttl_12h_tokens": 1}
                }
            },
        )
        priced = pricing.price_call(attempt, catalog=_test_catalog())
        self.assertIsNone(priced.cost_usd)
        self.assertFalse(priced.repriceable)

    def test_price_call_records_version_provider_cost_and_missing_rates(self):
        catalog = _test_catalog(version="2026-02-02.3")
        self.assertEqual(
            pricing.price_call(_attempt(), catalog=catalog).pricing_version, "2026-02-02.3"
        )
        provider_cost = pricing.price_call(_attempt(cost_usd=0.5), catalog=catalog)
        self.assertEqual((provider_cost.cost_usd, provider_cost.pricing_version), (0.5, "provider"))
        unknown = pricing.price_call(_attempt("mystery-model"), catalog=catalog)
        self.assertIsNone(unknown.cost_usd)
        self.assertTrue(unknown.repriceable)
        inconsistent = pricing.price_call(
            _attempt(prompt=100, uncached=10, cache_read=10), catalog=catalog
        )
        self.assertIsNone(inconsistent.cost_usd)
        self.assertFalse(inconsistent.repriceable)

    def test_attempt_from_payload_validates(self):
        attempt = pricing.attempt_from_payload(
            {
                "provider": "anthropic",
                "usage_schema": "anthropic",
                "model": "claude-test",
                "prompt": {"total": 10, "cache_read": 2},
                "completion": {"total": 3},
                "timestamp": 1.5,
                "timing": {"duration_ms": 1200.0, "ttft_ms": 200.0},
            }
        )
        self.assertEqual(attempt.timing.ttft_ms, 200.0)
        self.assertEqual(attempt.prompt.cache_read, 2)
        with self.assertRaises(ValueError):
            pricing.attempt_from_payload({"provider": "x", "model": "m", "prompt": {"total": -1}})
        with self.assertRaises(ValueError):
            pricing.attempts_from_usage_payload({"schema": "fast-agent.usage/v1"})

    def test_post_turn_cost_line_and_herdr_token(self):
        catalog = _test_catalog()
        turn = (_attempt(prompt=1_000_000, output=0),)
        session = (*turn, _attempt(prompt=1_000_000, output=0, timestamp=1.0))
        result = pricing.format_post_turn_cost(turn, session, catalog=catalog)
        self.assertIn("$3.00", result.display)
        self.assertIn("$6.00", result.display)
        self.assertEqual(result.usage_token, "$3.00 ($6.00)")
        unpriced = pricing.format_post_turn_cost(
            (_attempt("mystery"),), (_attempt("mystery"),), catalog=catalog
        )
        self.assertIn("1 unpriced model call", unpriced.display)
        self.assertEqual(unpriced.usage_token, "1,000 in · 100 out")
        self.assertIsNone(pricing.format_post_turn_cost((), session, catalog=catalog))

    def test_cost_report_modes(self):
        catalog = _test_catalog()
        attempts = (_attempt(), _attempt("gpt-test", provider="openai", timestamp=2.0))
        summary = pricing.cost_report(
            "", message_history=(), agent_name="agent", session_attempts=attempts, catalog=catalog
        )
        self.assertIn("### Model cost by user turn", summary.markdown)
        self.assertIn("### Model cost by model", summary.markdown)
        detail = pricing.cost_report(
            "detail",
            message_history=(),
            agent_name="agent",
            session_attempts=attempts,
            catalog=catalog,
        )
        self.assertIn("### Model cost detail", detail.markdown)
        self.assertIsNone(
            pricing.cost_report("bogus", message_history=(), agent_name="agent", catalog=catalog)
        )

    def test_active_catalog_follows_updater_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            pricing.set_active_data_dir(Path(tmp))
            try:
                self.assertEqual(
                    pricing.active_catalog().catalog_version, BUNDLED["catalog_version"]
                )
                (Path(tmp) / "pricing_catalog.json").write_text(
                    json.dumps(_catalog_payload(catalog_version="2999-01-01.1"))
                )
                self.assertEqual(pricing.active_catalog().catalog_version, "2999-01-01.1")
            finally:
                pricing.set_active_data_dir(None)


# =========================================================================== updater


class _Response:
    def __init__(self, status=200, body=b"", headers=None):
        self.status = status
        self._body = io.BytesIO(body)
        self.headers = headers or {}

    def read(self, size=-1):
        return self._body.read(size)

    def close(self):
        pass


class _Opener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _gzip_json(payload):
    return gzip.compress(json.dumps(payload).encode("utf-8"))


def _ok(payload, etag='"v1"', gzipped=True):
    body = _gzip_json(payload) if gzipped else json.dumps(payload).encode("utf-8")
    headers = {"ETag": etag}
    if gzipped:
        headers["Content-Encoding"] = "gzip"
    return _Response(200, body, headers)


class PricingUpdaterTests(_TempDirTest):
    URL = "https://example.invalid/pricing_catalog.json"

    def _check(self, opener, **kwargs):
        return pricing_updater.check_for_update(
            data_dir=self.tmp, pricing_url=self.URL, opener=opener, **kwargs
        )

    def _state(self):
        return json.loads((self.tmp / "pricing_state.json").read_text())

    def test_version_comparisons(self):
        self.assertGreater(pricing_updater.compare_versions("2026-11-20.2", "2026-11-20.1"), 0)
        self.assertLess(pricing_updater.compare_versions("2026-11-9.1", "2026-11-20.1"), 0)
        self.assertEqual(pricing_updater.compare_versions("2026-11-20", "2026-11-20.0"), 0)
        self.assertGreater(pricing_updater.compare_versions("beta", "alpha"), 0)
        self.assertTrue(pricing_updater.plugin_supports("0.1.0", "0.1.0"))
        self.assertTrue(pricing_updater.plugin_supports(None, "0.1.0"))
        self.assertFalse(pricing_updater.plugin_supports("0.2.0", "0.1.9"))
        self.assertTrue(pricing_updater.plugin_supports("0.1", "0.1.3"))

    def test_gzipped_update_then_not_modified(self):
        opener = _Opener(_ok(_catalog_payload(catalog_version="2999-01-01.1")))
        status = self._check(opener)
        request = opener.requests[0]
        self.assertEqual(request.get_header("Accept-encoding"), "gzip")
        self.assertIsNone(request.get_header("If-none-match"))
        self.assertEqual(status.active_source, "cached")
        self.assertEqual(status.active_version, "2999-01-01.1")
        self.assertEqual(status.cached_version, "2999-01-01.1")
        self.assertEqual(status.etag, '"v1"')
        self.assertFalse(status.upgrade_required)
        self.assertIsNone(status.last_error)
        self.assertEqual(self._state()["catalog_version"], "2999-01-01.1")
        self.assertEqual(list(self.tmp.glob("*.tmp")), [])

        first_checked = status.last_checked_at
        time.sleep(0.01)
        opener = _Opener(_Response(304))
        status = self._check(opener)
        self.assertEqual(opener.requests[0].get_header("If-none-match"), '"v1"')
        self.assertEqual(status.active_version, "2999-01-01.1")
        self.assertGreater(status.last_checked_at, first_checked)

        # urllib raises HTTPError for 304; that is not-modified too.
        not_modified = urllib.error.HTTPError(self.URL, 304, "Not Modified", {}, None)
        status = self._check(_Opener(not_modified))
        self.assertIsNone(status.last_error)
        self.assertEqual(status.active_version, "2999-01-01.1")

    def test_force_without_cached_catalog_skips_etag(self):
        self._check(_Opener(_ok(_catalog_payload(catalog_version="2999-01-01.1"))))
        (self.tmp / "pricing_catalog.json").unlink()
        opener = _Opener(_ok(_catalog_payload(catalog_version="2999-01-02.1"), etag='"v2"'))
        status = self._check(opener, force=True)
        self.assertIsNone(opener.requests[0].get_header("If-none-match"))
        self.assertEqual(status.active_version, "2999-01-02.1")

    def test_incompatible_schema_or_plugin_version_requires_upgrade(self):
        self._check(_Opener(_ok(_catalog_payload(catalog_version="2999-01-01.1"))))
        for payload in (
            _catalog_payload(schema="fast-agent.pricing/v2", catalog_version="3000-01-01.1"),
            _catalog_payload(min_plugin_version="99.0.0", catalog_version="3000-01-01.1"),
        ):
            with self.subTest(payload=payload.get("schema")):
                status = self._check(_Opener(_ok(payload, etag='"new"')))
                self.assertTrue(status.upgrade_required)
                self.assertEqual(
                    status.upgrade_notice,
                    "New pricing rules available — run /plugins update usage-tracker",
                )
                self.assertEqual(status.etag, '"new"')
                # The working catalog is kept.
                self.assertEqual(status.active_version, "2999-01-01.1")
                self.assertEqual(status.cached_version, "2999-01-01.1")

    def test_compatible_min_plugin_version_is_installed(self):
        payload = _catalog_payload(catalog_version="2999-05-05.1", min_plugin_version="0.0.1")
        status = self._check(_Opener(_ok(payload, gzipped=False)))
        self.assertEqual(status.active_version, "2999-05-05.1")
        self.assertFalse(status.upgrade_required)

    def test_plugin_update_clears_stale_upgrade_notice(self):
        payload = _catalog_payload(min_plugin_version="99.0.0")
        self._check(_Opener(_ok(payload, etag='"new"')))
        state = self._state()
        state["plugin_version"] = "0.0.1"  # recorded by an older plugin release
        (self.tmp / "pricing_state.json").write_text(json.dumps(state))
        status = pricing_updater.get_status(self.tmp)
        self.assertFalse(status.upgrade_required)
        self.assertIsNone(status.etag)

    def test_invalid_catalog_and_network_errors_keep_current_catalog(self):
        self._check(_Opener(_ok(_catalog_payload(catalog_version="2999-01-01.1"))))
        rules = copy.deepcopy(BUNDLED["rules"])
        rules[0]["rates"]["input"] = "-1"
        bad = _catalog_payload(catalog_version="3000-01-01.1", rules=rules)
        for response in (
            _ok(bad, etag='"bad"'),
            _Response(200, b"{not json"),
            _Response(500),
            urllib.error.URLError("offline"),
            TimeoutError("slow"),
        ):
            with self.subTest(response=response):
                status = self._check(_Opener(response))
                self.assertIsNotNone(status.last_error)
                self.assertEqual(status.active_version, "2999-01-01.1")
                self.assertEqual(status.etag, '"v1"')

    def test_bundled_vs_cached_precedence(self):
        cached = self.tmp / "pricing_catalog.json"
        bundled_version = BUNDLED["catalog_version"]
        self.assertEqual(
            pricing_updater.get_active_catalog(self.tmp)[1], "bundled"
        )  # nothing cached
        cached.write_text(json.dumps(_catalog_payload(catalog_version="2000-01-01.1")))
        catalog, source = pricing_updater.get_active_catalog(self.tmp)
        self.assertEqual((catalog.catalog_version, source), (bundled_version, "bundled"))
        cached.write_text(json.dumps(_catalog_payload(catalog_version="2999-12-31.10")))
        catalog, source = pricing_updater.get_active_catalog(self.tmp)
        self.assertEqual((catalog.catalog_version, source), ("2999-12-31.10", "cached"))
        self.assertIs(pricing_updater.get_active_catalog(self.tmp)[0], catalog)  # memoised
        cached.write_text("{corrupt")
        self.assertEqual(pricing_updater.get_active_catalog(self.tmp)[1], "bundled")
        cached.write_text(json.dumps(_catalog_payload(catalog_version=bundled_version)))
        self.assertEqual(pricing_updater.get_active_catalog(self.tmp)[1], "bundled")

    def test_resolve_data_dir(self):
        self.assertEqual(
            pricing_updater.resolve_data_dir({"db_path": str(self.tmp / "x" / "u.db")}),
            self.tmp / "x",
        )
        self.assertEqual(
            pricing_updater.resolve_data_dir({"data_dir": str(self.tmp / "d")}), self.tmp / "d"
        )
        self.assertEqual(
            pricing_updater.resolve_data_dir({}), Path.home() / ".fast-agent" / "usage"
        )

    def test_background_update_throttles_and_deduplicates(self):
        config = {"pricing_url": self.URL}
        self.assertFalse(
            pricing_updater.maybe_schedule_background_update(
                data_dir=self.tmp, config={"pricing_auto_update": False}
            )
        )
        release = threading.Event()
        started = threading.Event()

        def slow_opener(request, timeout=None):
            started.set()
            release.wait(5)
            return _Response(304)

        self.assertTrue(
            pricing_updater.maybe_schedule_background_update(
                data_dir=self.tmp, config=config, opener=slow_opener
            )
        )
        started.wait(5)
        # A second request while the first is in flight is dropped.
        self.assertFalse(
            pricing_updater.maybe_schedule_background_update(
                data_dir=self.tmp, config=config, opener=slow_opener
            )
        )
        release.set()
        self.assertTrue(pricing_updater.wait_for_background_update(5))
        checked_at = self._state()["checked_at"]

        def schedule(now, has_unpriced=False):
            started = pricing_updater.maybe_schedule_background_update(
                data_dir=self.tmp,
                config=config,
                opener=_Opener(_Response(304)),
                has_unpriced=has_unpriced,
                now=now,
            )
            pricing_updater.wait_for_background_update(5)
            return started

        self.assertFalse(schedule(checked_at + 2 * 3600))
        self.assertTrue(schedule(checked_at + 2 * 3600, has_unpriced=True))
        checked_at = self._state()["checked_at"]
        self.assertFalse(schedule(checked_at + 30 * 60, has_unpriced=True))
        self.assertTrue(schedule(checked_at + 25 * 3600))


# =========================================================================== store


def _call(start, duration_ms, ttft_ms, tokens, model="claude-test"):
    return perf.Call(
        model=model,
        end=start + duration_ms / 1000,
        duration_ms=duration_ms,
        ttft_ms=ttft_ms,
        tokens=tokens,
    )


class StoreTests(_TempDirTest):
    def setUp(self):
        super().setUp()
        self.catalog = _test_catalog()

    def _record(self, attempts, record=None, session_id="s1", catalog=None):
        return store.record_turn(
            self.db,
            session_id=session_id,
            attempts=attempts,
            turn_record=record,
            catalog=catalog or self.catalog,
        )

    def _report(self, **kwargs):
        kwargs.setdefault("now", NOW)
        return store.query_report(self.db, **kwargs)

    def test_schema_pragmas_and_indexes(self):
        with store.UsageStore(self.db) as usage:
            connection = usage.connection
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 2000)
            version = connection.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()[0]
            self.assertEqual(version, str(store.SCHEMA_VERSION))
            indexes = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")
            }
        self.assertTrue(
            {
                "idx_calls_timestamp",
                "idx_calls_model_timestamp",
                "idx_turns_timestamp",
                "idx_tool_runs_timestamp",
                "idx_tool_runs_tool_name",
            }
            <= indexes
        )

    def test_record_turn_is_idempotent_across_sessions(self):
        start = NOW.timestamp()
        attempts = (
            _attempt(timestamp=start, duration_ms=2_000, ttft_ms=500),
            _attempt(timestamp=start + 5, duration_ms=1_000, ttft_ms=900),
        )
        record = perf.TurnRecord(
            calls=(_call(0.0, 2_000, 500, 100), _call(3.0, 1_000, 900, 100)),
            tools=(perf.ToolRun("bash", 2.0, 1_000, False),),
        )
        self.assertEqual(self._record(attempts, record), (2, 1, 1))
        self.assertEqual(self._record(attempts, record), (0, 0, 0))
        # A forked session replays the same history: nothing is double-counted.
        self.assertEqual(self._record(attempts, record, session_id="fork"), (0, 0, 0))
        report = self._report(window="7d")
        self.assertEqual(report.total_calls, 2)
        self.assertEqual(report.total_sessions, 1)

    def test_turn_phases_and_tool_runs_across_sessions(self):
        base = NOW.timestamp() - 3600
        # Session 1: 2s call (0.5s wait, 1.5s stream), a 1s tool, then a 3s process wait.
        record_one = perf.TurnRecord(
            calls=(_call(100.0, 2_000, 500, 300), _call(106.0, 1_000, 1_000, 10)),
            tools=(
                perf.ToolRun("bash", 102.0, 1_000, False),
                perf.ToolRun("process", 103.0, 3_000, True),
            ),
        )
        attempts_one = (
            _attempt(timestamp=base + 2, duration_ms=2_000, ttft_ms=500),
            _attempt(timestamp=base + 7, duration_ms=1_000, ttft_ms=1_000),
        )
        record_two = perf.TurnRecord(
            calls=(_call(500.0, 1_000, 200, 50),),
            tools=(perf.ToolRun("bash", 501.0, 4_000, False),),
        )
        attempts_two = (_attempt(timestamp=base + 60, duration_ms=1_000, ttft_ms=200),)
        self.assertEqual(self._record(attempts_one, record_one, session_id="s1"), (2, 1, 2))
        self.assertEqual(self._record(attempts_two, record_two, session_id="s2"), (1, 1, 1))

        report = self._report(window="7d")
        self.assertEqual(report.total_sessions, 2)
        phases = report.phases
        self.assertEqual(phases.turns, 2)
        self.assertAlmostEqual(phases.wait_ms, 500 + 1_000 + 200)
        self.assertAlmostEqual(phases.stream_ms, 1_500 + 800)
        self.assertAlmostEqual(phases.tools_ms, 1_000 + 4_000)
        self.assertAlmostEqual(phases.process_ms, 3_000)
        self.assertAlmostEqual(
            phases.wall_ms,
            phases.wait_ms
            + phases.stream_ms
            + phases.tools_ms
            + phases.process_ms
            + phases.other_ms,
        )
        self.assertAlmostEqual(phases.wall_ms, 7_000 + 5_000)
        self.assertEqual([tool.tool_name for tool in report.tools], ["bash"])
        (bash,) = report.tools
        self.assertEqual((bash.runs, bash.total_ms, bash.max_ms), (2, 5_000, 4_000))
        self.assertEqual(bash.avg_ms, 2_500)
        self.assertIsNotNone(report.process_waits)
        assert report.process_waits is not None
        self.assertEqual(report.process_waits.waits, 1)
        self.assertAlmostEqual(report.process_waits.total_ms, 3_000.0)
        self.assertEqual(report.process_waits.by_tool, (("process", 1),))
        self.assertEqual([tool.tool_name for tool in report.raw_tools], ["bash", "process"])
        self.assertEqual(report.raw_tools[1].process_wait_runs, 1)
        with sqlite3.connect(self.db) as connection:
            tool_times = sorted(
                row[0] for row in connection.execute("SELECT timestamp_ms FROM tool_runs")
            )
        # Tool runs are anchored to wall-clock time via the turn's attempts.
        turn_start_ms = round((base + 2 - 2) * 1000)
        self.assertEqual(tool_times[0], turn_start_ms + 2_000)

    def test_reprice_unpriced_uses_new_catalog(self):
        old = _test_catalog(version="2026-01-01.1")
        priced = _attempt(prompt=1_000_000, output=0)
        newly_known = _attempt("new-model", prompt=1_000_000, output=1_000_000, timestamp=1e9)
        unpriceable = _attempt("new-model", prompt=100, uncached=50, cache_read=10, timestamp=2e9)
        self._record((priced, newly_known, unpriceable), catalog=old)
        self.assertEqual(store.unpriced_count(self.db), 1)
        new = _test_catalog(
            version="2026-02-01.1",
            extra_rules=(
                {
                    "id": "new-model",
                    "match": {"model": {"values": ["new-model"]}},
                    "rates": {"input": "1.00", "cache_read": "0.10", "output": "2.00"},
                },
            ),
        )
        self.assertEqual(store.reprice_unpriced(self.db, new), 1)
        self.assertEqual(store.reprice_unpriced(self.db, new), 0)
        with sqlite3.connect(self.db) as connection:
            rows = dict(
                connection.execute(
                    "SELECT model || ':' || timestamp_ms, cost_usd || '@' || "
                    "COALESCE(pricing_version, '-') FROM calls"
                ).fetchall()
            )
        self.assertEqual(rows[f"claude-test:{round(NOW.timestamp() * 1000)}"], "3.0@2026-01-01.1")
        self.assertEqual(rows["new-model:1000000000000"], "3.0@2026-02-01.1")
        self.assertIsNone(rows["new-model:2000000000000"])  # still unpriced
        with sqlite3.connect(self.db) as connection:
            marker = connection.execute(
                "SELECT pricing_version FROM calls WHERE timestamp_ms = 2000000000000"
            ).fetchone()[0]
        self.assertEqual(marker, store.UNPRICEABLE)
        self.assertEqual(store.unpriced_count(self.db), 0)

    def test_ttft_percentiles_tps_and_burst_counts(self):
        start = NOW.timestamp() - 60
        attempts = [
            _attempt(timestamp=start + index, duration_ms=ttft + 10, ttft_ms=ttft, output=5)
            for index, ttft in enumerate(range(100, 1001, 100))
        ]  # ten burst calls (10ms of streaming)
        attempts += [
            _attempt(timestamp=start + 20, duration_ms=1_500, ttft_ms=None, output=10),
            _attempt(timestamp=start + 21, duration_ms=1_500, ttft_ms=500, output=100),
            _attempt(timestamp=start + 22, duration_ms=3_500, ttft_ms=500, output=500),
        ]
        self._record(attempts)
        (row,) = self._report(window="7d").models
        self.assertEqual(row.calls, 13)
        ttfts = sorted([*range(100, 1001, 100), 500, 500])
        self.assertEqual(row.ttft_p50_ms, ttfts[5])  # nearest rank
        self.assertEqual(row.ttft_p90_ms, ttfts[10])
        self.assertAlmostEqual(row.tps, 600 / 4)
        self.assertEqual((row.streamed_calls, row.burst_calls), (2, 10))

    def test_streaks_daily_points_and_windows(self):
        today = NOW.date()
        active_offsets = (0, 1, 2, 5, 6, 7, 8, 40)
        for offset in active_offsets:
            moment = datetime.combine(today - timedelta(days=offset), datetime.min.time(), UTC)
            self._record(
                (_attempt(timestamp=moment.timestamp() + 3600, output=10),),
                session_id=f"s{offset}",
            )
        report = self._report(window="30d")
        self.assertEqual(report.window_label, "Last 30 days")
        self.assertEqual(len(report.daily), 30)
        self.assertEqual(report.daily[-1].date_iso, today.isoformat())
        self.assertEqual(report.start_date, (today - timedelta(days=29)).isoformat())
        self.assertEqual(report.total_calls, 7)
        self.assertEqual(report.active_days, 7)
        self.assertEqual(report.current_streak_days, 3)
        self.assertEqual(report.longest_streak_days, 4)
        self.assertEqual(sum(point.calls for point in report.daily), 7)
        self.assertEqual(report.daily[-1].sessions, 1)

        self.assertEqual(self._report(window="90d").total_calls, 8)
        self.assertEqual(self._report(window="7d").total_calls, 5)
        self.assertEqual(self._report(window="1d").total_calls, 1)
        hours = self._report(window="24h")
        self.assertEqual((hours.total_calls, hours.window_label), (1, "Last 24 hours"))
        everything = self._report(window="all")
        self.assertEqual(everything.total_calls, 8)
        self.assertEqual(everything.start_date, (today - timedelta(days=40)).isoformat())
        self.assertEqual(len(everything.daily), 41)
        self.assertIsNone(everything.rhythm.window_days)
        with self.assertRaises(ValueError):
            self._report(window="fortnight")

        # A gap today does not break the current streak until the day is over.
        tomorrow = self._report(window="30d", now=NOW + timedelta(days=1))
        self.assertEqual(tomorrow.current_streak_days, 3)
        later = self._report(window="30d", now=NOW + timedelta(days=2))
        self.assertEqual(later.current_streak_days, 0)

    def test_heatmap_utc_and_local(self):
        monday = datetime(2026, 11, 16, 9, 30, tzinfo=UTC)
        self._record((_attempt(timestamp=monday.timestamp()),))
        utc = self._report(window="30d").rhythm
        self.assertEqual(utc.timezone_label, "UTC")
        self.assertEqual(utc.window_days, 30)
        self.assertEqual(len(utc.cells), 7)
        self.assertTrue(all(len(row) == 6 for row in utc.cells))
        self.assertEqual(utc.cells[0][2], 1)  # Monday, 08-12 UTC
        self.assertEqual(sum(map(sum, utc.cells)), 1)

        with _timezone("Asia/Tokyo"):  # UTC+9: Monday 18:30
            local = self._report(window="30d", tz_mode="local")
        self.assertEqual(local.rhythm.cells[0][4], 1)
        self.assertEqual(local.rhythm.timezone_label, "JST")
        with _timezone("America/Los_Angeles"):  # UTC-8: Monday 01:30
            local = self._report(window="30d", tz_mode="local")
        self.assertEqual(local.rhythm.cells[0][0], 1)

    def test_model_filter(self):
        self._record(
            (
                _attempt("claude-test", prompt=5_000),
                _attempt("gpt-test", provider="openai", timestamp=NOW.timestamp() - 1),
            ),
            perf.TurnRecord(calls=(_call(0, 1_000, 100, 10),)),
        )
        report = self._report(window="7d", model_filter="CLAUDE")
        self.assertEqual([row.model for row in report.models], ["claude-test"])
        self.assertEqual(report.total_calls, 1)
        self.assertEqual(report.model_filter, "CLAUDE")
        self.assertIsNone(report.phases)  # turns are not attributed to models
        unfiltered = self._report(window="7d")
        self.assertEqual([row.model for row in unfiltered.models], ["claude-test", "gpt-test"])
        self.assertIsNotNone(unfiltered.phases)
        self.assertAlmostEqual(
            unfiltered.total_cost_usd, sum(row.cost_usd for row in unfiltered.models)
        )


@contextlib.contextmanager
def _timezone(name):
    previous = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


# =========================================================================== sweep


def _text(value):
    return [{"type": "text", "text": value if isinstance(value, str) else json.dumps(value)}]


def _usage(model, timestamp, *, prompt=1_000, output=100, ttft_ms=200.0, duration_ms=1_000.0):
    return {
        "schema": "fast-agent.usage/v2",
        "provider_attempts": [
            {
                "provider": "anthropic",
                "upstream_provider": None,
                "usage_schema": "anthropic",
                "model": model,
                "prompt": {"total": prompt, "uncached": None, "cache_read": 0, "cache_write": 0},
                "completion": {"total": output, "reasoning": 0},
                "tool_calls": 0,
                "service_tier": None,
                "cost_usd": None,
                "timestamp": timestamp,
                "timing": {"duration_ms": duration_ms, "ttft_ms": ttft_ms},
                "raw_usage": None,
            }
        ],
    }


def _user(text):
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def _assistant(timestamp, mono_end, *, tool_calls=None, model="claude-test", duration_ms=1_000.0):
    message = {
        "role": "assistant",
        "content": [],
        "stop_reason": "toolUse" if tool_calls else "endTurn",
        "channels": {
            "fast-agent-timing": _text(
                {
                    "start_time": mono_end - duration_ms / 1000,
                    "end_time": mono_end,
                    "duration_ms": duration_ms,
                    "ttft_ms": 200.0,
                }
            ),
            "fast-agent-usage": _text(_usage(model, timestamp, duration_ms=duration_ms)),
        },
    }
    if tool_calls:
        message["tool_calls"] = {
            call_id: {"method": "tools/call", "params": {"name": name, "arguments": args}}
            for call_id, (name, args) in tool_calls.items()
        }
    return message


def _tool_result(timings, extra_channels=None):
    return {
        "role": "user",
        "content": [],
        "tool_results": {call_id: {"content": []} for call_id in timings},
        "channels": {
            "fast-agent-tool-timing": _text(
                {call_id: {"timing_ms": ms} for call_id, ms in timings.items()}
            ),
            **(extra_channels or {}),
        },
    }


def _conversation(base_ts, mono, turns):
    """``turns`` user turns, each: prompt, tool call (bash), result, final answer."""
    messages = []
    for turn in range(turns):
        ts = base_ts + turn * 100
        clock = mono + turn * 100
        messages += [
            _user(f"question {turn}"),
            _assistant(ts, clock + 1, tool_calls={f"call-{turn}": ("bash", {"command": "ls"})}),
            _tool_result({f"call-{turn}": 2_000.0}),
            _assistant(ts + 5, clock + 5),
        ]
    return messages


def _write_session(session_dir, messages, *, session_id=None, history="history_agent.json"):
    session_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 7,
        "session_id": session_id or session_dir.name,
        "continuation": {"agents": {"agent": {"history_file": history}}},
    }
    (session_dir / "session.json").write_text(json.dumps(payload))
    (session_dir / history).write_text(json.dumps({"messages": messages}))


class SweepTests(_TempDirTest):
    def setUp(self):
        super().setUp()
        self.catalog = _test_catalog()
        self.root = self.tmp / "projects"
        self.base = NOW.timestamp() - 86_400

    def _sweep(self, root=None, **kwargs):
        return sweep.sweep_directory(
            root or self.root, db_path=self.db, catalog=self.catalog, **kwargs
        )

    def test_rejects_missing_directory(self):
        with self.assertRaises(ValueError):
            self._sweep(self.tmp / "missing")

    def test_active_history_only_and_pruned_directories(self):
        sessions = self.root / "app" / ".fast-agent" / "sessions"
        _write_session(sessions / "s1", _conversation(self.base, 1_000.0, 2))
        previous = _conversation(self.base - 5_000, 50.0, 3)
        (sessions / "s1" / "history_agent_previous.json").write_text(
            json.dumps({"messages": previous})
        )
        for pruned in (".git", ".venv", "node_modules"):
            _write_session(self.root / pruned / "s-hidden", _conversation(self.base - 9e4, 1.0, 1))

        summary = self._sweep()
        self.assertEqual(summary.sessions_scanned, 1)
        self.assertEqual(summary.files_scanned, 1)
        self.assertEqual(summary.calls_imported, 4)
        self.assertEqual(summary.turns_imported, 2)
        self.assertEqual(summary.tool_runs_imported, 2)
        self.assertEqual(summary.tokens_imported, 4 * 1_100)
        self.assertAlmostEqual(summary.cost_usd_imported, 4 * (1_000 * 3 + 100 * 15) / 1e6)
        self.assertEqual(summary.unpriced_calls_imported, 0)

        report = store.query_report(self.db, window="all", now=NOW)
        self.assertEqual(report.total_calls, 4)
        self.assertEqual(report.total_sessions, 1)
        self.assertEqual(report.phases.turns, 2)
        self.assertEqual(report.tools[0].tool_name, "bash")
        self.assertEqual(report.tools[0].runs, 2)
        with sqlite3.connect(self.db) as connection:
            sources = {row[0] for row in connection.execute("SELECT source FROM calls")}
        self.assertEqual(sources, {"sweep"})

    def test_forked_session_attempts_are_deduplicated(self):
        sessions = self.root / ".fast-agent" / "sessions"
        original = _conversation(self.base, 1_000.0, 2)
        _write_session(sessions / "parent", original)
        forked = [*original, *_conversation(self.base + 1_000, 5_000.0, 1)]
        _write_session(sessions / "fork", forked, session_id="fork-id")

        dry = self._sweep(dry_run=True)
        self.assertEqual((dry.calls_imported, dry.calls_duplicate), (6, 4))
        summary = self._sweep()
        self.assertEqual((summary.calls_imported, summary.calls_duplicate), (6, 4))
        self.assertEqual(summary.turns_imported, 3)
        self.assertEqual(summary.tool_runs_imported, 3)
        report = store.query_report(self.db, window="all", now=NOW)
        self.assertEqual(report.total_calls, 6)
        self.assertEqual(report.phases.turns, 3)

    def test_incremental_skip_and_reimport_of_changed_files(self):
        session = self.root / "s1"
        _write_session(session, _conversation(self.base, 1_000.0, 1))
        self.assertEqual(self._sweep().calls_imported, 2)
        again = self._sweep()
        self.assertEqual((again.files_skipped_unchanged, again.calls_imported), (1, 0))

        _write_session(session, _conversation(self.base, 1_000.0, 2))  # resumed session
        resumed = self._sweep()
        self.assertEqual(resumed.files_skipped_unchanged, 0)
        self.assertEqual((resumed.calls_imported, resumed.calls_duplicate), (2, 2))
        self.assertEqual(resumed.turns_imported, 1)

    def test_dry_run_writes_nothing(self):
        _write_session(self.root / "s1", _conversation(self.base, 1_000.0, 2))
        dry = self._sweep(dry_run=True)
        self.assertTrue(dry.dry_run)
        self.assertEqual(
            (dry.calls_imported, dry.turns_imported, dry.tool_runs_imported), (4, 2, 2)
        )
        self.assertEqual(store.query_report(self.db, window="all", now=NOW).total_calls, 0)
        # Not marked as swept, so the real run still imports everything.
        self.assertEqual(self._sweep(dry_run=True).files_skipped_unchanged, 0)
        real = self._sweep()
        self.assertEqual(real.calls_imported, 4)
        after = self._sweep(dry_run=True)
        self.assertEqual((after.files_skipped_unchanged, after.calls_imported), (1, 0))

    def test_corrupt_and_foreign_files_are_skipped(self):
        session = self.root / "s1"
        _write_session(session, _conversation(self.base, 1_000.0, 1))
        (session / "history_broken.json").write_text("{not json")
        (session / "history_legacy.json").write_text(json.dumps([{"role": "user"}]))
        (session / "history_other.json").write_text(json.dumps({"messages": [{"x": 1}]}))
        bad_usage = _conversation(self.base + 500, 9_000.0, 1)
        bad_usage[1]["channels"]["fast-agent-usage"] = _text("{oops")
        (session / "history_badusage.json").write_text(json.dumps({"messages": bad_usage}))
        broken_session = self.root / "s2"
        broken_session.mkdir(parents=True)
        (broken_session / "session.json").write_text("not json")
        (broken_session / "history_agent.json").write_text(
            json.dumps({"messages": _conversation(self.base + 900, 20_000.0, 1)})
        )

        summary = self._sweep()
        self.assertEqual(summary.sessions_scanned, 2)
        self.assertEqual(summary.files_scanned, 6)
        self.assertEqual(summary.files_skipped_invalid, 3)
        self.assertEqual(summary.calls_imported, 2 + 1 + 2)
        report = store.query_report(self.db, window="all", now=NOW)
        self.assertEqual(report.total_sessions, 2)  # s2 falls back to its folder name

    def test_folded_process_polls_are_restored(self):
        clock = 1_000.0
        wait = ("process", {"action": "wait", "process_id": "p1", "wait_sec": 60})
        archived_request = _assistant(self.base + 10, clock + 10, tool_calls={"poll-1": wait})
        archived_result = _tool_result({"poll-1": 30_000.0})
        retained_request = _assistant(self.base + 50, clock + 50, tool_calls={"poll-2": wait})
        retained_result = _tool_result({"poll-2": 20_000.0})
        fold = {
            "audit": {
                "removed_exchanges": [
                    {"call_id": "poll-1", "request": archived_request, "result": archived_result}
                ],
                "retained_exchanges": [
                    {"call_id": "poll-2", "request": retained_request, "result": retained_result}
                ],
                "context_rewrites": [],
            }
        }
        folded_result = copy.deepcopy(retained_result)
        folded_result["channels"]["fast-agent-process-poll-fold"] = _text(fold)
        messages = [
            _user("wait for it"),
            retained_request,
            folded_result,
            _assistant(self.base + 80, clock + 80),
        ]
        record = perf.record_from_history(messages)
        self.assertEqual(len(record.calls), 3)
        self.assertEqual(
            [(tool.duration_ms, tool.process_wait) for tool in record.tools],
            [(30_000.0, True), (20_000.0, True)],
        )
        _write_session(self.root / "s1", messages)
        summary = self._sweep()
        self.assertEqual(summary.calls_imported, 3)
        report = store.query_report(self.db, window="all", now=NOW)
        self.assertEqual(report.tools, ())
        self.assertIsNotNone(report.process_waits)
        assert report.process_waits is not None
        self.assertEqual(report.process_waits.waits, 2)
        self.assertAlmostEqual(report.process_waits.total_ms, 50_000.0)
        self.assertEqual(report.raw_tools[0].process_wait_runs, 2)
        self.assertAlmostEqual(report.phases.process_ms, 50_000.0)

    def test_default_sweep_targets_fast_agent_sidecars_and_skips_nested_benchmarks(self):
        # Ordinary .fast-agent/sessions (including subagent child session)
        ordinary = self.root / "app" / ".fast-agent" / "sessions" / "s-ordinary"
        _write_session(ordinary, _conversation(self.base, 1_000.0, 1))
        _write_session(
            ordinary / "children" / "s-child",
            _conversation(self.base + 500, 2_000.0, 1),
        )
        # Nested benchmark tree outside .fast-agent/sessions
        bench = (
            self.root
            / "bench-run"
            / "jobs"
            / "run-1"
            / "trial-1"
            / "agent"
            / "fast-agent-home"
            / "sessions"
            / "s-bench"
        )
        _write_session(bench, _conversation(self.base + 1_000, 3_000.0, 1))
        # Cache dir with bundled example .fast-agent/sessions
        cached = self.root / ".cache" / "uv" / "pkg" / ".fast-agent" / "sessions" / "s-cached"
        _write_session(cached, _conversation(self.base + 1_500, 4_000.0, 1))

        # Default sweep only picks up ordinary .fast-agent/sessions (and its child)
        default_summary = self._sweep()
        self.assertFalse(default_summary.include_all)
        self.assertEqual(default_summary.sessions_scanned, 2)
        self.assertEqual(default_summary.calls_imported, 4)

        # Direct targeting of the benchmark sessions folder works without --all
        direct_summary = self._sweep(root=bench.parent, dry_run=True)
        self.assertEqual(direct_summary.sessions_scanned, 1)
        self.assertEqual(direct_summary.calls_imported, 2)

        # --all picks up nested benchmark trees too, while still pruning .cache
        all_summary = self._sweep(include_all=True)
        self.assertTrue(all_summary.include_all)
        self.assertEqual(all_summary.sessions_scanned, 3)
        self.assertEqual(all_summary.calls_imported, 2)  # ordinary ones already unchanged

    def test_sweep_emits_progress_and_honors_cancel_event(self):
        sessions = self.root / "app" / ".fast-agent" / "sessions"
        for idx in range(3):
            _write_session(
                sessions / f"s{idx}", _conversation(self.base + idx * 500, 1_000.0 + idx * 500, 1)
            )

        events = []
        summary = self._sweep(on_progress=events.append)
        self.assertEqual(summary.sessions_scanned, 3)
        self.assertGreaterEqual(len(events), 2)
        self.assertEqual(events[0].phase, "scanning")
        self.assertEqual(events[-1].sessions_scanned, 3)
        self.assertEqual(events[-1].calls_imported, 6)

        import threading

        cancel = threading.Event()
        cancel.set()
        cancelled = self._sweep(dry_run=True, cancel_event=cancel)
        self.assertEqual(cancelled.sessions_scanned, 0)
        self.assertEqual(cancelled.files_scanned, 0)


class PerfHistoryTests(unittest.TestCase):
    def test_settings_fall_back_to_llm_perf_config(self):
        config = perf.plugin_config(
            {"llm-perf": {"burst_ms": 250, "tps": "e2e"}, "usage-tracker": {"burst_ms": 750}}
        )
        settings = perf.Settings.from_config(config)
        self.assertEqual((settings.burst_ms, settings.tps), (750.0, "e2e"))
        self.assertEqual(perf.plugin_config(None), {})

    def test_split_user_turns_on_raw_messages(self):
        messages = _conversation(1.0, 10.0, 3)
        turns = perf.split_user_turns(messages)
        self.assertEqual([len(turn) for turn in turns], [4, 4, 4])
        self.assertEqual(len(perf.history_turns(messages)), 3)

    def test_dicts_and_fast_agent_messages_agree(self):
        try:
            from fast_agent.mcp.prompt_serialization import from_json
        except ImportError:
            self.skipTest("fast-agent not installed")
        messages = _conversation(1.0, 10.0, 2)
        objects = from_json(json.dumps({"messages": messages}))
        self.assertEqual(perf.history_turns(objects), perf.history_turns(messages))
        live = [attempt for message in objects for attempt in pricing.usage_attempts(message)]
        swept = [attempt for message in messages for attempt in sweep.message_attempts(message)]
        self.assertEqual(
            [store.attempt_hash(attempt) for attempt in live],
            [store.attempt_hash(attempt) for attempt in swept],
        )

    def test_inspect_tool_result_reads_is_error_without_touching_deprecated_isError(self):
        class _V2Result:
            def __init__(self, is_error: bool) -> None:
                self.is_error = is_error
                self.meta = None
                self.content = []

            @property
            def isError(self) -> bool:
                raise AssertionError("deprecated isError property should not be accessed")

        self.assertFalse(perf._inspect_tool_result(_V2Result(False), "shell")[0])
        self.assertTrue(perf._inspect_tool_result(_V2Result(True), "shell")[0])
        self.assertTrue(perf._inspect_tool_result({"isError": True}, "shell")[0])
        self.assertFalse(perf._inspect_tool_result({"is_error": False}, "shell")[0])


class ReviewHardeningTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._temp.name)
        self.db = self.tmp / "usage.sqlite3"
        self.catalog = pricing.load_catalog()

    def tearDown(self):
        self._temp.cleanup()

    def test_sweep_rejects_symlinked_session_and_history_files(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        secret_history = outside / "history_secret.json"
        base = NOW.timestamp() - 3600
        secret_history.write_text(
            json.dumps({"messages": _conversation(base, 100.0, 1)}),
            encoding="utf-8",
        )
        secret_session = outside / "session.json"
        secret_session.write_text(
            json.dumps({"session_id": "escaped", "continuation": {}}),
            encoding="utf-8",
        )

        root = self.tmp / "sessions"
        s1 = root / "s1"
        s1.mkdir(parents=True)
        (s1 / "session.json").write_text(
            json.dumps(
                {
                    "session_id": "s1",
                    "continuation": {"agents": {"a": {"history_file": "history_a.json"}}},
                }
            ),
            encoding="utf-8",
        )
        (s1 / "history_a.json").symlink_to(secret_history)

        s2 = root / "s2"
        s2.mkdir(parents=True)
        (s2 / "session.json").symlink_to(secret_session)
        (s2 / "history_b.json").write_text(
            json.dumps({"messages": _conversation(base + 100, 200.0, 1)}),
            encoding="utf-8",
        )

        summary = sweep.sweep_directory(root, db_path=self.db, catalog=self.catalog)
        self.assertEqual(summary.calls_imported, 0)
        self.assertEqual(summary.files_scanned, 0)

    def test_dry_run_sweep_never_acquires_write_lock(self):
        root = self.tmp / "sessions"
        base = NOW.timestamp() - 3600
        _write_session(root / "s1", _conversation(base, 100.0, 1))
        # Initialize schema first, then hold a write lock on a separate connection.
        with store.UsageStore(self.db):
            pass
        locker = sqlite3.connect(self.db, isolation_level=None)
        try:
            locker.execute("BEGIN IMMEDIATE")
            summary = sweep.sweep_directory(
                root, db_path=self.db, catalog=self.catalog, dry_run=True
            )
            self.assertEqual(summary.calls_imported, 2)
        finally:
            locker.execute("ROLLBACK")
            locker.close()

    def test_bounded_gzip_decompression_rejects_oversized_payload(self):
        compressed = gzip.compress(b"A" * 2048)
        self.assertEqual(pricing_updater._decompress_gzip_bounded(compressed, 4096), b"A" * 2048)
        with self.assertRaises(ValueError):
            pricing_updater._decompress_gzip_bounded(compressed, 1024)

    def test_duplicate_backfills_missing_session_id_without_overwriting_existing(self):
        base = NOW.timestamp() - 3600
        messages = _conversation(base, 100.0, 1)
        attempts = [attempt for msg in messages for attempt in sweep.message_attempts(msg)]
        turn_record = perf.record_from_history(messages)

        with store.UsageStore(self.db) as usage_store:
            first = usage_store.record_turn(
                session_id=None,
                attempts=attempts,
                turn_record=turn_record,
                catalog=self.catalog,
                source="live",
            )
            self.assertEqual(first.calls_inserted, 2)
            # Duplicate with real session_id backfills NULL without counting as a new insert.
            backfill = usage_store.record_turn(
                session_id="sess-orig",
                attempts=attempts,
                turn_record=turn_record,
                catalog=self.catalog,
                source="sweep",
            )
            self.assertEqual(backfill.calls_inserted, 0)
            self.assertEqual(backfill.calls_duplicate, 2)
            # Later fork must preserve sess-orig.
            usage_store.record_turn(
                session_id="sess-fork",
                attempts=attempts,
                turn_record=turn_record,
                catalog=self.catalog,
                source="sweep",
            )
            call_sids = {
                row[0] for row in usage_store.connection.execute("SELECT session_id FROM calls")
            }
            turn_sids = {
                row[0] for row in usage_store.connection.execute("SELECT session_id FROM turns")
            }
            tool_sids = {
                row[0] for row in usage_store.connection.execute("SELECT session_id FROM tool_runs")
            }
            self.assertEqual(call_sids, {"sess-orig"})
            self.assertEqual(turn_sids, {"sess-orig"})
            self.assertEqual(tool_sids, {"sess-orig"})

    def test_sweep_timing_only_turn_uses_session_timestamp_or_skips(self):
        # Timing-only assistant message (no fast-agent-usage attempts).
        timing_only = [
            _user("ping"),
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "pong"}],
                "channels": {
                    "fast-agent-timing": _text(
                        {
                            "start_time": 10.0,
                            "end_time": 12.0,
                            "duration_ms": 2000.0,
                            "ttft_ms": 300.0,
                        }
                    )
                },
            },
        ]
        root = self.tmp / "sessions"
        dated = root / "dated"
        dated.mkdir(parents=True)
        (dated / "session.json").write_text(
            json.dumps({"session_id": "dated", "last_activity": "2025-01-15T12:00:00Z"}),
            encoding="utf-8",
        )
        (dated / "history_a.json").write_text(
            json.dumps({"messages": timing_only}), encoding="utf-8"
        )

        undated = root / "undated"
        undated.mkdir(parents=True)
        (undated / "session.json").write_text(
            json.dumps({"session_id": "undated"}), encoding="utf-8"
        )
        (undated / "history_b.json").write_text(
            json.dumps({"messages": timing_only}), encoding="utf-8"
        )

        summary = sweep.sweep_directory(root, db_path=self.db, catalog=self.catalog)
        self.assertEqual(summary.turns_imported, 1)
        with store.UsageStore(self.db) as usage_store:
            rows = usage_store.connection.execute(
                "SELECT session_id, timestamp_ms FROM turns"
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "dated")
        expected_ms = round(datetime(2025, 1, 15, 12, 0, tzinfo=UTC).timestamp() * 1000)
        self.assertEqual(rows[0][1], expected_ms)

    def test_schema_v1_migrates_to_v2_and_has_unpriced(self):
        # Create a v1 database without idx_calls_unpriced.
        conn = sqlite3.connect(self.db, isolation_level=None)
        conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        for stmt in store._SCHEMA.split(";"):
            text = stmt.strip()
            if text and "idx_calls_unpriced" not in text:
                conn.execute(text)
        conn.execute("INSERT INTO schema_meta (key, value) VALUES ('schema_version', '1')")
        conn.close()

        with store.UsageStore(self.db) as usage_store:
            ver = usage_store.connection.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()[0]
            self.assertEqual(int(ver), store.SCHEMA_VERSION)
            idx = usage_store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_calls_unpriced'"
            ).fetchone()
            self.assertIsNotNone(idx)
            self.assertFalse(usage_store.has_unpriced())
            unknown = pricing.UsageAttempt(
                provider="anthropic",
                usage_schema="anthropic_messages",
                model="claude-future-unknown-model",
                prompt=pricing.PromptUsage(total=100, uncached=100),
                completion=pricing.CompletionUsage(total=50),
                timestamp=NOW.timestamp() - 60,
            )
            usage_store.record_turn(
                session_id="s1",
                attempts=(unknown,),
                turn_record=None,
                catalog=self.catalog,
            )
            self.assertTrue(usage_store.has_unpriced())


class PtcTrackingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "usage.sqlite3"
        self.catalog = pricing.load_catalog()

    def test_record_from_history_extracts_ptc_nested_calls_errors_and_wait_phases(self):
        timing_payload = {
            "start_time": 10.0,
            "end_time": 11.0,
            "duration_ms": 1000.0,
            "ttft_ms": 200.0,
        }
        usage_payload = {
            "schema": "fast-agent.usage/v2",
            "provider_attempts": [{"model": "gemini-4-neon"}],
        }
        tool_timing_r1 = {
            "c1": {"timing_ms": 2000.0},
            "c2": {"timing_ms": 50.0},
            "c4": {"timing_ms": 10.0},
            "c5": {"timing_ms": 10.0},
            "c6": {"timing_ms": 50.0},
        }
        timing_r2 = {
            "start_time": 13.0,
            "end_time": 14.0,
            "duration_ms": 1000.0,
            "ttft_ms": 100.0,
        }
        history = [
            {
                "role": "assistant",
                "channels": {
                    "fast-agent-timing": [{"type": "text", "text": json.dumps(timing_payload)}],
                    "fast-agent-usage": [{"type": "text", "text": json.dumps(usage_payload)}],
                },
                "tool_calls": {
                    "c1": {"params": {"name": "exec_sandbox", "arguments": {"code": "pass"}}},
                    "c2": {"params": {"name": "exec_sandbox", "arguments": {"code": "1+1"}}},
                    "c4": {"params": {"name": "exec_sandbox", "arguments": {"code": "bad"}}},
                    "c5": {"params": {"name": "exec_sandbox", "arguments": {"code": "bad"}}},
                    "c6": {"params": {"name": "exec_sandbox", "arguments": {"code": "bad"}}},
                },
            },
            {
                "role": "user",
                "channels": {
                    "fast-agent-tool-timing": [{"type": "text", "text": json.dumps(tool_timing_r1)}]
                },
                "tool_results": {
                    "c1": {
                        "isError": False,
                        "content": [{"type": "text", "text": "ok"}],
                        "_meta": {
                            "fast-agent-ptc-calls": [
                                {
                                    "tool": "shell",
                                    "arguments": {"command": "pytest"},
                                    "status": "completed",
                                    "start_offset_ms": 0.0,
                                    "timing_ms": 1000.0,
                                },
                                {
                                    "tool": "process",
                                    "arguments": {"action": "wait", "process_id": "p1"},
                                    "status": "completed",
                                    "start_offset_ms": 1000.0,
                                    "timing_ms": 1000.0,
                                },
                            ]
                        },
                    },
                    "c2": {
                        "isError": False,
                        "content": [{"type": "text", "text": "2"}],
                        "_meta": {"fast-agent-ptc-calls": []},
                    },
                    "c4": {
                        "isError": True,
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "Syntax error; the code was NOT executed.\n"
                                    "SyntaxError: invalid syntax"
                                ),
                            }
                        ],
                    },
                    "c5": {
                        "isError": True,
                        "content": [
                            {
                                "type": "text",
                                "text": "Type check failed; the code was NOT executed.",
                            }
                        ],
                    },
                    "c6": {
                        "isError": True,
                        "content": [{"type": "text", "text": "RuntimeError: boom"}],
                        "_meta": {"fast-agent-ptc-calls": []},
                    },
                },
            },
            {
                "role": "assistant",
                "channels": {
                    "fast-agent-timing": [{"type": "text", "text": json.dumps(timing_r2)}],
                    "fast-agent-usage": [{"type": "text", "text": json.dumps(usage_payload)}],
                },
                "tool_calls": {
                    "c3": {"params": {"name": "exec_sandbox", "arguments": {"code": "wait"}}},
                },
            },
            {
                "role": "user",
                "channels": {
                    "fast-agent-tool-timing": [
                        {"type": "text", "text": json.dumps({"c3": {"timing_ms": 1000.0}})}
                    ]
                },
                "tool_results": {
                    "c3": {
                        "isError": False,
                        "content": [{"type": "text", "text": "done"}],
                        "_meta": {
                            "fast-agent-ptc-calls": [
                                {
                                    "tool": "process",
                                    "arguments": {"wait_sec": 20, "process_id": "p2"},
                                    "status": "completed",
                                }
                            ]
                        },
                    }
                },
            },
        ]

        record = perf.record_from_history(history)
        self.assertEqual(len(record.tools), 6)

        mixed, py_only, syn_err, tc_err, rt_err, wait_only = record.tools
        self.assertEqual(mixed.model, "gemini-4-neon")
        self.assertTrue(mixed.has_ptc_meta)
        self.assertFalse(mixed.process_wait)
        self.assertEqual([n.tool_name for n in mixed.nested_calls], ["shell", "process"])
        self.assertTrue(mixed.nested_calls[1].process_wait)
        self.assertAlmostEqual(mixed.nested_calls[1].start_offset_ms or 0.0, 1000.0)
        self.assertAlmostEqual(mixed.nested_calls[1].duration_ms or 0.0, 1000.0)

        self.assertTrue(py_only.has_ptc_meta)
        self.assertEqual(py_only.nested_calls, ())
        self.assertFalse(py_only.is_error)

        self.assertTrue(wait_only.process_wait)
        self.assertTrue(wait_only.nested_calls[0].process_wait)

        self.assertTrue(syn_err.is_error)
        self.assertEqual(syn_err.error_kind, "syntax")
        self.assertEqual(tc_err.error_kind, "typecheck")
        self.assertEqual(rt_err.error_kind, "runtime")

        # Mixed timed cell splits 1000ms tools + 1000ms process; wait_only adds 1000ms process.
        phases = perf.turn_phases(record, 500.0)
        self.assertAlmostEqual(phases["process"], 2000.0, places=1)
        self.assertAlmostEqual(phases["tools"], 1000.0, places=1)

    def test_schema_v2_migrates_to_v3_and_unwraps_ptc_with_raw_and_model_filter(self):
        # Create a genuine v2 database without v3 tool_runs columns or ptc_tool_calls.
        conn = sqlite3.connect(self.db, isolation_level=None)
        conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        for stmt in store._SCHEMA.split(";"):
            text = stmt.strip()
            if not text or "ptc_tool_calls" in text or "idx_ptc_calls" in text:
                continue
            if "CREATE TABLE IF NOT EXISTS tool_runs" in text:
                conn.execute(
                    "CREATE TABLE tool_runs ("
                    "run_hash TEXT PRIMARY KEY, turn_hash TEXT NOT NULL, "
                    "timestamp_ms INTEGER NOT NULL, session_id TEXT, "
                    "tool_name TEXT NOT NULL, duration_ms REAL NOT NULL, "
                    "is_process_wait INTEGER NOT NULL DEFAULT 0, "
                    "source TEXT NOT NULL DEFAULT 'live')"
                )
            else:
                conn.execute(text)
        conn.execute("INSERT INTO schema_meta (key, value) VALUES ('schema_version', '2')")
        conn.close()

        turn_a = perf.TurnRecord(
            calls=(_call(0.0, 1000.0, 100.0, 50),),
            tools=(
                # Parallel timed PTC cell: 1200 + 400 = 1600ms inside 1300ms wall time.
                perf.ToolRun(
                    name="exec_sandbox",
                    start=1.0,
                    duration_ms=1300.0,
                    process_wait=False,
                    model="gemini-4-neon",
                    has_ptc_meta=True,
                    nested_calls=(
                        perf.PtcNestedCall("shell", 1200.0, 0.0, False, "completed", False),
                        perf.PtcNestedCall("edit_file", 400.0, 100.0, False, "completed", False),
                    ),
                ),
                # Legacy 1-call untimed PTC cell: inherits 500ms outer duration.
                perf.ToolRun(
                    name="exec_sandbox",
                    start=2.4,
                    duration_ms=500.0,
                    process_wait=False,
                    model="gemini-4-neon",
                    has_ptc_meta=True,
                    nested_calls=(
                        perf.PtcNestedCall("shell", None, None, False, "completed", False),
                    ),
                ),
                # Legacy 2-call untimed PTC cell: stays unmeasured.
                perf.ToolRun(
                    name="exec_sandbox",
                    start=3.0,
                    duration_ms=1000.0,
                    process_wait=False,
                    model="gemini-4-neon",
                    has_ptc_meta=True,
                    nested_calls=(
                        perf.PtcNestedCall("shell", None, None, False, "completed", False),
                        perf.PtcNestedCall(
                            "write_text_file", None, None, False, "completed", False
                        ),
                    ),
                ),
                # Pure-Python cell.
                perf.ToolRun(
                    name="exec_sandbox",
                    start=4.1,
                    duration_ms=100.0,
                    process_wait=False,
                    model="gemini-4-neon",
                    has_ptc_meta=True,
                    nested_calls=(),
                ),
                # Typecheck error cell on another model.
                perf.ToolRun(
                    name="exec_sandbox",
                    start=4.3,
                    duration_ms=20.0,
                    process_wait=False,
                    model="gpt-6.2-sol",
                    is_error=True,
                    error_kind="typecheck",
                    has_ptc_meta=False,
                    nested_calls=(),
                ),
            ),
        )

        with store.UsageStore(self.db) as usage_store:
            ver = usage_store.connection.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()[0]
            self.assertEqual(int(ver), 3)
            usage_store.record_turn(
                session_id="s-ptc",
                attempts=(_attempt("gemini-4-neon", prompt=2000),),
                turn_record=turn_a,
                catalog=self.catalog,
            )
            # Re-recording must deduplicate ptc_tool_calls too.
            usage_store.record_turn(
                session_id="s-ptc",
                attempts=(_attempt("gemini-4-neon", prompt=2000),),
                turn_record=turn_a,
                catalog=self.catalog,
            )
            ptc_count = usage_store.connection.execute(
                "SELECT COUNT(*) FROM ptc_tool_calls"
            ).fetchone()[0]
            self.assertEqual(ptc_count, 5)

        report = store.query_report(self.db, window="7d", now=NOW)
        self.assertIsNotNone(report.ptc)
        assert report.ptc is not None
        self.assertEqual(report.ptc.cells, 5)
        self.assertEqual(report.ptc.nested_calls, 5)
        self.assertEqual(report.ptc.pure_python_cells, 1)
        self.assertEqual(report.ptc.error_cells, 1)
        self.assertEqual(report.ptc.typecheck_errors, 1)
        self.assertTrue(report.ptc.has_parallel_overlap)
        self.assertEqual(report.ptc.unmeasured_nested_calls, 2)
        self.assertEqual([m.model for m in report.ptc.by_model], ["gemini-4-neon", "gpt-6.2-sol"])

        # Raw view preserves single exec_sandbox row with exact wall time.
        self.assertEqual([t.tool_name for t in report.raw_tools], ["exec_sandbox"])
        self.assertEqual(report.raw_tools[0].runs, 5)

        # Unwrapped view exposes shell (2 timed + 1 unmeasured), edit_file, sandbox (python),
        # and write_text_file (0 timed).
        by_name = {t.tool_name: t for t in report.tools}
        self.assertNotIn("exec_sandbox", by_name)
        self.assertEqual(by_name["shell"].runs, 3)
        self.assertEqual(by_name["shell"].timed_runs, 2)
        self.assertAlmostEqual(by_name["shell"].total_ms, 1700.0, places=1)
        self.assertEqual(by_name["write_text_file"].runs, 1)
        self.assertEqual(by_name["write_text_file"].timed_runs, 0)
        self.assertEqual(by_name["sandbox (python)"].runs, 1)

        # Model filter narrows PTC summary and tools cleanly.
        filtered = store.query_report(self.db, window="7d", model_filter="gpt-6.2", now=NOW)
        self.assertIsNotNone(filtered.ptc)
        assert filtered.ptc is not None
        self.assertEqual(filtered.ptc.cells, 1)
        self.assertEqual(filtered.ptc.typecheck_errors, 1)
        self.assertEqual([m.model for m in filtered.ptc.by_model], ["gpt-6.2-sol"])

    def test_case_normalization_and_process_wait_percentiles(self):
        turn = perf.TurnRecord(
            calls=(_call(0.0, 1000.0, 100.0, 50),),
            tools=(
                perf.ToolRun("Bash", 1.0, 400.0, False, model="claude-opus-5.5"),
                perf.ToolRun("bash", 1.5, 600.0, False, model="claude-opus-5.5"),
                perf.ToolRun("github__getPullRequest", 2.2, 200.0, False, model="claude-opus-5.5"),
                perf.ToolRun("Process", 2.5, 10_000.0, True, model="claude-opus-5.5"),
                perf.ToolRun("process", 13.0, 30_000.0, True, model="claude-opus-5.5"),
                perf.ToolRun("poll_process", 44.0, 90_000.0, True, model="claude-opus-5.5"),
                perf.ToolRun("process", 135.0, 50.0, False, model="claude-opus-5.5"),
            ),
        )
        with store.UsageStore(self.db) as usage_store:
            usage_store.record_turn(
                session_id="s-norm",
                attempts=(_attempt("claude-opus-5.5", prompt=1000),),
                turn_record=turn,
                catalog=self.catalog,
            )

        report = store.query_report(self.db, window="7d", now=NOW)
        # Default view merges Bash+bash -> bash, keeps camelCase MCP names,
        # and keeps non-wait process management separate from blocking waits.
        by_name = {t.tool_name: t for t in report.tools}
        self.assertEqual(set(by_name), {"bash", "github__getPullRequest", "process"})
        self.assertEqual(by_name["bash"].runs, 2)
        self.assertAlmostEqual(by_name["bash"].total_ms, 1000.0)
        self.assertEqual(by_name["process"].runs, 1)
        self.assertAlmostEqual(by_name["process"].total_ms, 50.0)

        # Blocking waits move into process_waits with percentiles and merged Process/process.
        waits = report.process_waits
        self.assertIsNotNone(waits)
        assert waits is not None
        self.assertEqual(waits.waits, 3)
        self.assertEqual(waits.timed_waits, 3)
        self.assertAlmostEqual(waits.total_ms, 130_000.0)
        self.assertAlmostEqual(waits.p50_ms or 0.0, 30_000.0)
        self.assertAlmostEqual(waits.max_ms or 0.0, 90_000.0)
        self.assertEqual(waits.by_tool, (("process", 2), ("poll_process", 1)))

        # Raw view preserves exact recorded names and wait runs.
        raw_names = {t.tool_name for t in report.raw_tools}
        self.assertEqual(
            raw_names,
            {"Bash", "bash", "github__getPullRequest", "Process", "process", "poll_process"},
        )


# =========================================================================== remotes


_FAKE_SSH = """#!/bin/sh
# Stand-in for ssh: run the "remote" command line locally, stdin passed through.
for arg; do last=$arg; done
echo "$*" >> "$FAKE_SSH_LOG"
case "$*" in
  *unreachable*) echo "ssh: connect timed out" >&2; exit 255;;
  *flaky*) head -c 64 /dev/urandom; echo "Connection reset by peer" >&2; exit 255;;
esac
exec sh -c "$last"
"""


class RemoteAggregationTests(_TempDirTest):
    def setUp(self):
        super().setUp()
        self.catalog = _test_catalog()
        self.data_dir = self.db.parent
        self.other = self.tmp / "other-machine" / "usage.sqlite3"
        ts = NOW.timestamp()
        shared = _attempt(timestamp=ts - 3600, prompt=5_000)  # a session synced to both machines
        self._record(self.db, "local-1", shared, _attempt(timestamp=ts - 60, prompt=1_000))
        self._record(
            self.other,
            "local-1",
            shared,
        )
        self._record(
            self.other,
            "remote-1",
            _attempt("gpt-test", provider="openai", timestamp=ts - 7200, prompt=20_000),
            _attempt("gpt-test", provider="openai", timestamp=ts - 9 * 86400, prompt=7_000),
        )

    def _record(self, db, session_id, *attempts):
        store.record_turn(
            db, session_id=session_id, attempts=attempts, turn_record=None, catalog=self.catalog
        )

    def _report(self, machine=None, **kwargs):
        sources, label = remotes.report_sources(self.data_dir, local_name="desk", machine=machine)
        kwargs.setdefault("window", "7d")
        return store.query_report(self.db, now=NOW, sources=sources, machine_filter=label, **kwargs)

    def _add(self, **kwargs):
        remote = remotes.add_remote(self.data_dir, str(self.other), local_name="desk", **kwargs)
        return remote, remotes.sync_remotes(self.data_dir)

    def test_path_remote_syncs_and_reports_merge_without_double_counting(self):
        remote, statuses = self._add(name="laptop")
        self.assertEqual((remote.kind, remote.name), ("path", "laptop"))
        self.assertEqual([s.last_error for s in statuses], [None])
        self.assertEqual(statuses[0].calls, 3)
        self.assertTrue(statuses[0].changed)
        mirror = remotes.mirror_path(self.data_dir, "laptop")
        self.assertTrue(mirror.is_file())

        report = self._report()
        # 2 local + 2 remote in the window; the shared call counts once, for this machine.
        self.assertEqual(report.total_calls, 3)
        self.assertEqual(report.total_sessions, 2)
        self.assertEqual(
            [(m.name, m.is_local, m.calls, m.total_tokens) for m in report.machines],
            [("desk", True, 2, 6_200), ("laptop", False, 1, 20_100)],
        )
        self.assertIsNotNone(report.machines[1].synced_at)
        self.assertEqual({m.model for m in report.models}, {"claude-test", "gpt-test"})
        self.assertEqual(
            sorted((p.model, p.total_tokens) for p in report.daily_models),
            [("claude-test", 6_200), ("gpt-test", 20_100)],
        )
        self.assertEqual(sum(map(sum, report.rhythm.hourly)), 3)
        # The previous 7 days hold only the older remote call.
        assert report.previous is not None
        self.assertEqual((report.previous.calls, report.previous.total_tokens), (1, 7_100))

        # The local ledger itself is untouched by aggregation.
        self.assertEqual(store.query_report(self.db, window="7d", now=NOW).total_calls, 2)
        self.assertEqual(store.query_report(self.db, window="7d", now=NOW).machines, ())

    def test_machine_filter(self):
        self._add(name="laptop")
        only_remote = self._report(machine="laptop")
        # Narrowed to the remote, the shared session is that machine's own.
        self.assertEqual((only_remote.total_calls, only_remote.machine_filter), (2, "laptop"))
        only_local = self._report(machine="local")
        self.assertEqual((only_local.total_calls, only_local.machine_filter), (2, "desk"))
        self.assertEqual(self._report(machine="DESK").total_calls, 2)
        with self.assertRaisesRegex(ValueError, "Unknown machine"):
            self._report(machine="nope")

    def test_resync_is_skipped_when_unchanged_and_picks_up_new_calls(self):
        self._add(name="laptop")
        again = remotes.sync_remotes(self.data_dir)
        self.assertIs(again[0].changed, False)
        self._record(self.other, "remote-2", _attempt(timestamp=NOW.timestamp() - 30))
        updated = remotes.sync_remotes(self.data_dir)
        self.assertTrue(updated[0].changed)
        self.assertEqual(updated[0].calls, 4)
        self.assertEqual(self._report().total_calls, 4)
        # Fresh mirrors are left alone by the staleness-bounded (automatic) sync.
        self._record(self.other, "remote-3", _attempt(timestamp=NOW.timestamp() - 20))
        self.assertFalse(remotes.needs_sync(self.data_dir, 3600))
        skipped = remotes.sync_remotes(self.data_dir, max_age_seconds=3600)
        self.assertIsNone(skipped[0].changed)
        self.assertTrue(remotes.needs_sync(self.data_dir, 0))

    def test_failed_sync_keeps_the_last_mirror(self):
        self._add(name="laptop")
        self.other.unlink()
        for suffix in ("-wal", "-shm"):
            Path(str(self.other) + suffix).unlink(missing_ok=True)
        failed = remotes.sync_remotes(self.data_dir)
        self.assertIn("no usage ledger", failed[0].last_error)
        self.assertIsNotNone(failed[0].synced_at)
        report = self._report()
        self.assertEqual(report.total_calls, 3)
        self.assertIn("no usage ledger", report.machines[1].last_error)

    def test_unusable_mirrors_are_reported_not_fatal(self):
        remotes.add_remote(self.data_dir, str(self.other), name="laptop", local_name="desk")
        report = self._report()  # never synced
        self.assertEqual(report.total_calls, 2)
        self.assertEqual(report.machines[1].last_error, "not synced yet")

        remotes.sync_remotes(self.data_dir)
        mirror = remotes.mirror_path(self.data_dir, "laptop")
        with contextlib.closing(sqlite3.connect(mirror)) as connection:
            connection.execute("UPDATE schema_meta SET value='999' WHERE key='schema_version'")
            connection.commit()
        report = self._report()
        self.assertEqual(report.total_calls, 2)
        self.assertIn("newer than supported", report.machines[1].last_error)

        mirror.write_bytes(b"this is not a database")
        self.assertEqual(self._report().total_calls, 2)

    def test_registry_validation_and_removal(self):
        for bad in ("-oProxyCommand=evil", "two words", ""):
            with self.assertRaises(ValueError):
                remotes.add_remote(self.data_dir, bad)
        with self.assertRaisesRegex(ValueError, "names this machine"):
            remotes.add_remote(self.data_dir, "desk.lan", local_name="desk")
        with self.assertRaisesRegex(ValueError, "Invalid remote name"):
            remotes.add_remote(self.data_dir, "host", name="../escape")
        self.assertEqual(remotes.default_name("me@build-box.example.com"), "build-box")
        self.assertEqual(remotes.default_name("/mnt/nas/usage.sqlite3"), "nas")

        self._add(name="laptop")
        mirror = remotes.mirror_path(self.data_dir, "laptop")
        self.assertEqual(mirror.stat().st_mode & 0o777, 0o600)
        self.assertTrue(remotes.remove_remote(self.data_dir, "LAPTOP"))
        self.assertFalse(mirror.exists())
        self.assertFalse(remotes.remove_remote(self.data_dir, "laptop"))
        self.assertEqual(remotes.report_sources(self.data_dir, local_name="desk"), ([], None))

    def _stub_ssh(self):
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "ssh"
        fake.write_text(_FAKE_SSH, encoding="utf-8")
        fake.chmod(0o755)
        old_path = os.environ["PATH"]
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{old_path}"
        self.addCleanup(os.environ.__setitem__, "PATH", old_path)
        self.ssh_log = self.tmp / "ssh.log"
        os.environ["FAKE_SSH_LOG"] = str(self.ssh_log)
        self.addCleanup(os.environ.pop, "FAKE_SSH_LOG", None)

    @unittest.skipIf(os.name == "nt", "needs a POSIX shell")
    def test_ssh_remote_end_to_end_with_stub_ssh(self):
        self._stub_ssh()
        remote = remotes.add_remote(
            self.data_dir, "me@build-box", db_path=str(self.other), local_name="desk"
        )
        self.assertEqual((remote.kind, remote.name), ("ssh", "build-box"))
        first = remotes.sync_remotes(self.data_dir)
        self.assertEqual((first[0].last_error, first[0].calls, first[0].changed), (None, 3, True))
        self.assertTrue(first[0].host)
        self.assertEqual(self._report().total_calls, 3)
        self.assertIs(remotes.sync_remotes(self.data_dir)[0].changed, False)

        remotes.add_remote(self.data_dir, "unreachable", local_name="desk")
        remotes.add_remote(self.data_dir, "empty-box", db_path=str(self.tmp / "nope.sqlite3"))
        by_name = {s.name: s for s in remotes.sync_remotes(self.data_dir)}
        self.assertIn("timed out", by_name["unreachable"].last_error)
        self.assertIn("no usage ledger", by_name["empty-box"].last_error)
        self.assertIsNone(by_name["build-box"].last_error)
        self.assertEqual(list(remotes.remotes_dir(self.data_dir).glob(".*")), [])  # no temp files

    @unittest.skipIf(os.name == "nt", "needs a POSIX shell")
    def test_disconnected_remote_never_breaks_reports(self):
        self._stub_ssh()
        # Off the LAN the first address is dead; the fallback reaches the same machine.
        remote = remotes.add_remote(
            self.data_dir,
            "unreachable-lan",
            name="box",
            fallbacks=["me@build-box", "unreachable-lan"],
            db_path=str(self.other),
            local_name="desk",
        )
        self.assertEqual(remote.fallbacks, ("me@build-box",))
        self.assertEqual(remotes.load_remotes(self.data_dir)[0].fallbacks, ("me@build-box",))
        synced = remotes.sync_remotes(self.data_dir, connect_timeout=1)
        self.assertEqual((synced[0].last_error, synced[0].calls), (None, 3))
        self.assertIn("ConnectTimeout=1", self.ssh_log.read_text())
        self.assertIn("BatchMode=yes", self.ssh_log.read_text())
        mirror = remotes.mirror_path(self.data_dir, "box")
        good = mirror.read_bytes()

        # Fully offline: every address fails, the mirror and the report survive.
        remotes.add_remote(
            self.data_dir, "unreachable-lan", name="gone", fallbacks=["unreachable-vpn"]
        )
        registry = remotes.remotes_dir(self.data_dir) / remotes.REGISTRY_FILENAME
        payload = json.loads(registry.read_text())
        payload["remotes"][0].update(target="unreachable-lan", fallbacks=["unreachable-vpn"])
        registry.write_text(json.dumps(payload))
        offline = {s.name: s for s in remotes.sync_remotes(self.data_dir)}
        self.assertIn("timed out", offline["box"].last_error)
        self.assertIsNotNone(offline["box"].synced_at)
        self.assertEqual(mirror.read_bytes(), good)
        report = self._report()
        self.assertEqual(report.total_calls, 3)
        self.assertEqual(
            [(m.name, m.calls, bool(m.last_error)) for m in report.machines],
            [("desk", 2, False), ("box", 1, True), ("gone", 0, True)],
        )
        # A machine that just failed is not retried by the automatic refresh...
        before = self.ssh_log.read_text()
        self.assertFalse(remotes.needs_sync(self.data_dir, 1800))
        remotes.sync_remotes(self.data_dir, max_age_seconds=1800)
        self.assertEqual(self.ssh_log.read_text(), before)

        # A connection dropped mid-transfer leaves garbage: it must not replace the mirror.
        payload["remotes"][0].update(target="flaky", fallbacks=[])
        registry.write_text(json.dumps(payload))
        dropped = remotes.sync_remotes(self.data_dir, names=["box"])
        self.assertIn("reset by peer", dropped[0].last_error)
        self.assertEqual(mirror.read_bytes(), good)
        self.assertEqual(self._report().total_calls, 3)
        self.assertEqual(list(remotes.remotes_dir(self.data_dir).glob(".*")), [])

        for bad in (["-oProxyCommand=x"], ["/a/path"], ["two words"]):
            with self.assertRaises(ValueError):
                remotes.add_remote(self.data_dir, "host", fallbacks=bad)
        with self.assertRaisesRegex(ValueError, "only applies to SSH"):
            remotes.add_remote(self.data_dir, str(self.other), name="p", fallbacks=["h"])


if __name__ == "__main__":
    unittest.main()
