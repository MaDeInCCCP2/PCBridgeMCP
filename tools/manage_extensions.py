"""Import, review, enable and export local PCBridge extensions."""
import argparse
import json
import os
import sys
import zipfile
from pathlib import Path


APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP / "server"))
from extensions import Extensions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("import").add_argument("path", type=Path)
    for action in ("list", "enable", "disable", "export"):
        option = actions.add_parser(action)
        option.add_argument("kind", choices=("skill", "plugin"))
        if action != "list":
            option.add_argument("name")
        if action == "export":
            option.add_argument("destination", type=Path)
    args = parser.parse_args()
    registry = Extensions(os.environ.get("PCBRIDGE_ROOT") or APP)
    try:
        if args.action == "import":
            result = registry.import_package(args.path)
        elif args.action == "list":
            result = registry.list(args.kind)
        elif args.action == "export":
            result = {"path": registry.export(args.kind, args.name, args.destination)}
        else:
            result = registry.set_enabled(args.kind, args.name, args.action == "enable")
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        parser.exit(1, f"{error}\n")
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
