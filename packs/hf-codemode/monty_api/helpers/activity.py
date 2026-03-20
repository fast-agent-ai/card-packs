from __future__ import annotations

# ruff: noqa: C901, PLR0912, PLR0913, PLR0915, PLR0917
from functools import partial
from typing import Any, Callable

from ..constants import (
    ACTIVITY_CANONICAL_FIELDS,
    EXHAUSTIVE_HELPER_RETURN_HARD_CAP,
    RECENT_ACTIVITY_PAGE_SIZE,
    RECENT_ACTIVITY_SCAN_MAX_PAGES,
)
from ..context_types import HelperRuntimeContext


async def hf_recent_activity(
    ctx: HelperRuntimeContext,
    feed_type: str | None = None,
    entity: str | None = None,
    activity_types: list[str] | None = None,
    repo_types: list[str] | None = None,
    limit: int | None = None,
    max_pages: int | None = None,
    start_cursor: str | None = None,
    count_only: bool = False,
    where: dict[str, Any] | None = None,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    start_calls = ctx.call_count["n"]
    default_limit = ctx._policy_int("hf_recent_activity", "default_limit", 100)
    page_cap = ctx._policy_int(
        "hf_recent_activity", "page_limit", RECENT_ACTIVITY_PAGE_SIZE
    )
    pages_cap = ctx._policy_int(
        "hf_recent_activity", "max_pages", RECENT_ACTIVITY_SCAN_MAX_PAGES
    )
    requested_max_pages = max_pages
    ft = str(feed_type or "").strip().lower()
    ent = str(entity or "").strip()
    if ft not in {"user", "org"}:
        if ft and (not ent):
            ent = ft
            ft = "user"
        elif not ft and ent:
            ft = "user"
    if ft not in {"user", "org"}:
        return ctx._helper_error(
            start_calls=start_calls,
            source="/api/recent-activity",
            error="feed_type must be 'user' or 'org'",
        )
    if not ent:
        return ctx._helper_error(
            start_calls=start_calls,
            source="/api/recent-activity",
            error="entity is required",
        )
    limit_plan = ctx._resolve_exhaustive_limits(
        limit=limit,
        count_only=count_only,
        default_limit=default_limit,
        max_limit=EXHAUSTIVE_HELPER_RETURN_HARD_CAP,
    )
    applied_limit = int(limit_plan["applied_limit"])
    page_lim = page_cap
    pages_lim = ctx._clamp_int(
        requested_max_pages, default=pages_cap, minimum=1, maximum=pages_cap
    )
    type_filter = {
        str(t).strip().lower() for t in activity_types or [] if str(t).strip()
    }
    repo_filter = {
        ctx._canonical_repo_type(t, default="")
        for t in repo_types or []
        if str(t).strip()
    }
    next_cursor = (
        str(start_cursor).strip()
        if isinstance(start_cursor, str) and start_cursor.strip()
        else None
    )
    items: list[dict[str, Any]] = []
    scanned = 0
    matched = 0
    pages = 0
    exhausted_feed = False
    stopped_for_budget = False
    try:
        normalized_where = ctx._normalize_where(
            where, allowed_fields=ACTIVITY_CANONICAL_FIELDS
        )
    except ValueError as exc:
        return ctx._helper_error(
            start_calls=start_calls,
            source="/api/recent-activity",
            error=exc,
        )
    while pages < pages_lim and (applied_limit == 0 or len(items) < applied_limit):
        if ctx._budget_remaining() <= 0:
            stopped_for_budget = True
            break
        params: dict[str, Any] = {"feedType": ft, "entity": ent, "limit": page_lim}
        if next_cursor:
            params["cursor"] = next_cursor
        resp = ctx._host_raw_call("/api/recent-activity", params=params)
        if not resp.get("ok"):
            if pages == 0:
                return ctx._helper_error(
                    start_calls=start_calls,
                    source="/api/recent-activity",
                    error=resp.get("error") or "recent-activity fetch failed",
                )
            break
        payload = resp.get("data") if isinstance(resp.get("data"), dict) else {}
        rows = (
            payload.get("recentActivity")
            if isinstance(payload.get("recentActivity"), list)
            else []
        )
        cursor_raw = payload.get("cursor")
        next_cursor = cursor_raw if isinstance(cursor_raw, str) and cursor_raw else None
        pages += 1
        if not rows:
            exhausted_feed = True
            break
        for row in rows:
            if not isinstance(row, dict):
                continue
            scanned += 1
            typ = str(row.get("type") or "").strip().lower()
            repo_id = row.get("repoId")
            repo_type = row.get("repoType")
            repo_data = (
                row.get("repoData") if isinstance(row.get("repoData"), dict) else None
            )
            repo_obj = row.get("repo") if isinstance(row.get("repo"), dict) else None
            if repo_id is None and repo_data is not None:
                repo_id = repo_data.get("id") or repo_data.get("name")
            if repo_id is None and repo_obj is not None:
                repo_id = repo_obj.get("id") or repo_obj.get("name")
            if repo_type is None and repo_data is not None:
                repo_type = repo_data.get("type")
            if repo_type is None and repo_obj is not None:
                repo_type = repo_obj.get("type")
            rt = ctx._canonical_repo_type(repo_type, default="") if repo_type else ""
            if type_filter and typ not in type_filter:
                continue
            if repo_filter and rt not in repo_filter:
                continue
            item = {
                "timestamp": row.get("time"),
                "event_type": row.get("type"),
                "repo_type": rt or repo_type,
                "repo_id": repo_id,
            }
            if not ctx._item_matches_where(item, normalized_where):
                continue
            matched += 1
            if len(items) < applied_limit:
                items.append(item)
        if not next_cursor:
            exhausted_feed = True
            break
    try:
        items = ctx._project_activity_items(items, fields)
    except ValueError as exc:
        return ctx._helper_error(
            start_calls=start_calls,
            source="/api/recent-activity",
            error=exc,
        )
    exact_count = exhausted_feed and (not stopped_for_budget)
    sample_complete = (
        exact_count and applied_limit >= matched and (not count_only or matched == 0)
    )
    page_limit_hit = (
        next_cursor is not None and pages >= pages_lim and (not exhausted_feed)
    )
    more_available: bool | str = ctx._derive_more_available(
        sample_complete=sample_complete,
        exact_count=exact_count,
        returned=len(items),
        total=matched if exact_count else None,
    )
    if next_cursor is not None:
        more_available = True
    elif stopped_for_budget and (not exact_count):
        more_available = "unknown"
    meta = ctx._build_exhaustive_result_meta(
        base_meta={
            "scanned": scanned,
            "total": matched,
            "total_matched": matched,
            "pages": pages,
            "count_source": "scan" if exact_count else "none",
            "lower_bound": not exact_count,
            "page_limit": page_lim,
            "stopped_for_budget": stopped_for_budget,
            "feed_type": ft,
            "entity": ent,
        },
        limit_plan=limit_plan,
        matched_count=matched,
        returned_count=len(items),
        exact_count=exact_count,
        count_only=count_only,
        sample_complete=sample_complete,
        more_available=more_available,
        page_limit_hit=page_limit_hit,
        truncated_extra=stopped_for_budget,
        requested_max_pages=requested_max_pages,
        applied_max_pages=pages_lim,
    )
    return ctx._helper_success(
        start_calls=start_calls,
        source="/api/recent-activity",
        items=items,
        meta=meta,
        cursor=next_cursor,
    )


def register_activity_helpers(
    ctx: HelperRuntimeContext,
) -> dict[str, Callable[..., Any]]:
    return {"hf_recent_activity": partial(hf_recent_activity, ctx)}
