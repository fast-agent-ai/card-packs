from __future__ import annotations

# ruff: noqa: C901, PLR0912, PLR0913, PLR0915, PLR0917
import inspect
from functools import partial
from typing import Any, Callable

from ..aliases import REPO_SORT_KEYS
from ..constants import (
    ACTIVITY_CANONICAL_FIELDS,
    ACTOR_CANONICAL_FIELDS,
    COLLECTION_CANONICAL_FIELDS,
    DAILY_PAPER_CANONICAL_FIELDS,
    DISCUSSION_CANONICAL_FIELDS,
    DISCUSSION_DETAIL_CANONICAL_FIELDS,
    DEFAULT_MAX_CALLS,
    DEFAULT_TIMEOUT_SEC,
    GRAPH_SCAN_LIMIT_CAP,
    LIKES_SCAN_LIMIT_CAP,
    MAX_CALLS_LIMIT,
    OUTPUT_ITEMS_TRUNCATION_LIMIT,
    PROFILE_CANONICAL_FIELDS,
    RECENT_ACTIVITY_SCAN_MAX_PAGES,
    REPO_CANONICAL_FIELDS,
    TRENDING_ENDPOINT_MAX_LIMIT,
    USER_CANONICAL_FIELDS,
    USER_LIKES_CANONICAL_FIELDS,
)
from ..context_types import HelperRuntimeContext
from ..registry import (
    HELPER_COVERED_ENDPOINT_PATTERNS,
    HELPER_DEFAULT_METADATA,
    PAGINATION_POLICY,
    REPO_SEARCH_ALLOWED_EXPAND,
    REPO_SEARCH_EXTRA_ARGS,
)


def _render_annotation(annotation: Any) -> str:
    if annotation is inspect.Signature.empty:
        return "Any"
    return str(annotation)


def _render_default(default: Any) -> str | None:
    if default is inspect.Signature.empty:
        return None
    return repr(default)


def _signature_payload(fn: Callable[..., Any]) -> dict[str, Any]:
    signature = inspect.signature(fn)
    parameters: list[dict[str, Any]] = []
    for parameter in signature.parameters.values():
        item: dict[str, Any] = {
            "name": parameter.name,
            "kind": str(parameter.kind).replace("Parameter.", "").lower(),
            "annotation": _render_annotation(parameter.annotation),
            "required": parameter.default is inspect.Signature.empty,
        }
        default = _render_default(parameter.default)
        if default is not None:
            item["default"] = default
        parameters.append(item)
    return {
        "parameters": parameters,
        "returns": _render_annotation(signature.return_annotation),
    }


async def hf_runtime_capabilities(
    ctx: HelperRuntimeContext,
    section: str | None = None,
) -> dict[str, Any]:
    start_calls = ctx.call_count["n"]
    ctx.internal_helper_used["used"] = True

    helper_functions = {
        **ctx.helper_registry,
        "hf_runtime_capabilities": partial(hf_runtime_capabilities, ctx),
    }
    helper_payload = {
        name: _signature_payload(fn) for name, fn in sorted(helper_functions.items())
    }
    manifest: dict[str, Any] = {
        "overview": {
            "helper_count": len(helper_functions),
            "supports_current_user": True,
            "helper_result_envelope": {
                "ok": "bool",
                "item": "dict | None",
                "items": "list[dict]",
                "meta": "dict",
                "error": "str | None",
            },
            "raw_result_envelope": {
                "result": "Any",
                "meta": {
                    "ok": "bool",
                    "api_calls": "int",
                    "elapsed_ms": "int",
                    "limits_reached": "bool",
                    "limit_summary": "list[dict]",
                },
            },
        },
        "helpers": helper_payload,
        "fields": {
            "profile": list(PROFILE_CANONICAL_FIELDS),
            "repo": list(REPO_CANONICAL_FIELDS),
            "user": list(USER_CANONICAL_FIELDS),
            "actor": list(ACTOR_CANONICAL_FIELDS),
            "user_likes": list(USER_LIKES_CANONICAL_FIELDS),
            "activity": list(ACTIVITY_CANONICAL_FIELDS),
            "collection": list(COLLECTION_CANONICAL_FIELDS),
            "daily_paper": list(DAILY_PAPER_CANONICAL_FIELDS),
            "discussion": list(DISCUSSION_CANONICAL_FIELDS),
            "discussion_detail": list(DISCUSSION_DETAIL_CANONICAL_FIELDS),
        },
        "helper_defaults": {
            helper_name: dict(sorted(metadata.items()))
            for helper_name, metadata in sorted(HELPER_DEFAULT_METADATA.items())
        },
        "limits": {
            "default_timeout_sec": DEFAULT_TIMEOUT_SEC,
            "default_max_calls": DEFAULT_MAX_CALLS,
            "max_calls_limit": MAX_CALLS_LIMIT,
            "output_items_truncation_limit": OUTPUT_ITEMS_TRUNCATION_LIMIT,
            "graph_scan_limit_cap": GRAPH_SCAN_LIMIT_CAP,
            "likes_scan_limit_cap": LIKES_SCAN_LIMIT_CAP,
            "recent_activity_scan_max_pages": RECENT_ACTIVITY_SCAN_MAX_PAGES,
            "trending_endpoint_max_limit": TRENDING_ENDPOINT_MAX_LIMIT,
            "pagination_policy": {
                helper_name: dict(sorted(policy.items()))
                for helper_name, policy in sorted(PAGINATION_POLICY.items())
            },
            "helper_covered_endpoint_patterns": [
                {"pattern": pattern, "helper": helper_name}
                for pattern, helper_name in HELPER_COVERED_ENDPOINT_PATTERNS
            ],
        },
        "repo_search": {
            "helper_selection": {
                "preferred_rule": (
                    "Prefer hf_models_search for model queries, hf_datasets_search for "
                    "dataset queries, and hf_spaces_search for space queries. Use "
                    "hf_repo_search only for intentionally cross-type search."
                ),
                "model": "hf_models_search",
                "dataset": "hf_datasets_search",
                "space": "hf_spaces_search",
                "cross_type": "hf_repo_search",
            },
            "can_do": [
                "search models",
                "search datasets",
                "search spaces",
                "search across multiple repo types",
                "project selected fields",
                "apply local post-fetch row filtering",
            ],
            "parameter_contract": {
                "search": {
                    "meaning": "Upstream Hugging Face search text.",
                },
                "filter": {
                    "meaning": (
                        "Upstream Hugging Face filter/tag argument passed directly into "
                        "the Hub client."
                    ),
                },
                "post_filter": {
                    "meaning": (
                        "Local predicate applied after the rows are fetched and normalized."
                    ),
                    "recommended_shapes": [
                        {"runtime_stage": "RUNNING"},
                        {"runtime_stage": {"in": ["BUILD_ERROR", "RUNTIME_ERROR"]}},
                        {"downloads": {"gte": 1000}},
                        {"likes": {"lte": 5000}},
                    ],
                    "prefer_for": [
                        "normalized returned fields such as runtime_stage",
                        "downloads / likes thresholds after a broad search",
                    ],
                    "avoid_when": [
                        "author is already a first-class helper argument",
                        "pipeline_tag is already a first-class model-search argument",
                        "dataset_name, language, task_ids, apps, models, or datasets already have first-class helper args",
                    ],
                },
                "fields": {
                    "meaning": "Select which normalized row fields are returned to the caller.",
                    "canonical_only": True,
                },
            },
            "repo_type_specific_helpers": {
                "model": {
                    "helper": "hf_models_search",
                    "preferred_params": [
                        "search",
                        "filter",
                        "author",
                        "pipeline_tag",
                        "sort",
                        "limit",
                        "expand",
                        "fields",
                        "post_filter",
                    ],
                    "expand_values": list(REPO_SEARCH_ALLOWED_EXPAND["model"]),
                },
                "dataset": {
                    "helper": "hf_datasets_search",
                    "preferred_params": [
                        "search",
                        "filter",
                        "author",
                        "dataset_name",
                        "language",
                        "task_categories",
                        "task_ids",
                        "sort",
                        "limit",
                        "expand",
                        "fields",
                        "post_filter",
                    ],
                    "expand_values": list(REPO_SEARCH_ALLOWED_EXPAND["dataset"]),
                },
                "space": {
                    "helper": "hf_spaces_search",
                    "preferred_params": [
                        "search",
                        "filter",
                        "author",
                        "datasets",
                        "models",
                        "linked",
                        "sort",
                        "limit",
                        "expand",
                        "fields",
                        "post_filter",
                    ],
                    "expand_values": list(REPO_SEARCH_ALLOWED_EXPAND["space"]),
                },
            },
            "generic_helper": {
                "helper": "hf_repo_search",
                "use_for": "Intentionally cross-type search only.",
                "supports": [
                    "search",
                    "repo_type",
                    "repo_types",
                    "filter",
                    "author",
                    "sort",
                    "limit",
                    "fields",
                    "post_filter",
                ],
                "does_not_support": [
                    "repo-type-specific knobs such as pipeline_tag or dataset_name",
                    "nested advanced routing",
                ],
            },
            "sort_keys": {
                repo_type: sorted(keys)
                for repo_type, keys in sorted(REPO_SORT_KEYS.items())
            },
            "extra_args": {
                repo_type: sorted(args)
                for repo_type, args in sorted(REPO_SEARCH_EXTRA_ARGS.items())
            },
            "space_runtime_contract": {
                "returned_field": "runtime_stage",
                "full_runtime_field": "runtime",
                "preferred_filter_channel": "post_filter",
                "note": (
                    "Treat runtime_stage like any other returned field: use exact values "
                    "or an 'in' list in post_filter."
                ),
                "common_values": ["BUILD_ERROR", "RUNTIME_ERROR", "RUNNING", "SLEEPING"],
            },
        },
    }
    allowed_sections = sorted(manifest)
    requested = str(section or "").strip().lower()
    if requested:
        if requested not in manifest:
            return ctx._helper_error(
                start_calls=start_calls,
                source="internal://runtime-capabilities",
                error=f"Unsupported section {section!r}. Allowed sections: {allowed_sections}",
                section=section,
                allowed_sections=allowed_sections,
            )
        payload = {
            "section": requested,
            "content": manifest[requested],
            "allowed_sections": allowed_sections,
        }
    else:
        payload = {"allowed_sections": allowed_sections, **manifest}
    return ctx._helper_success(
        start_calls=start_calls,
        source="internal://runtime-capabilities",
        items=[payload],
        section=requested or None,
    )


def register_introspection_helpers(
    ctx: HelperRuntimeContext,
) -> dict[str, Callable[..., Any]]:
    return {"hf_runtime_capabilities": partial(hf_runtime_capabilities, ctx)}
