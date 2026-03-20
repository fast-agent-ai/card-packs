from __future__ import annotations

# ruff: noqa: C901, PLR0912, PLR0913, PLR0915, PLR0917
from itertools import islice
from typing import Any, Callable
from huggingface_hub import HfApi
from ..context_types import HelperRuntimeContext
from ..constants import (
    ACTOR_CANONICAL_FIELDS,
    DAILY_PAPER_CANONICAL_FIELDS,
    EXHAUSTIVE_HELPER_RETURN_HARD_CAP,
    LIKES_ENRICHMENT_MAX_REPOS,
    LIKES_RANKING_WINDOW_DEFAULT,
    LIKES_SCAN_LIMIT_CAP,
    OUTPUT_ITEMS_TRUNCATION_LIMIT,
    REPO_CANONICAL_FIELDS,
    SELECTIVE_ENDPOINT_RETURN_HARD_CAP,
    TRENDING_ENDPOINT_MAX_LIMIT,
    USER_LIKES_CANONICAL_FIELDS,
)
from ..registry import (
    REPO_SEARCH_ALLOWED_EXPAND,
    REPO_SEARCH_DEFAULT_EXPAND,
    REPO_SEARCH_EXTRA_ARGS,
)


from .common import resolve_username_or_current

from functools import partial


def _sanitize_repo_expand_values(
    repo_type: str, raw_expand: Any
) -> tuple[list[str] | None, list[str], str | None]:
    if raw_expand is None:
        return (None, [], None)
    if isinstance(raw_expand, str):
        requested_values = [raw_expand]
    elif isinstance(raw_expand, (list, tuple, set)):
        requested_values = list(raw_expand)
    else:
        return (None, [], "expand must be a string or a list of strings")

    cleaned: list[str] = []
    for value in requested_values:
        value_str = str(value).strip()
        if value_str and value_str not in cleaned:
            cleaned.append(value_str)

    allowed = set(REPO_SEARCH_ALLOWED_EXPAND.get(repo_type, ()))
    dropped = [value for value in cleaned if value not in allowed]
    kept = [value for value in cleaned if value in allowed]
    return (kept or None, dropped, None)


def _resolve_repo_search_types(
    ctx: HelperRuntimeContext,
    *,
    repo_type: str | None,
    repo_types: list[str] | None,
    default_repo_type: str = "model",
) -> tuple[list[str] | None, str | None]:
    if repo_type is not None and repo_types is not None:
        return (None, "Pass either repo_type or repo_types, not both")

    if repo_types is None:
        raw_type = str(repo_type or "").strip()
        if not raw_type:
            return ([default_repo_type], None)
        canonical = ctx._canonical_repo_type(raw_type, default="")
        if canonical not in {"model", "dataset", "space"}:
            return (None, f"Unsupported repo_type '{repo_type}'")
        return ([canonical], None)

    raw_types = ctx._coerce_str_list(repo_types)
    if not raw_types:
        return (None, "repo_types must not be empty")

    requested_repo_types: list[str] = []
    for raw in raw_types:
        canonical = ctx._canonical_repo_type(raw, default="")
        if canonical not in {"model", "dataset", "space"}:
            return (None, f"Unsupported repo_type '{raw}'")
        if canonical not in requested_repo_types:
            requested_repo_types.append(canonical)
    return (requested_repo_types, None)


def _clean_repo_search_text(value: str | None) -> str | None:
    cleaned = str(value or "").strip()
    return cleaned or None


def _normalize_repo_search_filter(
    ctx: HelperRuntimeContext, value: str | list[str] | None
) -> tuple[list[str] | None, str | None]:
    if value is None:
        return (None, None)
    try:
        normalized = ctx._coerce_str_list(value)
    except ValueError:
        return (None, "filter must be a string or a list of strings")
    return (normalized or None, None)


def _build_repo_search_extra_args(
    repo_type: str, **candidate_args: Any
) -> tuple[dict[str, Any], list[str], str | None]:
    normalized: dict[str, Any] = {}
    for key, value in candidate_args.items():
        if value is None:
            continue
        if key in {"card_data", "cardData"}:
            if value:
                normalized["cardData"] = True
            continue
        if key in {"fetch_config", "linked"}:
            if value:
                normalized[key] = True
            continue
        normalized[key] = value

    allowed_extra = REPO_SEARCH_EXTRA_ARGS.get(repo_type, set())
    unsupported = sorted(str(key) for key in normalized if str(key) not in allowed_extra)
    if unsupported:
        return (
            {},
            [],
            f"Unsupported search args for repo_type='{repo_type}': {unsupported}. Allowed args: {sorted(allowed_extra)}",
        )

    dropped_expand: list[str] = []
    if "expand" in normalized:
        kept_expand, dropped_expand, expand_error = _sanitize_repo_expand_values(
            repo_type, normalized.get("expand")
        )
        if expand_error:
            return ({}, [], expand_error)
        if kept_expand is None:
            normalized.pop("expand", None)
        else:
            normalized["expand"] = kept_expand

    if not any(
        key in normalized for key in ("expand", "full", "cardData", "fetch_config")
    ):
        normalized["expand"] = list(REPO_SEARCH_DEFAULT_EXPAND[repo_type])

    return (normalized, dropped_expand, None)


def _normalize_user_likes_sort(sort: str | None) -> tuple[str | None, str | None]:
    normalized = str(sort or "liked_at").strip() or "liked_at"
    if normalized not in {"liked_at", "repo_likes", "repo_downloads"}:
        return (None, "sort must be one of liked_at, repo_likes, repo_downloads")
    return (normalized, None)


async def _run_repo_search(
    ctx: HelperRuntimeContext,
    *,
    helper_name: str,
    requested_repo_types: list[str],
    search: str | None,
    filter: str | list[str] | None,
    author: str | None,
    sort: str | None,
    limit: int,
    fields: list[str] | None,
    post_filter: dict[str, Any] | None,
    extra_args_by_type: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    start_calls = ctx.call_count["n"]
    default_limit = ctx._policy_int(helper_name, "default_limit", 20)
    max_limit = ctx._policy_int(
        helper_name, "max_limit", SELECTIVE_ENDPOINT_RETURN_HARD_CAP
    )
    filter_list, filter_error = _normalize_repo_search_filter(ctx, filter)
    if filter_error:
        return ctx._helper_error(
            start_calls=start_calls,
            source="/api/repos",
            error=filter_error,
        )

    term = _clean_repo_search_text(search)
    author_clean = _clean_repo_search_text(author)
    requested_limit = limit
    applied_limit = ctx._clamp_int(
        limit,
        default=default_limit,
        minimum=1,
        maximum=max_limit,
    )
    limit_meta = ctx._derive_limit_metadata(
        requested_limit=requested_limit,
        applied_limit=applied_limit,
        default_limit_used=limit == default_limit,
    )
    hard_cap_applied = bool(limit_meta.get("hard_cap_applied"))

    sort_keys: dict[str, str | None] = {}
    for repo_type in requested_repo_types:
        sort_key, sort_error = ctx._normalize_repo_sort_key(repo_type, sort)
        if sort_error:
            return ctx._helper_error(
                start_calls=start_calls,
                source=f"/api/{repo_type}s",
                error=sort_error,
            )
        sort_keys[repo_type] = sort_key

    all_items: list[dict[str, Any]] = []
    scanned = 0
    source_endpoints: list[str] = []
    limit_boundary_hit = False
    ignored_expand: dict[str, list[str]] = {}
    api = ctx._get_hf_api_client()

    for repo_type in requested_repo_types:
        endpoint = f"/api/{repo_type}s"
        source_endpoints.append(endpoint)
        raw_extra_args = dict((extra_args_by_type or {}).get(repo_type, {}))
        extra_args, dropped_expand, extra_error = _build_repo_search_extra_args(
            repo_type,
            **raw_extra_args,
        )
        if extra_error:
            return ctx._helper_error(
                start_calls=start_calls,
                source=endpoint,
                error=extra_error,
            )
        if dropped_expand:
            ignored_expand[repo_type] = dropped_expand
        try:
            payload = ctx._host_hf_call(
                endpoint,
                lambda repo_type=repo_type, extra_args=extra_args: ctx._repo_list_call(
                    api,
                    repo_type,
                    search=term,
                    author=author_clean,
                    filter=filter_list,
                    sort=sort_keys[repo_type],
                    limit=applied_limit,
                    **extra_args,
                ),
            )
        except Exception as e:
            return ctx._helper_error(start_calls=start_calls, source=endpoint, error=e)
        scanned += len(payload)
        if len(payload) >= applied_limit:
            limit_boundary_hit = True
        all_items.extend(
            ctx._normalize_repo_search_row(row, repo_type)
            for row in payload[:applied_limit]
        )

    try:
        all_items = ctx._apply_where(
            all_items, post_filter, allowed_fields=REPO_CANONICAL_FIELDS
        )
    except ValueError as exc:
        return ctx._helper_error(
            start_calls=start_calls,
            source="/api/repos",
            error=exc,
        )
    combined_sort_key = next(iter(sort_keys.values()), None)
    all_items = ctx._sort_repo_rows(all_items, combined_sort_key)
    matched = len(all_items)
    try:
        all_items = ctx._project_repo_items(all_items[:applied_limit], fields)
    except ValueError as exc:
        return ctx._helper_error(
            start_calls=start_calls,
            source="/api/repos",
            error=exc,
        )

    more_available: bool | str = False
    truncated = False
    truncated_by = "none"
    next_request_hint: str | None = None
    if hard_cap_applied and scanned >= applied_limit:
        truncated = True
        truncated_by = "hard_cap"
        more_available = "unknown"
        next_request_hint = f"Increase limit above {applied_limit} to improve coverage"
    elif limit_boundary_hit:
        more_available = "unknown"
        next_request_hint = (
            f"Increase limit above {applied_limit} to check whether more rows exist"
        )

    return ctx._helper_success(
        start_calls=start_calls,
        source=",".join(source_endpoints),
        items=all_items,
        helper=helper_name,
        search=term,
        repo_types=requested_repo_types,
        filter=filter_list,
        sort=combined_sort_key,
        author=author_clean,
        limit=applied_limit,
        post_filter=post_filter if isinstance(post_filter, dict) and post_filter else None,
        scanned=scanned,
        matched=matched,
        returned=len(all_items),
        truncated=truncated,
        truncated_by=truncated_by,
        more_available=more_available,
        limit_boundary_hit=limit_boundary_hit,
        next_request_hint=next_request_hint,
        ignored_expand=ignored_expand or None,
        **limit_meta,
    )


async def hf_models_search(
    ctx: HelperRuntimeContext,
    search: str | None = None,
    filter: str | list[str] | None = None,
    author: str | None = None,
    apps: str | list[str] | None = None,
    gated: bool | None = None,
    inference: str | None = None,
    inference_provider: str | list[str] | None = None,
    model_name: str | None = None,
    trained_dataset: str | list[str] | None = None,
    pipeline_tag: str | None = None,
    emissions_thresholds: tuple[float, float] | None = None,
    sort: str | None = None,
    limit: int = 20,
    expand: list[str] | None = None,
    full: bool | None = None,
    card_data: bool = False,
    fetch_config: bool = False,
    fields: list[str] | None = None,
    post_filter: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return await _run_repo_search(
        ctx,
        helper_name="hf_models_search",
        requested_repo_types=["model"],
        search=search,
        filter=filter,
        author=author,
        sort=sort,
        limit=limit,
        fields=fields,
        post_filter=post_filter,
        extra_args_by_type={
            "model": {
                "apps": apps,
                "gated": gated,
                "inference": inference,
                "inference_provider": inference_provider,
                "model_name": model_name,
                "trained_dataset": trained_dataset,
                "pipeline_tag": pipeline_tag,
                "emissions_thresholds": emissions_thresholds,
                "expand": expand,
                "full": full,
                "card_data": card_data,
                "fetch_config": fetch_config,
            }
        },
    )


async def hf_datasets_search(
    ctx: HelperRuntimeContext,
    search: str | None = None,
    filter: str | list[str] | None = None,
    author: str | None = None,
    benchmark: str | bool | None = None,
    dataset_name: str | None = None,
    gated: bool | None = None,
    language_creators: str | list[str] | None = None,
    language: str | list[str] | None = None,
    multilinguality: str | list[str] | None = None,
    size_categories: str | list[str] | None = None,
    task_categories: str | list[str] | None = None,
    task_ids: str | list[str] | None = None,
    sort: str | None = None,
    limit: int = 20,
    expand: list[str] | None = None,
    full: bool | None = None,
    fields: list[str] | None = None,
    post_filter: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return await _run_repo_search(
        ctx,
        helper_name="hf_datasets_search",
        requested_repo_types=["dataset"],
        search=search,
        filter=filter,
        author=author,
        sort=sort,
        limit=limit,
        fields=fields,
        post_filter=post_filter,
        extra_args_by_type={
            "dataset": {
                "benchmark": benchmark,
                "dataset_name": dataset_name,
                "gated": gated,
                "language_creators": language_creators,
                "language": language,
                "multilinguality": multilinguality,
                "size_categories": size_categories,
                "task_categories": task_categories,
                "task_ids": task_ids,
                "expand": expand,
                "full": full,
            }
        },
    )


async def hf_spaces_search(
    ctx: HelperRuntimeContext,
    search: str | None = None,
    filter: str | list[str] | None = None,
    author: str | None = None,
    datasets: str | list[str] | None = None,
    models: str | list[str] | None = None,
    linked: bool = False,
    sort: str | None = None,
    limit: int = 20,
    expand: list[str] | None = None,
    full: bool | None = None,
    fields: list[str] | None = None,
    post_filter: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return await _run_repo_search(
        ctx,
        helper_name="hf_spaces_search",
        requested_repo_types=["space"],
        search=search,
        filter=filter,
        author=author,
        sort=sort,
        limit=limit,
        fields=fields,
        post_filter=post_filter,
        extra_args_by_type={
            "space": {
                "datasets": datasets,
                "models": models,
                "linked": linked,
                "expand": expand,
                "full": full,
            }
        },
    )


async def hf_repo_search(
    ctx: HelperRuntimeContext,
    search: str | None = None,
    repo_type: str | None = None,
    repo_types: list[str] | None = None,
    filter: str | list[str] | None = None,
    author: str | None = None,
    sort: str | None = None,
    limit: int = 20,
    fields: list[str] | None = None,
    post_filter: dict[str, Any] | None = None,
) -> dict[str, Any]:
    start_calls = ctx.call_count["n"]
    requested_repo_types, type_error = _resolve_repo_search_types(
        ctx,
        repo_type=repo_type,
        repo_types=repo_types,
    )
    if type_error:
        return ctx._helper_error(
            start_calls=start_calls,
            source="/api/repos",
            error=type_error,
        )
    if not requested_repo_types:
        return ctx._helper_error(
            start_calls=start_calls,
            source="/api/repos",
            error="repo_type or repo_types is required",
        )
    return await _run_repo_search(
        ctx,
        helper_name="hf_repo_search",
        requested_repo_types=requested_repo_types,
        search=search,
        filter=filter,
        author=author,
        sort=sort,
        limit=limit,
        fields=fields,
        post_filter=post_filter,
    )


async def hf_user_likes(
    ctx: HelperRuntimeContext,
    username: str | None = None,
    repo_types: list[str] | None = None,
    limit: int | None = None,
    scan_limit: int | None = None,
    count_only: bool = False,
    where: dict[str, Any] | None = None,
    fields: list[str] | None = None,
    sort: str | None = None,
    ranking_window: int | None = None,
) -> dict[str, Any]:
    start_calls = ctx.call_count["n"]
    default_limit = ctx._policy_int("hf_user_likes", "default_limit", 100)
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
        limit=limit,
        count_only=count_only,
        default_limit=default_limit,
        max_limit=EXHAUSTIVE_HELPER_RETURN_HARD_CAP,
        scan_limit=scan_limit,
        scan_cap=scan_cap,
    )
    applied_limit = int(limit_plan["applied_limit"])
    scan_lim = int(limit_plan["applied_scan_limit"])
    try:
        normalized_where = ctx._normalize_where(
            where, allowed_fields=USER_LIKES_CANONICAL_FIELDS
        )
    except ValueError as exc:
        return ctx._helper_error(
            start_calls=start_calls,
            source=f"/api/users/{resolved_username}/likes",
            error=exc,
        )
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
        selected_pairs = matched_rows[:applied_limit]
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
        selected_pairs = ranked_shortlist[:applied_limit]
        ranking_complete = (
            exact_count
            and shortlist_size >= matched
            and (len(candidates) <= enrich_budget)
        )
    try:
        items = ctx._project_user_like_items([row for _, row in selected_pairs], fields)
    except ValueError as exc:
        return ctx._helper_error(
            start_calls=start_calls,
            source=endpoint,
            error=exc,
        )
    popularity_present = sum(
        (1 for _, row in selected_pairs if row.get("repo_likes") is not None)
    )
    sample_complete = (
        exact_count
        and applied_limit >= matched
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
    limit: int | None = None,
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
    default_limit = ctx._policy_int("hf_repo_likers", "default_limit", 1000)
    requested_limit = limit
    default_limit_used = requested_limit is None and (not count_only)
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
    try:
        normalized_where = ctx._normalize_where(
            where, allowed_fields=ACTOR_CANONICAL_FIELDS
        )
    except ValueError as exc:
        return ctx._helper_error(
            start_calls=start_calls,
            source=endpoint,
            error=exc,
            repo_id=rid,
            repo_type=rt,
        )
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
            "is_pro": row.get("isPro"),
        }
        if pro_only is True and item.get("is_pro") is not True:
            continue
        if pro_only is False and item.get("is_pro") is True:
            continue
        if not ctx._item_matches_where(item, normalized_where):
            continue
        normalized.append(item)
    if count_only:
        applied_limit = 0
    elif requested_limit is None:
        applied_limit = default_limit
    else:
        try:
            applied_limit = max(0, int(requested_limit))
        except Exception:
            applied_limit = default_limit
    limit_plan = {
        "requested_limit": requested_limit,
        "applied_limit": applied_limit,
        "default_limit_used": default_limit_used,
        "hard_cap_applied": False,
    }
    matched = len(normalized)
    items = [] if count_only else normalized[:applied_limit]
    limit_hit = applied_limit > 0 and matched > applied_limit
    truncated_by = ctx._derive_truncated_by(
        hard_cap=False, limit_hit=limit_hit
    )
    sample_complete = matched <= applied_limit and (not count_only or matched == 0)
    truncated = truncated_by != "none"
    more_available = ctx._derive_more_available(
        sample_complete=sample_complete,
        exact_count=True,
        returned=len(items),
        total=matched,
    )
    try:
        items = ctx._project_actor_items(items, fields)
    except ValueError as exc:
        return ctx._helper_error(
            start_calls=start_calls,
            source=endpoint,
            error=exc,
            repo_id=rid,
            repo_type=rt,
        )
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
    ctx: HelperRuntimeContext,
    repo_type: str,
    repo_id: str,
    limit: int = 20,
    fields: list[str] | None = None,
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
                "repo_id": rid,
                "repo_type": rt,
                "title": getattr(d, "title", None),
                "author": getattr(d, "author", None),
                "created_at": str(getattr(d, "created_at", None))
                if getattr(d, "created_at", None) is not None
                else None,
                "status": getattr(d, "status", None),
                "url": getattr(d, "url", None),
            }
        )
    try:
        items = ctx._project_discussion_items(items, fields)
    except ValueError as exc:
        return ctx._helper_error(start_calls=start_calls, source=endpoint, error=exc)
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
    ctx: HelperRuntimeContext,
    repo_type: str,
    repo_id: str,
    discussion_num: int,
    fields: list[str] | None = None,
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
                    "created_at": ctx._dt_to_str(getattr(event, "created_at", None)),
                    "text": getattr(event, "content", None),
                    "rendered": getattr(event, "rendered", None),
                }
            )
    latest_comment: dict[str, Any] | None = None
    if comment_events:
        latest_comment = max(
            comment_events, key=lambda row: str(row.get("created_at") or "")
        )
    item: dict[str, Any] = {
        "num": num,
        "repo_id": rid,
        "repo_type": rt,
        "title": getattr(detail, "title", None),
        "author": getattr(detail, "author", None),
        "created_at": ctx._dt_to_str(getattr(detail, "created_at", None)),
        "status": getattr(detail, "status", None),
        "url": getattr(detail, "url", None),
        "comment_count": len(comment_events),
        "latest_comment_author": latest_comment.get("author")
        if latest_comment
        else None,
        "latest_comment_created_at": latest_comment.get("created_at")
        if latest_comment
        else None,
        "latest_comment_text": latest_comment.get("text") if latest_comment else None,
        "latest_comment_html": latest_comment.get("rendered")
        if latest_comment
        else None,
    }
    try:
        items = ctx._project_discussion_detail_items([item], fields)
    except ValueError as exc:
        return ctx._helper_error(start_calls=start_calls, source=endpoint, error=exc)
    return ctx._helper_success(
        start_calls=start_calls,
        source=endpoint,
        items=items,
        scanned=len(comment_events),
        matched=1,
        returned=len(items),
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
    try:
        items = ctx._project_repo_items(items, fields)
    except ValueError as exc:
        return ctx._helper_error(start_calls=start_calls, source="/api/repos", error=exc)
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
    default_limit = ctx._policy_int("hf_trending", "default_limit", 20)
    max_limit = ctx._policy_int(
        "hf_trending", "max_limit", TRENDING_ENDPOINT_MAX_LIMIT
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
    lim = ctx._clamp_int(limit, default=default_limit, minimum=1, maximum=max_limit)
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
    try:
        items = ctx._apply_where(items, where, allowed_fields=REPO_CANONICAL_FIELDS)
    except ValueError as exc:
        return ctx._helper_error(
            start_calls=start_calls,
            source="/api/trending",
            error=exc,
        )
    matched = len(items)
    try:
        items = ctx._project_repo_items(items[:lim], fields)
    except ValueError as exc:
        return ctx._helper_error(
            start_calls=start_calls,
            source="/api/trending",
            error=exc,
        )
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
    default_limit = ctx._policy_int("hf_daily_papers", "default_limit", 20)
    max_limit = ctx._policy_int(
        "hf_daily_papers", "max_limit", OUTPUT_ITEMS_TRUNCATION_LIMIT
    )
    lim = ctx._clamp_int(limit, default=default_limit, minimum=1, maximum=max_limit)
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
    try:
        items = ctx._apply_where(
            items, where, allowed_fields=DAILY_PAPER_CANONICAL_FIELDS
        )
    except ValueError as exc:
        return ctx._helper_error(
            start_calls=start_calls,
            source="/api/daily_papers",
            error=exc,
        )
    matched = len(items)
    try:
        items = ctx._project_daily_paper_items(items[:lim], fields)
    except ValueError as exc:
        return ctx._helper_error(
            start_calls=start_calls,
            source="/api/daily_papers",
            error=exc,
        )
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
        "hf_models_search": partial(hf_models_search, ctx),
        "hf_datasets_search": partial(hf_datasets_search, ctx),
        "hf_spaces_search": partial(hf_spaces_search, ctx),
        "hf_repo_search": partial(hf_repo_search, ctx),
        "hf_user_likes": partial(hf_user_likes, ctx),
        "hf_repo_likers": partial(hf_repo_likers, ctx),
        "hf_repo_discussions": partial(hf_repo_discussions, ctx),
        "hf_repo_discussion_details": partial(hf_repo_discussion_details, ctx),
        "hf_repo_details": partial(hf_repo_details, ctx),
        "hf_trending": partial(hf_trending, ctx),
        "hf_daily_papers": partial(hf_daily_papers, ctx),
    }
