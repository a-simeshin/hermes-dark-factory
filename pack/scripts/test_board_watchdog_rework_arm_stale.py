#!/usr/bin/env python3
"""A stale rework arm is retired, not raised.

A rework arm raised to p90 started on a PR that already lived on a newer
head with APPROVED: the run was spent on work already done. A rework arm
is built on the verdict of an EXACT head; its ground is gone when the PR
head moved, a PASS arrived on the same head, or the PR left OPEN.
rework_priority_lift (cron --rework-gate-only, every 5 minutes) compares
the head named by the card with the live PR head before lifting and
retires a stale arm: comment + archive, no priority raise. A gh failure or
a card without a named head is treated conservatively: not retired (lifted
as before). running is untouched (status filter).

Run: python3 test_board_watchdog_rework_arm_stale.py
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
HARNESS = os.path.join(HERE, "test_board_watchdog_board_lint.py")
spec = importlib.util.spec_from_file_location("wd_lint_harness_rework_stale", HARNESS)
if spec is None or spec.loader is None:
    print("SKIP: cannot load harness")
    sys.exit(0)
h = importlib.util.module_from_spec(spec)
sys.modules["wd_lint_harness_rework_stale"] = h
spec.loader.exec_module(h)

FAILS = []


def check(name, cond, detail=""):
    print("%s %s%s" % ("ok  " if cond else "FAIL", name,
                       (" :: %s" % (detail,)) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


OPEN_MILESTONES = json.dumps([
    {"title": "2.4.0", "due_on": "2026-12-01T00:00:00Z"},   # active, 80
])

BW_PATH = os.path.join(HERE, "board-watchdog.py")

# Live PR heads by number (the shape of _pr_review_state). 397 is the
# main case: the head moved 7b6f2f9 -> e68ea45b, APPROVED on the new one.
_LIVE = {
    397: {"head": "e68ea45b80cdd1498daeef62dcea11df39bdafde",
          "pr_state": "OPEN", "head_kind": "accept"},
    401: {"head": "aaa1111" + "0" * 33,
          "pr_state": "OPEN", "head_kind": "accept"},
    402: {"head": "bbb2222" + "0" * 33,
          "pr_state": "OPEN", "head_kind": "reject"},
    403: {"head": "ccc3333" + "0" * 33,
          "pr_state": "MERGED", "head_kind": None},
    405: None,   # gh failure: conservatively not retired
}

TASKS = [
    # S1 the head moved
    {"id": "t_s1", "status": "ready", "priority": 80,
     "title": "[coder] PR #397 (head 7b6f2f9): rework by verdict "
              "(watchdog auto-arm)",
     "key": "coder-pr397-rework-7b6f2f9", "age": 13 * 3600},
    # S2 PASS on the same head
    {"id": "t_s2", "status": "ready", "priority": 80,
     "title": "[coder] PR #401 (head aaa1111): rework by verdict",
     "key": "coder-pr401-rework-aaa1111", "age": 3600},
    # S3 a live CR on the same head: the lift works as before
    {"id": "t_s3", "status": "ready", "priority": 80,
     "title": "[coder] PR #402 (head bbb2222): rework by verdict",
     "key": "coder-pr402-rework-bbb2222", "age": 3600},
    # S4 PR merged
    {"id": "t_s4", "status": "ready", "priority": 80,
     "title": "[coder] PR #403 (head ccc3333): rework by verdict",
     "key": "coder-pr403-rework-ccc3333", "age": 7200},
    # S5 no head named (verdict-fix key, title without a sha)
    {"id": "t_s5", "status": "ready", "priority": 80,
     "title": "Fix PR #404: correct the promises text",
     "key": "reviewer-PR404-rework-verdict-987654", "age": 1800},
    # S6 gh failure
    {"id": "t_s6", "status": "ready", "priority": 80,
     "title": "[coder] PR #405 (head ddd4444): rework by verdict",
     "key": "coder-pr405-rework-ddd4444", "age": 1800},
    # S7 a running arm on a stale head: untouched
    {"id": "t_s7", "status": "running", "priority": 80,
     "title": "[coder] PR #397 (head 7b6f2f9): rework by verdict",
     "key": "coder-pr397-rework-7b6f2f9-r1", "age": 600},
]


def load_bw(src=None, tag="65"):
    """board-watchdog module from the real file or from the mutated src."""
    sbx = tempfile.mkdtemp(prefix="bw-sbx-rework-stale-")
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


def make_run(db, calls, milestones=OPEN_MILESTONES):
    """run stub: gh api from the fixture, kanban edits (comment/edit/
    archive) are applied to the test database so re-reads can verify."""

    def _run(cmd, timeout=120):
        calls.append(cmd)
        if cmd[:2] == ["gh", "api"]:
            return milestones
        if len(cmd) > 5 and cmd[1] == "kanban":
            action, tid = cmd[4], cmd[5]
            con = sqlite3.connect(db, timeout=10)
            try:
                if action == "comment":
                    con.execute(
                        "insert into task_comments (task_id, body,"
                        " created_at) values (?,?,?)",
                        (tid, cmd[6], int(time.time())))
                elif action == "edit":
                    if "--priority" in cmd:
                        con.execute(
                            "update tasks set priority=? where id=?",
                            (int(cmd[cmd.index("--priority") + 1]), tid))
                elif action == "archive":
                    con.execute(
                        "update tasks set status='archived' where id=?",
                        (tid,))
                con.commit()
            finally:
                con.close()
        return "ok"

    return _run


def write_admission(sbx, slug, issues, pr_issues, pr_milestones=None):
    with open(os.path.join(sbx, slug, "admission-approved.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"updated": "test", "issues": issues,
                   "prs": sorted(int(n) for n in pr_issues),
                   "pr_issues": pr_issues,
                   "pr_milestones": pr_milestones or {}}, fh)


def battery(mod):
    """S1-S7 + idempotency. Returns the list of problems."""
    problems = []

    def want(name, cond, detail=""):
        print("%s %s%s" % ("ok  " if cond else "FAIL", name,
                           (" :: %s" % (detail,)) if detail and not cond
                           else ""))
        if not cond:
            problems.append(name)

    slug, db, _ = h.make_board(tasks=TASKS)
    mod.BOARDS = os.path.dirname(os.path.dirname(db))
    write_admission(mod.BOARDS, slug,
                    issues={"500": "2.4.0"},
                    pr_issues={"397": ["500"], "401": ["500"],
                               "402": ["500"], "403": ["500"],
                               "404": ["500"], "405": ["500"]})
    calls = []
    real_run, real_repo, real_st = mod.run, mod.board_repo, \
        mod._pr_review_state
    mod.run = make_run(db, calls)
    mod.board_repo = lambda s: "acme/widget"
    mod._pr_review_state = lambda repo, number: _LIVE.get(number)
    try:
        found = mod.rework_priority_lift(slug)
    except Exception as exc:
        problems.append("S0: rework_priority_lift crashed: %s: %s"
                        % (type(exc).__name__, exc))
        return problems
    finally:
        mod.run, mod.board_repo, mod._pr_review_state = \
            real_run, real_repo, real_st

    con = sqlite3.connect(db)
    state = {}
    for tid in ("t_s1", "t_s2", "t_s3", "t_s4", "t_s5", "t_s6", "t_s7"):
        row = con.execute("select status, priority from tasks where id=?",
                          (tid,)).fetchone()
        state[tid] = row
    stale_marks = {r[0] for r in con.execute(
        "select task_id from task_comments where body like ?",
        (mod.REWORK_STALE_MARKER + "%",))}
    lift_marks = {r[0] for r in con.execute(
        "select task_id from task_comments where body like ?",
        (mod.REWORK_LIFT_MARKER + "%",))}
    con.close()

    # --- retirement ------------------------------------------------------
    want("S1 head moved (#397): retired, priority not raised",
         state["t_s1"] == ("archived", 80), str(state))
    want("S2 PASS on the same head: retired",
         state["t_s2"] == ("archived", 80), str(state))
    want("S3 live CR on the same head: raised 80 -> 90, alive",
         state["t_s3"] == ("ready", 90), str(state))
    want("S4 PR merged: retired",
         state["t_s4"] == ("archived", 80), str(state))
    want("S5 no head named: conservatively raised, alive",
         state["t_s5"] == ("ready", 90), str(state))
    want("S6 gh failure: conservatively raised, alive",
         state["t_s6"] == ("ready", 90), str(state))
    want("S7 running untouched",
         state["t_s7"] == ("running", 80), str(state))
    want("S8 retirement marker on exactly the three retired cards",
         stale_marks == {"t_s1", "t_s2", "t_s4"}, str(stale_marks))
    want("S9 lift marker only on the live S3/S5/S6",
         lift_marks == {"t_s3", "t_s5", "t_s6"}, str(lift_marks))
    edits = [c for c in calls if "edit" in c]
    want("S10 edit only for live cards (3, none of the retired)",
         len(edits) == 3 and not any(
             tid in c for c in edits
             for tid in ("t_s1", "t_s2", "t_s4")), str(edits))
    stales = [f for f in found if "rework-stale" in f[0]]
    want("S11 retirement findings for three cards",
         len(stales) == 3, str([f[0] for f in found]))
    text1 = " ".join(f[2] for f in stales if "t_s1" in f[0])
    want("S12 the retirement text names the old and the new head",
         "7b6f2f9" in text1 and "e68ea45" in text1, text1)

    # --- idempotency: a repeated pass is silent ----------------------------
    calls2 = []
    mod.run = make_run(db, calls2)
    mod.board_repo = lambda s: "acme/widget"
    mod._pr_review_state = lambda repo, number: _LIVE.get(number)
    try:
        mod.rework_priority_lift(slug)
    finally:
        mod.run, mod.board_repo, mod._pr_review_state = \
            real_run, real_repo, real_st
    want("S13 a repeated pass does nothing",
         not [c for c in calls2 if "edit" in c or "archive" in c],
         str(calls2))

    # --- dry-run: retirement only as a finding ----------------------------
    slug2, db2, _ = h.make_board(tasks=[TASKS[0]])
    mod.BOARDS = os.path.dirname(os.path.dirname(db2))
    write_admission(mod.BOARDS, slug2, issues={"500": "2.4.0"},
                    pr_issues={"397": ["500"]})
    calls3 = []
    mod.run = make_run(db2, calls3)
    mod.board_repo = lambda s: "acme/widget"
    mod._pr_review_state = lambda repo, number: _LIVE.get(number)
    try:
        dry = mod.rework_priority_lift(slug2, apply=False)
    finally:
        mod.run, mod.board_repo, mod._pr_review_state = \
            real_run, real_repo, real_st
    con = sqlite3.connect(db2)
    row = con.execute("select status, priority from tasks where"
                      " id='t_s1'").fetchone()
    con.close()
    want("S14 dry-run neither archives nor edits",
         row == ("ready", 80) and not calls3, str((row, calls3)))
    want("S15 dry-run returns the finding [dry-run: not archived]",
         any("dry-run: not archived" in f[2] for f in dry), str(dry))
    return problems


def main():
    mod, _ = load_bw()
    problems = battery(mod)

    # mutant: retirement switched off, the stale arm is raised again
    src = open(BW_PATH, encoding="utf-8").read()
    mut = src.replace("why_stale = _rework_arm_stale(slug, number,",
                      "why_stale = None and _rework_arm_stale(slug, number,")
    assert mut != src, "mutant 1 did not apply"
    m1, _ = load_bw(mut, tag="65m1")
    if not battery(m1):
        problems.append("MUT1: the battery did not turn red (retirement switched off)")

    print("\nTOTAL rework-arm-stale: %s" % ("OK" if not problems
                                    else "FAIL %s" % "; ".join(problems)))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
