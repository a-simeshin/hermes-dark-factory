#!/usr/bin/env python3
"""The review-lane repair gate reassigns a handoff that skipped reviewer=.

The native kanban_request_review called WITHOUT reviewer= does not reassign
the card: the assignee stays the executor, the dispatcher hands the review
claim back to the coder, and the coder closes the delivery with
kanban_complete without a verdict. review_lane_repair_gate finishes the
routing: a native reassign to the reviewer plus one marker comment.

Checks:
  L1 a scheduled coder card whose last review_requested has reviewer=null:
     one reassign of that card to the reviewer, one marker comment, a
     severity-0 finding;
  L2 a live reviewer card keyed reviewer-pr<N>-... for the PR in the title
     (structural owner): no calls, an "owned" finding;
  L3 review_requested with reviewer set: silent;
  L4 changes_requested after the handoff: silent;
  L5 the marker comment already stands: silent (idempotent);
  L6 a running card: not touched;
  L7 the reassign fails (empty CLI output): severity 1, no comment;
  L8 the gate is registered in the hourly pass and in --rework-gate-only.
The checks run on the real script (all must pass) and then on mutants of
the gate lines (each must be killed by at least one check).

Run: python3 test_board_watchdog_review_lane_repair.py
"""

import json
import os
import sqlite3
import sys
import tempfile
import time
import types

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
BW_PATH = os.path.join(HERE, "board-watchdog.py")

MUTANTS = {
    "reviewer-field-ignored": (
        "            if isinstance(reviewer, str) and reviewer.strip():\n"
        "                continue  # the native routing happened",
        "            if False:\n"
        "                continue  # the native routing happened"),
    "later-verdict-ignored": (
        "            if cr:\n"
        "                continue  # a verdict already returned",
        "            if False:\n"
        "                continue  # a verdict already returned"),
    "marker-ignored": (
        "            if _card_commented(db, tid, REVIEW_LANE_REPAIR_MARKER):",
        "            if False:"),
    "wrong-reassign-target": (
        "                        \"reassign\", tid, \"reviewer\",\n"
        "                        \"--reason\",\n"
        "                        \"review_requested without reviewer",
        "                        \"reassign\", tid, assignee,\n"
        "                        \"--reason\",\n"
        "                        \"review_requested without reviewer"),
    "structural-owner-ignored": (
        "            if owner is not None:\n"
        "                findings.append((\n"
        "                    \"reviewlane:%s:%s:owned\"",
        "            if False:\n"
        "                findings.append((\n"
        "                    \"reviewlane:%s:%s:owned\""),
    "running-touched": (
        "            \"WHERE status IN ('review', 'scheduled', 'ready', "
        "'blocked') \"\n"
        "            \"AND assignee IS NOT NULL AND assignee != 'reviewer'\"",
        "            \"WHERE status IN ('review', 'scheduled', 'ready', "
        "'blocked', 'running') \"\n"
        "            \"AND assignee IS NOT NULL AND assignee != 'reviewer'\""),
    "not-registered-hourly": (
        "        found.extend(review_lane_repair_gate(slug))",
        "        pass"),
    "not-registered-fast": (
        "                for _, _, text in review_lane_repair_gate(slug):",
        "                for _, _, text in []:"),
}

SCHEMA = """
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
"""


def load_module(src):
    mod = types.ModuleType("bw_review_lane_repair")
    mod.__file__ = BW_PATH
    exec(compile(src, BW_PATH, "exec"), mod.__dict__)
    mod.SRC = src
    return mod


def handoff(reviewer):
    return {"summary": "rework", "implementer": "coder", "reviewer": reviewer}


def make_board(mod, tasks, events, comments=()):
    """tasks: dicts (id, status, assignee, title, key); events:
    (task_id, kind, payload) in insertion order (ids grow)."""
    tmp = tempfile.mkdtemp(prefix="wdtest-review-lane-")
    slug = "testboard"
    os.makedirs(os.path.join(tmp, slug), exist_ok=True)
    con = sqlite3.connect(os.path.join(tmp, slug, "kanban.db"))
    con.executescript(SCHEMA)
    now = int(time.time())
    for t in tasks:
        con.execute(
            "insert into tasks (id, title, status, assignee, priority,"
            " created_by, created_at, idempotency_key)"
            " values (?, ?, ?, ?, 90, 'user', ?, ?)",
            (t["id"], t.get("title", "card " + t["id"]), t["status"],
             t.get("assignee", "coder"), now, t.get("key")))
    for tid, kind, payload in events:
        con.execute(
            "insert into task_events (task_id, kind, payload, created_at)"
            " values (?, ?, ?, ?)",
            (tid, kind, json.dumps(payload) if payload is not None else None,
             now))
    for tid, body in comments:
        con.execute("insert into task_comments (task_id, body, created_at)"
                    " values (?, ?, ?)", (tid, body, now))
    con.commit()
    con.close()
    mod.BOARDS = tmp
    return slug


class FakeRun(object):
    """The kanban CLI: reassign answers reassign_out (empty = refused)."""

    def __init__(self, reassign_out="Reassigned"):
        self.reassign_out = reassign_out
        self.calls = []

    def __call__(self, cmd, timeout=None):
        self.calls.append(cmd)
        if len(cmd) > 4 and cmd[4] == "reassign":
            return self.reassign_out
        return "ok"

    def of(self, verb):
        return [c for c in self.calls if len(c) > 4 and c[4] == verb]


def gate(mod, tasks, events, comments=(), reassign_out="Reassigned"):
    fake = FakeRun(reassign_out)
    slug = make_board(mod, tasks, events, comments)
    mod.run = fake
    return mod.review_lane_repair_gate(slug, apply=True), fake


def checks(mod):
    fails = []

    def check(name, cond, detail=""):
        if not cond:
            fails.append("%s :: %s" % (name, detail))

    bad = {"id": "t_bad", "status": "scheduled"}
    bad_ev = [("t_bad", "review_requested", handoff(None))]

    found, fake = gate(mod, [bad], bad_ev)
    re_ = fake.of("reassign")
    check("L1 one reassign of the card to the reviewer",
          len(re_) == 1 and re_[0][5:7] == ["t_bad", "reviewer"],
          str(fake.calls))
    com = fake.of("comment")
    check("L1b one marker comment",
          len(com) == 1 and mod.REVIEW_LANE_REPAIR_MARKER in " ".join(com[0]),
          str(com))
    check("L1c severity-0 finding",
          len(found) == 1 and found[0][1] == 0, str(found))

    owned = {"id": "t_own", "status": "scheduled",
             "title": "Rework PR #415 on the new head"}
    arm = {"id": "t_arm", "status": "ready", "assignee": "reviewer",
           "title": "arm", "key": "reviewer-pr415-22e88f8"}
    found, fake = gate(mod, [owned, arm],
                       [("t_own", "review_requested", handoff(None))])
    check("L2 structural owner: no calls, owned finding",
          not fake.calls and found and found[0][0].endswith(":owned"),
          str(fake.calls) + str(found))

    found, fake = gate(mod, [{"id": "t_good", "status": "review"}],
                       [("t_good", "review_requested", handoff("reviewer"))])
    check("L3 reviewer set: silent", not fake.calls and not found,
          str(fake.calls) + str(found))

    found, fake = gate(mod, [{"id": "t_cr", "status": "ready"}],
                       [("t_cr", "review_requested", handoff(None)),
                        ("t_cr", "changes_requested",
                         {"reason": "CR", "reviewer": "reviewer"})])
    check("L4 later verdict: silent", not fake.calls and not found,
          str(fake.calls) + str(found))

    found, fake = gate(mod, [bad], bad_ev, comments=[
        ("t_bad", mod.REVIEW_LANE_REPAIR_MARKER + " already handled.")])
    check("L5 marker stands: silent", not fake.calls and not found,
          str(fake.calls) + str(found))

    found, fake = gate(mod, [{"id": "t_run", "status": "running"}],
                       [("t_run", "review_requested", handoff(None))])
    check("L6 running: not touched", not fake.calls and not found,
          str(fake.calls) + str(found))

    found, fake = gate(mod, [bad], bad_ev, reassign_out="")
    check("L7 failed reassign: severity 1, no comment",
          found and found[0][1] == 1 and not fake.of("comment"),
          str(fake.calls) + str(found))

    check("L8 registered in the hourly pass",
          "found.extend(review_lane_repair_gate(slug))" in mod.SRC,
          "no call in check()")
    check("L8b registered in --rework-gate-only",
          "for _, _, text in review_lane_repair_gate(slug):" in mod.SRC,
          "no call in the fast pass")
    return fails


def main():
    src = open(BW_PATH, encoding="utf-8").read()
    failed = 0
    for name, (old, _new) in MUTANTS.items():
        if src.count(old) != 1:
            print("FAIL: the anchor of mutant %s occurs %d times"
                  % (name, src.count(old)))
            failed += 1
    if failed:
        return 1

    fails = checks(load_module(src))
    if fails:
        for f in fails:
            print("FAIL %s" % f)
        return 1
    print("ok  real script: L1-L8 pass")

    for name, (old, new) in MUTANTS.items():
        try:
            fails = checks(load_module(src.replace(old, new, 1)))
        except Exception as exc:
            fails = ["crashed: %s: %s" % (type(exc).__name__, exc)]
        if fails:
            print("ok  mutant %s killed (%s)" % (name, fails[0][:90]))
        else:
            print("FAIL mutant %s survived, the test is blind to it" % name)
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    rc = main()
    print("\nTOTAL review-lane-repair: %s" % ("OK" if rc == 0 else "FAIL"))
    sys.exit(rc)
