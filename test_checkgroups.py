"""Test the /checkgroups ✅/➕ marking logic (no Telegram calls)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bot

FAILS = []


def check(name, cond, extra=""):
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


CHATS = [
    {"chat_id": -100111, "title": "Added Group One", "type": "supergroup", "username": "@one"},
    {"chat_id": -100222, "title": "Not Added", "type": "group", "username": "—"},
    {"chat_id": -100333, "title": "Added Channel", "type": "channel 🔒", "username": "@three"},
    {"chat_id": -100444, "title": "Another Not Added", "type": "supergroup", "username": "—"},
]
SAVED = {-100111, -100333}

import re

_ROW = re.compile(r"^[✅➕]\s+\d+\.\s")


def group_lines(out):
    """Only the per-group rows (skip header/legend lines)."""
    return [ln for ln in out.split("\n") if _ROW.match(ln)]


print("\n=== 1. Marking logic ===")
out = "\n".join(bot.fmt_checkgroups(CHATS, SAVED))
check("added group 1 has ✅", "✅ 1. Added Group One" in out, )
check("not-added group 2 has ➕", "➕ 2. Not Added" in out)
check("added channel 3 has ✅", "✅ 3. Added Channel" in out)
check("not-added group 4 has ➕", "➕ 4. Another Not Added" in out)
check("no ✅ on group 2", "✅ 2." not in out)
check("no ➕ on group 1", "➕ 1." not in out)
check("exactly 2 marked ✅", sum(1 for l in group_lines(out) if l[0] == "✅") == 2)
check("exactly 2 marked ➕", sum(1 for l in group_lines(out) if l[0] == "➕") == 2)

print("\n=== 2. Summary counts ===")
check("total shown", "৪টি" in out or "4টি" in out, "")
check("added count = 2", "**2**" in out)
check("remaining count = 2", "**2**" in out)
check("legend for ✅", "✅ = ইতোমধ্যে" in out)
check("legend for ➕", "➕ = এখনো যোগ করা হয়নি" in out)

print("\n=== 3. Chat ids and meta still shown ===")
check("chat_id 1 present", "-100111" in out)
check("chat_id 4 present", "-100444" in out)
check("type shown", "supergroup" in out)
check("username shown", "@one" in out)
check("dash username kept", "—" in out)

print("\n=== 4. Nothing saved yet ===")
out0 = "\n".join(bot.fmt_checkgroups(CHATS, set()))
gl = group_lines(out0)
check("all 4 rows ➕", len(gl) == 4 and all(l[0] == "➕" for l in gl), f"rows={gl}")
check("no ✅ in rows", not any(l[0] == "✅" for l in gl))
check("added count = 0", "**0**" in out0)

print("\n=== 5. Everything saved ===")
allids = {c["chat_id"] for c in CHATS}
outA = "\n".join(bot.fmt_checkgroups(CHATS, allids))
glA = group_lines(outA)
check("all 4 rows ✅", len(glA) == 4 and all(l[0] == "✅" for l in glA), f"rows={glA}")
check("remaining = 0", "**0**" in outA)

print("\n=== 6. Long titles truncated ===")
long_chats = [{"chat_id": -1, "title": "X" * 120, "type": "group", "username": "—"}]
outL = bot.fmt_checkgroups(long_chats, set())[0]
check("title cut to 45", "X" * 45 in outL and "X" * 46 not in outL)

print("\n=== 7. Chunking for many groups ===")
many = [{"chat_id": -i, "title": f"Group {i} " + "y" * 60,
         "type": "supergroup", "username": f"@g{i}"} for i in range(1, 121)]
chunks = bot.fmt_checkgroups(many, set(range(-1, -61, -1)))
check("split into multiple messages", len(chunks) > 1, f"chunks={len(chunks)}")
check("all chunks under 4096", all(len(c) < 4096 for c in chunks),
      f"max={max(len(c) for c in chunks)}")
joined = "".join(chunks)
glAll = group_lines(joined)
check("no group lost", len(glAll) == 120, f"rows={len(glAll)}")
check("first chunk has legend", "✅ = ইতোমধ্যে" in chunks[0])
check("60 marked as added", sum(1 for l in glAll if l[0] == "✅") == 60,
      f"count={sum(1 for l in glAll if l[0] == '✅')}")

print("\n=== 8. saved_group_ids() reads db ===")
import db
db.init_db()
ids = bot.saved_group_ids()
real = {bot.normalize_id(g["chat_id"]) for g in db.get_groups()}
check("matches db", ids == real, f"{len(ids)} ids")

print("\n=== 9. normalize_id (the real-world mismatch) ===")
# DB stores raw entity.id; dialogs return utils.get_peer_id
check("supergroup raw == peer", bot.normalize_id(4484263789) == bot.normalize_id(-1004484263789),
      f"{bot.normalize_id(4484263789)} vs {bot.normalize_id(-1004484263789)}")
check("basic group raw == peer", bot.normalize_id(1234567) == bot.normalize_id(-1234567))
check("positive stays positive", bot.normalize_id(4484263789) == 4484263789)
check("str input works", bot.normalize_id("4484263789") == 4484263789)
check("bad input passthrough", bot.normalize_id("abc") == "abc")

print("\n=== 10. Mixed formats marked correctly ===")
mixed = [
    {"chat_id": -1004484263789, "title": "Supergroup Saved", "type": "supergroup", "username": "—"},
    {"chat_id": 3991719155, "title": "Saved Raw Positive", "type": "supergroup", "username": "—"},
    {"chat_id": -1009999999999, "title": "Not Saved", "type": "channel", "username": "—"},
]
# saved in the DB-style raw form
outM = "\n".join(bot.fmt_checkgroups(mixed, {4484263789, 3991719155}))
check("peer-id supergroup marked ✅", "✅ 1. Supergroup Saved" in outM)
check("raw positive marked ✅", "✅ 2. Saved Raw Positive" in outM)
check("unsaved marked ➕", "➕ 3. Not Saved" in outM)
check("summary says 2 added", "**2**" in outM)
glM = group_lines(outM)
check("rows: 2 ✅ 1 ➕", sum(1 for l in glM if l[0] == "\u2705") == 2
      and sum(1 for l in glM if l[0] == "\u2795") == 1, f"rows={glM}")

print("\n=== 11. Real DB data round-trip ===")
real_chats = [
    {"chat_id": g["chat_id"], "title": g["title"], "type": "supergroup", "username": "—"}
    for g in db.get_groups()
]
if real_chats:
    outR = "\n".join(bot.fmt_checkgroups(real_chats, bot.saved_group_ids()))
    glR = group_lines(outR)
    check("every real saved group shows ✅", all(l[0] == "\u2705" for l in glR),
          f"{len(glR)} rows")
    check("added count == group count",
          f"**{len(real_chats)}**" in outR, f"expected {len(real_chats)}")

print("\n" + "=" * 46)
if FAILS:
    print(f"❌ {len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("✅ ALL CHECKGROUPS TESTS PASSED")
