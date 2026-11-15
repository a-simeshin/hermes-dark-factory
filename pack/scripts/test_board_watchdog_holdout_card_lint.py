#!/usr/bin/env python3
"""Lint of hidden-zone paths in cards (holdout_card_lint).

Run: python3 test_board_watchdog_holdout_card_lint.py
Mutants: BOARD_WATCHDOG_SCRIPT=/tmp/mutant.py python3 test_board_watchdog_holdout_card_lint.py

Why: a holdout run of the tester created coder cards with evidence paths in
the body (cache/holdout/report-*.md, cache/scratch/rN), and the coder went
and read the hidden scenarios by them. The lint scans live coder/reviewer
cards: the body is cleaned through kanban edit, a comment is edited in the
database (the CLI cannot edit comments), and the watchdog adds a comment to
the card. done/archived cards are left alone: workers are not dispatched
there and the owner keeps the history as evidence. fake_run applies the
edits to the test database, so "cleaned" is confirmed by re-reading the
database, not by the watchdog's optimism.
"""
import atexit
import importlib.util
import os
import shutil
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
_HOME = tempfile.mkdtemp(prefix="wd-home-")
atexit.register(shutil.rmtree, _HOME, True)
os.environ.update({
    "HERMES_HOME": _HOME,
    "FACTORY_REPO": "acme/widget",
    "FACTORY_REPO_PATH": "/srv/hermes/widget",
    "FACTORY_BOARD": "main",
    "FACTORY_OWNER_LOGIN": "owner-login",
    "FACTORY_OWNER_CHAT": "telegram:000000000",
})
SCRIPT = os.environ.get("BOARD_WATCHDOG_SCRIPT",
                        os.path.join(HERE, "board-watchdog.py"))
FAILED = []

spec = importlib.util.spec_from_file_location("wd26_test", SCRIPT)
assert spec is not None and spec.loader is not None, SCRIPT
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def check(name, cond, detail=""):
    if cond:
        print("  ok %s" % name)
    else:
        FAILED.append(name)
        print("  FAIL %s %s" % (name, detail))


def make_board(tasks=(), comments=()):
    """A mini board. tasks: dict(id, assignee, status, body); comments:
    (task_id, author, body)."""
    tmp = tempfile.mkdtemp(prefix="wd26-")
    slug = "testboard26"
    os.makedirs(os.path.join(tmp, slug), exist_ok=True)
    db = os.path.join(tmp, slug, "kanban.db")
    con = sqlite3.connect(db)
    con.executescript("""
        create table tasks (id text primary key, title text,
            body text default '', assignee text default 'coder',
            status text default 'todo', priority int default 0,
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
        create table task_comments (id integer primary key,
            task_id text, author text, body text, created_at int);
    """)
    now = time.time()
    for t in tasks:
        con.execute(
            "insert into tasks (id, title, body, assignee, status,"
            " created_at) values (?,?,?,?,?,?)",
            (t["id"], t.get("title", "t " + t["id"]), t.get("body", ""),
             t.get("assignee", "coder"), t.get("status", "todo"), int(now)))
    for task_id, author, body in comments:
        con.execute(
            "insert into task_comments (task_id, author, body, created_at)"
            " values (?,?,?,?)", (task_id, author, body, int(now)))
    con.commit()
    con.close()
    old = mod.BOARDS
    mod.BOARDS = tmp
    return slug, db, old


def fake_run(db, calls):
    """APPLIES kanban edits to the test database: the lint re-reads the
    database after an edit, and the cleanup is confirmed by the real state."""

    def _run(cmd, timeout=120):
        calls.append(cmd)
        if len(cmd) > 5 and cmd[1] == "kanban":
            action, tid = cmd[4], cmd[5]
            con = sqlite3.connect(db, timeout=10)
            try:
                if action == "edit" and "--body" in cmd:
                    con.execute("update tasks set body=? where id=?",
                                (cmd[cmd.index("--body") + 1], tid))
                elif action == "comment":
                    con.execute(
                        "insert into task_comments (task_id, author, body,"
                        " created_at) values (?,?,?,?)",
                        (tid, "watchdog", cmd[6], int(time.time())))
                con.commit()
            finally:
                con.close()
        return ""
    return _run


def bodies(db):
    con = sqlite3.connect(db)
    out = dict(con.execute("select id, body from tasks"))
    con.close()
    return out


def comment_bodies(db):
    con = sqlite3.connect(db)
    out = {cid: (b, a) for cid, b, a in con.execute(
        "select id, body, author from task_comments")}
    con.close()
    return out


DIRTY_BODY = ("Symptom: reconnect drops the session. Evidence and logs: "
              "/srv/hermes/.hermes/cache/holdout/report-2026-10-30.md"
              " (section 21d), /srv/hermes/.hermes/cache/scratch/r21/"
              "mock189d.jsonl. FIX in one place.")
DIRTY_COMMENT = ("verdict on the screenshots: "
                 "/srv/hermes/.hermes/profiles/tester/cache/scratch/"
                 "s1_sse_proxy_badge.png and the rest next to it")


def main():
    print("holdout_card_lint")
    calls = []
    slug, db, old_boards = make_board(
        tasks=[
            {"id": "t_dirty", "assignee": "coder", "status": "todo",
             "body": DIRTY_BODY},
            {"id": "t_com", "assignee": "reviewer", "status": "blocked",
             "body": "about the review of PR #189"},
            {"id": "t_tester", "assignee": "tester", "status": "todo",
             "body": "rerun the scenarios, the report goes to "
                     "/srv/hermes/.hermes/cache/holdout/latest.md"},
            {"id": "t_done", "assignee": "coder", "status": "done",
             "body": DIRTY_BODY},
            {"id": "t_own", "assignee": "coder", "status": "ready",
             "body": "draft: /srv/hermes/.hermes/profiles/coder/cache/"
                     "scratch/pr-body-v2.md and the round one "
                     "/srv/hermes/.hermes/profiles/reviewer/cache/scratch/"
                     "r177/pr_uc.diff"},
            {"id": "t_word", "assignee": "coder", "status": "ready",
             "title": "[holdout-189-af185b9] mergeRequestHeaders",
             "body": "body without paths, only the word holdout"},
        ],
        comments=[("t_com", "tester", DIRTY_COMMENT),
                  ("t_dirty", "user", "just a comment without paths")])
    try:
        out = mod.holdout_card_lint(slug, runner=fake_run(db, calls))
        keys = sorted(k for k, _, _ in out)
        check("two dirty cards found",
              keys == ["holdout-card-path:testboard26:t_com",
                       "holdout-card-path:testboard26:t_dirty"], str(keys))
        b = bodies(db)
        cb = comment_bodies(db)
        check("the body of t_dirty is cleaned in the database (re-read)",
              "report-2026-10-30" not in b["t_dirty"]
              and "cache/scratch/r21" not in b["t_dirty"]
              and "removed by the watchdog" in b["t_dirty"],
              repr(b["t_dirty"][:90]))
        check("the comment of t_com is masked in the database",
              "profiles/tester/cache" not in cb[1][0]
              and "removed by the watchdog" in cb[1][0], repr(cb[1][0][:90]))
        check("a watchdog comment with the reason is added to t_com",
              any(a == "watchdog" and "comment #1" in body
                  and "hidden zone" in body
                  for body, a in cb.values()), str(list(cb.values())))
        check("edit --body is called for t_dirty",
              any(c[4:6] == ["edit", "t_dirty"] and "--body" in c
                  for c in calls), str(calls))
        check("the tester card is untouched",
              "cache/holdout/latest.md" in b["t_tester"]
              and "holdout-card-path:testboard26:t_tester" not in keys,
              repr(b["t_tester"]))
        check("the done card is untouched (evidence is kept)",
              b["t_done"] == DIRTY_BODY, "the body changed")
        check("a profile's own scratch is not the hidden zone",
              "holdout-card-path:testboard26:t_own" not in keys, str(keys))
        check("the word holdout without a path is not a finding",
              "holdout-card-path:testboard26:t_word" not in keys, str(keys))

        # second run over a clean database: silence, no edits
        calls2 = []
        out2 = mod.holdout_card_lint(slug, runner=fake_run(db, calls2))
        check("a repeated run is silent and makes no edits",
              out2 == [] and calls2 == [], str(out2) + str(calls2))

        # dry run: a finding, the database is untouched
        slug3, db3, old3 = make_board(
            tasks=[{"id": "t_dry", "assignee": "coder", "status": "todo",
                    "body": DIRTY_BODY}])
        try:
            calls3 = []
            out3 = mod.holdout_card_lint(slug3, apply=False,
                                         runner=fake_run(db3, calls3))
            check("dry run: a finding without edits",
                  len(out3) == 1 and bodies(db3)["t_dry"] == DIRTY_BODY
                  and calls3 == [], str(out3) + str(calls3))
        finally:
            mod.BOARDS = old3

        # wiring in main(): the lint must run before the dedup of findings
        # (the loop over `found` that drops what was already pushed today)
        import inspect
        src = inspect.getsource(mod.main)
        loop = "for key, _rank, text in found:"
        check("main() calls holdout_card_lint before the findings loop",
              "holdout_card_lint(slug) + found" in src
              and loop in src
              and src.index("holdout_card_lint") < src.index(loop),
              "no integration in main")
    finally:
        mod.BOARDS = old_boards

    print()
    if FAILED:
        print("FAIL: %d" % len(FAILED))
        sys.exit(1)
    print("OK: all holdout_card_lint tests are green")


if __name__ == "__main__":
    main()
