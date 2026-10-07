#!/usr/bin/env python3
"""Restart bot with unbuffered output."""
import os
import signal
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(BASE, "bot.log")


def find_pids():
    pids = []
    for pid in os.listdir("/proc"):
        if pid.isdigit():
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmd = f.read().decode(errors="ignore")
                if "bot.py" in cmd and "restart" not in cmd:
                    pids.append(int(pid))
            except Exception:
                pass
    return pids


def main():
    for pid in find_pids():
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"stopped {pid}")
        except Exception as e:
            print(f"stop {pid} failed: {e}")
    time.sleep(1.5)

    with open(LOG, "w") as lf:
        proc = subprocess.Popen(
            [sys.executable, "-u", os.path.join(BASE, "bot.py")],
            cwd=BASE,
            stdout=lf,
            stderr=lf,
            start_new_session=True,
        )
    print(f"started PID {proc.pid}")


if __name__ == "__main__":
    main()