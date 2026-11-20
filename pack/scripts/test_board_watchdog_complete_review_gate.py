#!/usr/bin/env python3
"""The complete-review gate raises a review a closed PR card skipped.

A PR is handed to review only through the native kanban_request_review.
When a coder closes the card with kanban_complete and metadata.published_pr
instead, the native review lane never starts on the done card and the
watchdog no longer arms reviewer cards by head, so the PR sits open without
any review. complete_review_gate is the safety net.

Checks:
  R1 a closed card names an open PR with no verdict on its head: a
     closed-review finding, one reviewer card (key reviewer-pr<N>-<head7>,
     assignee reviewer, priority 100, body with the native guard receipt
     and the full head) and one marker comment with the release condition;
  R2 an accept verdict on the head: silent;
  R2b a reject verdict on the head: silent (the rework arm owns it);
  R3 a live card keyed reviewer-pr<N>-<head7>: no finding, no create
     (the owner lookup is stubbed empty here, so the native guard receipt
     alone must hold);
  R4 a head younger than 30 minutes (CI tail): silent;
  R5 dry-run: a finding, no create, no comment;
  R6 the marker comment already stands: no new comment, the create holds;
  R7 the gate is registered in the main run;
  R8 result is NULL and the PR is named only in the run summary: armed;
  G1 a live owner card in scheduled, linked to the PR only by its title
     (its handoff has not written metadata yet): silent;
  G2 a live owner card in review, linked only by the structural metadata
     of its handoff run: silent;
  G3 the only closed card is a reviewer card (verdict on an older head):
     silent, a reviewer card never handed the PR over;
  G3b the same closed reviewer card plus a live owner: silent;
  G4 a closed coder card and no live owner still arms (this is R1);
  G5 a live service wait card naming the PR is not an owner: armed;
  G6 a draft PR: silent.
The checks run on the real script (all must pass) and then on mutants of
the gate lines (each must be killed by at least one check).

Run: python3 test_board_watchdog_complete_review_gate.py
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
    "result-not-null-filter": (
        "            \"AND completed_at IS NOT NULL AND completed_at > ? \"\n"
        "            \"ORDER BY completed_at DESC\",\n"
        "            (now - OWNER_MERGE_HOLD_WINDOW_S,)).fetchall()\n"
        "        memo = {}\n"
        "        review_memo = {}\n"
        "        armed = set()",
        "            \"AND completed_at IS NOT NULL AND completed_at > ? \"\n"
        "            \"AND result IS NOT NULL \"\n"
        "            \"ORDER BY completed_at DESC\",\n"
        "            (now - OWNER_MERGE_HOLD_WINDOW_S,)).fetchall()\n"
        "        memo = {}\n"
        "        review_memo = {}\n"
        "        armed = set()"),
    "accept-only-silences": (
        "                if st.get(\"head_kind\") is not None:\n"
        "                    continue\n"
        "                age = st.get(\"age\")",
        "                if st.get(\"head_kind\") == \"accept\":\n"
        "                    continue\n"
        "                age = st.get(\"age\")"),
    "live-ignored": (
        "                    card_key, live_keys=_live_keys(slug),\n"
        "                    native_checked=[\n"
        "                        board_native_guard.NATIVE_RESPAWN,\n"
        "                        board_native_guard.NATIVE_REVIEW_LANE],\n"
        "                    why=\"card %s was closed",
        "                    card_key, live_keys=[],\n"
        "                    native_checked=[\n"
        "                        board_native_guard.NATIVE_RESPAWN,\n"
        "                        board_native_guard.NATIVE_REVIEW_LANE],\n"
        "                    why=\"card %s was closed"),
    "young-head-armed": (
        "                if age is None or age < REVIEW_ARM_MIN_AGE_S \\\n"
        "                        or age > REVIEW_ARM_MAX_AGE_S:\n"
        "                    continue\n"
        "                head = st.get(\"head\") or \"\"\n"
        "                if not head:\n"
        "                    continue\n"
        "                card_key = \"reviewer-pr%d-%s\"",
        "                if age is None or age > REVIEW_ARM_MAX_AGE_S:\n"
        "                    continue\n"
        "                head = st.get(\"head\") or \"\"\n"
        "                if not head:\n"
        "                    continue\n"
        "                card_key = \"reviewer-pr%d-%s\""),
    "report-only": (
        "                card_id = _kanban_create(\n"
        "                    slug,\n"
        "                    \"[reviewer] PR #%d (head %s): review",
        "                card_id = None and _kanban_create(\n"
        "                    slug,\n"
        "                    \"[reviewer] PR #%d (head %s): review"),
    "owner-ignored": (
        "                if owner_memo[number]:\n"
        "                    continue",
        "                if False and owner_memo[number]:\n"
        "                    continue"),
    "reviewer-trigger": (
        "            if (t_assignee or \"\") == \"reviewer\" \\\n"
        "                    or (t_key or \"\").lower().startswith("
        "\"reviewer-pr\"):",
        "            if False:"),
    "draft-armed": (
        "                if st.get(\"is_draft\"):",
        "                if False:"),
    "not-registered": (
        "        found.extend(complete_review_gate(slug))",
        "        pass"),
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
    mod = types.ModuleType("bw_complete_review_gate")
    mod.__file__ = BW_PATH
    exec(compile(src, BW_PATH, "exec"), mod.__dict__)
    mod.SRC = src
    return mod


def make_board(mod, tasks, comments=(), runs=()):
    tmp = tempfile.mkdtemp(prefix="wdtest-complete-review-")
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
             now - 3600 if done else None, t.get("result"),
             t.get("assignee", "coder"), t.get("key")))
    for tid, body in comments:
        con.execute("insert into task_comments (task_id, body, created_at)"
                    " values (?, ?, ?)", (tid, body, now))
    for run_row in runs:
        tid, summary = run_row[0], run_row[1]
        meta = json.dumps(run_row[2]) if len(run_row) > 2 else None
        con.execute("insert into task_runs (task_id, summary, metadata,"
                    " started_at, ended_at) values (?, ?, ?, ?, ?)",
                    (tid, summary, meta, now - 7200, now - 3600))
    con.commit()
    con.close()
    mod.BOARDS = tmp
    return slug


class FakeRun(object):
    """gh for _pr_merged plus the kanban CLI; create answers a JSON card and
    keeps the body file, which _kanban_create deletes afterwards."""

    def __init__(self, pr_states):
        self.pr_states = pr_states
        self.calls = []
        self.bodies = []

    def __call__(self, cmd, timeout=None):
        self.calls.append(cmd)
        if cmd[0] == "gh" and "state,mergedAt" in cmd:
            st = self.pr_states.get(int(cmd[cmd.index("view") + 1]))
            if st is None:
                raise RuntimeError("gh down")
            return json.dumps({"state": st, "mergedAt": None})
        if len(cmd) > 4 and cmd[4] == "create":
            try:
                with open(arg(cmd, "--body-file"), encoding="utf-8") as fh:
                    self.bodies.append(fh.read())
            except (OSError, TypeError):
                self.bodies.append("")
            return json.dumps({"id": "t_new1"})
        if len(cmd) > 4 and cmd[4] == "comment":
            return "Commented %s" % cmd[5]
        return ""

    def of(self, verb):
        return [c for c in self.calls if len(c) > 4 and c[4] == verb]


# The live shape: a card closed with kanban_complete names the PR in its
# result, and the head has no verdict. #20 is the issue number (gh fails).
CARD = {"id": "t_w1", "title": "rebase PR #31 onto main (issue #20)",
        "result": "PR #31 ready for review on the rebased head abc1234: CI "
                  "green, mergeable."}
STATES = {20: None, 31: "OPEN"}
HEAD = "abc1234" + "0" * 33
OPEN_NO_VERDICT = {"pr_state": "OPEN", "head": HEAD, "head_kind": None,
                   "verdict": False, "age": 3600}
KEY = "reviewer-pr31-abc1234"


def gate(mod, review=None, tasks=(CARD,), comments=(), runs=(),
         apply=True, no_owner_lookup=False):
    fake = FakeRun(STATES)
    slug = make_board(mod, tasks, comments=comments, runs=runs)
    mod.run = fake
    mod.board_repo = lambda s: "acme/widget"
    states = {31: review if review is not None else dict(OPEN_NO_VERDICT)}
    mod._pr_review_state = lambda repo, n: states[n]
    real_owners = mod._gate_live_pr_owners
    if no_owner_lookup:
        mod._gate_live_pr_owners = lambda *a: []
    try:
        found = mod.complete_review_gate(slug, apply=apply)
    finally:
        mod._gate_live_pr_owners = real_owners
    return [f for f in found if "closed-review" in f[0]], fake


def arg(cmd, flag):
    return cmd[cmd.index(flag) + 1] if flag in cmd else None


def checks(mod):
    fails = []

    def check(name, cond, detail=""):
        if not cond:
            fails.append("%s :: %s" % (name, detail))

    found, fake = gate(mod)
    created = fake.of("create")
    check("R1 finding names the PR",
          any(":31" in f[0] for f in found), str(found))
    check("R1b one reviewer card", len(created) == 1, str(created))
    if created:
        cmd = created[0]
        check("R1c key", arg(cmd, "--idempotency-key") == KEY, str(cmd))
        check("R1d assignee reviewer",
              arg(cmd, "--assignee") == "reviewer", str(cmd))
        check("R1e priority 100", arg(cmd, "--priority") == "100", str(cmd))
        body = " ".join(fake.bodies)
        check("R1f body: guard receipt and full head",
              "Native guard" in body and HEAD in body, body[:200])
    comments = fake.of("comment")
    check("R1g one marker comment with the release condition",
          len(comments) == 1
          and mod.REVIEW_HOLD_MARKER in " ".join(comments[0])
          and "Release condition" in " ".join(comments[0])
          and "abc1234" in " ".join(comments[0]), str(comments))

    found, fake = gate(mod, review=dict(OPEN_NO_VERDICT, head_kind="accept",
                                        verdict=True))
    check("R2 accept on the head: silent",
          not found and not fake.of("create"), str(fake.calls))

    found, fake = gate(mod, review=dict(OPEN_NO_VERDICT, head_kind="reject",
                                        verdict=True))
    check("R2b reject on the head: silent",
          not found and not fake.of("create"), str(fake.calls))

    live = {"id": "t_live", "title": "[reviewer] PR #31 (head abc1234)",
            "result": "", "assignee": "reviewer", "status": "ready",
            "key": KEY}
    found, fake = gate(mod, tasks=(CARD, live), no_owner_lookup=True)
    check("R3 live key: no finding, no create",
          not found and not fake.of("create"), str(fake.calls))

    found, fake = gate(mod, review=dict(OPEN_NO_VERDICT, age=600))
    check("R4 young head: silent",
          not found and not fake.of("create"), str(fake.calls))

    found, fake = gate(mod, apply=False)
    check("R5 dry-run: finding without actions",
          found and not fake.of("create") and not fake.of("comment"),
          str(fake.calls))

    found, fake = gate(mod, comments=[
        ("t_w1", mod.REVIEW_HOLD_MARKER + " the card was closed with PR #31")])
    check("R6 marker stands: no comment, create holds",
          not fake.of("comment") and len(fake.of("create")) == 1,
          str(fake.calls))

    check("R7 registered in the main run",
          "found.extend(complete_review_gate(slug))" in mod.SRC,
          "no call in check()")

    card = {"id": "t_w2", "title": "rebase onto main (issue #20)",
            "result": None}
    found, fake = gate(mod, tasks=(card,), runs=[(
        "t_w2", "PR #31 ready for review on the rebased head abc1234.")])
    check("R8 result NULL, PR in the summary: armed",
          found and [arg(c, "--idempotency-key")
                     for c in fake.of("create")] == [KEY],
          str(found) + str(fake.of("create")))

    def silent(found, fake):
        return not found and not fake.of("create")

    owner_sched = {"id": "t_own", "title": "[coder] PR #31 (head 0dd0dd0): "
                   "close finding 2", "result": "", "status": "scheduled",
                   "key": "coder-f2-m4"}
    found, fake = gate(mod, tasks=(CARD, owner_sched))
    check("G1 live owner in scheduled, title only: silent",
          silent(found, fake), str(fake.calls))

    owner_meta = {"id": "t_own2", "title": "stale banner rework",
                  "result": "", "status": "review", "key": "coder-f2-m4-r2"}
    found, fake = gate(mod, tasks=(CARD, owner_meta), runs=[(
        "t_own2", "waiting for review",
        {"pr": "https://github.com/acme/widget/pull/31", "pr_number": 31,
         "head_sha": "0dd0dd0"})])
    check("G2 structural owner by handoff metadata: silent",
          silent(found, fake), str(fake.calls))

    closed_reviewer = {"id": "t_rev", "title": "[reviewer] PR #31 (head "
                       "0dd0dd0): review", "assignee": "reviewer",
                       "result": "CHANGES REQUESTED on 0dd0dd0.",
                       "key": "reviewer-pr31-0dd0dd0"}
    found, fake = gate(mod, tasks=(closed_reviewer,))
    check("G3 closed reviewer card is not a trigger: silent",
          silent(found, fake), str(fake.calls))

    found, fake = gate(mod, tasks=(closed_reviewer, owner_sched))
    check("G3b closed reviewer plus live owner: silent",
          silent(found, fake), str(fake.calls))

    wait = {"id": "t_svc", "title": "[wait-merge] PR #31 waits for the "
            "merge", "result": "", "status": "blocked", "key": "svc-31"}
    found, fake = gate(mod, tasks=(CARD, wait))
    check("G5 a service wait card is not an owner: armed",
          found and len(fake.of("create")) == 1, str(fake.calls))

    found, fake = gate(mod, review=dict(OPEN_NO_VERDICT, is_draft=True))
    check("G6 draft PR: silent", silent(found, fake), str(fake.calls))
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
    print("ok  real script: R1-R8, G1-G6 pass")

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
    print("\nTOTAL complete-review-gate: %s" % ("OK" if rc == 0 else "FAIL"))
    sys.exit(rc)
