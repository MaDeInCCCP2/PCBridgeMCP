import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
from extensions import Extensions
from plugin_manager import SCHEMAS


SKILL = """---
name: example
description: Content for a fixture
allowed-tools: Bash
metadata:
  platform: fixture
---
Proceed with the documented task.
"""


class Packages(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.registry = Extensions(self.root / "bridge")
        self.source = self.root / "example"
        self.source.mkdir()
        (self.source / "SKILL.md").write_text(SKILL, encoding="utf-8")

    def test_import_enable_export_and_disable_skill(self):
        (self.source / "scripts").mkdir()
        (self.source / "scripts" / "local.py").write_text("raise RuntimeError('do not execute')", encoding="utf-8")
        record = self.registry.import_package(self.source)
        self.assertTrue(record["scripts_present"])
        self.assertEqual(record["permissions_granted"], [])
        self.assertEqual(self.registry.skills(), [])
        self.assertTrue(self.registry.import_package(self.source)["duplicate"])
        self.registry.set_enabled("skill", "example", True)
        self.assertEqual(self.registry.skills()[0]["id"], "example")
        read = self.registry.skill("example")
        self.assertEqual(read["trust"], "untrusted_external")
        self.assertIn("platform: fixture", read["content"])
        exported = Path(self.registry.export("skill", "example", self.root / "export"))
        self.assertEqual((exported / "SKILL.md").read_text(encoding="utf-8"), SKILL)
        self.registry.set_enabled("skill", "example", False)
        self.assertEqual(self.registry.skills(), [])
        with self.assertRaises(ValueError):
            self.registry.skill("example")
        self.assertTrue(self.source.exists())

    def test_reject_malformed_yaml_or_name_mismatch(self):
        for text in (SKILL.replace("name: example", "name: different"), "---\na: [\n---\n"):
            (self.source / "SKILL.md").write_text(text, encoding="utf-8")
            with self.assertRaises(ValueError):
                self.registry.import_package(self.source)

    def test_zip_traversal_and_windows_special_paths(self):
        archive = self.root / "example.zip"
        for member in ("../escape", "nested/../escape", "/escape", "C:/escape", "nested/CON.txt", "a\\escape"):
            with self.subTest(member=member):
                with zipfile.ZipFile(archive, "w") as bundle:
                    bundle.writestr("SKILL.md", SKILL)
                    entry = zipfile.ZipInfo(member)
                    entry.filename = member
                    bundle.writestr(entry, "invalid")
                with self.assertRaises(ValueError):
                    self.registry.import_package(archive)
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("example/SKILL.md", SKILL)
        self.assertEqual(self.registry.import_package(archive)["name"], "example")

    def test_containment_rejects_symlinks(self):
        external = self.root / "outside.txt"
        external.write_text("external content", encoding="utf-8")
        try:
            (self.source / "linked.txt").symlink_to(external)
        except OSError:
            if os.name != "nt":
                raise
            self.skipTest("Creating symlinks requires Windows Developer Mode or privileges")
        with self.assertRaises(ValueError):
            self.registry.import_package(self.source)

    def test_plugin_discovers_safe_components_and_reports_unsupported_extensions(self):
        package = self.root / "plugin"
        package.mkdir()
        (package / "plugin.json").write_text(json.dumps({
            "$schema": SCHEMAS + "plugin.schema.json", "name": "fixture.plugin",
            "extensions": {"com.example.client": {"hooks": True}},
        }), encoding="utf-8")
        skill = package / "skills" / "example"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(SKILL, encoding="utf-8")
        (package / "mcp.json").write_text(json.dumps({
            "$schema": SCHEMAS + "mcp.schema.json", "mcpServers": {
                "local": {"type": "stdio", "command": "fixture-executable", "env": {"EXAMPLE": "local-value"}},
                "remote": {"type": "streamable-http", "url": "https://example.test/mcp",
                           "headers": {"Authorization": "fixture-secret"}},
                "invalid": {"type": "stdio", "command": "../outside"},
            },
        }), encoding="utf-8")
        result = self.registry.import_package(package)
        self.assertEqual(result["unsupported"], ["com.example.client"])
        self.assertEqual([server["name"] for server in result["mcp_servers"]], ["local", "remote"])
        self.assertEqual(result["diagnostics"][0]["name"], "invalid")
        self.assertNotIn("fixture-secret", json.dumps(result))
        self.registry.set_enabled("plugin", "fixture.plugin", True)
        self.assertIn("Content for a fixture", self.registry.skill("fixture.plugin/example")["content"])
