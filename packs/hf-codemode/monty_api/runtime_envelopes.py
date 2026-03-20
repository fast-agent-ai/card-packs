from __future__ import annotations

from typing import Any

from .http_runtime import _as_int, _clamp_int


def _helper_meta(
    self: Any, start_calls: int, *, source: str, **extra: Any
) -> dict[str, Any]:
    out = {
        "source": source,
        "normalized": True,
        "budget_used": max(0, self.call_count["n"] - start_calls),
        "budget_remaining": self._budget_remaining(),
    }
    out.update(extra)
    return out


def _derive_limit_metadata(
    self: Any,
    *,
    requested_limit: int | None,
    applied_limit: int,
    default_limit_used: bool,
    requested_scan_limit: int | None = None,
    applied_scan_limit: int | None = None,
    requested_max_pages: int | None = None,
    applied_max_pages: int | None = None,
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "requested_limit": requested_limit,
        "applied_limit": applied_limit,
        "default_limit_used": default_limit_used,
    }
    if requested_scan_limit is not None or applied_scan_limit is not None:
        meta["requested_scan_limit"] = requested_scan_limit
        meta["scan_limit"] = applied_scan_limit
        meta["scan_limit_applied"] = requested_scan_limit != applied_scan_limit
    if requested_max_pages is not None or applied_max_pages is not None:
        meta["requested_max_pages"] = requested_max_pages
        meta["applied_max_pages"] = applied_max_pages
        meta["page_limit_applied"] = requested_max_pages != applied_max_pages
    if requested_limit is not None:
        meta["hard_cap_applied"] = applied_limit < requested_limit
    return meta


def _derive_more_available(
    self: Any,
    *,
    sample_complete: bool,
    exact_count: bool,
    returned: int,
    total: int | None,
) -> bool | str:
    if sample_complete:
        return False
    if exact_count and total is not None and returned < total:
        return True
    return "unknown"


def _derive_truncated_by(
    self: Any,
    *,
    hard_cap: bool = False,
    scan_limit_hit: bool = False,
    page_limit_hit: bool = False,
    limit_hit: bool = False,
) -> str:
    causes = [hard_cap, scan_limit_hit, page_limit_hit, limit_hit]
    if sum(1 for cause in causes if cause) > 1:
        return "multiple"
    if hard_cap:
        return "hard_cap"
    if scan_limit_hit:
        return "scan_limit"
    if page_limit_hit:
        return "page_limit"
    if limit_hit:
        return "limit"
    return "none"


def _derive_can_request_more(
    self: Any, *, sample_complete: bool, truncated_by: str
) -> bool:
    if sample_complete:
        return False
    return truncated_by in {"limit", "scan_limit", "page_limit", "multiple"}


def _derive_next_request_hint(
    self: Any,
    *,
    truncated_by: str,
    more_available: bool | str,
    applied_limit: int,
    applied_scan_limit: int | None = None,
    applied_max_pages: int | None = None,
) -> str:
    if truncated_by == "limit":
        return f"Ask for limit>{applied_limit} to see more rows"
    if truncated_by == "scan_limit" and applied_scan_limit is not None:
        return f"Increase scan_limit above {applied_scan_limit} for broader coverage"
    if truncated_by == "page_limit" and applied_max_pages is not None:
        return f"Increase max_pages above {applied_max_pages} to continue paging"
    if truncated_by == "hard_cap":
        return "No more rows can be returned in a single call because a hard cap was applied"
    if truncated_by == "multiple":
        return "Increase the relevant return/page/scan bounds to improve coverage"
    if more_available is False:
        return "No more results available"
    if more_available == "unknown":
        return "More results may exist; narrow filters or raise scan/page bounds for better coverage"
    return "Ask for a larger limit to see more rows"


def _resolve_exhaustive_limits(
    self: Any,
    *,
    limit: int | None,
    count_only: bool,
    default_limit: int,
    max_limit: int,
    scan_limit: int | None = None,
    scan_cap: int | None = None,
) -> dict[str, Any]:
    requested_limit = None if count_only else limit
    effective_requested_limit = 0 if count_only else requested_limit
    out: dict[str, Any] = {
        "requested_limit": requested_limit,
        "applied_limit": _clamp_int(
            effective_requested_limit,
            default=default_limit,
            minimum=0,
            maximum=max_limit,
        ),
        "default_limit_used": requested_limit is None and not count_only,
    }
    out["hard_cap_applied"] = (
        requested_limit is not None and out["applied_limit"] < requested_limit
    )
    if scan_cap is not None:
        out["requested_scan_limit"] = scan_limit
        out["applied_scan_limit"] = _clamp_int(
            scan_limit,
            default=scan_cap,
            minimum=1,
            maximum=scan_cap,
        )
    return out


def _build_exhaustive_meta(
    self: Any,
    *,
    base_meta: dict[str, Any],
    limit_plan: dict[str, Any],
    sample_complete: bool,
    exact_count: bool,
    truncated_by: str,
    more_available: bool | str,
    requested_max_pages: int | None = None,
    applied_max_pages: int | None = None,
) -> dict[str, Any]:
    meta = dict(base_meta)
    applied_limit = int(limit_plan["applied_limit"])
    applied_scan_limit = limit_plan.get("applied_scan_limit")
    meta.update(
        {
            "complete": sample_complete,
            "exact_count": exact_count,
            "sample_complete": sample_complete,
            "more_available": more_available,
            "can_request_more": _derive_can_request_more(
                self,
                sample_complete=sample_complete,
                truncated_by=truncated_by,
            ),
            "truncated_by": truncated_by,
            "next_request_hint": _derive_next_request_hint(
                self,
                truncated_by=truncated_by,
                more_available=more_available,
                applied_limit=applied_limit,
                applied_scan_limit=applied_scan_limit
                if isinstance(applied_scan_limit, int)
                else None,
                applied_max_pages=applied_max_pages,
            ),
        }
    )
    meta.update(
        _derive_limit_metadata(
            self,
            requested_limit=limit_plan["requested_limit"],
            applied_limit=applied_limit,
            default_limit_used=bool(limit_plan["default_limit_used"]),
            requested_scan_limit=limit_plan.get("requested_scan_limit"),
            applied_scan_limit=applied_scan_limit
            if isinstance(applied_scan_limit, int)
            else None,
            requested_max_pages=requested_max_pages,
            applied_max_pages=applied_max_pages,
        )
    )
    return meta


def _overview_count_only_success(
    self: Any,
    *,
    start_calls: int,
    source: str,
    total: int,
    limit_plan: dict[str, Any],
    base_meta: dict[str, Any],
) -> dict[str, Any]:
    meta = _build_exhaustive_meta(
        self,
        base_meta={
            **base_meta,
            "matched": total,
            "returned": 0,
            "total": total,
            "total_available": total,
            "total_matched": total,
            "truncated": False,
        },
        limit_plan=limit_plan,
        sample_complete=True,
        exact_count=True,
        truncated_by="none",
        more_available=False,
    )
    return _helper_success(
        self,
        start_calls=start_calls,
        source=source,
        items=[],
        meta=meta,
    )


def _build_exhaustive_result_meta(
    self: Any,
    *,
    base_meta: dict[str, Any],
    limit_plan: dict[str, Any],
    matched_count: int,
    returned_count: int,
    exact_count: bool,
    count_only: bool = False,
    sample_complete: bool | None = None,
    more_available: bool | str | None = None,
    scan_limit_hit: bool = False,
    page_limit_hit: bool = False,
    truncated_extra: bool = False,
    requested_max_pages: int | None = None,
    applied_max_pages: int | None = None,
) -> dict[str, Any]:
    applied_limit = int(limit_plan["applied_limit"])
    if count_only:
        effective_sample_complete = exact_count
    else:
        effective_sample_complete = (
            sample_complete
            if isinstance(sample_complete, bool)
            else exact_count and matched_count <= applied_limit
        )
    limit_hit = (
        False
        if count_only
        else (applied_limit > 0 and matched_count > applied_limit)
    )
    truncated_by = _derive_truncated_by(
        self,
        hard_cap=bool(limit_plan.get("hard_cap_applied")),
        scan_limit_hit=scan_limit_hit,
        page_limit_hit=page_limit_hit,
        limit_hit=limit_hit,
    )
    truncated = truncated_by != "none" or truncated_extra
    total_value = _as_int(base_meta.get("total"))
    effective_more_available = more_available
    if count_only and exact_count:
        effective_more_available = False
    if effective_more_available is None:
        effective_more_available = _derive_more_available(
            self,
            sample_complete=effective_sample_complete,
            exact_count=exact_count,
            returned=returned_count,
            total=total_value,
        )

    return _build_exhaustive_meta(
        self,
        base_meta={
            **base_meta,
            "matched": matched_count,
            "returned": returned_count,
            "truncated": truncated,
        },
        limit_plan=limit_plan,
        sample_complete=effective_sample_complete,
        exact_count=exact_count,
        truncated_by=truncated_by,
        more_available=effective_more_available,
        requested_max_pages=requested_max_pages,
        applied_max_pages=applied_max_pages,
    )


def _helper_success(
    self: Any,
    *,
    start_calls: int,
    source: str,
    items: list[dict[str, Any]],
    cursor: str | None = None,
    meta: dict[str, Any] | None = None,
    **extra_meta: Any,
) -> dict[str, Any]:
    merged_meta = dict(meta or {})
    merged_meta.update(extra_meta)
    if cursor is not None:
        merged_meta["cursor"] = cursor
    return {
        "ok": True,
        "item": items[0] if len(items) == 1 else None,
        "items": items,
        "meta": _helper_meta(self, start_calls, source=source, **merged_meta),
        "error": None,
    }


def _helper_error(
    self: Any,
    *,
    start_calls: int,
    source: str,
    error: Any,
    **meta: Any,
) -> dict[str, Any]:
    envelope = {
        "ok": False,
        "item": None,
        "items": [],
        "meta": _helper_meta(self, start_calls, source=source, **meta),
        "error": str(error),
    }
    self.latest_helper_error_box["value"] = envelope
    return envelope
