from __future__ import annotations


from ..context_types import HelperRuntimeContext


async def resolve_username_or_current(
    ctx: HelperRuntimeContext,
    username: str | None,
) -> tuple[str | None, str | None]:
    resolved = str(username or "").strip()
    if resolved:
        return resolved, None

    whoami = await ctx.call_helper("hf_whoami")
    if whoami.get("ok") is not True:
        return (
            None,
            str(whoami.get("error") or "Could not resolve current authenticated user"),
        )
    item = ctx._helper_item(whoami)
    current = item.get("username") if isinstance(item, dict) else None
    if not isinstance(current, str) or not current.strip():
        return (
            None,
            "username was not provided and current authenticated user could not be resolved",
        )
    return current.strip(), None
