"""List OAuth client IDs or revoke a registration and all of its access tokens."""
import argparse
import json
import os
import sys
from pathlib import Path


APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP / "server"))
from oauth_store import OAuthStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("client_id", nargs="?", help="Omit to list registered clients")
    args = parser.parse_args()
    root = Path(os.environ.get("PCBRIDGE_ROOT") or APP)
    database = root / "oauth.sqlite3"
    if not database.is_file():
        print("No OAuth clients are registered.")
        return
    settings = json.loads((root / "config.json").read_text(encoding="utf-8-sig"))
    store = OAuthStore(database, settings["secret"])
    if not args.client_id:
        print(json.dumps(store.clients(), ensure_ascii=True, indent=2))
        return
    if args.client_id == "pcbridge":
        store.remove_client(args.client_id)
        print("Revoked tokens for the owner service client.")
    elif store.remove_client(args.client_id):
        print("Revoked client and its access tokens.")
    else:
        parser.exit(1, "Unknown client ID.\n")


if __name__ == "__main__":
    main()
