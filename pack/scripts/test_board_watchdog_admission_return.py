#!/usr/bin/env python3
"""Admission gate: return from parking, subtree, milestone bands.

Why: a report claimed "the return is automatic: once the issue gets a
milestone, the next hourly run of the gate lets the card back in". It was
false: milestones were assigned, the gate ran, and the parked cards stayed
scheduled, because admission_gate had no return code and no test.

Checks:

  R1. Return from parking: issue #100 got the active milestone. The gate's
      parked cards linked by task_links inside the group (t_keeper -> t_dup
      -> t_dup_kid, t_selfref -> t_keeper) are links of one chain, not
      duplicates: ALL of them return, with the priority band of the
      milestone (80). Only the duplicate with no link to the group (t_hold)
      is archived, with a reference to the working card (one live card per
      PR/issue; this does not apply to links of one issue's chain).
  R2. A duplicate with a live child outside the group is archived, the
      child is rebound to the working card.
  R3. The card's own binding to an unapproved issue dominates the service
      exception of an open PR (a PR against an approved issue must not hide
      an issue without a milestone): parking.
  R4. Unclaimed without requiring the hermes label: an owner issue #374
      with a milestone and no card appears in the findings.
  R5. Milestone priority bands: active = 80, next = 40.
  R6. The admission cache for the claim guard is written next to the board.
  R7. Idempotency: a repeated run does not duplicate actions.

Uses the board lint harness (test_board_watchdog_board_lint.py).
Run: python3 test_board_watchdog_admission_return.py
"""
import importlib.util
import json
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.join(HERE, "test_board_watchdog_board_lint.py")
spec = importlib.util.spec_from_file_location("wd_lint_harness", HARNESS)
if spec is None or spec.loader is None:
    print("SKIP: cannot load harness")
    sys.exit(0)
h = importlib.util.module_from_spec(spec)
sys.modules["wd_lint_harness"] = h
spec.loader.exec_module(h)
mod = h.mod   # the harness sets an isolated HERMES_HOME before loading

ACTIVE, NEXT = "3.1.0", "3.2.0"

FAILS = []


def check(name, cond, detail=""):
    print("%s %s%s" % ("ok" if cond else "FAIL", name,
                       (" :: " + detail) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def issues_json():
    """#100/#300 active, #301/#182 next (approved), #330 without a
    milestone, #374 of the owner: has a milestone, NO hermes label."""
    def lab(*names):
        return [{"name": n} for n in names]
    return json.dumps([
        {"number": 100, "milestone": {"title": ACTIVE}, "labels": lab("bug")},
        {"number": 300, "milestone": {"title": ACTIVE}, "labels": lab("hermes")},
        {"number": 301, "milestone": {"title": NEXT}, "labels": lab("hermes")},
        {"number": 182, "milestone": {"title": NEXT}, "labels": lab("hermes")},
        {"number": 330, "milestone": None, "labels": lab("enhancement")},
        {"number": 374, "milestone": {"title": ACTIVE}, "labels": lab("bug")},
    ])


def prs_json():
    """PR #321 against the approved #182."""
    return json.dumps([
        {"number": 321, "body": "Refs #182, follow-up work"},
    ])


def make_fake(db, calls):
    def _run(cmd, timeout=120):
        calls.append(cmd)
        # Open milestones for the priority bands: the earliest due date is
        # active (80), the next one gets 40.
        if cmd[:2] == ["gh", "api"] and any("milestones" in t for t in cmd):
            return json.dumps([
                {"title": ACTIVE, "due_on": "2026-11-23T00:00:00Z"},
                {"title": NEXT, "due_on": "2026-12-20T00:00:00Z"}])
        if cmd[:2] == ["gh", "issue"] and "list" in cmd:
            return issues_json()
        if cmd[:2] == ["gh", "pr"] and "list" in cmd:
            return prs_json()
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
                    assert "--priority" in cmd, cmd
                    val = cmd[cmd.index("--priority") + 1]
                    con.execute("update tasks set priority=? where id=?",
                                (int(val), tid))
                elif action == "link":
                    con.execute("insert into task_links values (?,?)",
                                (tid, cmd[6]))
                elif action == "unlink":
                    con.execute("delete from task_links where parent_id=?"
                                " and child_id=?", (tid, cmd[6]))
                con.commit()
            finally:
                con.close()
            return "ok"
        return ""
    return _run


PARK = "admission gate: not admitted (#100 without a milestone). Taken off the queue"
LINK100 = "issue-link: #100 (admission gate binding)"

slug, db, old_boards = h.make_board(
    tasks=[
        # group of issue #100: parked cards of the gate
        {"id": "t_keeper", "status": "scheduled", "priority": 5,
         "age": 90000, "body": "work on issue #100"},
        {"id": "t_dup", "status": "scheduled", "priority": 5,
         "age": 5000, "body": "duplicate on issue #100"},
        {"id": "t_dup_kid", "status": "scheduled", "priority": 5,
         "age": 4000, "body": "subtask of issue #100"},
        # a duplicate with a live child outside the group
        {"id": "t_hold", "status": "scheduled", "priority": 5,
         "age": 3000, "body": "another duplicate of issue #100"},
        {"id": "t_outside", "status": "todo", "priority": 5,
         "body": "child of t_hold without its own binding"},
        # a duplicate whose live child is the working card itself: a
        # rebind would link the keeper to itself
        {"id": "t_selfref", "status": "scheduled", "priority": 5,
         "age": 2000, "body": "duplicate of issue #100, child is the working card"},
        # promoted leak: own #330 without a milestone, service through PR #321
        {"id": "t_leak", "status": "ready", "priority": 80,
         "body": "issue #330, follow-up from the review of PR #321"},
        # priority bands
        {"id": "t_b23", "status": "ready", "priority": 40,
         "body": "card on issue #300"},
        {"id": "t_b24", "status": "ready", "priority": 80,
         "body": "card on issue #301"},
    ],
    links=[("t_keeper", "t_dup"), ("t_dup", "t_dup_kid"),
           ("t_hold", "t_outside"), ("t_selfref", "t_keeper")],
    comments=[
        ("t_keeper", LINK100), ("t_keeper", PARK),
        ("t_dup", LINK100), ("t_dup", PARK),
        ("t_dup_kid", LINK100), ("t_dup_kid", PARK),
        ("t_hold", LINK100), ("t_hold", PARK),
        ("t_selfref", LINK100), ("t_selfref", PARK),
        ("t_b23", "issue-link: #300"), ("t_b24", "issue-link: #301"),
    ])
mod.board_repo = lambda s: "acme/demo"
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
con.close()
txt = "\n".join(t for _, _, t in findings)

check("R1a working card t_keeper returned from parking",
      statuses["t_keeper"] in ("ready", "todo"),
      "status %r" % statuses["t_keeper"])
check("R1b t_dup returned as a chain link, not archived",
      statuses["t_dup"] == "todo", "status %r" % statuses["t_dup"])
check("R1c t_dup_kid returned as a chain link",
      statuses["t_dup_kid"] == "todo",
      "status %r" % statuses["t_dup_kid"])
check("R1d unlinked duplicate archived with a reference to the working card",
      any("t_keeper" in c and "duplicate" in c
          for c in commented.get("t_hold", [])),
      "comments %r" % commented.get("t_hold"))
check("R1e active band 80 applied to the returned card",
      prios["t_keeper"] == 80, "prio %r" % prios["t_keeper"])
check("R1f child t_outside untouched (admitted through its ancestor)",
      statuses["t_outside"] == "todo", "status %r" % statuses["t_outside"])

check("R2 duplicate with a live child archived, the child stays live",
      statuses["t_hold"] == "archived" and statuses["t_outside"] == "todo"
      and any("t_outside" in c for c in commented.get("t_hold", [])),
      "statuses %r/%r" % (statuses["t_hold"], statuses["t_outside"]))

# Archiving a duplicate rebinds its live children to the working card,
# otherwise the child keeps an archived parent and hits lint I6.
# t_selfref is linked to the working t_keeper (a chain link), so it is not
# archived and the link stays.
lcon = sqlite3.connect(db)
links_after = set(lcon.execute(
    "select parent_id, child_id from task_links").fetchall())
lcon.close()
check("R9a live child of the duplicate rebound to the working card",
      ("t_keeper", "t_outside") in links_after,
      "links %r" % sorted(links_after))
check("R9b parent link t_selfref not archived, the link is alive",
      ("t_selfref", "t_keeper") in links_after
      and statuses["t_selfref"] == "todo",
      "links %r, status %r" % (sorted(links_after),
                               statuses["t_selfref"]))

check("R3 own unapproved #330 parked despite the service PR #321",
      statuses["t_leak"] == "scheduled", "status %r" % statuses["t_leak"])

check("R4 unclaimed sees the owner issue #374 without the hermes label",
      "374" in txt and "without a card" in txt, txt[:400])

check("R5a active band: p40 -> p80", prios["t_b23"] == 80,
      "prio %r" % prios["t_b23"])
check("R5b next band: p80 -> p40", prios["t_b24"] == 40,
      "prio %r" % prios["t_b24"])

cache_path = os.path.join(mod.BOARDS, slug, "admission-approved.json")
cache_ok = False
if os.path.exists(cache_path):
    cache = json.load(open(cache_path))
    cache_ok = (set(cache.get("issues", {})) >= {"100", "300", "301", "182",
                                                 "374"}
                and 321 in cache.get("prs", []))
check("R6 admission cache for the claim guard written", cache_ok,
      "missing %s" % cache_path)

# R7: idempotency of a repeated run
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
check("R7 a repeated run does not duplicate actions", not actions2,
      "actions %r" % (actions2[:3],))
check("R7b the state is stable after the second run",
      statuses2["t_keeper"] in ("ready", "todo")
      and statuses2["t_dup"] == "todo"
      and statuses2["t_dup_kid"] == "todo"
      and statuses2["t_selfref"] == "todo"
      and statuses2["t_hold"] == "archived",
      "statuses %r" % statuses2)

print("\n%s" % ("ALL OK" if not FAILS else "FAILURES: %s" % ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
