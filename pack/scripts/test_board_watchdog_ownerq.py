#!/usr/bin/env python3
"""A question to the owner older than 24 h must reach the "KANBAN NEEDS
ATTENTION" report.

The stock core rule (_rule_stuck_in_blocked in hermes_cli/kanban_diagnostics.py)
resets its stale timer on ANY commented event after the block, so a plan
review gate with an open question stayed blocked for days without reaching the
report. The core is not edited (layer changes live in scripts/), so
board-watchdog.py has a layer detector owner_question_pending: blocked + the
last comment older than 24 h + no sanctioned "Release condition:".

Checks:
  A. a blocked card whose last comment (a question to the owner) is 30 h old:
     an owner finding; red on code without the detector;
  B. classify_finding puts ownerq:* into class owner (the header
     "KANBAN NEEDS ATTENTION"), checked inside A;
  C. a comment with "Release condition: ..." is a sanctioned wait, no finding;
  D. a fresh comment (2 h) means work is going on, no finding;
  E. a blocked card without comments is core territory (stuck_in_blocked),
     the layer does not duplicate it;
  F. a non-blocked card with an ancient comment: no finding.

Run: python3 test_board_watchdog_ownerq.py
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
# Isolated Hermes home and neutral project settings, set before the script is loaded.
_HOME_TMP = tempfile.mkdtemp(prefix="wd-home-")
atexit.register(shutil.rmtree, _HOME_TMP, ignore_errors=True)
os.environ.update({
    "HERMES_HOME": _HOME_TMP,
    "FACTORY_REPO": "acme/widget",
    "FACTORY_REPO_PATH": "/srv/hermes/widget",
    "FACTORY_BOARD": "main",
    "FACTORY_OWNER_LOGIN": "owner-login",
    "FACTORY_OWNER_CHAT": "telegram:000000000",
    "FACTORY_BASE_BRANCHES": "main",
})
# BOARD_WATCHDOG_SCRIPT lets a mutation run test a modified copy of the script.
SCRIPT = os.environ.get("BOARD_WATCHDOG_SCRIPT") or os.path.join(
    HERE, "board-watchdog.py")

spec = importlib.util.spec_from_file_location("board_watchdog", SCRIPT)
if spec is None or spec.loader is None:
    print("SKIP: cannot load board-watchdog.py")
    sys.exit(0)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)




def make_board(tasks=(), links=(), runs=(), events=(), comments=()):
    """Mini board. tasks: dicts with id,title,status,assignee,priority,key,age.
    Returns (slug, db path, old BOARDS). Same schema as the lint harness."""
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




FAILS = []


def check(name, cond, detail=""):
    print("%s %s%s" % ("ok" if cond else "FAIL", name,
                       (" :: " + detail) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def aged_comments(db, mapping):
    """Sets the age of the cards' comments from mapping (task_id -> hours)."""
    now = time.time()
    con = sqlite3.connect(db, timeout=10)
    for task_id, hours in mapping.items():
        con.execute("update task_comments set created_at=? where task_id=?",
                    (int(now - hours * 3600), task_id))
    con.commit()
    con.close()


def run_case(tasks, comments, ages):
    slug, db, old_boards = make_board(tasks, comments=comments)
    aged_comments(db, ages)
    return slug, db, old_boards


def case_a_live_replay():
    """A: a question to the owner unanswered for more than a day."""
    tasks = [{"id": "t_gate", "title": "Plan review gate", "status": "blocked",
              "assignee": "reviewer", "age": 13 * 24 * 3600}]
    comments = [("t_gate", "## Verdict amendment\n### Owner to decide\n"
                            "- archive the duplicates\n- priorities of the children")]
    slug, db, old_boards = run_case(tasks, comments, {"t_gate": 30})
    try:
        tasks_g, kids, blockers, last_end, waited, recent = mod.graph(slug)
        found = mod.owner_question_pending(slug, tasks_g, waited)
        keys = [f[0] for f in found]
        check("A: finding exists", "ownerq:%s:t_gate" % slug in keys,
              "keys=%s" % keys)
        check("A: class owner", mod.classify_finding(
            "ownerq:%s:t_gate" % slug) == "owner")
        txt = [f[2] for f in found
               if f[0] == "ownerq:%s:t_gate" % slug][0]
        check("A: card id in the text", "t_gate" in txt)
    except AttributeError as exc:
        check("A: the detector exists (red on the old code)", False,
              str(exc))
    finally:
        mod.BOARDS = old_boards
        shutil.rmtree(os.path.dirname(db), ignore_errors=True)


def case_c_sanctioned_wait():
    tasks = [{"id": "t_wait", "title": "wait ext", "status": "blocked",
              "assignee": "coder", "age": 5 * 24 * 3600}]
    comments = [("t_wait", "Release condition: PR #999 merged by the owner.")]
    slug, db, old_boards = run_case(tasks, comments, {"t_wait": 48})
    try:
        tasks_g, kids, blockers, last_end, waited, recent = mod.graph(slug)
        found = mod.owner_question_pending(slug, tasks_g, waited)
        check("C: a sanctioned wait is silent", not found,
              "keys=%s" % [f[0] for f in found])
    finally:
        mod.BOARDS = old_boards
        shutil.rmtree(os.path.dirname(db), ignore_errors=True)


def case_d_fresh_comment():
    tasks = [{"id": "t_hot", "title": "hot", "status": "blocked",
              "assignee": "coder", "age": 3 * 24 * 3600}]
    comments = [("t_hot", "working on it, preparing the question")]
    slug, db, old_boards = run_case(tasks, comments, {"t_hot": 2})
    try:
        tasks_g, kids, blockers, last_end, waited, recent = mod.graph(slug)
        found = mod.owner_question_pending(slug, tasks_g, waited)
        check("D: a fresh comment does not alert", not found,
              "keys=%s" % [f[0] for f in found])
    finally:
        mod.BOARDS = old_boards
        shutil.rmtree(os.path.dirname(db), ignore_errors=True)


def case_e_no_comments():
    tasks = [{"id": "t_mute", "title": "mute", "status": "blocked",
              "assignee": "coder", "age": 4 * 24 * 3600}]
    slug, db, old_boards = make_board(tasks)
    try:
        tasks_g, kids, blockers, last_end, waited, recent = mod.graph(slug)
        found = mod.owner_question_pending(slug, tasks_g, waited)
        check("E: no comments is silent (core territory)", not found,
              "keys=%s" % [f[0] for f in found])
    finally:
        mod.BOARDS = old_boards
        shutil.rmtree(os.path.dirname(db), ignore_errors=True)


def case_f_not_blocked():
    tasks = [{"id": "t_run", "title": "running", "status": "running",
              "assignee": "coder", "age": 2 * 24 * 3600}]
    comments = [("t_run", "started")]
    slug, db, old_boards = run_case(tasks, comments, {"t_run": 40})
    try:
        tasks_g, kids, blockers, last_end, waited, recent = mod.graph(slug)
        found = mod.owner_question_pending(slug, tasks_g, waited)
        check("F: a non-blocked card does not alert", not found,
              "keys=%s" % [f[0] for f in found])
    finally:
        mod.BOARDS = old_boards
        shutil.rmtree(os.path.dirname(db), ignore_errors=True)


if __name__ == "__main__":
    case_a_live_replay()
    case_c_sanctioned_wait()
    case_d_fresh_comment()
    case_e_no_comments()
    case_f_not_blocked()
    print("---")
    if FAILS:
        print("FAILED: %d" % len(FAILS))
        sys.exit(1)
    print("ALL OK")
