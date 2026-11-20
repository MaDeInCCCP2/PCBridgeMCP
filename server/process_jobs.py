"""Structured process payloads and immutable job artifacts."""
from collections.abc import Mapping
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

import psutil


def process_command(executable, argv, cwd, resolve, shell_mode):
    if not executable or "\x00" in executable or len(argv) > 512 or any("\x00" in arg for arg in argv):
        raise ValueError("Executable and argv must be nonempty paths/strings without NUL; argv is limited to 512 items.")
    if "/" in executable or "\\" in executable or ":" in executable:
        path = resolve(executable)
    else:
        found = shutil.which(executable)
        if not found:
            raise FileNotFoundError(executable)
        path = Path(found).resolve()
    if not path.is_file():
        raise FileNotFoundError(str(path))
    command = [str(path), *argv]
    if len(subprocess.list2cmdline(command)) > 30000:
        raise ValueError("Executable plus arguments exceed the Windows command line limit.")
    if path.suffix.lower() in {".cmd", ".bat"}:
        if os.name != "nt" or shell_mode != "cmd":
            raise ValueError("Batch files require explicit shell_mode='cmd'.")
        if any(re.search(r'[%!^&|<>()"\r\n]', arg) for arg in command):
            raise ValueError("Batch arguments contain shell metacharacters; use a native executable for literal argv.")
        cmd = Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe"
        joined = " ".join(f'"{part}"' for part in command)
        return str(path), f'"{cmd}" /d /v:off /s /c "{joined}"'
    if shell_mode != "none":
        raise ValueError("shell_mode='cmd' is only for .cmd or .bat files.")
    if os.name == "nt" and path.suffix.lower() not in {".exe", ".com"}:
        raise ValueError("Use a native executable, or explicitly select cmd mode for a batch file.")
    return str(path), command


def process_environment(profile, delta):
    if not isinstance(profile, Mapping) or len(profile) + len(delta) > 256:
        raise ValueError("Environment profile/delta is invalid or exceeds 256 entries.")
    values = {k.upper() if os.name == "nt" else k: v for k, v in os.environ.items()}
    for changes in (profile, delta):
        for k, v in changes.items():
            if not isinstance(k, str) or not k or "=" in k or "\x00" in k or (
                    v is not None and (not isinstance(v, str) or "\x00" in v or len(v) > 30000)):
                raise ValueError("Invalid environment override.")
            key = k.upper() if os.name == "nt" else k
            if v is None:
                values.pop(key, None)
            else:
                values[key] = v
    return values


def artifacts_snapshot(spec, result, directory):
    """Copy this job's outputs into its new directory before publishing job completion."""
    entries = spec.get("artifacts", [])
    if not entries:
        return []
    directory.mkdir(parents=True, exist_ok=False)
    bundle = []
    try:
        for entry in entries:
            source = Path(entry["source"])
            dest = directory / entry["name"]
            fd, temp = tempfile.mkstemp(dir=directory, prefix=".copy-")
            temporary = Path(temp)
            try:
                digest = hashlib.sha256()
                size = 0
                with os.fdopen(fd, "wb") as out, source.open("rb") as src:
                    before = os.fstat(src.fileno())
                    for block in iter(lambda: src.read(1 << 20), b""):
                        size += len(block)
                        if size > spec["artifact_max_bytes"]:
                            raise ValueError("Artifact exceeded the configured size limit.")
                        out.write(block)
                        digest.update(block)
                    out.flush()
                    os.fsync(out.fileno())
                    after = os.fstat(src.fileno())
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or size != after.st_size:
                    raise OSError("Artifact changed during copy.")
                os.replace(temporary, dest)
                bundle.append({"job_id": result["job_id"], "artifact_id": f'{result["job_id"]}/{dest.name}',
                               "source": str(source), "path": str(dest), "size": size, "sha256": digest.hexdigest()})
            finally:
                temporary.unlink(missing_ok=True)
    except BaseException:
        for entry in bundle:
            Path(entry["path"]).unlink(missing_ok=True)
        directory.rmdir()
        raise
    return bundle


def stop_children(proc):
    try:
        children = psutil.Process(proc.pid).children(recursive=True)
    except psutil.NoSuchProcess:
        children = []
    for child in reversed(children):
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    proc.wait(timeout=5)
    psutil.wait_procs(children, timeout=2)
