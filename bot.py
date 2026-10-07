"""Telegram bot: /login, group management, share links."""
import asyncio
import json
import os
import random
import re
import time

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

import db
from auto import (
    STATE,
    count_down,
    fmt_dur,
    fmt_ts,
    get_message,
    get_next_at,
    get_window,
    is_enabled,
    pick_next_gap,
    set_enabled,
    set_next_at,
    set_window,
    clear_next_at,
)
from engine import UserbotEngine

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


# ---------- Config / state ----------
cfg = load_config()
OWNER_ID = cfg["owner_id"]
engine = UserbotEngine()

# Login state machine: user_id -> state dict
login_states = {}
# Share state: user_id -> {text, pending}
share_states = {}
# Auto-post message setup: user_id -> {"step": "await_msg"}
auto_states = {}

URL_RE = re.compile(r"(https?://\S+|t\.me/\S+|@\w+)", re.IGNORECASE)


# ---------- Helpers ----------
def is_owner(update: Update) -> bool:
    return update.effective_user.id == OWNER_ID


def owner_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_owner(update):
            await update.message.reply_text("⛔ শুধু Owner এই বট ব্যবহার করতে পারবেন।")
            return
        return await func(update, context)

    return wrapper


def normalize_id(chat_id):
    """Canonical comparable form of a Telegram chat id.

    DB stores raw entity.id (e.g. 4484263789) while dialogs come back as
    utils.get_peer_id (e.g. -1004484263789). Both must compare equal.
    """
    try:
        chat_id = int(chat_id)
    except (TypeError, ValueError):
        return chat_id
    if chat_id <= -1000000000000:
        return abs(chat_id) - 1000000000000   # -100XXXXXXXXXX -> XXXXXXXXXX
    if chat_id < 0:
        return -chat_id                  # basic group: -XXXXXXX -> XXXXXXX
    return chat_id


def saved_group_ids():
    """Canonical set of chat_ids currently saved for posting."""
    return {normalize_id(g["chat_id"]) for g in db.get_groups()}


def fmt_checkgroups(chats, saved_ids):
    """Format joined chats, marking the ones already added for posting with checkmark."""
    saved_ids = {normalize_id(i) for i in saved_ids}
    total = len(chats)
    added = sum(1 for c in chats if normalize_id(c["chat_id"]) in saved_ids)
    header = (
        f"📚 **আপনার জয়েন "
        f"করা {total}টি গ্রুপ/"
        f"চ্যানেল**\n"
        f"✅ পোস্টে যুক্ত: "
        f"**{added}** টি  |  ➕ বাকি: "
        f"**{total - added}** টি\n\n"
        "✅ = ইতোমধ্যে `/addgroup` করা "
        "(অটো-পোস্টে যাবে)\n"
        "➕ = এখনো যোগ করা "
        "হয়নি — আইডি কপি "
        "করে `/addgroup <আইডি>` দিন\n\n"
    )

    chunks, cur = [], header
    for i, c in enumerate(chats, 1):
        mark = ("✅" if normalize_id(c["chat_id"]) in saved_ids
                else "➕")
        line = (f"{mark} {i}. {c['title'][:45]}\n"
                f"   `{c['chat_id']}` — {c['type']}"
                + (f" {c['username']}" if c["username"] != "—" else "") + "\n")
        if len(cur) + len(line) > 3900:
            chunks.append(cur)
            cur = ""
        cur += line
    chunks.append(cur)
    return chunks


def fmt_groups_list(groups):
    if not groups:
        return "📭 কোনো গ্রুপ যোগ করা হয়নি।\n\n`/addgroup <লিংক/আইডি>` দিয়ে যোগ করুন।"
    lines = []
    for i, g in enumerate(groups, 1):
        lines.append(f"{i}. {g['title']} (`{g['chat_id']}`)")
    return "📋 সিলেক্ট করা গ্রুপ:\n\n" + "\n".join(lines)


async def resolve_group_and_add(target: str) -> str:
    """Resolve a group link/username and add it."""
    entity, err = await engine.resolve_entity(target)
    if err:
        return f"❌ গ্রুপ পাওয়া যায়নি: {err}\n\nনিশ্চিত করুন আপনার অ্যাকাউন্ট গ্রুপে মেম্বার আছে এবং লিংক/আইডি সঠিক।"
    try:
        chat_id = entity.id
        title = getattr(entity, "title", None) or entity.username or str(entity.id)
        ok = db.add_group(chat_id, title)
        if not ok:
            return f"⚠️ গ্রুপটি আগে থেকেই যোগ করা আছে: **{title}**"
        return f"✅ গ্রুপ যোগ হয়েছে: **{title}**\n`{chat_id}`"
    except Exception as e:
        return f"❌ যোগ করা যায়নি: {e}"


# ---------- Menu ----------
def build_menu_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📋 গ্রুপ লিস্ট", callback_data="menu:groups"),
            InlineKeyboardButton("➕ গ্রুপ যোগ", callback_data="menu:addgroup"),
        ],
        [
            InlineKeyboardButton("🔍 চেক গ্রুপস", callback_data="menu:checkgroups"),
            InlineKeyboardButton("🗑️ গ্রুপ রিমুভ", callback_data="menu:removegroup"),
        ],
        [
            InlineKeyboardButton("📊 স্ট্যাটাস", callback_data="menu:status"),
            InlineKeyboardButton("📜 লগস", callback_data="menu:logs"),
        ],
        [
            InlineKeyboardButton("🔑 লগইন", callback_data="menu:login"),
            InlineKeyboardButton("🧹 সব মুছুন", callback_data="menu:cleargroups"),
        ],
        [
            InlineKeyboardButton("🤖 অটো-পোস্ট", callback_data="menu:auto"),
        ],
    ])

MENU_TEXT = (
    "📚 **কমান্ড মেনু**\n\n"
    "নিচের বাটন থেকে যেকোনো অপশন বেছে নিন:\n\n"
    "📋 গ্রুপ লিস্ট — যোগ করা গ্রুপ দেখে\n"
    "➕ গ্রুপ যোগ — লিংক/আইডি দিয়ে গ্রুপ যোগ\n"
    "🔍 চেক গ্রুপস — অ্যাকাউন্টের সব জয়েন করা গ্রুপ (প্রাইভেট সহ)\n"
    "🗑️ গ্রুপ রিমুভ — গ্রুপ বাদ দেওয়া\n"
    "📊 স্ট্যাটাস — লগইন/গ্রুপ অবস্থা\n"
    "📜 লগস — শেয়ার রিপোর্ট\n"
    "🔑 লগইন — অ্যাকাউন্ট লগইন\n"
    "🧹 সব মুছুন — সব গ্রুপ মুছে ফেলা\n"
    "🤖 অটো-পোস্ট — সেট মেসেজ নিয়মিত সব গ্রুপে পোস্ট"
)

AUTO_HELP = (
    "🤖 **অটো-পোস্ট সিস্টেম**\n\n"
    "**কীভাবে কাজ করে:**\n"
    "1. `/setMsg` দিন → বট মেসেজ পাঠাতে বলবে\n"
    "2. মেসেজটি পাঠান → সেভ হয়ে যাবে\n"
    "3. `/autostart` দিন → অটো-পোস্ট চালু\n\n"
    "**পোস্ট কীভাবে যায়:**\n"
    f"• সব গ্রুপে **{cfg['delay_min']}-{cfg['delay_max']} সেকেন্ড** বিরতিতে ধীরে ধীরে\n"
    "• পাঠানোর ঠিক আগে `typing...` দেখাবে (ট্রাকিং এড়াতে)\n"
    "• পুরো রাউন্ড শেষ হলে **১-২ ঘণ্টার মধ্যে র‍্যান্ডম** সময় বেছে নিয়ে আবার শুরু\n"
    "• বট বন্ধ/রিস্টার্ট হলেও সেটেড মেসেজ মনে থাকে\n\n"
    "**কমান্ড:**\n"
    "`/setMsg` — মেসেজ সেট করুন\n"
    "`/autostart` — চালু করুন\n"
    "`/autostop` — বন্ধ করুন\n"
    "`/astatus` — স্ট্যাটাস দেখুন\n"
)

@owner_only
async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(MENU_TEXT, reply_markup=build_menu_keyboard(), parse_mode="Markdown")

async def menu_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    val = query.data.split(":", 1)[1]

    if val == "groups":
        await query.edit_message_text(fmt_groups_list(db.get_groups()), parse_mode="Markdown")
    elif val == "addgroup":
        await query.edit_message_text(
            "➕ **গ্রুপ যোগ করতে** এই ফরম্যাটে লিখুন:\n\n"
            "`/addgroup https://t.me/example`\n"
            "`/addgroup @example`\n"
            "`/addgroup https://t.me/+abc123` (প্রাইভেট ইনভাইট লিংক)\n"
            "`/addgroup -1001234567890` (নিউমেরিক আইডি)",
            parse_mode="Markdown",
        )
    elif val == "checkgroups":
        ok, err = await engine.ensure_connected()
        if not ok:
            return
        await query.edit_message_text("🔍 আপনার জয়েন করা গ্রুপ/চ্যানেল খোঁজা হচ্ছে...")
        chats, err = await engine.list_joined_chats()
        if err:
            await query.edit_message_text(f"❌ তালিকা আনা যায়নি: {err}")
            return
        if not chats:
            await query.edit_message_text("📭 আপনার অ্যাকাউন্টে কোনো গ্রুপ/চ্যানেল নেই।")
            return
        chunks = fmt_checkgroups(chats, saved_group_ids())
        await query.edit_message_text(chunks[0], parse_mode="Markdown")
        for ch in chunks[1:]:
            await context.bot.send_message(update.effective_user.id, ch, parse_mode="Markdown")
    elif val == "removegroup":
        groups = db.get_groups()
        if not groups:
            await query.edit_message_text("📭 কোনো গ্রুপ নেই।")
            return
        keyboard = [
            [InlineKeyboardButton(f"{i}. {g['title'][:30]}", callback_data=f"rm:{i-1}")]
            for i, g in enumerate(groups, 1)
        ]
        keyboard.append([InlineKeyboardButton("❌ বাতিল", callback_data="rm:cancel")])
        await query.edit_message_text(
            "🗑️ কোন গ্রুপ রিমুভ করবেন?",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    elif val == "status":
        logged = engine.is_logged_in
        groups = db.get_groups()
        await query.edit_message_text(
            f"📊 **স্ট্যাটাস**\n\n"
            f"👤 ইউজারবট: {'✅ লগইন করা' if logged else '❌ লগইন করা হয়নি'}\n"
            f"👥 গ্রুপ: {len(groups)} টি\n\n"
            f"লগইন করতে: `/login`",
            parse_mode="Markdown",
        )
    elif val == "logs":
        logs = db.recent_logs(10)
        if not logs:
            await query.edit_message_text("📭 এখনো কোনো শেয়ার হয়নি।")
            return
        lines = ["📜 **সাম্প্রতিক শেয়ার:**\n"]
        for l in logs:
            icon = "✅" if l["status"] == "sent" else "❌"
            lines.append(
                f"{icon} {l['group_title']} — {l['status']}"
                + (f" ({l['reason']})" if l["reason"] else "")
            )
        await query.edit_message_text("\n".join(lines), parse_mode="Markdown")
    elif val == "login":
        uid = update.effective_user.id
        login_states[uid] = {"step": "phone"}
        await query.edit_message_text(
            "🔑 **লগইন শুরু**\n\n"
            "আপনার ফোন নম্বর দিন (আন্তর্জাতিক ফরম্যাটে, যেমন `+8801XXXXXXXXX`):\n\n"
            "বাতিল করতে `/cancel` লিখুন।",
            parse_mode="Markdown",
        )
    elif val == "cleargroups":
        db.clear_groups()
        await query.edit_message_text("🧹 সব গ্রুপ মুছে ফেলা হয়েছে।")

    elif val == "auto":
        text = get_message()
        enabled = is_enabled()
        lo, hi = get_window()
        next_at = get_next_at()

        lines = ["🤖 **অটো-পোস্ট**\n"]
        lines.append(f"অবস্থা: {'🟢 চালু' if enabled else '🔴 বন্ধ'}")
        lines.append(f"গ্রুপ: {len(db.get_groups())} টি")
        lines.append(f"ইন্টারভাল: {lo}-{hi} ঘণ্টা (র‍্যান্ডম)")
        if text:
            prev = text if len(text) <= 100 else text[:100] + "…"
            lines.append(f"\n📝 মেসেজ: `{prev}`")
        else:
            lines.append("\n📝 মেসেজ: ❌ সেট করা নেই")
        if enabled and next_at:
            lines.append(f"\n⏭️ পরবর্তী পোস্ট: **{fmt_ts(next_at)}**")

        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("📝 মেসেজ সেট", callback_data="auto:setmsg"),
                    InlineKeyboardButton("ℹ️ তথ্য", callback_data="auto:info"),
                ],
                [
                    InlineKeyboardButton("▶️ চালু", callback_data="auto:start"),
                    InlineKeyboardButton("🛑 বন্ধ", callback_data="auto:stop"),
                ],
                [InlineKeyboardButton("🔙 মেনু", callback_data="menu:groups")],
            ]),
        )

# ---------- Core share runner ----------
async def resolve_groups():
    """Resolve all configured groups. Returns (entities, failures)."""
    groups = db.get_groups()
    entities, failures = [], []
    for g in groups:
        ent, err = await engine.resolve_entity(g["chat_id"])
        if err:
            failures.append({"title": g["title"], "reason": f"resolve: {err}"})
        else:
            entities.append({"chat_id": g["chat_id"], "title": g["title"], "entity": ent})
    return entities, failures


async def run_share(text, owner_id, bot, show_start=True, show_each=True,
                    show_done=True, extra_delay_before=0, trigger="manual"):
    """Share `text` to all groups with slow random delays.

    Returns summary dict. Uses STATE.should_stop() so /autostop can abort.
    """
    STATE.reset()
    if show_start:
        await bot.send_message(
            owner_id,
            f"📤 **শেয়ার শুরু হয়েছে...**\nতারপর পাঠানো হচ্ছে "
            f"{len(db.get_groups())} টি গ্রুপে।\n\n"
            f"প্রতিটি গ্রুপে {cfg['delay_min']}-{cfg['delay_max']} সেকেন্ড বিরতি।\n"
            f"🛑 থামাতে `/autostop`",
        )

    entities, failures = await resolve_groups()
    if failures and show_each:
        for f in failures:
            db.log_share(0, f["title"], "error", f["reason"], text)
            await bot.send_message(owner_id, f"❌ **{f['title']}** — {f['reason']}")

    if not entities:
        if show_done:
            await bot.send_message(owner_id, "❌ কোনো গ্রুপে পাঠানো যায়নি (সব রেজলভ ব্যর্থ)।")
        return {"total": 0, "sent": 0, "failed": len(failures), "stopped": True,
                "results": []}

    async def report(g, status, reason):
        db.log_share(g["chat_id"], g["title"], status, reason, text)
        if not show_each:
            return
        icon = "✅" if status == "sent" else "❌"
        tail = "" if status == "sent" else f"\n↳ {reason}"
        await bot.send_message(owner_id, f"{icon} **{g['title']}**{tail}")

    async def before_each(g, i):
        # Human-like "typing..." right before the message lands
        await engine.send_typing(g["entity"])
        if show_each and len(entities) > 1:
            await bot.send_message(
                owner_id, f"⏳ পাঠানো হচ্ছে ({i+1}/{len(entities)}) — **{g['title']}**"
            )

    started = time.time()
    results, stopped = await engine.share_to_groups(
        entities,
        text,
        report_func=report,
        should_stop=STATE.should_stop,
        on_before_each=before_each,
        extra_delay_before=extra_delay_before,
    )

    ok_count = sum(1 for r in results if r["status"] == "sent")
    fail_count = (len(results) - ok_count) + len(failures)
    finished = time.time()

    if trigger:
        db.log_auto_run(trigger, len(entities), ok_count, fail_count, started, finished)

    if show_done:
        lines = [f"📊 **শেয়ার সম্পন্ন!**\n", f"✅ সফল: {ok_count}", f"❌ ব্যর্থ: {fail_count}"]
        if stopped:
            lines.append("\n🛑 মাঝপথে থামানো হয়েছে।")
        lines.append(f"\n⏱️ সময় লেগেছে: {fmt_dur(finished - started)}")
        lines.append("বিস্তারিত: `/logs`")
        await bot.send_message(owner_id, "\n".join(lines))

    return {"total": len(entities), "sent": ok_count, "failed": fail_count,
            "stopped": stopped, "results": results}


# ---------- Auto scheduler ----------
async def scheduler_loop(bot, owner_id, application):
    """sleep until next_at -> share -> pick new random 1-2h -> repeat."""
    print("⏰ Auto scheduler task started.")
    while True:
        try:
            if not is_enabled():
                await asyncio.sleep(5)
                continue

            text = get_message()
            groups = db.get_groups()

            if not text:
                clear_next_at()
                await bot.send_message(
                    owner_id,
                    "⏸️ **অটো-পোস্ট বন্ধ** — কোনো মেসেজ সেট করা নেই।\n\n"
                    "মেসেজ দিতে `/setMsg` লিখুন।",
                )
                set_enabled(False)
                await asyncio.sleep(5)
                continue

            if not groups:
                await bot.send_message(
                    owner_id,
                    "⏸️ **অটো-পোস্ট বন্ধ** — কোনো গ্রুপ যোগ করা নেই।\n"
                    "`/addgroup <লিংক>` দিয়ে গ্রুপ যোগ করুন।",
                )
                set_enabled(False)
                await asyncio.sleep(5)
                continue

            ok, err = await engine.ensure_connected()
            if not ok:
                await bot.send_message(
                    owner_id,
                    f"⚠️ **অটো-পোস্ট বন্ধ** — ইউজারবট কানেক্ট হয়নি: `{err}`\n"
                    f"আগে `/login` দিন।",
                )
                set_enabled(False)
                await asyncio.sleep(5)
                continue

            next_at = get_next_at()
            if not next_at or next_at < int(time.time()):
                gap = pick_next_gap()
                next_at = int(time.time()) + gap
                set_next_at(next_at)
                lo, hi = get_window()
                await bot.send_message(
                    owner_id,
                    f"🔁 **অটো-পোস্ট চালু**\n\n"
                    f"প্রথম পোস্ট হবে: **{fmt_ts(next_at)}**\n"
                    f"(প্রায় {count_down(gap)})\n"
                    f"এরপর প্রতি **{lo}-{hi} ঘণ্টা** পর পর অটো পোস্ট।\n"
                    f"🛑 থামাতে `/autostop`",
                )

            # ---- wait for the scheduled time (ALWAYS runs, every pass) ----
            # This must live OUTSIDE the `if` above. If it sat inside, a
            # next_at in the future skipped the wait entirely and the share
            # fired immediately, spamming every few minutes instead of 1-2h.
            while True:
                remain = next_at - int(time.time())
                if remain <= 0 or STATE.stop_flag or not is_enabled():
                    break
                await asyncio.sleep(min(10, remain))
            if STATE.stop_flag or not is_enabled():
                STATE.reset()
                continue

            await run_share(text, owner_id, bot, show_start=True,
                            show_each=False, show_done=True, trigger="auto")

            gap = pick_next_gap()
            next_at = int(time.time()) + gap
            set_next_at(next_at)
            lo, hi = get_window()
            await bot.send_message(
                owner_id,
                f"⏳ **পরবর্তী পোস্টের সময় ঠিক হয়েছে**\n\n"
                f"পরবর্তী পোস্ট: **{fmt_ts(next_at)}**\n"
                f"(প্রায় {count_down(gap)})\n\n"
                f"🛑 থামাতে `/autostop`",
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"⚠️ scheduler error: {e}")
            try:
                await bot.send_message(
                    owner_id, f"⚠️ অটো-পোস্টে সমস্যা: `{e}`\nসিস্টেম আবার চেষ্টা করছে।"
                )
            except Exception:
                pass
            # Reschedule even after a failure, otherwise next_at stays in the
            # past and every loop retries instantly -> retry storm.
            try:
                gap = pick_next_gap()
                set_next_at(int(time.time()) + gap)
            except Exception:
                pass
            await asyncio.sleep(30)


# ---------- Command handlers ----------
@owner_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await update.message.reply_text("⛔ শুধু Owner এই বট ব্যবহার করতে পারবেন।")
        return
    await update.message.reply_text(
        "👋 স্বাগতম Boss!\n\n"
        "কমান্ড:\n"
        "🔑 `/login` — অ্যাকাউন্ট লগইন\n"
        "📋 `/groups` — গ্রুপ লিস্ট\n"
        "📚 `/checkgroups` — জয়েন করা সব গ্রুপ/চ্যানেল দেখুন (প্রাইভেট সহ)\n"
        "➕ `/addgroup <লিংক/আইডি>` — গ্রুপ যোগ\n"
        "➖ `/removegroup <নম্বর>` — গ্রুপ রিমুভ\n"
        "🧹 `/cleargroups` — সব গ্রুপ মুছে ফেলা\n"
        "📤 শুধু লিংক/টেক্সট পাঠান — সব গ্রুপে শেয়ার হবে\n"
        "📜 `/logs` — সাম্প্রতিক শেয়ার রিপোর্ট\n"
        "ℹ️ `/status` — লগইন/গ্রুপ স্ট্যাটাস\n"
        "\n"
        "🤖 **অটো-পোস্ট (নতুন):**\n"
        "📝 `/setMsg` — অটো-পোস্টের মেসেজ সেট করুন\n"
        "▶️ `/autostart` — অটো-পোস্ট চালু করুন\n"
        "🛑 `/autostop` — অটো-পোস্ট বন্ধ করুন\n"
        "📊 `/astatus` — অটো-পোস্ট স্ট্যাটাস\n"
        "\n"
        "📚 `/menu` — বাটন মেনু",
        reply_markup=build_menu_keyboard(),
    )


@owner_only
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logged = engine.is_logged_in
    groups = db.get_groups()
    await update.message.reply_text(
        f"📊 **স্ট্যাটাস**\n\n"
        f"👤 ইউজারবট: {'✅ লগইন করা' if logged else '❌ লগইন করা হয়নি'}\n"
        f"👥 গ্রুপ: {len(groups)} টি\n\n"
        f"লগইন করতে: `/login`"
    )


@owner_only
async def cmd_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    login_states[uid] = {"step": "phone"}
    await update.message.reply_text(
        "🔑 **লগইন শুরু**\n\n"
        "আপনার ফোন নম্বর দিন (আন্তর্জাতিক ফরম্যাটে, যেমন `+8801XXXXXXXXX`):\n\n"
        "বাতিল করতে `/cancel` লিখুন।"
    )


@owner_only
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    login_states.pop(uid, None)
    share_states.pop(uid, None)
    auto_states.pop(uid, None)
    await update.message.reply_text("❌ বাতিল করা হয়েছে।")


@owner_only
async def cmd_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(fmt_groups_list(db.get_groups()))

@owner_only
async def cmd_checkgroups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all groups/channels the userbot account has joined (incl. private ones)."""
    ok, err = await engine.ensure_connected()
    if not ok:
        await update.message.reply_text("⚠️ আগে `/login` দিয়ে অ্যাকাউন্ট লগইন করুন।"
        )
        return
    msg = await update.message.reply_text("🔍 আপনার জয়েন করা গ্রুপ/চ্যানেল খোঁজা হচ্ছে...")
    chats, err = await engine.list_joined_chats()
    if err:
        await msg.edit_text(f"❌ তালিকা আনা যায়নি: {err}")
        return
    if not chats:
        await msg.edit_text("📭 আপনার অ্যাকাউন্টে কোনো গ্রুপ/চ্যানেল নেই।")
        return

    chunks = fmt_checkgroups(chats, saved_group_ids())
    await msg.edit_text(chunks[0], parse_mode="Markdown")
    for ch in chunks[1:]:
        await update.message.reply_text(ch, parse_mode="Markdown")


@owner_only
async def cmd_addgroup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text(
            "❌ গ্রুপ/চ্যানেলের লিংক/আইডি দিন।\n\n"
            "যেমন:\n`/addgroup https://t.me/example`\n`/addgroup @example`\n"
            "`/addgroup https://t.me/+abc123`  (প্রাইভেট ইনভাইট লিংক)\n"
            "`/addgroup -1001234567890`  (নিউমেরিক আইডি)"
        )
        return
    target = args[0].strip()
    if not engine.is_logged_in:
        await update.message.reply_text(
            "⚠️ আগে `/login` দিয়ে অ্যাকাউন্ট লগইন করুন — গ্রুপ যাচাই করতে লাগবে।"
        )
        return
    msg = await update.message.reply_text("🔍 গ্রুপ যাচাই করা হচ্ছে...")
    result = await resolve_group_and_add(target)
    await msg.edit_text(result)


@owner_only
async def cmd_removegroup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    groups = db.get_groups()
    if not groups:
        await update.message.reply_text("📭 কোনো গ্রুপ নেই।")
        return
    if context.args:
        try:
            idx = int(context.args[0]) - 1
        except ValueError:
            await update.message.reply_text("❌ সঠিক নম্বর দিন (যেমন `/removegroup 2`).")
            return
        removed = db.remove_group_by_index(idx)
        if removed is None:
            await update.message.reply_text("❌ ভুল নম্বর।")
            return
        await update.message.reply_text(f"🗑️ গ্রুপ রিমুভ হয়েছে: `{removed}`")
        return
    # No arg: show inline buttons
    keyboard = [
        [InlineKeyboardButton(f"{i}. {g['title'][:30]}", callback_data=f"rm:{i-1}")]
        for i, g in enumerate(groups, 1)
    ]
    keyboard.append([InlineKeyboardButton("❌ বাতিল", callback_data="rm:cancel")])
    await update.message.reply_text(
        "🗑️ কোন গ্রুপ রিমুভ করবেন?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


@owner_only
async def cmd_cleargroups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db.clear_groups()
    await update.message.reply_text("🧹 সব গ্রুপ মুছে ফেলা হয়েছে।")


@owner_only
async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logs = db.recent_logs(10)
    if not logs:
        await update.message.reply_text("📭 এখনো কোনো শেয়ার হয়নি।")
        return
    lines = ["📜 **সাম্প্রতিক শেয়ার:**\n"]
    for l in logs:
        icon = "✅" if l["status"] == "sent" else "❌"
        lines.append(
            f"{icon} {l['group_title']} — {l['status']}"
            + (f" ({l['reason']})" if l["reason"] else "")
        )
    await update.message.reply_text("\n".join(lines))


# ---------- Auto post commands ----------
@owner_only
async def cmd_setmsg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setMsg -> ask owner to send the message to auto-share."""
    uid = update.effective_user.id
    auto_states[uid] = {"step": "await_msg"}
    await update.message.reply_text(
        "📝 **অটো-পোস্টের মেসেজ সেট করুন**\n\n"
        "এখন **মেসেজটি পাঠিয়ে দিন** (লিংক, টেক্সট, যা খুশি)।\n"
        "মেসেজটি সেভ হয়ে যাবে এবং অটো-পোস্ট চালু করলে "
        f"{cfg['delay_min']}-{cfg['delay_max']} সেকেন্ড বিরতিতে ধীরে ধীরে সব গ্রুপে যাবে।\n\n"
        "❌ বাতিল করতে `/cancel`"
    )


@owner_only
async def cmd_autostart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/autostart -> start the looping auto-post system."""
    uid = update.effective_user.id
    text = get_message()
    if not text:
        auto_states[uid] = {"step": "await_msg"}
        await update.message.reply_text(
            "⚠️ আগে মেসেজ সেট করতে হবে।\n\n"
            "এখন **মেসেজটি পাঠিয়ে দিন** — সেট হয়ে গেলে অটো-পোস্ট চালু হবে।\n"
            "❌ বাতিল করতে `/cancel`"
        )
        return
    if not db.get_groups():
        await update.message.reply_text("⚠️ আগে `/addgroup <লিংক>` দিয়ে অন্তত একটি গ্রুপ যোগ করুন।")
        return

    ok, err = await engine.ensure_connected()
    if not ok:
        await update.message.reply_text(f"⚠️ ইউজারবট কানেক্ট হয়নি। আগে `/login` দিন।\n`{err}`")
        return

    STATE.reset()
    set_enabled(True)
    gap = pick_next_gap()
    set_next_at(int(time.time()) + gap)
    lo, hi = get_window()
    await update.message.reply_text(
        "✅ **অটো-পোস্ট চালু হয়েছে!**\n\n"
        f"প্রথম পোস্ট: প্রায় **{count_down(gap)}** পরে\n"
        f"এরপর প্রতি **{lo}-{hi} ঘণ্টা** পর পর অটো পোস্ট হবে।\n"
        f"🛑 থামাতে `/autostop`\n"
        f"ℹ️ দেখতে `/astatus`"
    )


@owner_only
async def cmd_autostop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/autostop -> stop the looping auto-post system."""
    was_running = is_enabled()
    set_enabled(False)
    clear_next_at()
    STATE.request_stop()
    await update.message.reply_text(
        "🛑 **অটো-পোস্ট বন্ধ করা হয়েছে।**\n\n"
        + ("চালু করতে `/autostart`\nমেসেজ বদলাতে `/setMsg`"
           if was_running else
           "ℹ️ অটো-পোস্ট এমনিতেই চালু ছিল না।\nচালু করতে `/autostart`")
    )


@owner_only
async def cmd_astatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/astatus -> show auto-post system status."""
    text = get_message()
    enabled = is_enabled()
    next_at = get_next_at()
    lo, hi = get_window()

    lines = ["🤖 **অটো-পোস্ট স্ট্যাটাস**\n"]
    lines.append(f"অবস্থা: {'🟢 চালু' if enabled else '🔴 বন্ধ'}")
    lines.append(f"গ্রুপ সংখ্যা: {len(db.get_groups())}")
    lines.append(f"ইন্টারভাল: {lo}-{hi} ঘণ্টা (র‍্যান্ডম)")

    if text:
        preview = text if len(text) <= 120 else text[:120] + "…"
        lines.append(f"\n📝 **সেট করা মেসেজ:**\n`{preview}`")
    else:
        lines.append("\n📝 সেট করা মেসেজ: ❌ নেই (`/setMsg` দিন)")

    if enabled and next_at:
        remain = next_at - int(time.time())
        lines.append(f"\n⏭️ পরবর্তী পোস্ট: **{fmt_ts(next_at)}** (প্রায় {count_down(remain)})")
    elif enabled:
        lines.append("\n⏭️ পরবর্তী পোস্ট: হিসাব করা হচ্ছে…")

    if STATE.running:
        lines.append("\n⏳ এখন একটি শেয়ার চলছে…")

    lines.append("\n\n🛑 থামাতে: `/autostop`\n▶️ চালু: `/autostart`\n📝 মেসেজ বদলাতে: `/setMsg`")
    await update.message.reply_text("\n".join(lines))


@owner_only
async def cmd_autoinfo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/autoinfo -> show full help for the auto-post system."""
    await update.message.reply_text(AUTO_HELP + "\n\n💡 মেনুতে বাটনও আছে।")


# ---------- Message handler ----------
@owner_only
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = (update.message.text or "").strip()

    # --- Auto-post message capture (/setMsg flow) ---
    if uid in auto_states and auto_states[uid].get("step") == "await_msg":
        auto_states.pop(uid, None)
        db.set_setting("auto_msg", text)
        preview = text if len(text) <= 300 else text[:300] + "…"
        await update.message.reply_text(
            "✅ **মেসেজ সেভ হয়েছে!**\n\n"
            f"`{preview}`\n\n"
            f"এখন `/autostart` দিলেই অটো-পোস্ট চালু হবে — "
            f"{cfg['delay_min']}-{cfg['delay_max']} সেকেন্ড বিরতিতে ধীরে ধীরে "
            "সব গ্রুপে পাঠানো হবে, আর প্রতিবার শেষ হলে ১-২ ঘণ্টার মধ্যে "
            "র‍্যান্ডম সময় বেছে নিয়ে আবার পোস্ট হবে।\n\n"
            "▶️ `/autostart`\nℹ️ `/autoinfo` — বিস্তারিত"
        )
        return

    # Login flow
    if uid in login_states:
        state = login_states[uid]
        if state["step"] == "phone":
            phone = text
            ok, err = await engine.request_login_code(phone)
            if not ok:
                await update.message.reply_text(f"❌ OTP পাঠানো যায়নি: {err}")
                return
            state["step"] = "code"
            state["phone"] = phone
            await update.message.reply_text(
                "📩 Telegram থেকে আসা **OTP কোড** দিন (5 সংখ্যার):\n\n"
                "2FA পাসওয়ার্ড থাকলে পরে চাওয়া হবে। বাতিল: `/cancel`"
            )
        elif state["step"] == "code":
            code = text.strip()
            ok, err = await engine.complete_login(state["phone"], code)
            if not ok:
                if err == "2FA_PASSWORD_REQUIRED":
                    state["step"] = "password"
                    await update.message.reply_text(
                        "🔐 অ্যাকাউন্টে 2FA পাসওয়ার্ড আছে — সেটা দিন:"
                    )
                    return
                await update.message.reply_text(f"❌ লগইন ব্যর্থ: {err}")
                return
            login_states.pop(uid, None)
            await update.message.reply_text(
                "✅ **লগইন সফল!**\n\n"
                "এখন গ্রুপ যোগ করুন:\n`/addgroup <লিংক>`\n\n"
                "তারপর যেকোনো লিংক/টেক্সট পাঠালেই সব গ্রুপে শেয়ার হবে।"
            )
        elif state["step"] == "password":
            ok, err = await engine.complete_login(state["phone"], None, password=text)
            if not ok:
                await update.message.reply_text(f"❌ পাসওয়ার্ড ভুল: {err}")
                return
            login_states.pop(uid, None)
            await update.message.reply_text(
                "✅ **লগইন সফল!**\n\n"
                "এখন গ্রুপ যোগ করুন:\n`/addgroup <লিংক>`\n\n"
                "তারপর যেকোনো লিংক/টেক্সট পাঠালেই সব গ্রুপে শেয়ার হবে।"
            )
        return

    # Share flow
    groups = db.get_groups()
    if not groups:
        await update.message.reply_text(
            "📭 কোনো গ্রুপ যোগ করা হয়নি।\n`/addgroup <লিংক>` দিয়ে আগে গ্রুপ যোগ করুন।"
        )
        return
    if not engine.is_logged_in:
        await update.message.reply_text(
            "⚠️ ইউজারবট লগইন করা নেই। আগে `/login` দিন।"
        )
        return

    # Confirm before sharing
    share_states[uid] = {"text": text}
    keyboard = [
        [
            InlineKeyboardButton("✅ শেয়ার করুন", callback_data="share:yes"),
            InlineKeyboardButton("❌ বাতিল", callback_data="share:no"),
        ]
    ]
    await update.message.reply_text(
        f"📤 **শেয়ার কনফার্মেশন**\n\n"
        f"এই টেক্সটটি **{len(groups)} টি গ্রুপে** পাঠানো হবে:\n\n"
        f"`{text[:200]}`\n\n"
        f"প্রতি গ্রুপে {cfg['delay_min']}-{cfg['delay_max']} সেকেন্ড বিরতিতে।",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ---------- Callback handler ----------
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    if uid != OWNER_ID:
        await query.answer("⛔ শুধু Owner এই বট ব্যবহার করতে পারবেন।", show_alert=True)
        return
    await query.answer()
    data = query.data

    if data.startswith("menu:"):
        await menu_action(update, context)
        return

    if data.startswith("auto:"):
        sub = data.split(":", 1)[1]
        if sub == "setmsg":
            auto_states[uid] = {"step": "await_msg"}
            await query.edit_message_text(
                "📝 **অটো-পোস্টের মেসেজ সেট করুন**\n\n"
                "এখন **মেসেজটি পাঠিয়ে দিন**।\n"
                f"এরপর {cfg['delay_min']}-{cfg['delay_max']} সেকেন্ড বিরতিতে "
                "ধীরে ধীরে সব গ্রুপে যাবে।\n\n"
                "❌ বাতিল করতে `/cancel`"
            )
            return
        if sub == "info":
            await query.edit_message_text(AUTO_HELP, parse_mode="Markdown")
            return
        if sub == "start":
            if not get_message():
                auto_states[uid] = {"step": "await_msg"}
                await query.edit_message_text(
                    "⚠️ আগে মেসেজ সেট করতে হবে।\n\nএখন **মেসেজটি পাঠিয়ে দিন**।\n"
                    "❌ বাতিল করতে `/cancel`"
                )
                return
            if not db.get_groups():
                await query.edit_message_text("⚠️ আগে `/addgroup <লিংক>` দিয়ে গ্রুপ যোগ করুন।")
                return
            ok, err = await engine.ensure_connected()
            if not ok:
                await query.edit_message_text(f"⚠️ ইউজারবট কানেক্ট হয়নি। আগে `/login` দিন।\n`{err}`")
                return
            STATE.reset()
            set_enabled(True)
            gap = pick_next_gap()
            set_next_at(int(time.time()) + gap)
            lo, hi = get_window()
            await query.edit_message_text(
                "✅ **অটো-পোস্ট চালু হয়েছে!**\n\n"
                f"প্রথম পোস্ট: প্রায় **{count_down(gap)}** পরে\n"
                f"এরপর প্রতি **{lo}-{hi} ঘণ্টা** পর পর অটো পোস্ট হবে।\n"
                "🛑 থামাতে `/autostop`",
                parse_mode="Markdown",
            )
            return
        if sub == "stop":
            set_enabled(False)
            clear_next_at()
            STATE.request_stop()
            await query.edit_message_text(
                "🛑 **অটো-পোস্ট বন্ধ করা হয়েছে।**\nচালু করতে `/autostart`"
            )
            return

    if data.startswith("rm:"):
        val = data.split(":", 1)[1]
        if val == "cancel":
            await query.edit_message_text("❌ বাতিল।")
            return
        idx = int(val)
        removed = db.remove_group_by_index(idx)
        if removed is None:
            await query.edit_message_text("❌ ভুল নম্বর।")
            return
        await query.edit_message_text(f"🗑️ গ্রুপ রিমুভ হয়েছে: `{removed}`")
        return

    if data == "share:no":
        share_states.pop(uid, None)
        await query.edit_message_text("❌ শেয়ার বাতিল করা হয়েছে।")
        return

    if data == "share:yes":
        state = share_states.pop(uid, None)
        if not state:
            await query.edit_message_text("⚠️ শেয়ার সেশন নেই। নতুন করে টেক্সট পাঠান।")
            return
        text = state["text"]
        groups = db.get_groups()
        await query.edit_message_text(
            f"📤 **শেয়ার শুরু হয়েছে...**\n{len(groups)} টি গ্রুপে পাঠানো হচ্ছে।\n\n"
            f"প্রতিটি গ্রুপে {cfg['delay_min']}-{cfg['delay_max']} সেকেন্ড বিরতি।\n"
            f"🛑 থামাতে `/autostop`"
        )
        await run_share(text, uid, context.bot, show_start=False, show_each=True,
                        show_done=True, trigger="manual")
        return


# ---------- Main ----------
async def post_init(app):
    """Set the native Telegram command menu (shown when user types /)."""
    from telegram import BotCommand
    commands = [
        BotCommand("start", "স্বাগতম / মেনু"),
        BotCommand("menu", "বাটন মেনু"),
        BotCommand("login", "অ্যাকাউন্ট লগইন"),
        BotCommand("groups", "গ্রুপ লিস্ট"),
        BotCommand("checkgroups", "জয়েন করা সব গ্রুপ (প্রাইভেট সহ)"),
        BotCommand("addgroup", "গ্রুপ যোগ (লিংক/আইডি)"),
        BotCommand("removegroup", "গ্রুপ রিমুভ"),
        BotCommand("cleargroups", "সব গ্রুপ মুছুন"),
        BotCommand("logs", "শেয়ার রিপোর্ট"),
        BotCommand("status", "স্ট্যাটাস"),
        BotCommand("setmsg", "অটো-পোস্টের মেসেজ সেট করুন"),
        BotCommand("autostart", "অটো-পোস্ট চালু করুন"),
        BotCommand("autostop", "অটো-পোস্ট বন্ধ করুন"),
        BotCommand("astatus", "অটো-পোস্ট স্ট্যাটাস"),
        BotCommand("autoinfo", "অটো-পোস্ট সম্পর্কে তথ্য"),
    ]
    try:
        await app.bot.set_my_commands(commands)
        print("✅ Command menu set.")
    except Exception as e:
        print(f"⚠️ Could not set command menu: {e}")

    # Connect the userbot (MTProto) so /status is accurate and auto-post is ready
    ok, err = await engine.ensure_connected()
    print(f"{'✅' if ok else '⚠️'} Userbot: {err or 'connected'}")

    # Start the auto-post scheduler
    loop = asyncio.get_running_loop()
    STATE.task = loop.create_task(
        scheduler_loop(app.bot, OWNER_ID, app), name="auto-scheduler"
    )
    print("⏰ Auto-post scheduler launched.")


async def post_shutdown(app):
    """Clean shutdown of the scheduler and the MTProto client."""
    if STATE.task:
        STATE.task.cancel()
        try:
            await STATE.task
        except (asyncio.CancelledError, Exception):
            pass
    try:
        await engine.stop()
        print("🛑 Userbot disconnected.")
    except Exception as e:
        print(f"⚠️ shutdown error: {e}")


def main():
    db.init_db()
    app = Application.builder().token(cfg["bot_token"]).build()
    app.post_init = post_init
    app.post_shutdown = post_shutdown

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("login", cmd_login))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("groups", cmd_groups))
    app.add_handler(CommandHandler("checkgroups", cmd_checkgroups))
    app.add_handler(CommandHandler("addgroup", cmd_addgroup))
    app.add_handler(CommandHandler("removegroup", cmd_removegroup))
    app.add_handler(CommandHandler("cleargroups", cmd_cleargroups))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("setmsg", cmd_setmsg))
    app.add_handler(CommandHandler("setMsg", cmd_setmsg))
    app.add_handler(CommandHandler("autostart", cmd_autostart))
    app.add_handler(CommandHandler("autostop", cmd_autostop))
    app.add_handler(CommandHandler("astatus", cmd_astatus))
    app.add_handler(CommandHandler("autoinfo", cmd_autoinfo))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    print("🤖 Bot started. Press Ctrl+C to stop.")
    app.run_polling()

if __name__ == "__main__":
    main()