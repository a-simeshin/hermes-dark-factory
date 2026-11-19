#!/usr/bin/env python3
"""Rework of an open PR is recognised by structure, not by words.

A rework card titled "[coder] PR #405 rework: pin the notification ref
(RETURN issuecomment-...)" with the key reviewer-pr405-return was never
raised to p90 by rework_priority_lift: the word list ("rework" in the
key, "rework by verdict" / "Fix PR #N" in the title) missed the first new
spelling.

Rules:
  A. The rework class is decided by STRUCTURE (_rework_by_structure): a
     coder card bound to an open PR (number in the key or title, gh
     resolves it as OPEN), a reject verdict on the current head, and the
     card created AFTER the verdict. The title word list stays a hint
     (conservative fallback when gh fails).
  B. One key for all rework generators: coder-pr<N>-rework-<head7> through
     the shared helper board_native_guard.rework_key (watchdog arm,
     pr-hygiene, reviewer on RETURN).
  C. New work on an issue is not raised: an issue number does not resolve
     into an open PR with a reject verdict.
  D. The forms "-return", "rework", "Fix PR", "rework by verdict" and an
     invented wording are all recognised.

A mutant with the structural class switched off must turn the battery red.

Run: python3 test_board_watchdog_rework_by_structure.py
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
spec = importlib.util.spec_from_file_location("wd_lint_harness_rework_structure", HARNESS)
if spec is None or spec.loader is None:
    print("SKIP: cannot load harness")
    sys.exit(0)
h = importlib.util.module_from_spec(spec)
sys.modules["wd_lint_harness_rework_structure"] = h
spec.loader.exec_module(h)

NOW = time.time()
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
BNG_PATH = os.path.join(HERE, "board_native_guard.py")

_VERDICT_AT = NOW - 5400   # verdict 1.5 h ago: the cards are younger

# Live PR states (the shape of _pr_review_state). 405 is the main case:
# OPEN, reject verdict on the current head b0cb893, the card (age 1800) was
# created after the FIRST RETURN; a duplicate of the same RETURN (head_at)
# came LATER than the card and must not disqualify it. 212 is an issue.
_LIVE = {
    405: {"head": "b0cb8936faf8a98e071b0a36abf10a3920835a00",
          "pr_state": "OPEN", "head_kind": "reject",
          "head_reject_at": _VERDICT_AT, "head_at": NOW - 200},
    407: {"head": "777aaaa" + "0" * 33,
          "pr_state": "OPEN", "head_kind": "reject", "head_at": _VERDICT_AT},
    408: {"head": "888bbbb" + "0" * 33,
          "pr_state": "OPEN", "head_kind": "reject", "head_at": _VERDICT_AT},
    409: {"head": "999cccc" + "0" * 33,
          "pr_state": "OPEN", "head_kind": "reject", "head_at": _VERDICT_AT},
    # an open PR without a reject verdict on the head: not a rework
    411: {"head": "aaadddd" + "0" * 33,
          "pr_state": "OPEN", "head_kind": None, "head_at": None},
    # the verdict is NEWER than the card: the work predates RETURN
    412: {"head": "bbbeeee" + "0" * 33,
          "pr_state": "OPEN", "head_kind": "reject", "head_at": NOW - 3600},
    212: None,   # an issue: gh pr view does not resolve it
}

TASKS = [
    # A/D: the word list recognises neither the key nor the title
    {"id": "t_ret", "status": "ready", "priority": 80,
     "title": "[coder] PR #405 rework: pin the notification ref and "
              "the polling stop message (RETURN issuecomment-6983758376)",
     "key": "reviewer-pr405-return", "assignee": "coder", "age": 1800},
    # D: an invented wording no word list ever had
    {"id": "t_invent", "status": "ready", "priority": 80,
     "title": "[coder] PR #409: answer to the reviewer's remarks, bring "
              "the pin tests to evidential strength",
     "key": "reviewer-pr409-revisit", "assignee": "coder", "age": 1800},
    # D: the form "Fix PR" (a hint word) on a "-return" key
    {"id": "t_fix", "status": "ready", "priority": 80,
     "title": "Fix PR #407: correct the promises text",
     "key": "reviewer-pr407-return", "assignee": "coder", "age": 1800},
    # D: the form "rework by verdict" (a hint word)
    {"id": "t_verdict", "status": "ready", "priority": 80,
     "title": "[coder] PR #408 (head 888bbbb): rework by verdict",
     "key": "watch-408-fit", "assignee": "coder", "age": 1800},
    # C: new work on an issue: not raised
    {"id": "t_new", "status": "ready", "priority": 80,
     "title": "[coder] issue #212: new work, traffic capture core",
     "key": "coder-212-core", "assignee": "coder", "age": 1800},
    # C: an open PR without a reject verdict on the head: not a rework
    {"id": "t_nov", "status": "ready", "priority": 80,
     "title": "[coder] PR #411: edits before the first review",
     "key": "fit-411", "assignee": "coder", "age": 1800},
    # A: the card was created BEFORE the verdict: structure does not raise it
    {"id": "t_before", "status": "ready", "priority": 80,
     "title": "[coder] PR #412: an old work card",
     "key": "fit-412", "assignee": "coder", "age": 24 * 3600},
]


def load_bw(src=None, tag="68"):
    """board-watchdog module from the real file or from the mutated src."""
    sbx = tempfile.mkdtemp(prefix="bw-sbx-rework-structure-")
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
    """run stub: gh api from the fixture, kanban edits are applied to the
    test database (re-read verification sees the real effect)."""

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
    """A (structure), B (key), C (negatives), D (forms). Returns problems."""
    problems = []

    def want(name, cond, detail=""):
        print("%s %s%s" % ("ok  " if cond else "FAIL", name,
                           (" :: %s" % (detail,)) if detail and not cond
                           else ""))
        if not cond:
            problems.append(name)

    # the word list really misses this form
    want("A0 the word list misses the '-return' key and the 'rework' title",
         not mod._REWORK_LIFT_KEY_RE.search("reviewer-pr405-return")
         and not mod._REWORK_LIFT_TITLE_RE.search(TASKS[0]["title"])
         and not mod._REWORK_LIFT_KEY_RE.search("reviewer-pr409-revisit")
         and not mod._REWORK_LIFT_TITLE_RE.search(TASKS[1]["title"]),
         "the form is suddenly recognisable by words")

    slug, db, _ = h.make_board(tasks=TASKS)
    mod.BOARDS = os.path.dirname(os.path.dirname(db))
    write_admission(mod.BOARDS, slug,
                    issues={"500": "2.4.0"},
                    pr_issues={str(n): ["500"] for n in
                               (405, 407, 408, 409, 411, 412)})
    calls = []
    real_run, real_repo, real_st = mod.run, mod.board_repo, \
        mod._pr_review_state
    mod.run = make_run(db, calls)
    mod.board_repo = lambda s: "acme/widget"
    mod._pr_review_state = lambda repo, number: _LIVE.get(number)
    try:
        found = mod.rework_priority_lift(slug)
    except Exception as exc:
        problems.append("A1: rework_priority_lift crashed: %s: %s"
                        % (type(exc).__name__, exc))
        return problems
    finally:
        mod.run, mod.board_repo, mod._pr_review_state = \
            real_run, real_repo, real_st

    con = sqlite3.connect(db)
    prio = {tid: (con.execute("select priority from tasks where id=?",
                              (tid,)).fetchone() or [None])[0]
            for tid in ("t_ret", "t_invent", "t_fix", "t_verdict",
                        "t_new", "t_nov", "t_before")}
    marks = {r[0] for r in con.execute(
        "select task_id from task_comments where body like ?",
        (mod.REWORK_LIFT_MARKER + "%",))}
    con.close()

    # --- D: all forms are recognised --------------------------------------
    want("D1 '-return' + 'rework' 80 -> 90 (structure)",
         prio["t_ret"] == 90, "prio=%s" % prio)
    want("D2 invented wording 80 -> 90 (structure)",
         prio["t_invent"] == 90, "prio=%s" % prio)
    want("D3 'Fix PR' on a '-return' key 80 -> 90",
         prio["t_fix"] == 90, "prio=%s" % prio)
    want("D4 'rework by verdict' 80 -> 90",
         prio["t_verdict"] == 90, "prio=%s" % prio)
    want("D5 marker comment on exactly the four raised cards",
         marks == {"t_ret", "t_invent", "t_fix", "t_verdict"}, str(marks))

    # --- A: the structural evidence in the finding --------------------------
    text_ret = " ".join(f[2] for f in found if "t_ret" in f[0])
    want("A2 the finding names the structural sign",
         "structure" in text_ret and "b0cb893" in text_ret, text_ret)

    # --- C: negatives -------------------------------------------------------
    want("C1 new work on an issue not raised: 80",
         prio["t_new"] == 80, "prio=%s" % prio)
    want("C2 open PR without a reject verdict not raised: 80",
         prio["t_nov"] == 80, "prio=%s" % prio)
    want("C3 card older than the verdict not raised: 80",
         prio["t_before"] == 80, "prio=%s" % prio)

    # --- idempotency ---------------------------------------------------------
    calls2 = []
    mod.run = make_run(db, calls2)
    mod.board_repo = lambda s: "acme/widget"
    mod._pr_review_state = lambda repo, number: _LIVE.get(number)
    try:
        mod.rework_priority_lift(slug)
    finally:
        mod.run, mod.board_repo, mod._pr_review_state = \
            real_run, real_repo, real_st
    want("I1 a repeated pass is silent (idempotency)",
         not [c for c in calls2 if "edit" in c], str(calls2))

    # --- dry-run: findings only -----------------------------------------------
    slug2, db2, _ = h.make_board(tasks=[TASKS[0]])
    mod.BOARDS = os.path.dirname(os.path.dirname(db2))
    write_admission(mod.BOARDS, slug2, issues={"500": "2.4.0"},
                    pr_issues={"405": ["500"]})
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
    row = con.execute("select priority from tasks where id='t_ret'") \
        .fetchone()[0]
    con.close()
    want("DRY dry-run does not edit the priority, the finding carries the sign",
         row == 80 and not calls3
         and any("dry-run: priority not raised" in f[2] for f in dry),
         str((row, calls3)))

    # --- B: one key for all generators ---------------------------------------
    bng_spec = importlib.util.spec_from_file_location("bng_rework_structure", BNG_PATH)
    bng = importlib.util.module_from_spec(bng_spec)
    sys.modules["bng_rework_structure"] = bng
    bng_spec.loader.exec_module(bng)
    want("B1 rework_key: head of PR #405",
         bng.rework_key(405, "b0cb8936faf8a98e071b0a36abf10a3920835a00")
         == "coder-pr405-rework-b0cb893",
         bng.rework_key(405, "b0cb8936faf8a98e071b0a36abf10a3920835a00"))
    made = {}
    real_create = mod._kanban_create

    def fake_create(slug_, title, body, assignee, priority, key,
                    skills=(), guard=None):
        made[key] = priority
        return "t_made"

    mod._kanban_create = fake_create
    try:
        mod._rework_arm_card(slug, 397, "7b6f2f9abcdef", {"at": NOW})
    except Exception as exc:
        problems.append("B2: _rework_arm_card crashed: %s: %s"
                        % (type(exc).__name__, exc))
    finally:
        mod._kanban_create = real_create
    want("B3 _rework_arm_card builds the key with the helper and creates at 90",
         made.get("coder-pr397-rework-7b6f2f9") == 90, str(made))
    return problems


def main():
    mod, _ = load_bw()
    problems = battery(mod)

    # mutant: the structural class is off, the "-return" form is lost again
    src = open(BW_PATH, encoding="utf-8").read()
    mut = src.replace("structural = _rework_by_structure(",
                      "structural = None and _rework_by_structure(")
    assert mut != src, "mutant 1 did not apply"
    m1, _ = load_bw(mut, tag="68m1")
    if not battery(m1):
        problems.append("MUT1: the battery did not turn red (structure off)")

    print("\nTOTAL rework-by-structure: %s" % ("OK" if not problems
                                   else "FAIL %s" % "; ".join(problems)))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
