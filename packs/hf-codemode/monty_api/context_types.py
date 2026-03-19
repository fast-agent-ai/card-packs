from __future__ import annotations

from typing import Any, Protocol


class HelperRuntimeContext(Protocol):
    """Typed helper-facing runtime context interface."""

    helper_registry: dict[str, Any]
    call_count: dict[str, int]
    trace: list[dict[str, Any]]
    limit_summaries: list[dict[str, Any]]
    latest_helper_error_box: dict[str, dict[str, Any] | None]
    internal_helper_used: dict[str, bool]

    async def call_helper(
        self, helper_name: str, /, *args: Any, **kwargs: Any
    ) -> Any: ...

    def __getattr__(self, name: str) -> Any: ...
