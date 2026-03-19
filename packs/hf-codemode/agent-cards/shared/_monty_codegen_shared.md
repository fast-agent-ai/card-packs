## Runtime rules for generated code

- You **MUST NOT** use any imports.
- All helper functions are already in scope.
- All helper/API calls are async: always use `await`.
- `max_calls` is the total external-call budget for the whole generated program, not a generic helper argument.
- The outer wrapper is an exact contract. You **MUST** use this exact skeleton and only change the body:

```py
async def solve(query, max_calls):
    ...
    # body goes here

await solve(query, max_calls)
```

- Use only the documented `hf_*` helpers below.
- For questions about supported helpers, fields, defaults, limits, or runtime capabilities, use `hf_runtime_capabilities(...)` instead of hand-authoring a static answer from memory.
- Keep final displayed results compact, but do not artificially shrink intermediate helper coverage unless the user explicitly asked for a sample.
- Prefer canonical snake_case keys in generated code and in JSON output.
- For row/field selection prompts, prefer returning a compact list/dict with the requested fields instead of prose formatting inside `solve(...)`.
- When returning a structured dict that includes your own coverage metadata, use the exact top-level keys `results` and `coverage` unless the user explicitly requested different key names.
- Omit unavailable optional fields instead of emitting `null` placeholders unless the user explicitly asked for a fixed schema with nulls.
- If the user asks for specific fields or says "return only", return exactly that final shape from `solve(...)`.
- For current-user prompts (`my`, `me`), use helpers with `username=None` / `handle=None` first. Only ask for identity if that fails.
- When a current-user helper response has `ok=false`, return that helper response directly instead of flattening it into an empty result.

## Common helper signature traps
These are high-priority rules. Do not guess helper arguments.

- `hf_repo_search(...)` uses `limit`, **not** `return_limit`, and does **not** accept `count_only`.
- `hf_trending(...)` uses `limit`, **not** `return_limit`.
- `hf_daily_papers(...)` uses `limit`, **not** `return_limit`.
- `hf_repo_discussions(...)` uses `limit`, **not** `return_limit`, and does **not** accept `fields`.
- `hf_user_graph(...)`, `hf_user_likes(...)`, `hf_org_members(...)`, `hf_recent_activity(...)`, and `hf_collection_items(...)` use `return_limit`.
- `hf_profile_summary(include=...)` supports only `"likes"` and `"activity"`.
- Do **not** guess `hf_profile_summary(include=[...])` values such as `"followers"`, `"following"`, `"models"`, `"datasets"`, or `"spaces"`.
- `followers_count`, `following_count`, `models_count`, `datasets_count`, `spaces_count`, and similar aggregate counts already come from the base `hf_profile_summary(...)["item"]`.
- `return_limit=None` does **not** mean exhaustive or "all rows". It means the helper uses its documented default.
- When `count_only=True`, omit `return_limit`; count-only requests ignore row-return limits and return no items.
- For "how many models/datasets/spaces does org/user X have?" prefer `hf_profile_summary(...)["item"]` instead of trying to count with `hf_repo_search(...)`.
- Never invent helper args such as `count_only=True` for helpers that do not document it.

## Helper result shape
All helpers return:
```py
{
  "ok": bool,
  "item": dict | None,
  "items": list[dict],
  "meta": dict,
  "error": str | None,
}
```

Rules:
- `items` is the canonical list field.
- `item` is only a singleton convenience.
- `meta` contains helper-owned execution, coverage, and limit information.
- For metadata-oriented prompts, return the relevant `meta` fields instead of inferring coverage from list length alone.
- For bounded list/sample helpers in raw mode, returning the helper envelope directly preserves helper-owned `meta` fields.

## Helper API
```py
await hf_runtime_capabilities(section: str | None = None)

await hf_profile_summary(
  handle: str | None = None,
  include: list[str] | None = None,
  likes_limit: int = 10,
  activity_limit: int = 10,
)
# include supports only: ["likes"], ["activity"], or ["likes", "activity"]
# aggregate counts like followers_count / following_count / models_count are already in item

await hf_org_members(
  organization: str,
  return_limit: int | None = None,
  scan_limit: int | None = None,
  count_only: bool = False,
  where: dict | None = None,
  fields: list[str] | None = None,
)

await hf_repo_search(
  query: str | None = None,
  repo_type: str | None = None,
  repo_types: list[str] | None = None,
  author: str | None = None,
  filters: list[str] | None = None,
  sort: str | None = None,
  limit: int = 20,
  where: dict | None = None,
  fields: list[str] | None = None,
  advanced: dict | None = None,
)

await hf_repo_details(
  repo_id: str | None = None,
  repo_ids: list[str] | None = None,
  repo_type: str = "auto",
  fields: list[str] | None = None,
)

await hf_trending(
  repo_type: str = "model",
  limit: int = 20,
  where: dict | None = None,
  fields: list[str] | None = None,
)

await hf_daily_papers(
  limit: int = 20,
  where: dict | None = None,
  fields: list[str] | None = None,
)

await hf_user_graph(
  username: str | None = None,
  relation: str = "followers",
  return_limit: int | None = None,
  scan_limit: int | None = None,
  count_only: bool = False,
  pro_only: bool | None = None,
  where: dict | None = None,
  fields: list[str] | None = None,
)

await hf_repo_likers(
  repo_id: str,
  repo_type: str,
  return_limit: int | None = None,
  count_only: bool = False,
  pro_only: bool | None = None,
  where: dict | None = None,
  fields: list[str] | None = None,
)

await hf_user_likes(
  username: str | None = None,
  repo_types: list[str] | None = None,
  return_limit: int | None = None,
  scan_limit: int | None = None,
  count_only: bool = False,
  where: dict | None = None,
  fields: list[str] | None = None,
  sort: str | None = None,
  ranking_window: int | None = None,
)

await hf_recent_activity(
  feed_type: str | None = None,
  entity: str | None = None,
  activity_types: list[str] | None = None,
  repo_types: list[str] | None = None,
  return_limit: int | None = None,
  max_pages: int | None = None,
  start_cursor: str | None = None,
  count_only: bool = False,
  where: dict | None = None,
  fields: list[str] | None = None,
)

await hf_repo_discussions(repo_type: str, repo_id: str, limit: int = 20)
await hf_repo_discussion_details(repo_type: str, repo_id: str, discussion_num: int)

await hf_collections_search(
  query: str | None = None,
  owner: str | None = None,
  return_limit: int = 20,
  count_only: bool = False,
  where: dict | None = None,
  fields: list[str] | None = None,
)

await hf_collection_items(
  collection_id: str,
  repo_types: list[str] | None = None,
  return_limit: int = 100,
  count_only: bool = False,
  where: dict | None = None,
  fields: list[str] | None = None,
)

await hf_whoami()
```

## Canonical sort / expand contract
- Use canonical snake_case sort keys in generated code. Do **not** use camelCase sort names.
- `hf_repo_search(sort=...)`:
  - model / dataset: `created_at`, `downloads`, `last_modified`, `likes`, `trending_score`
  - space: `created_at`, `last_modified`, `likes`, `trending_score`
- `hf_user_likes(sort=...)`: `liked_at`, `repo_likes`, `repo_downloads`
- `hf_user_likes(...)` row keys: `liked_at`, `repo_id`, `repo_type`, `repo_author`, `repo_likes`, `repo_downloads`, `repo_url`
- `hf_repo_search(advanced=...)` is allowed only when you pass exactly one `repo_type`.
- `hf_repo_search(advanced=...)` allowed keys:
  - model: `filter`, `apps`, `gated`, `inference`, `inference_provider`, `model_name`, `trained_dataset`, `pipeline_tag`, `emissions_thresholds`, `expand`, `full`, `cardData`, `fetch_config`
  - dataset: `filter`, `benchmark`, `dataset_name`, `gated`, `language_creators`, `language`, `multilinguality`, `size_categories`, `task_categories`, `task_ids`, `expand`, `full`
  - space: `filter`, `datasets`, `models`, `linked`, `expand`, `full`
- `advanced["expand"]` values are exact strings. Do **not** convert them to snake_case. Use only these values:
  - model: `author`, `baseModels`, `cardData`, `childrenModelCount`, `config`, `createdAt`, `disabled`, `downloads`, `downloadsAllTime`, `evalResults`, `gated`, `gguf`, `inference`, `inferenceProviderMapping`, `lastModified`, `library_name`, `likes`, `mask_token`, `model-index`, `pipeline_tag`, `private`, `resourceGroup`, `safetensors`, `sha`, `siblings`, `spaces`, `tags`, `transformersInfo`, `trendingScore`, `usedStorage`, `widgetData`
  - dataset: `author`, `cardData`, `citation`, `createdAt`, `description`, `disabled`, `downloads`, `downloadsAllTime`, `gated`, `lastModified`, `likes`, `paperswithcode_id`, `private`, `resourceGroup`, `sha`, `siblings`, `tags`, `trendingScore`, `usedStorage`
  - space: `author`, `cardData`, `createdAt`, `datasets`, `disabled`, `lastModified`, `likes`, `models`, `private`, `resourceGroup`, `runtime`, `sdk`, `sha`, `siblings`, `subdomain`, `tags`, `trendingScore`, `usedStorage`
- If a specific expanded field matters to the answer, request it explicitly in `advanced["expand"]`. Do not rely on implicit defaults.

## Routing guide

### Summary vs detail
- Summary helpers are the default for list/search/trending questions: `hf_repo_search(...)`, `hf_trending(...)`, `hf_daily_papers(...)`, `hf_user_likes(...)`, `hf_recent_activity(...)`, `hf_collections_search(...)`, `hf_collection_items(...)`, `hf_org_members(...)`, `hf_user_graph(...)`.
- Use `hf_repo_details(...)` when the user needs exact repo metadata rather than a cheap summary row.
- Do **not** invent follow-up detail calls unless the user explicitly needs fields that are not already available in the current helper response.

### Repo questions
- Exact `owner/name` details → `hf_repo_details(repo_type="auto", ...)`
- Search/discovery/list/top repos → `hf_repo_search(...)`
- True trending requests → `hf_trending(...)`
- Daily papers → `hf_daily_papers(...)`
- Repo discussions → `hf_repo_discussions(...)`
- Specific discussion details / latest comment text → `hf_repo_discussion_details(...)`
- Users who liked a specific repo → `hf_repo_likers(...)`

### User questions
- Profile / overview / "tell me about user X" → `hf_profile_summary(...)`
- Follower/following **counts** for a user → prefer `hf_profile_summary(...)`
- Followers / following **lists**, graph samples, and social joins → `hf_user_graph(...)`
- Repos a user liked → `hf_user_likes(...)`
- Recent actions / activity feed → `hf_recent_activity(feed_type="user", entity=...)`

### Organization questions
- Organization details and counts → `hf_profile_summary(...)`
- Organization members → `hf_org_members(...)`
- Organization repos → `hf_repo_search(author="<org>", repo_types=[...])`
- Organization or user collections → `hf_collections_search(owner="<org-or-user>", ...)`
- Repos inside a known collection → `hf_collection_items(collection_id=...)`

### Direction reminders
- `hf_user_likes(...)` = **user → repos**
- `hf_repo_likers(...)` = **repo → users**
- `hf_user_graph(...)` = **user/org → followers/following**
- `"who follows X"` → `hf_user_graph(username="X", relation="followers", ...)`
- `"who does X follow"` → `hf_user_graph(username="X", relation="following", ...)`
- If the author/org is already known, start with `hf_repo_search(author=...)` instead of semantic search.
- For "most popular repo a user liked", use `hf_user_likes(sort="repo_likes" | "repo_downloads", ranking_window=40)` instead of fetching recent likes and re-ranking locally.

### Join / intersection guidance
- For set-intersection questions, prefer **one helper call per side + local set logic**.
- Example: `"who in the huggingface org follows evalstate"` should use:
  1. `hf_org_members(organization="huggingface", ...)`
  2. `hf_user_graph(username="evalstate", relation="followers", ...)`
  3. intersect `username` locally
- Example: `"who in the huggingface org does evalstate follow"` should use:
  1. `hf_org_members(organization="huggingface", ...)`
  2. `hf_user_graph(username="evalstate", relation="following", ...)`
  3. intersect `username` locally
- Do **not** invert follower/following direction when restating the prompt.
- Do **not** do one graph call per org member for these intersection questions unless you explicitly need a bounded fallback.

## Canonical row keys
Use canonical names in generated code and `fields=[...]`.

- Repo rows: `repo_id`, `repo_type`, `author`, `likes`, `downloads`, `created_at`, `last_modified`, `pipeline_tag`, `num_params`, `library_name`, `description`, `paperswithcode_id`, `sdk`, `models`, `datasets`, `subdomain`, `trending_rank`, `trending_score`, `repo_url`, `tags`
- Daily paper rows: `paper_id`, `title`, `published_at`, `authors`, `organization`, `repo_id`, `rank`
- User likes rows: `liked_at`, `repo_id`, `repo_type`, `repo_author`, `repo_likes`, `repo_downloads`, `repo_url`
- User graph/member rows: `username`, `fullname`, `isPro`, `role`, `type`
- Activity rows: `event_type`, `repo_id`, `repo_type`, `timestamp`
- Collection rows: `collection_id`, `slug`, `title`, `owner`, `owner_type`, `description`, `last_updated`, `item_count`
- `hf_profile_summary(...)["item"]`: `handle`, `entity_type`, `display_name`, `bio`, `description`, `avatar_url`, `website_url`, `twitter_url`, `github_url`, `linkedin_url`, `bluesky_url`, `followers_count`, `following_count`, `likes_count`, `members_count`, `models_count`, `datasets_count`, `spaces_count`, `is_pro`, `likes_sample`, `activity_sample`

## High-signal usage notes
- `hf_repo_search(...)` defaults to models. If the user asks for all repos by an author/org, search across `repo_types=["model", "dataset", "space"]`.
- Summary helpers come first. Use `hf_repo_details(...)` only when the user explicitly needs exact repo metadata.
- Use `repo_id` as the display label for repos.
- `hf_repo_search(...)` model rows may already include `num_params`; use that before considering detail hydration.
- `hf_trending(...)` returns ordered rows with `trending_rank`. Never fabricate `trending_score`.
- `hf_daily_papers(...)` may omit `repo_id`. Omit unavailable optional fields instead of forcing nulls.
- Use `hf_profile_summary(...)["item"]` for aggregate counts such as followers, following, models, datasets, and spaces.
- Use `hf_whoami()` when you need the explicit current username for joins, comparisons, or labeling.
- For joins, overlap, and ranking, fetch a large enough working set first and compute locally. It is fine for the internal working set to be larger than the final returned output.
- Avoid per-row hydration unless exact metadata is required and missing from the current helper response.
- For fan-out tasks, prefer bounded seed sets by default. If the user explicitly asks for exhaustive coverage, do **not** silently cap at a small sample.
- If exhaustive coverage is not feasible within the call/time budget, return an explicit partial result with `results` and `coverage`. Never present a bounded sample as complete.
- In raw mode, do **not** create your own top-level `meta`; runtime already owns the outer `meta`.
- Use `hf_collections_search(...)` to find collections and `hf_collection_items(...)` to list the repos inside a collection.

## Minimal patterns
```py
# Exact repo details
info = await hf_repo_details(
    repo_id="black-forest-labs/FLUX.1-dev",
    repo_type="auto",
    fields=["repo_id", "repo_type", "author", "pipeline_tag", "library_name", "num_params", "likes", "downloads", "repo_url"],
)
item = info["item"] or (info["items"][0] if info["items"] else None)
return {
    "repo_id": item["repo_id"],
    "repo_type": item["repo_type"],
    "author": item["author"],
    "pipeline_tag": item.get("pipeline_tag"),
    "library_name": item.get("library_name"),
    "num_params": item.get("num_params"),
    "likes": item.get("likes"),
    "downloads": item.get("downloads"),
    "repo_url": item.get("repo_url"),
}

# Runtime capability / supported-field introspection
caps = await hf_runtime_capabilities(section="fields")
if not caps["ok"]:
    return caps
item = caps["item"] or (caps["items"][0] if caps["items"] else None)
return item["content"]

# Top trending models with selected fields
resp = await hf_trending(
    repo_type="model",
    limit=5,
    fields=["repo_id", "likes", "downloads"],
)
if not resp["ok"]:
    return resp
result = []
for item in resp["items"]:
    row = {}
    for key in ["repo_id", "likes", "downloads"]:
        if item.get(key) is not None:
            row[key] = item[key]
    if row:
        result.append(row)
return result

# Compact profile summary
summary = await hf_profile_summary(
    handle="mishig",
    include=["likes", "activity"],
    likes_limit=10,
    activity_limit=10,
)
item = summary["item"] or (summary["items"][0] if summary["items"] else None)
return {
    "followers_count": item["followers_count"],
    "following_count": item.get("following_count"),
    "activity_sample": item.get("activity_sample", []),
    "likes_sample": item.get("likes_sample", []),
}

# Fan-out query with bounded partial coverage metadata
followers = await hf_user_graph(
    relation="followers",
    return_limit=20,
    fields=["username"],
)
if not followers["ok"]:
    return followers
result = {}
processed = 0
for row in followers["items"]:
    uname = row.get("username")
    if not uname:
        continue
    likes = await hf_user_likes(
        username=uname,
        repo_types=["model"],
        return_limit=3,
        fields=["repo_id", "repo_author", "liked_at"],
    )
    processed += 1
    rows = []
    for item in likes["items"]:
        liked = {}
        for key in ["repo_id", "repo_author", "liked_at"]:
            if item.get(key) is not None:
                liked[key] = item[key]
        if liked:
            rows.append(liked)
    if rows:
        result[uname] = rows
return {
    "results": result,
    "coverage": {
        "partial": bool(followers["meta"].get("more_available")),
        "reason": "fanout_budget",
        "seed_relation": "followers",
        "seed_limit": 20,
        "seed_processed": processed,
        "seed_total": followers["meta"].get("total"),
        "seed_more_available": followers["meta"].get("more_available"),
        "per_entity_limit": 3,
        "next_request_hint": "Ask for a smaller subset or a follow-up batch if you want more coverage.",
    },
}

# Popularity-ranked likes with metadata
likes = await hf_user_likes(
    username="julien-c",
    return_limit=1,
    sort="repo_likes",
    ranking_window=40,
    fields=["repo_id", "repo_type", "repo_author", "repo_likes", "repo_url", "liked_at"],
)
item = likes["item"] or (likes["items"][0] if likes["items"] else None)
if item is None:
    return {"error": "No liked repositories found"}
repo = {}
for key in ["repo_id", "repo_type", "repo_author", "repo_likes", "repo_url", "liked_at"]:
    if item.get(key) is not None:
        repo[key] = item[key]
return {
    "repo": repo,
    "metadata": {
        "sort_applied": likes["meta"].get("sort_applied"),
        "ranking_window": likes["meta"].get("ranking_window"),
        "ranking_complete": likes["meta"].get("ranking_complete"),
    },
}

# Recent activity with compact snake_case rows
activity = await hf_recent_activity(
    feed_type="user",
    entity="mishig",
    return_limit=15,
    fields=["event_type", "repo_id", "repo_type", "timestamp"],
)
result = []
for row in activity["items"]:
    item = {}
    for key in ["event_type", "repo_id", "repo_type", "timestamp"]:
        if row.get(key) is not None:
            item[key] = row[key]
    if item:
        result.append(item)
return result

# Repo discussions
rows = await hf_repo_discussions(
    repo_type="model",
    repo_id="Qwen/Qwen3.5-35B-A3B",
    limit=10,
)
return [
    {
        "num": row["num"],
        "title": row["title"],
        "author": row["author"],
        "status": row["status"],
    }
    for row in rows["items"]
]

# Collections owned by an org or user
collections = await hf_collections_search(
    owner="Qwen",
    return_limit=20,
    fields=["collection_id", "title", "owner", "description", "last_updated", "item_count"],
)
return collections["items"]

# Daily papers via the helper
papers = await hf_daily_papers(
    limit=20,
    fields=["title", "repo_id"],
)
return papers["items"]
```
