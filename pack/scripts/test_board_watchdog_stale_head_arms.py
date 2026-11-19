#!/usr/bin/env python3
"""Review and WCFY arms on a stale head, and a PR with a live executor.

Why: a reviewer arm was created for a PR head one hour without a verdict
while the executor card kept working and pushing fixes; the reviewer would
have taken a head that no longer existed. The executor card named neither
the PR nor the head (it was created before the PR was opened and is linked
through the PR's closing issue).

Rules:
  1. Review AND WCFY arms on a head that moved on are retired in any status
     except running. A review arm is also retired on a PR that is not
     OPEN; a WCFY arm on a CLOSED PR and when a fresh WCFY already exists on
     the exact head (MERGED does not retire it: a WCFY after the merge still
     makes sense).
  2. The review layer creates no review card while the PR has a live
     executor (a running/ready coder card that has not delivered), matched
     by the PR in the title or by the closing issue. Since one PR = one card
     the layer creates no arms at all, including the old safety arm for a
     departed executor; the checks pin that.

A mutant (the stale-head retire switched off) must turn the battery red;
it runs in-process on a temporary copy of the script.

Run: python3 test_board_watchdog_stale_head_arms.py
"""
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        print("SKIP: cannot load %s" % path)
        sys.exit(0)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


# Harnesses: the gate battery uses the board lint harness, the layer uses
# test_board_watchdog_review_layer.py (its own board-watchdog in hrl.mod).
h = _load("wd_lint_harness_stale_head", os.path.join(HERE, "test_board_watchdog_board_lint.py"))
hrl = _load("wd_revlayer_harness_stale_head", os.path.join(HERE, "test_board_watchdog_review_layer.py"))

FAILS = []

OPEN_MILESTONES = json.dumps([
    {"title": "1.1.0", "due_on": "2026-12-01T00:00:00Z"},
])

BW_PATH = os.path.join(HERE, "board-watchdog.py")

HEAD_408 = "3816ece2844f" + "0" * 28      # the live head of #408
HEAD_408_OLD = "cfb34e1520f052b39728b52064eb5372fdf28e88"  # the head that moved on
HEAD_406 = "b73830084c2cf00c6c8d9a328ebc9d6f6dee0b55"      # the live WCFY arm head
HEAD_406_OLD = "111aaaa" + "0" * 33
HEAD_412 = "ccc3333" + "0" * 33
HEAD_413 = "ddd4444" + "0" * 33

_LIVE = {
    408: {"head": HEAD_408, "pr_state": "OPEN", "verdict": False,
          "head_kind": None},
    406: {"head": HEAD_406, "pr_state": "OPEN", "verdict": True,
          "head_kind": "accept", "wcfy_at": None, "age": 7200},
    411: {"head": "abc1234" + "0" * 33, "pr_state": "MERGED",
          "verdict": False, "head_kind": None},
    412: {"head": HEAD_412, "pr_state": "OPEN", "verdict": True,
          "head_kind": "accept", "age": 3600,
          "wcfy_at": time.time() - 1800},   # fresh: younger than the head
    413: {"head": HEAD_413, "pr_state": "MERGED", "verdict": True,
          "head_kind": "accept", "wcfy_at": None, "age": 7200},
    414: None,                              # gh failure
}

TASKS = [
    # B1: an arm on the departed head cfb34e1, ready to be handed out
    {"id": "t_b1", "status": "ready", "assignee": "reviewer",
     "priority": 100,
     "title": "[reviewer] PR #408 (head cfb34e1): review of a head without "
              "a verdict (watchdog auto-arm)",
     "key": "reviewer-pr408-cfb34e1", "age": 1800},
    # B2 the same class in blocked (any status except running)
    {"id": "t_b2", "status": "blocked", "assignee": "reviewer",
     "priority": 100,
     "title": "[reviewer] PR #408 (head cfb34e1): review of the head",
     "key": "reviewer-pr408-cfb34e1-r1", "age": 1800},
    # B3 control: an arm on the LIVE head of #408 without a verdict: alive
    {"id": "t_b3", "status": "ready", "assignee": "reviewer",
     "priority": 100,
     "title": "[reviewer] PR #408 (head 3816ece): review of the head",
     "key": "reviewer-pr408-3816ece", "age": 1800},
    # B4 a review arm on a MERGED PR: retired
    {"id": "t_b4", "status": "ready", "assignee": "reviewer",
     "priority": 100,
     "title": "[reviewer] PR #411 (head abc1234): review of the head",
     "key": "reviewer-pr411-abc1234", "age": 1800},
    # B5 a WCFY arm on the departed head of #406: retired
    {"id": "t_b5", "status": "ready", "assignee": "coder",
     "priority": 95,
     "title": "[coder] PR #406 (head 111aaaa): fresh \"What changes for "
              "you\" (watchdog auto-arm)",
     "key": "coder-pr406-wcfy-111aaaa", "age": 1800},
    # B6 control: a WCFY arm on the live head, the WCFY is old: alive
    {"id": "t_b6", "status": "ready", "assignee": "coder",
     "priority": 95,
     "title": "[coder] PR #406 (head b738300): fresh \"What changes for "
              "you\" (watchdog auto-arm)",
     "key": "coder-pr406-wcfy-b738300", "age": 1800},
    # B7 a WCFY arm on the live head with a FRESH WCFY: retired
    {"id": "t_b7", "status": "scheduled", "assignee": "coder",
     "priority": 95,
     "title": "[coder] PR #412 (head ccc3333): fresh \"What changes for "
              "you\" (watchdog auto-arm)",
     "key": "coder-pr412-wcfy-ccc3333", "age": 1800},
    # B8 control: a WCFY arm on a MERGED PR: alive (a WCFY after merge makes sense)
    {"id": "t_b8", "status": "ready", "assignee": "coder",
     "priority": 95,
     "title": "[coder] PR #413 (head ddd4444): fresh \"What changes for "
              "you\" (watchdog auto-arm)",
     "key": "coder-pr413-wcfy-ddd4444", "age": 1800},
    # B9 a running WCFY arm: not touched
    {"id": "t_b9", "status": "running", "assignee": "coder",
     "priority": 95,
     "title": "[coder] PR #412 (head ccc3333): WCFY",
     "key": "coder-pr412-wcfy-ccc3333-r1", "age": 600},
    # B10 gh failure: both classes stay alive (conservative)
    {"id": "t_b10", "status": "ready", "assignee": "reviewer",
     "priority": 100,
     "title": "[reviewer] PR #414 (head eee5555): review of the head",
     "key": "reviewer-pr414-eee5555", "age": 1800},
    {"id": "t_b10w", "status": "ready", "assignee": "coder",
     "priority": 95,
     "title": "[coder] PR #414 (head eee5555): WCFY",
     "key": "coder-pr414-wcfy-eee5555", "age": 1800},
    # B11 non-arm coder keys (rework) are not touched
    {"id": "t_b11", "status": "ready", "assignee": "coder",
     "priority": 90,
     "title": "[coder] rework by verdict PR #408",
     "key": "coder-pr408-rework-3816ece", "age": 1800},
]


def load_bw(src=None, tag="stale_head"):
    """The board-watchdog module from the real file or from the mutated src."""
    import shutil
    sbx = tempfile.mkdtemp(prefix="bw-sbx-stale-head-")
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


def make_run(db, calls):
    def _run(cmd, timeout=120):
        calls.append(cmd)
        if cmd[:2] == ["gh", "api"]:
            return OPEN_MILESTONES
        if len(cmd) > 5 and cmd[1] == "kanban":
            action, tid = cmd[4], cmd[5]
            con = sqlite3.connect(db, timeout=10)
            try:
                if action == "comment":
                    con.execute(
                        "insert into task_comments (task_id, body,"
                        " created_at) values (?,?,?)",
                        (tid, cmd[6], int(time.time())))
                elif action == "archive":
                    con.execute(
                        "update tasks set status='archived' where id=?",
                        (tid,))
                con.commit()
            finally:
                con.close()
        return "ok"

    return _run


def battery(mod):
    problems = []

    def want(name, cond, detail=""):
        print("%s %s%s" % ("ok  " if cond else "FAIL", name,
                           (" :: %s" % (detail,)) if detail and not cond
                           else ""))
        if not cond:
            problems.append(name)

    slug, db, _ = h.make_board(tasks=TASKS)
    mod.BOARDS = os.path.dirname(os.path.dirname(db))
    calls = []
    real_run, real_repo, real_st = mod.run, mod.board_repo, \
        mod._pr_review_state
    mod.run = make_run(db, calls)
    mod.board_repo = lambda s: "acme/widget"
    mod._pr_review_state = lambda repo, number: _LIVE.get(number)
    try:
        found = mod.review_arm_liveness_gate(slug)
    except Exception as exc:
        problems.append("B0: review_arm_liveness_gate failed: %s: %s"
                        % (type(exc).__name__, exc))
        return problems
    finally:
        mod.run, mod.board_repo, mod._pr_review_state = \
            real_run, real_repo, real_st

    con = sqlite3.connect(db)
    state = {tid: con.execute(
        "select status, priority from tasks where id=?", (tid,)).fetchone()
        for tid in ("t_b1", "t_b2", "t_b3", "t_b4", "t_b5", "t_b6",
                    "t_b7", "t_b8", "t_b9", "t_b10", "t_b10w", "t_b11")}
    marks = {r[0] for r in con.execute(
        "select task_id from task_comments where body like ?",
        (mod.REVIEW_ARM_STALE_MARKER + "%",))}
    wmarks = {r[0] for r in con.execute(
        "select task_id from task_comments where body like ?",
        (mod.WCFY_ARM_STALE_MARKER + "%",))}
    con.close()

    # --- retiring arms ----------------------------------------------------
    want("B1 #408: the arm on the departed head cfb34e1 (ready) is retired",
         state["t_b1"] == ("archived", 100), str(state))
    want("B2 #408: the same class in blocked is retired",
         state["t_b2"] == ("archived", 100), str(state))
    want("B3 control: the arm on the live head 3816ece without a verdict is alive",
         state["t_b3"] == ("ready", 100), str(state))
    want("B4 the review arm on a MERGED PR is retired",
         state["t_b4"] == ("archived", 100), str(state))
    want("B5 the WCFY arm on the departed head of #406 is retired",
         state["t_b5"] == ("archived", 95), str(state))
    want("B6 control: the WCFY arm on the live head with an old WCFY is alive",
         state["t_b6"] == ("ready", 95), str(state))
    want("B7 the WCFY arm with a fresh WCFY on the head (scheduled) is retired",
         state["t_b7"] == ("archived", 95), str(state))
    want("B8 control: the WCFY arm on a MERGED PR is alive",
         state["t_b8"] == ("ready", 95), str(state))
    want("B9 the running WCFY arm is not touched",
         state["t_b9"] == ("running", 95), str(state))
    want("B10 gh failure: review and WCFY arms conservatively stay alive",
         state["t_b10"] == ("ready", 100)
         and state["t_b10w"] == ("ready", 95), str(state))
    want("B11 the rework key is not touched",
         state["t_b11"] == ("ready", 90), str(state))
    want("B12 the review marker is exactly on B1/B2/B4",
         marks == {"t_b1", "t_b2", "t_b4"}, str(marks))
    want("B13 the WCFY marker is exactly on B5/B7",
         wmarks == {"t_b5", "t_b7"}, str(wmarks))
    stales = [f for f in found if "rev-arm-stale" in f[0]]
    want("B14 retire findings for five cards",
         len(stales) == 5, str([f[0] for f in found]))
    text1 = " ".join(f[2] for f in stales if "t_b1" in f[0])
    want("B15 the B1 text names both heads",
         "cfb34e1" in text1 and "3816ece" in text1, text1)

    # --- idempotency ------------------------------------------------------
    calls2 = []
    mod.run = make_run(db, calls2)
    mod.board_repo = lambda s: "acme/widget"
    mod._pr_review_state = lambda repo, number: _LIVE.get(number)
    try:
        mod.review_arm_liveness_gate(slug)
    finally:
        mod.run, mod.board_repo, mod._pr_review_state = \
            real_run, real_repo, real_st
    want("B16 a repeated pass does nothing",
         not [c for c in calls2 if "comment" in c or "archive" in c],
         str(calls2))
    return problems


# === Part 2: the layer does not arm while an executor is live ==============

HEAD_C1 = "3816ece2844f" + "0" * 28
HEAD_C2 = "f00ba12" + "0" * 33


def layer_scenario(prs, tasks=(), closing=None):
    """The layer with a stubbed closingIssuesReferences gh call."""
    slug, tmp, old = hrl.make_board(tasks)
    calls = []
    real_run, real_repo, real_ci = hrl.mod.run, hrl.mod.board_repo, \
        hrl.mod._pr_closing_issues
    inner = hrl.fake_run(prs, calls)

    def _run(cmd, timeout=120):
        if cmd[:2] == ["gh", "pr"] \
                and "closingIssuesReferences" in cmd:
            return json.dumps({"closingIssuesReferences": [
                {"number": n} for n in
                (closing or {}).get(int(cmd[3]), [])]})
        return inner(cmd, timeout)

    hrl.mod.run = _run
    hrl.mod.board_repo = lambda s: "acme/widget"
    try:
        found = hrl.mod.review_layer_check(slug)
    finally:
        hrl.mod.run = real_run
        hrl.mod.board_repo = real_repo
        hrl.mod.BOARDS = old
    found = [f for f in found if ":r36:ms-orphan:" not in f[0]]
    return found, calls, tmp


def layer_battery():
    problems = []

    def want(name, cond, detail=""):
        print("%s %s%s" % ("ok  " if cond else "FAIL", name,
                           (" :: %s" % (detail,)) if detail and not cond
                           else ""))
        if not cond:
            problems.append(name)

    c3_task = {
        "id": "t_c3", "status": "running", "assignee": "coder",
        "priority": 80,
        "title": "[coder] issue #120 C3: REST /api/client-connections on "
                 "both stacks + Clients tab",
        "key": None, "age": 7200,
        "body": "Issue: #120 (milestone 1.1.0, severity: critical). "
                "Layer C3 of 4. ARCHITECT DECISIONS (do not change).",
    }
    # C1: the executor is running, linked only through the closing issue,
    # the card names neither the PR nor the head
    found, calls, _ = layer_scenario(
        {408: hrl.pr_json(HEAD_C1, 2 * 3600)},
        tasks=[c3_task], closing={408: [120]})
    want("C1 #408: a live executor by closing issue: NO arm is created",
         not hrl.creates(calls), str(hrl.creates(calls)))
    txt = " ".join(f[2] for f in found if "arm-live-exec" in f[0])
    want("C2 no safety-arm finding (the mechanism is removed)",
         txt == "", txt[:80])
    # C3: the executor left the board (done): there is no safety arm any
    # more (one PR = one card), no cards are created at all.
    found, calls, _ = layer_scenario(
        {408: hrl.pr_json(HEAD_C1, 2 * 3600)},
        tasks=[dict(c3_task, status="done")],
        closing={408: [120]})
    want("C3 executor done: NO arm is created (the safety arm is removed)",
         not hrl.creates(calls), str(hrl.creates(calls)))
    # C4: the executor is running, the PR is named in the card TITLE
    t_direct = dict(c3_task, id="t_c4",
                    title="[coder] fix PR #408: repairs the IT gate")
    found, calls, _ = layer_scenario(
        {408: hrl.pr_json(HEAD_C1, 2 * 3600)},
        tasks=[t_direct], closing={})
    want("C4 executor running, PR in the title: NO arm is created",
         not hrl.creates(calls), str(hrl.creates(calls)))
    # C5: a service card (issue-close, waiting for the merge) is not an executor
    t_wait = dict(c3_task, id="t_c5",
                  title="[issue-close] close issue #120 after the merge of "
                        "PR #408")
    found, calls, _ = layer_scenario(
        {408: hrl.pr_json(HEAD_C1, 2 * 3600)},
        tasks=[t_wait], closing={408: [120]})
    want("C5 a service wait for the merge: NO arm is created",
         not hrl.creates(calls), str(hrl.creates(calls)))
    # C6: the executor in ready (between runs) is live too, no arm
    t_ready = dict(c3_task, status="ready")
    found, calls, _ = layer_scenario(
        {408: hrl.pr_json(HEAD_C1, 2 * 3600)},
        tasks=[t_ready], closing={408: [120]})
    want("C6 executor ready: NO arm is created",
         not hrl.creates(calls), str(hrl.creates(calls)))
    return problems


def main():
    mod, _ = load_bw()
    problems = battery(mod)
    problems += layer_battery()

    # mutant: retiring on a departed head is switched off
    src = open(BW_PATH, encoding="utf-8").read()
    mut = src.replace(
        "if live_head7 and live_head7 != head7:",
        "if False and live_head7 and live_head7 != head7:")
    assert mut != src, "mutant 1 did not apply"
    m1, _ = load_bw(mut, tag="stale_head_m1")
    if not battery(m1):
        problems.append("MUT1: the battery did not turn red (head retire "
                        "switched off)")

    # The mutant for create suppression by a live executor is gone with
    # the create mechanism itself.

    print("\nTOTAL stale head arms: %s" % ("OK" if not problems
                                    else "FAIL %s" % "; ".join(problems)))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
