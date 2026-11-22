"""Check that a PC Bridge checkout or extracted release is complete."""
import argparse
import re
from pathlib import Path


REQUIRED = (
    "START.bat", "setup.ps1", "README.md", "RUN_TESTS.bat",
    "server/server.py", "server/supervisor.py", "server/requirements.txt",
    "server/http_policy.py", "server/oauth.py", "server/oauth_store.py", "server/panel.html",
    "server/browser_worker.py", "server/browser_requests.py", "server/mcp_protocol.py", "server/automation_router.py", "server/diagnostics.py",
    "server/operation_receipts.py", "server/uia_receipts.py", "server/desktop_peers.py", "server/leases.py", "server/job_worker.py", "server/process_jobs.py",
    "server/extensions.py", "server/external_mcp.py", "server/extension_packages.py", "server/skills_manager.py", "server/plugin_manager.py",
    "tools/revoke_oauth_client.py", "tools/verify_distribution.py", "tools/manage_extensions.py", "tools/smoke_mcp.py",
    "tools/benchmark_win32_session.py", "tools/windows_sandbox.py", "tools/child_session.ps1", "server/windows_isolation.py",
    "tools/benchmark_computer_use.py", "tools/benchmark_vision.py", "tools/benchmark_win32_fixtures.py",
    "tools/benchmark_browser_sessions.py",
    "tests/fixtures/desktop_webgl.html", "tests/fixtures/desktop_electron.cjs",
    "tests/fixtures/winui/PCBridge.WinUI.Fixture.csproj", "tests/fixtures/winui/Fixture.cs", "server/desktop_sessions.py", "server/desktop_worker.py", "server/desktop_process.py",
)


def verify(root):
    missing = [name for name in ("VERSION.txt", *REQUIRED) if not (root / name).is_file()]
    if missing:
        raise ValueError("Missing release files: " + ", ".join(missing))
    actual = (root / "VERSION.txt").read_text(encoding="utf-8-sig").strip()
    if not re.fullmatch(r"(0|[1-9][0-9]*)(\.(0|[1-9][0-9]*)){2}", actual):
        raise ValueError("VERSION.txt must contain a major.minor.patch version")
    if f"PCBridge {actual}" not in (root / "README.md").read_text(encoding="utf-8-sig"):
        raise ValueError("README.md does not match VERSION.txt")
    return actual


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, nargs="?", default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()
    try:
        version = verify(args.directory)
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")
    print(f"PC Bridge {version} distribution verified")


if __name__ == "__main__":
    main()
