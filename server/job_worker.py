"""Detached job monitor: persists the child's real exit status even if the MCP server restarts.
No MCP/config import: arguments are private local job files created by start_job.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid


def write_result(path: Path, result: dict) -> None:
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(result, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def structured_job(spec, result_path, result):
    from process_jobs import artifacts_snapshot, stop_children

    result.update(status="failed", executed=False, phase="launch")
    try:
        payload = json.load(sys.stdin)
        spec.update(payload)
        stdin = spec.get("stdin_text")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if spec["hidden"] else 0
        stderr = result_path.with_name(f'{spec["job_id"]}.stderr.log')
        with stderr.open("wb") if spec["stderr_separate"] else open(os.devnull, "wb") as err:
            proc = subprocess.Popen(spec["command"], cwd=spec.get("cwd"), env=spec["env"],
                                    stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                    stderr=err if spec["stderr_separate"] else subprocess.STDOUT,
                                    creationflags=flags)
            result.update(executed=True, phase="process_exit")
            try:
                proc.communicate(stdin.encode("utf-8") if stdin is not None else None, timeout=spec["timeout"])
            except subprocess.TimeoutExpired:
                stop_children(proc)
                result.update(phase="timeout", error_code="process_timeout", error="Process timed out.")
                return
        result["exit_code"] = proc.returncode
        if proc.returncode != 0:
            result.update(error_code="process_exit_nonzero", error="Process returned a nonzero exit code.")
            return
        result["phase"] = "artifacts"
        result["artifacts"] = artifacts_snapshot(spec, result, result_path.parent / "artifacts" / spec["job_id"])
        result.update(status="completed", phase="complete")
    except Exception as exc:
        result.update(error_code="artifact_failed" if result["phase"] == "artifacts" else
                      "process_outcome_unknown" if result["executed"] else "process_launch_failed",
                      error=f'{result["phase"]}: {type(exc).__name__}')


def main() -> int:
    spec_path, result_path = map(Path, sys.argv[1:3])
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec_path.unlink()  # command arguments need not remain on disk after launch
    result = {"job_id": spec["job_id"], "exit_code": None}
    if spec.get("structured"):
        structured_job(spec, result_path, result)
    else:
        try:
            proc = subprocess.Popen(spec["command"], cwd=spec.get("cwd"), stdin=subprocess.DEVNULL,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            result["exit_code"] = proc.wait()
            result["status"] = "completed" if proc.returncode == 0 else "failed"
        except Exception as e:
            result.update(status="failed", error=str(e))
    result["finished"] = time.time()
    write_result(result_path, result)
    return 0  # the child's exit code is recorded without Windows exit-code conversion


if __name__ == "__main__":
    raise SystemExit(main())
