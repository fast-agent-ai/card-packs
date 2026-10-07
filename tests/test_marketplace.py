import importlib.util
import json
import re
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
MARKETPLACE = json.loads((ROOT / "marketplace.json").read_text(encoding="utf-8"))
PLUGINS = sorted(path.parent for path in ROOT.glob("plugins/*/plugin.yaml"))


def _load_script():
    spec = importlib.util.spec_from_file_location("sync_marketplace", ROOT / "scripts" / "sync_marketplace.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _has_fast_agent() -> bool:
    return importlib.util.find_spec("fast_agent") is not None


class MarketplaceMetadataTests(unittest.TestCase):
    def test_marketplace_matches_plugin_contents(self):
        sync = _load_script()
        current = (ROOT / "marketplace.json").read_text(encoding="utf-8")
        self.assertEqual(
            sync.render(sync.synced(json.loads(current))),
            current,
            "marketplace.json is stale; run: python scripts/sync_marketplace.py",
        )

    def test_plugins_declare_fast_agent_floor(self):
        for plugin in PLUGINS:
            with self.subTest(plugin=plugin.name):
                manifest = yaml.safe_load((plugin / "plugin.yaml").read_text(encoding="utf-8"))
                self.assertRegex(manifest.get("requires_fast_agent", ""), r"^>=\d+\.\d+\.\d+$")


@unittest.skipUnless(_has_fast_agent(), "fast-agent is not installed")
class InstalledFastAgentCompatibilityTests(unittest.TestCase):
    """Contract checks against whichever fast-agent is installed (CI: latest release)."""

    def test_marketplace_parses(self):
        from fast_agent.plugins.marketplace import parse_marketplace_plugins

        parsed = {plugin.name for plugin in parse_marketplace_plugins(MARKETPLACE)}
        self.assertEqual({entry["name"] for entry in MARKETPLACE["command_plugins"]}, parsed)

    def test_plugin_handler_modules_import(self):
        for plugin in PLUGINS:
            manifest = yaml.safe_load((plugin / "plugin.yaml").read_text(encoding="utf-8"))
            handlers = [spec["handler"] for spec in manifest.get("commands", {}).values()]
            handlers += list(manifest.get("hooks", {}).values())
            for module_path in sorted({re.split(r":", handler)[0] for handler in handlers}):
                with self.subTest(plugin=plugin.name, module=module_path):
                    path = (plugin / module_path).resolve()
                    name = f"_plugin_{plugin.name}_{path.stem}"
                    spec = importlib.util.spec_from_file_location(name, path)
                    module = importlib.util.module_from_spec(spec)
                    sys.modules[name] = module  # dataclasses resolve their module during exec
                    try:
                        spec.loader.exec_module(module)
                    finally:
                        sys.modules.pop(name)


if __name__ == "__main__":
    unittest.main()
