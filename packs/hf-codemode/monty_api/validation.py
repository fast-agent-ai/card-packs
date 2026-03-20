from __future__ import annotations

import ast
import re
import tokenize
from io import StringIO
from typing import Any, Callable, cast

from .constants import (
    GRAPH_SCAN_LIMIT_CAP,
    LIKES_SCAN_LIMIT_CAP,
    OUTPUT_ITEMS_TRUNCATION_LIMIT,
    SELECTIVE_ENDPOINT_RETURN_HARD_CAP,
    TRENDING_ENDPOINT_MAX_LIMIT,
)
from .registry import (
    ALLOWLIST_PATTERNS,
    HELPER_EXTERNALS,
    STRICT_ALLOWLIST_PATTERNS,
)


def _resolve_helper_functions(
    namespace: dict[str, Any],
) -> dict[str, Callable[..., Any]]:
    resolved: dict[str, Callable[..., Any]] = {}
    for helper_name in HELPER_EXTERNALS:
        candidate = namespace.get(helper_name)
        if not callable(candidate):
            raise RuntimeError(f"Helper '{helper_name}' is not defined or not callable")
        resolved[helper_name] = cast(Callable[..., Any], candidate)
    return resolved


def _normalize_endpoint(endpoint: str) -> str:
    ep = (endpoint or "").strip()
    if not ep:
        raise ValueError("endpoint is required")
    if "?" in ep:
        raise ValueError("endpoint must not include query string; use params")
    if ep.startswith("http://") or ep.startswith("https://"):
        raise ValueError("endpoint must be path-only")
    if not ep.startswith("/"):
        ep = "/" + ep
    if not ep.startswith("/api/"):
        ep = "/api" + ep
    if ep in {"/api/collections/search", "/api/collections/search/"}:
        ep = "/api/collections"
    if ".." in ep:
        raise ValueError("path traversal not allowed")
    return ep


def _endpoint_allowed(endpoint: str, strict_mode: bool) -> bool:
    path = endpoint.split("?", 1)[0]
    patterns = STRICT_ALLOWLIST_PATTERNS if strict_mode else ALLOWLIST_PATTERNS
    return any(re.match(p, path) for p in patterns)


def _sanitize_params(endpoint: str, params: dict[str, Any] | None) -> dict[str, Any]:
    clean = dict(params or {})
    path = endpoint.split("?", 1)[0]

    if path == "/api/collections":
        if "q" not in clean and "search" in clean:
            clean["q"] = clean.get("search")
        clean.pop("search", None)

    if path == "/api/trending":
        t = str(clean.get("type") or "").strip().lower()
        aliases = {"models": "model", "datasets": "dataset", "spaces": "space"}
        if t in aliases:
            clean["type"] = aliases[t]
        lim = clean.get("limit")
        if lim is not None:
            try:
                n = int(lim)
            except Exception:
                n = TRENDING_ENDPOINT_MAX_LIMIT
            clean["limit"] = max(1, min(n, TRENDING_ENDPOINT_MAX_LIMIT))
        return clean

    lim = clean.get("limit")
    if lim is None:
        return clean
    try:
        n = int(lim)
    except Exception:
        return clean

    endpoint_limit_max = SELECTIVE_ENDPOINT_RETURN_HARD_CAP
    if re.match(r"^/api/users/[^/]+/(followers|following)$", path):
        endpoint_limit_max = GRAPH_SCAN_LIMIT_CAP
    elif re.match(r"^/api/users/[^/]+/likes$", path):
        endpoint_limit_max = LIKES_SCAN_LIMIT_CAP

    clean["limit"] = max(1, min(n, endpoint_limit_max))
    return clean


def _truncate_result_payload(output: Any) -> Any:
    if not isinstance(output, dict):
        return output

    items = output.get("items")
    if not isinstance(items, list) or len(items) <= OUTPUT_ITEMS_TRUNCATION_LIMIT:
        return output

    trimmed = dict(output)
    trimmed_items = items[:OUTPUT_ITEMS_TRUNCATION_LIMIT]
    trimmed["items"] = trimmed_items
    trimmed["item"] = trimmed_items[0] if len(trimmed_items) == 1 else None
    note = f"truncated items to first {OUTPUT_ITEMS_TRUNCATION_LIMIT} rows for token efficiency"
    steps = trimmed.get("steps")
    if isinstance(steps, list):
        trimmed["steps"] = [*steps, note]
    else:
        trimmed["steps"] = [note]
    return trimmed


def _is_helper_envelope(output: Any) -> bool:
    return (
        isinstance(output, dict)
        and isinstance(output.get("ok"), bool)
        and "items" in output
        and "meta" in output
        and "error" in output
    )


def _summarize_limit_hit(helper_name: str, result: Any) -> dict[str, Any] | None:
    if not _is_helper_envelope(result):
        return None
    meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
    if not isinstance(meta, dict):
        return None

    truncated_by = str(meta.get("truncated_by") or "")
    limit_hit = any(
        [
            meta.get("truncated") is True,
            meta.get("hard_cap_applied") is True,
            truncated_by in {"scan_limit", "page_limit", "multiple"},
        ]
    )
    if not limit_hit:
        return None

    summary: dict[str, Any] = {
        "helper": helper_name,
        "source": meta.get("source"),
        "returned": meta.get("returned"),
        "total": meta.get("total"),
        "truncated": meta.get("truncated"),
        "truncated_by": meta.get("truncated_by"),
        "more_available": meta.get("more_available"),
        "requested_limit": meta.get("requested_limit"),
        "applied_limit": meta.get("applied_limit"),
        "next_request_hint": meta.get("next_request_hint"),
    }
    if meta.get("scan_limit") is not None:
        summary["scan_limit"] = meta.get("scan_limit")
    if meta.get("applied_max_pages") is not None:
        summary["applied_max_pages"] = meta.get("applied_max_pages")
    return summary


def _wrap_raw_result(
    result: Any,
    *,
    ok: bool,
    api_calls: int,
    elapsed_ms: int,
    limit_summaries: list[dict[str, Any]] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    hits = [dict(summary) for summary in (limit_summaries or [])[:10]]
    meta: dict[str, Any] = {
        "ok": ok,
        "api_calls": api_calls,
        "elapsed_ms": elapsed_ms,
        "limits_reached": bool(hits),
        "limit_summary": hits,
    }
    if error is not None:
        meta["error"] = error
    return {
        "result": result,
        "meta": meta,
    }


def _validate_generated_code(code: str) -> None:
    if not code.strip():
        raise ValueError("Generated code is empty")

    blocked_patterns: list[tuple[str, str]] = [
        (r"(?m)^\s*import\s+\S", "import statement"),
        (r"(?m)^\s*from\s+\S+\s+import\s+\S", "from-import statement"),
        (r"\bexec\s*\(", "exec("),
        (r"\beval\s*\(", "eval("),
        (r"\bopen\s*\(", "open("),
        (r"\b__import__\b", "__import__"),
        (r"(?i)\bwhile\s+true\b", "while true"),
    ]
    for pattern, label in blocked_patterns:
        if re.search(pattern, code):
            raise ValueError(f"Generated code contains blocked pattern: {label}")

    try:
        parsed = compile(  # noqa: S102 - compile is used for AST validation only.
            code,
            "<generated-monty-code>",
            "exec",
            flags=ast.PyCF_ONLY_AST | ast.PyCF_ALLOW_TOP_LEVEL_AWAIT,
            dont_inherit=True,
        )
    except SyntaxError as e:
        message = e.msg or "invalid syntax"
        raise ValueError(f"Generated code is not valid Python: {message}") from e

    if not isinstance(parsed, ast.Module):
        raise ValueError("Generated code must be a Python module")

    solve_defs = [
        node
        for node in parsed.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "solve"
    ]
    if not solve_defs:
        raise ValueError(
            "Generated code must define `async def solve(query, max_calls): ...`."
        )

    def _valid_solve_signature(node: ast.AsyncFunctionDef) -> bool:
        args = node.args
        return (
            not args.posonlyargs
            and len(args.args) == 2
            and [arg.arg for arg in args.args] == ["query", "max_calls"]
            and args.vararg is None
            and not args.kwonlyargs
            and args.kwarg is None
            and not args.defaults
            and not args.kw_defaults
        )

    if not any(_valid_solve_signature(node) for node in solve_defs):
        raise ValueError(
            "`solve` must have signature `async def solve(query, max_calls): ...`."
        )

    if not parsed.body:
        raise ValueError("Generated code is empty")

    final_stmt = parsed.body[-1]
    valid_final_await = (
        isinstance(final_stmt, ast.Expr)
        and isinstance(final_stmt.value, ast.Await)
        and isinstance(final_stmt.value.value, ast.Call)
        and isinstance(final_stmt.value.value.func, ast.Name)
        and final_stmt.value.value.func.id == "solve"
        and len(final_stmt.value.value.args) == 2
        and not final_stmt.value.value.keywords
        and all(isinstance(arg, ast.Name) for arg in final_stmt.value.value.args)
        and [cast(ast.Name, arg).id for arg in final_stmt.value.value.args]
        == ["query", "max_calls"]
    )
    if not valid_final_await:
        raise ValueError(
            "Generated code must end with `await solve(query, max_calls)`."
        )

    for node in ast.walk(parsed):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id == "call_api":
            raise ValueError(
                "Generated code must use documented hf_* helpers only; raw `call_api(...)` is not part of the prompt contract."
            )

    helper_name_set = set(HELPER_EXTERNALS)
    has_external_call = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in helper_name_set
        for node in ast.walk(parsed)
    )
    if not has_external_call:
        raise ValueError(
            "Generated code must call at least one documented hf_* helper."
        )


def _coerce_jsonish_python_literals(code: str) -> str:
    """Normalize common JSON literals into valid Python names in generated code."""
    replacements = {
        "true": "True",
        "false": "False",
        "null": "None",
    }

    out_tokens: list[tuple[int, str]] = []
    for tok in tokenize.generate_tokens(StringIO(code).readline):
        tok_type = tok.type
        tok_str = tok.string
        if tok_type == tokenize.NAME and tok_str in replacements:
            tok_str = replacements[tok_str]
        out_tokens.append((tok_type, tok_str))
    return tokenize.untokenize(out_tokens)
