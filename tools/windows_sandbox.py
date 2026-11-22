"""Prepare or open a local Windows Sandbox profile for an isolated GUI test."""
import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
WINDOWS_FIXTURE = ROOT / "tests" / "fixtures" / "uia_form.ps1"
GUEST_FOLDER = r"C:\PCBridgeFixture"
ISOLATION = ("vGPU", "Networking", "AudioInput", "VideoInput", "PrinterRedirection", "ClipboardRedirection")


def sandbox_launcher():
    if os.name != "nt":
        return None
    # Prefer the modern CLI when both it and the profile association are installed.
    cmd = shutil.which("wsb.exe")
    if cmd:
        return cmd, True
    native = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsSandbox.exe"
    return (str(native), False) if native.is_file() else None


def launch_arguments(launcher, profile):
    executable, is_cli = launcher
    if is_cli:
        return [executable, "start", "--config", profile.read_text(encoding="utf-8")]
    return [executable, str(profile)]


def configuration(fixture_folder=None):
    config = ET.Element("Configuration")
    for name in ISOLATION:
        ET.SubElement(config, name).text = "Disable"
    ET.SubElement(config, "ProtectedClient").text = "Enable"
    if fixture_folder is not None:
        mapped = ET.SubElement(ET.SubElement(config, "MappedFolders"), "MappedFolder")
        ET.SubElement(mapped, "HostFolder").text = str(fixture_folder)
        ET.SubElement(mapped, "SandboxFolder").text = GUEST_FOLDER
        ET.SubElement(mapped, "ReadOnly").text = "true"
        command = (
            rf'powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File "{GUEST_FOLDER}\uia_form.ps1" '
            r'-Title "PCBridge Sandbox Fixture"'
        )
        ET.SubElement(ET.SubElement(config, "LogonCommand"), "Command").text = command
    ET.indent(config)
    return ET.tostring(config, encoding="utf-8", xml_declaration=True)


def prepare(path, with_fixture=False):
    if path.suffix.lower() != ".wsb":
        raise ValueError("Choose a .wsb output file.")
    parent = path.parent.resolve(strict=True)
    target = parent / path.name
    asset = Path(tempfile.mkdtemp(prefix="pcbridge-fixture-", dir=parent)) if with_fixture else None
    try:
        if asset is not None:
            shutil.copyfile(WINDOWS_FIXTURE, asset / WINDOWS_FIXTURE.name)
        content = configuration(asset)
        with target.open("xb") as file:
            try:
                file.write(content)
            except BaseException:
                file.close()
                target.unlink(missing_ok=True)
                raise
    except BaseException:
        if asset is not None:
            shutil.rmtree(asset)
        raise
    return target


def receipt_path(profile):
    return profile.with_suffix(".wsb.session.json")


def start(launcher, profile):
    command = launch_arguments(launcher, profile)
    if not launcher[1]:
        subprocess.Popen(command, shell=False)
        return {"profile": str(profile), "launch_requested": True, "managed": False}

    id_ = str(uuid.uuid4())
    receipt = {"profile_sha256": sha256(profile.read_bytes()).hexdigest(), "sandbox_id": id_}
    with receipt_path(profile).open("x", encoding="utf-8") as record:
        json.dump(receipt, record)
    # Keep the receipt even when the command times out, since a guest might have started.
    subprocess.run([*command, "--id", id_], check=True, timeout=60,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"profile": str(profile), "launch_requested": True, "managed": True, "sandbox_id": id_}


def stop(launcher, profile):
    if not launcher[1]:
        raise ValueError("Managed stop requires the modern Windows Sandbox CLI.")
    target = profile.resolve(strict=True)
    if target.suffix.lower() != ".wsb":
        raise ValueError("Specify the original .wsb profile.")
    ticket = receipt_path(target)
    receipt = json.loads(ticket.read_text(encoding="utf-8"))
    if (not isinstance(receipt, dict) or not isinstance(receipt.get("sandbox_id"), str)
            or receipt.get("profile_sha256") != sha256(target.read_bytes()).hexdigest()):
        raise ValueError("The profile no longer matches its Sandbox launch receipt.")
    id_ = uuid.UUID(receipt["sandbox_id"])
    if id_.version != 4:
        raise ValueError("Invalid Sandbox launch receipt ID.")
    subprocess.run([launcher[0], "stop", "--id", str(id_)], check=True, timeout=30,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ticket.unlink(missing_ok=True)
    return {"profile": str(target), "sandbox_id": str(id_), "stopped": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "prepare", "start", "stop"))
    parser.add_argument("path", type=Path, nargs="?", help="The .wsb profile to create or stop")
    parser.add_argument("--fixture", action="store_true", help="Map only a generated read-only GUI test fixture")
    args = parser.parse_args()
    launcher = sandbox_launcher()
    if args.command == "status":
        print(json.dumps({"windows": os.name == "nt", "launcher_detected": launcher is not None,
                          "managed_lifecycle": bool(launcher and launcher[1])}))
        return
    if args.path is None:
        parser.error("Supply a .wsb file.")
    if args.command in ("start", "stop") and launcher is None:
        parser.exit(2, "Windows Sandbox launcher is unavailable. Use prepare to validate the profile separately.\n")
    try:
        if args.command == "stop":
            result = stop(launcher, args.path)
        else:
            profile = prepare(args.path, args.fixture)
            result = start(launcher, profile) if args.command == "start" else {
                "profile": str(profile), "launch_requested": False,
            }
    except subprocess.SubprocessError:
        parser.exit(1, "Windows Sandbox CLI failed or timed out. Inspect the Windows session before retrying.\n")
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
