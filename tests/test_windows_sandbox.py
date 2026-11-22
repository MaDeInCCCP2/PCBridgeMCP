from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from test_computer_use import APP
import sys

sys.path.insert(0, str(APP / "tools"))
from windows_sandbox import ISOLATION, launch_arguments, prepare, receipt_path, start, stop


class SandboxProfile(unittest.TestCase):
    def test_defaults_disable_redirection_without_host_mounts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.wsb"
            root = ET.parse(prepare(path)).getroot()
            self.assertEqual(root.tag, "Configuration")
            self.assertEqual({name: root.findtext(name) for name in ISOLATION}, dict.fromkeys(ISOLATION, "Disable"))
            self.assertEqual(root.findtext("ProtectedClient"), "Enable")
            self.assertIsNone(root.find("MappedFolders"))
            self.assertIsNone(root.find("LogonCommand"))

    def test_modern_cli_receives_xml_and_legacy_launcher_receives_the_profile_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = prepare(Path(directory) / "test.wsb")
            command = launch_arguments(("wsb.exe", True), path)
            self.assertEqual(command[:3], ["wsb.exe", "start", "--config"])
            self.assertEqual(ET.fromstring(command[3]).findtext("Networking"), "Disable")
            self.assertEqual(launch_arguments(("WindowsSandbox.exe", False), path), ["WindowsSandbox.exe", str(path)])

    def test_modern_start_tracks_only_its_uuid_and_stop_uses_the_same_uuid(self):
        with tempfile.TemporaryDirectory() as directory, patch("windows_sandbox.subprocess.run") as run:
            path = prepare(Path(directory) / "test.wsb")
            result = start(("wsb.exe", True), path)
            id_ = result["sandbox_id"]
            self.assertEqual(run.call_args.args[0][-2:], ["--id", id_])
            self.assertTrue(receipt_path(path).is_file())
            self.assertTrue(stop(("wsb.exe", True), path)["stopped"])
            self.assertEqual(run.call_args.args[0], ["wsb.exe", "stop", "--id", id_])
            self.assertFalse(receipt_path(path).exists())
            with self.assertRaises(FileNotFoundError):
                stop(("wsb.exe", True), path)
            self.assertEqual(run.call_count, 2)

    def test_ambiguous_start_retains_receipt_and_edited_profile_cannot_stop(self):
        with tempfile.TemporaryDirectory() as directory, patch("windows_sandbox.subprocess.run") as run:
            path = prepare(Path(directory) / "test.wsb")
            run.side_effect = subprocess.TimeoutExpired("wsb", 60)
            with self.assertRaises(subprocess.TimeoutExpired):
                start(("wsb.exe", True), path)
            self.assertTrue(receipt_path(path).is_file())
            path.write_text("new configuration", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "matches"):
                stop(("wsb.exe", True), path)
            run.assert_called_once()

    def test_fixture_mount_contains_only_the_bundled_script_and_preserves_existing_file(self):
        with tempfile.TemporaryDirectory(prefix="test & sandbox-") as directory:
            path = Path(directory) / "test.wsb"
            config = ET.parse(prepare(path, with_fixture=True)).getroot()
            mount = config.find("MappedFolders/MappedFolder")
            host = Path(mount.findtext("HostFolder"))
            self.assertEqual(host.parent, path.parent.resolve())
            copied, = host.iterdir()
            self.assertEqual(copied.read_bytes(), (APP / "tests" / "fixtures" / "uia_form.ps1").read_bytes())
            self.assertEqual(mount.findtext("ReadOnly"), "true")
            old = path.read_bytes()
            with self.assertRaises(FileExistsError):
                prepare(path, with_fixture=True)
            self.assertEqual(path.read_bytes(), old)
            self.assertEqual(len(list(path.parent.iterdir())), 2)
