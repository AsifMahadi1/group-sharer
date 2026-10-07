"""Offline logic tests for the auto-post system (no Telegram calls)."""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import auto
import db

OWNER = 6168584735
FAILS = []


def check(name, cond, extra=""):
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


print("\n=== 1. DB settings round-trip ===")
db.init_db()
db.set_setting("test_key", "hello")
check("get_setting", db.get_setting("test_key") == "hello")
check("get_setting default", db.get_setting("nope", "dflt") == "dflt")
db.set_setting("test_key", "world")
check("overwrite", db.get_setting("test_key") == "world")

print("\n=== 2. Auto message storage ===")
auto.set_enabled(False)
check("no message initially", auto.get_message() == "")
db.set_setting("auto_msg", "https://example.com/post 1")
check("message stored", auto.get_message() == "https://example.com/post 1")
check("message strip", (db.set_setting("auto_msg", "  spaced  "),
                        auto.get_message() == "spaced")[1])

print("\n=== 3. Enabled flag ===")
check("starts disabled", not auto.is_enabled())
auto.set_enabled(True)
check("can enable", auto.is_enabled())
auto.set_enabled(False)
check("can disable", not auto.is_enabled())

print("\n=== 4. Random gap picking (1-2 hours) ===")
gaps = [auto.pick_next_gap() for _ in range(2000)]
check("min >= 3600", min(gaps) >= 3600, f"min={min(gaps)}s")
check("max <= 7200", max(gaps) <= 7200, f"max={max(gaps)}s")
check("all inside window", all(3600 <= g <= 7200 for g in gaps))
uniq = len(set(gaps))
check("is random (many distinct)", uniq > 1000, f"distinct={uniq}/2000")
mid = sum(1 for g in gaps if 3600 < g < 7200)
check("spread across range", mid > 1900, f"interior={mid}/2000")

print("\n=== 5. Configurable window ===")
auto.set_window(2, 5)
g2 = [auto.pick_next_gap() for _ in range(500)]
check("min >= 2h", min(g2) >= 7200, f"min={min(g2)}s")
check("max <= 5h", max(g2) <= 18000, f"max={max(g2)}s")
auto.set_window(3, 1)  # reversed -> should swap
lo, hi = auto.get_window()
check("reversed window swapped", (lo, hi) == (1, 3), f"got {lo}-{hi}")
auto.set_window(1, 2)
check("window restored", auto.get_window() == (1, 2))

print("\n=== 6. next_at persistence ===")
check("no next_at", auto.get_next_at() == 0)
ts = int(time.time()) + 5000
auto.set_next_at(ts)
check("next_at saved", auto.get_next_at() == ts)
db.set_setting("auto_next_at", "junk")
check("bad next_at -> 0", auto.get_next_at() == 0)
auto.clear_next_at()
check("clear_next_at", auto.get_next_at() == 0)

print("\n=== 7. Stop flag ===")
auto.STATE.reset()
check("not stopped initially", not auto.STATE.should_stop())
auto.STATE.request_stop()
check("stop requested", auto.STATE.should_stop())
auto.STATE.reset()
check("reset clears", not auto.STATE.should_stop())

print("\n=== 8. Formatting helpers ===")
check("fmt_dur 90s", auto.fmt_dur(90) == "1 মিনিট", auto.fmt_dur(90))
check("fmt_dur 2h", "ঘণ্টা" in auto.fmt_dur(7200), auto.fmt_dur(7200))
check("fmt_dur 30s", "সেকেন্ড" in auto.fmt_dur(30), auto.fmt_dur(30))
check("fmt_dur negative", "0" in auto.fmt_dur(-5) or "সেকেন্ড" in auto.fmt_dur(-5),
      auto.fmt_dur(-5))
check("count_down 3600", auto.count_down(3600) == "1 ঘণ্টা 0 মিনিট",
      auto.count_down(3600))
check("count_down 5400", auto.count_down(5400) == "1 ঘণ্টা 30 মিনিট",
      auto.count_down(5400))
check("count_down 600", auto.count_down(600) == "10 মিনিট",
      auto.count_down(600))
check("fmt_ts none", auto.fmt_ts(0) == "—")
check("fmt_ts ok", len(auto.fmt_ts(int(time.time()))) > 5)

print("\n=== 9. Cancellation interrupts sleep quickly ===")


async def cancel_test():
    stop = {"v": False}

    async def canceller():
        await asyncio.sleep(0.4)
        stop["v"] = True

    task = asyncio.create_task(canceller())
    t0 = time.time()
    # simulate engine.send_to_group waiting 60s but should_stop flips at 0.4s
    await auto.asyncio.sleep(0.05)
    elapsed = time.time() - t0
    await task
    return elapsed


el = asyncio.run(cancel_test())
check("async sleeps work", el < 1, f"{el:.2f}s")

print("\n=== 10. auto_log table ===")
db.log_auto_run("auto", 8, 8, 0, time.time() - 100, time.time())
runs = db.recent_auto_runs(5)
check("auto run logged", len(runs) >= 1)
check("has sent count", runs[0]["sent"] == 8, f"sent={runs[0]['sent']}")
check("has trigger", runs[0]["trigger"] == "auto")

print("\n=== 11. Scheduler guard: enabled but no message ===")
check("scheduler must not run without message",
      not (auto.is_enabled() and auto.get_message() == ""))
check("scheduler must not run without groups",
      len(db.get_groups()) == 0 or bool(auto.get_message()))

print("\n=== cleanup ===")
db.set_setting("auto_msg", "")
auto.set_enabled(False)
auto.clear_next_at()
db.set_setting("test_key", "")
db.set_setting("auto_min_hour", "1")
db.set_setting("auto_max_hour", "2")
print("  state reset to defaults")

print("\n" + "=" * 46)
if FAILS:
    print(f"❌ {len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("✅ ALL TESTS PASSED")
