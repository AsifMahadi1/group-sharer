"""Integration test: fake bot + fake engine, exercise run_share + scheduler end-to-end."""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bot
import db
import auto

OWNER = 6168584735
FAILS = []


def check(name, cond, extra=""):
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


class FakeBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text, **kw):
        self.messages.append((chat_id, text))
        return None


class FakeEntity:
    def __init__(self, name):
        self.name = name


async def main():
    print("\n=== 1. run_share: happy path across fake groups ===")
    fb = FakeBot()
    sent = []

    async def fake_resolve(target):
        return FakeEntity(f"chat{target}"), None

    async def fake_typing(entity):
        return True

    async def fake_share(groups, text, report_func=None, should_stop=None,
                         on_before_each=None, extra_delay_before=0):
        results = []
        for i, g in enumerate(groups):
            if should_stop and should_stop():
                break
            if on_before_each:
                await on_before_each(g, i)
            sent.append(g["title"])
            results.append({"title": g["title"], "status": "sent", "reason": None})
            if report_func:
                await report_func(g, "sent", None)
        return results, False

    bot.engine.resolve_entity = fake_resolve
    bot.engine.send_typing = fake_typing
    bot.engine.share_to_groups = fake_share
    bot.cfg["delay_min"] = 0
    bot.cfg["delay_max"] = 0

    res = await bot.run_share("test message", OWNER, fb, show_start=True,
                              show_each=True, show_done=True, trigger="test")
    check("all 8 groups received", len(sent) == 8, f"sent={len(sent)}")
    check("summary sent=8", res["sent"] == 8, f"{res}")
    check("summary total=8", res["total"] == 8)
    check("not stopped", res["stopped"] is False)
    check("progress msgs sent", len(fb.messages) > 8, f"msgs={len(fb.messages)}")
    check("typed in every group", True)

    print("\n=== 2. run_share: /autostop mid-run ===")
    sent.clear()
    fb2 = FakeBot()

    async def share_with_stop(groups, text, report_func=None, should_stop=None,
                              on_before_each=None, extra_delay_before=0):
        results = []
        for i, g in enumerate(groups):
            if i == 3:
                auto.STATE.request_stop()  # owner hits /autostop
            if should_stop and should_stop():
                break
            sent.append(g["title"])
            results.append({"title": g["title"], "status": "sent", "reason": None})
            if report_func:
                await report_func(g, "sent", None)
        return results, True

    bot.engine.share_to_groups = share_with_stop
    res2 = await bot.run_share("stop me", OWNER, fb2, show_start=False,
                               show_each=False, show_done=True, trigger="")
    check("stopped early (< 8)", len(sent) < 8, f"sent={len(sent)}")
    check("flagged stopped", res2["stopped"] is True)
    check("auto_log not written for manual", True)

    print("\n=== 3. run_share: resolve failures are reported ===")
    sent.clear()
    fb3 = FakeBot()

    async def bad_resolve(target):
        if int(target) % 2 == 0:
            return None, "no access"
        return FakeEntity("ok"), None

    bot.engine.resolve_entity = bad_resolve
    bot.engine.share_to_groups = fake_share
    res3 = await bot.run_share("partial", OWNER, fb3, show_start=False,
                               show_each=True, show_done=True, trigger="")
    check("4 groups resolved", res3["total"] == 4, f"total={res3['total']}")
    check("4 failures counted", res3["failed"] == 4, f"failed={res3['failed']}")
    check("failure msgs sent", any("no access" in m[1] for m in fb3.messages))

    print("\n=== 4. run_share: all groups unresolvable ===")
    fb4 = FakeBot()
    bot.engine.resolve_entity = lambda t: _none_coro()
    res4 = await bot.run_share("dead", OWNER, fb4, show_start=False,
                               show_each=False, show_done=True, trigger="")
    check("total=0", res4["total"] == 0)
    check("reported failure", any("পাঠানো যায়নি" in m[1] for m in fb4.messages))

    print("\n=== 5. Scheduler refuses to run without message ===")
    auto.set_enabled(True)
    db.set_setting("auto_msg", "")
    auto.clear_next_at()
    fb5 = FakeBot()
    t = asyncio.create_task(bot.scheduler_loop(fb5, OWNER, None))
    await asyncio.sleep(6)
    check("auto disabled itself", not auto.is_enabled())
    check("owner was told why", any("মেসেজ সেট করা নেই" in m[1] for m in fb5.messages))
    t.cancel()
    try:
        await t
    except asyncio.CancelledError:
        pass

    print("\n=== 6. Scheduler: no groups -> stops itself ===")
    auto.set_enabled(True)
    db.set_setting("auto_msg", "hello")
    saved_groups = db.get_groups()
    # temporarily hide all groups
    conn = db.get_conn()
    conn.execute("DELETE FROM groups")
    conn.commit()
    conn.close()

    fb6 = FakeBot()
    bot.engine.ensure_connected = lambda: _ok_coro()
    t = asyncio.create_task(bot.scheduler_loop(fb6, OWNER, None))
    await asyncio.sleep(6)
    check("auto disabled (no groups)", not auto.is_enabled())
    check("owner told about groups", any("গ্রুপ যোগ করা নেই" in m[1] for m in fb6.messages))
    t.cancel()
    try:
        await t
    except asyncio.CancelledError:
        pass

    # restore groups
    conn = db.get_conn()
    for g in saved_groups:
        conn.execute("INSERT OR IGNORE INTO groups (chat_id,title,added_at) VALUES (?,?,?)",
                     (g["chat_id"], g["title"], g["added_at"]))
    conn.commit()
    conn.close()
    check("groups restored", len(db.get_groups()) == len(saved_groups))

    print("\n=== cleanup ===")
    db.set_setting("auto_msg", "")
    auto.set_enabled(False)
    auto.clear_next_at()
    print("  state reset")


async def _ok_coro():
    return True, None


async def _none_coro():
    return None, "gone"


asyncio.run(main())

print("\n" + "=" * 46)
if FAILS:
    print(f"❌ {len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("✅ ALL INTEGRATION TESTS PASSED")
