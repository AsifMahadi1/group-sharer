"""SQLite helpers for group-sharer."""
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
    conn.commit()
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