from __future__ import annotations

# ruff: noqa: C901, PLR0912, PLR0913, PLR0915, PLR0917
from functools import partial
from typing import Any, Callable

from ..constants import (
    COLLECTION_CANONICAL_FIELDS,
    OUTPUT_ITEMS_TRUNCATION_LIMIT,
    REPO_CANONICAL_FIELDS,
)
from ..context_types import HelperRuntimeContext


async def hf_collections_search(
    ctx: HelperRuntimeContext,
    query: str | None = None,
    owner: str | None = None,
    limit: int = 20,
    count_only: bool = False,
    where: dict[str, Any] | None = None,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    start_calls = ctx.call_count["n"]
    default_limit = ctx._policy_int("hf_collections_search", "default_limit", 20)
    max_limit = ctx._policy_int(
        "hf_collections_search", "max_limit", OUTPUT_ITEMS_TRUNCATION_LIMIT
    )
    if count_only:
        limit = 0
    applied_limit = ctx._clamp_int(
        limit,
        default=default_limit,
        minimum=0,
        maximum=max_limit,
    )
    owner_clean = str(owner or "").strip() or None
    owner_casefold = owner_clean.casefold() if owner_clean is not None else None
    fetch_limit = max_limit if applied_limit == 0 or owner_clean else applied_limit
    if owner_clean:
        fetch_limit = min(fetch_limit, 100)
    term = str(query or "").strip()
    if not term and owner_clean:
        term = owner_clean
    if not term:
        return ctx._helper_error(
            start_calls=start_calls,
            source="/api/collections",
            error="query or owner is required",
        )
    params: dict[str, Any] = {"limit": fetch_limit}
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

    def _row_owner_matches_owner(row: Any) -> bool:
        if owner_casefold is None or not isinstance(row, dict):
            return owner_casefold is None
        row_owner = ctx._author_from_any(row.get("owner")) or ctx._author_from_any(
            row.get("ownerData")
        )
        if (
            not row_owner
            and isinstance(row.get("slug"), str)
            and "/" in str(row.get("slug"))
        ):
            row_owner = str(row.get("slug")).split("/", 1)[0]
        if not isinstance(row_owner, str) or not row_owner:
            return False
        return row_owner.casefold() == owner_casefold

    owner_fallback_used = False
    if owner_casefold is not None and not any(
        _row_owner_matches_owner(row) for row in payload
    ):
        fallback_params: dict[str, Any] = {"limit": fetch_limit}
        if term:
            fallback_params["q"] = term
        fallback_resp = ctx._host_raw_call("/api/collections", params=fallback_params)
        if fallback_resp.get("ok"):
            fallback_payload = (
                fallback_resp.get("data")
                if isinstance(fallback_resp.get("data"), list)
                else []
            )
            if any(_row_owner_matches_owner(row) for row in fallback_payload):
                payload = fallback_payload
                owner_fallback_used = True

    items: list[dict[str, Any]] = []
    for row in payload[:fetch_limit]:
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
        if owner_casefold is not None and (
            not isinstance(row_owner, str) or row_owner.casefold() != owner_casefold
        ):
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
    try:
        items = ctx._apply_where(
            items, where, allowed_fields=COLLECTION_CANONICAL_FIELDS
        )
    except ValueError as exc:
        return ctx._helper_error(
            start_calls=start_calls,
            source="/api/collections",
            error=exc,
        )
    total_matched = len(items)
    items = items[:applied_limit]
    try:
        items = ctx._project_collection_items(items, fields)
    except ValueError as exc:
        return ctx._helper_error(
            start_calls=start_calls,
            source="/api/collections",
            error=exc,
        )
    truncated = (
        applied_limit > 0 and total_matched > applied_limit
        or (applied_limit == 0 and len(payload) >= fetch_limit)
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
        owner_case_insensitive_fallback=owner_fallback_used,
    )


async def hf_collection_items(
    ctx: HelperRuntimeContext,
    collection_id: str,
    repo_types: list[str] | None = None,
    limit: int = 100,
    count_only: bool = False,
    where: dict[str, Any] | None = None,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    start_calls = ctx.call_count["n"]
    default_limit = ctx._policy_int("hf_collection_items", "default_limit", 100)
    max_limit = ctx._policy_int(
        "hf_collection_items", "max_limit", OUTPUT_ITEMS_TRUNCATION_LIMIT
    )
    cid = str(collection_id or "").strip()
    if not cid:
        return ctx._helper_error(
            start_calls=start_calls,
            source="/api/collections/<collection_id>",
            error="collection_id is required",
        )
    if count_only:
        limit = 0
    applied_limit = ctx._clamp_int(
        limit,
        default=default_limit,
        minimum=0,
        maximum=max_limit,
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
    try:
        normalized_where = ctx._normalize_where(
            where, allowed_fields=REPO_CANONICAL_FIELDS
        )
    except ValueError as exc:
        return ctx._helper_error(
            start_calls=start_calls,
            source=endpoint,
            error=exc,
            collection_id=cid,
        )
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
    items = [] if count_only else normalized[:applied_limit]
    try:
        items = ctx._project_repo_items(items, fields)
    except ValueError as exc:
        return ctx._helper_error(
            start_calls=start_calls,
            source=endpoint,
            error=exc,
            collection_id=cid,
        )
    truncated = applied_limit > 0 and total_matched > applied_limit
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
