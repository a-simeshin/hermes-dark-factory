#!/usr/bin/env python3
"""WCFY close gate in owner_merge_hold_gate (one PR = one card).

Once review lives on the card that owns a PR, the WCFY step ("## What
changes for you") belongs to that card too. A card closed with its PR open,
a reviewer accept verdict on the head and no WCFY drops out of sight, and
the owner does not merge without WCFY (pr-deliverable). The native
completion contract checks only the CI gates of the head and cannot
express WCFY, so owner_merge_hold_gate gains a second branch: such a closed
card keeps an owner finding plus one comment with a release condition.
The PR number is also looked up in the summary of the finishing run: a PR
can be named only there, while the title carries the issue number and an
already merged PR.

Checks:
  W1/W1b/W1c PR from the run summary, accept on the head, no WCFY: a
             finding with a release condition and one comment;
  W2 a fresh WCFY on the head: no finding;
  W3 a reject verdict on the head: no finding;
  W4 no verdict on the head: no finding;
  W5 a merged PR: no finding;
  W6 a repeated pass keeps the signal and does not duplicate the comment;
  W7 a gh failure in the review state skips silently;
  W8 dry-run: a finding without actions;
  W9 a WCFY older than the head does not release the gate;
  W10 the finding is classified as owner.
The checks run on the real script (all must pass) and then on mutants of
the gate lines (each must be killed by at least one check).

Run: python3 test_board_watchdog_owner_merge_wcfy.py
"""

import json
import os
import sqlite3
import sys
import tempfile
import time
import types

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
BW_PATH = os.path.join(HERE, "board-watchdog.py")

# name -> (anchor in the gate, replacement). Each mutant disables one
# behaviour of the WCFY branch.
MUTANTS = {
    "summary-ignored": (
        "for number in _closed_card_pr_numbers(result, title,\n"
        "                                                  last_summary):",
        "for number in _closed_card_pr_numbers(result, title,\n"
        "                                                  \"\"):"),
    "freshness-ignored": (
        "if wcfy_at is not None and (\n"
        "                        head_at is None or wcfy_at >= head_at):",
        "if wcfy_at is not None and (\n"
        "                        True or wcfy_at >= head_at):"),
    "verdict-ignored": (
        "                if st.get(\"head_kind\") != \"accept\":\n"
        "                    continue\n"
        "                wcfy_at",
        "                if False:\n"
        "                    continue\n"
        "                wcfy_at"),
    "no-comment-dedup": (
        "(tid, WCFY_HOLD_MARKER[:40] + \"%\")).fetchone()[0]",
        "(tid, \"\\x00\" + \"%\")).fetchone()[0]"),
    "class-digest": (
        "    if \":owner-wcfy-hold:\" in key:\n        return \"owner\"",
        "    if \":owner-wcfy-hold:\" in key:\n        return \"digest\""),
}


def load_module(src):
    mod = types.ModuleType("bw_owner_merge_wcfy")
    mod.__file__ = BW_PATH
    exec(compile(src, BW_PATH, "exec"), mod.__dict__)
    return mod


def make_board(mod, tasks=(), comments=(), runs=()):
    tmp = tempfile.mkdtemp(prefix="wdtest-owner-merge-wcfy-")
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
    now = int(time.time())
    for t in tasks:
        con.execute("insert into tasks (id, title, status, completed_at,"
                    " result) values (?, ?, 'done', ?, ?)",
                    (t["id"], t.get("title", ""), now - 3600,
                     t.get("result", "")))
    for tid, body in comments:
        con.execute("insert into task_comments (task_id, body, created_at)"
                    " values (?, ?, ?)", (tid, body, now))
    for tid, summary in runs:
        con.execute("insert into task_runs (task_id, status, outcome,"
                    " summary) values (?, 'done', 'completed', ?)",
                    (tid, summary))
    con.commit()
    con.close()
    mod.BOARDS = tmp
    return slug


class FakeRun(object):
    """gh for _pr_merged: state by number, failure on an unknown number."""

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
        if len(cmd) > 4 and cmd[4] == "comment":
            return "Commented %s" % cmd[5]
        return ""

    def comments(self):
        return [c for c in self.calls if len(c) > 4 and c[4] == "comment"]


def fake_review_state(states):
    def _rs(repo, number):
        if number not in states:
            raise RuntimeError("gh down")
        return states[number]
    return _rs


# The issue is #30, an older PR #29 is merged, the open PR #31 is named
# only in the summary of the finishing run.
CARD = {"id": "t_w1",
        "title": "coder: fix stale wording (issue #30, after PR #29 merged)",
        "result": ""}
RUN = ("t_w1", "Closed the review cycle on PR #31 (head abc1234): accept "
       "verdict already posted, CI green.")
STATES = {30: None, 29: "MERGED", 31: "OPEN"}
OPEN_ACCEPT = {"pr_state": "OPEN", "head": "abc1234" + "0" * 33,
               "head_kind": "accept", "wcfy_at": None,
               "head_commit_at": time.time() - 7200}


def gate(mod, review, pr_states=STATES, comments=(), apply=True):
    fake = FakeRun(pr_states)
    slug = make_board(mod, tasks=[CARD], runs=[RUN], comments=comments)
    mod.run = fake
    mod.board_repo = lambda s: "acme/widget"
    mod._pr_review_state = fake_review_state(review)
    found = mod.owner_merge_hold_gate(slug, apply=apply)
    return [f for f in found if "owner-wcfy-hold" in f[0]], fake


def checks(mod):
    fails = []

    def check(name, cond, detail=""):
        if not cond:
            fails.append("%s :: %s" % (name, detail))

    found, fake = gate(mod, {31: dict(OPEN_ACCEPT)})
    keys = [f[0] for f in found]
    check("W1 finding by the PR from the run summary",
          any("t_w1" in k and k.endswith(":31") for k in keys), str(keys))
    txt = " ".join(f[2] for f in found)
    check("W1b the finding names the release condition",
          "Release condition" in txt and "#31" in txt, txt[:200])
    check("W1c one signal comment", len(fake.comments()) == 1,
          str(fake.comments()))

    found, _ = gate(mod, {31: dict(OPEN_ACCEPT, wcfy_at=time.time() - 3600)})
    check("W2 fresh WCFY on the head: no finding", not found, str(found))

    found, _ = gate(mod, {31: dict(OPEN_ACCEPT, head_kind="reject")})
    check("W3 reject on the head: no finding", not found, str(found))

    found, _ = gate(mod, {31: dict(OPEN_ACCEPT, head_kind=None)})
    check("W4 no verdict on the head: no finding", not found, str(found))

    found, _ = gate(mod, {31: dict(OPEN_ACCEPT)},
                    pr_states={30: None, 29: "MERGED", 31: "MERGED"})
    check("W5 merged PR: no finding", not found, str(found))

    found, fake = gate(mod, {31: dict(OPEN_ACCEPT)}, comments=[
        ("t_w1", mod.WCFY_HOLD_MARKER + " the card is closed, PR #31")])
    check("W6 repeated pass: signal holds, no duplicate comment",
          found and not fake.comments(), str(fake.calls))

    found, _ = gate(mod, {})
    check("W7 review state failure: no finding, no crash", not found,
          str(found))

    found, fake = gate(mod, {31: dict(OPEN_ACCEPT)}, apply=False)
    check("W8 dry-run: finding without actions",
          found and not fake.comments(), str(fake.calls))

    found, _ = gate(mod, {31: dict(OPEN_ACCEPT,
                                   wcfy_at=time.time() - 9000)})
    check("W9 WCFY older than the head: finding holds", found, str(found))

    key = "testboard:owner-wcfy-hold:t_w1:31"
    check("W10 classify gives owner", mod.classify_finding(key) == "owner",
          str(mod.classify_finding(key)))
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
    print("ok  real script: W1-W10 pass")

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
    print("\nTOTAL owner-merge-wcfy: %s" % ("OK" if rc == 0 else "FAIL"))
    sys.exit(rc)
