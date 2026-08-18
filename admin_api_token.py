#!/usr/bin/env python3
"""Bootstrap CLI for the admin API's bearer token (see admin_api.py).

Run this once after deploying -- e.g. in a PythonAnywhere Bash console --
to print the token admin_gui.py needs to connect in Remote mode. Safe to
run again later: it never overwrites an existing token, only prints it.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import admin_api
import db


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--db", type=str, default=None, help="Path to the app's database (default: data/app.db)"
    )
    parser.add_argument(
        "--show", action="store_true", help="Print the token, generating it on first run"
    )
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else db.DEFAULT_DB_PATH
    token_path = db_path.parent / admin_api.TOKEN_FILENAME
    token = admin_api._load_or_create_token(token_path)

    if args.show:
        print(token)
    else:
        print(f"Token file: {token_path}")
        print("Run with --show to print the token.")


if __name__ == "__main__":
    main()
