#!/usr/bin/env python3
"""Admission gate of board-watchdog.py: live duplicates of one issue.

Why: three coder cards ended up on one patch. A reviewer created a card with
a wrong link direction, could not unlink from a worker context and created a
second one 90 seconds later; a manual restore added a third, and the
dispatcher started a run on it. The gates stayed silent: the admission
dedup scanned only parked cards, gen_admission checks admission by issue and
milestone before create, not duplicates, and chain_peers tells chain links
apart by coder->review edges that both sibling coders had.

Rule: a live duplicate is the same open issue, the same producing role
(coder/architect/tester), an active status and NO direct task_links edge
between the copies. Keeper = the running one, otherwise priority, then the
oldest. More than one running card is only a finding: a human decides
whether to stop active runs.

Scenarios:
  A. three coders of one issue without edges between them: the running one
     survives, the other two are archived with a reference, their children
     are relinked to the keeper.
  B. two coders linked by a direct edge are a chain: both stay alive.
  C. two running coders of one issue: a finding only, no actions.
  D. a repeated run is idempotent.

Run: python3 test_board_watchdog_admission_live_dups.py
Mutant run: BOARD_WATCHDOG_SCRIPT=/path/to/mutant.py python3 test_board_watchdog_admission_live_dups.py
"""
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.join(HERE, "test_board_watchdog_board_lint.py")
spec = importlib.util.spec_from_file_location("wd_lint_harness_live_dups",
                                              HARNESS)
if spec is None or spec.loader is None:
    print("SKIP: cannot load harness")
    sys.exit(0)
h = importlib.util.module_from_spec(spec)
sys.modules["wd_lint_harness_live_dups"] = h
spec.loader.exec_module(h)
mod = h.mod

_sbx = tempfile.mkdtemp(prefix="bw-sbx-live-dups-")
mod.BACKLOG_CACHE = os.path.join(_sbx, "backlog-cache.json")
mod.STATE = os.path.join(_sbx, "watchdog-state.json")

FAILS = []


def check(name, cond, detail=""):
    print("%s %s%s" % ("ok" if cond else "FAIL", name,
                       (" :: " + detail) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def issues_json():
    return json.dumps([
        {"number": n, "milestone": {"title": "2.3.0"},
         "labels": [{"name": "hermes"}]} for n in (100, 200, 300)
    ])


def make_fake(db, calls):
    def _run(cmd, timeout=120):
        calls.append(cmd)
        if cmd[:2] == ["gh", "api"] and any("milestones" in t
                                            for t in cmd):
            return json.dumps([
                {"title": "2.3.0", "due_on": "2026-11-23T00:00:00Z"},
                {"title": "2.4.0", "due_on": "2026-12-20T00:00:00Z"}])
        if cmd[:2] == ["gh", "issue"] and "list" in cmd:
            return issues_json()
        if cmd[:2] == ["gh", "pr"] and "list" in cmd:
            return "[]"
        if len(cmd) > 5 and cmd[1] == "kanban":
            action, tid = cmd[4], cmd[5]
            con = sqlite3.connect(db, timeout=10)
            try:
                if action == "schedule":
                    con.execute("update tasks set status='scheduled' "
                                "where id=?", (tid,))
                elif action == "unblock":
                    con.execute("update tasks set status='todo' where id=?",
                                (tid,))
                elif action == "archive":
                    con.execute("update tasks set status='archived' "
                                "where id=?", (tid,))
                elif action == "comment":
                    con.execute(
                        "insert into task_comments (task_id, body,"
                        " created_at) values (?,?,?)",
                        (tid, " ".join(cmd[6:]), int(time.time())))
                elif action == "edit":
                    if "--priority" in cmd:
                        val = cmd[cmd.index("--priority") + 1]
                        con.execute("update tasks set priority=? where id=?",
                                    (int(val), tid))
                elif action == "link":
                    con.execute("insert into task_links values (?,?)",
                                (tid, cmd[6]))
                elif action == "unlink":
                    con.execute("delete from task_links where parent_id=? "
                                "and child_id=?", (tid, cmd[6]))
                con.commit()
            finally:
                con.close()
            return "ok"
        return ""
    return _run


now = time.time()
slug, db, old_boards = h.make_board(
    tasks=[
        # A: three coders, no edges between them
        {"id": "t_dup1", "status": "todo", "priority": 5,
         "assignee": "coder", "age": 90000, "created_by": "reviewer",
         "body": "rework issue #100 (first copy)"},
        {"id": "t_keep", "status": "running", "priority": 5,
         "assignee": "coder", "age": 60000, "created_by": "reviewer",
         "body": "rework issue #100 (working copy)"},
        {"id": "t_dup3", "status": "ready", "priority": 80,
         "assignee": "coder", "age": 30000, "created_by": "auto-decomposer",
         "body": "Switch to relative budget issue #100 (restored)"},
        {"id": "t_rev", "status": "todo", "priority": 5,
         "assignee": "reviewer", "age": 50000, "body": "review issue #100"},
        {"id": "t_stab", "status": "todo", "priority": 5,
         "assignee": "tester", "age": 40000, "body": "stability #100"},
        # B: two coders linked by an edge = a chain, not a duplicate
        {"id": "t_e1", "status": "todo", "priority": 5,
         "assignee": "coder", "age": 20000, "body": "work issue #200"},
        {"id": "t_e2", "status": "todo", "priority": 5,
         "assignee": "coder", "age": 10000, "body": "continuation issue #200"},
        # C: two running cards, a finding only
        {"id": "t_r1", "status": "running", "priority": 5,
         "assignee": "coder", "age": 8000, "body": "run-1 issue #300"},
        {"id": "t_r2", "status": "running", "priority": 5,
         "assignee": "coder", "age": 7000, "body": "run-2 issue #300"},
        {"id": "t_rrev", "status": "todo", "priority": 5,
         "assignee": "reviewer", "age": 6000, "body": "review issue #300"},
    ],
    links=[("t_keep", "t_rev"), ("t_dup3", "t_stab"),
           ("t_dup3", "t_rev"), ("t_e1", "t_e2"),
           ("t_r1", "t_rrev"), ("t_r2", "t_rrev")])

mod.board_repo = lambda s: "acme/demo"
calls = []
real_run = mod.run
mod.run = make_fake(db, calls)
findings = mod.admission_gate(slug)

con = sqlite3.connect(db)
statuses = dict(con.execute("select id, status from tasks"))
commented = {}
for tid, cbody in con.execute("select task_id, body from task_comments"):
    commented.setdefault(tid, []).append(cbody)
links_after = set(con.execute(
    "select parent_id, child_id from task_links").fetchall())
con.close()
txt = "\n".join(f[-1] if isinstance(f, tuple) else str(f)
               for f in findings)

check("A1 the running coder survived (t_keep)",
      statuses["t_keep"] == "running", "status %r" % statuses["t_keep"])
check("A2 the first copy is archived (t_dup1)",
      statuses["t_dup1"] == "archived", "status %r" % statuses["t_dup1"])
check("A3 the restored copy is archived (t_dup3)",
      statuses["t_dup3"] == "archived", "status %r" % statuses["t_dup3"])
check("A4 the duplicate refers to the working t_keep",
      all(any("t_keep" in c and "duplicate" in c
              for c in commented.get(t, []))
          for t in ("t_dup1", "t_dup3")),
      "comments %r / %r" % (commented.get("t_dup1"),
                            commented.get("t_dup3")))
check("A5 the children of the duplicate are relinked to t_keep (t_stab)",
      ("t_keep", "t_stab") in links_after, "links %r" % sorted(links_after))
check("A6 review and check cards are untouched (other roles)",
      statuses["t_rev"] == "todo" and statuses["t_stab"] == "todo",
      "statuses %r" % [statuses["t_rev"], statuses["t_stab"]])
check("A7 a live-dup finding in the report", "live-dup" in txt or
      "live duplicate" in txt, txt[:300])

check("B coders linked by an edge stay alive (a chain)",
      statuses["t_e1"] == "todo" and statuses["t_e2"] == "todo",
      "statuses %r %r" % (statuses["t_e1"], statuses["t_e2"]))

check("C two running cards are not stopped automatically",
      statuses["t_r1"] == "running" and statuses["t_r2"] == "running",
      "statuses %r %r" % (statuses["t_r1"], statuses["t_r2"]))
check("C2 two running cards give a finding for a human",
      "more than one running" in txt, txt[:400])

# D: a repeated run is idempotent
calls2 = []
mod.run = make_fake(db, calls2)
mod.admission_gate(slug)
mod.run = real_run
mod.BOARDS = old_boards
con = sqlite3.connect(db)
statuses2 = dict(con.execute("select id, status from tasks"))
con.close()
actions2 = [c for c in calls2 if len(c) > 4 and c[1] == "kanban"
            and c[4] in ("schedule", "comment", "unblock", "archive",
                         "link", "unlink")]
check("D1 a repeated run does not duplicate actions", not actions2,
      "actions %r" % (actions2[:3],))
check("D2 the state is stable after the second run",
      statuses2["t_keep"] == "running" and statuses2["t_dup1"] == "archived"
      and statuses2["t_dup3"] == "archived" and statuses2["t_e1"] == "todo",
      "statuses %r" % statuses2)

print("\n%s" % ("ALL OK" if not FAILS else "FAILURES: %s" % ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
