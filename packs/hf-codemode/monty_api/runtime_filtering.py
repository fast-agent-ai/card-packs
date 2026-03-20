from __future__ import annotations

from typing import Any

from .constants import (
    ACTIVITY_CANONICAL_FIELDS,
    ACTOR_CANONICAL_FIELDS,
    COLLECTION_CANONICAL_FIELDS,
    DAILY_PAPER_CANONICAL_FIELDS,
    DISCUSSION_CANONICAL_FIELDS,
    DISCUSSION_DETAIL_CANONICAL_FIELDS,
    REPO_CANONICAL_FIELDS,
    USER_CANONICAL_FIELDS,
    USER_LIKES_CANONICAL_FIELDS,
)
from .http_runtime import _as_int


def _allowed_field_set(allowed_fields: tuple[str, ...] | list[str] | set[str]) -> set[str]:
    return {str(field).strip() for field in allowed_fields if str(field).strip()}


def _project_items(
    self: Any,
    items: list[dict[str, Any]],
    fields: list[str] | None,
    *,
    allowed_fields: tuple[str, ...] | list[str] | set[str] | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(fields, list) or not fields:
        return items
    wanted = [str(field).strip() for field in fields if str(field).strip()]
    if not wanted:
        return items
    if allowed_fields is not None:
        allowed = _allowed_field_set(allowed_fields)
        invalid = sorted(field for field in wanted if field not in allowed)
        if invalid:
            raise ValueError(
                f"Unsupported fields {invalid}. Allowed fields: {sorted(allowed)}"
            )
    projected: list[dict[str, Any]] = []
    for row in items:
        out: dict[str, Any] = {}
        for key in wanted:
            value = row.get(key)
            if value is None:
                continue
            out[key] = value
        projected.append(out)
    return projected


def _project_repo_items(
    self: Any, items: list[dict[str, Any]], fields: list[str] | None
) -> list[dict[str, Any]]:
    return _project_items(self, items, fields, allowed_fields=REPO_CANONICAL_FIELDS)


def _project_collection_items(
    self: Any, items: list[dict[str, Any]], fields: list[str] | None
) -> list[dict[str, Any]]:
    return _project_items(
        self, items, fields, allowed_fields=COLLECTION_CANONICAL_FIELDS
    )


def _project_daily_paper_items(
    self: Any, items: list[dict[str, Any]], fields: list[str] | None
) -> list[dict[str, Any]]:
    return _project_items(
        self, items, fields, allowed_fields=DAILY_PAPER_CANONICAL_FIELDS
    )


def _project_user_items(
    self: Any, items: list[dict[str, Any]], fields: list[str] | None
) -> list[dict[str, Any]]:
    return _project_items(self, items, fields, allowed_fields=USER_CANONICAL_FIELDS)


def _project_actor_items(
    self: Any, items: list[dict[str, Any]], fields: list[str] | None
) -> list[dict[str, Any]]:
    return _project_items(self, items, fields, allowed_fields=ACTOR_CANONICAL_FIELDS)


def _project_user_like_items(
    self: Any, items: list[dict[str, Any]], fields: list[str] | None
) -> list[dict[str, Any]]:
    return _project_items(
        self, items, fields, allowed_fields=USER_LIKES_CANONICAL_FIELDS
    )


def _project_activity_items(
    self: Any, items: list[dict[str, Any]], fields: list[str] | None
) -> list[dict[str, Any]]:
    return _project_items(
        self, items, fields, allowed_fields=ACTIVITY_CANONICAL_FIELDS
    )


def _project_discussion_items(
    self: Any, items: list[dict[str, Any]], fields: list[str] | None
) -> list[dict[str, Any]]:
    return _project_items(
        self, items, fields, allowed_fields=DISCUSSION_CANONICAL_FIELDS
    )


def _project_discussion_detail_items(
    self: Any, items: list[dict[str, Any]], fields: list[str] | None
) -> list[dict[str, Any]]:
    return _project_items(
        self, items, fields, allowed_fields=DISCUSSION_DETAIL_CANONICAL_FIELDS
    )


def _normalize_where(
    self: Any,
    where: dict[str, Any] | None,
    *,
    allowed_fields: tuple[str, ...] | list[str] | set[str] | None = None,
) -> dict[str, Any] | None:
    if not isinstance(where, dict) or not where:
        return where
    allowed = _allowed_field_set(allowed_fields) if allowed_fields is not None else None
    normalized: dict[str, Any] = {}
    for key, value in where.items():
        raw_key = str(key).strip()
        if not raw_key:
            continue
        if allowed is not None and raw_key not in allowed:
            raise ValueError(
                f"Unsupported filter fields {[raw_key]}. Allowed fields: {sorted(allowed)}"
            )
        normalized[raw_key] = value
    return normalized


def _item_matches_where(
    self: Any, item: dict[str, Any], where: dict[str, Any] | None
) -> bool:
    if not isinstance(where, dict) or not where:
        return True
    for key, cond in where.items():
        value = item.get(str(key))
        if isinstance(cond, dict):
            if "eq" in cond and value != cond.get("eq"):
                return False
            if "in" in cond:
                allowed = cond.get("in")
                if isinstance(allowed, (list, tuple, set)) and value not in allowed:
                    return False
            if "contains" in cond:
                needle = cond.get("contains")
                if (
                    not isinstance(value, str)
                    or not isinstance(needle, str)
                    or needle not in value
                ):
                    return False
            if "icontains" in cond:
                needle = cond.get("icontains")
                if (
                    not isinstance(value, str)
                    or not isinstance(needle, str)
                    or needle.lower() not in value.lower()
                ):
                    return False
            if "gte" in cond:
                left = _as_int(value)
                right = _as_int(cond.get("gte"))
                if left is None or right is None or left < right:
                    return False
            if "lte" in cond:
                left = _as_int(value)
                right = _as_int(cond.get("lte"))
                if left is None or right is None or left > right:
                    return False
            continue
        if isinstance(cond, (list, tuple, set)):
            if value not in cond:
                return False
            continue
        if value != cond:
            return False
    return True


def _apply_where(
    self: Any,
    items: list[dict[str, Any]],
    where: dict[str, Any] | None,
    *,
    allowed_fields: tuple[str, ...] | list[str] | set[str] | None = None,
) -> list[dict[str, Any]]:
    normalized_where = _normalize_where(self, where, allowed_fields=allowed_fields)
    if not isinstance(normalized_where, dict) or not normalized_where:
        return items
    return [row for row in items if _item_matches_where(self, row, normalized_where)]


def _helper_item(self: Any, resp: dict[str, Any]) -> dict[str, Any] | None:
    item = resp.get("item")
    if isinstance(item, dict):
        return item
    items = resp.get("items")
    if isinstance(items, list) and items and isinstance(items[0], dict):
        return items[0]
    return None


def _overview_count(self: Any, item: dict[str, Any] | None, key: str) -> int | None:
    if not isinstance(item, dict):
        return None
    return _as_int(item.get(key))
