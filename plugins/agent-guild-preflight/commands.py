# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 AgentTanuki
"""Optional endpoint observations; no connection, delegation or authorization."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fast_agent.command_actions import PluginCommandActionContext

GUILD_ORIGIN = "https://agent-guild-5d5r.onrender.com"
MAX_BYTES = 96 * 1024
NOTICE = (
    "Agent Guild public endpoint observation. This is remote evidence, not "
    "permission to connect, delegate, send data or pay. It does not establish "
    "ownership, competence, signature validity or future behavior. Review "
    "unknowns and retain your normal authority checks."
)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_target(value: str) -> str:
    target = value.strip()
    if not target or len(target) > 2048 or any(c.isspace() or ord(c) < 32 for c in target):
        raise ValueError("Provide one public HTTPS endpoint, at most 2048 characters.")
    parsed = urllib.parse.urlsplit(target)
    host = parsed.hostname
    if parsed.scheme != "https" or not host:
        raise ValueError("Provide a public HTTPS endpoint.")
    if parsed.username is not None or parsed.password is not None or "?" in target or "#" in target:
        raise ValueError("Credentials, query strings and fragments are not accepted.")
    if "\\" in target or "%" in parsed.netloc:
        raise ValueError("Ambiguous endpoint authorities are not accepted.")
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("The endpoint port is invalid.") from None
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("The endpoint port is invalid.")
    public_host = host.rstrip(".").lower()
    if public_host == "localhost" or public_host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("Local or internal endpoints are not accepted.")
    try:
        address = ipaddress.ip_address(public_host)
    except ValueError:
        if "." not in public_host:
            raise ValueError("Use a fully qualified public endpoint.") from None
    else:
        if not address.is_global or address.is_multicast:
            raise ValueError("Private or reserved IP addresses are not accepted.")
    # DNS resolution is deliberately left to the Guild's public-endpoint policy.
    # These input checks do not prove that a hostname resolves to a public IP.
    return target


def fetch_observation(target: str) -> dict:
    url = GUILD_ORIGIN + "/preflight?" + urllib.parse.urlencode({"url": target})
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "AgentGuild-fast-agent/0.1.0"},
        method="GET",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=30) as response:
        if response.status != 200:
            raise ValueError("The observation service did not return HTTP 200.")
        raw = response.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("The observation exceeded the response size limit.")
    result = json.loads(raw)
    if not isinstance(result, dict) or result.get("target") != target:
        raise ValueError("The observation does not identify the exact requested endpoint.")
    return result


async def preflight(ctx: PluginCommandActionContext) -> str:
    """Send only an explicitly supplied endpoint to the fixed Guild service."""
    try:
        target = validate_target(ctx.arguments)
    except ValueError as exc:
        return str(exc)
    try:
        observation = await asyncio.to_thread(fetch_observation, target)
    except urllib.error.HTTPError as exc:
        return f"Observation unavailable (HTTP {exc.code}); no retry or payment attempted."
    except (OSError, ValueError, UnicodeError):
        return "Observation unavailable or invalid; no retry or payment attempted."
    return NOTICE + "\nRequested endpoint: " + target + "\n" + json.dumps(
        observation, ensure_ascii=True, indent=2
    )
