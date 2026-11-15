#!/usr/bin/env python3
"""Admission rule of board-watchdog.py (admission_gate, review_layer_check).

Rule: only issues of the current or an assigned milestone, or issues the owner
released from wait-for-triage, go to implementation. An idea of an ideation
cron lives as a GitHub issue labeled hermes + wait-for-triage and WITHOUT a
card on the board; a card is created once the owner assigns a milestone.

Checks:

  A. Cards of unapproved issues (wait-for-triage, no milestone) in
     todo/ready/scheduled are parked by the admission gate with the marker
     "admission gate:" and do not stay in the queue.
  B. A clean case is untouched: a card of an approved issue (milestone set)
     is not parked.
  C. Idempotency: a repeated run does not duplicate the comment.
  D. blocked is untouched (the card already waits for a human).
  E. An approved hermes issue without a live card gives an "unclaimed"
     finding.
  F. A draft PR is not armed: the live reviewer card of a draft PR is
     archived by the review layer and no new arm happens.

Self-contained: the minimal board builder is below (same schema as the
board lint test harness).

Run: python3 test_board_watchdog_admission.py
Red run against an old script copy (A and F must fail): copy it next to the
other scripts and run BOARD_WATCHDOG_SCRIPT=<copy> python3 test_board_watchdog_admission.py
"""
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import time

# Settings and Hermes home are fixed before the script is loaded.
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="wd-home-")
os.environ.update({
    "FACTORY_REPO": "acme/widget",
    "FACTORY_REPO_PATH": "/srv/hermes/widget",
    "FACTORY_BOARD": "main",
    "FACTORY_OWNER_LOGIN": "owner-login",
    "FACTORY_OWNER_CHAT": "telegram:000000000",
    "FACTORY_BASE_BRANCHES": "develop/1.x,develop/2.x",
})

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.environ.get("BOARD_WATCHDOG_SCRIPT",
                        os.path.join(HERE, "board-watchdog.py"))
spec = importlib.util.spec_from_file_location("board_watchdog_admission",
                                              SCRIPT)
if spec is None or spec.loader is None:
    print("SKIP: cannot load board-watchdog.py")
    sys.exit(0)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def make_board(tasks=(), links=()):
    """A mini board. tasks: id, title, body, status, assignee, priority,
    key, age. Points mod.BOARDS at it; returns (slug, db, old_boards)."""
    tmp = tempfile.mkdtemp(prefix="wd-admission-")
    slug = "testboard"
    os.makedirs(os.path.join(tmp, slug), exist_ok=True)
    db = os.path.join(tmp, slug, "kanban.db")
    con = sqlite3.connect(db)
    con.executescript("""
        create table tasks (id text primary key, title text, body text default '',
            assignee text default 'ops', status text, priority int default 0,
            created_by text default 'agent', created_at int,
            started_at int, completed_at int,
            workspace_kind text, workspace_path text, branch_name text,
            project_id text, claim_lock text, claim_expires int,
            tenant text, result text, idempotency_key text,
            consecutive_failures int default 0, worker_pid int,
            last_failure_error text, max_runtime_seconds int default null,
            last_heartbeat_at int, current_run_id int,
            workflow_template_id text, current_step_key text, skills text,
            model_override text, provider_override text,
            reasoning_effort text, max_retries int,
            goal_mode int default 0, goal_max_turns int,
            session_id text, block_kind text default null,
            block_recurrences int default 0);
        create table task_links (parent_id text, child_id text);
        create table task_runs (id integer primary key, task_id text,
            profile text, status text, started_at int, ended_at int,
            outcome text, summary text, metadata text, error text);
        create table task_events (id integer primary key, task_id text,
            run_id int, kind text, payload text, created_at int);
        create table task_comments (id integer primary key, task_id text,
            body text, created_at int);
    """)
    now = time.time()
    for t in tasks:
        con.execute(
            "insert into tasks (id, title, status, assignee, priority,"
            " created_by, created_at, idempotency_key, body)"
            " values (?,?,?,?,?,?,?,?,?)",
            (t["id"], t.get("title", "t " + t["id"]), t.get("status", "todo"),
             t.get("assignee", "coder"), t.get("priority", 42),
             t.get("created_by", "agent"), int(now - t.get("age", 3600)),
             t.get("key"), t.get("body", "body " + t["id"])))
    con.executemany("insert into task_links values (?,?)", links)
    con.commit()
    con.close()
    old_boards = mod.BOARDS
    mod.BOARDS = tmp
    return slug, db, old_boards


FAILS = []


def check(name, cond, detail=""):
    print("%s %s%s" % ("ok" if cond else "FAIL", name,
                       (" :: " + detail) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def issues_json():
    """Open issues of the fictional repo: unapproved and approved ones."""
    return json.dumps([
        {"number": 369, "milestone": None,
         "labels": [{"name": "hermes"}, {"name": "wait-for-triage"},
                    {"name": "enhancement"}]},
        {"number": 370, "milestone": None,
         "labels": [{"name": "hermes"}, {"name": "wait-for-triage"},
                    {"name": "ui"}]},
        {"number": 380, "milestone": {"title": "2.3.0"},
         "labels": [{"name": "hermes"}, {"name": "enhancement"}]},
        {"number": 381, "milestone": {"title": "2.4.0"},
         "labels": [{"name": "hermes"}, {"name": "enhancement"}]},
    ])


# --- A-E. Admission gate ---------------------------------------------------
check("A0 admission_gate exists", hasattr(mod, "admission_gate"),
      "no admission_gate: this is an old script")

if hasattr(mod, "admission_gate"):
    slug, db, old_boards = make_board(tasks=[
        # an idea of the ideation cron -> cards from auto_decompose
        {"id": "t_root", "status": "todo", "priority": 0,
         "title": "Idea: Tool Health (issue #369)",
         "body": "mirror of issue #369"},
        {"id": "t_kid1", "status": "ready", "priority": 0,
         "title": "Spike: Tool Health document #369",
         "body": "decomposition of #369"},
        {"id": "t_kid2", "status": "scheduled", "priority": 0,
         "title": "Tool Health: metrics #369",
         "body": "decomposition of #369"},
        # clean case: an approved issue of the current milestone
        {"id": "t_ok", "status": "ready", "priority": 80,
         "title": "PR #244 cycle (issue #380)",
         "body": "milestone 2.3.0, issue #380"},
        # parked by the rule: waits for a human, the gate does not touch it
        {"id": "t_park", "status": "blocked", "priority": 40,
         "title": "Waits for triage #86 (issue #370)",
         "body": "parking, issue #370"},
    ], links=[("t_root", "t_kid1"), ("t_root", "t_kid2")])
    mod.board_repo = lambda s: "acme/demo"
    calls = []
    real_run = mod.run

    def _run(cmd, timeout=120):
        """gh issue list -> scenario; kanban schedule/comment/archive is
        APPLIED to the test database (like the fake_run of the harness)."""
        calls.append(cmd)
        if cmd[:2] == ["gh", "issue"] and "list" in cmd:
            return issues_json()
        if cmd[:2] == ["gh", "pr"] and "list" in cmd:
            return "[]"        # no open PRs
        if len(cmd) > 5 and cmd[1] == "kanban":
            action, tid = cmd[4], cmd[5]
            con = sqlite3.connect(db, timeout=10)
            try:
                if action == "schedule":
                    con.execute("update tasks set status='scheduled' "
                                "where id=?", (tid,))
                elif action == "comment":
                    con.execute(
                        "insert into task_comments (task_id, body,"
                        " created_at) values (?,?,?)",
                        (tid, " ".join(cmd[6:]), int(time.time())))
                elif action == "archive":
                    con.execute("update tasks set status='archived' "
                                "where id=?", (tid,))
                con.commit()
            finally:
                con.close()
            return "Scheduled" if action == "schedule" else "ok"
        return ""

    mod.run = _run
    findings = mod.admission_gate(slug)
    mod.run = real_run
    mod.BOARDS = old_boards
    con = sqlite3.connect(db)
    statuses = dict(con.execute("select id, status from tasks"))
    markers = {r[0] for r in con.execute(
        "select task_id from task_comments "
        "where body like '%admission gate:%'")}
    con.close()
    viol = {k for k, _, _ in findings}
    check("A1 cards of #369/#370 are off the queue (park)",
          statuses["t_root"] == "scheduled"
          and statuses["t_kid1"] == "scheduled",
          "statuses %r" % statuses)
    check("A2 findings for all unapproved cards",
          any(k.endswith(":admission:t_root") for k in viol)
          and any(k.endswith(":admission:t_kid1") for k in viol),
          "keys %r" % viol)
    check("A3 the marker comment is there", {"t_root", "t_kid1"} <= markers,
          "markers %r" % markers)
    check("B approved issue of 2.3.0 is untouched",
          statuses["t_ok"] == "ready" and "t_ok" not in markers,
          "statuses %r markers %r" % (statuses, markers))
    check("D blocked is untouched (already waits for a human)",
          statuses["t_park"] == "blocked" and "t_park" not in markers,
          "statuses %r markers %r" % (statuses, markers))
    check("E approved #381 without a card -> unclaimed finding",
          any(k.endswith(":admission:unclaimed:381") for k in viol),
          "keys %r" % viol)

    # C. idempotency: a second run over the same database
    calls2 = []

    def _run2(cmd, timeout=120):
        calls2.append(cmd)
        if cmd[:2] == ["gh", "issue"] and "list" in cmd:
            return issues_json()
        return ""

    mod.run = _run2
    mod.admission_gate(slug)
    mod.run = real_run
    comments2 = [c for c in calls2
                 if len(c) > 4 and c[4] in ("comment", "schedule")]
    check("C a repeated run does not duplicate actions",
          not comments2, "actions %r" % (comments2[:3],))

# --- F. A draft PR is not armed ---------------------------------------------
check("F0 review layer exists", hasattr(mod, "review_layer_check"))

if hasattr(mod, "review_layer_check"):
    slug, db, old_boards = make_board(tasks=[
        {"id": "t_rev", "status": "ready", "priority": 100,
         "title": "[reviewer] PR #371 spike doc",
         "body": "review of head 5cb5574",
         "assignee": "reviewer", "key": "reviewer-pr371-5cb5574"},
    ])
    mod.board_repo = lambda s: "acme/demo"
    listing = json.dumps([
        {"number": 371, "isDraft": True,
         "headRefName": "hermes/spike-tool-health",
         "headRefOid": "5cb5574" + "a" * 33,
         "baseRefName": "develop/2.x"},
        {"number": 372, "isDraft": True,
         "headRefName": "hermes/tool-health",
         "headRefOid": "6dc0ffee" + "b" * 32,
         "baseRefName": "develop/2.x"},
    ])
    real_run = mod.run

    def _run_f(cmd, timeout=120):
        """gh pr list -> scenario; kanban comment/archive is applied to the
        test database."""
        if cmd[:2] == ["gh", "pr"] and "list" in cmd:
            return listing
        if len(cmd) > 5 and cmd[1] == "kanban":
            action, tid = cmd[4], cmd[5]
            con = sqlite3.connect(db, timeout=10)
            try:
                if action == "comment":
                    con.execute(
                        "insert into task_comments (task_id, body,"
                        " created_at) values (?,?,?)",
                        (tid, " ".join(cmd[6:]), int(time.time())))
                elif action == "archive":
                    con.execute("update tasks set status='archived' "
                                "where id=?", (tid,))
                con.commit()
            finally:
                con.close()
            return "ok"
        return ""

    calls = []
    mod.run = lambda cmd, timeout=120: (calls.append(cmd),
                                        _run_f(cmd, timeout))[1]
    findings = mod.review_layer_check(slug)
    mod.run = real_run
    mod.BOARDS = old_boards
    con = sqlite3.connect(db)
    status = con.execute(
        "select status from tasks where id='t_rev'").fetchone()[0]
    marker = con.execute(
        "select count(*) from task_comments where task_id='t_rev' "
        "and body like '%draft%'").fetchone()[0]
    con.close()
    creates = [c for c in calls if len(c) > 4 and c[1] == "kanban"
               and "create" in c[:6]]
    check("F1 reviewer card of a draft PR is archived",
          status == "archived", "status %r" % status)
    check("F2 a comment about the draft is left", marker == 1,
          "markers %r" % marker)
    check("F3 no new arm happened", not creates,
          "create calls %r" % (creates[:2],))

print("\n%s" % ("ALL OK" if not FAILS else "FAILURES: %s" % ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
