"""Auto post scheduler: one message -> slow share to all groups -> reschedule 1-2h.

Design:
- State lives in the SQLite `settings` table (survives restarts).
- A single asyncio task (`scheduler`) drives everything. It sleeps in small
  slices so /autostop can interrupt instantly.
- Anti-tracking: random gaps between groups, "typing..." indicator right
  before each send, and a fully random 1-2 hour gap between whole cycles.
"""

import asyncio
import random
import time

import db

# settings keys
K_MSG = "auto_msg"
K_ENABLED = "auto_enabled"
K_NEXT_AT = "auto_next_at"
K_MIN_HOUR = "auto_min_hour"
K_MAX_HOUR = "auto_max_hour"

MIN_SEC = 60
HOUR = 3600


def get_message():
    return (db.get_setting(K_MSG) or "").strip()


def is_enabled():
    return db.get_setting(K_ENABLED, "0") == "1"


def set_enabled(on: bool):
    db.set_setting(K_ENABLED, "1" if on else "0")


def get_window():
    lo = int(db.get_setting(K_MIN_HOUR, "1") or 1)
    hi = int(db.get_setting(K_MAX_HOUR, "2") or 2)
    if lo > hi:
        lo, hi = hi, lo
    return lo, hi


def set_window(lo: int, hi: int):
    if lo > hi:
        lo, hi = hi, lo
    db.set_setting(K_MIN_HOUR, lo)
    db.set_setting(K_MAX_HOUR, hi)


def get_next_at():
    v = db.get_setting(K_NEXT_AT)
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def set_next_at(ts: int):
    db.set_setting(K_NEXT_AT, int(ts))


def clear_next_at():
    db.set_setting(K_NEXT_AT, "")


def pick_next_gap():
    """Random seconds until the next full cycle (default 1-2 hours)."""
    lo, hi = get_window()
    lo = max(1, int(lo))
    hi = max(lo, int(hi))
    return random.randint(lo * HOUR, hi * HOUR)


def fmt_ts(ts):
    if not ts:
        return "—"
    return time.strftime("%d %b %I:%M %p", time.localtime(ts))


def fmt_dur(sec):
    sec = int(max(0, sec))
    h, m = divmod(sec // 60, 60)
    d, h = divmod(h, 24)
    out = []
    if d:
        out.append(f"{d} দিন")
    if h:
        out.append(f"{h} ঘণ্টা")
    if m and not d:
        out.append(f"{m} মিনিট")
    if not out:
        out.append(f"{sec} সেকেন্ড")
    return " ".join(out)


def count_down(sec):
    """Human countdown, e.g. 1 ঘণ্টা ২০ মিনিট."""
    lo, hi = get_window()
    h, m = divmod(int(max(0, sec)) // 60, 60)
    if h >= 24:
        d, h = divmod(h, 24)
        return f"{d} দিন {h} ঘণ্টা"
    if h:
        return f"{h} ঘণ্টা {m} মিনিট"
    return f"{m} মিনিট"


# ---------------------------------------------------------------- state
class AutoState:
    """Shared runtime state (not persisted except next_at)."""

    def __init__(self):
        self.stop_flag = False      # owner pressed /autostop
        self.running = False        # a share cycle is in progress
        self.task = None            # scheduler task
        self.last_result = None     # (sent, failed, total) of last cycle

    def reset(self):
        self.stop_flag = False

    def request_stop(self):
        self.stop_flag = True

    def should_stop(self):
        return self.stop_flag


STATE = AutoState()
