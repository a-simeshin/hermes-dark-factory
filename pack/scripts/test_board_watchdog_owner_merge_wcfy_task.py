#!/usr/bin/env python3
"""The WCFY close gate creates work, not only a report.

In the native review lane the reviewer's approval closes the card in one
transition, so a WCFY step "between the accept verdict and closing" never
runs: the implementer gets no turn after done. The normal path posts WCFY
before kanban_request_review; the WCFY branch of owner_merge_hold_gate is
the safety net and, besides the signal and the comment, creates a task card
for the implementer of the closed card.

Checks:
  T1 a task card is created once: key wcfy-hold-<slug>-<tid>-<PR>,
     addressee from the latest review_requested (not the card assignee),
     priority 80 without band data, body with the native guard receipt and
     the pr-deliverable protocol; the signal comment names the key;
  T2 a live card with the same key: no create, the finding holds;
  T3 dry-run: a finding, no create, no comment;
  T4 the marker comment already stands: no new comment, the create holds;
  T5 _task_implementer: review_requested, then assignee, then None;
  T6 no implementer known: the addressee is coder;
  T7 a fresh WCFY on the head: no finding, no create.
The checks run on the real script (all must pass) and then on mutants of
the gate lines (each must be killed by at least one check).

Run: python3 test_board_watchdog_owner_merge_wcfy_task.py
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
    "report-only": (
        "                card_id = \"\"\n                if not covered:",
        "                card_id = \"\"\n                if False:"),
    "implementer-ignored": (
        "implementer = _task_implementer(con, tid) or \"coder\"",
        "implementer = \"coder\""),
    "live-ignored": (
        "                    card_key,\n"
        "                    live_keys=_live_keys(slug),",
        "                    card_key,\n"
        "                    live_keys=[],"),
    "priority-dropped": (
        "str(svc_priority(slug, number, 80)),",
        "\"0\","),
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
    mod = types.ModuleType("bw_owner_merge_wcfy_task")
    mod.__file__ = BW_PATH
    exec(compile(src, BW_PATH, "exec"), mod.__dict__)
    return mod


def make_board(mod, tasks, comments=(), events=()):
    tmp = tempfile.mkdtemp(prefix="wdtest-owner-merge-wcfy-task-")
    slug = "testboard"
    os.makedirs(os.path.join(tmp, slug), exist_ok=True)
    con = sqlite3.connect(os.path.join(tmp, slug, "kanban.db"))
    con.executescript(SCHEMA)
    now = int(time.time())
    for t in tasks:
        done = t.get("status", "done") == "done"
        con.execute(
            "insert into tasks (id, title, status, completed_at, result,"
            " assignee, idempotency_key) values (?, ?, ?, ?, ?, ?, ?)",
            (t["id"], t.get("title", ""), t.get("status", "done"),
             now - 3600 if done else None, t.get("result", ""),
             t.get("assignee", "ops"), t.get("key")))
    for tid, body in comments:
        con.execute("insert into task_comments (task_id, body, created_at)"
                    " values (?, ?, ?)", (tid, body, now))
    for tid, kind, payload in events:
        con.execute("insert into task_events (task_id, kind, payload,"
                    " created_at) values (?, ?, ?, ?)",
                    (tid, kind, payload, now))
    con.commit()
    con.close()
    mod.BOARDS = tmp
    return slug


class FakeRun(object):
    """gh for _pr_merged plus the kanban CLI; create answers a JSON card."""

    def __init__(self, pr_states):
        self.pr_states = pr_states
        self.calls = []

    def __call__(self, cmd, timeout=None):
        self.calls.append(cmd)
        if cmd[0] == "gh" and "state,mergedAt" in cmd:
            st = self.pr_states.get(int(cmd[cmd.index("view") + 1]))
            if st is None:
                raise RuntimeError("gh down")
            return json.dumps({"state": st, "mergedAt": None})
        if len(cmd) > 4 and cmd[4] == "create":
            return json.dumps({"id": "t_new1"})
        if len(cmd) > 4 and cmd[4] == "comment":
            return "Commented %s" % cmd[5]
        return ""

    def of(self, verb):
        return [c for c in self.calls if len(c) > 4 and c[4] == verb]


CARD = {"id": "t_w1", "title": "coder: demo client end to end (issue #20)",
        "result": "PR #31 accepted, CI green.", "assignee": "reviewer"}
EV_IMPL = ("t_w1", "review_requested",
           json.dumps({"implementer": "builder", "reviewer": "reviewer"}))
STATES = {20: None, 31: "OPEN"}
OPEN_ACCEPT = {"pr_state": "OPEN", "head": "abc1234" + "0" * 33,
               "head_kind": "accept", "wcfy_at": None,
               "head_commit_at": time.time() - 7200}
KEY = "wcfy-hold-testboard-t_w1-31"


def gate(mod, review=None, tasks=(CARD,), comments=(), events=(EV_IMPL,),
         apply=True):
    fake = FakeRun(STATES)
    slug = make_board(mod, tasks, comments=comments, events=events)
    mod.run = fake
    mod.board_repo = lambda s: "acme/widget"
    states = {31: review if review is not None else dict(OPEN_ACCEPT)}
    mod._pr_review_state = lambda repo, n: states[n]
    found = mod.owner_merge_hold_gate(slug, apply=apply)
    return [f for f in found if "owner-wcfy-hold" in f[0]], fake


def arg(cmd, flag):
    return cmd[cmd.index(flag) + 1] if flag in cmd else None


def checks(mod):
    fails = []

    def check(name, cond, detail=""):
        if not cond:
            fails.append("%s :: %s" % (name, detail))

    found, fake = gate(mod)
    created = fake.of("create")
    check("T1 finding holds", found, str(found))
    check("T1b one task card", len(created) == 1, str(created))
    if created:
        cmd = created[0]
        check("T1c key", arg(cmd, "--idempotency-key") == KEY, str(cmd))
        check("T1d addressee from review_requested",
              arg(cmd, "--assignee") == "builder", str(cmd))
        check("T1e priority 80", arg(cmd, "--priority") == "80", str(cmd))
        body = arg(cmd, "--body") or ""
        check("T1f body: guard receipt and protocol",
              "Native guard" in body and "pr-deliverable" in body, body[:200])
    comments = fake.of("comment")
    check("T1g one comment naming the key",
          len(comments) == 1 and KEY in " ".join(comments[0]), str(comments))

    live = {"id": "t_live", "title": "WCFY: PR #31", "status": "ready",
            "key": KEY}
    found, fake = gate(mod, tasks=(CARD, live))
    check("T2 live key: finding holds, no create",
          found and not fake.of("create"), str(fake.of("create")))

    found, fake = gate(mod, apply=False)
    check("T3 dry-run: finding without actions",
          found and not fake.of("create") and not fake.of("comment"),
          str(fake.calls))

    found, fake = gate(mod, comments=[
        ("t_w1", mod.WCFY_HOLD_MARKER + " the card is closed, PR #31")])
    check("T4 marker stands: no comment, create holds",
          not fake.of("comment") and len(fake.of("create")) == 1,
          str(fake.calls))

    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA)
    con.execute("insert into tasks (id, status, assignee) values"
                " ('t_a', 'done', 'reviewer')")
    con.execute("insert into task_events (task_id, kind, payload) values"
                " ('t_a', 'review_requested', ?)",
                (json.dumps({"implementer": "builder"}),))
    got = mod._task_implementer(con, "t_a")
    check("T5a implementer from review_requested", got == "builder", got)
    con.execute("delete from task_events")
    got = mod._task_implementer(con, "t_a")
    check("T5b fallback to assignee", got == "reviewer", got)
    con.execute("update tasks set assignee = '' where id = 't_a'")
    got = mod._task_implementer(con, "t_a")
    check("T5c nothing known: None", got is None, got)
    con.close()

    found, fake = gate(mod, tasks=(dict(CARD, assignee=""),), events=())
    check("T6 no implementer known: coder",
          [arg(c, "--assignee") for c in fake.of("create")] == ["coder"],
          str(fake.of("create")))

    found, fake = gate(mod, review=dict(OPEN_ACCEPT,
                                        wcfy_at=time.time() - 60))
    check("T7 fresh WCFY: no finding, no create",
          not found and not fake.of("create"), str(fake.calls))
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
    print("ok  real script: T1-T7 pass")

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
    print("\nTOTAL owner-merge-wcfy-task: %s" % ("OK" if rc == 0 else "FAIL"))
    sys.exit(rc)
