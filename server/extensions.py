"""Local owner-managed registry for skills and portable plugin packages."""
import json
import os
import re
import tempfile
from pathlib import Path

from extension_packages import checked_path, copy_package, fingerprint, relative_name
from plugin_manager import inspect as inspect_plugin
from skills_manager import parse as parse_skill


class Extensions:
    def __init__(self, root):
        self.root = Path(root).resolve() / "data" / "extensions"

    def key_path(self, kind, name):
        if kind not in ("skill", "plugin") or "/" in name:
            raise ValueError("Invalid extension ID")
        relative_name(name)
        return self.root / kind / (name + ".json")

    def get(self, kind, name):
        path = self.key_path(kind, name)
        try:
            checked_path(path, self.root.resolve())
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ValueError("Extension is not registered") from None

    def save(self, record):
        path = self.key_path(record["kind"], record["name"])
        path.parent.mkdir(parents=True, exist_ok=True)
        checked_path(path.parent, self.root.resolve())
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            tmp = Path(handle.name)
            json.dump(record, handle, ensure_ascii=False)
        try:
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    def import_package(self, source):
        self.root.mkdir(parents=True, exist_ok=True)
        checked_path(self.root, self.root.resolve())
        checked_path(self.root.parent, self.root.parent.resolve())
        with tempfile.TemporaryDirectory(dir=self.root) as stage:
            files = Path(stage) / "files"
            files.mkdir()
            dirname, package = copy_package(Path(source), files)
            if (package / "plugin.json").is_file():
                kind, metadata = "plugin", inspect_plugin(package)
            else:
                kind, metadata = "skill", parse_skill(package, dirname)[0]
            name = metadata["name"]
            checksum = fingerprint(package)
            path = self.key_path(kind, name)
            if path.exists():
                previous = self.get(kind, name)
                if previous["sha256"] == checksum:
                    return {**previous, "duplicate": True}
            destination = self.root / "packages" / checksum
            destination.parent.mkdir(exist_ok=True)
            checked_path(destination.parent, self.root.resolve())
            try:
                package.rename(destination)
            except OSError:
                if not destination.is_dir():
                    raise
                checked_path(destination, self.root.resolve())
            record = {"kind": kind, "sha256": checksum, "enabled": False, **metadata}
            self.save(record)
            return record

    def set_enabled(self, kind, name, enabled):
        record = self.get(kind, name)
        record["enabled"] = enabled
        self.save(record)
        return record

    def list(self, kind, enabled_only=False):
        if kind not in ("skill", "plugin"):
            raise ValueError("Invalid extension kind")
        path = self.root / kind
        if not path.exists():
            return []
        checked_path(path, self.root.resolve())
        records = [self.get(kind, item.stem) for item in sorted(path.glob("*.json"))]
        return [record for record in records if record["enabled"] or not enabled_only]

    def package_root(self, record):
        checksum = record.get("sha256", "")
        if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
            raise ValueError("Invalid package hash")
        root = self.root / "packages" / checksum
        checked_path(root, self.root.resolve())
        return root

    def skills(self):
        standalone = [{**record, "id": record["name"]} for record in self.list("skill", enabled_only=True)]
        bundled = [{**skill, "id": f"{plugin['name']}/{skill['name']}", "plugin": plugin["name"]}
                   for plugin in self.list("plugin", enabled_only=True) for skill in plugin["skills"]]
        return standalone + bundled

    def skill(self, skill_id):
        prefix, separator, name = skill_id.partition("/")
        if separator:
            record = self.get("plugin", prefix)
            if not record["enabled"] or name not in {skill["name"] for skill in record["skills"]}:
                raise ValueError("Skill is not enabled")
            package = self.package_root(record)
            root = package / "skills" / name
            checked_path(root, package.resolve())
        else:
            name = prefix
            record = self.get("skill", name)
            if not record["enabled"]:
                raise ValueError("Skill is not enabled")
            root = self.package_root(record)
        metadata, text = parse_skill(root, name)
        return {**metadata, "id": skill_id, "content": text, "source": "skill",
                "trust": "untrusted_external"}

    def export(self, kind, name, destination):
        record = self.get(kind, name)
        source = self.package_root(record)
        target = Path(destination) / name
        if target.exists():
            raise FileExistsError("The target extension directory already exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=target.parent) as scratch:
            stage = Path(scratch) / name
            stage.mkdir()
            copy_package(source, stage)
            stage.rename(target)
        return str(target)
