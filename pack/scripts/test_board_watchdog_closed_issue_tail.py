#!/usr/bin/env python3
"""The tail of a closed issue must not live on the board.

The owner closes an issue (its PR is merged), and the links of its card
chain (root, tester, an ops card "close #N") keep living and taking slots.
Checks of closed_issue_tail_gate:

  A. An anchor on an issue closed more than an hour ago -> the task_links
     chain is retired: todo/scheduled cards are archived with the marker
     comment closed-issue-tail; a follow-up link anchored on an OPEN issue
     stays alive (finding closedtail-keep); a running card on the tail
     gives the owner finding closedtail-run and is not touched.
  B. Idempotency: a second run over the same database changes nothing.
  C. Grace: an issue closed 5 minutes ago must not be retired.
  D. The anchor comes from the "Issue:" line, not from the whole body: a
     side mention of an open #324 does not save the tail of #325; the line
     "Issue: #324 #325" keeps both anchors.
  E. An anchor "PR #323" (a PR number, absent from the issue lists) does
     not retire the card: a silent skip, the review lane is left alone.
  F. A card outside the task_links chain of the anchor is not retired,
     even if its body mentions the closed issue in passing.

The harness (mini board, script module) is imported from
test_board_watchdog_board_lint.py.

Run: python3 test_board_watchdog_closed_issue_tail.py
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
mod = h.mod

FAILS = []


def check(name, cond, detail=""):
    print("%s %s%s" % ("ok" if cond else "FAIL", name,
                       (" :: " + detail) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def fake_run(db, calls):
    """kanban comment/archive are applied to the test database; no gh."""
    def _run(cmd, timeout=120):
        calls.append(cmd)
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


def fetch_stub(closed, opens):
    return lambda now: (dict(closed), set(opens))


check("A0 the gate exists", hasattr(mod, "closed_issue_tail_gate"),
      "closed_issue_tail_gate is missing")

if hasattr(mod, "closed_issue_tail_gate"):
    now = time.time()
    # A: the chain of the closed #325 (closed 2 hours ago), #343 is open
    slug, db, old_boards = h.make_board(
        tasks=[
            {"id": "t_root", "status": "todo", "priority": 80,
             "title": "CI red base: derisk ConcurrentToolCallsIT",
             "body": "Issue: #325\nthe red base holds 24 open PRs (#324)"},
            {"id": "t_stab", "status": "scheduled", "priority": 80,
             "title": "Verify budget stability across repeated CI runs",
             "body": "Issue: #325\nsample after the PR is merged"},
            {"id": "t_ops", "status": "todo", "priority": 100,
             "title": "Post final verdict and close #325",
             "body": "Issue: #325"},
            {"id": "t_fu", "status": "todo", "priority": 0,
             "title": "Follow-up: move Thread.sleep out of the event loop",
             "body": "Issue: #343"},
            {"id": "t_run", "status": "running", "priority": 80,
             "title": "final stability run",
             "body": "Issue: #325"},
            {"id": "t_out", "status": "todo", "priority": 0,
             "title": "neighbouring work",
             "body": "mentions #325 in passing in the text"},
        ],
        links=[("t_root", "t_stab"), ("t_root", "t_ops"),
               ("t_root", "t_fu"), ("t_root", "t_run")])
    old_run = mod.run
    calls = []
    mod.run = fake_run(db, calls)
    findings = mod.closed_issue_tail_gate(
        slug, apply=True, now=now, fetch=fetch_stub({325: now - 7200}, {343}))
    mod.run = old_run
    keys = [k for k, _r, _t in findings]

    con = sqlite3.connect(db)
    st = {tid: con.execute("select status from tasks where id=?",
                           (tid,)).fetchone()[0]
          for tid in ("t_root", "t_stab", "t_ops", "t_fu", "t_run", "t_out")}
    markers = con.execute(
        "select count(*) from task_comments where task_id='t_ops' and"
        " body like 'closed-issue-tail:%'").fetchone()[0]
    con.close()
    mod.BOARDS = old_boards

    check("A1 root/t_stab/t_ops archived",
          st["t_root"] == "archived" and st["t_stab"] == "archived"
          and st["t_ops"] == "archived", json.dumps(st))
    check("A2 follow-up #343 alive",
          st["t_fu"] not in ("archived",), st["t_fu"])
    check("A3 running untouched, owner finding present",
          st["t_run"] == "running"
          and any(k.startswith("closedtail-run:") for k in keys), str(keys))
    check("A4 card outside the chain alive (side mention)",
          st["t_out"] == "todo", st["t_out"])
    check("A5 marker comment on the archived card", markers == 1)
    check("A6 keep finding on the follow-up",
          any(k == "closedtail-keep:%s:t_fu" % slug for k in keys))
    check("A7 classification closedtail-run -> owner",
          mod.classify_finding("closedtail-run:x:t_y") == "owner",
          mod.classify_finding("closedtail-run:x:t_y"))
    check("A8 classification of the archive -> digest",
          mod.classify_finding("closedtail:x:t_y") == "digest")

    # B: idempotency
    calls2 = []
    mod.run = fake_run(db, calls2)
    findings2 = mod.closed_issue_tail_gate(
        slug, apply=True, now=now, fetch=fetch_stub({325: now - 7200}, {343}))
    mod.run = old_run
    check("B1 second run neither mutates nor reports",
          not calls2 and not findings2,
          "calls=%d findings=%s" % (len(calls2), findings2))

    # C: grace of 5 minutes
    slug, db, old_boards = h.make_board(
        tasks=[{"id": "t_root", "status": "todo", "title": "close #325",
                "body": "Issue: #325"}])
    calls3 = []
    mod.run = fake_run(db, calls3)
    findings3 = mod.closed_issue_tail_gate(
        slug, apply=True, now=now, fetch=fetch_stub({325: now - 300}, set()))
    mod.run = old_run
    con = sqlite3.connect(db)
    st3 = con.execute("select status from tasks where id='t_root'"
                      ).fetchone()[0]
    con.close()
    mod.BOARDS = old_boards
    check("C1 closed 5 minutes ago -> must not be retired",
          st3 == "todo" and not findings3, st3)

    # D: the anchor comes from the Issue: line, not from the whole body
    check("D1 a side #324 in the body is not an anchor",
          mod.closed_tail_anchor_refs(
              "Verify budget stability",
              "Issue: #325\ncontext: release prep (#324)") == {325})
    check("D2 the line 'Issue: #324 #325' keeps both anchors",
          mod.closed_tail_anchor_refs(
              "x", "Issue: #324 #325") == {324, 325})

    # E: a PR number in the title is skipped silently
    slug, db, old_boards = h.make_board(
        tasks=[{"id": "t_rev", "status": "scheduled",
                "title": "[reviewer] PR #323 (head abc): review",
                "body": "body"}])
    calls4 = []
    mod.run = fake_run(db, calls4)
    findings4 = mod.closed_issue_tail_gate(
        slug, apply=True, now=now, fetch=fetch_stub({325: now - 7200}, {343}))
    mod.run = old_run
    con = sqlite3.connect(db)
    st4 = con.execute("select status from tasks where id='t_rev'"
                      ).fetchone()[0]
    con.close()
    mod.BOARDS = old_boards
    check("E1 anchor PR #323 is not retired (a PR is not an issue)",
          st4 == "scheduled" and not findings4, st4)

print()
if FAILS:
    print("FAIL: %d: %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("closed-issue-tail: all ok")
