#!/usr/bin/env python3
"""worker-api-watchdog.py executes written merge conditions of blocked cards.

Live case: a card blocked "until PR #408 is merged", with the one check
command written out (gh pr view 408 --json state == MERGED), but nothing
executed that text. 20 minutes after the merge the card was still blocked and
only the owner woke it.

Native mechanisms do not cover it:
- the dependency wait fires when the parent CARD closes, and the predecessor
  card closes when the rework is delivered, long before the PR merges;
- SCHEDULED_UNTIL wakes at a MOMENT, and the merge moment is unknown: an early
  wake is an empty run, a late one is the same hole.

Fix (an extension of the watchdog layer, not a new daemon): the watchdog
checks the written merge condition and returns the card to ready once it
holds. The condition is a line with a wait marker ("Unblock", "Unblock
condition", "BLOCKED until") + a PR reference + a merge word. Compound
conditions (milestone, owner decision, re-review, awaiting plan) are left
alone: only unambiguous merge triggers wake.

No real board: an isolated home, subprocess.run replaced (hermes CLI and gh),
PR states come from the PR_STATES map.

Run: python3 test_worker_api_watchdog_merged_pr_wait.py
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
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
_HOME = tempfile.mkdtemp(prefix="wd-home-")
atexit.register(shutil.rmtree, _HOME, ignore_errors=True)
os.environ["HERMES_HOME"] = _HOME
os.environ["FACTORY_REPO"] = "owner/repo"
SCRIPT = os.environ.get("WORKER_API_WATCHDOG_SCRIPT",
                        os.path.join(HERE, "worker-api-watchdog.py"))
spec = importlib.util.spec_from_file_location("wd_merge_%d" % int(time.time()), SCRIPT)
assert spec and spec.loader, "cannot load the watchdog module"
wd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wd)

FAILURES = []
CALLS = []     # every subprocess.run command as strings
BOARDS = []    # HERMES_KANBAN_BOARD of every call


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILURES.append(name + (" :: " + str(detail)[:200] if detail else ""))


class FakeCompleted:
    returncode = 0
    stdout = b""
    stderr = b""


PR_STATES = {}   # {"408": "MERGED", "332": "OPEN", ...} for the gh fake


def fake_run(cmd, *a, **kw):
    cmd = [str(x) for x in cmd]
    CALLS.append(cmd)
    BOARDS.append((kw.get("env") or {}).get("HERMES_KANBAN_BOARD"))
    res = FakeCompleted()
    if cmd[:2] == ["gh", "pr"]:
        pr = next((x for x in cmd if x.isdigit()), None)
        state = PR_STATES.get(pr)
        res.stdout = json.dumps({"state": state}).encode() if state else b""
        res.returncode = 0 if state else 1
    return res


def build_board(tasks, events=(), comments=()):
    """tasks: [(id, status, body|None)]; events: [(id, kind, payload)];
    comments: [(id, author, body)]."""
    root = Path(tempfile.mkdtemp(prefix="wd-merge-", dir=_HOME))
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
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, body TEXT, "
        "idempotency_key TEXT, assignee TEXT)")
    con.execute(
        "CREATE TABLE task_comments (id INTEGER PRIMARY KEY, task_id TEXT, "
        "author TEXT, body TEXT, created_at REAL)")
    for tid, status, body in tasks:
        con.execute("INSERT INTO tasks (id, status, body) VALUES (?,?,?)",
                    (tid, status, body))
    for tid, kind, payload in events:
        con.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at)"
            " VALUES (?,?,?,?,?)",
            (tid, 3731, kind,
             payload if isinstance(payload, str) else json.dumps(payload),
             time.time()))
    for tid, author, body in comments:
        con.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at)"
            " VALUES (?,?,?,?)", (tid, author, body, time.time()))
    con.commit()
    con.close()
    return root, bdb


def bind_home(root):
    wd.HOME, wd.STATE_FILE = root, root / "state.json"
    wd.BOARDS_GLOB = str(root / "kanban" / "boards" / "*" / "kanban.db")


def run_wake(tasks, events=(), comments=(), state0=None):
    state0 = dict(state0 or {})
    root, bdb = build_board(tasks, events, comments)
    CALLS.clear()
    BOARDS.clear()
    old = (wd.HOME, wd.STATE_FILE, wd.BOARDS_GLOB)
    bind_home(root)
    real_run = wd.subprocess.run
    wd.subprocess.run = fake_run
    try:
        return wd.wake_merged_pr_waits(state0, [str(bdb)]), state0
    finally:
        wd.subprocess.run = real_run
        wd.HOME, wd.STATE_FILE, wd.BOARDS_GLOB = old


def unblocked(task):
    return any("kanban" in c and "unblock" in c and task in c for c in CALLS)


def gh_pr_calls():
    return [c for c in CALLS if c[:2] == ["gh", "pr"]]


# 1. The live case: condition in a comment, marker + PR + MERGED, PR merged.
PR_STATES.clear(); PR_STATES["408"] = "MERGED"
texts, _ = run_wake(
    [("t_a", "blocked", None)], comments=[(
        "t_a", "coder",
        "Unblock when gh pr view 408 --json state returns MERGED after a PASS"
        " on the reworked head; the rebase mechanics were checked locally.")])
check("1: a met merge condition unblocks the card", unblocked("t_a"), CALLS)
check("1b: the CLI call carries HERMES_KANBAN_BOARD of the card's board",
      BOARDS and all(b == "main" for b in BOARDS if b is not None), BOARDS)
check("1c: the finding names the card and the PR",
      texts and "t_a" in texts[0] and "408" in texts[0], texts)
check("1d: gh is asked about the configured repository",
      any("-R" in c and "owner/repo" in c for c in gh_pr_calls()), gh_pr_calls())

# 2. The PR is still OPEN: silence.
PR_STATES.clear(); PR_STATES["332"] = "OPEN"
texts, _ = run_wake(
    [("t_a", "blocked", None)], comments=[(
        "t_a", "default",
        "BLOCKED: Unblock condition: PR #332 merged into the base branch "
        "(gh pr view 332 --json state == MERGED).")])
check("2: PR OPEN, no unblock", not unblocked("t_a") and not texts, (CALLS, texts))

# 3. A compound condition (milestone): the marker sits in a heading without a
#    PR or merge word, the merge line has no marker.
PR_STATES.clear(); PR_STATES["395"] = "MERGED"
texts, _ = run_wake(
    [("t_a", "blocked",
      "## Unblock condition (for the owner)\n"
      "1. PR #395 is MERGED: gh pr view 395 --json state.\n"
      "2. Issue #396 has a milestone: without it the admission gate returns "
      "the card to scheduled.")])
check("3: a compound condition (merge + milestone) is not unblocked",
      not unblocked("t_a") and not texts, (CALLS, texts))

# 4. A condition with an owner decision / re-review is left alone even when
#    the mentioned PR is already MERGED.
PR_STATES.clear(); PR_STATES["345"] = "MERGED"
texts, _ = run_wake(
    [("t_a", "blocked", None)], comments=[(
        "t_a", "default",
        "Unblock condition: all children closed, a new head pushed and the "
        "owner asked for a re-review in PR #345 before it is merged.")])
check("4: an 'owner asked' trigger is not executed on MERGED",
      not unblocked("t_a") and not texts, (CALLS, texts))

# 5. The condition in the worker's last output (worker_result.last_output),
#    plus noise with gh pr view of ANOTHER PR on a line without a marker.
PR_STATES.clear(); PR_STATES.update({"408": "MERGED", "413": "OPEN"})
texts, _ = run_wake(
    [("t_a", "blocked",
      "How to: take the head branch from gh pr view 413 --json headRefName.")],
    events=[("t_a", "worker_result",
             {"pid": 1, "exit_code": 0,
              "last_output": "Unblock when PR 408 carries a PASS verdict "
              "or is merged: check gh pr view 408 --json state, headRefOid "
              "(at 01:53Z: still OPEN)"})])
check("5: a condition in worker_result.last_output is executed",
      unblocked("t_a"), CALLS)
check("5b: a PR on a line without a marker (413) is not checked",
      any("408" in c for c in gh_pr_calls())
      and not any("413" in c for c in gh_pr_calls()), gh_pr_calls())

# 6. The PR state is cached (two cards, one PR, one gh call).
PR_STATES.clear(); PR_STATES["500"] = "MERGED"
cond = "Unblock condition: PR #500 MERGED (gh pr view 500 --json state)."
texts, _ = run_wake(
    [("t_a", "blocked", None), ("t_b", "blocked", None)],
    comments=[("t_a", "default", cond), ("t_b", "default", cond)])
check("6: both cards with a met condition are unblocked",
      unblocked("t_a") and unblocked("t_b"), CALLS)
check("6b: gh is asked once per PR (state cache)",
      len([c for c in gh_pr_calls() if "500" in c]) == 1, gh_pr_calls())

# 7. A plain needs_input without a merge condition: silence.
PR_STATES.clear()
texts, _ = run_wake(
    [("t_a", "blocked", None)], comments=[(
        "t_a", "coder", "waiting for the owner's decision on a semantic conflict")])
check("7: a block without a merge condition is left alone",
      not unblocked("t_a") and not texts and not gh_pr_calls(), CALLS)

# 8. Scheduled cards are not scanned (wake_scheduled_cards wakes them).
PR_STATES.clear(); PR_STATES["408"] = "MERGED"
texts, _ = run_wake(
    [("t_a", "scheduled", None)], comments=[(
        "t_a", "coder", "Unblock when gh pr view 408 --json state returns MERGED.")])
check("8: a scheduled card is not unblocked by the merge branch",
      not unblocked("t_a"), CALLS)

# 9. Integration: main() runs wake_merged_pr_waits.
PR_STATES.clear(); PR_STATES["408"] = "MERGED"
root, bdb = build_board(
    [("t_a", "blocked", None)],
    comments=[("t_a", "coder",
               "Unblock when gh pr view 408 --json state returns MERGED.")])
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
check("9: main() runs wake_merged_pr_waits (unblock went through)",
      unblocked("t_a"), CALLS)

print()
if FAILURES:
    print(f"{len(FAILURES)} FAIL: {FAILURES}")
    sys.exit(1)
print("OK: merged PR waits, all checks passed")
