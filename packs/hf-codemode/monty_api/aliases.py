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
