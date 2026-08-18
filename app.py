"""Flask app factory. Three surfaces, one codebase:

  /            marketing + owner web app (auth, upload, dashboards)
  /w           worker PWA (offline scan loop)

This module wires routes; db.py owns all SQL, barcode.py owns matching,
manifest_ingest.py owns ingest/bundle building, billing.py owns pricing
logic. Each request gets its own SQLite connection via flask.g, closed in
a teardown handler -- WAL mode makes this cheap and lets many worker
phones write scans concurrently without blocking each other.
"""

from __future__ import annotations

import contextlib
import functools
import gzip
import hashlib
import hmac
import io
import json
import re
import secrets
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import segno
from flask import (
    Flask,
    Response,
    flash,
    g,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)

import admin_api
import auth
import barcode
import billing
import db
import i18n
import manifest_ingest
import pricing
import tz
from sqlstore import SQL

LANG_COOKIE_NAME = "lang"

# Client-generated scan uuids (static/js/worker/app.js's uuidv4()) are the
# idempotency key for scans and, via /w/appeal, get interpolated into a
# filesystem path for the saved photo. Never trust that shape without
# checking it first -- an unvalidated value here is a path-traversal /
# arbitrary-file-write primitive, not just a bad id.
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

ALLOWED_PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"}

# Mirrors the CHECK constraint on scans.result. Validated before insert so
# a malformed sync entry is reported back to the client instead of raising
# an IntegrityError the outbox would retry forever.
SCAN_RESULTS = {"ok", "reject", "duplicate", "unresolved"}

# Endpoints exempt from CSRF checking. The worker PWA carries no session
# cookie -- it authenticates with an opaque session token in the request
# body/URL, which an attacker's page cannot read or guess, so there is no
# ambient authority to ride. `login_submit` and `signup_submit` are
# pre-session by definition and are protected by the credentials they
# carry.
#
# These are Flask *endpoint* names (the view function), not URL paths:
# /signup GET is `signup` but /signup POST is `signup_submit`, and only
# the latter is ever CSRF-checked. Naming the GET endpoint here exempted
# nothing, and made a signup POST from anyone still holding a session
# cookie fail with a confusing 400 -- see
# tests/test_public_hardening.py::test_signup_works_while_holding_a_stale_session_cookie.
CSRF_EXEMPT_ENDPOINTS = frozenset(
    {
        "w_join_submit",
        "w_sync",
        "w_appeal",
        "login_submit",
        "signup_submit",
        # Bearer-token authenticated, cookieless -- see admin_api.py. There's
        # no session to ride so CSRF protection doesn't apply, but this is
        # listed explicitly rather than relying on the "no session cookie ->
        # nothing to forge" fallback below.
        "admin_api.rpc",
        "admin_api.action",
        "admin_api.sql_console",
        "admin_api.backup_route",
        "admin_api.restore_route",
    }
)

# Deliberately permissive: this exists to catch typos and obvious garbage
# so a verification email has somewhere real to go, not to adjudicate
# RFC 5322 (which permits far stranger addresses than any regex should
# try to encode). Actual proof the address works is the verification
# round trip, not this check.
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")

# Self-service signup is the one endpoint an anonymous stranger can use to
# create durable server-side state, so it needs its own budget. Keyed on
# IP alone -- there's no prior identity to key on, by definition.
SIGNUP_MAX_PER_IP = 3
SIGNUP_WINDOW_SECONDS = 3600


def _scan_payload_is_valid(scan: object) -> bool:
    """Whether one entry from a /w/sync batch is structurally usable.

    Typed `object`, not `dict`: this runs on JSON straight off a phone, so
    the isinstance check below is load-bearing rather than defensive --
    a client can legitimately send a list, a string, or null here.
    """
    if not isinstance(scan, dict):
        return False
    if scan.get("result") not in SCAN_RESULTS:
        return False
    return all(isinstance(scan.get(k), str) for k in ("rawPayload", "normalized", "tsClient"))


OFFLINE_DRILL_BLOCK_UNTIL: float | None = None


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _load_or_create_secret_key(path: Path) -> str:
    """Read the app secret from disk, generating it on first run.

    Minting a fresh key per process (the old behaviour) silently breaks
    anything keyed to it the moment the app restarts or runs under more
    than one worker: flashed messages vanish, and the CSRF tokens below
    would stop validating for sessions issued by a sibling process.
    """
    try:
        existing = path.read_text().strip()
        if existing:
            return existing
    except FileNotFoundError:
        pass
    key = secrets.token_hex(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key)
    # Best effort -- some filesystems (e.g. mounted shares) refuse chmod.
    # A key file the OS will not let us lock down is still better than no
    # key file, and the deployment docs cover file permissions separately.
    with contextlib.suppress(OSError):
        path.chmod(0o600)
    return key


def _linked(row: sqlite3.Row | None, what: str) -> sqlite3.Row:
    """Assert a foreign-key-guaranteed row is present.

    Lookups like `db.get_shift(conn, session["shift_id"])` cannot miss:
    `sessions.shift_id` is NOT NULL with a foreign key, and foreign keys
    are enforced (db.connect turns them on). The row is therefore always
    there, but db.get_shift is honestly typed `Row | None` because it is
    also called with ids that came from a URL.

    This marks the difference at the call site. If it ever raises, the
    database has lost referential integrity, which is a 500 worth seeing
    rather than an AttributeError three frames later.
    """
    if row is None:
        raise LookupError(f"{what} referenced by a foreign key is missing")
    return row


def create_app(db_path=None, secret_key: str | None = None) -> Flask:
    app = Flask(__name__)
    app.config["DB_PATH"] = Path(db_path) if db_path else db.DEFAULT_DB_PATH
    app.config["APPEAL_PHOTOS_DIR"] = app.config["DB_PATH"].parent / "appeal_photos"
    app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 8MB -- a phone photo, not a video
    app.secret_key = secret_key or _load_or_create_secret_key(
        app.config["DB_PATH"].parent / "secret_key"
    )

    def get_db():
        if "db" not in g:
            g.db = db.init_db(app.config["DB_PATH"])
        return g.db

    # Attached to the app object so other modules and tests can reach the
    # per-request connection without importing create_app's closure.
    app.get_db = get_db  # type: ignore[attr-defined]

    @app.teardown_appcontext
    def _close_db(exception=None):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    @app.template_filter("localtime")
    def localtime_filter(value):
        # Display-only conversion from the stored UTC string to the
        # logged-in owner's account timezone -- see tz.py. Falls back to
        # UTC if there's no account in scope (shouldn't happen on an
        # @login_required page, but a display filter should never 500).
        account = getattr(g, "account", None)
        tz_name = account["timezone"] if account else tz.DEFAULT_TIMEZONE
        return tz.to_local(value, tz_name)

    @app.template_filter("from_json")
    def from_json_filter(value):
        # audit_log.before_json/after_json are stored as JSON text (see
        # db.record_audit) -- this is the template-side inverse, used by
        # manifest_history.html to render a readable diff instead of a
        # raw JSON blob.
        return json.loads(value) if value else None

    def csrf_token() -> str:
        """A token bound to the caller's own session cookie.

        Derived rather than stored: HMAC(secret_key, session_token) is
        unguessable without the cookie, needs no extra column, and dies
        with the session it belongs to. Returns "" when logged out, since
        there is nothing to protect and nothing to bind to.
        """
        session_token = request.cookies.get(auth.SESSION_COOKIE_NAME)
        if not session_token:
            return ""
        # app.secret_key is typed `str | bytes | None` by Flask, but
        # create_app always assigns the str from _load_or_create_secret_key.
        secret = app.secret_key
        assert isinstance(secret, str)
        return hmac.new(secret.encode(), session_token.encode(), hashlib.sha256).hexdigest()

    app.csrf_token = csrf_token  # type: ignore[attr-defined]  # exposed for tests
    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.before_request
    def _require_csrf_token():
        # Cookie-authenticated writes need proof the request came from our
        # own page rather than a form on someone else's site. SameSite=Lax
        # on the session cookie already blocks most cross-site POSTs; this
        # is the part that doesn't depend on browser behaviour.
        if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
            return None
        if request.endpoint in CSRF_EXEMPT_ENDPOINTS:
            return None
        expected = csrf_token()
        if not expected:
            return None  # not logged in: no session to ride, nothing to forge
        supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
        if not hmac.compare_digest(supplied, expected):
            return ("CSRF token missing or invalid.", 400)
        return None

    register_worker_routes(app, get_db)
    register_owner_routes(app, get_db)
    register_marketing_routes(app, get_db)
    admin_api.register_admin_api_routes(app, get_db)

    return app


# ---------------------------------------------------------------------------
# Worker PWA (/w)
# ---------------------------------------------------------------------------


def _server_confirms_reject(result: barcode.MatchResult) -> bool:
    """True only if the server's own recomputation agrees with a client's
    REJECT claim: no tier resolved, AND no tier even saw ambiguous
    candidates. A tier that hit the ambiguity guard (2+ candidates) means
    the server sees this as genuinely uncertain, not a confident absence
    -- the same standard the client's own JS matcher uses to distinguish
    'reject' (every tier: zero candidates) from 'unresolved' (some tier:
    2+ candidates before falling through). See billing.py's module
    docstring for that distinction on the client-reported side.
    """
    return not result.is_resolved and not any(result.candidates_by_tier.values())


def _bill_confirmed_reject(conn, shift, scan_uuid: str, raw_payload: str) -> None:
    """Bill a client-reported REJECT only after the server itself confirms
    the barcode doesn't match the shift's current manifest.

    The phone's own matcher runs offline against a downloaded bundle and
    is the only thing that can decide match/no-match without a signal --
    that's the point of the offline-first design. But billing is real
    money, and "the phone said reject" is not something the server can
    take on faith: a scripted client with a valid (unauthenticated, by
    design) join session could POST a fabricated reject for a barcode
    that was never scanned, and a bug in the client matcher produces the
    same effect honestly. So before charging the account, recompute the
    same match independently, using the identical index the bundle itself
    was built from (manifest_ingest.build_match_index -- the same
    function admin_gui's forensic panel already uses to recompute a scan
    after the fact).

    Agreement -> bill, same as before. Disagreement -> don't bill; file a
    'manual_review' exception instead (already in the schema's CHECK
    constraint and the exceptions_list.html label map, never previously
    created by anything), pointing at the line the server itself matched
    when it found exactly one, so an owner reviewing it has a concrete
    lead instead of a bare "these disagreed."
    """
    account = _linked(db.get_account(conn, shift["account_id"]), "account")
    manifest_ids = db.get_shift_manifest_ids(conn, shift["id"])
    idx = manifest_ingest.cached_match_index(conn, account, shift, manifest_ids)
    result = idx.match(raw_payload)

    if _server_confirms_reject(result):
        billing.process_scan_for_billing(conn, shift["account_id"], scan_uuid)
    else:
        # MatchIndex is generic over the id type (barcode.py has no idea
        # these are rows), so manifest_line_id comes back as `object`.
        # Every id put into the index is a manifest_lines rowid.
        matched_line_id = cast("int | None", result.manifest_line_id)
        exception_id = db.create_exception(
            conn, scan_uuid, "manual_review", manifest_line_id=matched_line_id
        )
        # System-initiated, not a human action, but it's still a decision
        # not to bill a client-reported reject -- exactly the kind of
        # event that should be traceable independent of the exceptions
        # row itself.
        db.record_audit(
            conn,
            actor="system:reject_verification",
            action="scan.flagged_for_review",
            target_table="exceptions",
            target_id=str(exception_id),
            after={
                "scan_uuid": scan_uuid,
                "server_matched_line_id": result.manifest_line_id,
                "server_resolution": result.resolution.name,
            },
        )


def register_worker_routes(app: Flask, get_db) -> None:
    def resolve_lang() -> str:
        query_lang = request.args.get("lang")
        if query_lang:
            return i18n.normalize_lang(query_lang)
        cookie_lang = request.cookies.get(LANG_COOKIE_NAME)
        if cookie_lang:
            return i18n.normalize_lang(cookie_lang)
        return i18n.detect_lang_from_accept_header(request.headers.get("Accept-Language"))

    def render_join(token, shift, error, lang):
        s = i18n.strings_for(lang)
        next_path = "/w/join" + (f"?t={token}" if token else "")
        toggle_url = url_for("w_set_lang", lang=i18n.other_lang(lang), next=next_path)
        resp = make_response(
            render_template(
                "worker_join.html",
                token=token,
                shift=shift,
                error=error,
                lang=lang,
                s=s,
                lang_toggle_url=toggle_url,
            )
        )
        resp.set_cookie(LANG_COOKIE_NAME, lang, max_age=365 * 24 * 3600, samesite="Lax")
        return resp

    @app.route("/w/lang/<lang>")
    def w_set_lang(lang):
        next_url = request.args.get("next") or url_for("w_join_placeholder")
        resp = make_response(redirect(next_url))
        resp.set_cookie(
            LANG_COOKIE_NAME, i18n.normalize_lang(lang), max_age=365 * 24 * 3600, samesite="Lax"
        )
        return resp

    @app.route("/w/join")
    def w_join():
        token = request.args.get("t", "")
        lang = resolve_lang()
        conn = get_db()
        shift = db.get_shift_by_token(conn, token) if token else None
        account = db.get_account(conn, shift["account_id"]) if shift else None
        error = None
        if not shift:
            error = i18n.t(lang, "error_qr_not_recognized")
        elif shift["revoked_at"]:
            error = i18n.t(lang, "error_shift_closed")
        elif shift["token_expires_at"] < _now_iso():
            error = i18n.t(lang, "error_shift_expired")
        elif not account or account["status"] != "active":
            error = i18n.t(lang, "error_account_not_active")
            shift = None
        return render_join(token, shift, error, lang)

    @app.route("/w/join", methods=["POST"])
    def w_join_submit():
        token = request.form.get("t", "")
        name = (request.form.get("name") or "").strip()
        lang = i18n.normalize_lang(request.form.get("lang") or resolve_lang())
        conn = get_db()
        shift = db.get_shift_by_token(conn, token)
        account = db.get_account(conn, shift["account_id"]) if shift else None
        if not shift or shift["revoked_at"] or shift["token_expires_at"] < _now_iso():
            return render_join(token, None, i18n.t(lang, "error_shift_not_active"), lang), 400
        if not account or account["status"] != "active":
            return render_join(token, None, i18n.t(lang, "error_account_not_active"), lang), 400
        if not name:
            return render_join(token, shift, i18n.t(lang, "error_enter_name"), lang), 400

        worker_id = db.get_or_create_worker(conn, shift["account_id"], name)
        session_id = db.create_session(
            conn, shift["id"], worker_id, request.headers.get("User-Agent", "")
        )
        sess = _linked(db.get_session(conn, session_id), "session")
        # sid is the session's opaque token, not its integer id -- the id
        # is a small sequential number and would let anyone increment
        # their way into a different worker's session (bundle contents,
        # scan sync, appeal uploads). No lang= query param here on
        # purpose: the lang cookie was already set on the preceding GET
        # /w/join (render_join), and w_scan reads it from there via
        # resolve_lang() -- keeping this redirect's query string to just
        # sid= matches what every other route expects.
        return redirect(url_for("w_scan", sid=sess["token"]))

    @app.route("/w/scan")
    def w_scan():
        sid = request.args.get("sid")
        lang = resolve_lang()
        conn = get_db()
        sess = db.get_session_by_token(conn, sid) if sid else None
        if not sess:
            return redirect(url_for("w_join_placeholder"))
        shift = _linked(db.get_shift(conn, sess["shift_id"]), "shift")
        account = _linked(db.get_account(conn, shift["account_id"]), "account")
        return render_template(
            "worker_scan.html",
            session_id=sess["token"],
            shift=shift,
            lang=lang,
            worker_self_resolve=bool(account["worker_self_resolve"]),
        )

    @app.route("/w")
    def w_join_placeholder():
        lang = resolve_lang()
        return render_join("", None, None, lang)

    @app.route("/w/bundle/<token>")
    def w_bundle(token):
        conn = get_db()
        sess = db.get_session_by_token(conn, token)
        if not sess:
            return jsonify({"error": "session not found"}), 404
        shift = _linked(db.get_shift(conn, sess["shift_id"]), "shift")
        if shift["revoked_at"]:
            return jsonify({"error": "shift revoked"}), 403
        account = _linked(db.get_account(conn, shift["account_id"]), "account")
        manifest_ids = db.get_shift_manifest_ids(conn, shift["id"])
        payload = manifest_ingest.bundle_payload(conn, account, shift, manifest_ids)
        payload["contentHash"] = manifest_ingest.content_hash(payload)

        body = json.dumps(payload).encode()
        compressed = gzip.compress(body, compresslevel=6)
        return Response(
            compressed,
            mimetype="application/json",
            headers={"Content-Encoding": "gzip", "Cache-Control": "no-store"},
        )

    @app.route("/w/sync", methods=["POST"])
    def w_sync():
        if OFFLINE_DRILL_BLOCK_UNTIL and time.time() < OFFLINE_DRILL_BLOCK_UNTIL:
            return jsonify({"error": "offline drill: sync intentionally blocked"}), 503

        conn = get_db()
        payload = request.get_json(force=True, silent=True) or {}
        session_id = payload.get("sessionId")
        client_now = payload.get("clientNow")
        scans = payload.get("scans", [])

        sess = db.get_session_by_token(conn, session_id) if session_id else None
        if not sess:
            return jsonify({"error": "unknown session"}), 404

        # Revoking a shift is the documented response to a leaked door QR,
        # and /shifts/<id>/revoke tells the owner workers "can no longer
        # join or sync with it" -- so enforce that here, not just at join
        # time. Deliberately keyed on revoked_at only, NOT on
        # token_expires_at: a phone that was offline past the shift's
        # natural expiry still needs to drain its outbox, and dropping
        # those scans would break the offline-first promise. Rejected
        # scans stay queued client-side rather than being destroyed.
        shift_now = _linked(db.get_shift(conn, sess["shift_id"]), "shift")
        if shift_now["revoked_at"]:
            return jsonify({"error": "shift revoked"}), 403

        server_now = _now_iso()
        skew_ms = None
        if client_now:
            try:
                c = datetime.strptime(client_now, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
                s = datetime.strptime(server_now, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
                skew_ms = int((s - c).total_seconds() * 1000)
            except ValueError:
                skew_ms = None
        db.touch_session(conn, sess["id"], clock_skew_ms=skew_ms)

        accepted = []
        rejected = []
        for scan in scans[:200]:  # outbox drains in batches of 200; enforce server-side too
            uuid_val = str(scan.get("uuid", ""))
            # A malformed entry is reported in `rejected` rather than
            # silently skipped: the outbox only drops what the server
            # names, so anything we neither accept nor reject would be
            # retried forever with no way for the worker to find out.
            if not UUID_RE.match(uuid_val) or not _scan_payload_is_valid(scan):
                if uuid_val:
                    rejected.append(uuid_val)
                continue

            # One transaction per scan: the scan row, the exception it
            # raises, and the billing event derived from it must land
            # together or not at all -- a billed scan with no exception
            # row (or vice versa) is exactly the kind of drift the
            # append-only ledger exists to prevent.
            with db.transaction(conn):
                inserted = db.insert_scan(
                    conn,
                    {
                        "uuid": scan["uuid"],
                        "session_id": sess["id"],
                        "manifest_line_id": scan.get("manifestLineId"),
                        "raw_payload": scan["rawPayload"],
                        "normalized": scan["normalized"],
                        "matched_tier": scan.get("matchedTier"),
                        "result": scan["result"],
                        "decode_ms": scan.get("decodeMs"),
                        "match_ms": scan.get("matchMs"),
                        "ts_client": scan["tsClient"],
                        "bundle_version": scan.get("bundleVersion", 0),
                        "seq": scan.get("seq"),
                        "device_ua": request.headers.get("User-Agent", ""),
                    },
                )
                if inserted and scan["result"] == "unresolved":
                    db.create_exception(conn, scan["uuid"], "unresolved")
                elif inserted and scan.get("needsConfirmation"):
                    db.create_exception(
                        conn, scan["uuid"], "tier6_confirm", scan.get("manifestLineId")
                    )
                elif inserted and scan["result"] == "reject":  # noqa: SIM102
                    # Mid-shift manifest changes: a scan made against a bundle
                    # version older than the shift's current one is recorded
                    # for audit, but not billed until the phone refreshes --
                    # see the "Bundle stale" tradeoff in the README.
                    if scan.get("bundleVersion", 0) >= shift_now["bundle_version"]:
                        _bill_confirmed_reject(conn, shift_now, scan["uuid"], scan["rawPayload"])
            accepted.append(scan["uuid"])

        return jsonify(
            {
                "accepted": accepted,
                "rejected": rejected,
                "serverTime": server_now,
                "bundleVersion": shift_now["bundle_version"],
                "clockSkewMs": skew_ms,
            }
        )

    @app.route("/w/heartbeat/<token>")
    def w_heartbeat(token):
        conn = get_db()
        sess = db.get_session_by_token(conn, token)
        if not sess:
            return jsonify({"error": "unknown session"}), 404
        shift = _linked(db.get_shift(conn, sess["shift_id"]), "shift")
        return jsonify(
            {
                "serverTime": _now_iso(),
                "bundleVersion": shift["bundle_version"],
                "revoked": bool(shift["revoked_at"]),
                "drillBlocked": bool(
                    OFFLINE_DRILL_BLOCK_UNTIL and time.time() < OFFLINE_DRILL_BLOCK_UNTIL
                ),
            }
        )

    @app.route("/w/session-summary/<token>")
    def w_session_summary(token):
        # Powers the worker's own end-of-shift summary -- factual counts
        # only, no dollar figures (that framing is for the owner's
        # "Workers" page; a worker's own screen stays neutral, not punitive).
        conn = get_db()
        sess = db.get_session_by_token(conn, token)
        if not sess:
            return jsonify({"error": "unknown session"}), 404
        stats = db.worker_session_stats(conn, sess["id"])
        return jsonify(
            {
                "totalScans": stats["total_scans"] or 0,
                "okCount": stats["ok_count"] or 0,
                "rejectCount": stats["reject_count"] or 0,
                "duplicateCount": stats["duplicate_count"] or 0,
                "unresolvedCount": stats["unresolved_count"] or 0,
            }
        )

    @app.route("/w/appeal", methods=["POST"])
    def w_appeal():
        # A worker who disagrees with a REJECT can appeal it (only shown
        # client-side when the account's worker_self_resolve is on), but a
        # photo is mandatory -- this is a billing-integrity review queue,
        # not a one-tap override. See db.create_worker_appeal (idempotent:
        # the phone may retry this upload like any other queued item) and
        # billing.reverse_if_billed, called if the owner later approves it.
        conn = get_db()
        session_id = request.form.get("sessionId")
        scan_uuid = request.form.get("scanUuid")
        note = (request.form.get("note") or "").strip() or None
        photo = request.files.get("photo")

        if not session_id or not scan_uuid or not photo:
            return jsonify({"error": "sessionId, scanUuid, and a photo are required"}), 400

        # scan_uuid ends up in a filesystem path below -- an unvalidated
        # value here is a path-traversal / arbitrary-file-write primitive,
        # not just a bad id, so reject anything that isn't a real UUID
        # before it touches the DB or the filesystem.
        if not UUID_RE.match(scan_uuid):
            return jsonify({"error": "invalid scanUuid"}), 400

        sess = db.get_session_by_token(conn, session_id)
        if not sess:
            return jsonify({"error": "unknown session"}), 404

        scan = db.get_scan(conn, scan_uuid)
        if not scan:
            # The scan itself may not have synced yet (offline-first: scan
            # sync and appeal upload are independent queues). Ask the
            # client to retry once the normal outbox sync has caught up.
            return jsonify({"error": "scan not yet synced, retry shortly"}), 409

        # The scan must belong to the same shift as the appealing session.
        # Without this, any valid session could raise a worker_reported
        # exception (with an attacker-supplied photo and note) against any
        # scan in the database, including another account's.
        scan_sess = db.get_session(conn, scan["session_id"])
        if not scan_sess or scan_sess["shift_id"] != sess["shift_id"]:
            return jsonify({"error": "scan does not belong to this session"}), 403

        photos_dir = Path(app.config["APPEAL_PHOTOS_DIR"])
        photos_dir.mkdir(parents=True, exist_ok=True)
        ext = Path(photo.filename or "").suffix.lower()
        if ext not in ALLOWED_PHOTO_EXTS:
            ext = ".jpg"
        photo_path = photos_dir / f"{scan_uuid}{ext}"
        photo.save(photo_path)

        exception_id = db.create_worker_appeal(conn, scan_uuid, str(photo_path), note)
        if exception_id is not None:
            # Only on a genuinely new exception, not a resubmission -- the
            # offline outbox retries this upload like any other queued
            # item, and create_worker_appeal is idempotent specifically so
            # those retries are silent no-ops; logging each one would just
            # be retry noise, not a new event.
            worker = db.get_worker(conn, sess["worker_id"])
            db.record_audit(
                conn,
                actor=f"worker:{worker['display_name']}" if worker else "worker:unknown",
                action="scan.appeal_submitted",
                target_table="exceptions",
                target_id=str(exception_id),
                after={"scan_uuid": scan_uuid, "note": note, "session_id": sess["id"]},
            )
        return jsonify(
            {"ok": True, "exceptionId": exception_id, "alreadySubmitted": exception_id is None}
        )

    @app.route("/w/manifest.webmanifest")
    def w_manifest():
        manifest = {
            "name": "Autorack Verify",
            "short_name": "Verify",
            "start_url": "/w",
            "scope": "/w/",
            "display": "standalone",
            "background_color": "#14171a",
            "theme_color": "#14171a",
            "icons": [
                {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
                {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
                {
                    "src": "/static/icons/icon-512-maskable.png",
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "maskable",
                },
            ],
        }
        return Response(json.dumps(manifest), mimetype="application/manifest+json")

    @app.route("/w/sw.js")
    def w_service_worker():
        sw_path = Path(app.root_path) / "static" / "js" / "worker" / "sw.js"
        return Response(sw_path.read_text(), mimetype="application/javascript")


def set_offline_drill_block(seconds: float | None) -> None:
    """Used by serve.py --offline-drill to hard-block /w/sync for a window,
    proving the offline path actually survives instead of assuming it."""
    global OFFLINE_DRILL_BLOCK_UNTIL
    OFFLINE_DRILL_BLOCK_UNTIL = time.time() + seconds if seconds else None


# ---------------------------------------------------------------------------
# Owner web app: auth, manifest upload, shift prep, wall QR, live floor
# view, exception review.
# ---------------------------------------------------------------------------


LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 300

# Rate-limit bucket names -- see db.count_rate_limit_events.
RATE_BUCKET_LOGIN = "login"
RATE_BUCKET_SIGNUP = "signup"


def register_owner_routes(app: Flask, get_db) -> None:
    # Login throttling lives in the database (rate_limit_events), not in
    # a per-process dict. In-memory state reset on every restart and was
    # silently multiplied by worker count under the documented
    # `gunicorn --workers 4` deployment, which made the configured limit
    # roughly meaningless the moment this is exposed publicly. Keyed on
    # "email|ip" so one abusive IP can't lock a real user out of their
    # own account.
    def _login_key(email: str) -> str:
        return f"{email.lower()}|{request.remote_addr or ''}"

    def _login_locked_out(key: str) -> bool:
        return (
            db.count_rate_limit_events(get_db(), RATE_BUCKET_LOGIN, key, LOGIN_LOCKOUT_SECONDS)
            >= LOGIN_MAX_ATTEMPTS
        )

    def _record_login_failure(key: str) -> None:
        db.record_rate_limit_event(get_db(), RATE_BUCKET_LOGIN, key)

    def current_session():
        token = request.cookies.get(auth.SESSION_COOKIE_NAME)
        return auth.current_user(get_db(), token), token

    def login_required(view):
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            ctx, _token = current_session()
            if not ctx:
                return redirect(url_for("login"))
            g.account = ctx["account"]
            g.user = ctx["user"]
            return view(*args, **kwargs)

        return wrapped

    def owner_only(view):
        # users.role is 'owner' or 'manager'; billing is the one place
        # today where the two must not be equivalent. Stacks under
        # @login_required (which sets g.user), so it always runs second:
        # @app.route(...) / @login_required / @owner_only, top to bottom.
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            if g.user["role"] != "owner":
                return "Forbidden -- billing is owner-only.", 403
            return view(*args, **kwargs)

        return wrapped

    @app.route("/login")
    def login():
        ctx, _token = current_session()
        if ctx:
            return redirect(url_for("dashboard"))
        return render_template("login.html", error=None, email=None)

    @app.route("/login", methods=["POST"])
    def login_submit():
        conn = get_db()
        email = (request.form.get("email") or "").strip()
        password = request.form.get("password") or ""
        key = _login_key(email)
        if _login_locked_out(key):
            db.record_audit(
                conn,
                actor=email,
                action="user.login_locked_out",
                target_table="users",
                ip=request.remote_addr,
            )
            return render_template(
                "login.html",
                error="Too many failed attempts. Try again in a few minutes.",
                email=email,
            ), 429
        user = db.get_user_by_email(conn, email)
        if not user or not auth.verify_password(password, user["pw_hash"]):
            _record_login_failure(key)
            db.record_audit(
                conn,
                actor=email,
                action="user.login_failed",
                target_table="users",
                target_id=str(user["id"]) if user else None,
                ip=request.remote_addr,
            )
            return render_template("login.html", error="Wrong email or password.", email=email), 400
        db.clear_rate_limit_events(conn, RATE_BUCKET_LOGIN, key)
        db.record_audit(
            conn,
            actor=user["email"],
            action="user.login",
            target_table="users",
            target_id=str(user["id"]),
            ip=request.remote_addr,
        )
        token = auth.start_session(conn, user["id"], user["account_id"])
        resp = make_response(redirect(url_for("dashboard")))
        resp.set_cookie(
            auth.SESSION_COOKIE_NAME,
            token,
            httponly=True,
            secure=True,
            samesite="Lax",
            max_age=auth.SESSION_LIFETIME_HOURS * 3600,
        )
        return resp

    @app.route("/impersonate/<token>")
    def impersonate(token):
        # Redeemed single-use, five-minute-expiry links generated by the
        # operator GUI's Accounts tab. Redemption is logged with the
        # redeeming IP (impersonation_tokens.redeemed_ip), and starting the
        # resulting web session is itself an auditable action.
        conn = get_db()
        row = db.redeem_impersonation_token(conn, token, request.remote_addr or "")
        if not row:
            flash("This impersonation link is invalid, already used, or has expired.")
            return redirect(url_for("login"))
        user_id = row["user_id"]
        if not user_id:
            user = db.query_one(
                conn,
                SQL["users.first_user_for_account"],
                (row["account_id"],),
            )
            user_id = user["id"] if user else None
        if not user_id:
            flash("This account has no user to impersonate.")
            return redirect(url_for("login"))

        db.record_audit(
            conn,
            actor="operator-gui",
            action="impersonate.redeem",
            target_table="accounts",
            target_id=str(row["account_id"]),
            after={"user_id": user_id, "ip": request.remote_addr},
            ip=request.remote_addr,
        )
        token_val = auth.start_session(conn, user_id, row["account_id"])
        resp = make_response(redirect(url_for("dashboard")))
        resp.set_cookie(
            auth.SESSION_COOKIE_NAME,
            token_val,
            httponly=True,
            secure=True,
            samesite="Lax",
            max_age=auth.SESSION_LIFETIME_HOURS * 3600,
        )
        return resp

    @app.route("/logout", methods=["POST"])
    def logout():
        ctx, token = current_session()
        if ctx:
            db.record_audit(
                get_db(),
                actor=ctx["user"]["email"],
                action="user.logout",
                target_table="users",
                target_id=str(ctx["user"]["id"]),
                ip=request.remote_addr,
            )
        auth.end_session(get_db(), token)
        resp = make_response(redirect(url_for("login")))
        resp.delete_cookie(auth.SESSION_COOKIE_NAME)
        return resp

    @app.route("/dashboard")
    @login_required
    def dashboard():
        conn = get_db()
        account_id = g.account["id"]
        # "Today" is the account's local day, not the UTC one -- see
        # tz.local_day_bounds_utc.
        day_start, day_end = tz.local_day_bounds_utc(g.account["timezone"])
        stats = {
            "manifests_committed": len(db.list_committed_manifests(conn, account_id)),
            "active_shifts": db.count_active_shifts(conn, account_id),
            "scans_today": db.count_scans_today(conn, account_id, day_start, day_end),
            "open_exceptions": db.count_open_exceptions(conn, account_id),
            "recent_exceptions": db.list_open_exceptions(conn, account_id)[:5],
        }
        return render_template("dashboard.html", active="dashboard", account=g.account, stats=stats)

    @app.route("/manifests")
    @login_required
    def manifests_list():
        conn = get_db()
        manifests = db.list_manifests(conn, g.account["id"])
        return render_template(
            "manifests_list.html", active="manifests", account=g.account, manifests=manifests
        )

    @app.route("/manifests/upload")
    @login_required
    def manifest_upload_form():
        return render_template(
            "manifest_upload.html", active="manifests", account=g.account, ref=None
        )

    @app.route("/manifests/upload", methods=["POST"])
    @login_required
    def manifest_upload_submit():
        ref = (request.form.get("ref") or "").strip()
        paste = request.form.get("paste") or ""
        uploaded = request.files.get("file")

        if uploaded and uploaded.filename:
            raw_text = uploaded.read().decode("utf-8", errors="replace")
            parsed = manifest_ingest.parse_delimited(raw_text, uploaded.filename)
            mode = "delimited"
            source_filename = uploaded.filename
        elif paste.strip():
            raw_text = paste
            parsed = manifest_ingest.parse_paste(raw_text)
            mode = "paste"
            source_filename = None
        else:
            flash("Choose a file or paste some barcodes first.")
            return redirect(url_for("manifest_upload_form"))

        header = manifest_ingest.parse_header(raw_text) if mode == "delimited" else []

        return render_template(
            "manifest_preview.html",
            active="manifests",
            account=g.account,
            ref=ref,
            source_filename=source_filename,
            mode=mode,
            raw_text=raw_text,
            header=header,
            parsed=parsed,
        )

    @app.route("/manifests/commit", methods=["POST"])
    @login_required
    def manifest_commit():
        conn = get_db()
        ref = request.form.get("ref") or "untitled"
        source_filename = request.form.get("source_filename") or None
        mode = request.form.get("mode")
        raw_text = request.form.get("raw_text") or ""

        if mode == "delimited":
            mapping = {
                "raw_barcode": request.form.get("col_raw_barcode") or None,
                "sku": request.form.get("col_sku") or None,
                "description": request.form.get("col_description") or None,
                "qty_expected": request.form.get("col_qty_expected") or None,
            }
            parsed = manifest_ingest.reparse_with_mapping(raw_text, mapping)
        else:
            parsed = manifest_ingest.parse_paste(raw_text)

        if not parsed.rows:
            flash("No barcode rows found -- nothing was committed.")
            return redirect(url_for("manifest_upload_form"))

        account = g.account
        manifest_id, report = manifest_ingest.commit_manifest(
            conn,
            account["id"],
            ref,
            source_filename,
            parsed.rows,
            loose_match_enabled=bool(account["loose_match_enabled"]),
            loose_suffix_len=account["loose_suffix_len"],
        )
        # Logged after, not before: commit_manifest is already atomic (see
        # its own db.transaction() wrapping), so there's no partial-create
        # state to protect against -- it either fully exists by the time
        # we get here, with a real id to attach, or this line never runs.
        # Every later edit to one of its lines already carries manifest_id
        # in its own audit entry; this is the one event that was missing
        # from that trail -- the creation itself.
        db.record_audit(
            conn,
            actor=g.user["email"],
            action="manifest.commit",
            target_table="manifests",
            target_id=str(manifest_id),
            after={"ref": ref, "source_filename": source_filename, "line_count": len(parsed.rows)},
        )
        for w in report.warnings():
            flash(w)
        flash(f"Committed manifest '{ref}' with {len(parsed.rows)} lines.")
        return redirect(url_for("manifest_detail", manifest_id=manifest_id))

    LINES_PER_PAGE = 50

    def _owned_manifest_or_404(conn, manifest_id):
        manifest = db.get_manifest(conn, manifest_id)
        if not manifest or manifest["account_id"] != g.account["id"]:
            return None
        return manifest

    def _regenerate_manifest_keys(conn, manifest_id):
        account = g.account
        manifest_ingest.regenerate_keys(
            conn, manifest_id, bool(account["loose_match_enabled"]), account["loose_suffix_len"]
        )

    @app.route("/manifests/<int:manifest_id>")
    @login_required
    def manifest_detail(manifest_id):
        conn = get_db()
        manifest = _owned_manifest_or_404(conn, manifest_id)
        if not manifest:
            return "Not found", 404
        try:
            page = max(int(request.args.get("page", 1)), 1)
        except ValueError:
            page = 1
        offset = (page - 1) * LINES_PER_PAGE
        lines, total = db.get_manifest_lines_page(
            conn, manifest_id, limit=LINES_PER_PAGE, offset=offset
        )
        pages = max(1, (total + LINES_PER_PAGE - 1) // LINES_PER_PAGE)
        return render_template(
            "manifest_detail.html",
            active="manifests",
            account=g.account,
            manifest=manifest,
            lines=lines,
            page=page,
            pages=pages,
            total=total,
        )

    @app.route("/manifests/<int:manifest_id>/history")
    @login_required
    def manifest_history(manifest_id):
        # All of this data already existed in audit_log the moment
        # manifest_line.add/edit/delete started calling record_audit; the
        # gap was never the logging, just that nothing surfaced it in the
        # owner app (previously visible only via the operator GUI's raw
        # SQL console).
        conn = get_db()
        manifest = _owned_manifest_or_404(conn, manifest_id)
        if not manifest:
            return "Not found", 404
        entries = db.list_audit_log_for_manifest(conn, manifest_id)
        return render_template(
            "manifest_history.html",
            active="manifests",
            account=g.account,
            manifest=manifest,
            entries=entries,
        )

    @app.route("/manifests/<int:manifest_id>/lines/add", methods=["POST"])
    @login_required
    def manifest_line_add(manifest_id):
        conn = get_db()
        manifest = _owned_manifest_or_404(conn, manifest_id)
        if not manifest:
            return "Not found", 404
        raw_barcode = (request.form.get("raw_barcode") or "").strip()
        if not raw_barcode:
            flash("Barcode is required.")
            return redirect(url_for("manifest_detail", manifest_id=manifest_id))
        sku = (request.form.get("sku") or "").strip() or None
        description = (request.form.get("description") or "").strip() or None
        try:
            qty_expected = int(request.form.get("qty_expected") or 1)
        except ValueError:
            qty_expected = 1

        db.record_audit(
            conn,
            actor=g.user["email"],
            action="manifest_line.add",
            target_table="manifest_lines",
            target_id=None,
            after={"manifest_id": manifest_id, "raw_barcode": raw_barcode, "sku": sku},
        )
        db.add_manifest_line(conn, manifest_id, sku, description, qty_expected, raw_barcode)
        _regenerate_manifest_keys(conn, manifest_id)
        flash("Line added.")
        return redirect(url_for("manifest_detail", manifest_id=manifest_id))

    @app.route("/manifests/<int:manifest_id>/lines/<int:line_id>/edit", methods=["POST"])
    @login_required
    def manifest_line_edit(manifest_id, line_id):
        conn = get_db()
        manifest = _owned_manifest_or_404(conn, manifest_id)
        if not manifest:
            return "Not found", 404
        line = db.get_manifest_line(conn, line_id)
        if not line or line["manifest_id"] != manifest_id:
            return "Not found", 404
        page = request.form.get("page", 1)
        raw_barcode = (request.form.get("raw_barcode") or "").strip()
        if not raw_barcode:
            flash("Barcode is required.")
            return redirect(url_for("manifest_detail", manifest_id=manifest_id, page=page))
        sku = (request.form.get("sku") or "").strip() or None
        description = (request.form.get("description") or "").strip() or None
        try:
            qty_expected = int(request.form.get("qty_expected") or 1)
        except ValueError:
            qty_expected = 1

        db.record_audit(
            conn,
            actor=g.user["email"],
            action="manifest_line.edit",
            target_table="manifest_lines",
            target_id=str(line_id),
            before=dict(line),
            after={
                "sku": sku,
                "description": description,
                "qty_expected": qty_expected,
                "raw_barcode": raw_barcode,
            },
        )
        db.update_manifest_line(conn, line_id, sku, description, qty_expected, raw_barcode)
        _regenerate_manifest_keys(conn, manifest_id)
        flash("Line updated.")
        return redirect(url_for("manifest_detail", manifest_id=manifest_id, page=page))

    @app.route("/manifests/<int:manifest_id>/lines/<int:line_id>/delete", methods=["POST"])
    @login_required
    def manifest_line_delete(manifest_id, line_id):
        conn = get_db()
        manifest = _owned_manifest_or_404(conn, manifest_id)
        if not manifest:
            return "Not found", 404
        line = db.get_manifest_line(conn, line_id)
        if not line or line["manifest_id"] != manifest_id:
            return "Not found", 404
        page = request.form.get("page", 1)

        db.record_audit(
            conn,
            actor=g.user["email"],
            action="manifest_line.delete",
            target_table="manifest_lines",
            target_id=str(line_id),
            before=dict(line),
        )
        db.delete_manifest_line(conn, line_id)
        _regenerate_manifest_keys(conn, manifest_id)
        flash("Line deleted.")
        return redirect(url_for("manifest_detail", manifest_id=manifest_id, page=page))

    @app.route("/shifts")
    @login_required
    def shifts_list():
        conn = get_db()
        shifts = db.list_shifts(conn, g.account["id"])
        return render_template(
            "shifts_list.html", active="shifts", account=g.account, shifts=shifts, now=_now_iso()
        )

    @app.route("/shifts/new")
    @login_required
    def shift_new_form():
        conn = get_db()
        manifests = db.list_committed_manifests(conn, g.account["id"])
        return render_template(
            "shift_new.html",
            active="shifts",
            account=g.account,
            manifests=manifests,
            today=tz.today_local(g.account["timezone"]),
        )

    @app.route("/shifts/prepare", methods=["POST"])
    @login_required
    def shift_prepare():
        conn = get_db()
        label = request.form.get("label") or "Shift"
        shift_date = request.form.get("date") or tz.today_local(g.account["timezone"])
        manifest_ids = [int(x) for x in request.form.getlist("manifest_ids")]
        if not manifest_ids:
            flash("Pick at least one manifest for this shift.")
            return redirect(url_for("shift_new_form"))

        token = secrets.token_urlsafe(24)
        expires = (datetime.now(UTC) + timedelta(hours=16)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        account = g.account
        payload = manifest_ingest.bundle_payload(
            conn,
            account,
            {"id": None, "label": label, "date": shift_date, "bundle_version": 0},
            manifest_ids,
        )
        bundle_hash = manifest_ingest.content_hash(payload)
        shift_id = db.create_shift(
            conn, account["id"], label, shift_date, token, expires, bundle_hash, 0
        )
        for mid in manifest_ids:
            db.link_shift_manifest(conn, shift_id, mid)

        db.record_audit(
            conn,
            actor=g.user["email"],
            action="shift.prepare",
            target_table="shifts",
            target_id=str(shift_id),
            after={"label": label, "manifest_ids": manifest_ids},
        )
        return redirect(url_for("shift_detail", shift_id=shift_id))

    @app.route("/shifts/<int:shift_id>")
    @login_required
    def shift_detail(shift_id):
        conn = get_db()
        shift = db.get_shift(conn, shift_id)
        if not shift or shift["account_id"] != g.account["id"]:
            return "Not found", 404
        sessions = db.list_sessions_for_shift(conn, shift_id)
        join_url = request.host_url.rstrip("/") + url_for("w_join") + f"?t={shift['token']}"
        return render_template(
            "shift_detail.html",
            active="shifts",
            account=g.account,
            shift=shift,
            sessions=sessions,
            join_url=join_url,
        )

    @app.route("/shifts/<int:shift_id>/qr.png")
    @login_required
    def shift_qr(shift_id):
        conn = get_db()
        shift = db.get_shift(conn, shift_id)
        if not shift or shift["account_id"] != g.account["id"]:
            return "Not found", 404
        join_url = request.host_url.rstrip("/") + url_for("w_join") + f"?t={shift['token']}"
        qr = segno.make(join_url)
        buf = io.BytesIO()
        qr.save(buf, kind="png", scale=8, border=2, dark="#14171a", light="#ffffff")
        buf.seek(0)
        return Response(buf.read(), mimetype="image/png")

    @app.route("/shifts/<int:shift_id>/revoke", methods=["POST"])
    @login_required
    def shift_revoke(shift_id):
        conn = get_db()
        shift = db.get_shift(conn, shift_id)
        if not shift or shift["account_id"] != g.account["id"]:
            return "Not found", 404
        db.record_audit(
            conn,
            actor=g.user["email"],
            action="shift.revoke",
            target_table="shifts",
            target_id=str(shift_id),
            before=dict(shift),
        )
        db.revoke_shift(conn, shift_id)
        flash("Shift token revoked. Workers can no longer join or sync with it.")
        return redirect(url_for("shift_detail", shift_id=shift_id))

    @app.route("/floor")
    @login_required
    def floor_view():
        return render_template("floor_view.html", active="floor", account=g.account)

    @app.route("/floor/data")
    @login_required
    def floor_data():
        conn = get_db()
        rows = db.recent_scans(conn, g.account["id"], limit=100)
        account_tz = g.account["timezone"]
        out = []
        for r in rows:
            line = (
                db.query_one(
                    conn,
                    SQL["manifests.get_manifest_line_label"],
                    (r["manifest_line_id"],),
                )
                if r["manifest_line_id"]
                else None
            )
            out.append(
                {
                    "ts_server": tz.to_local(r["ts_server"], account_tz),
                    "result": r["result"],
                    "raw_payload": r["raw_payload"],
                    "matched_tier": r["matched_tier"],
                    "sku": line["sku"] if line else None,
                    "description": line["description"] if line else None,
                }
            )
        return jsonify({"scans": out})

    @app.route("/workers")
    @login_required
    def workers_stats():
        conn = get_db()
        rows = db.worker_stats_for_account(conn, g.account["id"])
        workers = []
        for r in rows:
            total = r["total_scans"] or 0
            reject_rate = f"{(r['reject_count'] / total * 100):.0f}%" if total else "--"
            workers.append(
                {
                    "id": r["worker_id"],
                    "display_name": r["display_name"],
                    "active": bool(r["active"]),
                    "total_scans": total,
                    "ok_count": r["ok_count"] or 0,
                    "reject_count": r["reject_count"] or 0,
                    "duplicate_count": r["duplicate_count"] or 0,
                    "unresolved_count": r["unresolved_count"] or 0,
                    "reject_rate": reject_rate,
                    "billed_amount": pricing.format_cents_as_dollars(r["billed_cents"] or 0),
                }
            )
        return render_template(
            "workers_stats.html", active="workers", account=g.account, workers=workers
        )

    def _owned_worker_or_404(conn, worker_id):
        worker = db.get_worker(conn, worker_id)
        if not worker or worker["account_id"] != g.account["id"]:
            return None
        return worker

    @app.route("/workers/<int:worker_id>/rename", methods=["POST"])
    @login_required
    def worker_rename(worker_id):
        conn = get_db()
        worker = _owned_worker_or_404(conn, worker_id)
        if not worker:
            return "Not found", 404
        new_name = (request.form.get("display_name") or "").strip()
        if not new_name:
            flash("Enter a name.")
            return redirect(url_for("workers_stats"))
        db.record_audit(
            conn,
            actor=g.user["email"],
            action="worker.rename",
            target_table="workers",
            target_id=str(worker_id),
            before={"display_name": worker["display_name"]},
            after={"display_name": new_name},
        )
        db.rename_worker(conn, worker_id, new_name)
        flash(f"Renamed to {new_name}.")
        return redirect(url_for("workers_stats"))

    @app.route("/workers/<int:worker_id>/toggle-active", methods=["POST"])
    @login_required
    def worker_toggle_active(worker_id):
        conn = get_db()
        worker = _owned_worker_or_404(conn, worker_id)
        if not worker:
            return "Not found", 404
        new_active = not bool(worker["active"])
        db.record_audit(
            conn,
            actor=g.user["email"],
            action="worker.set_active",
            target_table="workers",
            target_id=str(worker_id),
            before={"active": bool(worker["active"])},
            after={"active": new_active},
        )
        db.set_worker_active(conn, worker_id, new_active)
        flash(f"{worker['display_name']} marked {'active' if new_active else 'inactive'}.")
        return redirect(url_for("workers_stats"))

    @app.route("/workers/<int:worker_id>/merge", methods=["POST"])
    @login_required
    def worker_merge(worker_id):
        conn = get_db()
        from_worker = _owned_worker_or_404(conn, worker_id)
        if not from_worker:
            return "Not found", 404
        into_worker_id = request.form.get("into_worker_id")
        if not into_worker_id:
            flash("Choose which worker to merge into.")
            return redirect(url_for("workers_stats"))
        into_id = int(into_worker_id)
        into_worker = _owned_worker_or_404(conn, into_id)
        if not into_worker or into_id == worker_id:
            flash("Choose a different, valid worker to merge into.")
            return redirect(url_for("workers_stats"))

        db.record_audit(
            conn,
            actor=g.user["email"],
            action="worker.merge",
            target_table="workers",
            target_id=str(worker_id),
            before={"from": from_worker["display_name"], "into": into_worker["display_name"]},
        )
        db.merge_workers(conn, worker_id, into_id)
        flash(f"Merged {from_worker['display_name']} into {into_worker['display_name']}.")
        return redirect(url_for("workers_stats"))

    @app.route("/exceptions")
    @login_required
    def exceptions_list():
        conn = get_db()
        exceptions = db.list_open_exceptions(conn, g.account["id"])
        return render_template(
            "exceptions_list.html", active="exceptions", account=g.account, exceptions=exceptions
        )

    @app.route("/manifests/lines/search")
    @login_required
    def manifest_lines_search():
        # Backs the exception-resolution picker's type-ahead (see
        # exceptions_list.html) -- replaces what used to be a flat
        # <select> of up to 500 lines, which never scaled to a real
        # 15k-line manifest. GET + read-only, so no CSRF token needed.
        conn = get_db()
        q = (request.args.get("q") or "").strip()
        if len(q) < 2:
            return jsonify({"lines": []})
        lines = db.search_manifest_lines_for_account(conn, g.account["id"], q)
        return jsonify(
            {
                "lines": [
                    {
                        "id": ln["id"],
                        "sku": ln["sku"],
                        "description": ln["description"],
                        "raw_barcode": ln["raw_barcode"],
                    }
                    for ln in lines
                ]
            }
        )

    def _owned_exception_or_404(conn, exception_id):
        # An exception has no account_id of its own -- ownership runs
        # through the scan that raised it (scan -> session -> shift ->
        # account), the same path /exceptions/<id>/photo already walks.
        # Without this, any logged-in owner could resolve, close, or
        # reverse the billing on another account's exceptions just by
        # guessing the sequential id.
        row = db.query_one(
            conn,
            SQL["exceptions.get_owned_exception"],
            (exception_id, g.account["id"]),
        )
        return row

    @app.route("/exceptions/<int:exception_id>/resolve", methods=["POST"])
    @login_required
    def exception_resolve(exception_id):
        conn = get_db()
        manifest_line_id = request.form.get("manifest_line_id")
        remember = request.form.get("remember_alias") == "1"
        if not manifest_line_id:
            flash("Choose a manifest line to resolve to.")
            return redirect(url_for("exceptions_list"))
        try:
            line_id = int(manifest_line_id)
        except ValueError:
            flash("Choose a manifest line to resolve to.")
            return redirect(url_for("exceptions_list"))

        exc_row = _owned_exception_or_404(conn, exception_id)
        if not exc_row:
            return "Not found", 404

        # The target line must belong to this account too -- otherwise an
        # exception could be resolved (and aliased) against another
        # account's manifest data.
        line_owned = db.query_one(
            conn,
            SQL["manifests.get_owned_manifest_line"],
            (manifest_line_id, g.account["id"]),
        )
        if not line_owned:
            return "Not found", 404

        # record_audit MUST commit before the mutation it describes, on its
        # own, independent of whether that mutation goes on to succeed --
        # that's the whole point of an audit trail surviving a failure.
        # It therefore stays outside the transaction below: resolving the
        # exception and reversing its billing charge need to be atomic
        # *with each other*, but not with the audit row, which needs to be
        # durable even if one of them then fails.
        db.record_audit(
            conn,
            actor=g.user["email"],
            action="exception.resolve",
            target_table="exceptions",
            target_id=str(exception_id),
            before=dict(exc_row),
        )

        # Resolving and reversing the charge it invalidates are one
        # decision -- an exception marked resolved while its catch stays
        # billed silently overcharges, with nothing left in the queue to
        # show why.
        with db.transaction(conn):
            db.resolve_exception(
                conn,
                exception_id,
                g.user["email"],
                "Resolved by owner",
                manifest_line_id=line_id,
            )

            if remember:
                scan = db.get_scan(conn, exc_row["scan_uuid"])
                if scan and line_owned["sku"]:
                    db.add_alias(
                        conn,
                        g.account["id"],
                        scan["normalized"],
                        line_owned["sku"],
                        g.user["email"],
                    )

            # Resolving to a real line means this scan was NOT actually a
            # mis-ship after all -- if it was already billed as a 'reject'
            # catch (the common case for a worker's photo appeal), reverse
            # that charge. A no-op if it was never billed.
            billing.reverse_if_billed(
                conn,
                exc_row["scan_uuid"],
                f"Exception resolved to a real manifest line by {g.user['email']}",
                g.user["email"],
            )

        flash("Exception resolved.")
        return redirect(url_for("exceptions_list"))

    @app.route("/exceptions/<int:exception_id>/photo")
    @login_required
    def exception_photo(exception_id):
        conn = get_db()
        row = db.query_one(
            conn,
            SQL["exceptions.get_owned_exception_photo"],
            (exception_id, g.account["id"]),
        )
        if not row or not row["photo_path"]:
            return "Not found", 404
        photo_path = Path(row["photo_path"])
        if not photo_path.exists():
            return "Not found", 404
        mimetype = "image/png" if photo_path.suffix.lower() == ".png" else "image/jpeg"
        return Response(photo_path.read_bytes(), mimetype=mimetype)

    @app.route("/exceptions/<int:exception_id>/confirm-reject", methods=["POST"])
    @login_required
    def exception_confirm_reject(exception_id):
        # The owner determines an ambiguous scan really was a wrong item --
        # no manifest line to alias, just a confirmed catch. This is the
        # ONLY path that makes an 'unresolved' scan billable (see the
        # billing_events trigger and billing.confirm_exception_as_billable_catch).
        conn = get_db()
        exc_row = _owned_exception_or_404(conn, exception_id)
        if not exc_row:
            return "Not found", 404

        # Same ordering rule as exception_resolve above: the audit row
        # must be durable on its own before the mutation it describes, not
        # bundled into the same transaction as the thing that could fail.
        db.record_audit(
            conn,
            actor=g.user["email"],
            action="exception.confirm_reject",
            target_table="exceptions",
            target_id=str(exception_id),
            before=dict(exc_row),
        )

        # The resolution is what authorises the charge (the billing_events
        # trigger requires it), so the two must commit together or the
        # ledger and the review queue disagree about why money was owed.
        with db.transaction(conn):
            db.resolve_exception(
                conn, exception_id, g.user["email"], "Confirmed as a genuine wrong item"
            )
            billing.confirm_exception_as_billable_catch(conn, g.account["id"], exc_row["scan_uuid"])

        flash("Confirmed as a caught mis-ship.")
        return redirect(url_for("exceptions_list"))

    @app.route("/billing")
    @login_required
    @owner_only
    def billing_dashboard():
        conn = get_db()
        account = g.account
        events_raw = db.billing_events_for_account(conn, account["id"])
        events = [
            {
                "created_at": e["created_at"],
                "kind": e["kind"],
                "amount": pricing.format_cents_as_dollars(e["cents"]),
                "scan_uuid": e["scan_uuid"],
            }
            for e in reversed(events_raw[-100:])
        ]
        catches = db.count_billable_catches(conn, account["id"])
        stats = {
            "savings": pricing.format_cents_as_dollars(
                billing.savings_to_date_cents(conn, account["id"])
            ),
            "catches": catches,
            "free_remaining": max(account["free_allowance"] - catches, 0),
            "net_owed": pricing.format_cents_as_dollars(
                billing.net_amount_owed_cents(conn, account["id"])
            ),
            "free_allowance": account["free_allowance"],
            "price_per_catch": pricing.format_cents_as_dollars(account["price_per_catch_cents"]),
        }
        return render_template(
            "billing_dashboard.html", active="billing", account=account, stats=stats, events=events
        )


# ---------------------------------------------------------------------------
# Marketing landing page + signup. Pricing numbers are ALWAYS sourced from
# pricing.py -- never hardcoded here -- see
# tests/test_marketing_pricing_consistency.py, which renders this page and
# fails if a displayed dollar figure doesn't trace back to that module.
# ---------------------------------------------------------------------------


def register_marketing_routes(app: Flask, get_db) -> None:
    def _is_logged_in():
        token = request.cookies.get(auth.SESSION_COOKIE_NAME)
        return auth.current_user(get_db(), token) is not None

    @app.route("/")
    def index():
        if _is_logged_in():
            return redirect(url_for("dashboard"))
        return render_template(
            "marketing.html",
            logged_in=False,
            free_allowance=pricing.DEFAULT_FREE_ALLOWANCE,
            price_per_catch=pricing.format_cents_as_dollars(pricing.DEFAULT_PRICE_PER_CATCH_CENTS),
            savings_per_catch=pricing.format_cents_as_dollars(pricing.SAVINGS_PER_CATCH_CENTS),
            savings_example=pricing.format_cents_as_dollars(pricing.SAVINGS_PER_CATCH_CENTS * 10),
        )

    @app.route("/signup")
    def signup():
        return render_template(
            "signup.html",
            error=None,
            business_name=None,
            email=None,
            free_allowance=pricing.DEFAULT_FREE_ALLOWANCE,
            price_per_catch=pricing.format_cents_as_dollars(pricing.DEFAULT_PRICE_PER_CATCH_CENTS),
            timezones=tz.COMMON_TIMEZONES,
            selected_timezone=tz.DEFAULT_TIMEZONE,
        )

    @app.route("/signup", methods=["POST"])
    def signup_submit():
        conn = get_db()
        business_name = (request.form.get("business_name") or "").strip()
        email = (request.form.get("email") or "").strip()
        password = request.form.get("password") or ""
        account_timezone = tz.normalize_timezone(request.form.get("timezone"))

        def render_error(msg, status=400):
            return render_template(
                "signup.html",
                error=msg,
                business_name=business_name,
                email=email,
                free_allowance=pricing.DEFAULT_FREE_ALLOWANCE,
                price_per_catch=pricing.format_cents_as_dollars(
                    pricing.DEFAULT_PRICE_PER_CATCH_CENTS
                ),
                timezones=tz.COMMON_TIMEZONES,
                selected_timezone=account_timezone,
            ), status

        # Signup is the only endpoint an anonymous stranger can use to
        # create durable server-side state, so it gets its own budget
        # before anything is written. Keyed on IP -- there is no prior
        # identity to key on here, by definition.
        ip_key = request.remote_addr or ""
        if (
            db.count_rate_limit_events(conn, RATE_BUCKET_SIGNUP, ip_key, SIGNUP_WINDOW_SECONDS)
            >= SIGNUP_MAX_PER_IP
        ):
            db.record_audit(
                conn,
                actor=email or "(anonymous)",
                action="account.signup_rate_limited",
                target_table="accounts",
                ip=request.remote_addr,
            )
            return render_error(
                "Too many accounts created from this network recently. Try again later.", status=429
            )

        # Counted before validation, not after: the budget exists to cap
        # how hard one network can hammer this endpoint, and a script
        # posting deliberately-invalid input would otherwise consume none
        # of it while still costing us a request and a DB round trip each
        # time.
        db.record_rate_limit_event(conn, RATE_BUCKET_SIGNUP, ip_key)

        if not business_name or not email or len(password) < 8:
            return render_error(
                "Fill in a business name, email, and a password of at least 8 characters."
            )
        if not EMAIL_RE.match(email):
            return render_error("Enter a valid email address.")
        if db.get_user_by_email(conn, email):
            return render_error(
                "An account with that email already exists. Try logging in instead."
            )

        account_id = db.create_account(
            conn,
            business_name,
            price_per_catch_cents=pricing.DEFAULT_PRICE_PER_CATCH_CENTS,
            free_allowance=pricing.DEFAULT_FREE_ALLOWANCE,
            timezone=account_timezone,
        )
        user_id = db.create_user(
            conn,
            account_id,
            email,
            auth.hash_password(password),
            role="owner",
        )
        db.record_audit(
            conn,
            actor=email,
            action="account.signup",
            target_table="accounts",
            target_id=str(account_id),
            after={"business_name": business_name},
        )

        token = auth.start_session(conn, user_id, account_id)
        resp = make_response(redirect(url_for("dashboard")))
        resp.set_cookie(
            auth.SESSION_COOKIE_NAME,
            token,
            httponly=True,
            secure=True,
            samesite="Lax",
            max_age=auth.SESSION_LIFETIME_HOURS * 3600,
        )
        return resp
