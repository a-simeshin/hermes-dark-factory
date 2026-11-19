#!/usr/bin/env python3
"""worker-api-watchdog.py executes SCHEDULED_UNTIL marks.

Live case: an ops worker closed a counting run with `block` and the free text
SCHEDULED_UNTIL=2026-11-17T07:30:00+03:00 (04:30Z, already in the past, the
zone mixed up). Nothing read a mark in a block reason, so the card would have
stayed blocked forever although CI had gone green.

Rule: parking until a moment is one native form, `kanban schedule` with
SCHEDULED_UNTIL=<ISO8601 UTC> in the reason, executed by the watchdog
(wake_scheduled_cards). `block` with the mark is forbidden: a level-3 finding
and a safety return once the mark expires. Times in data live in one zone:
naive means UTC, a zone in the mark is respected.

No real board: an isolated home, subprocess.run replaced.

Run: python3 test_worker_api_watchdog_scheduled_wake.py
"""
from __future__ import annotations

import atexit
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
_HOME = tempfile.mkdtemp(prefix="wd-home-")
atexit.register(shutil.rmtree, _HOME, ignore_errors=True)
os.environ["HERMES_HOME"] = _HOME
SCRIPT = os.environ.get("WORKER_API_WATCHDOG_SCRIPT",
                        os.path.join(HERE, "worker-api-watchdog.py"))
spec = importlib.util.spec_from_file_location("wd_wake_%d" % int(time.time()), SCRIPT)
assert spec and spec.loader, "cannot load the watchdog module"
wd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wd)

FAILURES = []
CALLS = []
BOARDS = []   # HERMES_KANBAN_BOARD of every CLI call


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILURES.append(name + (" :: " + str(detail)[:200] if detail else ""))


class FakeCompleted:
    returncode = 0
    stdout = b""
    stderr = b""


def fake_run(cmd, *a, **kw):
    CALLS.append([str(x) for x in cmd])
    BOARDS.append((kw.get("env") or {}).get("HERMES_KANBAN_BOARD"))
    return FakeCompleted()


def utc(min_from_now):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ",
                         time.gmtime(time.time() + min_from_now * 60))


def build_board(tasks, events):
    """tasks: [(id, status)]; events: [(id, kind, reason)]."""
    root = Path(tempfile.mkdtemp(prefix="wd-wake-", dir=_HOME))
    board = root / "kanban" / "boards" / "main"
    board.mkdir(parents=True)
    bdb = board / "kanban.db"
    con = sqlite3.connect(str(bdb))
    con.execute(
        "CREATE TABLE task_runs (id INTEGER, task_id TEXT, profile TEXT, "
        "worker_pid INTEGER, started_at REAL, last_heartbeat_at REAL, "
        "status TEXT)")
    con.execute(
        "CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, "
        "run_id INTEGER, kind TEXT, payload TEXT, created_at REAL)")
    con.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, "
        "idempotency_key TEXT, assignee TEXT)")
    for tid, status in tasks:
        con.execute("INSERT INTO tasks (id, status) VALUES (?,?)", (tid, status))
    for tid, kind, reason in events:
        con.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at)"
            " VALUES (?,?,?,?,?)",
            (tid, 3547, kind, json.dumps({"reason": reason}), time.time()))
    con.commit()
    con.close()
    return root, bdb


def bind_home(root):
    wd.HOME, wd.STATE_FILE = root, root / "state.json"
    wd.BOARDS_GLOB = str(root / "kanban" / "boards" / "*" / "kanban.db")


def run_wake(tasks, events, state0=None):
    """wake_scheduled_cards on an isolated board, subprocess.run replaced."""
    state0 = dict(state0 or {})
    root, bdb = build_board(tasks, events)
    CALLS.clear()
    BOARDS.clear()
    old = (wd.HOME, wd.STATE_FILE, wd.BOARDS_GLOB)
    bind_home(root)
    real_run = wd.subprocess.run
    wd.subprocess.run = fake_run
    try:
        return wd.wake_scheduled_cards(state0, [str(bdb)]), state0
    finally:
        wd.subprocess.run = real_run
        wd.HOME, wd.STATE_FILE, wd.BOARDS_GLOB = old


def unblocked(tasks_ids=("t_a",)):
    return any("kanban" in c and "unblock" in c and t in c
               for c in CALLS for t in tasks_ids)


texts, _ = run_wake([("t_a", "scheduled")],
                    [("t_a", "scheduled", "SCHEDULED_UNTIL=" + utc(20))])
check("1: schedule + a future mark is silent", not unblocked() and not texts)

texts, _ = run_wake(
    [("t_a", "scheduled")],
    [("t_a", "scheduled",
      "SCHEDULED_UNTIL=" + utc(-1) + " count 4/5, check attempt 7")])
check("2: schedule + an expired mark unblocks the card", unblocked())
check("2b: the finding names the card and the mark",
      texts and "t_a" in texts[0] and "SCHEDULED_UNTIL" in texts[0], texts)
# The watchdog must pass the variable the kanban CLI actually reads,
# HERMES_KANBAN_BOARD; with any other name calls silently hit the current board.
check("2c: CLI calls carry HERMES_KANBAN_BOARD=<board of the card>",
      BOARDS and all(b == "main" for b in BOARDS if b is not None)
      and "hermes" in str(CALLS), str(BOARDS))

texts, _ = run_wake(
    [("t_a", "blocked")],
    [("t_a", "blocked",
      "SCHEDULED_UNTIL=2026-11-17T07:30:00+03:00 count 3/5, "
      "attempt 5 green, attempt 6 started")])
check("3: block + an expired mark: safety unblock", unblocked())
check("3b: the finding names the parking rule violation",
      texts and "violation" in texts[0].lower(), texts)

texts, st = run_wake([("t_a", "blocked")],
                     [("t_a", "blocked", "SCHEDULED_UNTIL=" + utc(30))])
check("4: block + a future mark: finding without unblock",
      texts and not unblocked() and "forbidden" in texts[0], texts)
check("4b: the cooldown key is written", "main:t_a:sched-until-in-block" in st)

texts2, _ = run_wake([("t_a", "blocked")],
                     [("t_a", "blocked", "SCHEDULED_UNTIL=" + utc(30))],
                     state0={"main:t_a:sched-until-in-block": time.time() - 60})
check("5: the cooldown silences a repeated finding", not texts2 and not unblocked())

ts_naive = (datetime.now(timezone.utc) + timedelta(minutes=-5)).strftime(
    "%Y-%m-%dT%H:%M:%S")
texts, _ = run_wake([("t_a", "scheduled")],
                    [("t_a", "scheduled", "SCHEDULED_UNTIL=" + ts_naive)])
check("6: a naive mark is UTC: expired, so unblock", unblocked())

texts, _ = run_wake(
    [("t_a", "scheduled"), ("t_b", "blocked")],
    [("t_a", "scheduled", "waiting on time"),
     ("t_b", "blocked", "needs_input: waiting for the owner's decision")])
check("7: no mark, silence (foreign parkings are left alone)",
      not texts and not unblocked(("t_a", "t_b")))

root, bdb = build_board([("t_a", "scheduled")],
                        [("t_a", "scheduled", "SCHEDULED_UNTIL=" + utc(-2))])
CALLS.clear()
old = (wd.HOME, wd.STATE_FILE, wd.BOARDS_GLOB)
bind_home(root)
real_run = wd.subprocess.run
wd.subprocess.run = fake_run
try:
    wd.main()
finally:
    wd.subprocess.run = real_run
    wd.HOME, wd.STATE_FILE, wd.BOARDS_GLOB = old
check("8: main() runs wake_scheduled_cards (unblock went through)", unblocked())

print()
if FAILURES:
    print(f"{len(FAILURES)} FAIL: {FAILURES}")
    sys.exit(1)
print("OK: scheduled wake, all checks passed")
