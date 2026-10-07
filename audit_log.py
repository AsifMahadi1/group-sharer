import time

import db

conn = db.get_conn()
rows = conn.execute("SELECT * FROM auto_log ORDER BY id DESC LIMIT 15").fetchall()
total = conn.execute("SELECT COUNT(*) c FROM auto_log").fetchone()["c"]
conn.close()

print("total runs:", total)
print()
print(" id  trigger  sent  failed  started_at            finished_at           gap since prev")
prev = None
for r in rows:
    sa, fa = r["started_at"], r["finished_at"]
    try:
        sas = time.strftime("%m-%d %H:%M:%S", time.localtime(int(sa)))
    except Exception:
        sas = str(sa)
    try:
        fas = time.strftime("%m-%d %H:%M:%S", time.localtime(int(fa)))
    except Exception:
        fas = str(fa)
    gap = ""
    if prev and sa:
        g = int(sa) - int(prev)
        flag = "  <-- TOO SOON (under 1h)" if 0 <= g < 3600 else ""
        gap = f"{g}s = {g / 3600:.2f}h{flag}"
    print(f"{r['id']:>3}  {r['trigger']:>7}  {r['sent']:>4}  {r['failed']:>6}  {sas:>19}  {fas:>19}  {gap}")
    if sa:
        prev = sa
