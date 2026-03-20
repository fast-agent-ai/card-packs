from __future__ import annotations

DEFAULT_TIMEOUT_SEC = 90  # Default end-to-end timeout for one Monty run.

DEFAULT_MAX_CALLS = 400  # Default external-call budget exposed to callers.

MAX_CALLS_LIMIT = 400  # Absolute max external-call budget accepted by the runtime.

INTERNAL_STRICT_MODE = False

OUTPUT_ITEMS_TRUNCATION_LIMIT = (
    500  # Final output truncation for oversized `items` payloads.
)

EXHAUSTIVE_HELPER_RETURN_HARD_CAP = (
    2_000  # Runtime hard cap for exhaustive-helper output rows.
)

SELECTIVE_ENDPOINT_RETURN_HARD_CAP = (
    200  # Default cap for one-shot selective endpoint helpers.
)

TRENDING_ENDPOINT_MAX_LIMIT = 20  # Upstream `/api/trending` endpoint maximum.

GRAPH_SCAN_LIMIT_CAP = 10_000  # Max follower/member rows scanned in one helper call.

LIKES_SCAN_LIMIT_CAP = 10_000  # Max like-event rows scanned in one helper call.

LIKES_RANKING_WINDOW_DEFAULT = (
    40  # Default shortlist size when ranking likes by repo popularity.
)

LIKES_ENRICHMENT_MAX_REPOS = (
    50  # Max liked repos enriched with extra repo-detail calls.
)

RECENT_ACTIVITY_PAGE_SIZE = 100  # Rows requested per `/api/recent-activity` page.

RECENT_ACTIVITY_SCAN_MAX_PAGES = (
    10  # Max recent-activity pages fetched in one helper call.
)

USER_SUMMARY_LIKES_SCAN_LIMIT = 1_000  # Like rows sampled for user summary.

USER_SUMMARY_ACTIVITY_MAX_PAGES = 3  # Activity pages sampled for user summary.

DEFAULT_MONTY_MAX_MEMORY = 64 * 1024 * 1024  # 64 MiB

DEFAULT_MONTY_MAX_ALLOCATIONS = (
    250_000  # Approximate object-allocation ceiling in the sandbox.
)

DEFAULT_MONTY_MAX_RECURSION_DEPTH = 100  # Python recursion limit inside the sandbox.

REPO_CANONICAL_FIELDS: tuple[str, ...] = (
    "repo_id",
    "repo_type",
    "author",
    "likes",
    "downloads",
    "created_at",
    "last_modified",
    "pipeline_tag",
    "num_params",
    "repo_url",
    "tags",
    "library_name",
    "description",
    "paperswithcode_id",
    "sdk",
    "models",
    "datasets",
    "subdomain",
    "runtime_stage",
    "runtime",
)

USER_CANONICAL_FIELDS: tuple[str, ...] = (
    "username",
    "fullname",
    "bio",
    "website_url",
    "twitter",
    "github",
    "linkedin",
    "bluesky",
    "followers",
    "following",
    "likes",
    "is_pro",
)

PROFILE_CANONICAL_FIELDS: tuple[str, ...] = (
    "handle",
    "entity_type",
    "display_name",
    "bio",
    "description",
    "avatar_url",
    "website_url",
    "twitter_url",
    "github_url",
    "linkedin_url",
    "bluesky_url",
    "followers_count",
    "following_count",
    "likes_count",
    "members_count",
    "models_count",
    "datasets_count",
    "spaces_count",
    "discussions_count",
    "papers_count",
    "upvotes_count",
    "organizations",
    "is_pro",
    "likes_sample",
    "activity_sample",
)

ACTOR_CANONICAL_FIELDS: tuple[str, ...] = (
    "username",
    "fullname",
    "is_pro",
    "role",
    "type",
)

USER_LIKES_CANONICAL_FIELDS: tuple[str, ...] = (
    "liked_at",
    "repo_id",
    "repo_type",
    "repo_author",
    "repo_likes",
    "repo_downloads",
    "repo_url",
)

DISCUSSION_CANONICAL_FIELDS: tuple[str, ...] = (
    "num",
    "repo_id",
    "repo_type",
    "title",
    "author",
    "created_at",
    "status",
    "url",
)

DISCUSSION_DETAIL_CANONICAL_FIELDS: tuple[str, ...] = (
    "num",
    "repo_id",
    "repo_type",
    "title",
    "author",
    "created_at",
    "status",
    "url",
    "comment_count",
    "latest_comment_author",
    "latest_comment_created_at",
    "latest_comment_text",
    "latest_comment_html",
)

ACTIVITY_CANONICAL_FIELDS: tuple[str, ...] = (
    "event_type",
    "repo_id",
    "repo_type",
    "timestamp",
)

COLLECTION_CANONICAL_FIELDS: tuple[str, ...] = (
    "collection_id",
    "slug",
    "title",
    "owner",
    "owner_type",
    "description",
    "last_updated",
    "item_count",
)

DAILY_PAPER_CANONICAL_FIELDS: tuple[str, ...] = (
    "paper_id",
    "title",
    "summary",
    "published_at",
    "submitted_on_daily_at",
    "authors",
    "organization",
    "submitted_by",
    "discussion_id",
    "upvotes",
    "github_repo_url",
    "github_stars",
    "project_page_url",
    "num_comments",
    "is_author_participating",
    "repo_id",
    "rank",
)
