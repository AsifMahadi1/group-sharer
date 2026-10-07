import random
import time

import db

db.set_setting("auto_enabled", "1")
gap = random.randint(3600, 7200)
nxt = int(time.time()) + gap
db.set_setting("auto_next_at", nxt)
print("AUTO ENABLED")
print("  next post:", time.strftime("%d %b %I:%M:%S %p", time.localtime(nxt)))
print("  gap:", gap, "s =", round(gap / 3600, 2), "hours")
