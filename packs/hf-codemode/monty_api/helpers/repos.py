from __future__ import annotations

# ruff: noqa: C901, PLR0912, PLR0913, PLR0915, PLR0917
from itertools import islice
from typing import Any, Callable
from huggingface_hub import HfApi
from ..context_types import HelperRuntimeContext
from ..aliases import (
    ACTOR_FIELD_ALIASES,
    DAILY_PAPER_FIELD_ALIASES,
    REPO_FIELD_ALIASES,
    USER_LIKES_FIELD_ALIASES,
)
from ..constants import (
    EXHAUSTIVE_HELPER_RETURN_HARD_CAP,
    LIKES_ENRICHMENT_MAX_REPOS,
    LIKES_RANKING_WINDOW_DEFAULT,
    LIKES_SCAN_LIMIT_CAP,
    OUTPUT_ITEMS_TRUNCATION_LIMIT,
    SELECTIVE_ENDPOINT_RETURN_HARD_CAP,
    TRENDING_ENDPOINT_MAX_LIMIT,
)
from ..registry import (
    REPO_SEARCH_DEFAULT_EXPAND,
    REPO_SEARCH_EXTRA_ARGS,
)


from .common import resolve_username_or_current

from functools import partial


def _normalize_user_likes_sort(sort: str | None) -> tuple[str | None, str | None]:
    raw = str(sort or "liked_at").strip()
    alias_map = {
        "": "liked_at",
        "likedat": "liked_at",
        "liked_at": "liked_at",
        "liked-at": "liked_at",
        "recency": "liked_at",
        "repolikes": "repo_likes",
        "repo_likes": "repo_likes",
        "repo-likes": "repo_likes",
        "repodownloads": "repo_downloads",
        "repo_downloads": "repo_downloads",
        "repo-downloads": "repo_downloads",
    }
    normalized = alias_map.get(raw.lower(), raw)
    if normalized not in {"liked_at", "repo_likes", "repo_downloads"}:
        return (None, "sort must be one of liked_at, repo_likes, repo_downloads")
    return (normalized, None)


async def hf_repo_search(
    ctx: HelperRuntimeContext,
    query: str | None = None,
    repo_type: str | None = None,
    repo_types: list[str] | None = None,
    author: str | None = None,
    filters: list[str] | None = None,
    sort: str | None = None,
    limit: int = 20,
    where: dict[str, Any] | None = None,
    fields: list[str] | None = None,
    advanced: dict[str, Any] | None = None,
) -> dict[str, Any]:
    start_calls = ctx.call_count["n"]
    default_return = ctx._policy_int("hf_repo_search", "default_return", 20)
    max_return = ctx._policy_int(
        "hf_repo_search", "max_return", SELECTIVE_ENDPOINT_RETURN_HARD_CAP
    )
    if repo_type is not None and repo_types is not None:
        return ctx._helper_error(
            start_calls=start_calls,
            source="/api/repos",
            error="Pass either repo_type or repo_types, not both",
        )
    if repo_types is None:
        if repo_type is None or not str(repo_type).strip():
            requested_repo_types = ["model"]
        else:
            rt = ctx._canonical_repo_type(repo_type, default="")
            if rt not in {"model", "dataset", "space"}:
                return ctx._helper_error(
                    start_calls=start_calls,
                    source="/api/repos",
                    error=f"Unsupported repo_type '{repo_type}'",
                )
            requested_repo_types = [rt]
    else:
        raw_types = ctx._coerce_str_list(repo_types)
        if not raw_types:
            return ctx._helper_error(
                start_calls=start_calls,
                source="/api/repos",
                error="repo_types must not be empty",
            )
        requested_repo_types: list[str] = []
        for raw in raw_types:
            rt = ctx._canonical_repo_type(raw, default="")
            if rt not in {"model", "dataset", "space"}:
                return ctx._helper_error(
                    start_calls=start_calls,
                    source="/api/repos",
                    error=f"Unsupported repo_type '{raw}'",
                )
            requested_repo_types.append(rt)
    filter_list = ctx._coerce_str_list(filters)
    term = str(query or "").strip()
    author_clean = str(author or "").strip() or None
    requested_limit = limit
    lim = ctx._clamp_int(limit, default=default_return, minimum=1, maximum=max_return)
    limit_meta = ctx._derive_limit_metadata(
        requested_return_limit=requested_limit,
        applied_return_limit=lim,
        default_limit_used=limit == default_return,
    )
    hard_cap_applied = bool(limit_meta.get("hard_cap_applied"))
    if advanced is not None and (not isinstance(advanced, dict)):
        return ctx._helper_error(
            start_calls=start_calls,
            source="/api/repos",
            error="advanced must be a dict when provided",
        )
    if advanced is not None and len(requested_repo_types) != 1:
        return ctx._helper_error(
            start_calls=start_calls,
            source="/api/repos",
            error="advanced may only be used with a single repo_type",
        )
    sort_keys: dict[str, str | None] = {}
    for rt in requested_repo_types:
        sort_key, sort_error = ctx._normalize_repo_sort_key(rt, sort)
        if sort_error:
            return ctx._helper_error(
                start_calls=start_calls, source=f"/api/{rt}s", error=sort_error
            )
        sort_keys[rt] = sort_key
    all_items: list[dict[str, Any]] = []
    scanned = 0
    source_endpoints: list[str] = []
    limit_boundary_hit = False
    api = ctx._get_hf_api_client()
    for rt in requested_repo_types:
        endpoint = f"/api/{rt}s"
        source_endpoints.append(endpoint)
        extra_args = dict(advanced or {}) if len(requested_repo_types) == 1 else {}
        allowed_extra = REPO_SEARCH_EXTRA_ARGS.get(rt, set())
        unsupported = sorted(
            (str(k) for k in extra_args.keys() if str(k) not in allowed_extra)
        )
        if unsupported:
            return ctx._helper_error(
                start_calls=start_calls,
                source=endpoint,
                error=f"Unsupported advanced args for repo_type='{rt}': {unsupported}. Allowed advanced args: {sorted(allowed_extra)}",
            )
        if "card_data" in extra_args and "cardData" not in extra_args:
            extra_args["cardData"] = extra_args.pop("card_data")
        else:
            extra_args.pop("card_data", None)
        if not any(
            (
                key in extra_args
                for key in ("expand", "full", "cardData", "fetch_config")
            )
        ):
            extra_args["expand"] = list(REPO_SEARCH_DEFAULT_EXPAND[rt])
        try:
            payload = ctx._host_hf_call(
                endpoint,
                lambda rt=rt, extra_args=extra_args: ctx._repo_list_call(
                    api,
                    rt,
                    search=term or None,
                    author=author_clean,
                    filter=filter_list or None,
                    sort=sort_keys[rt],
                    limit=lim,
                    **extra_args,
                ),
            )
        except Exception as e:
            return ctx._helper_error(start_calls=start_calls, source=endpoint, error=e)
        scanned += len(payload)
        if len(payload) >= lim:
            limit_boundary_hit = True
        all_items.extend(
            (ctx._normalize_repo_search_row(row, rt) for row in payload[:lim])
        )
    all_items = ctx._apply_where(all_items, where, aliases=REPO_FIELD_ALIASES)
    combined_sort_key = next(iter(sort_keys.values()), None)
    all_items = ctx._sort_repo_rows(all_items, combined_sort_key)
    matched = len(all_items)
    all_items = ctx._project_repo_items(all_items[:lim], fields)
    more_available: bool | str = False
    truncated = False
    truncated_by = "none"
    next_request_hint: str | None = None
    if hard_cap_applied and scanned >= lim:
        truncated = True
        truncated_by = "hard_cap"
        more_available = "unknown"
        next_request_hint = f"Increase limit above {lim} to improve coverage"
    elif limit_boundary_hit:
        more_available = "unknown"
        next_request_hint = (
            f"Increase limit above {lim} to check whether more rows exist"
        )
    return ctx._helper_success(
        start_calls=start_calls,
        source=",".join(source_endpoints),
        items=all_items,
        query=term or None,
        repo_types=requested_repo_types,
        filters=filter_list or None,
        sort=combined_sort_key,
        author=author_clean,
        limit=lim,
        scanned=scanned,
        matched=matched,
        returned=len(all_items),
        truncated=truncated,
        truncated_by=truncated_by,
        more_available=more_available,
        limit_boundary_hit=limit_boundary_hit,
        next_request_hint=next_request_hint,
        **limit_meta,
    )


async def hf_user_likes(
    ctx: HelperRuntimeContext,
    username: str | None = None,
    repo_types: list[str] | None = None,
    return_limit: int | None = None,
    scan_limit: int | None = None,
    count_only: bool = False,
    where: dict[str, Any] | None = None,
    fields: list[str] | None = None,
    sort: str | None = None,
    ranking_window: int | None = None,
) -> dict[str, Any]:
    start_calls = ctx.call_count["n"]
    default_return = ctx._policy_int("hf_user_likes", "default_return", 100)
    scan_cap = ctx._policy_int("hf_user_likes", "scan_max", LIKES_SCAN_LIMIT_CAP)
    ranking_default = ctx._policy_int(
        "hf_user_likes", "ranking_default", LIKES_RANKING_WINDOW_DEFAULT
    )
    enrich_cap = ctx._policy_int(
        "hf_user_likes", "enrich_max", LIKES_ENRICHMENT_MAX_REPOS
    )
    resolved_username, resolve_error = await resolve_username_or_current(ctx, username)
    if resolve_error:
        return ctx._helper_error(
            start_calls=start_calls,
            source="/api/users/<u>/likes",
            error=resolve_error,
        )
    if not isinstance(resolved_username, str):
        return ctx._helper_error(
            start_calls=start_calls,
            source="/api/users/<u>/likes",
            error="username is required",
        )
    sort_key, sort_error = _normalize_user_likes_sort(sort)
    if sort_error:
        return ctx._helper_error(
            start_calls=start_calls,
            source=f"/api/users/{resolved_username}/likes",
            error=sort_error,
        )
    if sort_key is None:
        return ctx._helper_error(
            start_calls=start_calls,
            source=f"/api/users/{resolved_username}/likes",
            error="sort must be one of liked_at, repo_likes, repo_downloads",
        )
    limit_plan = ctx._resolve_exhaustive_limits(
        return_limit=return_limit,
        count_only=count_only,
        default_return=default_return,
        max_return=EXHAUSTIVE_HELPER_RETURN_HARD_CAP,
        scan_limit=scan_limit,
        scan_cap=scan_cap,
    )
    ret_lim = int(limit_plan["applied_return_limit"])
    scan_lim = int(limit_plan["applied_scan_limit"])
    normalized_where = ctx._normalize_where(where, aliases=USER_LIKES_FIELD_ALIASES)
    allowed_repo_types: set[str] | None = None
    try:
        raw_repo_types: list[str] = (
            ctx._coerce_str_list(repo_types) if repo_types is not None else []
        )
    except ValueError as e:
        return ctx._helper_error(
            start_calls=start_calls,
            source=f"/api/users/{resolved_username}/likes",
            error=e,
        )
    if raw_repo_types:
        allowed_repo_types = set()
        for raw in raw_repo_types:
            canonical = ctx._canonical_repo_type(raw, default="")
            if canonical not in {"model", "dataset", "space"}:
                return ctx._helper_error(
                    start_calls=start_calls,
                    source=f"/api/users/{resolved_username}/likes",
                    error=f"Unsupported repo_type '{raw}'",
                )
            allowed_repo_types.add(canonical)
    endpoint = f"/api/users/{resolved_username}/likes"
    resp = ctx._host_raw_call(endpoint, params={"limit": scan_lim})
    if not resp.get("ok"):
        return ctx._helper_error(
            start_calls=start_calls,
            source=endpoint,
            error=resp.get("error") or "likes fetch failed",
        )
    payload = resp.get("data") if isinstance(resp.get("data"), list) else []
    scanned_rows = payload[:scan_lim]
    matched_rows: list[tuple[int, dict[str, Any]]] = []
    for row in scanned_rows:
        if not isinstance(row, dict):
            continue
        repo = row.get("repo") if isinstance(row.get("repo"), dict) else {}
        repo_data = row.get("repoData") if isinstance(row.get("repoData"), dict) else {}
        repo_id = repo_data.get("id") or repo_data.get("name") or repo.get("name")
        if not isinstance(repo_id, str) or not repo_id:
            continue
        repo_type = ctx._canonical_repo_type(
            repo_data.get("type") or repo.get("type"), default=""
        )
        if not repo_type:
            repo_type = ctx._canonical_repo_type(repo.get("type"), default="model")
        if allowed_repo_types is not None and repo_type not in allowed_repo_types:
            continue
        repo_author = repo_data.get("author")
        if not isinstance(repo_author, str) and "/" in repo_id:
            repo_author = repo_id.split("/", 1)[0]
        item = {
            "liked_at": row.get("likedAt") or row.get("createdAt"),
            "repo_id": repo_id,
            "repo_type": repo_type,
            "repo_author": repo_author,
            "repo_likes": ctx._as_int(repo_data.get("likes")),
            "repo_downloads": ctx._as_int(repo_data.get("downloads")),
            "repo_url": ctx._repo_web_url(repo_type, repo_id),
        }
        if not ctx._item_matches_where(item, normalized_where):
            continue
        matched_rows.append((len(matched_rows), item))
    matched = len(matched_rows)
    scan_exhaustive = len(payload) < scan_lim
    exact_count = scan_exhaustive
    total_matched = matched
    total = total_matched
    effective_ranking_window: int | None = None
    ranking_complete = sort_key == "liked_at" and exact_count
    enriched = 0
    selected_pairs: list[tuple[int, dict[str, Any]]]
    if count_only:
        selected_pairs = []
        ranking_complete = False if matched > 0 else exact_count
    elif sort_key == "liked_at":
        selected_pairs = matched_rows[:ret_lim]
    else:
        metric = str(sort_key)
        requested_window = (
            ranking_window if ranking_window is not None else ranking_default
        )
        effective_ranking_window = ctx._clamp_int(
            requested_window, default=ranking_default, minimum=1, maximum=enrich_cap
        )
        shortlist_size = min(effective_ranking_window, matched, scan_lim)
        shortlist = matched_rows[:shortlist_size]
        candidates = [
            pair
            for pair in shortlist
            if pair[1].get(metric) is None
            and isinstance(pair[1].get("repo_id"), str)
            and (pair[1].get("repo_type") in {"model", "dataset", "space"})
        ]
        enrich_budget = min(len(candidates), ctx._budget_remaining(), shortlist_size)
        for _, item in candidates[:enrich_budget]:
            repo_type = str(item.get("repo_type"))
            repo_id = str(item.get("repo_id"))
            detail_endpoint = f"/api/{ctx._canonical_repo_type(repo_type)}s/{repo_id}"
            try:
                detail = ctx._host_hf_call(
                    detail_endpoint,
                    lambda rt=repo_type, rid=repo_id: ctx._repo_detail_call(
                        ctx._get_hf_api_client(), rt, rid
                    ),
                )
            except Exception:
                continue
            likes = ctx._as_int(getattr(detail, "likes", None))
            downloads = ctx._as_int(getattr(detail, "downloads", None))
            if likes is not None:
                item["repo_likes"] = likes
            if downloads is not None:
                item["repo_downloads"] = downloads
            enriched += 1

        def _ranking_key(pair: tuple[int, dict[str, Any]]) -> tuple[int, int, int]:
            idx, row = pair
            metric_value = ctx._as_int(row.get(metric))
            if metric_value is None:
                return (1, 0, idx)
            return (0, -metric_value, idx)

        ranked_shortlist = sorted(shortlist, key=_ranking_key)
        selected_pairs = ranked_shortlist[:ret_lim]
        ranking_complete = (
            exact_count
            and shortlist_size >= matched
            and (len(candidates) <= enrich_budget)
        )
    items = ctx._project_user_like_items([row for _, row in selected_pairs], fields)
    popularity_present = sum(
        (1 for _, row in selected_pairs if row.get("repo_likes") is not None)
    )
    sample_complete = (
        exact_count
        and ret_lim >= matched
        and (sort_key == "liked_at" or ranking_complete)
        and (not count_only or matched == 0)
    )
    scan_limit_hit = not scan_exhaustive and len(payload) >= scan_lim
    more_available = ctx._derive_more_available(
        sample_complete=sample_complete,
        exact_count=exact_count,
        returned=len(items),
        total=total,
    )
    if scan_limit_hit:
        more_available = "unknown" if allowed_repo_types is not None or where else True
    meta = ctx._build_exhaustive_result_meta(
        base_meta={
            "scanned": len(scanned_rows),
            "total": total,
            "total_available": len(payload),
            "total_matched": total_matched,
            "count_source": "scan",
            "lower_bound": not exact_count,
            "enriched": enriched,
            "popularity_present": popularity_present,
            "sort_applied": sort_key,
            "ranking_window": effective_ranking_window,
            "ranking_complete": ranking_complete,
            "username": resolved_username,
        },
        limit_plan=limit_plan,
        matched_count=matched,
        returned_count=len(items),
        exact_count=exact_count,
        count_only=count_only,
        sample_complete=sample_complete,
        more_available=more_available,
        scan_limit_hit=scan_limit_hit,
        truncated_extra=sort_key != "liked_at" and (not ranking_complete),
    )
    return ctx._helper_success(
        start_calls=start_calls, source=endpoint, items=items, meta=meta
    )


async def hf_repo_likers(
    ctx: HelperRuntimeContext,
    repo_id: str,
    repo_type: str,
    return_limit: int | None = None,
    count_only: bool = False,
    pro_only: bool | None = None,
    where: dict[str, Any] | None = None,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    start_calls = ctx.call_count["n"]
    rid = str(repo_id or "").strip()
    if not rid:
        return ctx._helper_error(
            start_calls=start_calls,
            source="/api/repos/<repo>/likers",
            error="repo_id is required",
        )
    rt = ctx._canonical_repo_type(repo_type, default="")
    if rt not in {"model", "dataset", "space"}:
        return ctx._helper_error(
            start_calls=start_calls,
            source=f"/api/repos/{rid}/likers",
            error=f"Unsupported repo_type '{repo_type}'",
            repo_id=rid,
        )
    default_return = ctx._policy_int("hf_repo_likers", "default_return", 1000)
    requested_return_limit = return_limit
    default_limit_used = requested_return_limit is None and (not count_only)
    has_where = isinstance(where, dict) and bool(where)
    endpoint = f"/api/{rt}s/{rid}/likers"
    resp = ctx._host_raw_call(endpoint)
    if not resp.get("ok"):
        return ctx._helper_error(
            start_calls=start_calls,
            source=endpoint,
            error=resp.get("error") or "repo likers fetch failed",
            repo_id=rid,
            repo_type=rt,
        )
    payload = resp.get("data") if isinstance(resp.get("data"), list) else []
    normalized_where = ctx._normalize_where(where, aliases=ACTOR_FIELD_ALIASES)
    normalized: list[dict[str, Any]] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        username = row.get("user") or row.get("username")
        if not isinstance(username, str) or not username:
            continue
        item = {
            "username": username,
            "fullname": row.get("fullname"),
            "type": row.get("type")
            if isinstance(row.get("type"), str) and row.get("type")
            else "user",
            "isPro": row.get("isPro"),
        }
        if pro_only is True and item.get("isPro") is not True:
            continue
        if pro_only is False and item.get("isPro") is True:
            continue
        if not ctx._item_matches_where(item, normalized_where):
            continue
        normalized.append(item)
    if count_only:
        ret_lim = 0
    elif requested_return_limit is None:
        ret_lim = default_return
    else:
        try:
            ret_lim = max(0, int(requested_return_limit))
        except Exception:
            ret_lim = default_return
    limit_plan = {
        "requested_return_limit": requested_return_limit,
        "applied_return_limit": ret_lim,
        "default_limit_used": default_limit_used,
        "hard_cap_applied": False,
    }
    matched = len(normalized)
    items = [] if count_only else normalized[:ret_lim]
    return_limit_hit = ret_lim > 0 and matched > ret_lim
    truncated_by = ctx._derive_truncated_by(
        hard_cap=False, return_limit_hit=return_limit_hit
    )
    sample_complete = matched <= ret_lim and (not count_only or matched == 0)
    truncated = truncated_by != "none"
    more_available = ctx._derive_more_available(
        sample_complete=sample_complete,
        exact_count=True,
        returned=len(items),
        total=matched,
    )
    items = ctx._project_actor_items(items, fields)
    meta = ctx._build_exhaustive_meta(
        base_meta={
            "scanned": len(payload),
            "matched": matched,
            "returned": len(items),
            "total": matched,
            "total_available": len(payload),
            "total_matched": matched,
            "truncated": truncated,
            "count_source": "likers_list",
            "lower_bound": False,
            "repo_id": rid,
            "repo_type": rt,
            "pro_only": pro_only,
            "where_applied": has_where,
            "upstream_pagination": "none",
        },
        limit_plan=limit_plan,
        sample_complete=sample_complete,
        exact_count=True,
        truncated_by=truncated_by,
        more_available=more_available,
    )
    meta["hard_cap_applied"] = False
    return ctx._helper_success(
        start_calls=start_calls, source=endpoint, items=items, meta=meta
    )


async def hf_repo_discussions(
    ctx: HelperRuntimeContext, repo_type: str, repo_id: str, limit: int = 20
) -> dict[str, Any]:
    start_calls = ctx.call_count["n"]
    rt = ctx._canonical_repo_type(repo_type)
    rid = str(repo_id or "").strip()
    if "/" not in rid:
        return ctx._helper_error(
            start_calls=start_calls,
            source="/api/.../discussions",
            error="repo_id must be owner/name",
        )
    lim = ctx._clamp_int(
        limit, default=20, minimum=1, maximum=SELECTIVE_ENDPOINT_RETURN_HARD_CAP
    )
    endpoint = f"/api/{rt}s/{rid}/discussions"
    try:
        discussions = ctx._host_hf_call(
            endpoint,
            lambda: list(
                islice(
                    ctx._get_hf_api_client().get_repo_discussions(
                        repo_id=rid, repo_type=rt
                    ),
                    lim,
                )
            ),
        )
    except Exception as e:
        return ctx._helper_error(start_calls=start_calls, source=endpoint, error=e)
    items: list[dict[str, Any]] = []
    for d in discussions:
        num = ctx._as_int(getattr(d, "num", None))
        items.append(
            {
                "num": num,
                "number": num,
                "discussionNum": num,
                "id": num,
                "title": getattr(d, "title", None),
                "author": getattr(d, "author", None),
                "createdAt": str(getattr(d, "created_at", None))
                if getattr(d, "created_at", None) is not None
                else None,
                "status": getattr(d, "status", None),
            }
        )
    return ctx._helper_success(
        start_calls=start_calls,
        source=endpoint,
        items=items,
        scanned=len(items),
        matched=len(items),
        returned=len(items),
        truncated=False,
        total_count=None,
    )


async def hf_repo_discussion_details(
    ctx: HelperRuntimeContext, repo_type: str, repo_id: str, discussion_num: int
) -> dict[str, Any]:
    start_calls = ctx.call_count["n"]
    rt = ctx._canonical_repo_type(repo_type)
    rid = str(repo_id or "").strip()
    if "/" not in rid:
        return ctx._helper_error(
            start_calls=start_calls,
            source="/api/.../discussions/<num>",
            error="repo_id must be owner/name",
        )
    num = ctx._as_int(discussion_num)
    if num is None:
        return ctx._helper_error(
            start_calls=start_calls,
            source=f"/api/{rt}s/{rid}/discussions/<num>",
            error="discussion_num must be an integer",
        )
    endpoint = f"/api/{rt}s/{rid}/discussions/{num}"
    try:
        detail = ctx._host_hf_call(
            endpoint,
            lambda: ctx._get_hf_api_client().get_discussion_details(
                repo_id=rid, discussion_num=int(num), repo_type=rt
            ),
        )
    except Exception as e:
        return ctx._helper_error(start_calls=start_calls, source=endpoint, error=e)
    comment_events: list[dict[str, Any]] = []
    raw_events = getattr(detail, "events", None)
    if isinstance(raw_events, list):
        for event in raw_events:
            if str(getattr(event, "type", "")).strip().lower() != "comment":
                continue
            comment_events.append(
                {
                    "author": getattr(event, "author", None),
                    "createdAt": ctx._dt_to_str(getattr(event, "created_at", None)),
                    "text": getattr(event, "content", None),
                    "rendered": getattr(event, "rendered", None),
                }
            )
    latest_comment: dict[str, Any] | None = None
    if comment_events:
        latest_comment = max(
            comment_events, key=lambda row: str(row.get("createdAt") or "")
        )
    item: dict[str, Any] = {
        "num": num,
        "number": num,
        "discussionNum": num,
        "id": num,
        "repo_id": rid,
        "repo_type": rt,
        "title": getattr(detail, "title", None),
        "author": getattr(detail, "author", None),
        "createdAt": ctx._dt_to_str(getattr(detail, "created_at", None)),
        "status": getattr(detail, "status", None),
        "url": getattr(detail, "url", None),
        "commentCount": len(comment_events),
        "latestCommentAuthor": latest_comment.get("author") if latest_comment else None,
        "latestCommentCreatedAt": latest_comment.get("createdAt")
        if latest_comment
        else None,
        "latestCommentText": latest_comment.get("text") if latest_comment else None,
        "latestCommentHtml": latest_comment.get("rendered") if latest_comment else None,
        "latest_comment_author": latest_comment.get("author")
        if latest_comment
        else None,
        "latest_comment_created_at": latest_comment.get("createdAt")
        if latest_comment
        else None,
        "latest_comment_text": latest_comment.get("text") if latest_comment else None,
        "latest_comment_html": latest_comment.get("rendered")
        if latest_comment
        else None,
    }
    return ctx._helper_success(
        start_calls=start_calls,
        source=endpoint,
        items=[item],
        scanned=len(comment_events),
        matched=1,
        returned=1,
        truncated=False,
        total_comments=len(comment_events),
    )


def _resolve_repo_detail_row(
    ctx: HelperRuntimeContext,
    api: HfApi,
    repo_id: str,
    attempt_types: list[str],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    rid = str(repo_id or "").strip()
    if "/" not in rid:
        return (None, {"repo_id": rid, "error": "repo_id must be owner/name"})
    resolved_type: str | None = None
    detail: Any = None
    last_endpoint = "/api/repos"
    errors: list[str] = []
    for rt in attempt_types:
        endpoint = f"/api/{rt}s/{rid}"
        last_endpoint = endpoint
        try:
            detail = ctx._host_hf_call(
                endpoint, lambda rt=rt, rid=rid: ctx._repo_detail_call(api, rt, rid)
            )
            resolved_type = rt
            break
        except Exception as e:
            errors.append(f"{rt}: {str(e)}")
    if resolved_type is None or detail is None:
        return (
            None,
            {
                "repo_id": rid,
                "error": "; ".join(errors[:3]) if errors else "repo lookup failed",
                "attempted_repo_types": list(attempt_types),
                "source": last_endpoint,
            },
        )
    return (ctx._normalize_repo_detail_row(detail, resolved_type, rid), None)


async def hf_repo_details(
    ctx: HelperRuntimeContext,
    repo_id: str | None = None,
    repo_ids: list[str] | None = None,
    repo_type: str = "auto",
    fields: list[str] | None = None,
) -> dict[str, Any]:
    start_calls = ctx.call_count["n"]
    if repo_id is not None and repo_ids is not None:
        return ctx._helper_error(
            start_calls=start_calls,
            source="/api/repos",
            error="Pass either repo_id or repo_ids, not both",
        )
    requested_ids = (
        [str(repo_id).strip()]
        if isinstance(repo_id, str) and str(repo_id).strip()
        else []
    )
    if repo_ids is not None:
        requested_ids = ctx._coerce_str_list(repo_ids)
    if not requested_ids:
        return ctx._helper_error(
            start_calls=start_calls,
            source="/api/repos",
            error="repo_id or repo_ids is required",
        )
    raw_type = str(repo_type or "auto").strip().lower()
    if raw_type in {"", "auto"}:
        base_attempt_types = ["model", "dataset", "space"]
    else:
        canonical_type = ctx._canonical_repo_type(raw_type, default="")
        if canonical_type not in {"model", "dataset", "space"}:
            return ctx._helper_error(
                start_calls=start_calls,
                source="/api/repos",
                error=f"Unsupported repo_type '{repo_type}'",
            )
        base_attempt_types = [canonical_type]
    api = ctx._get_hf_api_client()
    items: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for rid in requested_ids:
        row, failure = _resolve_repo_detail_row(ctx, api, rid, base_attempt_types)
        if row is None:
            if failure is not None:
                failures.append(failure)
            continue
        items.append(row)
    if not items:
        summary = failures[0]["error"] if failures else "repo lookup failed"
        return ctx._helper_error(
            start_calls=start_calls,
            source="/api/repos",
            error=summary,
            failures=failures,
            repo_type=repo_type,
        )
    items = ctx._project_repo_items(items, fields)
    return ctx._helper_success(
        start_calls=start_calls,
        source="/api/repos",
        items=items,
        repo_type=repo_type,
        requested_repo_ids=requested_ids,
        failures=failures or None,
        matched=len(items),
        returned=len(items),
    )


async def hf_trending(
    ctx: HelperRuntimeContext,
    repo_type: str = "model",
    limit: int = 20,
    where: dict[str, Any] | None = None,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    start_calls = ctx.call_count["n"]
    default_return = ctx._policy_int("hf_trending", "default_return", 20)
    max_return = ctx._policy_int(
        "hf_trending", "max_return", TRENDING_ENDPOINT_MAX_LIMIT
    )
    raw_type = str(repo_type or "model").strip().lower()
    if raw_type == "all":
        requested_type = "all"
    else:
        requested_type = ctx._canonical_repo_type(raw_type, default="")
        if requested_type not in {"model", "dataset", "space"}:
            return ctx._helper_error(
                start_calls=start_calls,
                source="/api/trending",
                error=f"Unsupported repo_type '{repo_type}'",
            )
    lim = ctx._clamp_int(limit, default=default_return, minimum=1, maximum=max_return)
    resp = ctx._host_raw_call(
        "/api/trending", params={"type": requested_type, "limit": lim}
    )
    if not resp.get("ok"):
        return ctx._helper_error(
            start_calls=start_calls,
            source="/api/trending",
            error=resp.get("error") or "trending fetch failed",
        )
    payload = resp.get("data") if isinstance(resp.get("data"), dict) else {}
    rows = (
        payload.get("recentlyTrending")
        if isinstance(payload.get("recentlyTrending"), list)
        else []
    )
    items: list[dict[str, Any]] = []
    default_row_type = requested_type if requested_type != "all" else "model"
    for idx, row in enumerate(rows[:lim], start=1):
        if not isinstance(row, dict):
            continue
        repo = row.get("repoData") if isinstance(row.get("repoData"), dict) else {}
        items.append(ctx._normalize_trending_row(repo, default_row_type, rank=idx))
    items = ctx._apply_where(items, where, aliases=REPO_FIELD_ALIASES)
    matched = len(items)
    items = ctx._project_repo_items(items[:lim], fields)
    return ctx._helper_success(
        start_calls=start_calls,
        source="/api/trending",
        items=items,
        repo_type=requested_type,
        limit=lim,
        scanned=len(rows),
        matched=matched,
        returned=len(items),
        trending_score_available=any(
            (item.get("trending_score") is not None for item in items)
        ),
        ordered_ranking=True,
    )


async def hf_daily_papers(
    ctx: HelperRuntimeContext,
    limit: int = 20,
    where: dict[str, Any] | None = None,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    start_calls = ctx.call_count["n"]
    default_return = ctx._policy_int("hf_daily_papers", "default_return", 20)
    max_return = ctx._policy_int(
        "hf_daily_papers", "max_return", OUTPUT_ITEMS_TRUNCATION_LIMIT
    )
    lim = ctx._clamp_int(limit, default=default_return, minimum=1, maximum=max_return)
    resp = ctx._host_raw_call("/api/daily_papers", params={"limit": lim})
    if not resp.get("ok"):
        return ctx._helper_error(
            start_calls=start_calls,
            source="/api/daily_papers",
            error=resp.get("error") or "daily papers fetch failed",
        )
    payload = resp.get("data") if isinstance(resp.get("data"), list) else []
    items: list[dict[str, Any]] = []
    for idx, row in enumerate(payload[:lim], start=1):
        if not isinstance(row, dict):
            continue
        items.append(ctx._normalize_daily_paper_row(row, rank=idx))
    items = ctx._apply_where(items, where, aliases=DAILY_PAPER_FIELD_ALIASES)
    matched = len(items)
    items = ctx._project_daily_paper_items(items[:lim], fields)
    return ctx._helper_success(
        start_calls=start_calls,
        source="/api/daily_papers",
        items=items,
        limit=lim,
        scanned=len(payload),
        matched=matched,
        returned=len(items),
        ordered_ranking=True,
    )


def register_repo_helpers(ctx: HelperRuntimeContext) -> dict[str, Callable[..., Any]]:
    return {
        "hf_repo_search": partial(hf_repo_search, ctx),
        "hf_user_likes": partial(hf_user_likes, ctx),
        "hf_repo_likers": partial(hf_repo_likers, ctx),
        "hf_repo_discussions": partial(hf_repo_discussions, ctx),
        "hf_repo_discussion_details": partial(hf_repo_discussion_details, ctx),
        "hf_repo_details": partial(hf_repo_details, ctx),
        "hf_trending": partial(hf_trending, ctx),
        "hf_daily_papers": partial(hf_daily_papers, ctx),
    }
