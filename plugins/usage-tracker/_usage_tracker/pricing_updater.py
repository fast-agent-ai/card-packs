"""Cheap, non-blocking, ETag-based pricing catalog updates.

Downloaded catalogs live in the central usage directory (``~/.fast-agent/usage`` by
default), never in the plugin installation. The active catalog is whichever of the
bundled and cached catalogs has the newer ``catalog_version``.
"""

from __future__ import annotations

import contextlib
import json
import zlib
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .models import (
    DEFAULT_PRICING_URL,
    PLUGIN_NAME,
    PLUGIN_VERSION,
    SUPPORTED_PRICING_SCHEMAS,
    PricingUpdateStatus,
)
from .pricing import BUNDLED_CATALOG_PATH, PricingCatalog, parse_catalog

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

CATALOG_FILENAME = "pricing_catalog.json"
STATE_FILENAME = "pricing_state.json"
UPGRADE_NOTICE = f"New pricing rules available — run /plugins update {PLUGIN_NAME}"
CHECK_INTERVAL_S = 24 * 60 * 60
UNPRICED_CHECK_INTERVAL_S = 60 * 60
_MAX_CATALOG_BYTES = 16 * 1024 * 1024

type Opener = Callable[..., Any]
"""``urllib.request.urlopen``-compatible callable: ``opener(request, timeout=...)``."""


# --------------------------------------------------------------------------- paths


def default_usage_dir() -> Path:
    return Path.home() / ".fast-agent" / "usage"


def resolve_data_dir(
    config: Mapping[str, object] | None = None,
    data_dir: Path | None = None,
) -> Path:
    """Explicit ``data_dir``, then config ``data_dir``, then ``db_path``'s parent."""
    if data_dir is not None:
        return Path(data_dir).expanduser()
    config = config or {}
    configured = config.get("data_dir")
    if isinstance(configured, str | os.PathLike) and str(configured).strip():
        return Path(configured).expanduser()
    db_path = config.get("db_path")
    if isinstance(db_path, str | os.PathLike) and str(db_path).strip():
        return Path(db_path).expanduser().parent
    return default_usage_dir()


# --------------------------------------------------------------------------- versions


def version_key(version: str) -> tuple[int, ...] | None:
    """``"2026-11-20.1"`` -> ``(2026, 11, 20, 1)``; ``None`` when not purely numeric."""
    parts = [part for part in re.split(r"[^0-9A-Za-z]+", version.strip()) if part]
    if not parts or not all(part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def compare_versions(left: str, right: str) -> int:
    """Negative, zero, or positive as ``left`` is older, equal, or newer than ``right``.

    Numeric dotted/dashed versions compare part-wise (missing parts are zero);
    anything else falls back to plain string comparison.
    """
    left_key, right_key = version_key(left), version_key(right)
    if left_key is not None and right_key is not None:
        width = max(len(left_key), len(right_key))
        left_key += (0,) * (width - len(left_key))
        right_key += (0,) * (width - len(right_key))
        return (left_key > right_key) - (left_key < right_key)
    return (left > right) - (left < right)


def _semver_key(version: str) -> tuple[int, int, int] | None:
    match = re.match(r"^\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", version)
    if match is None:
        return None
    major, minor, patch = (int(part) if part else 0 for part in match.groups())
    return major, minor, patch


def plugin_supports(min_plugin_version: str | None, plugin_version: str = PLUGIN_VERSION) -> bool:
    """Whether this plugin satisfies a catalog's ``min_plugin_version``."""
    if min_plugin_version is None:
        return True
    required = _semver_key(min_plugin_version)
    current = _semver_key(plugin_version)
    if required is None or current is None:
        return False
    return current >= required


def _compatible(catalog: PricingCatalog) -> bool:
    return catalog.schema in SUPPORTED_PRICING_SCHEMAS and plugin_supports(
        catalog.min_plugin_version
    )


# --------------------------------------------------------------------------- catalogs

type _FileKey = tuple[str, int, int]

_catalog_cache: dict[str, tuple[_FileKey, PricingCatalog | None]] = {}
_catalog_cache_lock = threading.Lock()


def _file_key(path: Path) -> _FileKey | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return str(path), stat.st_mtime_ns, stat.st_size


def _cached_load(path: Path) -> PricingCatalog | None:
    """Parsed catalog at ``path``, memoised by ``(path, mtime_ns, size)``.

    Missing, corrupt, or incompatible catalogs yield ``None``.
    """
    key = _file_key(path)
    if key is None:
        return None
    with _catalog_cache_lock:
        entry = _catalog_cache.get(key[0])
        if entry is not None and entry[0] == key:
            return entry[1]
    catalog: PricingCatalog | None
    try:
        catalog = parse_catalog(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, UnicodeDecodeError):
        catalog = None
    if catalog is not None and not _compatible(catalog):
        catalog = None
    with _catalog_cache_lock:
        _catalog_cache[key[0]] = (key, catalog)
    return catalog


def _bundled() -> PricingCatalog:
    catalog = _cached_load(BUNDLED_CATALOG_PATH)
    if catalog is None:
        raise RuntimeError(f"Bundled pricing catalog is invalid: {BUNDLED_CATALOG_PATH}")
    return catalog


def get_active_catalog(data_dir: Path | None = None) -> tuple[PricingCatalog, str]:
    """``(catalog, "cached" | "bundled")``: whichever valid catalog is newer.

    Ties go to the bundled catalog; a missing or corrupt cache falls back to it.
    """
    bundled = _bundled()
    directory = data_dir if data_dir is not None else default_usage_dir()
    cached = _cached_load(directory / CATALOG_FILENAME)
    if cached is not None and compare_versions(cached.catalog_version, bundled.catalog_version) > 0:
        return cached, "cached"
    return bundled, "bundled"


# --------------------------------------------------------------------------- state


@dataclass(slots=True)
class _State:
    etag: str | None = None
    checked_at: float | None = None
    catalog_version: str | None = None
    upgrade_required: bool = False
    upgrade_notice: str | None = None
    last_error: str | None = None
    plugin_version: str | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "etag": self.etag,
            "checked_at": self.checked_at,
            "catalog_version": self.catalog_version,
            "upgrade_required": self.upgrade_required,
            "upgrade_notice": self.upgrade_notice,
            "last_error": self.last_error,
            "plugin_version": self.plugin_version,
        }


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _load_state(data_dir: Path) -> _State:
    try:
        payload = json.loads((data_dir / STATE_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return _State()
    if not isinstance(payload, dict):
        return _State()
    checked_at = payload.get("checked_at")
    state = _State(
        etag=_optional_str(payload.get("etag")),
        checked_at=(
            float(checked_at)
            if isinstance(checked_at, int | float) and not isinstance(checked_at, bool)
            else None
        ),
        catalog_version=_optional_str(payload.get("catalog_version")),
        upgrade_required=payload.get("upgrade_required") is True,
        upgrade_notice=_optional_str(payload.get("upgrade_notice")),
        last_error=_optional_str(payload.get("last_error")),
        plugin_version=_optional_str(payload.get("plugin_version")),
    )
    if state.upgrade_required and state.plugin_version != PLUGIN_VERSION:
        # The plugin was updated since the incompatible catalog was seen: forget its
        # ETag so the next check downloads and re-evaluates it.
        state.upgrade_required = False
        state.upgrade_notice = None
        state.etag = None
    return state


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp_name)
        raise


def _save_state(data_dir: Path, state: _State) -> None:
    state.plugin_version = PLUGIN_VERSION
    data = json.dumps(state.to_json(), indent=2, sort_keys=True).encode("utf-8")
    _atomic_write(data_dir / STATE_FILENAME, data)


# --------------------------------------------------------------------------- status


def _auto_update(config: Mapping[str, object] | None) -> bool:
    value = (config or {}).get("pricing_auto_update", True)
    return value if isinstance(value, bool) else True


def _pricing_url(config: Mapping[str, object] | None) -> str:
    value = (config or {}).get("pricing_url")
    return value.strip() if isinstance(value, str) and value.strip() else DEFAULT_PRICING_URL


def get_status(
    data_dir: Path | None = None,
    config: Mapping[str, object] | None = None,
) -> PricingUpdateStatus:
    directory = resolve_data_dir(config, data_dir)
    active, source = get_active_catalog(directory)
    cached = _cached_load(directory / CATALOG_FILENAME)
    state = _load_state(directory)
    return PricingUpdateStatus(
        active_version=active.catalog_version,
        active_source=source,
        bundled_version=_bundled().catalog_version,
        cached_version=cached.catalog_version if cached is not None else None,
        last_checked_at=state.checked_at,
        etag=state.etag,
        upgrade_required=state.upgrade_required,
        upgrade_notice=state.upgrade_notice,
        auto_update=_auto_update(config),
        pricing_url=_pricing_url(config),
        last_error=state.last_error,
    )


# --------------------------------------------------------------------------- checks


def _response_status(response: Any) -> int:
    status = getattr(response, "status", None)
    if isinstance(status, int):
        return status
    getcode = getattr(response, "getcode", None)
    code = getcode() if callable(getcode) else None
    return code if isinstance(code, int) else 200


def _header(response: Any, name: str) -> str | None:
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    value = headers.get(name)
    return value if isinstance(value, str) else None


def _decompress_gzip_bounded(data: bytes, max_bytes: int) -> bytes:
    out = bytearray()
    remaining = data
    chunk_size = 65536
    while remaining:
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        offset = 0
        while offset < len(remaining) and not decoder.eof:
            budget = max_bytes - len(out) + 1
            chunk = decoder.decompress(remaining[offset : offset + chunk_size], budget)
            out.extend(chunk)
            if len(out) > max_bytes or decoder.unconsumed_tail:
                raise ValueError("decompressed pricing catalog is too large")
            offset += chunk_size
        tail = decoder.flush(max_bytes - len(out) + 1)
        out.extend(tail)
        if len(out) > max_bytes:
            raise ValueError("decompressed pricing catalog is too large")
        if not decoder.eof:
            raise ValueError("truncated gzip pricing catalog")
        remaining = decoder.unused_data + remaining[offset:]
    return bytes(out)


def _read_body(response: Any) -> bytes:
    body = response.read(_MAX_CATALOG_BYTES + 1)
    if len(body) > _MAX_CATALOG_BYTES:
        raise ValueError("pricing catalog download is too large")
    if (_header(response, "Content-Encoding") or "").strip().casefold() == "gzip":
        body = _decompress_gzip_bounded(body, _MAX_CATALOG_BYTES)
    return body


def _close(response: Any) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        with contextlib.suppress(Exception):
            close()


def check_for_update(
    *,
    data_dir: Path | None = None,
    pricing_url: str = DEFAULT_PRICING_URL,
    force: bool = False,
    timeout_s: float = 3.0,
    opener: Opener | None = None,
) -> PricingUpdateStatus:
    """Fetch the remote catalog if it changed; never raises for network/parse errors."""
    directory = data_dir if data_dir is not None else default_usage_dir()
    catalog_path = directory / CATALOG_FILENAME
    state = _load_state(directory)
    state.checked_at = time.time()

    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "User-Agent": f"fast-agent-{PLUGIN_NAME}/{PLUGIN_VERSION}",
    }
    if state.etag and not (force and not catalog_path.exists()):
        headers["If-None-Match"] = state.etag
    request = urllib.request.Request(pricing_url, headers=headers, method="GET")
    open_url = opener or urllib.request.urlopen

    try:
        try:
            response = open_url(request, timeout=timeout_s)
        except urllib.error.HTTPError as exc:
            if exc.code != 304:
                raise
            response = exc
        try:
            status = _response_status(response)
            if status == 304:
                state.last_error = None
            elif status != 200:
                raise ValueError(f"unexpected HTTP status {status}")
            else:
                _apply_download(directory, state, response)
        finally:
            _close(response)
    except (OSError, ValueError, EOFError, urllib.error.URLError) as exc:
        state.last_error = f"{type(exc).__name__}: {exc}"

    with contextlib.suppress(OSError):
        _save_state(directory, state)
    return get_status(directory, {"pricing_url": pricing_url})


def _apply_download(directory: Path, state: _State, response: Any) -> None:
    body = _read_body(response)
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("pricing catalog must be a JSON object")
    etag = _header(response, "ETag")
    schema = payload.get("schema")
    min_plugin_version = payload.get("min_plugin_version")
    if min_plugin_version is not None and not isinstance(min_plugin_version, str):
        raise ValueError("catalog.min_plugin_version must be a string")
    if schema not in SUPPORTED_PRICING_SCHEMAS or not plugin_supports(min_plugin_version):
        # Newer rules this plugin cannot apply: keep the current catalog and remember
        # the ETag so unchanged checks stay cheap until the plugin is updated.
        state.etag = etag
        state.upgrade_required = True
        state.upgrade_notice = UPGRADE_NOTICE
        state.last_error = None
        return
    catalog = parse_catalog(payload)  # full validation, including rule overlaps
    _atomic_write(directory / CATALOG_FILENAME, body)
    state.etag = etag
    state.catalog_version = catalog.catalog_version
    state.upgrade_required = False
    state.upgrade_notice = None
    state.last_error = None


# --------------------------------------------------------------------------- background

_background_lock = threading.Lock()
_background_thread: threading.Thread | None = None


def maybe_schedule_background_update(
    *,
    data_dir: Path | None = None,
    config: Mapping[str, object] | None = None,
    has_unpriced: bool = False,
    opener: Opener | None = None,
    now: float | None = None,
) -> bool:
    """Start a daemon-thread check when due; ``True`` when one was started.

    Checks run at most daily, or hourly while unpriced calls are waiting for rates.
    Concurrent requests are deduplicated; the caller never blocks on the network.
    """
    global _background_thread
    if not _auto_update(config):
        return False
    directory = resolve_data_dir(config, data_dir)
    interval = UNPRICED_CHECK_INTERVAL_S if has_unpriced else CHECK_INTERVAL_S
    checked_at = _load_state(directory).checked_at
    current = time.time() if now is None else now
    if checked_at is not None and 0 <= current - checked_at < interval:
        return False
    if not _background_lock.acquire(blocking=False):
        return False
    pricing_url = _pricing_url(config)

    def run() -> None:
        try:
            check_for_update(data_dir=directory, pricing_url=pricing_url, opener=opener)
        except Exception:  # a background check must never surface in the session
            pass
        finally:
            _background_lock.release()

    try:
        thread = threading.Thread(target=run, name=f"{PLUGIN_NAME}-pricing", daemon=True)
        _background_thread = thread
        thread.start()
    except BaseException:
        _background_lock.release()
        raise
    return True


def wait_for_background_update(timeout_s: float | None = None) -> bool:
    """Join the latest background check (for tests and shutdown); ``True`` if idle."""
    thread = _background_thread
    if thread is None:
        return True
    thread.join(timeout_s)
    return not thread.is_alive()
