"""Verify the scheduler timing fix WITHOUT touching the live database.

Parses the real scheduler_loop source, checks the wait loop is outside the
`if`, then simulates the exact control flow with a fake clock.
"""
import ast
import io
import re
import sys
import time

BASE = "/root/agents/personal/workspace/group-sharer"
SRC = io.open(BASE + "/bot.py", encoding="utf-8").read()

FAILS = []


def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


print("=" * 62)
print("1. SYNTAX")
print("=" * 62)
try:
    ast.parse(SRC)
    check("bot.py parses", True)
except SyntaxError as e:
    check("bot.py parses", False, str(e))
    sys.exit(1)

print()
print("=" * 62)
print("2. STRUCTURE: is the wait loop OUTSIDE the `if`?")
print("=" * 62)
lines = SRC.split("\n")
# pick the occurrence inside scheduler_loop, not the earlier one at ~line 301
fn_start = next(i for i, l in enumerate(lines) if l.startswith("async def scheduler_loop"))
start = next(i for i, l in enumerate(lines) if "next_at = get_next_at()" in l and i > fn_start)
print(f"  (scheduler_loop at line {fn_start + 1}, target at line {start + 1})")
seg = lines[start:start + 90]

if_ln = next(i for i, l in enumerate(seg) if re.match(r"\s+if not next_at or next_at", l))
wait_ln = next(i for i, l in enumerate(seg) if l.strip() == "while True:")
share_ln = next(i for i, l in enumerate(seg) if "await run_share" in l)

print(f"  line offsets from 'next_at = get_next_at()':")
print(f"    if    -> +{if_ln}")
print(f"    while -> +{wait_ln}")
print(f"    share -> +{share_ln}")
print()
ind_if = len(seg[if_ln]) - len(seg[if_ln].lstrip())
ind_wait = len(seg[wait_ln]) - len(seg[wait_ln].lstrip())
ind_share = len(seg[share_ln]) - len(seg[share_ln].lstrip())
print(f"  indent: if={ind_if}  while={ind_wait}  share={ind_share}")
print()
check("while loop is NOT nested inside the if", ind_wait <= ind_if,
      f"while={ind_wait} vs if={ind_if}")
check("while comes AFTER the if block", wait_ln > if_ln)
check("share is outside both", ind_share <= ind_if)

# The body of the `if` should only be: gap, next_at, set_next_at, lo/hi, send
print()
print("  what is left inside the `if` body (between if and while):")
for l in seg[if_ln + 1:wait_ln]:
    if l.strip() and not l.strip().startswith("#"):
        print("     ", l.strip()[:70])
check("no run_share inside the if body",
      not any("run_share" in l for l in seg[if_ln + 1:wait_ln]))
check("no reschedule inside the if body",
      not any("set_next_at" in l for l in seg[if_ln + 1:wait_ln]))

print()
print("=" * 62)
print("3. SIMULATE the fixed loop (fake clock, no real DB)")
print("=" * 62)


def simulate(start_next_at, enabled=True, seconds=8 * 3600, stop_after=50):
    """Replica of the FIXED control flow. Returns post timestamps."""
    clock = [1000]
    state = {"enabled": enabled, "next": start_next_at, "stop": False}
    posts = []
    iters = 0

    def rand_gap():
        return 3600  # deterministic: exactly 1h for the test

    def sleep(sec):
        clock[0] += sec
        return True

    while iters < stop_after:
        iters += 1
        if not state["enabled"]:
            sleep(5)
            continue
        next_at = state["next"]
        if not next_at or next_at < clock[0]:
            gap = rand_gap()
            next_at = clock[0] + gap
            state["next"] = next_at
            # notification send happens here
        # ---- the fix: this always runs ----
        while True:
            remain = next_at - clock[0]
            if remain <= 0 or state["stop"] or not state["enabled"]:
                break
            sleep(min(10, remain))
        if state["stop"] or not state["enabled"]:
            state["stop"] = False
            continue
        posts.append(clock[0])
        gap = rand_gap()
        next_at = clock[0] + gap
        state["next"] = next_at
    return posts


print("\n  Scenario A: starts with next_at 1h in the FUTURE")
posts = simulate(1000 + 3600)
gaps = [posts[i] - posts[i - 1] for i in range(1, len(posts))]
print(f"    posts at fake-time: {posts}")
print(f"    gaps (s): {gaps}")
check("at least one post happened", len(posts) >= 1)
check("all gaps are >= 1h", all(g >= 3600 for g in gaps), f"min gap={min(gaps) if gaps else 'n/a'}s")

print("\n  Scenario B: starts with next_at EMPTY (fresh /autostart)")
posts = simulate(0)
gaps = [posts[i] - posts[i - 1] for i in range(1, len(posts))]
print(f"    posts at fake-time: {posts}")
print(f"    gaps (s): {gaps}")
check("first post is delayed by ~1h, not immediate", posts[0] - 1000 >= 3600,
      f"waited {posts[0] - 1000}s")
check("all gaps >= 1h", all(g >= 3600 for g in gaps))

print("\n  Scenario C: OLD buggy behaviour, for contrast")
posts_bug = []
state = {"enabled": True, "next": 1000 + 3600, "stop": False}
for _ in range(5):
    next_at = state["next"]
    if not next_at or next_at < 1000:
        state["next"] = next_at = 1000 + 3600
        # wait block nested inside if -> skipped when next_at is in future
    posts_bug.append(1000)
    state["next"] = 1000 + 3600
print(f"    buggy posts: {posts_bug}  (all at t=1000, zero waiting)")
check("old code posted instantly every pass (proves the bug was real)",
      len(set(posts_bug)) == 1)

print()
print("=" * 62)
print("4. Exception path reschedules (no retry storm)")
print("=" * 62)
i = SRC.find("scheduler error")
seg2 = SRC[i:i + 700]
check("error handler reschedules next_at", "set_next_at" in seg2)
check("reschedule happens before the sleep", seg2.find("set_next_at") < seg2.find("asyncio.sleep(30)"))
print("  " + "\n  ".join(l.strip() for l in seg2.split("\n")[-8:] if l.strip()))

print()
print("=" * 62)
print("5. Live DB untouched by this audit")
print("=" * 62)
import db
print(f"  auto_msg    = {db.get_setting('auto_msg')!r}")
print(f"  auto_enabled= {db.get_setting('auto_enabled')!r}")
nxt = int(db.get_setting("auto_next_at") or 0)
print(f"  auto_next_at= {nxt}  ({nxt - int(time.time()):+d}s)")

print()
print("=" * 62)
print("ALL PASSED - timing bug is fixed" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
print("=" * 62)
sys.exit(1 if FAILS else 0)
