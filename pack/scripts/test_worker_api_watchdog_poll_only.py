#!/usr/bin/env python3
"""Signature C of worker-api-watchdog.py (poll-only), both tiers.

Live case: a run delivered its rework and then only polled for the verdict
with `gh pr view` every ~7 minutes for 4.4 hours, holding a dispatcher slot.
Tier 1: within a 30-minute window >= 3 unique gh polling batches and zero
signs of work (edits, commits, gh mutations, builds, tests).

Calibration after five false findings in one day: the window is cut at the
run start (a session outlives runs), batches must spread over >= 10 minutes
(3 batches in 47 seconds is the initial read of a PR), reviewer and
architect are exempt (reading PRs and CI is their work).

Tier 2, slow cadence: a tester sampled CI reruns every ~20 minutes, tier 1
saw 1-2 batches per window and stayed silent. The window starts at the run's
last work (>= 40 min old), only polls, >= 3 batches spread over >= 30 min.

Run: python3 test_worker_api_watchdog_poll_only.py
"""
import importlib.util
import json
import os
import shutil
import atexit
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
spec = importlib.util.spec_from_file_location("wd_poll_%d" % int(time.time()), SCRIPT)
assert spec and spec.loader, "cannot load the watchdog module"
wd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wd)

FAILURES = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILURES.append(name)


def build_home(profile, msgs):
    """A temp home with profiles/<p>/state.db (messages), agent.log and a board."""
    root = Path(tempfile.mkdtemp(prefix="wd-poll-", dir=_HOME))
    prof = root / "profiles" / profile
    (prof / "logs").mkdir(parents=True)
    con = sqlite3.connect(str(prof / "state.db"))
    con.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, "
        "role TEXT, content TEXT, tool_call_id TEXT, tool_calls TEXT, "
        "tool_name TEXT, effect_disposition TEXT, timestamp REAL, "
        "token_count INTEGER)")
    sess = "local-" + "a" * 64
    for off_min, tool_name, tc in msgs:
        con.execute(
            "INSERT INTO messages (session_id, role, tool_calls, tool_name, "
            "timestamp) VALUES (?,?,?,?,?)",
            (sess, "assistant", json.dumps(tc), tool_name,
             time.time() - off_min * 60))
    con.commit()
    con.close()
    # agent.log: one live API call line, so candidate_sessions finds the session
    now_s = datetime.now().strftime("%Y-%m-%d %H:%M")
    (prof / "logs" / "agent.log").write_text(
        f"{now_s}:00,000 INFO [{sess}] agent.conversation_loop: API call #1: "
        "model=x provider=y in=1 out=1 total=2 latency=1s id=z\n")
    board = root / "kanban" / "boards" / "main"
    board.mkdir(parents=True)
    bdb = board / "kanban.db"
    con = sqlite3.connect(str(bdb))
    con.execute("CREATE TABLE task_runs (id INTEGER, task_id TEXT, profile TEXT, "
                "worker_pid INTEGER, started_at REAL, last_heartbeat_at REAL, "
                "status TEXT)")
    con.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, "
                "run_id INTEGER, kind TEXT, payload TEXT, created_at REAL)")
    con.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                "VALUES ('t_poll', 100, 'owner_admitted', ?, ?)",
                (json.dumps({"session_id": sess}), time.time()))
    con.commit()
    con.close()
    return root, bdb


def poll_tc(cmd):
    return [{"function": {"name": "terminal", "arguments": {"command": cmd}}}]


def work_tc(path="x.txt"):
    return [{"function": {"name": "write_file",
                          "arguments": {"path": path, "content": "y"}}}]


def run_evaluate(profile, msgs, run_age_min):
    root, bdb = build_home(profile, msgs)
    old_home = wd.HOME
    wd.HOME = root
    try:
        run = {"id": 100, "task_id": "t_poll", "profile": profile,
               "started_at": time.time() - run_age_min * 60,
               "last_heartbeat_at": time.time(), "board": "main"}
        return wd.evaluate(run, str(bdb), datetime.now(), force_alive=True)
    finally:
        wd.HOME = old_home


def sig(f):
    return [s for s, _ in f]


POLL = poll_tc("gh api repos/x/actions/runs/43937030239 --jq .status")
B1 = poll_tc("gh pr view 361 --json reviewDecision")
B2 = poll_tc("gh pr checks 361")
B3 = poll_tc("gh pr view 361")

# ---- Tier 1 ---------------------------------------------------------------

f = run_evaluate("coder", [(25, "terminal", B1), (18, "terminal", B1),
                           (11, "terminal", B2), (4, "terminal", B3)],
                 run_age_min=60)
check("1: poll-only gives a poll-only finding", "poll-only" in sig(f))
check("1b: the finding names the card", any("t_poll" in t for _, t in f))

f = run_evaluate("coder", [(25, "terminal", B1), (18, "terminal", B3),
                           (11, "terminal", B3), (9, "patch", poll_tc(""))],
                 run_age_min=60)
check("2: an edit in the window clears signature C", "poll-only" not in sig(f))

f = run_evaluate("coder", [
    (25, "terminal", poll_tc("gh api repos/x/y/pulls/239/comments -f body=v1 --method POST")),
    (18, "terminal", poll_tc("gh api repos/x/y/pulls/239/comments -F body=v2"))],
    run_age_min=60)
check("3: gh api with -f/--method POST is not polling", "poll-only" not in sig(f))

f = run_evaluate("coder", [(25, "terminal", B3), (18, "terminal", B3)],
                 run_age_min=60)
check("4: 2 batches give no finding", "poll-only" not in sig(f))

f = run_evaluate("coder", [(0.3, "terminal", B1), (0.15, "terminal", B2),
                           (0.01, "terminal", POLL)], run_age_min=60)
check("5: 3 batches within one minute are not poll-only", "poll-only" not in sig(f))

f = run_evaluate("coder", [(25, "terminal", B1), (18, "terminal", B2),
                           (11, "terminal", POLL)], run_age_min=0.8)
check("6: a run younger than the window does not inherit foreign polls",
      "poll-only" not in sig(f))

for prof in ("reviewer", "architect"):
    f = run_evaluate(prof, [(29, "terminal", B1), (22, "terminal", B2),
                            (15, "terminal", POLL), (8, "terminal", B1)],
                     run_age_min=45)
    check("7: %s polling with a cadence is not poll-only" % prof,
          "poll-only" not in sig(f))

f = run_evaluate("coder", [(29, "terminal", B1), (22, "terminal", B2),
                           (15, "terminal", POLL), (8, "terminal", B1)],
                 run_age_min=45)
check("8: a 7-minute cadence without work gives a finding", "poll-only" in sig(f))

# ---- Tier 2, slow cadence ---------------------------------------------------

f = run_evaluate("tester", [(105, "write_file", work_tc("rerun-loop.sh")),
                            (35, "terminal", POLL), (25, "terminal", POLL),
                            (15, "terminal", POLL), (5, "terminal", POLL)],
                 run_age_min=110)
check("9: 10-minute cadence after old work gives a finding (tier 2)",
      "poll-only" in sig(f))

f = run_evaluate("tester", [(105, "write_file", work_tc("rerun-loop.sh")),
                            (37, "terminal", POLL), (20, "terminal", POLL),
                            (5, "terminal", POLL)], run_age_min=110)
check("10: 15-20-minute cadence after old work gives a finding (tier 2)",
      "poll-only" in sig(f))

f = run_evaluate("tester", [(105, "write_file", work_tc("rerun-loop.sh")),
                            (85, "terminal", POLL), (65, "terminal", POLL),
                            (30, "write_file", work_tc("rerun-loop2.sh")),
                            (25, "terminal", POLL), (5, "terminal", POLL)],
                 run_age_min=110)
check("11: work 30 minutes ago clears the finding", "poll-only" not in sig(f))

f = run_evaluate("tester", [(33, "terminal", POLL), (18, "terminal", POLL),
                            (3, "terminal", POLL)], run_age_min=35)
check("12: a run younger than POLL_LONG_SPAN_MIN is quiet",
      "poll-only" not in sig(f))

f = run_evaluate("coder", [(75, "write_file", work_tc()),
                           (2.0, "terminal", B1), (1.0, "terminal", B2),
                           (0.1, "terminal", POLL)], run_age_min=110)
check("13: clustered batches after work are not poll-only",
      "poll-only" not in sig(f))

f = run_evaluate("reviewer", [(85, "terminal", POLL), (65, "terminal", POLL),
                              (45, "terminal", POLL), (25, "terminal", POLL),
                              (5, "terminal", POLL)], run_age_min=110)
check("14: reviewer with a 20-minute cadence is not poll-only",
      "poll-only" not in sig(f))

print()
if FAILURES:
    print(f"{len(FAILURES)} FAIL: {FAILURES}")
    sys.exit(1)
print("all poll-only checks passed")
