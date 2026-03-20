from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, NamedTuple

from .constants import (
    ACTIVITY_CANONICAL_FIELDS,
    ACTOR_CANONICAL_FIELDS,
    COLLECTION_CANONICAL_FIELDS,
    DAILY_PAPER_CANONICAL_FIELDS,
    DISCUSSION_CANONICAL_FIELDS,
    DISCUSSION_DETAIL_CANONICAL_FIELDS,
    GRAPH_SCAN_LIMIT_CAP,
    LIKES_ENRICHMENT_MAX_REPOS,
    LIKES_RANKING_WINDOW_DEFAULT,
    LIKES_SCAN_LIMIT_CAP,
    OUTPUT_ITEMS_TRUNCATION_LIMIT,
    PROFILE_CANONICAL_FIELDS,
    RECENT_ACTIVITY_PAGE_SIZE,
    RECENT_ACTIVITY_SCAN_MAX_PAGES,
    REPO_CANONICAL_FIELDS,
    TRENDING_ENDPOINT_MAX_LIMIT,
    USER_LIKES_CANONICAL_FIELDS,
)


class RepoApiAdapter(NamedTuple):
    list_method_name: str
    detail_method_name: str


@dataclass(frozen=True)
class HelperConfig:
    name: str
    endpoint_patterns: tuple[str, ...] = ()
    default_metadata: Mapping[str, Any] = field(default_factory=dict)
    pagination: Mapping[str, Any] = field(default_factory=dict)


REPO_SEARCH_EXTRA_ARGS: dict[str, set[str]] = {
    "dataset": {
        "benchmark",
        "dataset_name",
        "expand",
        "full",
        "gated",
        "language",
        "language_creators",
        "multilinguality",
        "size_categories",
        "task_categories",
        "task_ids",
    },
    "model": {
        "apps",
        "cardData",
        "emissions_thresholds",
        "expand",
        "fetch_config",
        "full",
        "gated",
        "inference",
        "inference_provider",
        "model_name",
        "pipeline_tag",
        "trained_dataset",
    },
    "space": {"datasets", "expand", "full", "linked", "models"},
}

REPO_SEARCH_DEFAULT_EXPAND: dict[str, list[str]] = {
    "dataset": [
        "author",
        "createdAt",
        "description",
        "downloads",
        "gated",
        "lastModified",
        "likes",
        "paperswithcode_id",
        "private",
        "sha",
        "tags",
        "trendingScore",
    ],
    "model": [
        "author",
        "createdAt",
        "downloads",
        "gated",
        "lastModified",
        "library_name",
        "likes",
        "pipeline_tag",
        "private",
        "safetensors",
        "sha",
        "tags",
        "trendingScore",
    ],
    "space": [
        "author",
        "createdAt",
        "datasets",
        "lastModified",
        "likes",
        "models",
        "private",
        "runtime",
        "sdk",
        "sha",
        "subdomain",
        "tags",
        "trendingScore",
    ],
}

# NOTE:
# The huggingface_hub client type literals currently advertise a few expand values
# that the live Hub API rejects (`childrenModelCount`, `usedStorage`) and omits a
# few that the API now accepts (`xetEnabled`, `gitalyUid`). Keep this allowlist in
# sync with the live API error contract rather than the client typing surface so we
# can sanitize generated requests before they hit the network.
REPO_SEARCH_ALLOWED_EXPAND: dict[str, list[str]] = {
    "dataset": [
        "author",
        "cardData",
        "citation",
        "createdAt",
        "description",
        "disabled",
        "downloads",
        "downloadsAllTime",
        "gated",
        "lastModified",
        "likes",
        "paperswithcode_id",
        "private",
        "resourceGroup",
        "sha",
        "siblings",
        "tags",
        "trendingScore",
        "xetEnabled",
        "gitalyUid",
    ],
    "model": [
        "author",
        "baseModels",
        "cardData",
        "config",
        "createdAt",
        "disabled",
        "downloads",
        "downloadsAllTime",
        "evalResults",
        "gated",
        "gguf",
        "inference",
        "inferenceProviderMapping",
        "lastModified",
        "library_name",
        "likes",
        "mask_token",
        "model-index",
        "pipeline_tag",
        "private",
        "resourceGroup",
        "safetensors",
        "sha",
        "siblings",
        "spaces",
        "tags",
        "transformersInfo",
        "trendingScore",
        "widgetData",
        "xetEnabled",
        "gitalyUid",
    ],
    "space": [
        "author",
        "cardData",
        "createdAt",
        "datasets",
        "disabled",
        "lastModified",
        "likes",
        "models",
        "private",
        "resourceGroup",
        "runtime",
        "sdk",
        "sha",
        "siblings",
        "subdomain",
        "tags",
        "trendingScore",
        "xetEnabled",
        "gitalyUid",
    ],
}

RUNTIME_CAPABILITY_FIELDS = [
    "allowed_sections",
    "overview",
    "helpers",
    "helper_defaults",
    "fields",
    "limits",
    "repo_search",
]
REPO_SUMMARY_FIELDS = list(REPO_CANONICAL_FIELDS)
REPO_SUMMARY_OPTIONAL_FIELDS = [
    field
    for field in REPO_CANONICAL_FIELDS
    if field not in {"repo_id", "repo_type", "author", "repo_url"}
]
ACTOR_OPTIONAL_FIELDS = [
    field for field in ACTOR_CANONICAL_FIELDS if field != "username"
]
PROFILE_OPTIONAL_FIELDS = [
    field
    for field in PROFILE_CANONICAL_FIELDS
    if field not in {"handle", "entity_type"}
]
TRENDING_DEFAULT_FIELDS = [*REPO_SUMMARY_FIELDS, "trending_rank", "trending_score"]
TRENDING_OPTIONAL_FIELDS = [
    field
    for field in TRENDING_DEFAULT_FIELDS
    if field not in {"repo_id", "repo_type", "author", "repo_url", "trending_rank"}
]
DAILY_PAPER_DEFAULT_FIELDS = list(DAILY_PAPER_CANONICAL_FIELDS)
DAILY_PAPER_OPTIONAL_FIELDS = [
    field
    for field in DAILY_PAPER_CANONICAL_FIELDS
    if field not in {"paper_id", "title", "published_at", "rank"}
]
COLLECTION_DEFAULT_FIELDS = list(COLLECTION_CANONICAL_FIELDS)
COLLECTION_OPTIONAL_FIELDS = [
    field
    for field in COLLECTION_CANONICAL_FIELDS
    if field not in {"collection_id", "title", "owner"}
]


def _metadata(
    *,
    default_fields: list[str],
    guaranteed_fields: list[str],
    notes: str,
    optional_fields: list[str] | None = None,
    default_upstream_calls: int = 1,
    may_fan_out: bool = False,
    default_limit: int | None = None,
    max_limit: int | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "default_fields": list(default_fields),
        "guaranteed_fields": list(guaranteed_fields),
        "optional_fields": list(
            optional_fields
            if optional_fields is not None
            else [
                field for field in default_fields if field not in set(guaranteed_fields)
            ]
        ),
        "default_upstream_calls": default_upstream_calls,
        "may_fan_out": may_fan_out,
        "notes": notes,
    }
    if default_limit is not None:
        metadata["default_limit"] = default_limit
    if max_limit is not None:
        metadata["max_limit"] = max_limit
    return metadata


def _config(
    name: str,
    *,
    endpoint_patterns: tuple[str, ...] = (),
    default_metadata: Mapping[str, Any],
    pagination: Mapping[str, Any] | None = None,
) -> HelperConfig:
    return HelperConfig(
        name=name,
        endpoint_patterns=endpoint_patterns,
        default_metadata=dict(default_metadata),
        pagination=dict(pagination or {}),
    )


HELPER_CONFIGS: dict[str, HelperConfig] = {
    "hf_runtime_capabilities": _config(
        "hf_runtime_capabilities",
        default_metadata=_metadata(
            default_fields=RUNTIME_CAPABILITY_FIELDS,
            guaranteed_fields=RUNTIME_CAPABILITY_FIELDS,
            optional_fields=[],
            default_upstream_calls=0,
            notes="Introspection helper. Use section=... to narrow the response.",
        ),
    ),
    "hf_whoami": _config(
        "hf_whoami",
        endpoint_patterns=(r"^/api/whoami-v2$",),
        default_metadata=_metadata(
            default_fields=["username", "fullname", "is_pro"],
            guaranteed_fields=["username"],
            notes="Returns the current authenticated user when a request token is available.",
        ),
    ),
    "hf_profile_summary": _config(
        "hf_profile_summary",
        endpoint_patterns=(
            r"^/api/users/[^/]+/overview$",
            r"^/api/organizations/[^/]+/overview$",
        ),
        default_metadata=_metadata(
            default_fields=list(PROFILE_CANONICAL_FIELDS),
            guaranteed_fields=["handle", "entity_type"],
            optional_fields=PROFILE_OPTIONAL_FIELDS,
            may_fan_out=True,
            notes=(
                "Profile summary helper. Aggregate counts like followers_count/following_count "
                "are in the base item. include=['likes', 'activity'] adds composed samples and "
                "extra upstream work; no other include values are supported."
            ),
        ),
    ),
    "hf_org_members": _config(
        "hf_org_members",
        endpoint_patterns=(r"^/api/organizations/[^/]+/members$",),
        default_metadata=_metadata(
            default_fields=list(ACTOR_CANONICAL_FIELDS),
            guaranteed_fields=["username"],
            optional_fields=ACTOR_OPTIONAL_FIELDS,
            default_limit=1_000,
            max_limit=GRAPH_SCAN_LIMIT_CAP,
            notes="Returns organization member summary rows.",
        ),
        pagination={"default_limit": 1_000, "scan_max": GRAPH_SCAN_LIMIT_CAP},
    ),
    "hf_models_search": _config(
        "hf_models_search",
        endpoint_patterns=(r"^/api/models$",),
        default_metadata=_metadata(
            default_fields=REPO_SUMMARY_FIELDS,
            guaranteed_fields=["repo_id", "repo_type", "author", "repo_url"],
            optional_fields=REPO_SUMMARY_OPTIONAL_FIELDS,
            default_limit=20,
            max_limit=5_000,
            notes=(
                "Thin model-search wrapper around the Hub list_models path. Prefer this "
                "over hf_repo_search for model-only queries."
            ),
        ),
        pagination={"default_limit": 20, "max_limit": 5_000},
    ),
    "hf_datasets_search": _config(
        "hf_datasets_search",
        endpoint_patterns=(r"^/api/datasets$",),
        default_metadata=_metadata(
            default_fields=REPO_SUMMARY_FIELDS,
            guaranteed_fields=["repo_id", "repo_type", "author", "repo_url"],
            optional_fields=REPO_SUMMARY_OPTIONAL_FIELDS,
            default_limit=20,
            max_limit=5_000,
            notes=(
                "Thin dataset-search wrapper around the Hub list_datasets path. Prefer "
                "this over hf_repo_search for dataset-only queries."
            ),
        ),
        pagination={"default_limit": 20, "max_limit": 5_000},
    ),
    "hf_spaces_search": _config(
        "hf_spaces_search",
        endpoint_patterns=(r"^/api/spaces$",),
        default_metadata=_metadata(
            default_fields=REPO_SUMMARY_FIELDS,
            guaranteed_fields=["repo_id", "repo_type", "author", "repo_url"],
            optional_fields=REPO_SUMMARY_OPTIONAL_FIELDS,
            default_limit=20,
            max_limit=5_000,
            notes=(
                "Thin space-search wrapper around the Hub list_spaces path. Prefer this "
                "over hf_repo_search for space-only queries."
            ),
        ),
        pagination={"default_limit": 20, "max_limit": 5_000},
    ),
    "hf_repo_search": _config(
        "hf_repo_search",
        endpoint_patterns=(r"^/api/models$", r"^/api/datasets$", r"^/api/spaces$"),
        default_metadata=_metadata(
            default_fields=REPO_SUMMARY_FIELDS,
            guaranteed_fields=["repo_id", "repo_type", "author", "repo_url"],
            optional_fields=REPO_SUMMARY_OPTIONAL_FIELDS,
            default_limit=20,
            max_limit=5_000,
            notes=(
                "Small generic repo-search helper. Prefer hf_models_search, "
                "hf_datasets_search, or hf_spaces_search for single-type queries; use "
                "hf_repo_search for intentionally cross-type search."
            ),
        ),
        pagination={"default_limit": 20, "max_limit": 5_000},
    ),
    "hf_user_graph": _config(
        "hf_user_graph",
        endpoint_patterns=(
            r"^/api/users/[^/]+/(followers|following)$",
            r"^/api/organizations/[^/]+/followers$",
        ),
        default_metadata=_metadata(
            default_fields=list(ACTOR_CANONICAL_FIELDS),
            guaranteed_fields=["username"],
            optional_fields=ACTOR_OPTIONAL_FIELDS,
            default_limit=1_000,
            max_limit=GRAPH_SCAN_LIMIT_CAP,
            notes="Returns followers/following summary rows.",
        ),
        pagination={
            "default_limit": 1_000,
            "max_limit": GRAPH_SCAN_LIMIT_CAP,
            "scan_max": GRAPH_SCAN_LIMIT_CAP,
        },
    ),
    "hf_repo_likers": _config(
        "hf_repo_likers",
        endpoint_patterns=(
            r"^/api/(models|datasets|spaces)/(?:[^/]+|[^/]+/[^/]+)/likers$",
        ),
        default_metadata=_metadata(
            default_fields=list(ACTOR_CANONICAL_FIELDS),
            guaranteed_fields=["username"],
            optional_fields=ACTOR_OPTIONAL_FIELDS,
            default_limit=1_000,
            notes="Returns users who liked a repo.",
        ),
        pagination={"default_limit": 1_000},
    ),
    "hf_user_likes": _config(
        "hf_user_likes",
        endpoint_patterns=(r"^/api/users/[^/]+/likes$",),
        default_metadata=_metadata(
            default_fields=list(USER_LIKES_CANONICAL_FIELDS),
            guaranteed_fields=["liked_at", "repo_id", "repo_type"],
            optional_fields=["repo_author", "repo_likes", "repo_downloads", "repo_url"],
            default_limit=100,
            max_limit=2_000,
            may_fan_out=True,
            notes=(
                "Default recency mode is cheap. Popularity-ranked sorts use canonical keys "
                "liked_at/repo_likes/repo_downloads and may enrich shortlisted repos with "
                "extra detail calls."
            ),
        ),
        pagination={
            "default_limit": 100,
            "enrich_max": LIKES_ENRICHMENT_MAX_REPOS,
            "ranking_default": LIKES_RANKING_WINDOW_DEFAULT,
            "scan_max": LIKES_SCAN_LIMIT_CAP,
        },
    ),
    "hf_recent_activity": _config(
        "hf_recent_activity",
        endpoint_patterns=(r"^/api/recent-activity$",),
        default_metadata=_metadata(
            default_fields=list(ACTIVITY_CANONICAL_FIELDS),
            guaranteed_fields=["event_type", "timestamp"],
            optional_fields=["repo_id", "repo_type"],
            default_limit=100,
            max_limit=2_000,
            may_fan_out=True,
            notes="Activity helper may fetch multiple pages when requested coverage exceeds one page.",
        ),
        pagination={
            "default_limit": 100,
            "max_pages": RECENT_ACTIVITY_SCAN_MAX_PAGES,
            "page_limit": RECENT_ACTIVITY_PAGE_SIZE,
        },
    ),
    "hf_repo_discussions": _config(
        "hf_repo_discussions",
        endpoint_patterns=(r"^/api/(models|datasets|spaces)/[^/]+/[^/]+/discussions$",),
        default_metadata=_metadata(
            default_fields=list(DISCUSSION_CANONICAL_FIELDS),
            guaranteed_fields=["num", "title", "author", "status"],
            optional_fields=["repo_id", "repo_type", "created_at", "url"],
            default_limit=20,
            max_limit=200,
            notes="Discussion summary helper.",
        ),
    ),
    "hf_repo_discussion_details": _config(
        "hf_repo_discussion_details",
        endpoint_patterns=(
            r"^/api/(models|datasets|spaces)/[^/]+/[^/]+/discussions/\d+$",
        ),
        default_metadata=_metadata(
            default_fields=list(DISCUSSION_DETAIL_CANONICAL_FIELDS),
            guaranteed_fields=["repo_id", "repo_type", "title", "author", "status"],
            optional_fields=[
                "num",
                "created_at",
                "url",
                "comment_count",
                "latest_comment_author",
                "latest_comment_created_at",
                "latest_comment_text",
                "latest_comment_html",
            ],
            notes="Exact discussion detail helper.",
        ),
    ),
    "hf_repo_details": _config(
        "hf_repo_details",
        endpoint_patterns=(r"^/api/(models|datasets|spaces)/[^/]+/[^/]+$",),
        default_metadata=_metadata(
            default_fields=REPO_SUMMARY_FIELDS,
            guaranteed_fields=["repo_id", "repo_type", "author", "repo_url"],
            optional_fields=REPO_SUMMARY_OPTIONAL_FIELDS,
            may_fan_out=True,
            notes="Exact repo metadata path. Multiple repo_ids may trigger one detail call per requested repo.",
        ),
    ),
    "hf_trending": _config(
        "hf_trending",
        endpoint_patterns=(r"^/api/trending$",),
        default_metadata=_metadata(
            default_fields=TRENDING_DEFAULT_FIELDS,
            guaranteed_fields=[
                "repo_id",
                "repo_type",
                "author",
                "repo_url",
                "trending_rank",
            ],
            optional_fields=TRENDING_OPTIONAL_FIELDS,
            default_limit=20,
            max_limit=TRENDING_ENDPOINT_MAX_LIMIT,
            notes="Returns ordered trending summary rows only. Use hf_repo_details for exact repo metadata.",
        ),
        pagination={"default_limit": 20, "max_limit": TRENDING_ENDPOINT_MAX_LIMIT},
    ),
    "hf_daily_papers": _config(
        "hf_daily_papers",
        endpoint_patterns=(r"^/api/daily_papers$",),
        default_metadata=_metadata(
            default_fields=DAILY_PAPER_DEFAULT_FIELDS,
            guaranteed_fields=["paper_id", "title", "published_at", "rank"],
            optional_fields=DAILY_PAPER_OPTIONAL_FIELDS,
            default_limit=20,
            max_limit=OUTPUT_ITEMS_TRUNCATION_LIMIT,
            notes="Returns daily paper summary rows. repo_id is omitted unless the upstream payload provides it.",
        ),
        pagination={"default_limit": 20, "max_limit": OUTPUT_ITEMS_TRUNCATION_LIMIT},
    ),
    "hf_collections_search": _config(
        "hf_collections_search",
        endpoint_patterns=(r"^/api/collections$",),
        default_metadata=_metadata(
            default_fields=COLLECTION_DEFAULT_FIELDS,
            guaranteed_fields=["collection_id", "title", "owner"],
            optional_fields=COLLECTION_OPTIONAL_FIELDS,
            default_limit=20,
            max_limit=OUTPUT_ITEMS_TRUNCATION_LIMIT,
            notes="Collection summary helper.",
        ),
        pagination={"default_limit": 20, "max_limit": OUTPUT_ITEMS_TRUNCATION_LIMIT},
    ),
    "hf_collection_items": _config(
        "hf_collection_items",
        endpoint_patterns=(
            r"^/api/collections/[^/]+$",
            r"^/api/collections/[^/]+/[^/]+$",
        ),
        default_metadata=_metadata(
            default_fields=REPO_SUMMARY_FIELDS,
            guaranteed_fields=["repo_id", "repo_type", "repo_url"],
            optional_fields=[
                field
                for field in REPO_CANONICAL_FIELDS
                if field not in {"repo_id", "repo_type", "repo_url"}
            ],
            default_limit=100,
            max_limit=OUTPUT_ITEMS_TRUNCATION_LIMIT,
            notes="Returns repos inside one collection as summary rows.",
        ),
        pagination={"default_limit": 100, "max_limit": OUTPUT_ITEMS_TRUNCATION_LIMIT},
    ),
}

HELPER_EXTERNALS = tuple(HELPER_CONFIGS)

HELPER_DEFAULT_METADATA: dict[str, dict[str, Any]] = {
    name: dict(config.default_metadata) for name, config in HELPER_CONFIGS.items()
}

PAGINATION_POLICY: dict[str, dict[str, Any]] = {
    name: dict(config.pagination)
    for name, config in HELPER_CONFIGS.items()
    if config.pagination
}

HELPER_COVERED_ENDPOINT_PATTERNS: list[tuple[str, str]] = [
    (pattern, config.name)
    for config in HELPER_CONFIGS.values()
    for pattern in config.endpoint_patterns
]

ALLOWLIST_PATTERNS = [
    r"^/api/whoami-v2$",
    r"^/api/trending$",
    r"^/api/daily_papers$",
    r"^/api/models$",
    r"^/api/datasets$",
    r"^/api/spaces$",
    r"^/api/models-tags-by-type$",
    r"^/api/datasets-tags-by-type$",
    r"^/api/(models|datasets|spaces)/[^/]+/[^/]+$",
    r"^/api/(models|datasets|spaces)/[^/]+/[^/]+/discussions$",
    r"^/api/(models|datasets|spaces)/[^/]+/[^/]+/discussions/\d+$",
    r"^/api/(models|datasets|spaces)/[^/]+/[^/]+/discussions/\d+/status$",
    r"^/api/users/[^/]+/overview$",
    r"^/api/users/[^/]+/socials$",
    r"^/api/users/[^/]+/followers$",
    r"^/api/users/[^/]+/following$",
    r"^/api/users/[^/]+/likes$",
    r"^/api/(models|datasets|spaces)/(?:[^/]+|[^/]+/[^/]+)/likers$",
    r"^/api/organizations/[^/]+/overview$",
    r"^/api/organizations/[^/]+/members$",
    r"^/api/organizations/[^/]+/followers$",
    r"^/api/collections$",
    r"^/api/collections/[^/]+$",
    r"^/api/collections/[^/]+/[^/]+$",
    r"^/api/recent-activity$",
]

STRICT_ALLOWLIST_PATTERNS = [
    r"^/api/users/[^/]+/overview$",
    r"^/api/users/[^/]+/socials$",
    r"^/api/whoami-v2$",
    r"^/api/trending$",
    r"^/api/daily_papers$",
    r"^/api/(models|datasets|spaces)/(?:[^/]+|[^/]+/[^/]+)/likers$",
    r"^/api/collections$",
    r"^/api/collections/[^/]+$",
    r"^/api/collections/[^/]+/[^/]+$",
    r"^/api/(models|datasets|spaces)/[^/]+/[^/]+/discussions$",
    r"^/api/(models|datasets|spaces)/[^/]+/[^/]+/discussions/\d+$",
    r"^/api/(models|datasets|spaces)/[^/]+/[^/]+/discussions/\d+/status$",
]

REPO_API_ADAPTERS: dict[str, RepoApiAdapter] = {
    "model": RepoApiAdapter(
        list_method_name="list_models", detail_method_name="model_info"
    ),
    "dataset": RepoApiAdapter(
        list_method_name="list_datasets", detail_method_name="dataset_info"
    ),
    "space": RepoApiAdapter(
        list_method_name="list_spaces", detail_method_name="space_info"
    ),
}
