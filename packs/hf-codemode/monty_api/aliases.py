from __future__ import annotations

from typing import get_args

from huggingface_hub.hf_api import DatasetSort_T, ModelSort_T, SpaceSort_T

REPO_SORT_KEYS: dict[str, set[str]] = {
    "model": set(get_args(ModelSort_T))
    or {
        "created_at",
        "downloads",
        "last_modified",
        "likes",
        "trending_score",
    },
    "dataset": set(get_args(DatasetSort_T))
    or {
        "created_at",
        "downloads",
        "last_modified",
        "likes",
        "trending_score",
    },
    "space": set(get_args(SpaceSort_T))
    or {
        "created_at",
        "last_modified",
        "likes",
        "trending_score",
    },
}

# Alias policy:
# - canonical names stay canonical
# - support a small compatibility set for observed prompt/output variants
# - do not add speculative synonyms unless they appear in prompts, evals, or
#   upstream payloads we already normalize
SORT_KEY_ALIASES: dict[str, str] = {
    "createdat": "created_at",
    "created_at": "created_at",
    "created-at": "created_at",
    "downloads": "downloads",
    "likes": "likes",
    "lastmodified": "last_modified",
    "last_modified": "last_modified",
    "last-modified": "last_modified",
    "trendingscore": "trending_score",
    "trending_score": "trending_score",
    "trending-score": "trending_score",
    "trending": "trending_score",
}

USER_FIELD_ALIASES: dict[str, str] = {
    "login": "username",
    "user": "username",
    "handle": "username",
    "name": "fullname",
    "full_name": "fullname",
    "is_pro": "isPro",
    "pro": "isPro",
}

ACTOR_FIELD_ALIASES: dict[str, str] = {
    **USER_FIELD_ALIASES,
    "entity_type": "type",
    "user_type": "type",
}

REPO_FIELD_ALIASES: dict[str, str] = {
    "repoid": "repo_id",
    "repotype": "repo_type",
    "repourl": "repo_url",
    "createdat": "created_at",
    "lastmodified": "last_modified",
    "pipelinetag": "pipeline_tag",
    "numparams": "num_params",
    "trendingrank": "trending_rank",
    "trendingscore": "trending_score",
    "libraryname": "library_name",
    "paperswithcodeid": "paperswithcode_id",
    "runtimestage": "runtime_stage",
    "runtimestatus": "runtime_stage",
}

COLLECTION_FIELD_ALIASES: dict[str, str] = {
    "collectionid": "collection_id",
    "lastupdated": "last_updated",
    "ownertype": "owner_type",
    "itemcount": "item_count",
    "author": "owner",
}

DAILY_PAPER_FIELD_ALIASES: dict[str, str] = {
    "paperid": "paper_id",
    "publishedat": "published_at",
    "submittedondailyat": "submitted_on_daily_at",
    "submittedby": "submitted_by",
    "discussionid": "discussion_id",
    "githubrepo": "github_repo_url",
    "githubstars": "github_stars",
    "projectpage": "project_page_url",
    "numcomments": "num_comments",
    "isauthorparticipating": "is_author_participating",
    "repoid": "repo_id",
}

USER_LIKES_FIELD_ALIASES: dict[str, str] = {
    "likedat": "liked_at",
    "repoid": "repo_id",
    "repotype": "repo_type",
    "repoauthor": "repo_author",
    "repolikes": "repo_likes",
    "repodownloads": "repo_downloads",
}

ACTIVITY_FIELD_ALIASES: dict[str, str] = {
    "time": "timestamp",
    "type": "event_type",
    "repoid": "repo_id",
    "repotype": "repo_type",
}
