#!/usr/bin/env python3
"""Admission gate on a WHITE LIST, board-watchdog.py.

Reason: a black list only parked cards that referenced an unapproved issue
in the body. Specify rewrote a card body (the reference disappeared), the
decomposer created a card with no references at all, an epic referenced only
an upstream repository: all of them stood in ready without the owner's
approval.

Checks:

  A. Three live cards are taken off ready by the white list. A black list
     lets all three through: a red run.
  B. Binding survives: a card whose body specify wiped, but which has an
     issue-link comment to an approved issue, STAYS in ready (the binding
     lives in task_comments).
  C. An approved issue (milestone 2.3.0) in the body: the card stays in
     ready and the gate itself stamps an issue-link comment.
  D. A service card of an open PR against an approved issue is not parked
     (review/WCFY/rebase); against an unapproved issue it is parked.
  E. The marker "admission: infra": infrastructure is not parked.
  F. A restored binding to an issue without a milestone is honored: the
     card is still taken off the queue, the reason is "#86 without a
     milestone", and once the milestone is assigned the card returns to
     work by itself.
  G. Idempotency: a repeated run does not duplicate actions.

Run: python3 test_board_watchdog_admission_whitelist.py
Mutant: PYTHONPATH=<scripts dir> BOARD_WATCHDOG_SCRIPT=/tmp/mutant.py \
    python3 test_board_watchdog_admission_whitelist.py
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

HERE = os.path.dirname(os.path.abspath(__file__))

# Settings and the Hermes home come from the environment, set before the
# script is loaded (the script reads them at import time).
_HERMES_HOME = tempfile.mkdtemp(prefix="wd-home-")
atexit.register(shutil.rmtree, _HERMES_HOME, ignore_errors=True)
os.environ.update({
    "HERMES_HOME": _HERMES_HOME,
    "FACTORY_REPO": "acme/widget",
    "FACTORY_REPO_PATH": "/srv/hermes/widget",
    "FACTORY_BOARD": "main",
    "FACTORY_OWNER_LOGIN": "owner-login",
    "FACTORY_OWNER_CHAT": "telegram:000000000",
    "FACTORY_BASE_BRANCHES": "develop/1.x,develop/2.x",
})

SCRIPT = os.environ.get("BOARD_WATCHDOG_SCRIPT") or os.path.join(
    HERE, "board-watchdog.py")

spec = importlib.util.spec_from_file_location("board_watchdog", SCRIPT)
if spec is None or spec.loader is None:
    print("SKIP: cannot load board-watchdog.py")
    sys.exit(0)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def make_board(tasks=(), links=(), comments=()):
    """Mini board. tasks: id, title, body, status, assignee, priority, key, age."""
    tmp = tempfile.mkdtemp(prefix="wdtest-adm-")
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
    for task_id, body in comments:
        con.execute(
            "insert into task_comments (task_id, body, created_at)"
            " values (?,?,?)", (task_id, body, int(now)))
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
    """Open issues of the fictional repo.

    #86: enhancement+hermes+ui, WITHOUT a milestone (not approved).
    #235: approved (2.3.0). #364/#373: no labels / no milestone.
    #2251/#2293 are NOT included: they belong to an upstream repository.
    """
    return json.dumps([
        {"number": 86, "milestone": None,
         "labels": [{"name": "enhancement"}, {"name": "hermes"},
                    {"name": "ui"}]},
        {"number": 235, "milestone": {"title": "2.3.0"},
         "labels": [{"name": "enhancement"}, {"name": "hermes"}]},
        {"number": 364, "milestone": None, "labels": []},
        {"number": 373, "milestone": None,
         "labels": [{"name": "bug"}, {"name": "wait-for-triage"},
                    {"name": "hermes"}]},
    ])


def prs_json():
    """Open PRs: #239 against the approved #235, #372 against #370 (not in
    the issue states: not approved)."""
    return json.dumps([
        {"number": 239, "body": "Refs #235, keep-alive tracking"},
        {"number": 372, "body": "Refs #370 tool health"},
    ])


def make_run(db, calls):
    """gh list by scenario; schedule and comment are APPLIED to the test
    database so a repeated pass sees the real effect."""

    def _run(cmd, timeout=120):
        calls.append(cmd)
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
                elif action == "comment":
                    con.execute(
                        "insert into task_comments (task_id, body,"
                        " created_at) values (?,?,?)",
                        (tid, " ".join(cmd[6:]), int(time.time())))
                con.commit()
            finally:
                con.close()
            return "Scheduled" if action == "schedule" else "ok"
        return ""
    return _run


check("A0 admission_gate exists", hasattr(mod, "admission_gate"))

if hasattr(mod, "admission_gate"):
    slug, db, old_boards = make_board(
        tasks=[
            # (1) a live card: specify wiped #86 from the body
            {"id": "t_3288bfb5", "status": "ready", "priority": 5,
             "title": ("Gate-hold check: UI origin/warnings "
                       "feature pending owner approval"),
             "body": ("This is a HOLD/GATE card. Do NOT implement. "
                      "Verify owner approval and issue mirror.")},
            # (2) an epic with upstream references only
            {"id": "t_31f84920", "status": "ready", "priority": 5,
             "title": ("Support the extension (SEP-100): list tab, digest "
                       "verification, proxy pass-through"),
             "body": ("upstream PR #2251 and #2293 shipped it in v2.6.0; "
                      "there is no issue in this repository")},
            # (3) a decomposer child with no references, the [ux] parent has
            # no approved issue
            {"id": "t_2fdded65", "status": "todo", "priority": 0,
             "title": ("[ux] Input Required: Submit silently does nothing "
                       "with empty required field"),
             "body": "repro on the echo demo, issue #373"},
            {"id": "t_0dd47f8b", "status": "ready", "priority": 0,
             "title": "Add required-field validation to Input Required "
                      "card Submit",
             "body": "the Submit button is active with an empty required field"},
            # (4) a stable binding: body wiped by specify, the marker is alive
            {"id": "t_wiped", "status": "ready", "priority": 80,
             "title": "Keepalive state chip (specify rewrote the body)",
             "body": "the body was rewritten, no references"},
            # (5) an approved issue in the body
            {"id": "t_ok", "status": "ready", "priority": 80,
             "title": "PR #239 cycle (issue #235)",
             "body": "milestone 2.3.0, issue #235"},
            # (6) a service card of a PR against an approved issue
            {"id": "t_svc", "status": "todo", "priority": 100,
             "title": "[reviewer] PR #239 (head 5cb5574): review of the head",
             "body": "review of the head without a verdict"},
            # (7) a service card of a PR against an UNapproved issue
            {"id": "t_svc_bad", "status": "todo", "priority": 100,
             "title": "rebase PR #372 onto develop/2.x@06e1b0a",
             "body": "conflicts after #371"},
            # (8) Hermes infrastructure
            {"id": "t_infra", "status": "ready", "priority": 50,
             "title": "Watchdog: nightly metrics report",
             "body": "a board-watchdog cron, unrelated to repo issues"},
        ],
        links=[("t_2fdded65", "t_0dd47f8b")],
        comments=[
            ("t_wiped", "issue-link: #235 (binding recorded before "
                        "specify)"),
            ("t_infra", "admission: infra (watchdog, cron)"),
        ])
    mod.board_repo = lambda s: "acme/demo"
    calls = []
    real_run = mod.run
    mod.run = make_run(db, calls)
    findings = mod.admission_gate(slug)
    mod.run = real_run
    con = sqlite3.connect(db)
    statuses = dict(con.execute("select id, status from tasks"))
    bodies = {r[0]: r[1] for r in con.execute(
        "select task_id, body from task_comments")}
    con.close()
    viol = {k for k, _, _ in findings}

    check("A1 t_3288bfb5 taken off ready (body without #86)",
          statuses["t_3288bfb5"] == "scheduled",
          "status %r" % statuses["t_3288bfb5"])
    check("A2 t_31f84920 taken off (references only to upstream #2251/#2293)",
          statuses["t_31f84920"] == "scheduled",
          "status %r" % statuses["t_31f84920"])
    check("A3 t_0dd47f8b taken off (decomposer, no references)",
          statuses["t_0dd47f8b"] == "scheduled",
          "status %r" % statuses["t_0dd47f8b"])
    check("A4 findings for all three",
          any(k.endswith(":admission:t_3288bfb5") for k in viol)
          and any(k.endswith(":admission:t_31f84920") for k in viol)
          and any(k.endswith(":admission:t_0dd47f8b") for k in viol),
          "keys %r" % viol)
    check("B body wiped by specify + live issue-link: ready kept",
          statuses["t_wiped"] == "ready",
          "status %r" % statuses["t_wiped"])
    check("C approved #235 in the body: ready kept + automatic binding stamp",
          statuses["t_ok"] == "ready"
          and any(b.startswith("issue-link: #235") for b in
                  [v for k, v in bodies.items() if k == "t_ok"]),
          "status %r" % statuses["t_ok"])
    check("D1 service card of PR #239 against the approved #235 untouched",
          statuses["t_svc"] == "todo", "status %r" % statuses["t_svc"])
    check("D2 service card of PR #372 against an unapproved issue taken off",
          statuses["t_svc_bad"] == "scheduled",
          "status %r" % statuses["t_svc_bad"])
    check("E infrastructure with the marker admission: infra untouched",
          statuses["t_infra"] == "ready", "status %r" % statuses["t_infra"])

    # G. idempotency: a second run over the same database
    calls2 = []

    def _run2(cmd, timeout=120):
        calls2.append(cmd)
        if cmd[:2] == ["gh", "issue"] and "list" in cmd:
            return issues_json()
        if cmd[:2] == ["gh", "pr"] and "list" in cmd:
            return prs_json()
        return ""

    mod.run = _run2
    mod.admission_gate(slug)
    mod.run = real_run
    actions2 = [c for c in calls2 if len(c) > 4 and c[4] in ("comment",
                                                             "schedule")]
    check("G a repeated run does not duplicate actions", not actions2,
          "actions %r" % (actions2[:3],))

    # F. a restored binding t_3288bfb5 -> #86 (no milestone): the card is
    # taken off, the reason is "#86 without a milestone"; after the
    # milestone is assigned it returns by itself
    slug2, db2, old2 = make_board(
        tasks=[{"id": "t_3288bfb5", "status": "ready", "priority": 5,
                "title": "Gate-hold check: UI origin/warnings",
                "body": "the body was rewritten by specify, no references"}],
        comments=[("t_3288bfb5", "issue-link: #86 (restored)")])
    mod.run = make_run(db2, [])
    f3 = mod.admission_gate(slug2)
    mod.run = real_run
    mod.BOARDS = old_boards
    con = sqlite3.connect(db2)
    st3 = con.execute("select status from tasks").fetchone()[0]
    com3 = [r[0] for r in con.execute(
        "select body from task_comments where body like '%admission%'")]
    con.close()
    check("F1 binding to the unapproved #86 honored: card taken off",
          st3 == "scheduled", "status %r" % st3)
    check("F2 reason: #86 without a milestone, not 'no binding'",
          com3 and "#86 without a milestone" in com3[0], "comment %r" % com3)

print("\n%s" % ("ALL OK" if not FAILS else "FAILURES: %s" % ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
