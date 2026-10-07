"""Verify /setMsg replace semantics: does a 2nd message overwrite the 1st?"""
import io
import os
import sys

BASE = "/root/agents/personal/workspace/group-sharer"
sys.path.insert(0, BASE)
import db

FAILS = []


def check(name, cond, extra=""):
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


print("\n=== 1. Save original state, then restore at end ===")
orig = db.get_setting("auto_msg")
print(f"  (original auto_msg = {orig!r})")

print("\n=== 2. Set message #1 ===")
db.set_setting("auto_msg", "MESSAGE_ONE_প্রথম মেসেজ")
check("msg1 stored", db.get_setting("auto_msg") == "MESSAGE_ONE_প্রথম মেসেজ",
      repr(db.get_setting("auto_msg")))

print("\n=== 3. Set message #2 (does #1 survive?) ===")
db.set_setting("auto_msg", "MESSAGE_TWO_দ্বিতীয় মেসেজ")
got = db.get_setting("auto_msg")
check("msg2 is now current", got == "MESSAGE_TWO_দ্বিতীয় মেসেজ", repr(got))
check("msg1 is GONE (replaced)", "MESSAGE_ONE" not in (got or ""), "replaced, not appended")

print("\n=== 4. Only ever one auto_msg row ===")
conn = db.get_conn()
rows = conn.execute(
    "SELECT key, value FROM settings WHERE key = 'auto_msg'"
).fetchall()
conn.close()
check("exactly 1 auto_msg row", len(rows) == 1, f"rows={len(rows)}")

print("\n=== 5. Set a 3rd time ===")
db.set_setting("auto_msg", "MESSAGE_THREE_তৃতীয়")
got3 = db.get_setting("auto_msg")
check("msg3 current", got3 == "MESSAGE_THREE_তৃতীয়")
check("msg2 also gone", "MESSAGE_TWO" not in got3)

print("\n=== 6. Survives a fresh connection (persisted, not cached) ===")
got4 = db.get_setting("auto_msg")
check("still msg3 on new conn", got4 == "MESSAGE_THREE_তৃতীয়", repr(got4))

print("\n=== 7. Scheduler reads the same key ===")
import auto
check("auto.K_MSG matches", auto.K_MSG == "auto_msg", auto.K_MSG)

print("\n=== 8. No message history / backup table anywhere ===")
src_db = io.open(os.path.join(BASE, "db.py"), encoding="utf-8").read()
check("no msg_history table", "msg_history" not in src_db)
check("no history/backup of messages", "old_msg" not in src_db and "prev_msg" not in src_db)

print("\n=== 9. Only one place writes auto_msg ===")
src_bot = io.open(os.path.join(BASE, "bot.py"), encoding="utf-8").read()
check("written in exactly 1 spot", src_bot.count('set_setting("auto_msg"') == 1,
      f"count={src_bot.count('set_setting(\"auto_msg\"')}")
check("cmd_setmsg writes it", 'db.set_setting("auto_msg", text)' in src_bot)

print("\n=== 10. What /astatus shows ===")
enabled = db.get_setting("auto_enabled")
next_at = db.get_setting("auto_next_at")
check("astatus reads message via auto.get_message()", "text = get_message()" in src_bot)
db.set_setting("auto_msg", "STATUS_PREVIEW_টেস্ট")
import importlib
import auto
importlib.reload(auto)
check("get_message() returns the live value", auto.get_message() == "STATUS_PREVIEW_টেস্ট",
      repr(auto.get_message()))
print(f"  (auto_enabled={enabled!r}, auto_next_at={next_at!r})")

# restore
if orig is None:
    conn = db.get_conn()
    conn.execute("DELETE FROM settings WHERE key = 'auto_msg'")
    conn.commit()
    conn.close()
    print("\n  (restored: removed test key)")
else:
    db.set_setting("auto_msg", orig)
    print(f"\n  (restored original: {orig!r})")

print("\n" + "=" * 46)
if FAILS:
    print(f"❌ {len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("✅ CONFIRMED: a new /setMsg REPLACES the previous message")
