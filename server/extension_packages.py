"""Stage extension packages without trusting filenames, links or executable content."""
import hashlib
import os
import re
import stat
import zipfile
from pathlib import Path, PurePosixPath


MAX_BYTES = 64 * 1024 * 1024
MAX_ENTRIES = 2048
DEVICES = re.compile(r"^(?:con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])(?:\.|$)", re.IGNORECASE)


def relative_name(name):
    parts = name.rstrip("/").split("/")
    if not parts or any(not part or part in (".", "..") or part.endswith((".", " "))
                        or DEVICES.match(part) or any(c in '\\<>:"|?*' or ord(c) < 32 for c in part)
                        for part in parts):
        raise ValueError("Unsafe extension path")
    return PurePosixPath(*parts)


def checked_path(path, root):
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
            or not path.resolve().is_relative_to(root.resolve())):
        raise ValueError("Links, reparse points and out-of-root paths are rejected")
    return info


def read_member(path, root, limit):
    if not stat.S_ISREG(checked_path(path, root).st_mode):
        raise ValueError("Extension member must be a regular file")
    with path.open("rb") as data:
        contents = data.read(limit + 1)
    if len(contents) > limit:
        raise ValueError("Extension member exceeds the size limit")
    return contents


def copy_package(source, destination):
    """Copy an explicit local folder or extract a ZIP into a private staging directory."""
    source = source.absolute()
    contents = {}
    spellings = {}
    total = 0

    def add(name, data=None):
        nonlocal total
        relative = relative_name(name)
        folded = relative.as_posix().casefold()
        if folded in contents or len(contents) >= MAX_ENTRIES:
            raise ValueError("Duplicate path or too many extension entries")
        for component in (relative, *relative.parents):
            spelling = component.as_posix()
            canonical = spelling.casefold()
            if spellings.setdefault(canonical, spelling) != spelling:
                raise ValueError("Extension paths differ only in case")
        for parent in relative.parents:
            if parent == PurePosixPath("."):
                break
            if parent.as_posix().casefold() in contents and contents[parent.as_posix().casefold()][:1] != ("dir",):
                raise ValueError("Extension path collides with an existing file")
        target = destination / relative
        if data is None:
            target.mkdir(parents=True, exist_ok=True)
            contents[folded] = ("dir", relative.as_posix())
            return
        with data() as stream:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as writer:
                while chunk := stream.read(65536):
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise ValueError("Extension exceeds the uncompressed size limit")
                    writer.write(chunk)
        contents[folded] = ("file", relative.as_posix())

    def folder(directory, root):
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.name == ".git":
                    continue
                path = Path(entry.path)
                info = checked_path(path, root)
                name = path.relative_to(source).as_posix()
                if stat.S_ISDIR(info.st_mode):
                    add(name)
                    folder(path, root)
                elif stat.S_ISREG(info.st_mode):
                    add(name, lambda: path.open("rb"))
                else:
                    raise ValueError("Unsupported extension filesystem entry")

    info = checked_path(source, source.resolve())
    if stat.S_ISDIR(info.st_mode):
        folder(source, source.resolve())
        return source.name, destination
    if not stat.S_ISREG(info.st_mode) or source.suffix.lower() != ".zip":
        raise ValueError("Use a skill or plugin folder, a local Git checkout, or a ZIP")
    if info.st_size > MAX_BYTES:
        raise ValueError("Extension ZIP exceeds the compressed size limit")
    with zipfile.ZipFile(source) as archive:
        for entry in archive.infolist():
            mode = (entry.external_attr >> 16) & 0o170000
            if mode not in (0, stat.S_IFREG, stat.S_IFDIR) or entry.flag_bits & 1:
                raise ValueError("Unsupported ZIP entry")
            # zipfile normalizes backslashes on Windows. Validate the archive spelling first.
            name = entry.orig_filename
            add(name, None if name.endswith("/") else lambda: archive.open(entry))
    if (destination / "SKILL.md").is_file() or (destination / "plugin.json").is_file():
        return source.stem, destination
    top = list(destination.iterdir())
    if len(top) == 1 and top[0].is_dir():
        return top[0].name, top[0]
    raise ValueError("ZIP must contain one extension package")


def fingerprint(root):
    result = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        info = checked_path(path, root.resolve())
        if stat.S_ISREG(info.st_mode):
            name = path.relative_to(root).as_posix().encode("utf-8")
            with path.open("rb") as data:
                result.update(len(name).to_bytes(4, "big") + name)
                result.update(hashlib.file_digest(data, "sha256").digest())
    return result.hexdigest()


def plugin_path(root, value, mode):
    if value in ("${PLUGIN_ROOT}", "${PLUGIN_DATA}") and mode == "cwd":
        return root
    if value.startswith("${PLUGIN_ROOT}/") or value.startswith("${PLUGIN_DATA}/"):
        if mode != "cwd":
            raise ValueError("Placeholders are unavailable in command paths")
        value = "./" + value.split("/", 1)[1]
        relative_name(value[2:])
        return None
    if not value.startswith("./"):
        if mode == "command" and re.fullmatch(r"[a-zA-Z0-9_.-]+", value) and not DEVICES.match(value):
            return None  # executable from PATH when an owner later configures a runtime
        raise ValueError("Expected a portable plugin-relative path")
    path = root / relative_name(value[2:])
    info = checked_path(path, root)
    if not (stat.S_ISDIR(info.st_mode) if mode == "cwd" else stat.S_ISREG(info.st_mode)):
        raise ValueError("Invalid plugin component path")
    return path
