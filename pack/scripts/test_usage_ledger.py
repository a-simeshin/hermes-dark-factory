#!/usr/bin/env python3
"""Tests for usage-ledger.py: prices are configuration, never built in.

  1  FACTORY_PRICES unset: a new card row is reported as unpriced, the total
     says "unknown", not zero credits and not a guessed number
  2  FACTORY_PRICES set: the same row is priced by the configured numbers

Each case runs the script in a temporary HERMES_HOME with a synthetic
state.db; PATH holds no `hermes`, so the board calls fail quietly.
Run: python3 test_usage_ledger.py
"""
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "usage-ledger.py")
FAILED = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (" " + detail if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def make_home():
    home = tempfile.mkdtemp(prefix="usage-ledger-home-")
    con = sqlite3.connect(os.path.join(home, "state.db"))
    con.execute("create table sessions (id text, source text, title text, model text,"
                " input_tokens int, output_tokens int, cache_read_tokens int,"
                " reasoning_tokens int, started_at real, ended_at real, profile_name text)")
    con.execute("insert into sessions values ('s1', 'kanban', 'work on t_0000abcd',"
                " 'model-a', 10000, 1000, 0, 0, 1000, 2000, 'coder')")
    con.commit()
    con.close()
    return home


def run(prices):
    home = make_home()
    env = {"HERMES_HOME": home, "PATH": os.path.dirname(sys.executable),
           "FACTORY_OWNER_LOGIN": "owner-login", "FACTORY_OWNER_CHAT": "telegram:0"}
    if prices is not None:
        env["FACTORY_PRICES"] = prices
    try:
        done = subprocess.run([sys.executable, SCRIPT], env=env, capture_output=True,
                              text=True, timeout=120)
    finally:
        shutil.rmtree(home, ignore_errors=True)
    return done.returncode, done.stdout + done.stderr


def main():
    print("1. FACTORY_PRICES unset")
    rc, out = run(None)
    check("exit 0", rc == 0, out[-300:])
    check("unpriced row reported as unknown",
          "new credits ~0 + unknown (1 unpriced rows)" in out, out[-300:])

    print("2. FACTORY_PRICES set")
    rc, out = run("model-a=10/1/100")
    # (10000 * 10 + 1000 * 100) / 1e4 = 20
    check("priced by the configured numbers", "new credits ~20;" in out, out[-300:])
    check("nothing unknown", "unknown" not in out, out[-300:])

    if FAILED:
        print("\nFAILED: %d" % len(FAILED))
        sys.exit(1)
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
