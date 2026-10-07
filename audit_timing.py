"""Deep timing audit of the auto-post scheduler."""
import io
import os
import sys
import time

BASE = "/root/agents/personal/workspace/group-sharer"
sys.path.insert(0, BASE)
import db
import auto

now = int(time.time())


def hdr(t):
    print("\n" + "=" * 60)
    print(t)
    print("=" * 60)


hdr("1. LIVE auto_log — how often has it actually posted?")
conn = db.get_conn()
try:
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(auto_log)").fetchall()]
    print("columns:", cols)
    rows = conn.execute("SELECT * FROM auto_log ORDER BY id DESC LIMIT 15").fetchall()
    print(f"\ntotal rows: {conn.execute('SELECT COUNT(*) c FROM auto_log').fetchone()['c']}")
    print(f"\n{'id':>4} {'trigger':>8} {'sent':>5} {'failed':>7}  created")
    for r in rows:
        ts = r.get("created") or r.get("created_at") or r.get("ts")
        try:
            ts_s = time.strftime("%m-%d %H:%M:%S", time.localtime(int(ts)))
            ago = f"  ({(now - int(ts))}s ago)"
        except Exception:
            ts_s, ago = str(ts), ""
        print(f"{r.get('id'):>4} {str(r.get('trigger')):>8} {str(r.get('sent')):>5} "
              f"{str(r.get('failed')):>7}  {ts_s}{ago}")
except Exception as e:
    print("ERR:", e)
conn.close()

hdr("2. Are posts happening back-to-back (timing broken)?")
conn = db.get_conn()
try:
    rows = conn.execute(
        "SELECT * FROM auto_log ORDER BY id DESC LIMIT 12"
    ).fetchall()
    stamps = []
    for r in rows:
        ts = r.get("created") or r.get("created_at") or r.get("ts")
        try:
            stamps.append(int(ts))
        except Exception:
            pass
    if len(stamps) >= 2:
        gaps = [stamps[i] - stamps[i + 1] for i in range(len(stamps) - 1)]
        print("gaps between consecutive runs (seconds), newest first:")
        print("  ", gaps)
        expected_min = 3600  # 1 hour
        tiny = [g for g in gaps if 0 <= g < expected_min]
        if tiny:
            print(f"\n  ❌ {len(tiny)} run(s) fired WAY earlier than the 1h minimum!")
            print("     -> the 1-2h window is being ignored")
        else:
            print("\n  ✅ all gaps respect the >=1h window")
except Exception as e:
    print("ERR:", e)
conn.close()

hdr("3. CONTROL FLOW BUG CHECK — scheduler_loop line 464")
src = io.open(os.path.join(BASE, "bot.py"), encoding="utf-8").read()
i = src.find("next_at = get_next_at()\n            if not next_at")
print("Found the branch condition:")
print("    if not next_at or next_at < int(time.time()):   <-- enters the WAIT block")
print()
print("Then, AFTER the wait block, the share call is UNCONDITIONAL:")
seg = src[i:i + 2200]
print(seg[seg.find("            await run_share") - 300:seg.find("await run_share") + 200])
print()
print("  ^ This line is at the SAME indent level as the `if`, so it runs")
print("    on EVERY loop iteration -- including when next_at is in the FUTURE.")
print()
print("  Consequence: when next_at is in the future, the `if` is False, the")
print("  wait block is skipped, and it posts IMMEDIATELY.")

hdr("4. SIMULATE the loop to prove it")
import asyncio


async def simulate(bg_next_at, iterations=4):
    """Replicate the exact control flow, printing what happens each pass."""
    state = {"enabled": True, "next": bg_next_at, "posts": 0}
    log = []
    for _ in range(iterations):
        if not state["enabled"]:
            log.append("  (disabled, sleep 5)")
            break
        next_at = state["next"]
        if not next_at or next_at < int(time.time()):
            gap = auto.pick_next_gap()
            state["next"] = int(time.time()) + gap
            log.append(f"  -> no/invalid next_at: scheduled +{gap}s, WAITING")
        else:
            log.append(f"  -> next_at is FUTURE (+{next_at - int(time.time())}s): "
                       f"WAIT BLOCK SKIPPED")
        state["posts"] += 1
        log.append(f"  -> 📤 POST #{state['posts']} sent right now")
        gap = auto.pick_next_gap()
        state["next"] = int(time.time()) + gap
    return log, state["posts"]


print("\nSCENARIO A: next_at is 1 hour in the FUTURE (the normal steady state)")
log, posts = asyncio.run(simulate(now + 3600))
for line in log[:8]:
    print(line)
print(f"\n  => {posts} posts in 4 loop iterations, ignoring the 1h wait. BROKEN.")

print("\nSCENARIO B: next_at is EMPTY (fresh start)")
log, posts = asyncio.run(simulate(0))
for line in log[:4]:
    print(line)
print("\n  => first pass waits correctly, then behaviour degenerates.")

hdr("5. Is the live bot currently stuck in this loop?")
live_next = int(db.get_setting("auto_next_at") or 0)
print(f"  auto_next_at = {live_next}  ({live_next - now:+d}s from now)")
print(f"  auto_enabled = {db.get_setting('auto_enabled')!r}")
print(f"  message set  = {bool(auto.get_message())}")
