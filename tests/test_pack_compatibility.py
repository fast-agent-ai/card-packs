import unittest
from pathlib import Path

import yaml


class PackCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        cls.packs = cls.root / "packs"

    def test_all_pack_manifests_use_current_schema_and_reference_existing_files(self):
        for manifest_path in sorted(self.packs.glob("*/card-pack.yaml")):
            with self.subTest(pack=manifest_path.parent.name):
                manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
                self.assertEqual(2, manifest["schema_version"])
                install = manifest.get("install", {})
                for group in ("agent_cards", "tool_cards", "files"):
                    for relative in install.get(group, []):
                        self.assertTrue(
                            (manifest_path.parent / relative).is_file(),
                            f"{manifest_path.parent.name}: missing {relative}",
                        )

    def test_developer_packs_require_price_calculator(self):
        for name in ("codex", "hf-dev", "mcp-working"):
            with self.subTest(pack=name):
                manifest = yaml.safe_load(
                    (self.packs / name / "card-pack.yaml").read_text(encoding="utf-8")
                )
                self.assertIn("price-calculator", manifest["plugins"]["required"])

    def test_agent_cards_do_not_use_removed_smart_type(self):
        for path in sorted(self.packs.glob("*/agent-cards/*.md")):
            with self.subTest(card=path.as_posix()):
                self.assertNotIn("type: smart", path.read_text(encoding="utf-8"))

    def test_pack_configs_use_mcp_servers_mapping(self):
        for path in sorted(self.packs.glob("*/fast-agent.yaml")):
            with self.subTest(config=path.as_posix()):
                config = yaml.safe_load(path.read_text(encoding="utf-8"))
                mcp = config.get("mcp", {})
                self.assertNotIn("targets", mcp)
                if mcp:
                    self.assertIsInstance(mcp.get("servers"), dict)

    def test_search_tool_cards_use_default_bash_tool(self):
        for path in sorted(self.packs.glob("*/tool-cards/*.md")):
            with self.subTest(tool_card=path.as_posix()):
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("`execute` tool", text)
                self.assertNotIn("Prefer `execute`", text)


if __name__ == "__main__":
    unittest.main()
