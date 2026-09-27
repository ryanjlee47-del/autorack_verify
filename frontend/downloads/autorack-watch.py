#!/usr/bin/env python3
"""Autorack watched folder: uploads every CSV saved into a folder.

Usage:
    python autorack-watch.py <folder> <drop URL>

The drop URL is on Autorack -> Connections -> "Import by email or from a
folder". Every .csv file that appears in <folder> is uploaded, then moved
into <folder>/imported (or <folder>/failed, with a .txt saying why).
Leave it running (or start it with Windows Task Scheduler / cron at boot).

Needs only Python 3.8+; nothing to install.
"""

import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request

POLL_SECONDS = 15
SETTLE_SECONDS = 5  # wait until a file stops changing (still being written)


def upload(path, url):
    with open(path, "rb") as f:
        body = f.read()
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "text/csv", "X-Filename": os.path.basename(path)},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def move(path, sub):
    dest_dir = os.path.join(os.path.dirname(path), sub)
    os.makedirs(dest_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(dest_dir, f"{stamp}-{os.path.basename(path)}")
    shutil.move(path, dest)
    return dest


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    folder, url = sys.argv[1], sys.argv[2]
    if not os.path.isdir(folder):
        sys.exit(f"Folder not found: {folder}")
    if "/api/inbound/drop/" not in url:
        sys.exit("That doesn't look like an Autorack drop URL (copy it from Connections).")
    print(f"Watching {folder} -- new CSV files go to Autorack. Ctrl+C to stop.")
    sizes = {}
    while True:
        for name in sorted(os.listdir(folder)):
            path = os.path.join(folder, name)
            if not name.lower().endswith(".csv") or not os.path.isfile(path):
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue
            key = (st.st_size, st.st_mtime)
            if sizes.get(path) != key or time.time() - st.st_mtime < SETTLE_SECONDS:
                sizes[path] = key
                continue
            sizes.pop(path, None)
            try:
                result = upload(path, url)
                dest = move(path, "imported")
                print(
                    f"{time.strftime('%H:%M:%S')} {name}: {result.get('orders_created', 0)} new order(s), "
                    f"{result.get('orders_skipped', 0)} already there -> {dest}"
                )
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:500]
                if e.code in (429,) or e.code >= 500:
                    print(f"{name}: Autorack busy ({e.code}); will retry.")
                    continue
                dest = move(path, "failed")
                with open(dest + ".txt", "w", encoding="utf-8") as f:
                    f.write(f"HTTP {e.code}\n{detail}\n")
                print(f"{name}: not imported ({e.code}) -> {dest}")
            except (urllib.error.URLError, OSError) as e:
                print(f"{name}: can't reach Autorack ({e}); will retry.")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
