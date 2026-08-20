"""Run a local URL-discovery fixture for the discover plugin."""

from __future__ import annotations

import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SKILL = b"---\nname: demo-skill\ndescription: Local demo skill\n---\n\n# Demo skill\n"


class DemoHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        base = f"http://{self.server.server_address[0]}:{self.server.server_address[1]}"
        routes = {
            "/page": (
                "text/html",
                b"<html><head><link rel='ai-catalog' href='/catalog.json'></head><body>demo</body></html>",
                {"Link": '</catalog.json>; rel="ai-catalog"'},
            ),
            "/catalog.json": (
                "application/ai-catalog+json",
                _json(
                    {
                        "specVersion": "1.0",
                        "host": {"displayName": "Demo catalog"},
                        "entries": [
                            {
                                "identifier": "urn:air:localhost:catalog:nested",
                                "displayName": "Nested catalog",
                                "type": "application/ai-catalog+json",
                                "url": "/nested.json",
                            },
                            {
                                "identifier": "urn:air:localhost:mcp:demo",
                                "displayName": "Demo MCP",
                                "type": "application/mcp-server-card+json",
                                "url": "/mcp.json",
                            },
                            {
                                "identifier": "urn:air:localhost:registry:demo",
                                "displayName": "Demo registry",
                                "type": "application/ai-registry+json",
                                "url": "/registry",
                            },
                        ],
                    }
                ),
                {},
            ),
            "/nested.json": (
                "application/ai-catalog+json",
                _json(
                    {
                        "specVersion": "1.0",
                        "host": {"displayName": "Nested demo catalog"},
                        "entries": [
                            {
                                "identifier": "urn:air:localhost:skill:nested",
                                "displayName": "Nested skill",
                                "type": "application/agent-skills+md",
                                "url": "/skill/SKILL.md",
                            }
                        ],
                    }
                ),
                {},
            ),
            "/mcp.json": (
                "application/mcp-server-card+json",
                _json({"name": "demo", "remotes": [{"url": f"{base}/mcp"}]}),
                {},
            ),
            "/.well-known/agent-skills/index.json": (
                "application/json",
                _json(
                    {
                        "$schema": "https://schemas.agentskills.io/discovery/0.2.0/schema.json",
                        "skills": [
                            {
                                "name": "demo-skill",
                                "type": "skill-md",
                                "description": "Digest-verified local skill",
                                "url": "/skill/SKILL.md",
                                "digest": f"sha256:{hashlib.sha256(SKILL).hexdigest()}",
                            }
                        ],
                    }
                ),
                {},
            ),
            "/skill/SKILL.md": ("text/markdown", SKILL, {}),
        }
        route = routes.get(self.path)
        if route is None:
            self.send_error(404)
            return
        content_type, body, headers = route
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self) -> None:
        content_types = {
            "/.well-known/agent-skills/index.json": "application/json",
            "/skill/SKILL.md": "text/markdown",
        }
        content_type = content_types.get(self.path)
        if content_type is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.end_headers()

    def do_POST(self) -> None:
        if self.path != "/registry/search":
            self.send_error(404)
            return
        body = _json({"results": [], "referrals": []})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def _json(value: object) -> bytes:
    return json.dumps(value).encode()


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), DemoHandler)
    host, port = server.server_address
    print(f"Run: /discover http://{host}:{port}/page", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
