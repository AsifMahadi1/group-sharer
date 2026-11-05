# BRIEF: WebUI for Group-Sharer (admin panel)

Add a Flask-based WebUI to this EXISTING bot project so the owner can manage
everything from a browser instead of typing bot commands.

## HARD CONSTRAINTS
- Python 3.8 syntax ONLY (project runs with `python3.8`; telethon + flask 3.0.3 already installed --user).
- Do NOT break the running bot. Read the SAME SQLite DB (`data.db`) — do not migrate/alter
  existing tables, only read them plus create at most one new table if truly needed.
- Do not commit secrets. Read `bot.py`, `db.py`, `config.json` (do not print or copy tokens)
  to understand the data model before coding.
- Plain HTML/CSS/JS (Flask templates), no build step, no external JS frameworks.
- Port 8017, bind 127.0.0.1 only (it will be exposed via an SSH/tunnel process managed
  separately — do NOT bind 0.0.0.0).

## Pages / features (single-password auth: session cookie; password stored hashed
in settings table key `webui_password_hash` — set on first visit via /setup when unset.
Use Python stdlib `scrypt` like fb-auto-poster does: format `scrypt:32768:8:1$salt$hash`.)

1. **Dashboard**
   - Stats: total groups, today's shares (success/failed from share_log), last auto round info (auto_log latest row), bot config summary.
2. **Groups** (the main pain point to solve)
   - Table of all groups (chat_id, title, added_at) with search box.
   - ADD: single (paste chat_id or @username or t.me link) AND bulk paste (one per line) —
     validate format client+server side. On add, do a live validation attempt with Telethon
     in the background is NOT required — just insert into DB with title = provided title or
     placeholder "(untitled)" and chat_id; keep it fast.
   - REMOVE: per-row delete button + multi-select bulk delete with confirm dialog.
   - CSV/JSON export and import.
3. **Share log**
   - Filterable (status, group, date range) table of share_log, newest first, pagination (50/page).
4. **Auto log**
   - Table of auto_log rounds with totals.
5. **Settings**
   - Show current settings key/values (mask anything that looks like a token: values of keys
     containing 'token', 'key', 'session', 'string' → show `***`).
   - Edit ONLY safe numeric/text settings the bot reads via db.get_setting — inspect db.py
     and config keys to list them (e.g., intervals, message text). Save writes to settings table.
   - "Trigger share round now" button: fires `python3.8 enable_auto.py` --help first to see if a
     manual trigger exists; if bot code exposes a function, prefer importing it in a subprocess
     `python3.8 enable_auto.py once` if that mode exists — if no manual trigger mechanism exists
     in the codebase, do NOT invent one silently: add the button but have it show "not supported
     yet" message and add a TODO comment. Report this in your summary either way.
6. **Style**: clean dark admin theme, Bengali labels ok (Bangla + English mixed fine), responsive,
   table sorting not required.

## Files
- `webui.py` (runnable: `python3.8 webui.py`), `templates_webui/*.html`, `static_webui/style.css`
  (separate folders so the bot files are untouched). `run_webui.sh` starter script.
- Update README.md with a short WebUI section (Bengali ok).

## Verify before reporting done
- `python3.8 -m py_compile webui.py`
- Start it: background, `curl -s localhost:8017` → login/setup page 200; stop it after.
- Confirm data.db tables unchanged (list before/after).
- Print summary: files created, features done, anything skipped.
