"""Behavioral URL-discovery tests using a real local HTTP server."""

from __future__ import annotations

import hashlib
import importlib
import io
import json
import sys
import tarfile
import threading
import zipfile
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "plugins" / "discover"))
discover = importlib.import_module("discover")


SKILL = b"---\nname: verified\ndescription: Local verified skill\n---\n\n# Local skill\n"


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        base = f"http://{self.server.server_address[0]}:{self.server.server_address[1]}"
        if self.path == "/page":
            self._send(
                b"<link rel='ai-catalog' href='/html-catalog.json'>",
                "text/html",
                Link='</header-catalog.json>; rel="ai-catalog"',
            )
        elif self.path == "/complex-link":
            self._send(
                b"<html><body>Complex Link header</body></html>",
                "text/html",
                Link='</header-catalog.json>; title="catalog, primary"; rel="ai-catalog"',
            )
        elif self.path == "/unsafe-link":
            self._send(
                b"<html><body>Unsafe link</body></html>",
                "text/html",
                Link='<file:///tmp/catalog.json>; rel="ai-catalog"',
            )
        elif self.path == "/no-links":
            self._send(b"<html><body>No links</body></html>", "text/html")
        elif self.path == "/.well-known/ai-catalog.json":
            self._send(
                _json(
                    {
                        "specVersion": "1.0",
                        "host": {"displayName": "Well-known catalog"},
                        "entries": [],
                    }
                ),
                "application/ai-catalog+json",
            )
        elif self.path == "/header-catalog.json":
            self._send(
                _json(
                    {
                        "specVersion": "1.0",
                        "host": {"displayName": "Header catalog"},
                        "entries": [
                            {
                                "identifier": "urn:air:example.test:catalog:nested",
                                "displayName": "Nested",
                                "type": "application/ai-catalog+json",
                                "url": "/nested.json",
                            },
                            {
                                "identifier": "urn:air:example.test:mcp:demo",
                                "displayName": "MCP",
                                "type": "application/mcp-server-card+json",
                                "url": "/mcp.json",
                            },
                            {
                                "identifier": "urn:air:example.test:registry:demo",
                                "displayName": "Registry",
                                "type": "application/ai-registry+json",
                                "url": "/registry",
                            },
                            {
                                "identifier": "urn:air:example.test:catalog:inline",
                                "displayName": "Inline catalog",
                                "type": "application/ai-catalog+json",
                                "data": {
                                    "specVersion": "1.0",
                                    "entries": [
                                        {
                                            "identifier": "urn:air:example.test:mcp:inline",
                                            "displayName": "Inline MCP",
                                            "type": "application/mcp-server-card+json",
                                            "data": {
                                                "name": "inline",
                                                "remotes": [{"url": f"{base}/mcp"}],
                                            },
                                        }
                                    ],
                                },
                            },
                        ],
                    }
                ),
                "application/ai-catalog+json",
            )
        elif self.path == "/html-catalog.json":
            self._send(
                _json({"specVersion": "1.0", "entries": []}),
                "application/ai-catalog+json",
            )
        elif self.path == "/nested.json":
            self._send(
                _json(
                    {
                        "specVersion": "1.0",
                        "host": {"displayName": "Nested catalog"},
                        "entries": [
                            {
                                "identifier": "urn:air:example.test:mcp:nested",
                                "displayName": "Nested MCP",
                                "type": "application/mcp-server-card+json",
                                "data": {"url": f"{base}/mcp"},
                            }
                        ],
                    }
                ),
                "application/ai-catalog+json",
            )
        elif self.path == "/mcp.json":
            self._send(
                _json({"remotes": [{"url": f"{base}/mcp"}]}), "application/mcp-server-card+json"
            )
        elif self.path == "/.well-known/agent-skills/index.json":
            self.send_response(302)
            self.send_header("Location", "/agent-index.json")
            self.end_headers()
        elif self.path == "/agent-index.json":
            self._send(
                _json(
                    {
                        "$schema": discover.AGENT_SKILLS_SCHEMA_URI,
                        "skills": [
                            {
                                "name": "verified",
                                "type": "skill-md",
                                "description": "Local verified skill",
                                "url": "skill/SKILL.md",
                                "digest": f"sha256:{hashlib.sha256(SKILL).hexdigest()}",
                            }
                        ],
                    }
                ),
                "application/json",
            )
        elif self.path == "/skill/SKILL.md":
            self._send(SKILL, "text/markdown")
        elif self.path == "/direct/SKILL.md":
            self._send(SKILL, "text/markdown")
        elif self.path == "/skill.zip":
            self._send(_zip_skill(), "application/agent-skills+zip")
        else:
            self.send_error(404)

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_HEAD(self) -> None:
        content_types = {
            "/.well-known/agent-skills/index.json": "application/json",
            "/agent-index.json": "application/json",
            "/skill/SKILL.md": "text/markdown",
            "/direct/SKILL.md": "text/markdown",
        }
        content_type = content_types.get(self.path)
        if content_type is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.end_headers()

    def _send(self, body: bytes, content_type: str, **headers: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def local_url() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_parser_keeps_ard_mode_and_accepts_url_mode() -> None:
    ard = discover._parse_discover_arguments("--federation none useful tools")
    url = discover._parse_discover_arguments("https://example.test/catalog.json weather tools")

    assert ard.url is None
    assert ard.query == "useful tools"
    assert url.url == "https://example.test/catalog.json"
    assert url.query == "weather tools"


def test_page_discovers_header_catalog_before_html_and_agent_skills(local_url: str) -> None:
    sources = discover._discover_url_sources(f"{local_url}/page")

    assert [source.title for source in sources] == ["Header catalog", "Agent Skills"]
    assert sources[0].method == "Link header"
    assert sources[0].document_url == f"{local_url}/header-catalog.json"
    assert sources[1].kind == "Agent Skills v0.2"
    skill = sources[1].items[0]
    assert skill.url == f"{local_url}/skill/SKILL.md"
    assert skill.digest == f"sha256:{hashlib.sha256(SKILL).hexdigest()}"


def test_link_header_allows_quoted_commas(local_url: str) -> None:
    sources = discover._discover_url_sources(f"{local_url}/complex-link")

    assert sources[0].title == "Header catalog"
    assert sources[0].method == "Link header"


def test_nested_catalog_entries_and_grouped_markdown(local_url: str) -> None:
    source = discover._discover_url_sources(f"{local_url}/page")[0]
    nested = next(item for item in source.items if item.kind == "catalog")
    nested_source = discover._catalog_source(discover._fetch_document(nested.url or ""), nested)
    inline = next(item for item in source.items if item.name == "Inline catalog")
    inline_source = discover._catalog_source_from_payload(
        inline.data,
        document_url=f"{inline.document_url}#{inline.name}",
        parent=inline,
    )
    markdown = discover._render_sources_markdown(f"{local_url}/page", [source])

    assert nested_source.title == "Nested catalog"
    assert nested_source.items[0].kind == "mcp"
    assert inline_source.items[0].name == "Inline MCP"
    assert "## Header catalog" in markdown
    assert "Provenance: **AI Catalog** via **Link header**" in markdown
    assert f"<{local_url}/header-catalog.json>" in markdown


def test_well_known_catalog_and_direct_skill_are_classified(local_url: str) -> None:
    sources = discover._discover_url_sources(f"{local_url}/no-links")
    direct = discover._discover_url_sources(f"{local_url}/direct/SKILL.md")

    assert [source.title for source in sources] == ["Well-known catalog", "Agent Skills"]
    assert sources[0].method == "well-known"
    assert direct[0].kind == "Direct skill"
    assert direct[0].items[0].name == "verified"


def test_authoritative_schema_versions_are_required() -> None:
    wrong_catalog = discover.FetchedDocument(
        "https://example.test/catalog.json",
        "application/ai-catalog+json",
        "",
        _json({"version": "1.0", "entries": []}),
    )
    wrong_skills = discover.FetchedDocument(
        "https://example.test/.well-known/agent-skills/index.json",
        "application/json",
        "",
        _json({"$schema": "https://example.test/unknown", "skills": []}),
    )

    with pytest.raises(RuntimeError, match="specVersion"):
        discover._catalog_source(wrong_catalog, None)
    with pytest.raises(RuntimeError, match="must declare"):
        discover._agent_skills_source(wrong_skills, method="direct")


def test_discovered_urls_reject_non_http_schemes_and_direct_registry_needs_no_get(
    local_url: str,
) -> None:
    with pytest.raises(RuntimeError, match="HTTP or HTTPS"):
        discover._discover_url_sources(f"{local_url}/unsafe-link")

    sources = discover._discover_url_sources(f"{local_url}/registry/search")
    assert sources[0].kind == "ARD registry"
    assert sources[0].items[0].kind == "registry"

    referral = discover._parse_results(
        [
            {
                "identifier": "private",
                "displayName": "Private referral",
                "type": "application/ai-registry+json",
                "url": "http://169.254.169.254/registry",
            }
        ],
        start_index=1,
    )[0]
    with pytest.raises(RuntimeError, match="private network"):
        discover._registry_result_search_url(
            referral,
            source_url="https://registry.example/search",
        )
    with pytest.raises(RuntimeError, match="different private host"):
        discover._resolved_discovery_url(
            "http://169.254.169.254/catalog.json",
            f"{local_url}/page",
        )


def test_agent_skill_digest_is_verified_before_install(local_url: str, tmp_path: Path) -> None:
    item = discover._discover_url_sources(f"{local_url}/page")[1].items[0]
    skill_dir = discover._install_discovery_skill(item, tmp_path)

    assert (skill_dir / "SKILL.md").read_bytes() == SKILL
    with pytest.raises(RuntimeError, match="SHA-256"):
        discover._install_discovery_skill(
            replace(item, name="bad", digest="sha256:" + "0" * 64), tmp_path
        )


def test_zip_and_tar_skill_archives_require_safe_root_skill(local_url: str, tmp_path: Path) -> None:
    zip_bytes = _zip_skill()
    item = discover.DiscoveryItem(
        "verified",
        "",
        discover.AGENT_SKILLS_ZIP_MEDIA_TYPE,
        f"{local_url}/skill.zip",
        None,
        f"sha256:{hashlib.sha256(zip_bytes).hexdigest()}",
        "Agent Skills v0.2",
        "well-known",
        f"{local_url}/agent-index.json",
    )
    installed = discover._install_discovery_skill(item, tmp_path)

    assert (installed / "SKILL.md").is_file()
    safe_tar = io.BytesIO()
    with tarfile.open(fileobj=safe_tar, mode="w:gz") as archive:
        info = tarfile.TarInfo("SKILL.md")
        archive_skill = SKILL.replace(b"name: verified", b"name: archive")
        info.size = len(archive_skill)
        archive.addfile(info, io.BytesIO(archive_skill))
    discover._extract_skill_archive(safe_tar.getvalue(), tmp_path / "tar")
    assert (tmp_path / "tar" / "SKILL.md").is_file()

    unsafe = io.BytesIO()
    with zipfile.ZipFile(unsafe, "w") as archive:
        archive.writestr("../SKILL.md", SKILL)
    with pytest.raises(RuntimeError, match="Unsafe path"):
        discover._extract_skill_archive(unsafe.getvalue(), tmp_path / "unsafe")


def _json(value: object) -> bytes:
    return json.dumps(value).encode()


def _zip_skill() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("SKILL.md", SKILL)
    return output.getvalue()
