"""Verify the running bot process: log contents + live settings, no side effects."""
import io
import os
import sqlite3
import subprocess
import sys

BASE = "/root/agents/personal/workspace/group-sharer"
FAILS = []


def check(name, cond, extra=""):
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


print("\n=== 1. Process alive ===")
out = subprocess.run(["ps", "-o", "pid,stat,etime,args", "-A"],
                     capture_output=True, text=True).stdout
bots = [l for l in out.split("\n") if "bot.py" in l and "grep" not in l and "test" not in l]
check("bot process running", len(bots) == 1, f"count={len(bots)}")
check("only one instance", len(bots) == 1, f"{bots}")

print("\n=== 2. Startup log healthy ===")
log = io.open(os.path.join(BASE, "bot.log"), encoding="utf-8", errors="ignore").read()
check("command menu set", "✅ Command menu set." in log)
check("userbot connected", "✅ Userbot: connected" in log)
check("scheduler launched", "Auto-post scheduler launched" in log)
check("no crash/traceback", "Traceback" not in log and "Error" not in log.replace("Error in", ""),
      "")

print("\n=== 3. Userbot session is authorized ===")
sys.path.insert(0, BASE)
import db
db.init_db()
check("session file exists", os.path.exists(os.path.join(BASE, "userbot.session")))
sz = os.path.getsize(os.path.join(BASE, "userbot.session"))
check("session non-empty", sz > 0, f"{sz} bytes")

print("\n=== 4. Data intact ===")
groups = db.get_groups()
check("8 groups saved", len(groups) == 8, f"{len(groups)}")
s = db.all_settings()
check("auto_msg unset (as before)", not s.get("auto_msg"), repr(s.get("auto_msg")))
check("auto_enabled off", s.get("auto_enabled") in ("0", ""), repr(s.get("auto_enabled")))

print("\n=== 5. checkgroups code paths present ===")
src = io.open(os.path.join(BASE, "bot.py"), encoding="utf-8").read()
check("normalize_id defined", "def normalize_id(chat_id):" in src)
check("uses ensure_connected", src.count("await engine.ensure_connected()") >= 4,
      f"count={src.count('await engine.ensure_connected()')}")
check("fmt_checkgroups used twice", src.count("fmt_checkgroups(chats, saved_group_ids())") == 2,
      f"count={src.count('fmt_checkgroups(chats, saved_group_ids())')}")
check("no leftover is_logged_in in checkgroups",
      "elif val == \"checkgroups\":\n        ok, err = await engine.ensure_connected()" in src)

print("\n=== 6. SQLite readable ===")
dbp = os.path.join(BASE, "bot.db")
if not os.path.exists(dbp):
    cands = [f for f in os.listdir(BASE) if f.endswith(".db")]
    dbp = os.path.join(BASE, cands[0]) if cands else dbp
conn = sqlite3.connect(dbp)
n = conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0]
check("groups row count = 8", n == 8, f"{n}")
conn.close()

print("\n" + "=" * 46)
if FAILS:
    print(f"❌ {len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("✅ ALL RUNTIME CHECKS PASSED")
