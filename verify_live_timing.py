"""Post-restart verification: does the FIXED scheduler wait instead of spamming?

Watches the live DB for a short window and reports whether any auto run
fired, and what the scheduler's in-memory state says.
"""
import sys
import time

sys.path.insert(0, "/root/agents/personal/workspace/group-sharer")
import db
import auto

DURATION = 90
print(f"Watching live bot for {DURATION}s (fixed code should NOT post immediately)...")
print("=" * 60)

conn = db.get_conn()
before = conn.execute("SELECT MAX(id) m FROM auto_log").fetchone()["m"] or 0
conn.close()
print(f"max auto_log id before: {before}")
print(f"auto_enabled = {db.get_setting('auto_enabled')!r}")
print(f"auto_next_at = {db.get_setting('auto_next_at')!r}")
print(f"auto_msg set = {bool(auto.get_message())}")
print("-" * 60)

start = time.time()
ticks = 0
while time.time() - start < DURATION:
    ticks += 1
    conn = db.get_conn()
    cur = conn.execute("SELECT MAX(id) m FROM auto_log").fetchone()["m"] or 0
    conn.close()
    elapsed = int(time.time() - start)
    if cur > before:
        print(f"\n  [!] a run happened at t+{elapsed}s -- id {cur}")
        break
    if ticks % 6 == 0:
        na = db.get_setting("auto_next_at")
        en = db.get_setting("auto_enabled")
        remain = ""
        if na:
            try:
                r = int(na) - int(time.time())
                remain = f" (in {r}s)" if r > 0 else f" (DUE {abs(r)}s ago)"
            except ValueError:
                remain = " (invalid)"
        print(f"  t+{elapsed:>3}s  enabled={en!r} next_at={na!r}{remain}  (no post yet)")
    time.sleep(15)

conn = db.get_conn()
after = conn.execute("SELECT MAX(id) m FROM auto_log").fetchone()["m"] or 0
conn.close()
print("=" * 60)
print(f"max auto_log id after: {after}")
if after == before:
    print("PASS: no auto run fired in the window (scheduler is waiting, not spamming)")
else:
    print(f"NOTE: a run fired (id {before + 1}-{after})")
print(f"final auto_enabled = {db.get_setting('auto_enabled')!r}")
print(f"final auto_next_at = {db.get_setting('auto_next_at')!r}")
