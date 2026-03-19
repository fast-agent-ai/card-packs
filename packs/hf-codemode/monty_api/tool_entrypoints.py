#!/usr/bin/env python3
"""File-based function tool entrypoints for the production Monty runtime."""

from __future__ import annotations

import sys
from pathlib import Path

_PACKAGE_DIR = Path(__file__).resolve().parent
_ROOT_DIR = _PACKAGE_DIR.parent
for candidate in (_ROOT_DIR, _PACKAGE_DIR):
    candidate_str = str(candidate)
    if candidate_str not in sys.path:
        sys.path.insert(0, candidate_str)

from monty_api import (  # noqa: E402
    HELPER_EXTERNALS,
    hf_hub_query as _hf_hub_query,
    hf_hub_query_raw as _hf_hub_query_raw,
    main,
)


async def hf_hub_query(*args, **kwargs):
    return await _hf_hub_query(*args, **kwargs)


async def hf_hub_query_raw(*args, **kwargs):
    return await _hf_hub_query_raw(*args, **kwargs)

__all__ = [
    "HELPER_EXTERNALS",
    "hf_hub_query",
    "hf_hub_query_raw",
    "main",
]

if __name__ == "__main__":
    raise SystemExit(main())
