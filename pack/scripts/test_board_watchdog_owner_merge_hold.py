#!/usr/bin/env python3
"""Close gate "remaining: owner merge" (one PR = one card) and the removal
of the arm mechanics.

One PR = one card: a PR card closed with the result "remaining: owner
merge" before the owner merged the PR used to drop out of sight, and the
WCFY and merge steps were caught up by hand. owner_merge_hold_gate keeps
the signal (an owner finding plus one comment with a release condition)
until the PR is merged. The serial rework gate is gone with the arm
mechanics.

Checks:
  G0 the serial rework mechanics are removed;
  G1/G1b an unmerged PR gives a finding that names the release condition;
  G2 a merged PR gives no finding;
  G3 exactly one signal comment is left;
  G4 a repeated pass keeps the signal and does not duplicate the comment;
  G5 a gh failure skips the pass silently;
  G6 dry-run: a finding without actions;
  G7 the finding is classified as owner.

Run: python3 test_board_watchdog_owner_merge_hold.py
Mutant run (expected to FAIL): copy board-watchdog.py next to the other
scripts, break the gate, and run
  BOARD_WATCHDOG_SCRIPT=<copy> python3 test_board_watchdog_owner_merge_hold.py
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
    "FACTORY_BASE_BRANCHES": "main",
})

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.environ.get("BOARD_WATCHDOG_SCRIPT") or os.path.join(
    HERE, "board-watchdog.py")

spec = importlib.util.spec_from_file_location("board_watchdog", SCRIPT)
if spec is None or spec.loader is None:
    print("SKIP: cannot load board-watchdog.py")
    sys.exit(0)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print("ok  %s" % name)
    else:
        FAILS.append(name)
        print("FAIL %s :: %s" % (name, detail))


def make_board(tasks=(), comments=()):
    tmp = tempfile.mkdtemp(prefix="wdtest-owner-merge-hold-")
    slug = "testboard"
    os.makedirs(os.path.join(tmp, slug), exist_ok=True)
    con = sqlite3.connect(os.path.join(tmp, slug, "kanban.db"))
    con.executescript("""
        create table tasks (id text primary key, title text,
            body text default '', assignee text default 'ops',
            status text, priority int default 0,
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
            " created_by, created_at, completed_at, result)"
            " values (?,?,?,?,?,?,?,?,?)",
            (t["id"], t.get("title", "t " + t["id"]),
             t.get("status", "done"), t.get("assignee", "coder"),
             t.get("priority", 80), "agent", int(now - 7200),
             int(now - t.get("closed_age", 3600)), t.get("result")))
    for task_id, body in comments:
        con.execute("insert into task_comments (task_id, body, created_at)"
                    " values (?,?,?)", (task_id, body, int(now)))
    con.commit()
    con.close()
    old = mod.BOARDS
    mod.BOARDS = tmp
    return slug, tmp, old


class FakeRun(object):
    def __init__(self, pr_states, calls):
        self.pr_states = pr_states
        self.calls = calls

    def __call__(self, cmd, timeout=None):
        self.calls.append(cmd)
        if cmd[0] == "gh" and "state,mergedAt" in cmd:
            n = int(cmd[cmd.index("view") + 1])
            st = self.pr_states.get(n)
            if st is None:
                raise RuntimeError("gh down")
            return json.dumps({"state": st, "mergedAt": None})
        if cmd[4] == "comment":
            return "Commented %s" % cmd[5]
        return ""


RESULT = ("Layers C1-C4 merged into the base branch. Remaining: owner merge "
          "(PR #499). Release condition: PR #499 merged by the owner.")


def main():
    # --- the arm mechanics are removed -----------------------------------
    check("G0 serial_rework_gate/_serial_card_pr are absent",
          not any(hasattr(mod, n) for n in
                  ("serial_rework_gate", "_serial_card_pr",
                   "_serial_has_path", "SERIAL_REWORK_MARKER")),
          "the serial mechanics are still there")

    pr_open = FakeRun({499: "OPEN", 500: "MERGED"}, [])
    slug, tmp, old = make_board(tasks=[
        {"id": "t_g1", "result": RESULT},
        {"id": "t_g2", "result": "Layers merged, PR #500 merged by owner.",
         "result_closed_age": 7200},
    ])
    real_run, real_repo = mod.run, mod.board_repo
    mod.run = pr_open
    mod.board_repo = lambda s: "acme/widget"
    try:
        found = mod.owner_merge_hold_gate(slug)
    finally:
        mod.BOARDS = old
    keys = [f[0] for f in found]
    check("G1 unmerged PR: finding present",
          any("t_g1" in k and "499" in k for k in keys), str(keys))
    check("G2 merged PR: no finding",
          not any("t_g2" in k for k in keys), str(keys))
    txt = " ".join(f[2] for f in found)
    check("G1b the finding names the release condition",
          "Release condition" in txt and "#499" in txt, txt[:200])
    comments = [c for c in pr_open.calls if len(c) > 4 and c[4] == "comment"]
    check("G3 one signal comment left", len(comments) == 1,
          str(comments))

    # G4 a repeated pass: the comment is deduplicated by the marker.
    pr_open2 = FakeRun({499: "OPEN", 500: "MERGED"}, [])
    slug, tmp, old = make_board(tasks=[{"id": "t_g1", "result": RESULT}],
                                comments=[("t_g1",
                                           mod.OWNER_MERGE_HOLD_MARKER
                                           + " the card was closed before the merge")])
    mod.run = pr_open2
    mod.board_repo = lambda s: "acme/widget"
    try:
        found = mod.owner_merge_hold_gate(slug)
    finally:
        mod.BOARDS = old
    check("G4 repeated pass: the signal holds, no duplicate comments",
          len(found) == 1
          and not [c for c in pr_open2.calls
                   if len(c) > 4 and c[4] == "comment"],
          str(pr_open2.calls))

    # G5 gh fails on the PR: the pass is skipped silently (merged None).
    pr_fail = FakeRun({499: None}, [])
    slug, tmp, old = make_board(tasks=[{"id": "t_g1", "result": RESULT}])
    mod.run = pr_fail
    mod.board_repo = lambda s: "acme/widget"
    try:
        found = mod.owner_merge_hold_gate(slug)
    finally:
        mod.BOARDS = old
    check("G5 gh failure: no finding, no crash", not found, str(found))

    # G6 dry-run: a finding, no actions.
    pr_dry = FakeRun({499: "OPEN"}, [])
    slug, tmp, old = make_board(tasks=[{"id": "t_g1", "result": RESULT}])
    mod.run = pr_dry
    mod.board_repo = lambda s: "acme/widget"
    try:
        found = mod.owner_merge_hold_gate(slug, apply=False)
    finally:
        mod.BOARDS = old
    check("G6 dry-run: finding without actions",
          len(found) == 1
          and not [c for c in pr_dry.calls
                   if len(c) > 4 and c[4] == "comment"], str(pr_dry.calls))

    # G7 the finding class is owner.
    check("G7 classify gives owner for owner-merge-hold",
          mod.classify_finding(
              "testboard:owner-merge-hold:t_g1:499") == "owner",
          str(mod.classify_finding(
              "testboard:owner-merge-hold:t_g1:499")))

    mod.run, mod.board_repo = real_run, real_repo
    return 1 if FAILS else 0


if __name__ == "__main__":
    rc = main()
    print("\nTOTAL owner-merge-hold: %s" % ("OK" if rc == 0 and not FAILS else "FAIL"))
    sys.exit(rc)
