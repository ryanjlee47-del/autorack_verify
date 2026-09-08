#!/usr/bin/env python3
"""Local dev entrypoint: `python serve.py`.

- Creates the DB and runs migrations if absent.
- Seeds a demo account (three manifests: UPC-E/UPC-A/EAN-13/GS1-128
  variety, 5-digit collision cases, alphanumeric SKUs) if the DB is empty.
- Auto-detects the LAN IP, generates a self-signed cert if needed, and
  serves HTTPS -- phone cameras require HTTPS (or localhost), nothing
  works without this.
- Prints the LAN URL and an ASCII QR so you can point a real phone at it
  in ten seconds.

`python serve.py --offline-drill` additionally seeds a shift, prints its
join QR, and hard-blocks /w/sync for 60 seconds so you can prove the
offline path actually survives instead of assuming it.
"""

from __future__ import annotations

import argparse
import secrets
import socket
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import segno

import app as app_module
import backup
import db
import manifest_ingest
import seed

BASE_DIR = Path(__file__).parent
CERT_DIR = BASE_DIR / "certs"
CERT_FILE = CERT_DIR / "dev-cert.pem"
KEY_FILE = CERT_DIR / "dev-key.pem"


def detect_lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def ensure_self_signed_cert(lan_ip: str) -> tuple[Path, Path]:
    if CERT_FILE.exists() and KEY_FILE.exists():
        return CERT_FILE, KEY_FILE
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    san = f"subjectAltName=DNS:localhost,IP:127.0.0.1,IP:{lan_ip}"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(KEY_FILE),
            "-out",
            str(CERT_FILE),
            "-days",
            "825",
            "-subj",
            "/CN=autorack-verify.local",
            "-addext",
            san,
        ],
        check=True,
        capture_output=True,
    )
    return CERT_FILE, KEY_FILE


def print_qr(url: str) -> None:
    qr = segno.make(url)
    qr.terminal(compact=True)


def prepare_offline_drill_shift(conn) -> tuple[str, str]:
    """Seed (or reuse) the demo account, commit a small manifest if none
    exists, create a shift, and return (shift_token, label).

    The docstring used to say (join_url, label); it has always returned the
    token, and main() builds the URL from it. The first `info =` assignment
    was likewise dead -- it was overwritten on the only branch that read it.
    """
    account_id = 1
    if db.get_account(conn, account_id) is None:
        account_id = seed.seed_demo_account(conn)["account_id"]
    else:
        seed.ensure_seeded(conn)

    manifests = db.list_manifests(conn, account_id)
    manifest_ids = [m["id"] for m in manifests if m["status"] == "committed"]
    if not manifest_ids:
        rows = [
            {
                "line_no": 1,
                "sku": "DRILL-1",
                "description": "Offline drill item",
                "qty_expected": 1,
                "raw_barcode": "0000012345",
            }
        ]
        mid, _report = manifest_ingest.commit_manifest(
            conn, account_id, "DRILL", None, rows, False, 8
        )
        manifest_ids = [mid]

    token = secrets.token_urlsafe(24)
    # Milliseconds -- see auth._expiry for why the precision matters.
    expires = (datetime.now(UTC) + timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    # A real content hash, like every other shift. The literal "pending" was
    # written here and never replaced, so this shift's bundle_hash recorded
    # nothing at all -- see app.py's shift_prepare, which computes the same
    # value from the same function.
    account = db.get_account(conn, account_id)
    shift_date = datetime.now(UTC).date().isoformat()
    bundle_hash = manifest_ingest.bundle_content_hash(
        manifest_ingest.bundle_payload(
            conn,
            account,
            {"id": None, "label": "Offline drill shift", "date": shift_date, "bundle_version": 0},
            manifest_ids,
        )
    )
    shift_id = db.create_shift(
        conn,
        account_id,
        "Offline drill shift",
        shift_date,
        token,
        expires,
        bundle_hash,
        0,
    )
    for mid in manifest_ids:
        db.link_shift_manifest(conn, shift_id, mid)
    return token, "Offline drill shift"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--offline-drill",
        action="store_true",
        help="Seed a shift, print its join QR, block /w/sync for 60s",
    )
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--db", type=str, default=None)
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else db.DEFAULT_DB_PATH
    if db_path.exists():
        backup_result = backup.backup_everything(db_path)
        print(f"Backed up existing database to {backup_result['db']}")
        if backup_result["photos"]:
            print(f"Backed up appeal photos to {backup_result['photos']}")

    conn = db.init_db(db_path)
    seed_info = seed.ensure_seeded(conn)
    if seed_info:
        print(f"Seeded demo account -- login: {seed_info['email']} / {seed_info['password']}")

    lan_ip = detect_lan_ip()
    cert_file, key_file = ensure_self_signed_cert(lan_ip)

    flask_app = app_module.create_app(db_path=db_path)

    if args.offline_drill:
        drill_conn = db.connect(db_path)
        token, label = prepare_offline_drill_shift(drill_conn)
        join_url = f"https://{lan_ip}:{args.port}/w/join?t={token}"
        print(f"\nOffline drill: '{label}'")
        print(f"Join URL: {join_url}\n")
        print_qr(join_url)
        print("\n/w/sync will return 503 for the next 60 seconds. Join the shift, download the")
        print("bundle, put the phone in airplane mode, scan a few items, then reconnect after")
        print("60s and confirm the outbox drains and everything appears server-side.\n")
        app_module.set_offline_drill_block(60)
        drill_conn.close()

    owner_url = f"https://{lan_ip}:{args.port}/"
    print(f"\nOwner app:  {owner_url}")
    print(f"Worker PWA: https://{lan_ip}:{args.port}/w\n")
    print_qr(owner_url)

    flask_app.run(
        host="0.0.0.0",
        port=args.port,
        ssl_context=(str(cert_file), str(key_file)),
        debug=False,
        threaded=True,
    )


if __name__ == "__main__":
    main()
