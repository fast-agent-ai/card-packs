from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, NamedTuple, cast

from huggingface_hub import HfApi

from .constants import MAX_CALLS_LIMIT
from .helpers.activity import register_activity_helpers
from .helpers.collections import register_collection_helpers
from .helpers.introspection import register_introspection_helpers
from .helpers.profiles import register_profile_helpers
from .helpers.repos import register_repo_helpers
from .http_runtime import (
    _as_int,
    _author_from_any,
    _canonical_repo_type,
    _clamp_int,
    _coerce_str_list,
    _dt_to_str,
    _extract_author_names,
    _extract_num_params,
    _extract_profile_name,
    _load_token,
    _normalize_collection_repo_item,
    _normalize_daily_paper_row,
    _normalize_repo_detail_row,
    _normalize_repo_search_row,
    _normalize_repo_sort_key,
    _normalize_trending_row,
    _optional_str_list,
    _repo_detail_call,
    _repo_list_call,
    _repo_web_url,
    _sort_repo_rows,
    call_api_host,
)
from .registry import PAGINATION_POLICY
from .runtime_envelopes import (
    _build_exhaustive_meta,
    _build_exhaustive_result_meta,
    _derive_can_request_more,
    _derive_limit_metadata,
    _derive_more_available,
    _derive_next_request_hint,
    _derive_truncated_by,
    _helper_error,
    _helper_meta,
    _helper_success,
    _overview_count_only_success,
    _resolve_exhaustive_limits,
)
from .runtime_filtering import (
    _apply_where,
    _helper_item,
    _item_matches_where,
    _normalize_where,
    _overview_count,
    _project_activity_items,
    _project_actor_items,
    _project_collection_items,
    _project_daily_paper_items,
    _project_items,
    _project_repo_items,
    _project_user_items,
    _project_user_like_items,
)
from .validation import _resolve_helper_functions


class RuntimeHelperEnvironment(NamedTuple):
    context: "RuntimeContext"
    call_count: dict[str, int]
    trace: list[dict[str, Any]]
    limit_summaries: list[dict[str, Any]]
    latest_helper_error_box: dict[str, dict[str, Any] | None]
    internal_helper_used: dict[str, bool]
    helper_functions: dict[str, Callable[..., Any]]


@dataclass(slots=True)
class RuntimeContext:
    max_calls: int
    strict_mode: bool
    timeout_sec: int
    call_count: dict[str, int] = field(default_factory=lambda: {"n": 0})
    trace: list[dict[str, Any]] = field(default_factory=list)
    limit_summaries: list[dict[str, Any]] = field(default_factory=list)
    latest_helper_error_box: dict[str, dict[str, Any] | None] = field(
        default_factory=lambda: {"value": None}
    )
    internal_helper_used: dict[str, bool] = field(
        default_factory=lambda: {"used": False}
    )
    helper_registry: dict[str, Callable[..., Any]] = field(default_factory=dict)
    _hf_api_client: HfApi | None = field(default=None, init=False, repr=False)

    def _budget_remaining(self) -> int:
        return max(0, self.max_calls - self.call_count["n"])

    def _policy_int(self, helper_name: str, key: str, default: int) -> int:
        cfg = PAGINATION_POLICY.get(helper_name) or {}
        try:
            return int(cfg.get(key, default))
        except Exception:
            return int(default)

    def _consume_call(self, endpoint: str, method: str = "GET") -> int:
        if self.call_count["n"] >= self.max_calls:
            raise RuntimeError(f"Max API calls exceeded ({self.max_calls})")
        self.call_count["n"] += 1
        return self.call_count["n"]

    def _trace_ok(
        self, idx: int, endpoint: str, method: str = "GET", status: int = 200
    ) -> None:
        self.trace.append(
            {
                "call_index": idx,
                "depth": idx,
                "method": method,
                "endpoint": endpoint,
                "ok": True,
                "status": status,
            }
        )

    def _trace_err(
        self, idx: int, endpoint: str, err: Any, method: str = "GET", status: int = 0
    ) -> None:
        self.trace.append(
            {
                "call_index": idx,
                "depth": idx,
                "method": method,
                "endpoint": endpoint,
                "ok": False,
                "status": status,
                "error": str(err),
            }
        )

    def _host_raw_call(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        method: str = "GET",
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        idx = self._consume_call(endpoint, method)
        try:
            resp = call_api_host(
                endpoint,
                method=method,
                params=params,
                json_body=json_body,
                timeout_sec=self.timeout_sec,
                strict_mode=self.strict_mode,
            )
            if resp.get("ok"):
                self._trace_ok(
                    idx, endpoint, method=method, status=int(resp.get("status") or 200)
                )
            else:
                self._trace_err(
                    idx,
                    endpoint,
                    resp.get("error"),
                    method=method,
                    status=int(resp.get("status") or 0),
                )
            return resp
        except Exception as exc:
            self._trace_err(idx, endpoint, exc, method=method, status=0)
            raise

    def _get_hf_api_client(self) -> HfApi:
        if self._hf_api_client is None:
            endpoint = os.getenv("HF_ENDPOINT", "https://huggingface.co").rstrip("/")
            self._hf_api_client = HfApi(endpoint=endpoint, token=_load_token())
        return self._hf_api_client

    def _host_hf_call(self, endpoint: str, fn: Callable[[], Any]) -> Any:
        idx = self._consume_call(endpoint, "GET")
        try:
            out = fn()
            self._trace_ok(idx, endpoint, method="GET", status=200)
            return out
        except Exception as exc:
            self._trace_err(idx, endpoint, exc, method="GET", status=0)
            raise

    async def call_helper(self, helper_name: str, /, *args: Any, **kwargs: Any) -> Any:
        fn = self.helper_registry.get(helper_name)
        if not callable(fn):
            raise RuntimeError(f"Helper '{helper_name}' is not registered")
        return await cast(Callable[..., Any], fn)(*args, **kwargs)


for name, value in {
    "_helper_meta": _helper_meta,
    "_derive_limit_metadata": _derive_limit_metadata,
    "_derive_more_available": _derive_more_available,
    "_derive_truncated_by": _derive_truncated_by,
    "_derive_can_request_more": _derive_can_request_more,
    "_derive_next_request_hint": _derive_next_request_hint,
    "_resolve_exhaustive_limits": _resolve_exhaustive_limits,
    "_build_exhaustive_meta": _build_exhaustive_meta,
    "_overview_count_only_success": _overview_count_only_success,
    "_build_exhaustive_result_meta": _build_exhaustive_result_meta,
    "_helper_success": _helper_success,
    "_helper_error": _helper_error,
    "_project_items": _project_items,
    "_project_repo_items": _project_repo_items,
    "_project_collection_items": _project_collection_items,
    "_project_daily_paper_items": _project_daily_paper_items,
    "_project_user_items": _project_user_items,
    "_project_actor_items": _project_actor_items,
    "_project_user_like_items": _project_user_like_items,
    "_project_activity_items": _project_activity_items,
    "_normalize_where": _normalize_where,
    "_item_matches_where": _item_matches_where,
    "_apply_where": _apply_where,
    "_helper_item": _helper_item,
    "_overview_count": _overview_count,
    "_as_int": staticmethod(_as_int),
    "_author_from_any": staticmethod(_author_from_any),
    "_canonical_repo_type": staticmethod(_canonical_repo_type),
    "_clamp_int": staticmethod(_clamp_int),
    "_coerce_str_list": staticmethod(_coerce_str_list),
    "_dt_to_str": staticmethod(_dt_to_str),
    "_extract_author_names": staticmethod(_extract_author_names),
    "_extract_num_params": staticmethod(_extract_num_params),
    "_extract_profile_name": staticmethod(_extract_profile_name),
    "_load_token": staticmethod(_load_token),
    "_normalize_collection_repo_item": staticmethod(_normalize_collection_repo_item),
    "_normalize_daily_paper_row": staticmethod(_normalize_daily_paper_row),
    "_normalize_repo_detail_row": staticmethod(_normalize_repo_detail_row),
    "_normalize_repo_search_row": staticmethod(_normalize_repo_search_row),
    "_normalize_repo_sort_key": staticmethod(_normalize_repo_sort_key),
    "_normalize_trending_row": staticmethod(_normalize_trending_row),
    "_optional_str_list": staticmethod(_optional_str_list),
    "_repo_detail_call": staticmethod(_repo_detail_call),
    "_repo_list_call": staticmethod(_repo_list_call),
    "_repo_web_url": staticmethod(_repo_web_url),
    "_sort_repo_rows": staticmethod(_sort_repo_rows),
}.items():
    setattr(RuntimeContext, name, value)


def build_runtime_helper_environment(
    *,
    max_calls: int,
    strict_mode: bool,
    timeout_sec: int,
) -> RuntimeHelperEnvironment:
    ctx = RuntimeContext(
        max_calls=max(1, min(int(max_calls), MAX_CALLS_LIMIT)),
        strict_mode=strict_mode,
        timeout_sec=timeout_sec,
    )

    for registration in (
        register_profile_helpers,
        register_repo_helpers,
        register_activity_helpers,
        register_collection_helpers,
        register_introspection_helpers,
    ):
        ctx.helper_registry.update(registration(ctx))

    helper_functions = _resolve_helper_functions(ctx.helper_registry)
    return RuntimeHelperEnvironment(
        context=ctx,
        call_count=ctx.call_count,
        trace=ctx.trace,
        limit_summaries=ctx.limit_summaries,
        latest_helper_error_box=ctx.latest_helper_error_box,
        internal_helper_used=ctx.internal_helper_used,
        helper_functions=helper_functions,
    )
