# BRIEF: WebUI "Live Groups" page — check-bot-membership driven group manager

Goal: replicate the bot's `/checkgroups` experience in the WebUI. The owner wants to SEE
which Telegram groups/channels he is actually a member of, and then with checkboxes ADD
or REMOVE those groups from the sharing list — all in one place — then press SAVE once.

## CRITICAL ARCHITECTURE CONSTRAINT (do not violate)
The Telethon userbot session (`userbot.session`, SQLiteSession) is owned by the RUNNING
bot process (`bot.py`). A second Telethon client on the same session file from webui.py
will deadlock/corrupt. Therefore:
- The LIVE SCAN must be executed INSIDE the running bot process.
- webui.py only requests a scan and reads results via the SQLite DB (bridge pattern).

## Implementation plan

### 1. DB bridge (add to `db.py` — additive only, existing tables untouched)
- New table `webui_scan_requests`:
  `(id INTEGER PK AUTOINCREMENT, cmd TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
    result TEXT, created_at INTEGER, finished_at INTEGER)`
  ('result' holds a JSON summary).
- New table `live_groups` (cache of last scan, wiped and refilled each scan):
  `(chat_id INTEGER PRIMARY KEY, title TEXT, type TEXT, members TEXT,
    scanned_at INTEGER)`  — type: 'group'/'supergroup'/'channel'.

### 2. Bot side (`bot.py` or a small new module imported by it — additive)
- In the userbot task (where the client is already running) add a lightweight poller loop:
  every 5s check `webui_scan_requests` for a row with cmd='scan_groups', status='pending'.
- When found: set status='running'; iterate the userbot's dialogs
  (`await client.iter_dialogs()`), keep dialogs where the user is a participant
  (exclude users: only megagroup/channel/small group), upsert into `live_groups`,
  then set the request row: status='done', result=JSON like
  `{"found_groups": N, "in_db": M, "at": ts}`.
- On error: status='error', result = JSON {"error": "..."} — never crash the bot loop;
  wrap everything in try/except. Must survive flood waits (max_flood_wait config exists).
- IMPORTANT: keep the poller cheap (one SELECT every 5s). Do NOT touch any other bot logic.

### 3. WebUI side (`webui.py` + templates)
- New nav item **"Live Groups"** (বাংলা লেবেল: লাইভ গ্রুপ).
- Page flow:
  a. **"🔍 স্ক্যান করুন"** button → inserts a scan request row (if one is already
     pending/running, show "already running"; if request rows older than 5 min are stuck in
     'running', offer to reset them to 'error').
  b. Poll (fetch every 2s via JS) until status becomes done/error; show spinner text.
  c. When done: render a checklist of ALL entries in `live_groups` (title, chat_id, type,
     scanned time). Each row has a checkbox **"শেয়ার লিস্টে আছে"** — checked iff chat_id is
     currently in the `groups` table. Groups in DB but NOT found in the scan are also listed
     at the bottom in a separate "⚠️ বট মেম্বার নেই" section (unchecked, chat_id shown, so
     the user can uncheck/remove them).
  d. All changes are STAGED CLIENT-SIDE (track dirty state; do not apply anything until
     the user presses **"💾 SAVE"**).
  e. SAVE (POST /live-groups/save): for each chat_id — selected but not in DB → INSERT
     (INSERT OR IGNORE with title); deselected but in DB → DELETE. Show a success summary
     "X যোগ হয়েছে, Y বাদ দেওয়া হয়েছে" + the updated list stays on screen.
  f. Add a note where relevant (Bengali): "শেয়ার হবার জন্য বট/ইউজারবট গ্রুপের মেম্বার হতে হবে।"
- Login required like other pages. Dark theme consistent with existing static_webui/style.css.

### 4. Verify yourself (print results for each)
- python3.8 -m py_compile on every changed/added .py.
- On a COPY of data.db in a temp dir: unit-test the bridge functions (request insert,
  poller read, save logic) — you cannot test the real Telethon scan (bot owns the session),
  so mock `live_groups` rows to test render/save.
- Boot webui.py briefly on 127.0.0.1:8017 against the REAL data.db but do NOT insert any
  scan request; just confirm the page renders 200 and the checklist width empty state
  ("আগে স্ক্যান করুন") shows. Kill it after.
- Confirm existing tables' schemas untouched; only new tables added.
- Print a summary: files changed, how the bot poller was wired in (which function it
  watches), test results, and how the owner restarts the bot to activate the poller.
