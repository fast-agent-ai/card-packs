from __future__ import annotations

# ruff: noqa: C901, PLR0912, PLR0913, PLR0915, PLR0917
from functools import partial
from typing import Any, Callable

from ..aliases import COLLECTION_FIELD_ALIASES, REPO_FIELD_ALIASES
from ..constants import OUTPUT_ITEMS_TRUNCATION_LIMIT
from ..context_types import HelperRuntimeContext


async def hf_collections_search(
    ctx: HelperRuntimeContext,
    query: str | None = None,
    owner: str | None = None,
    return_limit: int = 20,
    count_only: bool = False,
    where: dict[str, Any] | None = None,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    start_calls = ctx.call_count["n"]
    default_return = ctx._policy_int("hf_collections_search", "default_return", 20)
    max_return = ctx._policy_int(
        "hf_collections_search", "max_return", OUTPUT_ITEMS_TRUNCATION_LIMIT
    )
    if count_only:
        return_limit = 0
    lim = ctx._clamp_int(
        return_limit, default=default_return, minimum=0, maximum=max_return
    )
    owner_clean = str(owner or "").strip() or None
    fetch_lim = max_return if lim == 0 or owner_clean else lim
    if owner_clean:
        fetch_lim = min(fetch_lim, 100)
    term = str(query or "").strip()
    if not term and owner_clean:
        term = owner_clean
    if not term:
        return ctx._helper_error(
            start_calls=start_calls,
            source="/api/collections",
            error="query or owner is required",
        )
    params: dict[str, Any] = {"limit": fetch_lim}
    if term:
        params["q"] = term
    if owner_clean:
        params["owner"] = owner_clean
    resp = ctx._host_raw_call("/api/collections", params=params)
    if not resp.get("ok"):
        return ctx._helper_error(
            start_calls=start_calls,
            source="/api/collections",
            error=resp.get("error") or "collections fetch failed",
        )
    payload = resp.get("data") if isinstance(resp.get("data"), list) else []
    items: list[dict[str, Any]] = []
    for row in payload[:fetch_lim]:
        if not isinstance(row, dict):
            continue
        row_owner = ctx._author_from_any(row.get("owner")) or ctx._author_from_any(
            row.get("ownerData")
        )
        if (
            not row_owner
            and isinstance(row.get("slug"), str)
            and "/" in str(row.get("slug"))
        ):
            row_owner = str(row.get("slug")).split("/", 1)[0]
        if owner_clean is not None and row_owner != owner_clean:
            continue
        owner_payload = row.get("owner") if isinstance(row.get("owner"), dict) else {}
        collection_items = (
            row.get("items") if isinstance(row.get("items"), list) else []
        )
        slug = row.get("slug")
        items.append(
            {
                "collection_id": slug,
                "slug": slug,
                "title": row.get("title"),
                "owner": row_owner,
                "owner_type": owner_payload.get("type")
                if isinstance(owner_payload.get("type"), str)
                else None,
                "description": row.get("description"),
                "gating": row.get("gating"),
                "last_updated": row.get("lastUpdated"),
                "item_count": len(collection_items),
            }
        )
    items = ctx._apply_where(items, where, aliases=COLLECTION_FIELD_ALIASES)
    total_matched = len(items)
    items = items[:lim]
    items = ctx._project_collection_items(items, fields)
    truncated = (
        lim > 0 and total_matched > lim or (lim == 0 and len(payload) >= fetch_lim)
    )
    return ctx._helper_success(
        start_calls=start_calls,
        source="/api/collections",
        items=items,
        scanned=len(payload),
        matched=total_matched,
        returned=len(items),
        total=len(payload),
        total_matched=total_matched,
        total_population=len(payload),
        truncated=truncated,
        complete=not truncated,
        query=term,
        owner=owner_clean,
    )


async def hf_collection_items(
    ctx: HelperRuntimeContext,
    collection_id: str,
    repo_types: list[str] | None = None,
    return_limit: int = 100,
    count_only: bool = False,
    where: dict[str, Any] | None = None,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    start_calls = ctx.call_count["n"]
    default_return = ctx._policy_int("hf_collection_items", "default_return", 100)
    max_return = ctx._policy_int(
        "hf_collection_items", "max_return", OUTPUT_ITEMS_TRUNCATION_LIMIT
    )
    cid = str(collection_id or "").strip()
    if not cid:
        return ctx._helper_error(
            start_calls=start_calls,
            source="/api/collections/<collection_id>",
            error="collection_id is required",
        )
    if count_only:
        return_limit = 0
    lim = ctx._clamp_int(
        return_limit, default=default_return, minimum=0, maximum=max_return
    )
    allowed_repo_types: set[str] | None = None
    try:
        raw_repo_types = (
            ctx._coerce_str_list(repo_types) if repo_types is not None else []
        )
    except ValueError as exc:
        return ctx._helper_error(
            start_calls=start_calls,
            source=f"/api/collections/{cid}",
            error=exc,
            collection_id=cid,
        )
    if raw_repo_types:
        allowed_repo_types = set()
        for raw in raw_repo_types:
            canonical = ctx._canonical_repo_type(raw, default="")
            if canonical not in {"model", "dataset", "space"}:
                return ctx._helper_error(
                    start_calls=start_calls,
                    source=f"/api/collections/{cid}",
                    error=f"Unsupported repo_type '{raw}'",
                    collection_id=cid,
                )
            allowed_repo_types.add(canonical)
    endpoint = f"/api/collections/{cid}"
    resp = ctx._host_raw_call(endpoint)
    if not resp.get("ok"):
        return ctx._helper_error(
            start_calls=start_calls,
            source=endpoint,
            error=resp.get("error") or "collection fetch failed",
            collection_id=cid,
        )
    payload = resp.get("data") if isinstance(resp.get("data"), dict) else {}
    raw_items = payload.get("items") if isinstance(payload.get("items"), list) else []
    owner = ctx._author_from_any(payload.get("owner"))
    owner_payload = (
        payload.get("owner") if isinstance(payload.get("owner"), dict) else {}
    )
    if owner is None and "/" in cid:
        owner = cid.split("/", 1)[0]
    normalized_where = ctx._normalize_where(where, aliases=REPO_FIELD_ALIASES)
    normalized: list[dict[str, Any]] = []
    for row in raw_items:
        if not isinstance(row, dict):
            continue
        item = ctx._normalize_collection_repo_item(row)
        if item is None:
            continue
        repo_type = item.get("repo_type")
        if allowed_repo_types is not None and repo_type not in allowed_repo_types:
            continue
        if not ctx._item_matches_where(item, normalized_where):
            continue
        normalized.append(item)
    total_matched = len(normalized)
    items = [] if count_only else normalized[:lim]
    items = ctx._project_repo_items(items, fields)
    truncated = lim > 0 and total_matched > lim
    return ctx._helper_success(
        start_calls=start_calls,
        source=endpoint,
        items=items,
        scanned=len(raw_items),
        matched=total_matched,
        returned=len(items),
        total=len(raw_items),
        total_matched=total_matched,
        total_population=len(raw_items),
        truncated=truncated,
        complete=not truncated,
        collection_id=cid,
        title=payload.get("title"),
        owner=owner,
        owner_type=owner_payload.get("type")
        if isinstance(owner_payload.get("type"), str)
        else None,
        repo_types=sorted(allowed_repo_types)
        if allowed_repo_types is not None
        else None,
    )


def register_collection_helpers(
    ctx: HelperRuntimeContext,
) -> dict[str, Callable[..., Any]]:
    return {
        "hf_collections_search": partial(hf_collections_search, ctx),
        "hf_collection_items": partial(hf_collection_items, ctx),
    }
