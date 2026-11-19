#!/usr/bin/env python3
"""Signature C of worker-api-watchdog.py acts instead of only reporting.

Live case: an ops run waited for CI inside one run (attempt 3, then 4 with
--failed, then a full rerun 5, ~25 minutes each). The C finding went out as a
comment and the run went on for 20+ more minutes. Fix: a repeated C finding
on the same state key POLL_ESCALATE_AFTER_MIN minutes later with nothing
changed is an action: `kanban schedule` closes the run with a SCHEDULED_UNTIL
mark (UTC), the worker client gets SIGTERM, and wake_scheduled_cards returns
the card once the mark expires.

1. First C finding (no state key): a comment only, no schedule.
2. Repeated C finding 28 minutes later: schedule with a +35 min mark, the
   poll-only key refreshed, no separate wake key.
3. Repeated finding 10 minutes later: still the cooldown, no action.
4. An expired mark on a scheduled card: unblock and a return comment.
5. An expired mark, but the card was moved by hand (ready): no unblock.
6. A mark not yet expired: silence.
7. The signature itself still fires (evaluate without state).

Run: python3 test_worker_api_watchdog_poll_escalation.py
"""
import atexit
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
_HOME = tempfile.mkdtemp(prefix="wd-home-")
atexit.register(shutil.rmtree, _HOME, ignore_errors=True)
os.environ["HERMES_HOME"] = _HOME
SCRIPT = os.environ.get("WORKER_API_WATCHDOG_SCRIPT",
                        os.path.join(HERE, "worker-api-watchdog.py"))
spec = importlib.util.spec_from_file_location("wd_esc_%d" % int(time.time()), SCRIPT)
assert spec and spec.loader, "cannot load the watchdog module"
wd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wd)

FAILURES = []
CALLS = []
KEY = "main:t_esc:poll-only"


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (" | " + extra if extra else ""))
    if not cond:
        FAILURES.append(name)


class FakeCompleted:
    returncode = 0
    stdout = b""
    stderr = b""


def fake_run(cmd, *a, **kw):
    CALLS.append(list(cmd))
    return FakeCompleted()


def build_home(profile, msgs, tasks=None, events=None):
    root = Path(tempfile.mkdtemp(prefix="wd-esc-", dir=_HOME))
    prof = root / "profiles" / profile
    (prof / "logs").mkdir(parents=True)
    con = sqlite3.connect(str(prof / "state.db"))
    con.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, "
        "role TEXT, content TEXT, tool_call_id TEXT, tool_calls TEXT, "
        "tool_name TEXT, effect_disposition TEXT, timestamp REAL, "
        "token_count INTEGER)")
    sess = "local-" + "b" * 64
    for off_min, tool_name, tc in msgs:
        con.execute(
            "INSERT INTO messages (session_id, role, tool_calls, tool_name, "
            "timestamp) VALUES (?,?,?,?,?)",
            (sess, "assistant", json.dumps(tc), tool_name,
             time.time() - off_min * 60))
    con.commit()
    con.close()
    now_s = datetime.now().strftime("%Y-%m-%d %H:%M")
    (prof / "logs" / "agent.log").write_text(
        f"{now_s}:00,000 INFO [{sess}] agent.conversation_loop: API call #1: "
        "model=x provider=y in=1 out=1 total=2 latency=1s id=z\n")
    board = root / "kanban" / "boards" / "main"
    board.mkdir(parents=True)
    bdb = board / "kanban.db"
    con = sqlite3.connect(str(bdb))
    con.execute(
        "CREATE TABLE task_runs (id INTEGER, task_id TEXT, profile TEXT, "
        "worker_pid INTEGER, started_at REAL, last_heartbeat_at REAL, "
        "status TEXT)")
    con.execute(
        "INSERT INTO task_runs VALUES (3545, 't_esc', ?, NULL, ?, ?, 'running')",
        (profile, time.time() - 70 * 60, time.time()))
    con.execute(
        "CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, "
        "run_id INTEGER, kind TEXT, payload TEXT, created_at REAL)")
    con.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, "
        "idempotency_key TEXT, assignee TEXT)")
    for tid, status in (tasks or [("t_esc", "running")]):
        con.execute("INSERT INTO tasks (id, status) VALUES (?,?)", (tid, status))
    for tid, kind, reason in (events or []):
        con.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?,?,?,?,?)",
            (tid, 3545, kind, json.dumps({"reason": reason}), time.time()))
    con.commit()
    con.close()
    return root, bdb


def with_home(root, fn):
    old = (wd.HOME, wd.STATE_FILE, wd.BOARDS_GLOB)
    wd.HOME, wd.STATE_FILE = root, root / "state.json"
    # BOARDS_GLOB is computed at import: without rebinding main() would read
    # the import-time home.
    wd.BOARDS_GLOB = str(root / "kanban" / "boards" / "*" / "kanban.db")
    try:
        return fn()
    finally:
        wd.HOME, wd.STATE_FILE, wd.BOARDS_GLOB = old


def run_main(profile, msgs, state0, tasks=None, events=None):
    """main() on an isolated home with subprocess.run replaced."""
    root, _ = build_home(profile, msgs, tasks, events)
    CALLS.clear()

    def body():
        wd.save_state(state0)
        real_run = wd.subprocess.run
        wd.subprocess.run = fake_run
        try:
            wd.main()
        finally:
            wd.subprocess.run = real_run
        return wd.load_state()

    return with_home(root, body)


def poll_tc(cmd):
    return [{"function": {"name": "terminal", "arguments": {"command": cmd}}}]


def utc(min_from_now):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ",
                         time.gmtime(time.time() + min_from_now * 60))


# The live shape: work long ago, then only gh polls at a slow cadence.
MSGS = [
    (28, "terminal", poll_tc("gh pr checks 384")),
    (15, "terminal", poll_tc("gh api repos/x/actions/runs/44030020587 --jq .status")),
    (4, "terminal", poll_tc("gh run view 44030020587 --json status,attempt")),
]


def hermes_calls():
    return [c for c in CALLS if c[:1] == ["hermes"]]


def has_verb(calls, verb):
    return any("kanban" in c and verb in c for c in calls)


st = run_main("ops", MSGS, {})
calls = hermes_calls()
check("1: the first C finding comments, no schedule",
      has_verb(calls, "comment") and not has_verb(calls, "schedule")
      and not has_verb(calls, "unblock"), str([c[2:4] for c in calls]))
check("1b: the poll-only key is written", KEY in st)

st = run_main("ops", MSGS, {KEY: time.time() - 28 * 60})
calls = hermes_calls()
check("2: a repeated C closes the run with schedule",
      has_verb(calls, "schedule"), str([c[2:4] for c in calls]))
reason = next((c[4] for c in calls
               if c[:4] == ["hermes", "kanban", "schedule", "t_esc"]), "")
until = wd.parse_scheduled_until(reason)
check("2b: SCHEDULED_UNTIL mark in the reason at +35 min (UTC)",
      until is not None and time.time() + 33 * 60 < until < time.time() + 37 * 60,
      reason[:80])
check("2c: the poll-only key is refreshed",
      st.get(KEY, 0) > time.time() - 120)
check("2d: no separate wake key (one mechanism)", "main:t_esc:poll-wake" not in st)

run_main("ops", MSGS, {KEY: time.time() - 10 * 60})
calls = hermes_calls()
check("3: 10 minutes is still the cooldown, no schedule",
      not has_verb(calls, "schedule") and not has_verb(calls, "unblock"))

run_main("ops", MSGS, {}, tasks=[("t_esc", "scheduled")],
         events=[("t_esc", "scheduled",
                  "SCHEDULED_UNTIL=" + utc(-1) + " counting wake")])
calls = hermes_calls()
check("4: an expired SCHEDULED_UNTIL mark unblocks the card",
      has_verb(calls, "unblock"), str([c[2:4] for c in calls]))
check("4b: a return comment naming the mark goes to the card",
      any("kanban" in c and "comment" in c
          and "SCHEDULED_UNTIL" in " ".join(map(str, c)) for c in calls))

run_main("ops", MSGS, {}, tasks=[("t_esc", "ready")],
         events=[("t_esc", "scheduled", "SCHEDULED_UNTIL=" + utc(-1))])
check("5: a card that is not scheduled/blocked is not unblocked",
      not has_verb(hermes_calls(), "unblock"))

run_main("ops", MSGS, {}, tasks=[("t_esc", "scheduled")],
         events=[("t_esc", "scheduled", "SCHEDULED_UNTIL=" + utc(20))])
check("6: a mark not yet expired is silent", not has_verb(hermes_calls(), "unblock"))

root, bdb = build_home("ops", MSGS)
findings = with_home(root, lambda: wd.evaluate(
    {"id": 3545, "task_id": "t_esc", "profile": "ops",
     "started_at": time.time() - 70 * 60, "last_heartbeat_at": time.time(),
     "board": "main", "worker_pid": None},
    str(bdb), datetime.now(), force_alive=True))
check("7: signature C itself still fires", "poll-only" in [s for s, _ in findings])

print()
if FAILURES:
    print(f"{len(FAILURES)} FAIL: {FAILURES}")
    sys.exit(1)
print("OK: poll escalation, all checks passed")
