#!/usr/bin/env python3
"""Group Sharer WebUI (admin panel).

Runs separately from the Telegram bot.  It only reads/writes the existing
SQLite tables (groups, share_log, auto_log, settings, users, user_state) and
never changes the schema.  Both the newer multi-user DB and the legacy
single-user DB shape are detected automatically.

Start with:  python3.8 webui.py
"""

import csv
import hashlib
import hmac
import io
import json
import math
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timedelta
from functools import wraps

from flask import (
    Flask,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

import db

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

PAGE_SIZE = 50
MAX_IMPORT_BYTES = 2 * 1024 * 1024

# Settings that are safe to edit from the WebUI.  In the newer schema these
# live in user_state; the legacy schema keeps them in settings.
EDITABLE_SETTINGS = ("auto_msg", "auto_enabled", "auto_min_hour", "auto_max_hour")

SENSITIVE_KEY_PARTS = (
    "token",
    "key",
    "session",
    "string",
    "password",
    "secret",
    "hash",
)

STATUS_CLASS = {
    "sent": "success",
    "flood_wait": "warning",
    "slowmode": "warning",
    "stopped": "muted",
    "no_permission": "danger",
    "banned": "danger",
    "error": "danger",
}

USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
NUMERIC_RE = re.compile(r"^-?\d{3,20}$")
PUBLIC_LINK_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/([A-Za-z][A-Za-z0-9_]{3,31})/?$",
    re.IGNORECASE,
)
INVITE_LINK_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/(?:\+|joinchat/)([A-Za-z0-9_-]{5,})/?$",
    re.IGNORECASE,
)

app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "templates_webui"),
    static_folder=os.path.join(BASE_DIR, "static_webui"),
    static_url_path="/static_webui",
)


# --------------------------------------------------------------------------
# Small DB helpers (read/limited writes only, no schema changes)
# --------------------------------------------------------------------------
def _connect():
    conn = sqlite3.connect(db.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=5000")
    except sqlite3.Error:
        pass
    return conn


def _fetchone(sql, params=()):
    conn = _connect()
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


def _fetchall(sql, params=()):
    conn = _connect()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _execute(sql, params=()):
    conn = _connect()
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


_COLUMN_CACHE = {}


def _columns(table):
    """Return the column set for a table (cached). Internal names only."""
    if table in _COLUMN_CACHE:
        return _COLUMN_CACHE[table]
    conn = _connect()
    try:
        rows = conn.execute("PRAGMA table_info({})".format(table)).fetchall()
        columns = {row["name"] for row in rows}
    except sqlite3.Error:
        columns = set()
    finally:
        conn.close()
    _COLUMN_CACHE[table] = columns
    return columns


def _has_column(table, column):
    return column in _columns(table)


def _multi_user():
    """True when the DB uses the newer multi-tenant schema."""
    return _has_column("groups", "user_id")


def _owner_id():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            return int(json.load(handle).get("owner_id") or 0)
    except (OSError, ValueError, TypeError):
        return 0


def _all_users():
    if not _columns("users"):
        return []
    rows = _fetchall(
        "SELECT user_id, first_name, username, status FROM users "
        "ORDER BY registered_at DESC"
    )
    return rows


def _default_user_id():
    """Owner from config if present, otherwise first registered user."""
    owner = _owner_id()
    users = _all_users()
    ids = [row["user_id"] for row in users]
    if owner and (not ids or owner in ids):
        return owner
    if ids:
        return ids[0]
    return owner or 0


def _valid_user_id(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    if _multi_user():
        row = _fetchone("SELECT user_id FROM users WHERE user_id = ?", (value,))
        if not row:
            return None
    return value


def _user_label(user_id):
    row = _fetchone(
        "SELECT first_name, username FROM users WHERE user_id = ?", (user_id,)
    )
    if not row:
        return str(user_id)
    name = row["first_name"] or row["username"] or str(user_id)
    return "{} ({})".format(name, user_id)


def _get_webui_setting(key, default=None):
    if _has_column("settings", "user_id"):
        row = _fetchone(
            "SELECT value FROM settings WHERE user_id = 0 AND key = ?", (key,)
        )
    else:
        row = _fetchone("SELECT value FROM settings WHERE key = ? LIMIT 1", (key,))
    return row["value"] if row else default


def _set_webui_setting(key, value):
    if _has_column("settings", "user_id"):
        _execute(
            "INSERT INTO settings (user_id, key, value) VALUES (0, ?, ?) "
            "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
    else:
        _execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )


def _auto_settings(user_id):
    """Return the bot's auto-post settings for one user (or legacy global)."""
    defaults = {
        "auto_enabled": "0",
        "auto_msg": "",
        "auto_next_at": "",
        "auto_min_hour": "1",
        "auto_max_hour": "2",
    }
    if _columns("user_state") and _multi_user():
        row = _fetchone("SELECT * FROM user_state WHERE user_id = ?", (user_id,))
        if row:
            for key in defaults:
                if row[key] is not None:
                    defaults[key] = row[key]
        return defaults
    for key in defaults:
        value = _get_webui_setting(key)
        if value is not None:
            defaults[key] = value
    return defaults


def _save_auto_settings(user_id, message, enabled, min_hour, max_hour):
    if _columns("user_state") and _multi_user():
        exists = _fetchone(
            "SELECT user_id FROM user_state WHERE user_id = ?", (user_id,)
        )
        if exists:
            _execute(
                "UPDATE user_state SET auto_msg = ?, auto_enabled = ?, "
                "auto_min_hour = ?, auto_max_hour = ? WHERE user_id = ?",
                (message, enabled, min_hour, max_hour, user_id),
            )
        else:
            _execute(
                "INSERT INTO user_state "
                "(user_id, auto_msg, auto_enabled, auto_min_hour, auto_max_hour) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, message, enabled, min_hour, max_hour),
            )
        return
    _set_webui_setting("auto_msg", message)
    _set_webui_setting("auto_enabled", enabled)
    _set_webui_setting("auto_min_hour", min_hour)
    _set_webui_setting("auto_max_hour", max_hour)


def _settings_rows():
    if _has_column("settings", "user_id"):
        return _fetchall(
            "SELECT user_id, key, value FROM settings ORDER BY user_id, key"
        )
    return _fetchall("SELECT 0 AS user_id, key, value FROM settings ORDER BY key")


def _add_group(user_id, chat_id, title):
    """Insert one group; returns False when it already exists."""
    if _multi_user():
        exists = _fetchone(
            "SELECT id FROM groups WHERE user_id = ? AND chat_id = ?",
            (user_id, chat_id),
        )
        if exists:
            return False
        _execute(
            "INSERT INTO groups (user_id, chat_id, title, added_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, chat_id, title, int(time.time())),
        )
        return True
    exists = _fetchone("SELECT id FROM groups WHERE chat_id = ?", (chat_id,))
    if exists:
        return False
    _execute(
        "INSERT INTO groups (chat_id, title, added_at) VALUES (?, ?, ?)",
        (chat_id, title, int(time.time())),
    )
    return True


# --------------------------------------------------------------------------
# Auth / password hashing (stdlib scrypt, fb-auto-poster compatible format)
# --------------------------------------------------------------------------
def hash_password(password):
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=32768,
        r=8,
        p=1,
        dklen=64,
        maxmem=64 * 1024 * 1024,
    )
    return "scrypt:32768:8:1${}${}".format(salt.hex(), derived.hex())


def check_password(password, stored):
    try:
        method, salt_hex, hash_hex = stored.split("$", 2)
        algo, n, r, p = method.split(":")
        if algo != "scrypt":
            return False
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(bytes.fromhex(hash_hex)),
            maxmem=64 * 1024 * 1024,
        )
        return hmac.compare_digest(derived.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


def saved_password_hash():
    try:
        return _get_webui_setting("webui_password_hash") or ""
    except sqlite3.Error:
        return ""


def _load_secret_key():
    try:
        value = _get_webui_setting("webui_secret")
    except sqlite3.Error:
        value = None
    if not value:
        value = secrets.token_hex(32)
        try:
            _set_webui_setting("webui_secret", value)
        except sqlite3.Error:
            pass
    return value


app.secret_key = _load_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)


def login_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not session.get("auth"):
            return redirect(url_for("login"))
        return func(*args, **kwargs)

    return wrapper


def csrf_token():
    token = session.get("_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf"] = token
    return token


@app.before_request
def _csrf_protect():
    if request.method == "POST":
        sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
        if not sent or not hmac.compare_digest(sent, session.get("_csrf", "")):
            abort(400, description="CSRF token missing or invalid. Reload the page.")


@app.after_request
def _security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


# --------------------------------------------------------------------------
# Template helpers
# --------------------------------------------------------------------------
def fmt_ts(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return "—"
    if value <= 0:
        return "—"
    return time.strftime("%d %b %Y, %I:%M %p", time.localtime(value))


def fmt_duration(seconds):
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return "—"
    seconds = max(0, seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return "{}h {}m".format(hours, minutes)
    if minutes:
        return "{}m {}s".format(minutes, secs)
    return "{}s".format(secs)


def status_class(status):
    return STATUS_CLASS.get(status, "danger")


def is_sensitive(key):
    key = (key or "").lower()
    return any(part in key for part in SENSITIVE_KEY_PARTS)


def mask_value(key, value):
    if is_sensitive(key):
        return "***"
    return value


def _safe_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            cfg = json.load(handle)
    except (OSError, ValueError):
        return {}
    safe_keys = (
        "owner_id",
        "delay_min",
        "delay_max",
        "max_flood_wait",
        "bot_username",
        "support_bot_username",
    )
    return {key: cfg[key] for key in safe_keys if key in cfg}


app.jinja_env.filters["fmt_ts"] = fmt_ts
app.jinja_env.filters["duration"] = fmt_duration
app.jinja_env.filters["status_class"] = status_class


@app.context_processor
def _inject_globals():
    return {
        "csrf_token": csrf_token,
        "current_path": request.path,
        "status_class": status_class,
        "now_ts": int(time.time()),
    }


# --------------------------------------------------------------------------
# Target parsing / validation
# --------------------------------------------------------------------------
def parse_target(raw):
    """Validate and normalise one group target.

    Accepts numeric ids, @usernames, t.me usernames and t.me invite links.
    Returns (normalised_chat_id, error).
    """
    raw = (raw or "").strip()
    if not raw:
        return None, "empty"
    if len(raw) > 256:
        return None, "too long"
    if NUMERIC_RE.match(raw):
        try:
            return int(raw), None
        except ValueError:
            return None, "invalid numeric id"
    if raw.startswith("@"):
        if USERNAME_RE.match(raw[1:]):
            return "@" + raw[1:], None
        return None, "invalid @username"
    if raw.lower().startswith(("t.me/", "telegram.me/", "http://", "https://")):
        invite = INVITE_LINK_RE.match(raw)
        if invite:
            return "t.me/+{}".format(invite.group(1)), None
        public = PUBLIC_LINK_RE.match(raw)
        if public:
            return "t.me/{}".format(public.group(1)), None
        return None, "invalid Telegram link"
    return None, "use a numeric id, @username or t.me link"


def parse_bulk_line(line):
    """Parse `target | optional title` (title is optional)."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None, None, None
    if "|" in line:
        raw_target, title = line.split("|", 1)
    else:
        raw_target, title = line, ""
    chat_id, error = parse_target(raw_target)
    title = title.strip()[:200]
    return chat_id, (title or "(untitled)"), error


# --------------------------------------------------------------------------
# Auth routes
# --------------------------------------------------------------------------
@app.route("/")
def index():
    if not saved_password_hash():
        return render_template("setup.html", first_run=True)
    if not session.get("auth"):
        return render_template("login.html")
    return render_template("dashboard.html", **_dashboard_context())


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if saved_password_hash():
        return redirect(url_for("login"))

    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        if len(password) < 6:
            flash("পাসওয়ার্ড কমপক্ষে ৬ অক্ষরের হতে হবে।", "error")
        elif password != confirm:
            flash("দুইবার দেওয়া পাসওয়ার্ড মেলেনি।", "error")
        else:
            _set_webui_setting("webui_password_hash", hash_password(password))
            session.clear()
            session["auth"] = True
            session["_csrf"] = secrets.token_urlsafe(32)
            flash("পাসওয়ার্ড সেট হয়েছে। WebUI-তে স্বাগতম।", "success")
            return redirect(url_for("dashboard"))

    return render_template("setup.html", first_run=True)


@app.route("/login", methods=["GET", "POST"])
def login():
    if not saved_password_hash():
        return redirect(url_for("setup"))
    if session.get("auth"):
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        password = request.form.get("password", "")
        if check_password(password, saved_password_hash()):
            session.clear()
            session["auth"] = True
            session["_csrf"] = secrets.token_urlsafe(32)
            flash("লগইন সফল হয়েছে।", "success")
            return redirect(url_for("dashboard"))
        flash("ভুল পাসওয়ার্ড।", "error")

    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("লগআউট হয়েছে।", "info")
    return redirect(url_for("login"))


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
def _dashboard_context():
    start_of_day = int(
        datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    )

    groups_total = _fetchone("SELECT COUNT(*) AS n FROM groups")
    groups_total = groups_total["n"] if groups_total else 0

    today = _fetchone(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END) AS sent "
        "FROM share_log WHERE ts >= ?",
        (start_of_day,),
    )
    today_total = (today["total"] if today else 0) or 0
    today_sent = (today["sent"] if today else 0) or 0
    today_failed = today_total - today_sent

    last_auto = _fetchone("SELECT * FROM auto_log ORDER BY id DESC LIMIT 1")
    recent_logs = _fetchall("SELECT * FROM share_log ORDER BY id DESC LIMIT 8")

    users = _all_users()
    default_user = _default_user_id()
    auto = _auto_settings(default_user)
    auto_enabled = str(auto.get("auto_enabled", "0")) == "1"
    try:
        auto_next_at = int(auto.get("auto_next_at") or 0)
    except (TypeError, ValueError):
        auto_next_at = 0
    auto_msg = auto.get("auto_msg", "") or ""
    if len(auto_msg) > 160:
        auto_msg = auto_msg[:160] + "…"

    return {
        "groups_total": groups_total,
        "today_total": today_total,
        "today_sent": today_sent,
        "today_failed": today_failed,
        "last_auto": last_auto,
        "recent_logs": recent_logs,
        "auto_enabled": auto_enabled,
        "auto_next_at": auto_next_at,
        "auto_msg": auto_msg,
        "auto_settings": auto,
        "users": users,
        "users_total": len(users),
        "default_user_id": default_user,
        "multi_user": _multi_user(),
        "config": _safe_config(),
    }


@app.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html", **_dashboard_context())


# --------------------------------------------------------------------------
# Groups
# --------------------------------------------------------------------------
def _groups_query(search, user_id=None):
    multi = _multi_user()
    where = []
    params = []

    if multi:
        base = (
            "SELECT g.*, u.first_name, u.username "
            "FROM groups g LEFT JOIN users u ON u.user_id = g.user_id"
        )
        prefix = "g."
        order = "g.id"
    else:
        base = (
            "SELECT *, 0 AS user_id, NULL AS first_name, NULL AS username "
            "FROM groups"
        )
        prefix = ""
        order = "id"

    if search:
        like = "%{}%".format(search)
        where.append(
            "({}title LIKE ? OR CAST({}chat_id AS TEXT) LIKE ?)".format(
                prefix, prefix
            )
        )
        params.extend([like, like])
    if multi and user_id:
        where.append("g.user_id = ?")
        params.append(user_id)

    sql = base
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY " + order
    return sql, tuple(params)


@app.route("/groups")
@login_required
def groups():
    search = request.args.get("q", "").strip()
    user_arg = request.args.get("user", "").strip()
    filter_user = _valid_user_id(user_arg) if user_arg else None
    sql, params = _groups_query(search, filter_user)
    rows = _fetchall(sql, params)
    users = _all_users()
    default_user = _default_user_id()
    return render_template(
        "groups.html",
        groups=rows,
        q=search,
        users=users,
        multi_user=_multi_user(),
        default_user_id=default_user,
        filter_user=filter_user,
    )


@app.route("/groups/add", methods=["POST"])
@login_required
def groups_add():
    chat_id, error = parse_target(request.form.get("target", ""))
    title = request.form.get("title", "").strip()[:200] or "(untitled)"
    user_id = _form_user_id()
    if _multi_user() and user_id is None:
        flash("সঠিক ইউজার সিলেক্ট করুন।", "error")
        return redirect(url_for("groups"))
    if error:
        flash("গ্রুপ যোগ হয়নি: {}।".format(error), "error")
        return redirect(url_for("groups", q=request.form.get("q", "")))

    try:
        added = _add_group(user_id, chat_id, title)
    except sqlite3.Error as exc:
        flash("ডেটাবেস সমস্যা: {}".format(exc), "error")
        return redirect(url_for("groups"))

    if added:
        flash("গ্রুপ যোগ হয়েছে: {} ({})".format(title, chat_id), "success")
    else:
        flash("গ্রুপটি আগে থেকেই আছে: {}".format(chat_id), "warning")
    return redirect(url_for("groups"))


@app.route("/groups/bulk-add", methods=["POST"])
@login_required
def groups_bulk_add():
    user_id = _form_user_id()
    if _multi_user() and user_id is None:
        flash("সঠিক ইউজার সিলেক্ট করুন।", "error")
        return redirect(url_for("groups"))

    raw = request.form.get("targets", "")
    lines = [line for line in raw.splitlines() if line.strip()][:2000]
    added = 0
    duplicates = 0
    invalid = []

    for line in lines:
        chat_id, title, error = parse_bulk_line(line)
        if error:
            invalid.append(line.strip()[:80])
            continue
        try:
            if _add_group(user_id, chat_id, title):
                added += 1
            else:
                duplicates += 1
        except sqlite3.Error:
            invalid.append(line.strip()[:80])

    message = "Bulk: {} যোগ হয়েছে, {} ডুপ্লিকেট, {} ভুল।".format(
        added, duplicates, len(invalid)
    )
    if invalid:
        message += " ভুল লাইন: " + ", ".join(invalid[:5])
        if len(invalid) > 5:
            message += " …"
    flash(message, "success" if added else "warning")
    return redirect(url_for("groups"))


@app.route("/groups/<int:group_id>/delete", methods=["POST"])
@login_required
def group_delete(group_id):
    removed = _execute("DELETE FROM groups WHERE id = ?", (group_id,))
    if removed:
        flash("গ্রুপ মুছে ফেলা হয়েছে।", "success")
    else:
        flash("গ্রুপটি পাওয়া যায়নি।", "error")
    return redirect(url_for("groups", q=request.form.get("q", "")))


@app.route("/groups/bulk-delete", methods=["POST"])
@login_required
def groups_bulk_delete():
    ids = []
    for value in request.form.getlist("ids"):
        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            continue
    if not ids:
        flash("কোনো গ্রুপ সিলেক্ট করা হয়নি।", "warning")
        return redirect(url_for("groups"))

    placeholders = ",".join("?" for _ in ids)
    removed = _execute(
        "DELETE FROM groups WHERE id IN ({})".format(placeholders), tuple(ids)
    )
    flash("{} টি গ্রুপ মুছে ফেলা হয়েছে।".format(removed), "success")
    return redirect(url_for("groups"))


@app.route("/groups/export")
@login_required
def groups_export():
    export_format = request.args.get("format", "csv").lower()
    search = request.args.get("q", "").strip()
    user_arg = request.args.get("user", "").strip()
    filter_user = _valid_user_id(user_arg) if user_arg else None
    sql, params = _groups_query(search, filter_user)
    rows = _fetchall(sql, params)
    multi = _multi_user()

    if export_format == "json":
        payload = []
        for row in rows:
            payload.append(
                {
                    "id": row["id"],
                    "user_id": row["user_id"] if multi else 0,
                    "username": row["first_name"] if multi else None,
                    "chat_id": row["chat_id"],
                    "title": row["title"],
                    "added_at": row["added_at"],
                    "added_at_text": fmt_ts(row["added_at"]),
                }
            )
        body = json.dumps(payload, ensure_ascii=False, indent=2)
        return Response(
            body,
            mimetype="application/json; charset=utf-8",
            headers={
                "Content-Disposition": "attachment; filename=groups.json"
            },
        )

    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(["id", "user_id", "chat_id", "title", "added_at"])
    for row in rows:
        writer.writerow(
            [
                row["id"],
                row["user_id"] if multi else 0,
                row["chat_id"],
                row["title"],
                row["added_at"],
            ]
        )
    return Response(
        stream.getvalue().encode("utf-8-sig"),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=groups.csv"},
    )


def _import_entries(filename, payload):
    """Turn an uploaded CSV/JSON payload into (target, title, user_id) tuples."""
    name = (filename or "").lower()
    text = payload.decode("utf-8-sig", errors="replace")

    if name.endswith(".json") or text.lstrip().startswith(("[", "{")):
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("groups", [])
        if not isinstance(data, list):
            raise ValueError("JSON must be a list or an object with a groups list")
        entries = []
        for item in data:
            if isinstance(item, str):
                entries.append((item, "", None))
            elif isinstance(item, dict):
                target = item.get("chat_id", item.get("target", ""))
                entries.append(
                    (target, item.get("title", ""), item.get("user_id"))
                )
        return entries

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV has no header row")
    fields = {(name or "").strip().lower(): name for name in reader.fieldnames}
    target_field = fields.get("chat_id") or fields.get("target")
    if not target_field:
        raise ValueError("CSV needs a chat_id or target column")
    title_field = fields.get("title")
    user_field = fields.get("user_id")
    entries = []
    for row in reader:
        entries.append(
            (
                row.get(target_field, ""),
                row.get(title_field, "") if title_field else "",
                row.get(user_field) if user_field else None,
            )
        )
    return entries


def _form_user_id():
    if not _multi_user():
        return 0
    return _valid_user_id(request.form.get("user_id"))


@app.route("/groups/import", methods=["POST"])
@login_required
def groups_import():
    upload = request.files.get("file")
    if not upload or not upload.filename:
        flash("কোনো ফাইল সিলেক্ট করা হয়নি।", "error")
        return redirect(url_for("groups"))

    payload = upload.read(MAX_IMPORT_BYTES + 1)
    if len(payload) > MAX_IMPORT_BYTES:
        flash("ফাইলটি অনেক বড় (সর্বোচ্চ ২ MB)।", "error")
        return redirect(url_for("groups"))

    try:
        entries = _import_entries(upload.filename, payload)
    except (ValueError, json.JSONDecodeError, csv.Error) as exc:
        flash("ফাইল পড়া যায়নি: {}".format(exc), "error")
        return redirect(url_for("groups"))

    default_user = _form_user_id()
    if _multi_user() and default_user is None:
        default_user = _default_user_id()

    added = 0
    duplicates = 0
    invalid = 0
    for raw_target, raw_title, raw_user in entries[:5000]:
        chat_id, error = parse_target(str(raw_target or ""))
        if error:
            invalid += 1
            continue
        user_id = default_user
        if _multi_user() and raw_user not in (None, ""):
            parsed_user = _valid_user_id(raw_user)
            if parsed_user is None:
                invalid += 1
                continue
            user_id = parsed_user
        title = str(raw_title or "").strip()[:200] or "(untitled)"
        try:
            if _add_group(user_id, chat_id, title):
                added += 1
            else:
                duplicates += 1
        except sqlite3.Error:
            invalid += 1

    flash(
        "Import: {} যোগ হয়েছে, {} ডুপ্লিকেট, {} ভুল।".format(
            added, duplicates, invalid
        ),
        "success" if added else "warning",
    )
    return redirect(url_for("groups"))


# --------------------------------------------------------------------------
# Share log
# --------------------------------------------------------------------------
def _parse_date(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def _page_number():
    try:
        return max(1, int(request.args.get("page", "1")))
    except (TypeError, ValueError):
        return 1


@app.route("/share-log")
@login_required
def share_log():
    status = request.args.get("status", "").strip()
    group = request.args.get("group", "").strip()
    date_from = request.args.get("from", "").strip()
    date_to = request.args.get("to", "").strip()
    user_arg = request.args.get("user", "").strip()
    filter_user = _valid_user_id(user_arg) if user_arg else None
    page = _page_number()
    multi = _multi_user()

    if multi:
        base = (
            "SELECT share_log.*, users.first_name, users.username "
            "FROM share_log LEFT JOIN users "
            "ON users.user_id = share_log.user_id"
        )
    else:
        base = (
            "SELECT *, 0 AS user_id, NULL AS first_name, NULL AS username "
            "FROM share_log"
        )
    prefix = "share_log."

    where = []
    params = []
    if status:
        where.append(prefix + "status = ?")
        params.append(status)
    if group:
        where.append(prefix + "group_title = ?")
        params.append(group)
    if multi and filter_user:
        where.append("share_log.user_id = ?")
        params.append(filter_user)

    start_dt = _parse_date(date_from)
    end_dt = _parse_date(date_to)
    if start_dt:
        where.append(prefix + "ts >= ?")
        params.append(int(start_dt.timestamp()))
    if end_dt:
        where.append(prefix + "ts < ?")
        params.append(int((end_dt + timedelta(days=1)).timestamp()))

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    total_row = _fetchone(
        "SELECT COUNT(*) AS n FROM share_log" + where_sql, tuple(params)
    )
    total = (total_row["n"] if total_row else 0) or 0
    pages = max(1, int(math.ceil(float(total) / PAGE_SIZE)))
    page = min(page, pages)
    offset = (page - 1) * PAGE_SIZE

    rows = _fetchall(
        base
        + where_sql
        + " ORDER BY share_log.id DESC LIMIT ? OFFSET ?",
        tuple(params + [PAGE_SIZE, offset]),
    )

    list_where = ""
    list_params = ()
    if multi and filter_user:
        list_where = " WHERE user_id = ?"
        list_params = (filter_user,)
    statuses = _fetchall(
        "SELECT DISTINCT status FROM share_log"
        + list_where
        + " ORDER BY status COLLATE NOCASE",
        list_params,
    )
    group_names = _fetchall(
        "SELECT DISTINCT group_title FROM share_log"
        + list_where
        + " ORDER BY group_title COLLATE NOCASE",
        list_params,
    )

    def page_url(target_page):
        args = {}
        if status:
            args["status"] = status
        if group:
            args["group"] = group
        if date_from:
            args["from"] = date_from
        if date_to:
            args["to"] = date_to
        if filter_user:
            args["user"] = filter_user
        if target_page > 1:
            args["page"] = target_page
        return url_for("share_log", **args)

    return render_template(
        "share_log.html",
        logs=rows,
        total=total,
        page=page,
        pages=pages,
        statuses=statuses,
        group_names=group_names,
        status=status,
        group=group,
        date_from=date_from,
        date_to=date_to,
        page_url=page_url,
        users=_all_users(),
        multi_user=multi,
        filter_user=filter_user,
    )


# --------------------------------------------------------------------------
# Auto log
# --------------------------------------------------------------------------
@app.route("/auto-log")
@login_required
def auto_log():
    page = _page_number()
    user_arg = request.args.get("user", "").strip()
    filter_user = _valid_user_id(user_arg) if user_arg else None
    multi = _multi_user()

    where_sql = ""
    params = ()
    if multi and filter_user:
        where_sql = " WHERE user_id = ?"
        params = (filter_user,)

    total_row = _fetchone(
        "SELECT COUNT(*) AS n FROM auto_log" + where_sql, params
    )
    total = (total_row["n"] if total_row else 0) or 0
    pages = max(1, int(math.ceil(float(total) / PAGE_SIZE)))
    page = min(page, pages)

    summary = _fetchone(
        "SELECT COUNT(*) AS rounds, "
        "COALESCE(SUM(total), 0) AS total, "
        "COALESCE(SUM(sent), 0) AS sent, "
        "COALESCE(SUM(failed), 0) AS failed "
        "FROM auto_log"
        + where_sql,
        params,
    )

    if multi:
        base = (
            "SELECT a.*, u.first_name, u.username "
            "FROM auto_log a LEFT JOIN users u ON u.user_id = a.user_id"
        )
        if where_sql:
            base += " WHERE a.user_id = ?"
            row_params = (filter_user, PAGE_SIZE, (page - 1) * PAGE_SIZE)
        else:
            row_params = (PAGE_SIZE, (page - 1) * PAGE_SIZE)
        rows = _fetchall(
            base + " ORDER BY a.id DESC LIMIT ? OFFSET ?", row_params
        )
    else:
        base = (
            "SELECT *, 0 AS user_id, NULL AS first_name, NULL AS username "
            "FROM auto_log"
        )
        rows = _fetchall(
            base + " ORDER BY id DESC LIMIT ? OFFSET ?",
            (PAGE_SIZE, (page - 1) * PAGE_SIZE),
        )

    def page_url(target_page):
        args = {}
        if filter_user:
            args["user"] = filter_user
        if target_page > 1:
            args["page"] = target_page
        return url_for("auto_log", **args)

    return render_template(
        "auto_log.html",
        runs=rows,
        total=total,
        page=page,
        pages=pages,
        summary=summary,
        page_url=page_url,
        users=_all_users(),
        multi_user=multi,
        filter_user=filter_user,
    )


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    multi = _multi_user()
    submitted = None

    if request.method == "POST":
        selected_user = _valid_user_id(request.form.get("user_id"))
        if multi and selected_user is None:
            flash("সঠিক ইউজার সিলেক্ট করুন।", "error")
            return redirect(url_for("settings"))

        errors = []
        message = request.form.get("auto_msg", "").rstrip()
        min_hour = request.form.get("auto_min_hour", "").strip()
        max_hour = request.form.get("auto_max_hour", "").strip()
        enabled = "1" if request.form.get("auto_enabled") else "0"
        submitted = {
            "auto_msg": message,
            "auto_enabled": enabled,
            "auto_min_hour": min_hour,
            "auto_max_hour": max_hour,
        }

        try:
            min_hour_i = int(min_hour)
            max_hour_i = int(max_hour)
            if not (1 <= min_hour_i <= 168 and 1 <= max_hour_i <= 168):
                raise ValueError
            if min_hour_i > max_hour_i:
                errors.append("সর্বনিম্ন ইন্টারভাল সর্বোচ্চের চেয়ে বড় হতে পারবে না।")
        except ValueError:
            errors.append("ইন্টারভাল ১ থেকে ১৬৮ ঘণ্টার মধ্যে পূর্ণসংখ্যা হতে হবে।")
            min_hour_i = max_hour_i = None

        if len(message) > 4096:
            errors.append("অটো-পোস্ট মেসেজ ৪০৯৬ অক্ষরের মধ্যে হতে হবে।")

        if errors:
            for error in errors:
                flash(error, "error")
        else:
            _save_auto_settings(
                selected_user, message, enabled, min_hour_i, max_hour_i
            )
            flash("সেটিংস সেভ হয়েছে।", "success")
            args = {"user": selected_user} if multi else {}
            return redirect(url_for("settings", **args))

    user_arg = request.args.get("user", "").strip()
    selected_user = _valid_user_id(user_arg) if user_arg else None
    if selected_user is None:
        selected_user = _default_user_id()

    auto = _auto_settings(selected_user)

    rows = []
    for row in _settings_rows():
        key = row["key"]
        rows.append(
            {
                "user_id": row["user_id"] if multi else 0,
                "key": key,
                "value": mask_value(key, row["value"]),
                "masked": is_sensitive(key),
                "editable": False,
                "source": "settings",
            }
        )
    for key in EDITABLE_SETTINGS:
        value = auto.get(key, "")
        rows.append(
            {
                "user_id": selected_user,
                "key": key,
                "value": value,
                "masked": is_sensitive(key),
                "editable": True,
                "source": "user_state" if multi else "settings",
            }
        )
    rows.sort(key=lambda item: (not item["editable"], item["user_id"], item["key"]))

    form_values = submitted or {
        "auto_msg": auto.get("auto_msg", "") or "",
        "auto_enabled": auto.get("auto_enabled", "0") or "0",
        "auto_min_hour": auto.get("auto_min_hour", "1"),
        "auto_max_hour": auto.get("auto_max_hour", "2"),
    }

    return render_template(
        "settings.html",
        rows=rows,
        editable=EDITABLE_SETTINGS,
        auto_msg=form_values["auto_msg"],
        auto_enabled=str(form_values["auto_enabled"]) == "1",
        auto_min_hour=form_values["auto_min_hour"],
        auto_max_hour=form_values["auto_max_hour"],
        auto_next_at=auto.get("auto_next_at", ""),
        selected_user=selected_user,
        users=_all_users(),
        multi_user=multi,
    )


@app.route("/settings/trigger", methods=["POST"])
@login_required
def trigger_share_round():
    # TODO: manual trigger is not implemented yet.  The share runner
    # (bot.run_share) only exists inside the live bot process, and
    # enable_auto.py has no "once"/manual mode (it just enables the scheduler).
    # Add a safe IPC/CLI trigger here once the bot exposes one.
    flash(
        "Not supported yet / এখনো সাপোর্ট করা হয়নি — "
        "বটে ম্যানুয়াল শেয়ার ট্রিগার নেই।",
        "warning",
    )
    return redirect(url_for("settings"))


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def main():
    if not os.path.exists(db.DB_PATH):
        raise SystemExit("data.db পাওয়া যায়নি — আগে বট চালান।")
    print("Group Sharer WebUI: http://<machine-ip>:8017")
    app.run(host="0.0.0.0", port=8017, debug=False, threaded=True)


if __name__ == "__main__":
    main()
