"""Keep plugin version metadata in marketplace.json in sync with the plugins.

For each command plugin hosted in this repository, writes:

- ``version``: from the plugin's ``plugin.yaml``.
- ``path_oid``: the git tree id of ``repo_path``. fast-agent records the same id
  as ``installed_path_oid`` at install time, so an update check is one fetch of
  this file and a string comparison, with no git operations.
- ``requires_fast_agent``: copied from ``plugin.yaml`` when declared.

It refuses to record changed plugin contents without a version bump, and checks
that ``plugin_bundles`` only name plugins listed here.

    python scripts/sync_marketplace.py          # rewrite marketplace.json
    python scripts/sync_marketplace.py --check  # exit 1 if it is stale (CI)
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
MARKETPLACE = ROOT / "marketplace.json"
REPO_URL = "https://github.com/fast-agent-ai/card-packs"


def tree_oid(repo_path: str) -> str:
    """Git tree id of ``repo_path`` as it would be committed from the working tree."""
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}

        def git(*args: str) -> str:
            return subprocess.run(
                ["git", "-C", str(ROOT), *args],
                env=env,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

        git("add", "--all", "--", repo_path)
        return git("write-tree", f"--prefix={repo_path}/")


def synced(marketplace: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(marketplace)
    plugins = result["command_plugins"]
    for entry in plugins:
        if entry.get("repo_url") != REPO_URL:
            continue
        repo_path = entry["repo_path"]
        manifest = yaml.safe_load((ROOT / repo_path / "plugin.yaml").read_text(encoding="utf-8"))
        path_oid = tree_oid(repo_path)
        if entry.get("path_oid") not in (None, path_oid) and entry.get("version") == manifest["version"]:
            raise SystemExit(
                f"{entry['name']}: contents changed but version is still {manifest['version']}; "
                f"bump version in {repo_path}/plugin.yaml"
            )
        entry["version"] = manifest["version"]
        entry["path_oid"] = path_oid
        if requires := manifest.get("requires_fast_agent"):
            entry["requires_fast_agent"] = requires
        else:
            entry.pop("requires_fast_agent", None)

    names = {entry["name"] for entry in plugins}
    for bundle in result.get("plugin_bundles", []):
        if missing := sorted(set(bundle["plugins"]) - names):
            raise SystemExit(f"plugin bundle {bundle['name']!r} names unknown plugins: {missing}")
    return result


def render(marketplace: dict[str, Any]) -> str:
    return json.dumps(marketplace, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail instead of writing")
    args = parser.parse_args()

    current = MARKETPLACE.read_text(encoding="utf-8")
    expected = render(synced(json.loads(current)))
    if current == expected:
        return 0
    if args.check:
        print(
            "marketplace.json is stale; run: python scripts/sync_marketplace.py",
            file=sys.stderr,
        )
        return 1
    MARKETPLACE.write_text(expected, encoding="utf-8")
    print("marketplace.json updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
