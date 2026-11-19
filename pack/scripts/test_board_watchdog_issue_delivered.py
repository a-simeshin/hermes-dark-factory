#!/usr/bin/env python3
"""The issue executor poll does not create a card when the work on an issue
is already delivered.

Why: the 5-minute poll created an executor card "chore(release): prepare
<version>" for a release-chore issue that the owner keeps open on purpose
until the tag, although the release preparation PR was already merged. After
the owner archived it, the poll created a duplicate ten minutes later.

Rule: there is no card when the issue has

  a) a merged PR referencing it, merged AFTER the last scope change of the
     issue (creation + labeled/unlabeled/renamed; milestoned and comments
     are not scope: moving a milestone is bookkeeping);
  b) a done executor card whose text mentions #N and a merged PR (done by
     itself is not coverage, the delivery is the PR).

A release chore (RELEASE_CHORE_RE) after a merged prep PR stays open until
the tag: full silence, the owner decides. Delivered work that is not a chore
gets no card but a question to the owner "what remains for #N"
(admission:delivered-open, class owner). Checks:

  C. The 5-minute poll: the release-chore shape (merged prep PR, open issue,
     no live card) -> silence; the same for the done branch; scope changed
     AFTER the merged PR -> a card is created.
  B. Admission gate: delivered-open is a question to the owner (owner), not
     no-executor/blk; a release chore is fully silent.
  D. CLI issue-executor-poll --check-issue: rc 3 COVERED for chore and
     non-chore, rc 0 NOT-DELIVERED.

Mutants: each must turn the battery red. Red before the fix: a copy of the
source with the fix's edits reverted (an in-process mutant).

The board harness is imported from test_board_watchdog_board_lint.py.

Run: python3 test_board_watchdog_issue_delivered.py
"""
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.join(HERE, "test_board_watchdog_board_lint.py")
spec = importlib.util.spec_from_file_location("wd_lint_harness_delivered",
                                              HARNESS)
if spec is None or spec.loader is None:
    print("SKIP: cannot load harness")
    sys.exit(0)
h = importlib.util.module_from_spec(spec)
sys.modules["wd_lint_harness_delivered"] = h
spec.loader.exec_module(h)
# The harness fixed HERMES_HOME and the FACTORY_* settings; the bot login
# marks issue mirrors.
os.environ["FACTORY_BOT_LOGINS"] = "widget-bot"

NOW = time.time()
FAILS = []


def check(name, cond, detail=""):
    print("%s %s%s" % ("ok  " if cond else "FAIL", name,
                       (" :: %s" % (detail,)) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


OPEN_MILESTONES = json.dumps([
    {"title": "2.3.0", "due_on": "2026-11-22T00:00:00Z"},
    {"title": "2.4.0", "due_on": "2026-12-20T00:00:00Z"},
])

# --- C: poll fixtures (the release-chore shape and its neighbours) --------
# merged PRs: 791 closes #702 (not a chore), 793 referenced #703 but is OLD,
# 797 was merged without an issue reference (the done card t_705d names it)
MERGED_P = json.dumps([
    {"number": 790, "title": "chore: prepare the 2.3.0 release",
     "body": "Refs #701", "mergedAt": _iso(NOW - 3600)},
    {"number": 791, "title": "Fix connect timeout", "body": "Closes #702",
     "mergedAt": _iso(NOW - 3600)},
    {"number": 793, "title": "old work", "body": "Refs #703",
     "mergedAt": _iso(NOW - 3 * 3600)},
    {"number": 797, "title": "release prep", "body": "no issue refs",
     "mergedAt": _iso(NOW - 3600)},
])
ISSUES_P = json.dumps([
    # release chore: the prep PR was merged an hour ago, milestoned AFTER the
    # merge (20 min ago) does not change the scope -> silence
    {"number": 701, "milestone": {"title": "2.3.0"}, "labels": [],
     "body": "Release chore for milestone 2.3.0",
     "updatedAt": _iso(NOW - 20 * 60)},
    # delivered, not a chore (PR 791 merged after the scope) -> the poll is
    # silent, the gate asks the question
    {"number": 702, "milestone": {"title": "2.3.0"},
     "labels": [{"name": "hermes"}], "body": "Crash on SSE connect",
     "updatedAt": _iso(NOW - 2 * 3600)},
    # scope changed AFTER the merged PR (labeled 30 min ago against a merge
    # 3 h ago) -> new work, a card is needed
    {"number": 703, "milestone": {"title": "2.3.0"},
     "labels": [{"name": "hermes"}], "body": "Regression after fix",
     "updatedAt": _iso(NOW - 25 * 60)},
    # a live card -> filtered out before the delivered check
    {"number": 704, "milestone": {"title": "2.3.0"},
     "labels": [{"name": "hermes"}], "body": "x",
     "updatedAt": _iso(NOW - 10 * 60)},
    # done branch: no merged reference to #705, the done card t_705d names
    # the work (PR #797 merged) -> silence
    {"number": 705, "milestone": {"title": "2.3.0"},
     "labels": [{"name": "hermes"}], "body": "Docs cleanup",
     "updatedAt": _iso(NOW - 2 * 3600)},
    # a bot mirror -> filtered out before the delivered check
    {"number": 706, "milestone": {"title": "2.3.0"},
     "labels": [{"name": "hermes"}],
     "body": "Mirrored from kanban card t_x",
     "updatedAt": _iso(NOW - 10 * 60)},
])
PRS_P = json.dumps([])     # no open PRs
EVENTS_P = {
    "701": [{"event": "milestoned", "created_at": _iso(NOW - 20 * 60)},
            {"event": "referenced", "created_at": _iso(NOW - 3600)}],
    "703": [{"event": "labeled", "created_at": _iso(NOW - 30 * 60)}],
}
VIEWS_P = {
    "701": {"number": 701, "title": "chore(release): prepare 2.3.0",
            "body": "Release chore", "createdAt": _iso(NOW - 13 * 86400),
            "author": {"login": "owner-login"}},
    "702": {"number": 702, "title": "Crash on SSE connect",
            "body": "Crash on SSE connect", "createdAt": _iso(NOW - 10 * 86400),
            "author": {"login": "owner-login"}},
    "703": {"number": 703, "title": "Regression after fix",
            "body": "Regression", "createdAt": _iso(NOW - 10 * 86400),
            "author": {"login": "owner-login"}},
    "705": {"number": 705, "title": "Docs cleanup", "body": "Docs",
            "createdAt": _iso(NOW - 10 * 86400),
            "author": {"login": "owner-login"}},
    "706": {"number": 706, "title": "Mirrored card work", "body": "x",
            "author": {"login": "widget-bot"}},
}

# --- B: gate fixtures ------------------------------------------------------
MERGED_G = json.dumps([
    {"number": 891, "title": "prepare release", "body": "Refs #801",
     "mergedAt": _iso(NOW - 2 * 3600)},
    {"number": 892, "title": "fix", "body": "Closes #802",
     "mergedAt": _iso(NOW - 2 * 3600)},
])
ISSUES_G = json.dumps([
    # release chore, work merged, 30 h without a live card -> silence
    {"number": 801, "milestone": {"title": "2.3.0"}, "labels": [],
     "body": "Release chore", "updatedAt": _iso(NOW - 30 * 3600)},
    # delivered, not a chore -> delivered-open (question to the owner), not no-executor
    {"number": 802, "milestone": {"title": "2.3.0"},
     "labels": [{"name": "hermes"}], "body": "Fixed already",
     "updatedAt": _iso(NOW - 30 * 3600)},
    # not delivered -> no-executor
    {"number": 803, "milestone": {"title": "2.3.0"},
     "labels": [{"name": "hermes"}], "body": "open work",
     "updatedAt": _iso(NOW - 30 * 3600)},
    # a live card -> silence
    {"number": 804, "milestone": {"title": "2.3.0"},
     "labels": [{"name": "hermes"}], "body": "x",
     "updatedAt": _iso(NOW - 30 * 3600)},
])
PRS_G = json.dumps([])
EVENTS_G = {}
VIEWS_G = {
    "801": {"number": 801, "title": "chore(release): prepare 2.4.0",
            "createdAt": _iso(NOW - 10 * 86400)},
    "802": {"number": 802, "title": "Connect timeout UX",
            "createdAt": _iso(NOW - 10 * 86400)},
    "803": {"number": 803, "title": "open work", "body": "open work",
            "createdAt": _iso(NOW - 10 * 86400)},
}

BW_PATH = os.path.join(HERE, "board-watchdog.py")
POLL_PATH = os.path.join(HERE, "issue-executor-poll.py")


def load_bw(src=None, tag="57"):
    """board-watchdog module from the real file or from the mutated source src."""
    sbx = tempfile.mkdtemp(prefix="bw-sbx57-")
    path = BW_PATH if src is None else os.path.join(sbx, "wd-mut.py")
    if src is not None:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(src)
        for nb in ("milestone_bands.py", "board_native_guard.py"):
            shutil.copy(os.path.join(HERE, nb), os.path.join(sbx, nb))
    sp = importlib.util.spec_from_file_location("bw_mod_%s" % tag, path)
    m = importlib.util.module_from_spec(sp)
    sys.modules["bw_mod_%s" % tag] = m
    sp.loader.exec_module(m)
    m.STATE = os.path.join(sbx, "state.json")
    m.BACKLOG_CACHE = os.path.join(sbx, "backlog.json")
    if hasattr(m, "milestone_bands"):
        m.milestone_bands.CACHE_PATH = os.path.join(sbx, "ms.json")
        m.milestone_bands._MEMO = {}
    return m, sbx


def make_run(db, issues, prs, merged, events, views, creates):
    """run stub: gh from the fixtures, kanban create is applied to the database."""

    def _run(cmd, timeout=120):
        joined = " ".join(str(c) for c in cmd)
        m = re.search(r"/issues/(\d+)/events", joined)
        if cmd[:2] == ["gh", "api"] and m:
            return json.dumps(events.get(m.group(1), []))
        if cmd[:2] == ["gh", "api"]:
            return OPEN_MILESTONES
        if cmd[:2] == ["gh", "pr"] and "list" in cmd and "merged" in cmd:
            return merged
        if cmd[:2] == ["gh", "pr"] and "list" in cmd:
            return prs
        if cmd[:2] == ["gh", "issue"] and "list" in cmd:
            return issues
        if cmd[:2] == ["gh", "issue"] and "view" in cmd:
            return json.dumps(views.get(cmd[3], {}))
        if len(cmd) > 4 and cmd[1] == "kanban" and cmd[4] == "create":
            creates.append(cmd)
            body_file = cmd[cmd.index("--body-file") + 1]
            body = open(body_file, encoding="utf-8").read()
            title = cmd[5]
            key = cmd[cmd.index("--idempotency-key") + 1]
            new_id = "t_new" + key.rsplit("-", 1)[-1]
            con = sqlite3.connect(db, timeout=10)
            con.execute(
                "insert into tasks (id, title, body, status, priority,"
                " assignee, created_by, created_at, idempotency_key)"
                " values (?,?,?, 'ready', ?, ?, 'watchdog', ?, ?)",
                (new_id, title, body,
                 int(cmd[cmd.index("--priority") + 1]),
                 cmd[cmd.index("--assignee") + 1], int(time.time()), key))
            con.commit()
            con.close()
            return json.dumps({"id": new_id})
        return "ok"

    return _run


def battery(mod):
    """Problems of the B/C battery on module mod. Returns a list of strings."""
    problems = []

    def want(name, cond, detail=""):
        if not cond:
            problems.append("%s :: %s" % (name, detail))

    # --- C: the 5-minute poll ----------------------------------------------
    slug, db, _ = h.make_board(tasks=[
        {"id": "t_704", "status": "ready", "priority": 80,
         "title": "[coder] issue #704", "body": "work on #704",
         "assignee": "coder", "created_by": "user"},
        {"id": "t_705d", "status": "done", "priority": 80,
         "title": "[coder] issue #705: docs cleanup",
         "body": "delivered: PR #797 merged, verified",
         "assignee": "coder", "created_by": "user"},
    ])
    mod.BOARDS = os.path.dirname(os.path.dirname(db))
    creates = []
    run_p = make_run(db, ISSUES_P, PRS_P, MERGED_P, EVENTS_P, VIEWS_P,
                     creates)
    real_run = mod.run
    mod.run = run_p
    mod.board_repo = lambda s: "acme/demo"
    try:
        plan = mod.issue_executor_poll()
    except Exception as exc:
        plan = None
        problems.append("C: the poll crashed: %s: %s" % (type(exc).__name__, exc))
    finally:
        mod.run = real_run
    if plan is not None:
        want("C1 release chore #701, #702, #705 are silent, only #703"
             " is created", plan == [(slug, 703, "t_new703")], str(plan))
        want("C2 one kanban create", len(creates) == 1,
             "%d calls" % len(creates))
        con = sqlite3.connect(db)
        rows = list(con.execute(
            "select idempotency_key from tasks where idempotency_key"
            " like 'owner-issue-%'"))
        con.close()
        want("C3 one card owner-issue-703 in the database",
             [r[0] for r in rows] == ["owner-issue-703"], str(rows))

    # --- B: admission gate ---------------------------------------------------
    slug2, db2, _ = h.make_board(tasks=[
        {"id": "t_804", "status": "ready", "priority": 80,
         "title": "[coder] issue #804", "body": "work on #804",
         "assignee": "coder", "created_by": "user"},
    ])
    mod.BOARDS = os.path.dirname(os.path.dirname(db2))
    creates2 = []
    run_g = make_run(db2, ISSUES_G, PRS_G, MERGED_G, EVENTS_G, VIEWS_G,
                     creates2)
    mod.run = run_g
    try:
        findings = mod.admission_gate(slug2)
    except Exception as exc:
        findings = []
        problems.append("B: admission_gate crashed: %s: %s"
                        % (type(exc).__name__, exc))
    finally:
        mod.run = real_run
    keys = {k for k, _, _ in findings}
    want("B1 #802 delivered-open instead of no-executor",
         any(":admission:delivered-open:802" in k for k in keys)
         and not any(":admission:no-executor:802" in k for k in keys),
         sorted(keys))
    texts = [t for k, _, t in findings if ":delivered-open:802" in k]
    want("B2 question to the owner \"what remains\"",
         texts and "what remains for #802" in texts[0], str(texts[:1]))
    want("B3 #801 release chore is fully silent",
         not any(("801" in k) for k in keys), sorted(keys))
    want("B4 #803 not delivered -> no-executor",
         any(":admission:no-executor:803" in k for k in keys), sorted(keys))
    try:
        want("B5 delivered-open is classified as owner",
             mod.classify_finding("testboard:admission:delivered-open:802")
             == "owner")
    except Exception as exc:
        problems.append("B5 classify_finding: %s: %s"
                        % (type(exc).__name__, exc))
    return problems


def cli_battery():
    """D: issue-executor-poll --check-issue (no network: issue_delivered is
    replaced with fixed answers)."""
    problems = []
    sp = importlib.util.spec_from_file_location("poll_wrap_57", POLL_PATH)
    if sp is None or sp.loader is None:
        return ["D: the wrapper does not load"]
    w = importlib.util.module_from_spec(sp)
    sys.modules["poll_wrap_57"] = w
    sp.loader.exec_module(w)
    real = w.wd.issue_delivered
    # the rc is checked by return codes (the wrapper prints to the real stdout)
    try:
        w.wd.issue_delivered = (
            lambda s, r, n, merged=None: (True, "PR #791 merged", True))
        if w.main(["--check-issue", "324", "--repo", "acme/demo",
                   "--board", "main"]) != 3:
            problems.append("D1 chore: rc is not 3 (COVERED)")
        w.wd.issue_delivered = (
            lambda s, r, n, merged=None: (True, "PR #791 merged", False))
        if w.main(["--check-issue", "702", "--repo", "acme/demo",
                   "--board", "main"]) != 3:
            problems.append("D2 not a chore: rc is not 3 (COVERED)")
        w.wd.issue_delivered = (
            lambda s, r, n, merged=None: (False, "", False))
        if w.main(["--check-issue", "703", "--repo", "acme/demo",
                   "--board", "main"]) != 0:
            problems.append("D3 not delivered: rc is not 0 (NOT-DELIVERED)")
    finally:
        w.wd.issue_delivered = real
    return problems


BW_MUTANTS = {
    "milestoned-is-scope": (
        '("labeled", "unlabeled", "renamed"))',
        '("labeled", "unlabeled", "renamed", "milestoned"))'),
    "merge-before-scope-covers": (
        'if info["merged_at"] > scope:',
        'if True:'),
    "done-branch-off": (
        'return True, ("done card %s delivered',
        'return False, ("done card %s delivered'),
    "release-chore-not-silent": (
        "if chore:\n                    continue",
        "if False:\n                    continue"),
    "question-went-to-digest": (
        'if ":admission:delivered-open:" in key:',
        'if ":admission:delivered-open-mut:" in key:'),
}


def main():
    bw_src = open(BW_PATH, encoding="utf-8").read()
    for name, (old, _new) in BW_MUTANTS.items():
        if bw_src.count(old) != 1:
            print("FAIL: the anchor of mutant %s occurs %d times"
                  % (name, bw_src.count(old)))
            return 1

    mod, _ = load_bw()
    problems = battery(mod) + cli_battery()
    for p in problems:
        print("FAIL %s" % p)
    if problems:
        print("\nRED: clean run, %d problems" % len(problems))
        return 1
    print("OK: clean run (C poll, B gate, D CLI)")

    failed = 0
    for name, (old, new) in BW_MUTANTS.items():
        mut = load_bw(bw_src.replace(old, new, 1), tag=name)[0]
        try:
            probs = battery(mut)
        except Exception as exc:
            probs = ["crashed: %s: %s" % (type(exc).__name__, exc)]
        if probs:
            print("OK   %s: mutant killed (%s)"
                  % (name, " | ".join(p[:70] for p in probs[:3])))
        else:
            print("FAIL %s: mutant survived" % name)
            failed += 1

    # Red before the fix: the code before the fix is rebuilt inside the test
    # as a copy of the source with all the fix's edits reverted (all
    # BW_MUTANTS pairs at once), not by walking a git history.
    refix_bw = bw_src
    for _n, (old, new) in BW_MUTANTS.items():
        refix_bw = refix_bw.replace(old, new, 1)
    mut = load_bw(refix_bw, tag="refix")[0]
    try:
        probs = battery(mut)
    except Exception as exc:
        probs = ["crashed: %s: %s" % (type(exc).__name__, exc)]
    if probs:
        print("OK   reverted fix (code before the fix, mutant) is red: %s"
              % probs[0][:80])
    else:
        print("FAIL reverted fix is green: the test is blind before the fix")
        failed += 1

    if failed:
        print("\nRESULT: FAIL, %d problems" % failed)
        return 1
    print("\nRESULT: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
