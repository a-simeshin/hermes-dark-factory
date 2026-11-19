#!/usr/bin/env python3
"""A review arm in ready is retired while the stock review lane is alive.

Case: the watchdog created a p100 review arm for a PR head without a verdict
while the stock review lane was not raised yet; five seconds later the
executor requested review and the stock lane claimed the card (claim
source_status=review). The arm stayed in ready waiting for a slot to run a
SECOND review of the same head.

Rule: review_arm_liveness_gate (the --rework-gate-only cron, every five
minutes) retires a live review arm (ready/todo, key reviewer-prN-<head7>
with suffixes) when the stock review lane already runs on the same PR and
head (_review_cycle_live), when a verdict sits on the exact head, or when the
arm's head is no longer the PR head. A gh failure is conservative: nothing
is retired. running arms are not touched (status filter). The same class as
retiring stale rework arms.

Checks:
  A1 arm in ready + stock lane running on the same head: retired;
  A2 verdict on the exact head (no lane): retired;
  A3 arm on a head the PR has left: retired;
  A3c arm on the live head without lane or verdict: alive;
  A3b a lane of the same PR on ANOTHER head does not cover the arm;
  A4 gh failure and no lane: alive;
  A5 running arm untouched;
  A6 a reviewer card outside the arm key class untouched;
  A7-A9 marker comments and findings; A10 idempotency; A11-A12 dry-run;
  MUT1 a mutant that does not recognise the arm class turns the battery red.

Uses the board lint harness (test_board_watchdog_board_lint.py).

Run: python3 test_board_watchdog_review_arm_liveness.py
"""
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.join(HERE, "test_board_watchdog_board_lint.py")
spec = importlib.util.spec_from_file_location("wd_lint_harness_ral", HARNESS)
if spec is None or spec.loader is None:
    print("SKIP: cannot load harness")
    sys.exit(0)
h = importlib.util.module_from_spec(spec)
sys.modules["wd_lint_harness_ral"] = h
spec.loader.exec_module(h)

OPEN_MILESTONES = json.dumps([
    {"title": "2.4.0", "due_on": "2026-12-01T00:00:00Z"},
])

BW_PATH = os.environ.get("BOARD_WATCHDOG_SCRIPT") or os.path.join(
    HERE, "board-watchdog.py")

HEAD_399 = "b58b544a0d3111a62a22ac80ea8785cb1b0c04f4"

# Live PR states (_pr_review_state). 399: no verdict on b58b544; 402: PASS
# on the same head; 403: gh failure; 405 and 407: head without lane and
# verdict.
_LIVE = {
    399: {"head": HEAD_399, "pr_state": "OPEN", "verdict": False,
          "head_kind": None},
    402: {"head": "eee1111" + "0" * 33, "pr_state": "OPEN",
          "verdict": True, "head_kind": "accept"},
    403: None,
    405: {"head": "aaa1111" + "0" * 33, "pr_state": "OPEN",
          "verdict": False, "head_kind": None},
    407: {"head": "cafe123" + "0" * 34, "pr_state": "OPEN",
          "verdict": False, "head_kind": None},
}

TASKS = [
    # A1: arm in ready, the executor runs with claim source_status=review on
    # the same head
    {"id": "t_a1", "status": "ready", "assignee": "reviewer",
     "priority": 100,
     "title": "[reviewer] PR #399 (head b58b544): review of a head without "
              "a verdict (watchdog auto-arm)",
     "key": "reviewer-pr399-b58b544", "age": 1800},
    # A2: verdict on the exact head (no stock lane)
    {"id": "t_a2", "status": "ready", "assignee": "reviewer",
     "priority": 100,
     "title": "[reviewer] PR #402 (head eee1111): head review",
     "key": "reviewer-pr402-eee1111", "age": 1800},
    # A3: arm on a head the PR has LEFT (live b58b544, arm cafe1234)
    {"id": "t_a3", "status": "ready", "assignee": "reviewer",
     "priority": 100,
     "title": "[reviewer] PR #399 (head cafe1234): head review",
     "key": "reviewer-pr399-cafe1234", "age": 1800},
    # A3c control: arm on the live head of PR 407, no lane, no verdict:
    # alive (that is the arm's job)
    {"id": "t_a3c", "status": "ready", "assignee": "reviewer",
     "priority": 100,
     "title": "[reviewer] PR #407 (head cafe123): head review",
     "key": "reviewer-pr407-cafe1234", "age": 1800},
    # A3b: a lane of the same PR on ANOTHER head does not cover the arm
    {"id": "t_a3b", "status": "ready", "assignee": "reviewer",
     "priority": 100,
     "title": "[reviewer] PR #405 (head aaa1111): head review",
     "key": "reviewer-pr405-aaa1111", "age": 1800},
    # A4: gh failure and no lane: conservatively alive
    {"id": "t_a4", "status": "ready", "assignee": "reviewer",
     "priority": 100,
     "title": "[reviewer] PR #403 (head fff2222): head review",
     "key": "reviewer-pr403-fff2222", "age": 1800},
    # A5: running arm: untouched (the run is paid for)
    {"id": "t_a5", "status": "running", "assignee": "reviewer",
     "priority": 100,
     "title": "[reviewer] PR #399 (head b58b544): head review",
     "key": "reviewer-pr399-b58b544-r1", "age": 600},
    # A6: a reviewer card outside the arm class (key is not reviewer-prN-<hex>)
    {"id": "t_a6", "status": "ready", "assignee": "reviewer",
     "priority": 80,
     "title": "[reviewer] review of the verdict fix of PR #404",
     "key": "reviewer-pr404-rework-verdict-987654", "age": 1800},
]

# Executors: t_exec1 is the case itself (running, claim review, head named by
# the review_requested event); t_exec3 runs on an old head.
EXTRA_EVENTS = [
    ("t_exec1", "review_requested",
     "PR #399 (head b58b544a, base develop/2.x): all findings closed."),
    ("t_exec1", "claimed",
     json.dumps({"lock": "x:1", "run_id": 3613,
                 "source_status": "review"})),
    ("t_exec3", "review_requested",
     "PR #405 (head bbb2222, base develop/2.x): round 1."),
    ("t_exec3", "claimed",
     json.dumps({"lock": "x:2", "run_id": 3599,
                 "source_status": "review"})),
]
EXTRA_TASKS = [
    {"id": "t_exec1", "status": "running", "assignee": "coder",
     "priority": 80,
     "title": "[coder] issue #79: version badge + mismatch notice",
     "key": None, "age": 7200},
    {"id": "t_exec3", "status": "running", "assignee": "coder",
     "priority": 80,
     "title": "[coder] issue #78: previous round", "key": None,
     "age": 9000},
]

_SIBLINGS = ("milestone_bands.py", "board_native_guard.py",
             "factory_config.py", "metrics-evening.py")


def load_bw(src=None, tag="ral"):
    """board-watchdog module from the real file or from a mutated text."""
    sbx = tempfile.mkdtemp(prefix="bw-sbx-ral-")
    path = BW_PATH if src is None else os.path.join(sbx, "wd-mut.py")
    if src is not None:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(src)
        for nb in _SIBLINGS:
            if os.path.exists(os.path.join(HERE, nb)):
                shutil.copy(os.path.join(HERE, nb), os.path.join(sbx, nb))
    sp = importlib.util.spec_from_file_location("bw_mod_%s" % tag, path)
    m = importlib.util.module_from_spec(sp)
    sys.modules["bw_mod_%s" % tag] = m
    sp.loader.exec_module(m)
    m.STATE = os.path.join(sbx, "state.json")
    m.BACKLOG_CACHE = os.path.join(sbx, "backlog.json")
    if hasattr(m, "milestone_bands"):
        m.milestone_bands.CACHE_PATH = os.path.join(sbx, "ms.json")
        m.milestone_bands._MEMO = {}
    return m, sbx


def make_run(db, calls):
    """run stub: gh api from the fixture, kanban commands applied to the
    test database so the gate's re-read verification sees them."""

    def _run(cmd, timeout=120):
        calls.append(cmd)
        if cmd[:2] == ["gh", "api"]:
            return OPEN_MILESTONES
        if len(cmd) > 5 and cmd[1] == "kanban":
            action, tid = cmd[4], cmd[5]
            con = sqlite3.connect(db, timeout=10)
            try:
                if action == "comment":
                    con.execute(
                        "insert into task_comments (task_id, body,"
                        " created_at) values (?,?,?)",
                        (tid, cmd[6], int(time.time())))
                elif action == "archive":
                    con.execute(
                        "update tasks set status='archived' where id=?",
                        (tid,))
                con.commit()
            finally:
                con.close()
        return "ok"

    return _run


def _seed_events(db, events):
    con = sqlite3.connect(db)
    for tid, kind, payload in events:
        con.execute(
            "insert into task_events (task_id, kind, payload, created_at)"
            " values (?,?,?,?)", (tid, kind, payload, int(time.time())))
    con.commit()
    con.close()


def _gate(mod, slug, db, calls, **kw):
    real = mod.run, mod.board_repo, mod._pr_review_state
    mod.run = make_run(db, calls)
    mod.board_repo = lambda s: "acme/widget"
    mod._pr_review_state = lambda repo, number: _LIVE.get(number)
    try:
        return mod.review_arm_liveness_gate(slug, **kw)
    finally:
        mod.run, mod.board_repo, mod._pr_review_state = real


def battery(mod):
    problems = []

    def want(name, cond, detail=""):
        print("%s %s%s" % ("ok  " if cond else "FAIL", name,
                           (" :: %s" % (detail,)) if detail and not cond
                           else ""))
        if not cond:
            problems.append(name)

    slug, db, _ = h.make_board(tasks=TASKS + EXTRA_TASKS)
    _seed_events(db, EXTRA_EVENTS)
    mod.BOARDS = os.path.dirname(os.path.dirname(db))

    calls = []
    try:
        found = _gate(mod, slug, db, calls)
    except Exception as exc:
        problems.append("A0: review_arm_liveness_gate crashed: %s: %s"
                        % (type(exc).__name__, exc))
        return problems

    con = sqlite3.connect(db)
    state = {tid: con.execute(
        "select status, priority from tasks where id=?", (tid,)).fetchone()
        for tid in ("t_a1", "t_a2", "t_a3", "t_a3b", "t_a3c", "t_a4",
                    "t_a5", "t_a6")}
    marks = {r[0] for r in con.execute(
        "select task_id from task_comments where body like ?",
        (mod.REVIEW_ARM_STALE_MARKER + "%",))}
    con.close()

    # --- retiring ---------------------------------------------------------
    want("A1 arm in ready + lane running on the same head: retired",
         state["t_a1"] == ("archived", 100), str(state))
    want("A2 verdict on the exact head (no lane): retired",
         state["t_a2"] == ("archived", 100), str(state))
    want("A3 arm on a head the PR has left: retired",
         state["t_a3"] == ("archived", 100), str(state))
    want("A3c control: arm on the live head without lane or verdict: alive",
         state["t_a3c"] == ("ready", 100), str(state))
    want("A3b lane of the same PR on ANOTHER head: arm alive",
         state["t_a3b"] == ("ready", 100), str(state))
    want("A4 gh failure and no lane: conservatively alive",
         state["t_a4"] == ("ready", 100), str(state))
    want("A5 running arm untouched",
         state["t_a5"] == ("running", 100), str(state))
    want("A6 key outside the arm class untouched",
         state["t_a6"] == ("ready", 80), str(state))
    want("A7 retire marker exactly on A1/A2/A3",
         marks == {"t_a1", "t_a2", "t_a3"}, str(marks))
    stales = [f for f in found if "rev-arm-stale" in f[0]]
    want("A8 retire findings for three cards",
         len(stales) == 3, str([f[0] for f in found]))
    text1 = " ".join(f[2] for f in stales if "t_a1" in f[0])
    want("A9 the A1 text names the lane card and the head",
         "t_exec1" in text1 and "b58b544" in text1, text1)

    # --- idempotency: a repeated pass is silent ----------------------------
    calls2 = []
    _gate(mod, slug, db, calls2)
    want("A10 a repeated pass does nothing",
         not [c for c in calls2 if "comment" in c or "archive" in c],
         str(calls2))

    # --- dry-run: retiring only as a finding --------------------------------
    slug2, db2, _ = h.make_board(tasks=[TASKS[0], EXTRA_TASKS[0]])
    _seed_events(db2, EXTRA_EVENTS[:2])
    mod.BOARDS = os.path.dirname(os.path.dirname(db2))
    calls3 = []
    dry = _gate(mod, slug2, db2, calls3, apply=False)
    con = sqlite3.connect(db2)
    row = con.execute("select status, priority from tasks where"
                      " id='t_a1'").fetchone()
    con.close()
    want("A11 dry-run neither archives nor comments",
         row == ("ready", 100) and not calls3, str((row, calls3)))
    want("A12 dry-run returns a [dry-run: not retired] finding",
         any("dry-run: not retired" in f[2] for f in dry), str(dry))
    return problems


def main():
    mod, _ = load_bw()
    problems = battery(mod)

    # Mutant: the arm class is not recognised (keys do not match), retiring
    # is off and the arm waits for a slot for a second review again. The
    # weak classification (key prefix + head from title/body) must be cut
    # too, otherwise the prefix restores the class and the battery stays
    # green for nothing.
    src = open(BW_PATH, encoding="utf-8").read()
    mut = src.replace(
        "m_rev = _REVIEW_ARM_KEY_RE.match(key)",
        "m_rev = None and _REVIEW_ARM_KEY_RE.match(key)")
    assert mut != src, "mutant 1 did not apply"
    mut = mut.replace(
        "            m_rev = (re.match(r\"reviewer-pr(\\d+)\", key, re.I)\n"
        "                     if key.lower().startswith(\"reviewer-pr\")"
        " else None)",
        "            m_rev = None")
    mut = mut.replace(
        "            m_wcfy = (re.match(r\"coder-pr(\\d+)-wcfy\", key, re.I)\n"
        "                      if m_rev is None else None)",
        "            m_wcfy = None")
    assert "            m_rev = None\n" in mut and "m_wcfy = None\n" in mut, \
        "mutant 1b (weak classification) did not apply"
    m1, _ = load_bw(mut, tag="ral_m1")
    print("--- MUT1 (expected red) ---")
    if not battery(m1):
        problems.append("MUT1: the battery did not turn red (retiring off)")

    print("\nTOTAL review arm liveness: %s" % (
        "OK" if not problems else "FAIL %s" % "; ".join(problems)))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
