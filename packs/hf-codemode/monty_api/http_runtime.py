from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from huggingface_hub import HfApi

from .aliases import REPO_SORT_KEYS, SORT_KEY_ALIASES
from .constants import (
    DEFAULT_TIMEOUT_SEC,
)
from .registry import REPO_API_ADAPTERS
from .validation import _endpoint_allowed, _normalize_endpoint, _sanitize_params


def _load_request_token() -> str | None:
    try:
        from fast_agent.mcp.auth.context import request_bearer_token  # type: ignore

        token = request_bearer_token.get()
        if token:
            return token
    except Exception:
        pass
    return None


def _load_token() -> str | None:
    token = _load_request_token()
    if token:
        return token
    return os.getenv("HF_TOKEN") or None


def _json_best_effort(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except Exception:
        return raw.decode("utf-8", errors="replace")


def _clamp_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        out = int(value)
    except Exception:
        out = default
    return max(minimum, min(out, maximum))


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None


def _canonical_repo_type(value: Any, *, default: str = "model") -> str:
    raw = str(value or "").strip().lower()
    aliases = {
        "model": "model",
        "models": "model",
        "dataset": "dataset",
        "datasets": "dataset",
        "space": "space",
        "spaces": "space",
    }
    return aliases.get(raw, default)


def _normalize_repo_sort_key(
    repo_type: str, sort_value: Any
) -> tuple[str | None, str | None]:
    raw = str(sort_value or "").strip()
    if not raw:
        return None, None

    key = SORT_KEY_ALIASES.get(raw.lower().replace(" ", "").replace("__", "_"))
    if key is None:
        key = SORT_KEY_ALIASES.get(raw.lower())
    if key is None:
        return None, f"Invalid sort key '{raw}'"

    rt = _canonical_repo_type(repo_type)
    allowed = REPO_SORT_KEYS.get(rt, set())
    if key not in allowed:
        return (
            None,
            f"Invalid sort key '{raw}' for repo_type='{rt}'. Allowed: {', '.join(sorted(allowed))}",
        )
    return key, None


def _repo_api_adapter(repo_type: str) -> Any:
    rt = _canonical_repo_type(repo_type, default="")
    adapter = REPO_API_ADAPTERS.get(rt)
    if adapter is None:
        raise ValueError(f"Unsupported repo_type '{repo_type}'")
    return adapter


def _repo_list_call(api: HfApi, repo_type: str, **kwargs: Any) -> list[Any]:
    adapter = _repo_api_adapter(repo_type)
    method = getattr(api, adapter.list_method_name)
    return list(method(**kwargs))


def _repo_detail_call(api: HfApi, repo_type: str, repo_id: str) -> Any:
    adapter = _repo_api_adapter(repo_type)
    method = getattr(api, adapter.detail_method_name)
    return method(repo_id)


def _coerce_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw = [value]
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raise ValueError("Expected a string or list of strings")
    return [str(v).strip() for v in raw if str(v).strip()]


def _optional_str_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        out = [value.strip()] if value.strip() else []
        return out or None
    if isinstance(value, (list, tuple, set)):
        out = [str(v).strip() for v in value if str(v).strip()]
        return out or None
    return None


def _extract_num_params(num_params: Any = None, safetensors: Any = None) -> int | None:
    direct = _as_int(num_params)
    if direct is not None:
        return direct

    total = getattr(safetensors, "total", None)
    if total is None and isinstance(safetensors, dict):
        total = safetensors.get("total")
    return _as_int(total)


def _extract_author_names(value: Any) -> list[str] | None:
    if not isinstance(value, (list, tuple)):
        return None
    names: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            names.append(item.strip())
            continue
        if isinstance(item, dict):
            name = item.get("name")
            if isinstance(name, str) and name.strip():
                names.append(name.strip())
            continue
        name = getattr(item, "name", None)
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names or None


def _extract_profile_name(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        for key in ("user", "name", "fullname", "handle"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return None
    for attr in ("user", "name", "fullname", "handle"):
        candidate = getattr(value, attr, None)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _author_from_any(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        for key in ("name", "username", "user", "login"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
    return None


def _dt_to_str(value: Any) -> str | None:
    if value is None:
        return None
    iso = getattr(value, "isoformat", None)
    if callable(iso):
        try:
            return str(iso())
        except Exception:
            pass
    return str(value)


def _repo_web_url(repo_type: str, repo_id: str | None) -> str | None:
    if not isinstance(repo_id, str) or not repo_id:
        return None
    base = os.getenv("HF_ENDPOINT", "https://huggingface.co").rstrip("/")
    rt = _canonical_repo_type(repo_type, default="")
    if rt == "dataset":
        return f"{base}/datasets/{repo_id}"
    if rt == "space":
        return f"{base}/spaces/{repo_id}"
    return f"{base}/{repo_id}"


def _build_repo_row(
    *,
    repo_id: Any,
    repo_type: str,
    author: Any = None,
    likes: Any = None,
    downloads: Any = None,
    created_at: Any = None,
    last_modified: Any = None,
    pipeline_tag: Any = None,
    num_params: Any = None,
    private: Any = None,
    trending_score: Any = None,
    tags: Any = None,
    sha: Any = None,
    gated: Any = None,
    library_name: Any = None,
    description: Any = None,
    paperswithcode_id: Any = None,
    sdk: Any = None,
    models: Any = None,
    datasets: Any = None,
    subdomain: Any = None,
) -> dict[str, Any]:
    rt = _canonical_repo_type(repo_type)
    author_value = author
    if (
        not isinstance(author_value, str)
        and isinstance(repo_id, str)
        and "/" in repo_id
    ):
        author_value = repo_id.split("/", 1)[0]

    return {
        "id": repo_id,
        "slug": repo_id,
        "repo_id": repo_id,
        "repo_type": rt,
        "author": author_value,
        "likes": _as_int(likes),
        "downloads": _as_int(downloads),
        "created_at": _dt_to_str(created_at),
        "last_modified": _dt_to_str(last_modified),
        "pipeline_tag": pipeline_tag,
        "num_params": _as_int(num_params),
        "private": private,
        "trending_score": _as_int(trending_score)
        if trending_score is not None
        else None,
        "repo_url": _repo_web_url(rt, repo_id if isinstance(repo_id, str) else None),
        "tags": _optional_str_list(tags),
        "sha": sha,
        "gated": gated,
        "library_name": library_name,
        "description": description,
        "paperswithcode_id": paperswithcode_id,
        "sdk": sdk,
        "models": _optional_str_list(models),
        "datasets": _optional_str_list(datasets),
        "subdomain": subdomain,
    }


def _normalize_repo_search_row(row: Any, repo_type: str) -> dict[str, Any]:
    return _build_repo_row(
        repo_id=getattr(row, "id", None),
        repo_type=repo_type,
        author=getattr(row, "author", None),
        likes=getattr(row, "likes", None),
        downloads=getattr(row, "downloads", None),
        created_at=getattr(row, "created_at", None),
        last_modified=getattr(row, "last_modified", None),
        pipeline_tag=getattr(row, "pipeline_tag", None),
        num_params=_extract_num_params(
            getattr(row, "num_params", None), getattr(row, "safetensors", None)
        ),
        private=getattr(row, "private", None),
        trending_score=getattr(row, "trending_score", None),
        tags=getattr(row, "tags", None),
        sha=getattr(row, "sha", None),
        gated=getattr(row, "gated", None),
        library_name=getattr(row, "library_name", None),
        description=getattr(row, "description", None),
        paperswithcode_id=getattr(row, "paperswithcode_id", None),
        sdk=getattr(row, "sdk", None),
        models=getattr(row, "models", None),
        datasets=getattr(row, "datasets", None),
        subdomain=getattr(row, "subdomain", None),
    )


def _normalize_repo_detail_row(
    detail: Any, repo_type: str, repo_id: str
) -> dict[str, Any]:
    row = _normalize_repo_search_row(detail, repo_type)
    resolved_repo_id = row.get("repo_id") or repo_id
    row["id"] = row.get("id") or resolved_repo_id
    row["slug"] = row.get("slug") or resolved_repo_id
    row["repo_id"] = resolved_repo_id
    row["repo_url"] = _repo_web_url(repo_type, resolved_repo_id)
    return row


def _normalize_trending_row(
    repo: dict[str, Any], default_repo_type: str, rank: int | None = None
) -> dict[str, Any]:
    raw_num_params = (
        repo.get("num_params")
        if repo.get("num_params") is not None
        else repo.get("numParameters")
    )
    row = _build_repo_row(
        repo_id=repo.get("id"),
        repo_type=repo.get("type") or repo.get("repoType") or default_repo_type,
        author=repo.get("author"),
        likes=repo.get("likes"),
        downloads=repo.get("downloads"),
        created_at=repo.get("createdAt"),
        last_modified=repo.get("lastModified"),
        pipeline_tag=repo.get("pipeline_tag"),
        num_params=_extract_num_params(raw_num_params, repo.get("safetensors")),
        private=repo.get("private"),
        trending_score=repo.get("trendingScore"),
        tags=repo.get("tags"),
        sha=repo.get("sha"),
        gated=repo.get("gated"),
        library_name=repo.get("library_name"),
        description=repo.get("description"),
        paperswithcode_id=repo.get("paperswithcode_id"),
        sdk=repo.get("sdk"),
        models=repo.get("models"),
        datasets=repo.get("datasets"),
        subdomain=repo.get("subdomain"),
    )
    if rank is not None:
        row["trending_rank"] = rank
    return row


def _normalize_daily_paper_row(
    row: dict[str, Any], rank: int | None = None
) -> dict[str, Any]:
    paper = row.get("paper") if isinstance(row.get("paper"), dict) else {}
    org = (
        row.get("organization")
        if isinstance(row.get("organization"), dict)
        else paper.get("organization")
    )
    organization = None
    if isinstance(org, dict):
        organization = org.get("name") or org.get("fullname")

    item = {
        "paper_id": paper.get("id"),
        "title": row.get("title") or paper.get("title"),
        "summary": row.get("summary")
        or paper.get("summary")
        or paper.get("ai_summary"),
        "published_at": row.get("publishedAt") or paper.get("publishedAt"),
        "submitted_on_daily_at": paper.get("submittedOnDailyAt"),
        "authors": _extract_author_names(paper.get("authors")),
        "organization": organization,
        "submitted_by": _extract_profile_name(
            row.get("submittedBy") or paper.get("submittedOnDailyBy")
        ),
        "discussion_id": paper.get("discussionId"),
        "upvotes": _as_int(paper.get("upvotes")),
        "github_repo_url": paper.get("githubRepo"),
        "github_stars": _as_int(paper.get("githubStars")),
        "project_page_url": paper.get("projectPage"),
        "num_comments": _as_int(row.get("numComments")),
        "is_author_participating": row.get("isAuthorParticipating")
        if isinstance(row.get("isAuthorParticipating"), bool)
        else None,
        "repo_id": row.get("repo_id") or paper.get("repo_id"),
        "rank": rank,
    }
    return item


def _normalize_collection_repo_item(row: dict[str, Any]) -> dict[str, Any] | None:
    repo_id = row.get("id") or row.get("repoId") or row.get("repo_id")
    if not isinstance(repo_id, str) or not repo_id:
        return None

    repo_type = _canonical_repo_type(
        row.get("repoType") or row.get("repo_type") or row.get("type"), default=""
    )
    if repo_type not in {"model", "dataset", "space"}:
        return None

    return _build_repo_row(
        repo_id=repo_id,
        repo_type=repo_type,
        author=row.get("author") or _author_from_any(row.get("authorData")),
        likes=row.get("likes"),
        downloads=row.get("downloads"),
        created_at=row.get("createdAt") or row.get("created_at"),
        last_modified=row.get("lastModified") or row.get("last_modified"),
        pipeline_tag=row.get("pipeline_tag") or row.get("pipelineTag"),
        num_params=_extract_num_params(row.get("num_params"), row.get("safetensors")),
        private=row.get("private"),
        tags=row.get("tags"),
        gated=row.get("gated"),
        library_name=row.get("library_name") or row.get("libraryName"),
        description=row.get("description"),
        paperswithcode_id=row.get("paperswithcode_id") or row.get("paperswithcodeId"),
        sdk=row.get("sdk"),
        models=row.get("models"),
        datasets=row.get("datasets"),
        subdomain=row.get("subdomain"),
    )


def _sort_repo_rows(
    rows: list[dict[str, Any]], sort_key: str | None
) -> list[dict[str, Any]]:
    if not sort_key:
        return rows

    if sort_key in {"likes", "downloads", "trending_score"}:
        return sorted(
            rows, key=lambda row: _as_int(row.get(sort_key)) or -1, reverse=True
        )

    if sort_key in {"created_at", "last_modified"}:
        return sorted(rows, key=lambda row: str(row.get(sort_key) or ""), reverse=True)

    return rows


def call_api_host(
    endpoint: str,
    *,
    method: str = "GET",
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    strict_mode: bool = False,
) -> dict[str, Any]:
    method_u = method.upper().strip()
    if method_u not in {"GET", "POST"}:
        raise ValueError("Only GET and POST are supported")

    ep = _normalize_endpoint(endpoint)
    if not _endpoint_allowed(ep, strict_mode):
        raise ValueError(f"Endpoint not allowed: {ep}")

    params = _sanitize_params(ep, params)
    if ep == "/api/recent-activity":
        feed_type = str((params or {}).get("feedType", "")).strip().lower()
        if feed_type not in {"user", "org"}:
            raise ValueError("/api/recent-activity requires feedType=user|org")
        if not str((params or {}).get("entity", "")).strip():
            raise ValueError("/api/recent-activity requires entity")

    base = os.getenv("HF_ENDPOINT", "https://huggingface.co").rstrip("/")
    q = urlencode(params or {}, doseq=True)
    url = f"{base}{ep}" + (f"?{q}" if q else "")

    headers = {"Accept": "application/json"}
    token = _load_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    data = None
    if method_u == "POST":
        headers["Content-Type"] = "application/json"
        data = json.dumps(json_body or {}).encode("utf-8")

    req = Request(url, method=method_u, headers=headers, data=data)
    try:
        with urlopen(req, timeout=timeout_sec) as res:
            payload = _json_best_effort(res.read())
            return {
                "ok": True,
                "status": int(res.status),
                "url": url,
                "data": payload,
                "error": None,
            }
    except HTTPError as e:
        payload = _json_best_effort(e.read())
        err = (
            payload
            if isinstance(payload, str)
            else json.dumps(payload, ensure_ascii=False)[:1000]
        )
        return {
            "ok": False,
            "status": int(e.code),
            "url": url,
            "data": payload,
            "error": err,
        }
    except URLError as e:
        return {
            "ok": False,
            "status": 0,
            "url": url,
            "data": None,
            "error": f"Network error: {e}",
        }
