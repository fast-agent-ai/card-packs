from __future__ import annotations

# ruff: noqa: C901, PLR0912, PLR0913, PLR0915, PLR0917
from itertools import islice
import re
from typing import Any, Callable
from ..context_types import HelperRuntimeContext
from ..aliases import (
    ACTOR_FIELD_ALIASES,
    USER_FIELD_ALIASES,
)
from ..constants import (
    EXHAUSTIVE_HELPER_RETURN_HARD_CAP,
    GRAPH_SCAN_LIMIT_CAP,
    OUTPUT_ITEMS_TRUNCATION_LIMIT,
    USER_SUMMARY_ACTIVITY_MAX_PAGES,
    USER_SUMMARY_LIKES_SCAN_LIMIT,
)


from .common import resolve_username_or_current

from functools import partial


def _clean_social_handle(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if re.match("^https?://", cleaned, flags=re.IGNORECASE):
        return cleaned
    return cleaned.lstrip("@")


def _social_url(kind: str, value: Any) -> str | None:
    cleaned = _clean_social_handle(value)
    if cleaned is None:
        return None
    if re.match("^https?://", cleaned, flags=re.IGNORECASE):
        return cleaned
    if kind == "twitter":
        return f"https://twitter.com/{cleaned}"
    if kind == "github":
        return f"https://github.com/{cleaned}"
    if kind == "linkedin":
        if cleaned.startswith(("in/", "company/")):
            return f"https://www.linkedin.com/{cleaned}"
        return f"https://www.linkedin.com/in/{cleaned}"
    if kind == "bluesky":
        return f"https://bsky.app/profile/{cleaned}"
    return cleaned


async def hf_whoami(ctx: HelperRuntimeContext) -> dict[str, Any]:
    start_calls = ctx.call_count["n"]
    endpoint = "/api/whoami-v2"
    token = ctx._load_token()
    if token is None:
        return ctx._helper_error(
            start_calls=start_calls,
            source=endpoint,
            error="Current authenticated user is unavailable for this request. No request-scoped or fallback HF token was found.",
        )
    try:
        payload = ctx._host_hf_call(
            endpoint,
            lambda: ctx._get_hf_api_client().whoami(token=token, cache=True),
        )
    except Exception as e:
        return ctx._helper_error(start_calls=start_calls, source=endpoint, error=e)
    username = payload.get("name") or payload.get("user") or payload.get("username")
    item = {
        "username": username,
        "fullname": payload.get("fullname"),
        "isPro": payload.get("isPro"),
    }
    items = [item] if isinstance(username, str) and username else []
    return ctx._helper_success(
        start_calls=start_calls,
        source=endpoint,
        items=items,
        scanned=1,
        matched=len(items),
        returned=len(items),
        truncated=False,
    )


async def _hf_user_overview(ctx: HelperRuntimeContext, username: str) -> dict[str, Any]:
    start_calls = ctx.call_count["n"]
    u = str(username or "").strip()
    if not u:
        return ctx._helper_error(
            start_calls=start_calls,
            source="/api/users/<u>/overview",
            error="username is required",
        )
    endpoint = f"/api/users/{u}/overview"
    try:
        obj = ctx._host_hf_call(
            endpoint, lambda: ctx._get_hf_api_client().get_user_overview(u)
        )
    except Exception as e:
        return ctx._helper_error(start_calls=start_calls, source=endpoint, error=e)
    twitter = getattr(obj, "twitter", None) or getattr(obj, "twitterUsername", None)
    github = getattr(obj, "github", None) or getattr(obj, "githubUsername", None)
    linkedin = getattr(obj, "linkedin", None) or getattr(obj, "linkedinUsername", None)
    bluesky = getattr(obj, "bluesky", None) or getattr(obj, "blueskyUsername", None)
    if ctx._budget_remaining() > 0 and any(
        (v in {None, ""} for v in [twitter, github, linkedin, bluesky])
    ):
        socials_ep = f"/api/users/{u}/socials"
        socials_resp = ctx._host_raw_call(socials_ep)
        if socials_resp.get("ok"):
            socials_payload = (
                socials_resp.get("data")
                if isinstance(socials_resp.get("data"), dict)
                else {}
            )
            handles = (
                socials_payload.get("socialHandles")
                if isinstance(socials_payload.get("socialHandles"), dict)
                else {}
            )
            twitter = twitter or handles.get("twitter")
            github = github or handles.get("github")
            linkedin = linkedin or handles.get("linkedin")
            bluesky = bluesky or handles.get("bluesky")
    orgs_raw = getattr(obj, "orgs", None)
    org_names: list[str] | None = None
    if isinstance(orgs_raw, (list, tuple, set)):
        names = []
        for org in orgs_raw:
            if isinstance(org, str) and org.strip():
                names.append(org.strip())
                continue
            name = getattr(org, "name", None)
            if isinstance(name, str) and name.strip():
                names.append(name.strip())
        org_names = names or None
    twitter_handle = _clean_social_handle(twitter)
    github_handle = _clean_social_handle(github)
    linkedin_handle = _clean_social_handle(linkedin)
    bluesky_handle = _clean_social_handle(bluesky)
    item = {
        "username": obj.username or u,
        "fullname": obj.fullname,
        "bio": getattr(obj, "details", None),
        "avatarUrl": obj.avatar_url,
        "websiteUrl": getattr(obj, "websiteUrl", None),
        "twitter": _social_url("twitter", twitter_handle),
        "github": _social_url("github", github_handle),
        "linkedin": _social_url("linkedin", linkedin_handle),
        "bluesky": _social_url("bluesky", bluesky_handle),
        "twitterHandle": twitter_handle,
        "githubHandle": github_handle,
        "linkedinHandle": linkedin_handle,
        "blueskyHandle": bluesky_handle,
        "followers": ctx._as_int(obj.num_followers),
        "following": ctx._as_int(obj.num_following),
        "likes": ctx._as_int(obj.num_likes),
        "models": ctx._as_int(getattr(obj, "num_models", None)),
        "datasets": ctx._as_int(getattr(obj, "num_datasets", None)),
        "spaces": ctx._as_int(getattr(obj, "num_spaces", None)),
        "discussions": ctx._as_int(getattr(obj, "num_discussions", None)),
        "papers": ctx._as_int(getattr(obj, "num_papers", None)),
        "upvotes": ctx._as_int(getattr(obj, "num_upvotes", None)),
        "orgs": org_names,
        "isPro": obj.is_pro,
    }
    return ctx._helper_success(
        start_calls=start_calls,
        source=endpoint,
        items=[item],
        scanned=1,
        matched=1,
        returned=1,
        truncated=False,
    )


async def _hf_org_overview(
    ctx: HelperRuntimeContext, organization: str
) -> dict[str, Any]:
    start_calls = ctx.call_count["n"]
    org = str(organization or "").strip()
    if not org:
        return ctx._helper_error(
            start_calls=start_calls,
            source="/api/organizations/<o>/overview",
            error="organization is required",
        )
    endpoint = f"/api/organizations/{org}/overview"
    try:
        obj = ctx._host_hf_call(
            endpoint,
            lambda: ctx._get_hf_api_client().get_organization_overview(org),
        )
    except Exception as e:
        return ctx._helper_error(start_calls=start_calls, source=endpoint, error=e)
    item = {
        "organization": obj.name or org,
        "displayName": obj.fullname,
        "avatarUrl": obj.avatar_url,
        "description": obj.details,
        "websiteUrl": getattr(obj, "websiteUrl", None),
        "followers": ctx._as_int(obj.num_followers),
        "members": ctx._as_int(obj.num_users),
        "models": ctx._as_int(getattr(obj, "num_models", None)),
        "datasets": ctx._as_int(getattr(obj, "num_datasets", None)),
        "spaces": ctx._as_int(getattr(obj, "num_spaces", None)),
    }
    return ctx._helper_success(
        start_calls=start_calls,
        source=endpoint,
        items=[item],
        scanned=1,
        matched=1,
        returned=1,
        truncated=False,
    )


async def hf_org_members(
    ctx: HelperRuntimeContext,
    organization: str,
    return_limit: int | None = None,
    scan_limit: int | None = None,
    count_only: bool = False,
    where: dict[str, Any] | None = None,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    start_calls = ctx.call_count["n"]
    org = str(organization or "").strip()
    if not org:
        return ctx._helper_error(
            start_calls=start_calls,
            source="/api/organizations/<o>/members",
            error="organization is required",
        )
    default_return = ctx._policy_int("hf_org_members", "default_return", 100)
    scan_cap = ctx._policy_int("hf_org_members", "scan_max", GRAPH_SCAN_LIMIT_CAP)
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
    has_where = isinstance(where, dict) and bool(where)
    overview_total: int | None = None
    overview_source = f"/api/organizations/{org}/overview"
    if ctx._budget_remaining() > 0:
        try:
            org_obj = ctx._host_hf_call(
                overview_source,
                lambda: ctx._get_hf_api_client().get_organization_overview(org),
            )
            overview_total = ctx._as_int(getattr(org_obj, "num_users", None))
        except Exception:
            overview_total = None
    if count_only and (not has_where) and (overview_total is not None):
        return ctx._overview_count_only_success(
            start_calls=start_calls,
            source=overview_source,
            total=overview_total,
            limit_plan=limit_plan,
            base_meta={
                "scanned": 1,
                "count_source": "overview",
                "organization": org,
            },
        )
    endpoint = f"/api/organizations/{org}/members"
    try:
        rows = ctx._host_hf_call(
            endpoint,
            lambda: list(
                islice(
                    ctx._get_hf_api_client().list_organization_members(org),
                    scan_lim,
                )
            ),
        )
    except Exception as e:
        return ctx._helper_error(
            start_calls=start_calls, source=endpoint, error=e, organization=org
        )
    normalized: list[dict[str, Any]] = []
    for row in rows:
        handle = getattr(row, "username", None)
        if not isinstance(handle, str) or not handle:
            continue
        item = {
            "username": handle,
            "fullname": getattr(row, "fullname", None),
            "isPro": getattr(row, "is_pro", None),
            "role": getattr(row, "role", None),
        }
        normalized.append(item)
    normalized = ctx._apply_where(normalized, where, aliases=ACTOR_FIELD_ALIASES)
    observed_total = len(rows)
    scan_exhaustive = observed_total < scan_lim
    overview_list_mismatch = (
        overview_total is not None
        and scan_exhaustive
        and (observed_total != overview_total)
    )
    if has_where:
        exact_count = scan_exhaustive
        total = len(normalized)
        total_matched = len(normalized)
    elif overview_total is not None:
        exact_count = True
        total = overview_total
        total_matched = overview_total
    else:
        exact_count = scan_exhaustive
        total = observed_total
        total_matched = observed_total
    total_available = overview_total if overview_total is not None else observed_total
    items = normalized[:ret_lim]
    scan_limit_hit = not exact_count and observed_total >= scan_lim
    count_source = (
        "overview" if overview_total is not None and (not has_where) else "scan"
    )
    sample_complete = (
        exact_count
        and len(normalized) <= ret_lim
        and (not count_only or len(normalized) == 0)
    )
    more_available = ctx._derive_more_available(
        sample_complete=sample_complete,
        exact_count=exact_count,
        returned=len(items),
        total=total,
    )
    if not exact_count and scan_limit_hit:
        more_available = "unknown" if has_where else True
    items = ctx._project_user_items(items, fields)
    meta = ctx._build_exhaustive_result_meta(
        base_meta={
            "scanned": observed_total,
            "total": total,
            "total_available": total_available,
            "total_matched": total_matched,
            "count_source": count_source,
            "lower_bound": bool(has_where and (not exact_count)),
            "overview_total": overview_total,
            "listed_total": observed_total,
            "overview_list_mismatch": overview_list_mismatch,
            "organization": org,
        },
        limit_plan=limit_plan,
        matched_count=len(normalized),
        returned_count=len(items),
        exact_count=exact_count,
        count_only=count_only,
        sample_complete=sample_complete,
        more_available=more_available,
        scan_limit_hit=scan_limit_hit,
    )
    return ctx._helper_success(
        start_calls=start_calls, source=endpoint, items=items, meta=meta
    )


async def _user_graph_helper(
    ctx: HelperRuntimeContext,
    kind: str,
    username: str,
    pro_only: bool | None,
    return_limit: int | None,
    scan_limit: int | None,
    count_only: bool,
    where: dict[str, Any] | None,
    fields: list[str] | None,
    *,
    helper_name: str,
) -> dict[str, Any]:
    start_calls = ctx.call_count["n"]
    default_return = ctx._policy_int(helper_name, "default_return", 100)
    scan_cap = ctx._policy_int(helper_name, "scan_max", GRAPH_SCAN_LIMIT_CAP)
    max_return = ctx._policy_int(
        helper_name, "max_return", EXHAUSTIVE_HELPER_RETURN_HARD_CAP
    )
    u = str(username or "").strip()
    if not u:
        return ctx._helper_error(
            start_calls=start_calls,
            source=f"/api/users/<u>/{kind}",
            error="username is required",
        )
    limit_plan = ctx._resolve_exhaustive_limits(
        return_limit=return_limit,
        count_only=count_only,
        default_return=default_return,
        max_return=max_return,
        scan_limit=scan_limit,
        scan_cap=scan_cap,
    )
    ret_lim = int(limit_plan["applied_return_limit"])
    scan_lim = int(limit_plan["applied_scan_limit"])
    has_where = isinstance(where, dict) and bool(where)
    filtered = pro_only is not None or has_where
    entity_type = "user"
    overview_total: int | None = None
    overview_source = f"/api/users/{u}/overview"
    if ctx._budget_remaining() > 0:
        try:
            user_obj = ctx._host_hf_call(
                overview_source,
                lambda: ctx._get_hf_api_client().get_user_overview(u),
            )
            overview_total = ctx._as_int(
                user_obj.num_followers
                if kind == "followers"
                else user_obj.num_following
            )
        except Exception:
            org_overview_source = f"/api/organizations/{u}/overview"
            try:
                org_obj = ctx._host_hf_call(
                    org_overview_source,
                    lambda: ctx._get_hf_api_client().get_organization_overview(u),
                )
            except Exception:
                overview_total = None
            else:
                entity_type = "organization"
                overview_source = org_overview_source
                if kind != "followers":
                    return ctx._helper_error(
                        start_calls=start_calls,
                        source=f"/api/organizations/{u}/{kind}",
                        error="organization graph only supports relation='followers'; organizations do not expose a following list",
                        relation=kind,
                        organization=u,
                        entity=u,
                        entity_type=entity_type,
                    )
                overview_total = ctx._as_int(getattr(org_obj, "num_followers", None))
    if count_only and (not filtered) and (overview_total is not None):
        return ctx._overview_count_only_success(
            start_calls=start_calls,
            source=overview_source,
            total=overview_total,
            limit_plan=limit_plan,
            base_meta={
                "scanned": 1,
                "count_source": "overview",
                "relation": kind,
                "pro_only": pro_only,
                "where_applied": has_where,
                "entity": u,
                "entity_type": entity_type,
                "username": u,
                "organization": u if entity_type == "organization" else None,
            },
        )
    endpoint = f"/api/users/{u}/{kind}"
    try:
        if entity_type == "organization":
            endpoint = f"/api/organizations/{u}/followers"
            rows = ctx._host_hf_call(
                endpoint,
                lambda: list(
                    islice(
                        ctx._get_hf_api_client().list_organization_followers(u),
                        scan_lim,
                    )
                ),
            )
        elif kind == "followers":
            rows = ctx._host_hf_call(
                endpoint,
                lambda: list(
                    islice(ctx._get_hf_api_client().list_user_followers(u), scan_lim)
                ),
            )
        else:
            rows = ctx._host_hf_call(
                endpoint,
                lambda: list(
                    islice(ctx._get_hf_api_client().list_user_following(u), scan_lim)
                ),
            )
    except Exception as e:
        return ctx._helper_error(
            start_calls=start_calls,
            source=endpoint,
            error=e,
            relation=kind,
            username=u,
            entity=u,
            entity_type=entity_type,
            organization=u if entity_type == "organization" else None,
        )
    normalized: list[dict[str, Any]] = []
    for row in rows:
        handle = getattr(row, "username", None)
        if not isinstance(handle, str) or not handle:
            continue
        item = {
            "username": handle,
            "fullname": getattr(row, "fullname", None),
            "isPro": getattr(row, "is_pro", None),
        }
        if pro_only is True and item.get("isPro") is not True:
            continue
        if pro_only is False and item.get("isPro") is True:
            continue
        normalized.append(item)
    normalized = ctx._apply_where(normalized, where, aliases=USER_FIELD_ALIASES)
    observed_total = len(rows)
    scan_exhaustive = observed_total < scan_lim
    overview_list_mismatch = (
        overview_total is not None
        and scan_exhaustive
        and (observed_total != overview_total)
    )
    if filtered:
        exact_count = scan_exhaustive
        total = len(normalized)
        total_matched = len(normalized)
    elif overview_total is not None:
        exact_count = True
        total = overview_total
        total_matched = overview_total
    else:
        exact_count = scan_exhaustive
        total = observed_total
        total_matched = observed_total
    total_available = overview_total if overview_total is not None else observed_total
    items = normalized[:ret_lim]
    scan_limit_hit = not exact_count and observed_total >= scan_lim
    count_source = (
        "overview" if overview_total is not None and (not filtered) else "scan"
    )
    sample_complete = (
        exact_count
        and len(normalized) <= ret_lim
        and (not count_only or len(normalized) == 0)
    )
    more_available = ctx._derive_more_available(
        sample_complete=sample_complete,
        exact_count=exact_count,
        returned=len(items),
        total=total,
    )
    if not exact_count and scan_limit_hit:
        more_available = "unknown" if filtered else True
    items = ctx._project_user_items(items, fields)
    meta = ctx._build_exhaustive_result_meta(
        base_meta={
            "scanned": observed_total,
            "total": total,
            "total_available": total_available,
            "total_matched": total_matched,
            "count_source": count_source,
            "lower_bound": bool(filtered and (not exact_count)),
            "overview_total": overview_total,
            "listed_total": observed_total,
            "overview_list_mismatch": overview_list_mismatch,
            "relation": kind,
            "pro_only": pro_only,
            "where_applied": has_where,
            "entity": u,
            "entity_type": entity_type,
            "username": u,
            "organization": u if entity_type == "organization" else None,
        },
        limit_plan=limit_plan,
        matched_count=len(normalized),
        returned_count=len(items),
        exact_count=exact_count,
        count_only=count_only,
        sample_complete=sample_complete,
        more_available=more_available,
        scan_limit_hit=scan_limit_hit,
    )
    return ctx._helper_success(
        start_calls=start_calls, source=endpoint, items=items, meta=meta
    )


async def hf_profile_summary(
    ctx: HelperRuntimeContext,
    handle: str | None = None,
    include: list[str] | None = None,
    likes_limit: int = 10,
    activity_limit: int = 10,
) -> dict[str, Any]:
    start_calls = ctx.call_count["n"]
    resolved_handle, resolve_error = await resolve_username_or_current(ctx, handle)
    if resolve_error:
        return ctx._helper_error(
            start_calls=start_calls,
            source="/api/users/<u>/overview",
            error=resolve_error,
        )
    if not isinstance(resolved_handle, str):
        return ctx._helper_error(
            start_calls=start_calls,
            source="/api/users/<u>/overview",
            error="handle was not provided and current authenticated user could not be resolved",
        )
    try:
        requested_sections = (
            {part.lower() for part in ctx._coerce_str_list(include) if part.strip()}
            if include is not None
            else set()
        )
    except ValueError as e:
        return ctx._helper_error(
            start_calls=start_calls,
            source=f"/api/users/{resolved_handle}/overview",
            error=e,
        )
    invalid_sections = sorted(requested_sections - {"likes", "activity"})
    if invalid_sections:
        return ctx._helper_error(
            start_calls=start_calls,
            source=f"/api/users/{resolved_handle}/overview",
            error=f"Unsupported include values: {invalid_sections}",
        )
    likes_lim = ctx._clamp_int(
        likes_limit, default=10, minimum=0, maximum=OUTPUT_ITEMS_TRUNCATION_LIMIT
    )
    activity_lim = ctx._clamp_int(
        activity_limit, default=10, minimum=0, maximum=OUTPUT_ITEMS_TRUNCATION_LIMIT
    )
    section_errors: dict[str, str] = {}
    user_overview = await _hf_user_overview(ctx, resolved_handle)
    if user_overview.get("ok") is True:
        overview_item = ctx._helper_item(user_overview) or {"username": resolved_handle}
        item: dict[str, Any] = {
            "handle": str(overview_item.get("username") or resolved_handle),
            "entity_type": "user",
            "display_name": overview_item.get("fullname")
            or str(overview_item.get("username") or resolved_handle),
            "bio": overview_item.get("bio"),
            "avatar_url": overview_item.get("avatarUrl"),
            "website_url": overview_item.get("websiteUrl"),
            "twitter_url": overview_item.get("twitter"),
            "github_url": overview_item.get("github"),
            "linkedin_url": overview_item.get("linkedin"),
            "bluesky_url": overview_item.get("bluesky"),
            "followers_count": ctx._overview_count(overview_item, "followers"),
            "following_count": ctx._overview_count(overview_item, "following"),
            "likes_count": ctx._overview_count(overview_item, "likes"),
            "models_count": ctx._overview_count(overview_item, "models"),
            "datasets_count": ctx._overview_count(overview_item, "datasets"),
            "spaces_count": ctx._overview_count(overview_item, "spaces"),
            "discussions_count": ctx._overview_count(overview_item, "discussions"),
            "papers_count": ctx._overview_count(overview_item, "papers"),
            "upvotes_count": ctx._overview_count(overview_item, "upvotes"),
            "organizations": overview_item.get("orgs"),
            "is_pro": overview_item.get("isPro"),
        }
        if "likes" in requested_sections:
            likes = await ctx.call_helper(
                "hf_user_likes",
                username=resolved_handle,
                return_limit=likes_lim,
                scan_limit=USER_SUMMARY_LIKES_SCAN_LIMIT,
                count_only=likes_lim == 0,
                sort="liked_at",
                fields=[
                    "liked_at",
                    "repo_id",
                    "repo_type",
                    "repo_author",
                    "repo_url",
                ],
            )
            item["likes_sample"] = likes.get("items") if likes.get("ok") is True else []
            if likes.get("ok") is not True:
                section_errors["likes"] = str(
                    likes.get("error") or "likes fetch failed"
                )
        if "activity" in requested_sections:
            activity = await ctx.call_helper(
                "hf_recent_activity",
                feed_type="user",
                entity=resolved_handle,
                return_limit=activity_lim,
                max_pages=USER_SUMMARY_ACTIVITY_MAX_PAGES,
                count_only=activity_lim == 0,
                fields=["timestamp", "event_type", "repo_type", "repo_id"],
            )
            item["activity_sample"] = (
                activity.get("items") if activity.get("ok") is True else []
            )
            if activity.get("ok") is not True:
                section_errors["activity"] = str(
                    activity.get("error") or "activity fetch failed"
                )
        return ctx._helper_success(
            start_calls=start_calls,
            source=f"/api/users/{resolved_handle}/overview",
            items=[item],
            scanned=1,
            matched=1,
            returned=1,
            truncated=False,
            handle=resolved_handle,
            entity_type="user",
            include=sorted(requested_sections),
            likes_limit=likes_lim,
            activity_limit=activity_lim,
            section_errors=section_errors or None,
        )
    org_overview = await _hf_org_overview(ctx, resolved_handle)
    if org_overview.get("ok") is True:
        overview_item = ctx._helper_item(org_overview) or {
            "organization": resolved_handle
        }
        item = {
            "handle": str(overview_item.get("organization") or resolved_handle),
            "entity_type": "organization",
            "display_name": overview_item.get("displayName")
            or str(overview_item.get("organization") or resolved_handle),
            "description": overview_item.get("description"),
            "avatar_url": overview_item.get("avatarUrl"),
            "website_url": overview_item.get("websiteUrl"),
            "followers_count": ctx._overview_count(overview_item, "followers"),
            "members_count": ctx._overview_count(overview_item, "members"),
            "models_count": ctx._overview_count(overview_item, "models"),
            "datasets_count": ctx._overview_count(overview_item, "datasets"),
            "spaces_count": ctx._overview_count(overview_item, "spaces"),
        }
        return ctx._helper_success(
            start_calls=start_calls,
            source=f"/api/organizations/{resolved_handle}/overview",
            items=[item],
            scanned=1,
            matched=1,
            returned=1,
            truncated=False,
            handle=resolved_handle,
            entity_type="organization",
            include=[],
            ignored_includes=sorted(requested_sections) or None,
        )
    error = (
        user_overview.get("error")
        or org_overview.get("error")
        or "profile fetch failed"
    )
    return ctx._helper_error(
        start_calls=start_calls,
        source=f"/api/profiles/{resolved_handle}",
        error=error,
        handle=resolved_handle,
    )


async def hf_user_graph(
    ctx: HelperRuntimeContext,
    username: str | None = None,
    relation: str = "followers",
    return_limit: int | None = None,
    scan_limit: int | None = None,
    count_only: bool = False,
    pro_only: bool | None = None,
    where: dict[str, Any] | None = None,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    start_calls = ctx.call_count["n"]
    rel = str(relation or "").strip().lower() or "followers"
    if rel not in {"followers", "following"}:
        return ctx._helper_error(
            start_calls=start_calls,
            source="/api/users/<u>/followers",
            error="relation must be 'followers' or 'following'",
        )
    resolved_username, resolve_error = await resolve_username_or_current(ctx, username)
    if resolve_error:
        return ctx._helper_error(
            start_calls=start_calls,
            source=f"/api/users/<u>/{rel}",
            error=resolve_error,
            relation=rel,
        )
    if not isinstance(resolved_username, str):
        return ctx._helper_error(
            start_calls=start_calls,
            source=f"/api/users/<u>/{rel}",
            error="username is required",
            relation=rel,
        )
    return await _user_graph_helper(
        ctx,
        rel,
        resolved_username,
        pro_only,
        return_limit,
        scan_limit,
        count_only,
        where,
        fields,
        helper_name="hf_user_graph",
    )


def register_profile_helpers(
    ctx: HelperRuntimeContext,
) -> dict[str, Callable[..., Any]]:
    return {
        "hf_whoami": partial(hf_whoami, ctx),
        "hf_org_members": partial(hf_org_members, ctx),
        "hf_profile_summary": partial(hf_profile_summary, ctx),
        "hf_user_graph": partial(hf_user_graph, ctx),
    }
