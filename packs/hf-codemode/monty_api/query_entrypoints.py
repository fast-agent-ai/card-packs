from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import sys
import time
from typing import Any, Callable

from .constants import (
    DEFAULT_MAX_CALLS,
    DEFAULT_MONTY_MAX_ALLOCATIONS,
    DEFAULT_MONTY_MAX_MEMORY,
    DEFAULT_MONTY_MAX_RECURSION_DEPTH,
    DEFAULT_TIMEOUT_SEC,
    INTERNAL_STRICT_MODE,
    MAX_CALLS_LIMIT,
)
from .runtime_context import build_runtime_helper_environment
from .validation import (
    _coerce_jsonish_python_literals,
    _summarize_limit_hit,
    _truncate_result_payload,
    _validate_generated_code,
    _wrap_raw_result,
)


class MontyExecutionError(RuntimeError):
    def __init__(self, message: str, api_calls: int, trace: list[dict[str, Any]]):
        super().__init__(message)
        self.api_calls = api_calls
        self.trace = trace


def _query_debug_enabled() -> bool:
    value = os.environ.get("MONTY_DEBUG_QUERY", "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _log_generated_query(
    *, query: str, code: str, max_calls: int | None, timeout_sec: int | None
) -> None:
    if not _query_debug_enabled():
        return
    print("[monty-debug] query:", file=sys.stderr)
    print(query, file=sys.stderr)
    print("[monty-debug] max_calls:", max_calls, file=sys.stderr)
    print("[monty-debug] timeout_sec:", timeout_sec, file=sys.stderr)
    print("[monty-debug] code:", file=sys.stderr)
    print(code, file=sys.stderr)
    sys.stderr.flush()


def _introspect_helper_signatures() -> dict[str, set[str]]:
    env = build_runtime_helper_environment(
        max_calls=DEFAULT_MAX_CALLS,
        strict_mode=INTERNAL_STRICT_MODE,
        timeout_sec=DEFAULT_TIMEOUT_SEC,
    )
    signatures = {
        name: {
            parameter.name for parameter in inspect.signature(fn).parameters.values()
        }
        for name, fn in env.helper_functions.items()
    }
    return signatures


async def _run_with_monty(
    *,
    code: str,
    query: str,
    max_calls: int,
    strict_mode: bool,
    timeout_sec: int,
) -> dict[str, Any]:
    try:
        import pydantic_monty
    except Exception as e:
        raise RuntimeError(
            "pydantic_monty is not installed. Install with `uv pip install pydantic-monty`."
        ) from e

    env = build_runtime_helper_environment(
        max_calls=max_calls,
        strict_mode=strict_mode,
        timeout_sec=timeout_sec,
    )

    m = pydantic_monty.Monty(
        code,
        inputs=["query", "max_calls"],
        script_name="monty_agent.py",
        type_check=False,
    )

    def _collecting_wrapper(
        helper_name: str, fn: Callable[..., Any]
    ) -> Callable[..., Any]:
        async def wrapped(*args: Any, **kwargs: Any) -> Any:
            result = await fn(*args, **kwargs)
            summary = _summarize_limit_hit(helper_name, result)
            if summary is not None and len(env.limit_summaries) < 20:
                env.limit_summaries.append(summary)
            return result

        return wrapped

    limits: pydantic_monty.ResourceLimits = {
        "max_duration_secs": float(timeout_sec),
        "max_memory": DEFAULT_MONTY_MAX_MEMORY,
        "max_allocations": DEFAULT_MONTY_MAX_ALLOCATIONS,
        "max_recursion_depth": DEFAULT_MONTY_MAX_RECURSION_DEPTH,
    }

    try:
        result = await pydantic_monty.run_monty_async(
            m,
            inputs={"query": query, "max_calls": max_calls},
            external_functions={
                name: _collecting_wrapper(name, fn)
                for name, fn in env.helper_functions.items()
            },
            limits=limits,
        )
    except Exception as e:
        raise MontyExecutionError(str(e), env.call_count["n"], env.trace) from e

    if env.call_count["n"] == 0:
        if env.internal_helper_used["used"]:
            return {
                "output": _truncate_result_payload(result),
                "api_calls": env.call_count["n"],
                "trace": env.trace,
                "limit_summaries": env.limit_summaries,
            }
        if isinstance(result, dict) and result.get("ok") is True:
            meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
            source = meta.get("source")
            if isinstance(source, str) and source.startswith("internal://"):
                return {
                    "output": _truncate_result_payload(result),
                    "api_calls": env.call_count["n"],
                    "trace": env.trace,
                    "limit_summaries": env.limit_summaries,
                }
        latest_helper_error = env.latest_helper_error_box.get("value")
        if latest_helper_error is not None:
            return {
                "output": _truncate_result_payload(latest_helper_error),
                "api_calls": env.call_count["n"],
                "trace": env.trace,
                "limit_summaries": env.limit_summaries,
            }
        if (
            isinstance(result, dict)
            and result.get("ok") is False
            and isinstance(result.get("error"), str)
        ):
            return {
                "output": _truncate_result_payload(result),
                "api_calls": env.call_count["n"],
                "trace": env.trace,
                "limit_summaries": env.limit_summaries,
            }
        raise MontyExecutionError(
            "Code completed without calling any external API function",
            env.call_count["n"],
            env.trace,
        )

    if not any(step.get("ok") is True for step in env.trace):
        if (
            isinstance(result, dict)
            and result.get("ok") is False
            and isinstance(result.get("error"), str)
        ):
            return {
                "output": _truncate_result_payload(result),
                "api_calls": env.call_count["n"],
                "trace": env.trace,
                "limit_summaries": env.limit_summaries,
            }
        raise MontyExecutionError(
            "Code completed without a successful API call; refusing non-live fallback result",
            env.call_count["n"],
            env.trace,
        )

    return {
        "output": _truncate_result_payload(result),
        "api_calls": env.call_count["n"],
        "trace": env.trace,
        "limit_summaries": env.limit_summaries,
    }


def _prepare_query_inputs(
    *,
    query: str,
    code: str,
    max_calls: int | None,
    timeout_sec: int | None,
) -> tuple[str, str, int, int]:
    if not query or not query.strip():
        raise ValueError("query is required")
    if not code or not code.strip():
        raise ValueError("code is required")

    resolved_max_calls = DEFAULT_MAX_CALLS if max_calls is None else max_calls
    resolved_timeout_sec = DEFAULT_TIMEOUT_SEC if timeout_sec is None else timeout_sec
    normalized_max_calls = max(1, min(int(resolved_max_calls), MAX_CALLS_LIMIT))
    normalized_timeout_sec = int(resolved_timeout_sec)
    normalized_code = _coerce_jsonish_python_literals(code.strip())
    _validate_generated_code(normalized_code)
    return query, normalized_code, normalized_max_calls, normalized_timeout_sec


async def _execute_query(
    *,
    query: str,
    code: str,
    max_calls: int | None,
    timeout_sec: int | None,
) -> dict[str, Any]:
    prepared_query, prepared_code, prepared_max_calls, prepared_timeout = (
        _prepare_query_inputs(
            query=query,
            code=code,
            max_calls=max_calls,
            timeout_sec=timeout_sec,
        )
    )
    _log_generated_query(
        query=prepared_query,
        code=prepared_code,
        max_calls=prepared_max_calls,
        timeout_sec=prepared_timeout,
    )
    return await _run_with_monty(
        code=prepared_code,
        query=prepared_query,
        max_calls=prepared_max_calls,
        strict_mode=INTERNAL_STRICT_MODE,
        timeout_sec=prepared_timeout,
    )


async def hf_hub_query(
    query: str,
    code: str,
    max_calls: int | None = DEFAULT_MAX_CALLS,
    timeout_sec: int | None = DEFAULT_TIMEOUT_SEC,
) -> dict[str, Any]:
    """Use natural-language queries to explore the Hugging Face Hub.

    Best for read-only Hub discovery, lookup, ranking, and relationship questions
    across users, organizations, repositories, activity, followers, likes,
    discussions, and collections.
    """
    try:
        run = await _execute_query(
            query=query,
            code=code,
            max_calls=max_calls,
            timeout_sec=timeout_sec,
        )
        return {
            "ok": True,
            "data": run["output"],
            "error": None,
            "api_calls": run["api_calls"],
        }
    except MontyExecutionError as e:
        return {
            "ok": False,
            "data": None,
            "error": str(e),
            "api_calls": e.api_calls,
        }
    except Exception as e:
        return {
            "ok": False,
            "data": None,
            "error": str(e),
            "api_calls": 0,
        }


async def hf_hub_query_raw(
    query: str,
    code: str,
    max_calls: int | None = DEFAULT_MAX_CALLS,
    timeout_sec: int | None = DEFAULT_TIMEOUT_SEC,
) -> Any:
    """Use natural-language queries to explore the Hugging Face Hub in raw mode.

    Best for read-only Hub discovery, lookup, ranking, and relationship
    questions when the caller wants a runtime-owned raw envelope:
    ``result`` contains the direct ``solve(...)`` output and ``meta`` contains
    execution details such as timing, call counts, and limit summaries.
    """
    started = time.perf_counter()
    try:
        run = await _execute_query(
            query=query,
            code=code,
            max_calls=max_calls,
            timeout_sec=timeout_sec,
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return _wrap_raw_result(
            run["output"],
            ok=True,
            api_calls=run["api_calls"],
            elapsed_ms=elapsed_ms,
            limit_summaries=run.get("limit_summaries"),
        )
    except MontyExecutionError as e:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return _wrap_raw_result(
            None,
            ok=False,
            api_calls=e.api_calls,
            elapsed_ms=elapsed_ms,
            error=str(e),
        )
    except Exception as e:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return _wrap_raw_result(
            None,
            ok=False,
            api_calls=0,
            elapsed_ms=elapsed_ms,
            error=str(e),
        )


def _arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Monty-backed API chaining tool (v3)")
    p.add_argument("--query", required=True, help="Natural language query")
    p.add_argument("--code", default=None, help="Inline Monty code to execute")
    p.add_argument(
        "--code-file", default=None, help="Path to .py file with Monty code to execute"
    )
    p.add_argument(
        "--max-calls",
        type=int,
        default=DEFAULT_MAX_CALLS,
        help="Max external API/helper calls",
    )
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SEC)
    return p


def main() -> int:
    args = _arg_parser().parse_args()
    code = args.code
    if args.code_file:
        with open(args.code_file, "r", encoding="utf-8") as f:
            code = f.read()

    if not code:
        print(
            json.dumps(
                {"ok": False, "error": "Either --code or --code-file is required"},
                ensure_ascii=False,
            )
        )
        return 1

    try:
        out = asyncio.run(
            hf_hub_query(
                query=args.query,
                code=code,
                max_calls=args.max_calls,
                timeout_sec=args.timeout,
            )
        )
        print(json.dumps(out, ensure_ascii=False))
        return 0 if out.get("ok") else 1
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 1
