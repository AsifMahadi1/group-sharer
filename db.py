"""SQLite helpers for group-sharer."""
import json
import sqlite3
import os
import time

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER UNIQUE NOT NULL,
            title TEXT NOT NULL,
            added_at INTEGER NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS share_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            group_title TEXT NOT NULL,
            status TEXT NOT NULL,
            reason TEXT,
            text TEXT NOT NULL,
            ts INTEGER NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS auto_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trigger TEXT NOT NULL,
            total INTEGER NOT NULL,
            sent INTEGER NOT NULL,
            failed INTEGER NOT NULL,
            started_at INTEGER NOT NULL,
            finished_at INTEGER NOT NULL
        )"""
    )
    # WebUI <-> bot bridge for live group scans (see BRIEF_LIVE_GROUPS.md)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS webui_scan_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cmd TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            result TEXT,
            created_at INTEGER NOT NULL,
            finished_at INTEGER
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS live_groups (
            chat_id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            type TEXT,
            scanned_at INTEGER NOT NULL
        )"""
    )
    conn.commit()
    conn.close()

# ---------- live groups scan bridge (WebUI asks, running bot fulfils) ----------
def request_scan():
    """Insert a pending scan request. Returns (row_id, error)."""
    conn = get_conn()
    try:
        pending = conn.execute(
            "SELECT id FROM webui_scan_requests WHERE status IN ('pending','running') "
            "AND created_at > ?",
            (int(time.time()) - 300,),
        ).fetchone()
        if pending:
            return None, "already_running"
        cur = conn.execute(
            "INSERT INTO webui_scan_requests (cmd, status, created_at) VALUES ('scan_groups','pending',?)",
            (int(time.time()),),
        )
        conn.commit()
        return cur.lastrowid, None
    finally:
        conn.close()

def next_scan_request():
    """Poller side: fetch one pending request (marks it running)."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id, created_at FROM webui_scan_requests WHERE status='pending' ORDER BY id LIMIT 1"
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE webui_scan_requests SET status='running' WHERE id=?", (row["id"],)
        )
        conn.commit()
        return {"id": row["id"], "created_at": row["created_at"]}
    finally:
        conn.close()

def finish_scan(request_id, status, result):
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE webui_scan_requests SET status=?, result=?, finished_at=? WHERE id=?",
            (status, json.dumps(result), int(time.time()), request_id),
        )
        conn.commit()
    finally:
        conn.close()

def reset_stale_scans(timeout=300):
    """Stuck running requests older than timeout become errored."""
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE webui_scan_requests SET status='error', result=?, finished_at=? "
            "WHERE status='running' AND created_at <= ?",
            (json.dumps({"error": "timeout"}), int(time.time()), int(time.time()) - timeout),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()

def latest_scan():
    """Most recent scan request row (dict) for status display."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id, status, result, created_at, finished_at FROM webui_scan_requests "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["result"] = json.loads(d["result"]) if d["result"] else None
        except ValueError:
            d["result"] = None
        return d
    finally:
        conn.close()

def replace_live_groups(rows):
    """Overwrite the live-group cache with a fresh scan result."""
    conn = get_conn()
    try:
        now = int(time.time())
        conn.execute("DELETE FROM live_groups")
        conn.executemany(
            "INSERT OR IGNORE INTO live_groups (chat_id, title, type, scanned_at) VALUES (?,?,?,?)",
            [
                (
                    int(r["chat_id"]),
                    (r["title"] or "(untitled)")[:200],
                    (r["type"] or "")[:40],
                    now,
                )
                for r in rows
            ],
        )
        conn.commit()
    finally:
        conn.close()

def live_groups_all():
    conn = get_conn()
    try:
        return conn.execute("SELECT * FROM live_groups ORDER BY title COLLATE NOCASE").fetchall()
    finally:
        conn.close()

def live_group_ids_in_db():
    conn = get_conn()
    try:
        rows = conn.execute("SELECT chat_id, title FROM groups").fetchall()
        return {r["chat_id"]: r["title"] for r in rows}
    finally:
        conn.close()

def save_live_selection(additions, removals):
    """additions: [(chat_id, title)], removals: [chat_id]. Returns (added, removed)."""
    conn = get_conn()
    try:
        added_now = 0
        for chat_id, title in additions or []:
            cur = conn.execute(
                "INSERT OR IGNORE INTO groups (chat_id, title, added_at) VALUES (?,?,?)",
                (int(chat_id), (title or "(untitled)")[:200], int(time.time())),
            )
            added_now += cur.rowcount
        removed_now = 0
        for chat_id in removals or []:
            cur = conn.execute("DELETE FROM groups WHERE chat_id=?", (int(chat_id),))
            removed_now += cur.rowcount
        conn.commit()
        return added_now, removed_now
    finally:
        conn.close()

# ---------- settings (key/value) ----------
def get_setting(key, default=None):
    conn = get_conn()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()

def set_setting(key, value):
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
        conn.commit()
    finally:
        conn.close()

def all_settings():
    conn = get_conn()
    try:
        return {r["key"]: r["value"] for r in conn.execute("SELECT * FROM settings").fetchall()}
    finally:
        conn.close()

# ---------- auto post log ----------
def log_auto_run(trigger, total, sent, failed, started_at, finished_at):
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO auto_log (trigger,total,sent,failed,started_at,finished_at) "
            "VALUES (?,?,?,?,?,?)",
            (trigger, total, sent, failed, int(started_at), int(finished_at)),
        )
        conn.commit()
    finally:
        conn.close()

def recent_auto_runs(limit=5):
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT * FROM auto_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    finally:
        conn.close()


def add_group(chat_id, title):
    conn = get_conn()
    try:
        exists = conn.execute(
            "SELECT id FROM groups WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        if exists:
            return False
        conn.execute(
            "INSERT INTO groups (chat_id, title, added_at) VALUES (?,?,?)",
            (chat_id, title, int(time.time())),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def remove_group(chat_id):
    conn = get_conn()
    try:
        cur = conn.execute("DELETE FROM groups WHERE chat_id = ?", (chat_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def remove_group_by_index(idx):
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, chat_id FROM groups ORDER BY id"
        ).fetchall()
        if idx < 0 or idx >= len(rows):
            return None
        row = rows[idx]
        conn.execute("DELETE FROM groups WHERE id = ?", (row["id"],))
        conn.commit()
        return row["chat_id"]
    finally:
        conn.close()


def clear_groups():
    conn = get_conn()
    try:
        conn.execute("DELETE FROM groups")
        conn.commit()
    finally:
        conn.close()


def get_groups():
    conn = get_conn()
    try:
        return conn.execute("SELECT * FROM groups ORDER BY id").fetchall()
    finally:
        conn.close()


def log_share(group_id, group_title, status, reason, text):
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO share_log (group_id, group_title, status, reason, text, ts) VALUES (?,?,?,?,?,?)",
            (group_id, group_title, status, reason, text[:500], int(time.time())),
        )
        conn.commit()
    finally:
        conn.close()


def recent_logs(limit=10):
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT * FROM share_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    finally:
        conn.close()