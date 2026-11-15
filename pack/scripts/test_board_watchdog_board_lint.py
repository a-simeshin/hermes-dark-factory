#!/usr/bin/env python3
"""Test of the board invariant lint I1-I6 in board-watchdog.py.

Invariants:

  I1. rework / twin port / re-review under a blocked or gave_up parent:
      the link is removed, a comment goes on both cards;
  I2. the reviewer card of an open PR is p100, a rework for an owner review
      is not below p80: raised automatically;
  I3. while the ready layer p>=80 is non-empty, a card p<50 does not hold a
      slot: it goes back to todo with a comment;
  I4. ready without an assignee: left to the stock stranded_in_ready
      diagnostic, the lint must stay silent and touch nothing;
  I5. a card in a nonstandard status (completed and the like) for over an
      hour: alert;
  I6. a live verify/port/force-push/re-review card whose parents are ALL
      archived (not done) is blocked as needs_input with a comment
      "assign a successor with link";
  dry-run changes nothing.

fake_run applies edits to the test kanban.db: the lint rereads the database
after every edit, and "FIXED" must be confirmed by a real change, not by
optimism.

This file is also the harness of the serial rework and ancestor priority
tests (they import make_board, fake_run, with_board, texts and mod).

BOARD_WATCHDOG_SCRIPT overrides the script path so that mutation runs can
test a copy of the script with a mutation; the copy must sit next to
factory_config.py and the sibling scripts.

Run: python3 test_board_watchdog_board_lint.py
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

# Settings come from the environment, the Hermes home is a throwaway
# directory: the test never reads or writes a real ~/.hermes.
_HOME = tempfile.mkdtemp(prefix="wdtest-home-")
atexit.register(shutil.rmtree, _HOME, ignore_errors=True)
os.environ["HERMES_HOME"] = _HOME
os.environ.update({
    "FACTORY_REPO": "acme/widget",
    "FACTORY_REPO_PATH": "/srv/hermes/widget",
    "FACTORY_BOARD": "main",
    "FACTORY_OWNER_LOGIN": "owner-login",
    "FACTORY_OWNER_CHAT": "telegram:000000000",
    "FACTORY_BASE_BRANCHES": "develop/1.x,develop/2.x",
    "FACTORY_PILOT_EXEMPT_BRANCHES": "hermes/example-pilot-branch",
})
PILOT_BRANCH = "hermes/example-pilot-branch"

SCRIPT = os.environ.get("BOARD_WATCHDOG_SCRIPT") or os.path.join(
    HERE, "board-watchdog.py")

spec = importlib.util.spec_from_file_location("board_watchdog", SCRIPT)
if spec is None or spec.loader is None:
    print("SKIP: cannot load board-watchdog.py")
    sys.exit(0)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def make_board(tasks=(), links=(), runs=(), events=(), comments=()):
    """Mini board. tasks: id,title,status,assignee,priority,key,age."""
    tmp = tempfile.mkdtemp(prefix="wdtest-lint-")
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
    for r in runs:
        con.execute(
            "insert into task_runs (task_id, profile, status, started_at,"
            " ended_at, outcome) values (?,?,?,?,?,?)",
            (r["task_id"], r.get("profile", "coder"), r.get("status", "ended"),
             int(now - r.get("age", 7200)), int(now - r.get("age", 7200) + 60),
             r.get("outcome", "completed")))
    for e in events:
        con.execute(
            "insert into task_events (task_id, kind, created_at) values (?,?,?)",
            (e["task_id"], e["kind"], int(now - e.get("age", 3600))))
    for task_id, body in comments:
        con.execute(
            "insert into task_comments (task_id, body, created_at)"
            " values (?,?,?)", (task_id, body, int(now)))
    con.commit()
    con.close()
    old_boards = mod.BOARDS
    mod.BOARDS = tmp
    return slug, db, old_boards


def fake_run(db, calls, open_prs=(154,)):
    """gh list by scenario; kanban edits are APPLIED to the test database so
    that the lint's reread sees the real effect."""

    def _run(cmd, timeout=120):
        calls.append(cmd)
        if cmd[0] == "gh":
            if "list" in cmd:
                return json.dumps([{"number": n} for n in sorted(open_prs)])
            return ""
        if len(cmd) > 4 and cmd[1] == "kanban":
            action, tid = cmd[4], cmd[5]
            con = sqlite3.connect(db, timeout=10)
            try:
                if action == "unlink":
                    con.execute("delete from task_links where parent_id=?"
                                " and child_id=?", (tid, cmd[6]))
                elif action == "comment":
                    con.execute("insert into task_comments (task_id, body,"
                                " created_at) values (?,?,?)",
                                (tid, cmd[6], int(time.time())))
                elif action == "edit":
                    val = cmd[7]
                    con.execute("update tasks set priority=? where id=?",
                                (int(val), tid))
                elif action == "move":
                    con.execute("update tasks set status=? where id=?",
                                (cmd[6], tid))
                elif action == "block":
                    # CLI: block tid [--kind K] reason... ; reason
                    # is appended as a comment, the card goes to blocked.
                    toks = cmd[6:]
                    kind = None
                    if "--kind" in toks:
                        i = toks.index("--kind")
                        kind = toks[i + 1] if i + 1 < len(toks) else None
                        toks = toks[:i] + toks[i + 2:]
                    con.execute("update tasks set status='blocked',"
                                " block_kind=? where id=?", (kind, tid))
                    con.execute("insert into task_comments (task_id, body,"
                                " created_at) values (?,?,?)",
                                (tid, " ".join(toks), int(time.time())))
                con.commit()
            finally:
                con.close()
            return "ok"
        return ""

    return _run


def with_board(tasks=(), links=(), runs=(), events=(), comments=(),
               open_prs=(154,), repo="test/repo"):
    """Builds a board and patches run/board_repo. The tests are linear, so
    it returns (slug, calls, restore) instead of being a decorator."""
    slug, db, old_boards = make_board(tasks, links, runs, events, comments)
    calls = []
    old_run, old_repo = mod.run, mod.board_repo
    mod.run = fake_run(db, calls, open_prs)
    mod.board_repo = lambda s: repo

    def restore():
        mod.run, mod.BOARDS = old_run, old_boards
        mod.board_repo = old_repo
        shutil.rmtree(os.path.dirname(db), ignore_errors=True)

    return slug, calls, restore


def texts(findings):
    return "\n".join(t for _, _, t in findings)


def test_i1_blocked_parent():
    slug, calls, restore = with_board(
        tasks=[
            {"id": "t_p", "title": "coder PR #186", "status": "blocked"},
            {"id": "t_c", "title": "Rework PR #186 per owner review",
             "status": "todo", "key": "owner-review-186-a75b92e", "priority": 80},
            {"id": "t_plain", "title": "plain subtask", "status": "todo"},
            {"id": "t_twin", "title": "[coder][1.x] fix (issue #170, 1.x twin)",
             "status": "todo"},
        ],
        links=[("t_p", "t_c"), ("t_p", "t_plain"), ("t_p", "t_twin")])
    try:
        found = mod.board_lint(slug)
        txt = texts(found)
        assert "I1 FIXED" in txt and "t_c -> t_p" in txt, txt
        assert any(c[4] == "unlink" and c[5] == "t_p" and c[6] == "t_c"
                   for c in calls), calls
        # a comment on both cards
        commented = {c[5] for c in calls if c[4] == "comment"}
        assert {"t_c", "t_p"} <= commented, calls
        # the twin port under the same parent is unlinked too
        assert any(c[4] == "unlink" and c[6] == "t_twin" for c in calls), calls
        assert "t_twin" in txt, txt
        # the plain subtask is untouched
        assert not any(c[4] == "unlink" and c[6] == "t_plain" for c in calls)
        # the link is really gone from the database
        con = sqlite3.connect(os.path.join(mod.BOARDS, slug, "kanban.db"))
        left = con.execute("select count(*) from task_links").fetchone()[0]
        con.close()
        assert left == 1, left       # only t_p -> t_plain is left
    finally:
        restore()
    print("OK test_i1_blocked_parent")


def test_i1_gave_up_parent():
    slug, calls, restore = with_board(
        tasks=[
            {"id": "t_gu", "title": "coder, gave up", "status": "todo"},
            {"id": "t_rev", "title": "[reviewer] PR #154", "status": "todo",
             "key": "reviewer-pr154-ff6dcdf", "priority": 100},
        ],
        links=[("t_gu", "t_rev")],
        runs=[{"task_id": "t_gu", "outcome": "gave_up"}])
    try:
        found = mod.board_lint(slug)
        txt = texts(found)
        assert "I1 FIXED" in txt and "re-review" in txt, txt
        assert any(c[4] == "unlink" and c[6] == "t_rev" for c in calls), calls
    finally:
        restore()
    print("OK test_i1_gave_up_parent")


def test_i2_reviewer_priority():
    slug, calls, restore = with_board(
        tasks=[
            {"id": "t_r154", "title": "[reviewer] PR #154", "status": "ready",
             "key": "reviewer-pr154-ff6dcdf", "priority": 40, "assignee": "reviewer"},
            # review of the closed PR #999: the threshold does not apply
            {"id": "t_r999", "title": "[reviewer] PR #999", "status": "ready",
             "key": "reviewer-pr999-abababa", "priority": 40, "assignee": "reviewer"},
        ])
    try:
        found = mod.board_lint(slug)
        txt = texts(found)
        assert "I2 FIXED" in txt and "t_r154" in txt and "p40 -> p100" in txt, txt
        assert any(c[4] == "edit" and c[5] == "t_r154" and c[7] == "100"
                   for c in calls), calls
        assert not any(c[5] == "t_r999" for c in calls if c[4] == "edit"), calls
        con = sqlite3.connect(os.path.join(mod.BOARDS, slug, "kanban.db"))
        p = con.execute("select priority from tasks where id='t_r154'").fetchone()[0]
        con.close()
        assert p == 100, p
    finally:
        restore()
    print("OK test_i2_reviewer_priority")


def test_i2_rework_floor():
    slug, calls, restore = with_board(
        tasks=[
            {"id": "t_rw", "title": "Rework PR #223 per owner review",
             "status": "todo", "key": "owner-review-223-371f50f", "priority": 0},
            {"id": "t_ok", "title": "Rework PR #154, already high",
             "status": "todo", "key": "owner-review-154-ff6dcdf", "priority": 80},
        ])
    try:
        found = mod.board_lint(slug)
        txt = texts(found)
        assert "I2 FIXED" in txt and "t_rw" in txt and "p0 -> p80" in txt, txt
        assert not any(c[5] == "t_ok" for c in calls if c[4] == "edit"), calls
    finally:
        restore()
    print("OK test_i2_rework_floor")


def test_i2_architect_followup_gets_floor_not_ceiling():
    # An architect follow-up with the key reviewer-pr188-<slug> is a rework
    # per owner review (floor p80), not a review card (p100).
    slug, calls, restore = with_board(
        tasks=[
            {"id": "t_arch", "title": "[2.x] Owner decision: teardown",
             "status": "todo", "assignee": "architect", "priority": 0,
             "key": "reviewer-pr188-unconditional-send-teardown"},
        ])
    try:
        found = mod.board_lint(slug)
        txt = texts(found)
        assert "t_arch" in txt and "p0 -> p80" in txt, txt
        assert "p100" not in txt, txt
    finally:
        restore()
    print("OK test_i2_architect_followup_gets_floor_not_ceiling")


def test_i2_architect_followup_open_pr_floor_not_ceiling():
    # Same architect follow-up, but PR #188 is OPEN: the p100 branch must
    # require assignee=reviewer, otherwise the architect card is raised to
    # p100 and chokes the review queue. PR #188 must be in open_prs, else
    # the p100 branch never reaches the assignee check.
    slug, calls, restore = with_board(
        tasks=[
            {"id": "t_arch", "title": "[2.x] Owner decision: teardown",
             "status": "todo", "assignee": "architect", "priority": 0,
             "key": "reviewer-pr188-unconditional-send-teardown"},
            # control: the real review card of the same PR is raised
            {"id": "t_rev", "title": "[reviewer] PR #188", "status": "ready",
             "assignee": "reviewer", "priority": 40,
             "key": "reviewer-pr188-00c0ffee"},
        ], open_prs=(188,))
    try:
        found = mod.board_lint(slug)
        txt = texts(found)
        assert "t_rev" in txt and "p40 -> p100" in txt, txt
        assert "t_arch" in txt and "p0 -> p80" in txt, txt
        assert "p0 -> p100" not in txt, txt
        con = sqlite3.connect(os.path.join(mod.BOARDS, slug, "kanban.db"))
        pa = con.execute(
            "select priority from tasks where id='t_arch'").fetchone()[0]
        con.close()
        assert pa == 80, pa
    finally:
        restore()
    print("OK test_i2_architect_followup_open_pr_floor_not_ceiling")


def test_i1_unremovable_link_reported():
    # The recheck by rereading the database must catch an unlink that did
    # NOT work (the CLI answered ok, the link stayed): without the alert
    # "I1 NOT FIXED" the watchdog would report "fixed" on a live violation.
    # A mutant `if False:` instead of `if still:` is caught here.
    slug, db, old_boards = make_board(
        tasks=[{"id": "t_p", "title": "coder PR #186", "status": "blocked"},
               {"id": "t_c", "title": "Rework PR #186 per owner review",
                "status": "todo", "key": "owner-review-186-a75b92e",
                "priority": 80}],
        links=[("t_p", "t_c")])
    calls = []
    old_run, old_repo = mod.run, mod.board_repo

    def _run(cmd, timeout=120):
        calls.append(cmd)
        if cmd[0] == "gh":
            if "list" in cmd:
                return json.dumps([{"number": 154}])
            return ""
        if len(cmd) > 4 and cmd[1] == "kanban":
            action, tid = cmd[4], cmd[5]
            if action == "comment":
                con = sqlite3.connect(db, timeout=10)
                con.execute(
                    "insert into task_comments (task_id, body, created_at)"
                    " values (?,?,?)", (tid, cmd[6], int(time.time())))
                con.commit()
                con.close()
            # unlink and edit "succeed" but the database does not change:
            # a silently broken CLI or a re-created link.
            return "ok"
        return ""

    mod.run = _run
    mod.board_repo = lambda s: "test/repo"
    try:
        found = mod.board_lint(slug)
        txt = texts(found)
        assert "I1 NOT FIXED" in txt, txt
        assert "I1 FIXED" not in txt, txt
        con = sqlite3.connect(os.path.join(mod.BOARDS, slug, "kanban.db"))
        n = con.execute("select count(*) from task_links").fetchone()[0]
        con.close()
        assert n == 1, n
    finally:
        mod.run, mod.BOARDS = old_run, old_boards
        mod.board_repo = old_repo
        shutil.rmtree(os.path.dirname(db), ignore_errors=True)
    print("OK test_i1_unremovable_link_reported")


def test_i3_running_under_high_layer():
    slug, calls, restore = with_board(
        tasks=[
            {"id": "t_hi", "title": "[reviewer] PR #154", "status": "ready",
             "key": "reviewer-pr154-ff6dcdf", "priority": 100,
             "assignee": "reviewer"},
            {"id": "t_lo", "title": "background task", "status": "running",
             "priority": 40},
        ])
    try:
        found = mod.board_lint(slug)
        txt = texts(found)
        assert "I3 FIXED" in txt and "t_lo" in txt, txt
        assert any(c[4] == "move" and c[5] == "t_lo" and c[6] == "todo"
                   for c in calls), calls
        assert any(c[4] == "comment" and c[5] == "t_lo"
                   and "Watchdog lint I3" in c[6] for c in calls), calls
        con = sqlite3.connect(os.path.join(mod.BOARDS, slug, "kanban.db"))
        st = con.execute("select status from tasks where id='t_lo'").fetchone()[0]
        con.close()
        assert st == "todo", st
    finally:
        restore()
    print("OK test_i3_running_under_high_layer")


def test_i3_no_high_layer_no_action():
    slug, calls, restore = with_board(
        tasks=[{"id": "t_lo", "title": "background task", "status": "running",
                "priority": 40}])
    try:
        found = mod.board_lint(slug)
        assert "I3" not in texts(found), texts(found)
        assert not any(c[4] == "move" for c in calls), calls
    finally:
        restore()
    print("OK test_i3_no_high_layer_no_action")


def test_i4_unassigned_ready():
    # The own I4 detector is gone (the stock stranded_in_ready covers it):
    # board_lint must not fail and must touch nothing.
    slug, calls, restore = with_board(
        tasks=[{"id": "t_u", "title": "ready without an assignee",
                "status": "ready", "assignee": "", "age": 2 * 3600}])
    try:
        found = mod.board_lint(slug)
        assert not any(c[4] in ("move", "unlink", "edit", "comment")
                       for c in calls), calls
    finally:
        restore()
    print("OK test_i4_unassigned_ready")


def test_i4_holdout_card_boundary():
    """A holdout cron card without --assignee: board_lint stays silent and
    touches nothing at the threshold boundary (the stock stranded_in_ready
    diagnostic covers it)."""
    slug, calls, restore = with_board(
        tasks=[
            {"id": "t_hold29", "title": "holdout FAIL card, 29 min",
             "status": "ready", "assignee": None, "age": 29 * 60,
             "key": "holdout-154-a649921", "created_by": "user"},
            {"id": "t_hold31", "title": "holdout FAIL card, 31 min",
             "status": "ready", "assignee": None, "age": 31 * 60,
             "key": "holdout-189-af185b9", "created_by": "user"},
        ])
    try:
        found = mod.board_lint(slug)
        assert not any(c[4] in ("move", "unlink", "edit", "comment")
                       for c in calls), calls
    finally:
        restore()
    print("OK test_i4_holdout_card_boundary")


def test_i5_nonstandard_status():
    slug, calls, restore = with_board(
        tasks=[
            {"id": "t_old", "title": "stuck in completed", "status": "completed",
             "age": 2 * 3600},
            {"id": "t_fresh", "title": "just completed", "status": "completed",
             "age": 30 * 60},
        ])
    try:
        found = mod.board_lint(slug)
        txt = texts(found)
        assert "I5" in txt and "t_old" in txt and "completed" in txt, txt
        assert "t_fresh" not in txt, txt
    finally:
        restore()
    print("OK test_i5_nonstandard_status")


def test_dry_run_touches_nothing():
    slug, calls, restore = with_board(
        tasks=[
            {"id": "t_p", "title": "parent", "status": "blocked"},
            {"id": "t_c", "title": "Rework PR #186", "status": "todo",
             "key": "owner-review-186-a75b92e", "priority": 80},
            {"id": "t_rw", "title": "Rework PR #223", "status": "todo",
             "key": "owner-review-223-371f50f", "priority": 0},
        ],
        links=[("t_p", "t_c")])
    try:
        found = mod.board_lint(slug, dry_run=True)
        txt = texts(found)
        assert "I1 (dry-run)" in txt and "t_c -> t_p" in txt, txt
        assert "I2 (dry-run)" in txt and "t_rw" in txt, txt
        kanban_calls = [c for c in calls if c[0] == mod.HERMES]
        assert not kanban_calls, kanban_calls   # not a single edit
        con = sqlite3.connect(os.path.join(mod.BOARDS, slug, "kanban.db"))
        n = con.execute("select count(*) from task_links").fetchone()[0]
        p = con.execute("select priority from tasks where id='t_rw'").fetchone()[0]
        con.close()
        assert n == 1 and p == 0, (n, p)
    finally:
        restore()
    print("OK test_dry_run_touches_nothing")


def test_lint_card_kind():
    assert mod.lint_card_kind("Rework PR #186", "owner-review-186-a75b92e") == "rework"
    assert mod.lint_card_kind("[reviewer] PR #154", "reviewer-pr154-ff6dcdf") == "re-review"
    assert mod.lint_card_kind("[coder][1.x] fix (issue #170, 1.x twin)", None) == "twin-port"
    assert mod.lint_card_kind("Port NOTICE.d to develop/1.x", None) == "twin-port"
    # rebase kind: key conventions + title; it does not override
    # rework/twin/re-review, and verify/force-push "rebased" is not a rebase
    assert mod.lint_card_kind("Rebase PR #241", "rebase-241-42424eb") == "rebase"
    assert mod.lint_card_kind("Rebase PR #241", "coder-pr241-rebase-42424eb") == "rebase"
    assert mod.lint_card_kind("rebase PR #224 onto the new head", None) == "rebase"
    assert mod.lint_card_kind("Adversarially verify rebased #224 branch", None) is None
    assert mod.lint_card_kind("Rework PR #198 with rebase in the text", None) == "rework"
    assert mod.lint_card_kind("plain task", None) is None
    print("OK test_lint_card_kind")


def test_lint_i6_kind():
    # order: key before title, force-push before verify
    assert mod.lint_i6_kind("Adversarially verify rebased #224 branch"
                            " meets acceptance criteria", None) == "verify"
    assert mod.lint_i6_kind("Force-push rebased #224 and update PR comment",
                            None) == "force-push"
    assert mod.lint_i6_kind("[coder][1.x] fix (issue #170, 1.x twin)",
                            None) == "port"
    assert mod.lint_i6_kind("Port NOTICE.d to develop/1.x", None) == "port"
    assert mod.lint_i6_kind("[reviewer] PR #154",
                            "reviewer-pr154-ff6dcdf") == "re-review"
    # a rebase card is self-sufficient, I6 does not touch it
    assert mod.lint_i6_kind("rebase PR #224 onto develop/2.x",
                            "rebase-224-06e1b0a") is None
    assert mod.lint_i6_kind("plain task", None) is None
    print("OK test_lint_i6_kind")


def test_i6_archived_parent_blocked():
    # A verify card whose only parent is archived must be blocked as
    # needs_input, not left to the dispatcher.
    slug, calls, restore = with_board(
        tasks=[
            {"id": "t_arch", "title": "Rebase #224 onto rebased #223 head",
             "status": "archived"},
            {"id": "t_v", "title": "Adversarially verify rebased #224"
                                   " branch meets acceptance criteria",
             "status": "ready", "assignee": "tester"},
        ],
        links=[("t_arch", "t_v")])
    try:
        found = mod.board_lint(slug)
        txt = texts(found)
        assert "I6 FIXED" in txt and "t_v" in txt and "verify" in txt, txt
        assert "t_arch" in txt, txt
        assert any(c[4] == "block" and c[5] == "t_v"
                   and c[c.index("--kind") + 1] == "needs_input"
                   if "--kind" in c else False for c in calls), calls
        con = sqlite3.connect(os.path.join(mod.BOARDS, slug, "kanban.db"))
        st, bk = con.execute(
            "select status, block_kind from tasks where id='t_v'").fetchone()
        body = con.execute(
            "select body from task_comments where task_id='t_v'"
        ).fetchone()[0]
        con.close()
        assert st == "blocked" and bk == "needs_input", (st, bk)
        assert "Watchdog lint I6" in body and "assign a successor with link" in body, body
    finally:
        restore()
    print("OK test_i6_archived_parent_blocked")


def test_i6_done_parent_released_legitimately():
    # A parent in done releases the child legitimately: I6 stays silent.
    slug, calls, restore = with_board(
        tasks=[
            {"id": "t_done", "title": "rebase PR #224 onto develop/2.x",
             "status": "done"},
            {"id": "t_v", "title": "Adversarially verify rebased #224",
             "status": "ready", "assignee": "tester"},
        ],
        links=[("t_done", "t_v")])
    try:
        found = mod.board_lint(slug)
        txt = texts(found)
        assert "I6" not in txt, txt
        assert not any(c[4] == "block" for c in calls), calls
    finally:
        restore()
    print("OK test_i6_done_parent_released_legitimately")


def test_i6_mixed_and_no_parents():
    # a live parent among archived ones: no release happened, I6 is silent;
    # a card with no parents at all: blocking every verify is not allowed.
    slug, calls, restore = with_board(
        tasks=[
            {"id": "t_arch", "title": "old parent", "status": "archived"},
            {"id": "t_live", "title": "rebase PR #224 (successor)",
             "status": "ready"},
            {"id": "t_v1", "title": "Adversarially verify rebased #224",
             "status": "ready", "assignee": "tester"},
            {"id": "t_v2", "title": "Adversarially verify rebased #224",
             "status": "ready", "assignee": "tester"},
        ],
        links=[("t_arch", "t_v1"), ("t_live", "t_v1")])
    try:
        found = mod.board_lint(slug)
        txt = texts(found)
        assert "I6" not in txt, txt
        assert not any(c[4] == "block" for c in calls), calls
    finally:
        restore()
    print("OK test_i6_mixed_and_no_parents")


def test_i6_already_blocked_skipped():
    # A repeated block after unblock sends the card to triage: an already
    # blocked I6 violator is left alone.
    slug, calls, restore = with_board(
        tasks=[
            {"id": "t_arch", "title": "old parent", "status": "archived"},
            {"id": "t_v", "title": "Adversarially verify rebased #224",
             "status": "blocked", "assignee": "tester"},
        ],
        links=[("t_arch", "t_v")])
    try:
        found = mod.board_lint(slug)
        txt = texts(found)
        assert "I6" not in txt, txt
        assert not any(c[4] == "block" for c in calls), calls
    finally:
        restore()
    print("OK test_i6_already_blocked_skipped")


def test_i6_scheduled_alert_only():
    # scheduled is a parking, a block would break it: alert without an edit.
    slug, calls, restore = with_board(
        tasks=[
            {"id": "t_arch", "title": "old parent", "status": "archived"},
            {"id": "t_v", "title": "Adversarially verify rebased #224",
             "status": "scheduled", "assignee": "tester"},
        ],
        links=[("t_arch", "t_v")])
    try:
        found = mod.board_lint(slug)
        txt = texts(found)
        assert "I6:" in txt and "no block" in txt and "t_v" in txt, txt
        assert not any(c[4] == "block" for c in calls), calls
    finally:
        restore()
    print("OK test_i6_scheduled_alert_only")


def test_i6_dry_run_touches_nothing():
    slug, calls, restore = with_board(
        tasks=[
            {"id": "t_arch", "title": "old parent", "status": "archived"},
            {"id": "t_v", "title": "Adversarially verify rebased #224",
             "status": "ready", "assignee": "tester"},
        ],
        links=[("t_arch", "t_v")])
    try:
        found = mod.board_lint(slug, dry_run=True)
        txt = texts(found)
        assert "I6 (dry-run)" in txt and "t_v" in txt, txt
        kanban_calls = [c for c in calls if c[0] == mod.HERMES]
        assert not kanban_calls, kanban_calls
        con = sqlite3.connect(os.path.join(mod.BOARDS, slug, "kanban.db"))
        st = con.execute("select status from tasks where id='t_v'").fetchone()
        con.close()
        assert st == ("ready",), st
    finally:
        restore()
    print("OK test_i6_dry_run_touches_nothing")


def test_i6_unblockable_reported():
    # block "succeeds" (the CLI said ok) but the card is not blocked in the
    # database: a "fixed" report without a reread would be a lie.
    slug, db, old_boards = make_board(
        tasks=[{"id": "t_arch", "title": "old parent", "status": "archived"},
               {"id": "t_v", "title": "Adversarially verify rebased #224",
                "status": "ready", "assignee": "tester"}],
        links=[("t_arch", "t_v")])
    calls = []
    old_run, old_repo = mod.run, mod.board_repo

    def _run(cmd, timeout=120):
        calls.append(cmd)
        if cmd[0] == "gh":
            if "list" in cmd:
                return json.dumps([{"number": 154}])
            return ""
        if len(cmd) > 4 and cmd[1] == "kanban" and cmd[4] == "comment":
            con = sqlite3.connect(db, timeout=10)
            con.execute("insert into task_comments (task_id, body, created_at)"
                        " values (?,?,?)", (cmd[5], cmd[6], int(time.time())))
            con.commit()
            con.close()
        return "ok"   # block "succeeded" but left the database untouched

    mod.run = _run
    mod.board_repo = lambda s: "test/repo"
    try:
        found = mod.board_lint(slug)
        txt = texts(found)
        assert "I6 NOT FIXED" in txt, txt
        assert "I6 FIXED" not in txt, txt
    finally:
        mod.run, mod.BOARDS = old_run, old_boards
        mod.board_repo = old_repo
        shutil.rmtree(os.path.dirname(db), ignore_errors=True)
    print("OK test_i6_unblockable_reported")


def main():
    test_lint_card_kind()
    test_lint_i6_kind()
    test_i1_blocked_parent()
    test_i1_gave_up_parent()
    test_i2_reviewer_priority()
    test_i2_rework_floor()
    test_i2_architect_followup_gets_floor_not_ceiling()
    test_i2_architect_followup_open_pr_floor_not_ceiling()
    test_i1_unremovable_link_reported()
    test_i3_running_under_high_layer()
    test_i3_no_high_layer_no_action()
    test_i4_unassigned_ready()
    test_i4_holdout_card_boundary()
    test_i5_nonstandard_status()
    test_i6_archived_parent_blocked()
    test_i6_done_parent_released_legitimately()
    test_i6_mixed_and_no_parents()
    test_i6_already_blocked_skipped()
    test_i6_scheduled_alert_only()
    test_i6_dry_run_touches_nothing()
    test_i6_unblockable_reported()
    test_dry_run_touches_nothing()
    print("ALL BOARD-LINT TESTS PASSED")


if __name__ == "__main__":
    main()
