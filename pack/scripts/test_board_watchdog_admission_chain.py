#!/usr/bin/env python3
"""Admission gate: links of one issue's chain are not duplicates.

Why: the admission gate used to pick the working card of an issue as
sorted(active)[0], the lexicographically smallest id. It archived the only
coder card writing the fix as a "duplicate" of an ops card that merely
closes the issue, although review, stability and derisk cards were linked
to the coder card by task_links and waited for its result.

Scenarios:
  A. Active ops/architect/tester/reviewer cards of one issue, a coder card
     linked to them and an unlinked visual-delta card in parking. The coder
     card returns (a chain link); the visual-delta card is archived as a
     duplicate naming the artifact producer (architect) as the working
     card, not the ops card.
  B. No active cards: a coder->reviewer chain in parking plus an unlinked
     duplicate. Both links return, only the unlinked duplicate is archived.
  C. A repeated run is idempotent.

Red run against an older copy of the script:
  BOARD_WATCHDOG_SCRIPT=<copy> python3 test_board_watchdog_admission_chain.py

Run: python3 test_board_watchdog_admission_chain.py
"""
import importlib.util
import json
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.join(HERE, "test_board_watchdog_board_lint.py")
spec = importlib.util.spec_from_file_location("wd_lint_harness2", HARNESS)
if spec is None or spec.loader is None:
    print("SKIP: cannot load harness")
    sys.exit(0)
h = importlib.util.module_from_spec(spec)
sys.modules["wd_lint_harness2"] = h
spec.loader.exec_module(h)

import tempfile as _tf
_sbx = _tf.mkdtemp(prefix="bw-sbx-chain-")
h.mod.BACKLOG_CACHE = os.path.join(_sbx, "backlog-cache.json")
h.mod.STATE = os.path.join(_sbx, "watchdog-state.json")
mod = h.mod

FAILS = []


def check(name, cond, detail=""):
    print("%s %s%s" % ("ok" if cond else "FAIL", name,
                       (" :: " + detail) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def issues_json():
    def lab(*names):
        return [{"name": n} for n in names]
    return json.dumps([
        {"number": 100, "milestone": {"title": "1.0.0"},
         "labels": lab("hermes")},
        {"number": 200, "milestone": {"title": "1.0.0"},
         "labels": lab("hermes")},
    ])


def make_fake(db, calls):
    def _run(cmd, timeout=120):
        calls.append(cmd)
        if cmd[:2] == ["gh", "api"] and any("milestones" in t
                                            for t in cmd):
            return json.dumps([
                {"title": "1.0.0", "due_on": "2026-11-23T00:00:00Z"},
                {"title": "1.1.0", "due_on": "2026-12-20T00:00:00Z"}])
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
                    if "--reason" in cmd:
                        con.execute(
                            "insert into task_comments (task_id, body,"
                            " created_at) values (?,?,?)",
                            (tid, "UNBLOCK: " + cmd[cmd.index("--reason")
                                                     + 1],
                             int(time.time())))
                elif action == "archive":
                    con.execute("update tasks set status='archived' "
                                "where id=?", (tid,))
                elif action == "comment":
                    con.execute(
                        "insert into task_comments (task_id, body,"
                        " created_at) values (?,?,?)",
                        (tid, " ".join(cmd[6:]), int(time.time())))
                elif action == "edit":
                    val = cmd[cmd.index("--priority") + 1]
                    con.execute("update tasks set priority=? where id=?",
                                (int(val), tid))
                elif action == "link":
                    con.execute("insert into task_links values (?,?)",
                                (tid, cmd[6]))
                con.commit()
            finally:
                con.close()
            return "ok"
        return ""
    return _run


PARK = ("admission gate: not admitted (#100 without a milestone). Taken off"
        " the queue")
PARK200 = ("admission gate: not admitted (#200 without a milestone). Taken"
           " off the queue")
L100 = "issue-link: #100 (admission gate binding)"
L200 = "issue-link: #200 (admission gate binding)"

slug, db, old_boards = h.make_board(
    tasks=[
        # A: an issue with several active role cards
        {"id": "t_coder", "status": "scheduled", "priority": 5,
         "assignee": "coder", "age": 90000, "body": "fix for issue #100"},
        {"id": "t_close", "status": "todo", "priority": 5,
         "assignee": "ops", "age": 60000, "body": "close issue #100"},
        {"id": "t_arch", "status": "todo", "priority": 5,
         "assignee": "architect", "age": 70000, "body": "derisk #100"},
        {"id": "t_stab", "status": "todo", "priority": 5,
         "assignee": "tester", "age": 50000, "body": "stability #100"},
        {"id": "t_rev", "status": "running", "priority": 5,
         "assignee": "reviewer", "age": 40000, "body": "review #100"},
        {"id": "t_vd", "status": "scheduled", "priority": 5,
         "assignee": "coder", "age": 30000,
         "title": "[visual-delta] issue #100: Before / After section",
         "body": "visual-delta issue #100"},
        # B: no active cards, a chain plus an unlinked duplicate
        {"id": "t_c2_kr", "status": "scheduled", "priority": 80,
         "assignee": "coder", "age": 80000, "body": "work on issue #200"},
        {"id": "t_c2_rev", "status": "scheduled", "priority": 80,
         "assignee": "reviewer", "age": 70000, "body": "review of issue #200"},
        {"id": "t_c2_dup", "status": "scheduled", "priority": 80,
         "assignee": "coder", "age": 1000, "body": "duplicate of issue #200"},
    ],
    links=[("t_coder", "t_rev"), ("t_coder", "t_stab"),
           ("t_coder", "t_arch"), ("t_c2_kr", "t_c2_rev")],
    comments=[
        ("t_coder", L100), ("t_coder", PARK),
        ("t_close", L100), ("t_arch", L100), ("t_stab", L100),
        ("t_rev", L100),
        ("t_vd", L100), ("t_vd", PARK),
        ("t_c2_kr", L200), ("t_c2_kr", PARK200),
        ("t_c2_rev", L200), ("t_c2_rev", PARK200),
        ("t_c2_dup", L200), ("t_c2_dup", PARK200),
    ])
mod.board_repo = lambda s: "acme/widget"
calls = []
real_run = mod.run
mod.run = make_fake(db, calls)
findings = mod.admission_gate(slug)

con = sqlite3.connect(db)
statuses = dict(con.execute("select id, status from tasks"))
prios = dict(con.execute("select id, priority from tasks"))
commented = {}
for tid, cbody in con.execute("select task_id, body from task_comments"):
    commented.setdefault(tid, []).append(cbody)
links_after = set(con.execute(
    "select parent_id, child_id from task_links").fetchall())
con.close()
txt = "\n".join(t for _, _, t in findings)

check("A1 the executor returned, not archived (t_coder)",
      statuses["t_coder"] == "todo", "status %r" % statuses["t_coder"])
check("A2 the active band 80 applied to the returned executor",
      prios["t_coder"] == 80, "prio %r" % prios["t_coder"])
check("A3 the return comment explains the chain link",
      any("chain link" in c for c in commented.get("t_coder", [])),
      "comments %r" % commented.get("t_coder"))
check("A4 the unlinked visual-delta card is archived as a duplicate",
      statuses["t_vd"] == "archived", "status %r" % statuses["t_vd"])
check("A5 the duplicate comment names the artifact producer (t_arch)"
      " as the working card, not the ops card t_close",
      any("t_arch" in c and "duplicate" in c
          for c in commented.get("t_vd", [])),
      "comments %r" % commented.get("t_vd"))
check("A6 the chain-return finding is in the report",
      "t_coder" in txt and "chain link" in txt, txt[:300])

check("B1 with no active cards the executor link returns (t_c2_kr)",
      statuses["t_c2_kr"] == "todo", "status %r" % statuses["t_c2_kr"])
check("B2 the review link returns too, not archived (t_c2_rev)",
      statuses["t_c2_rev"] == "todo", "status %r" % statuses["t_c2_rev"])
check("B3 the unlinked duplicate is archived (t_c2_dup)",
      statuses["t_c2_dup"] == "archived",
      "status %r" % statuses["t_c2_dup"])
check("B4 the duplicate points at the working card t_c2_kr",
      any("t_c2_kr" in c and "duplicate" in c
          for c in commented.get("t_c2_dup", [])),
      "comments %r" % commented.get("t_c2_dup"))

# C: a repeated run is idempotent
calls2 = []
mod.run = make_fake(db, calls2)
mod.admission_gate(slug)
mod.run = real_run
mod.BOARDS = old_boards
con = sqlite3.connect(db)
statuses2 = dict(con.execute("select id, status from tasks"))
links2 = set(con.execute(
    "select parent_id, child_id from task_links").fetchall())
con.close()
actions2 = [c for c in calls2 if len(c) > 4 and c[1] == "kanban"
            and c[4] in ("schedule", "comment", "unblock", "archive",
                         "link", "unlink")]
check("C1 a repeated run does not repeat actions", not actions2,
      "actions %r" % (actions2[:3],))
check("C2 the state is stable after the second run",
      statuses2["t_coder"] == "todo" and statuses2["t_vd"] == "archived"
      and statuses2["t_c2_kr"] == "todo"
      and statuses2["t_c2_rev"] == "todo"
      and statuses2["t_c2_dup"] == "archived",
      "statuses %r" % statuses2)
check("C3 the chain links are not broken",
      ("t_coder", "t_rev") in links2 and ("t_c2_kr", "t_c2_rev") in links2,
      "links %r" % sorted(links2))

print("\n%s" % ("ALL OK" if not FAILS else
                "FAILURES: %s" % ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
