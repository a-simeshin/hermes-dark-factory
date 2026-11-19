#!/usr/bin/env python3
"""No review is raised on a PR head that is under rebase.

Why: the watchdog once created a p100 reviewer card for a PR head without
a verdict although the PR was CONFLICTING and its rebase card was
deliberately blocked until a predecessor PR merged. A conflicting PR has no
merge ref, no pull_request CI run exists, so a PASS on that head is
impossible, and the head changes after the rebase anyway.

Rules:
  1. The review layer creates no review card on a CONFLICTING PR or on a
     PR with a live rebase card (any status, including blocked while
     waiting for a stack predecessor); such a head does not hold the review
     layer (no parking of low ready cards). Since one PR = one card the
     layer creates no review arms at all; the checks pin that.
  2. Review and WCFY arms already created on a CONFLICTING PR are retired
     by review_arm_liveness_gate.

Run: python3 test_board_watchdog_review_conflicting_pr.py
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


h = _load("wd_lint_harness_conflicting",
          os.path.join(HERE, "test_board_watchdog_board_lint.py"))
hrl = _load("wd_revlayer_harness_conflicting",
            os.path.join(HERE, "test_board_watchdog_review_layer.py"))

FAILS = []

OPEN_MILESTONES = json.dumps([
    {"title": "1.1.0", "due_on": "2026-12-01T00:00:00Z"},
])

BW_PATH = os.path.join(HERE, "board-watchdog.py")

HEAD_413 = "7ecc3be" + "0" * 33
HEAD_408 = "3816ece2844f" + "0" * 28
HEAD_412 = "aaa2222" + "0" * 33


def layer_prs(head, age_s, mergeable):
    p = json.loads(hrl.pr_json(head, age_s))
    p["mergeable"] = mergeable
    return json.dumps(p)


def want(name, cond, detail=""):
    print("%s %s%s" % ("ok  " if cond else "FAIL", name,
                       (" :: %s" % (detail,)) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


# === Part A: the layer does not arm a head under rebase ===================

def part_a():
    # A1: a CONFLICTING PR without cards: no arm, no stackwait finding
    found, calls, _ = hrl.with_scenario(
        {413: layer_prs(HEAD_413, 2.2 * 3600, "CONFLICTING")})
    want("A1 CONFLICTING #413: no review card is created",
         not hrl.creates(calls), str(calls))
    # stackwait findings were removed together with the arm.
    want("A1 no stackwait finding (the mechanism is removed)",
         not any("stackwait" in f[0] for f in found),
         str([f[0] for f in found]))

    # A2 control: the same PR MERGEABLE without a rebase card: still no arm
    found, calls, _ = hrl.with_scenario(
        {413: layer_prs(HEAD_413, 2.2 * 3600, "MERGEABLE")})
    want("A2 control MERGEABLE: nothing is created (one PR = one card)",
         not hrl.creates(calls), str(calls))

    # A3: a MERGEABLE PR with a live blocked rebase card (the card was
    # created before the conflict cleared): no arm
    found, calls, _ = hrl.with_scenario(
        {408: layer_prs(HEAD_408, 2.2 * 3600, "MERGEABLE")},
        tasks=[{"id": "t_reb", "status": "blocked",
                "assignee": "coder", "priority": 96,
                "title": "rebase PR #408 onto develop/2.x@c8c4f24",
                "key": "coder-pr408-rebase-c8c4f24",
                "body": "PR #408 conflicts with the head of develop/2.x"}])
    want("A3 a live rebase card blocks a review auto-arm",
         not hrl.creates(calls), str(calls))
    want("A3 no stackwait finding",
         not any("stackwait" in f[0] for f in found),
         str([(f[0], f[2][:120]) for f in found]))

    # A4: a head under rebase does not hold the layer: ready p40 is not parked
    tasks = [{"id": "t_low", "status": "ready", "assignee": "coder",
              "priority": 40, "title": "other milestone work",
              "body": "a card unrelated to #413"}]
    found, calls_wait, _ = hrl.with_scenario(
        {413: layer_prs(HEAD_413, 2.2 * 3600, "CONFLICTING")},
        tasks=tasks)
    want("A4 the layer is empty: ready p40 is not parked",
         not [c for c in calls_wait
              if len(c) > 4 and c[4] == "schedule"], str(calls_wait))
    found, calls_arm, _ = hrl.with_scenario(
        {413: layer_prs(HEAD_413, 2.2 * 3600, "MERGEABLE")},
        tasks=tasks)
    want("A4 control: no parking without an arm either",
         not [c for c in calls_arm
              if len(c) > 4 and c[4] == "schedule"], str(calls_arm))


# === Part B: retiring arms already created on a CONFLICTING PR ===========

_LIVE72 = {
    413: {"head": HEAD_413, "pr_state": "OPEN", "mergeable": "CONFLICTING",
          "verdict": False, "head_kind": None},
    408: {"head": HEAD_408, "pr_state": "OPEN", "mergeable": "MERGEABLE",
          "verdict": False, "head_kind": None},
    412: {"head": HEAD_412, "pr_state": "OPEN", "mergeable": "CONFLICTING",
          "verdict": False, "head_kind": None},
}

TASKS_B = [
    {"id": "t_r1", "status": "ready", "assignee": "reviewer",
     "priority": 100,
     "title": "[reviewer] PR #413 (head 7ecc3be): review of a head without "
              "a verdict (watchdog auto-arm)",
     "key": "reviewer-pr413-7ecc3be", "age": 1800},
    {"id": "t_r2", "status": "ready", "assignee": "coder", "priority": 95,
     "title": "[coder] PR #413 (head 7ecc3be): fresh \"What changes for "
              "you\" (watchdog auto-arm)",
     "key": "coder-pr413-wcfy-7ecc3be", "age": 1800},
    # control: an arm on a MERGEABLE PR without a verdict stays alive
    {"id": "t_r3", "status": "ready", "assignee": "reviewer",
     "priority": 100,
     "title": "[reviewer] PR #408 (head 3816ece): review of the head",
     "key": "reviewer-pr408-3816ece", "age": 1800},
    # running is not touched (the run is already paid for)
    {"id": "t_r4", "status": "running", "assignee": "reviewer",
     "priority": 100,
     "title": "[reviewer] PR #412 (head aaa2222): review of the head",
     "key": "reviewer-pr412-aaa2222", "age": 600},
]


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


def part_b():
    spec = importlib.util.spec_from_file_location("bw_mod_conflicting", BW_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bw_mod_conflicting"] = mod
    spec.loader.exec_module(mod)
    sbx = tempfile.mkdtemp(prefix="bw-sbx-conflicting-")
    mod.STATE = os.path.join(sbx, "state.json")
    mod.BACKLOG_CACHE = os.path.join(sbx, "backlog.json")

    slug, db, _ = h.make_board(tasks=TASKS_B)
    mod.BOARDS = os.path.dirname(os.path.dirname(db))
    calls = []
    real_run, real_repo, real_st = mod.run, mod.board_repo, \
        mod._pr_review_state
    mod.run = make_run(db, calls)
    mod.board_repo = lambda s: "acme/widget"
    mod._pr_review_state = lambda repo, number: _LIVE72.get(number)
    try:
        found = mod.review_arm_liveness_gate(slug)
    finally:
        mod.run, mod.board_repo, mod._pr_review_state = \
            real_run, real_repo, real_st

    con = sqlite3.connect(db)
    state = {tid: con.execute(
        "select status from tasks where id=?", (tid,)).fetchone()[0]
        for tid in ("t_r1", "t_r2", "t_r3", "t_r4")}
    rmarks = {r[0]: r[1] for r in con.execute(
        "select task_id, body from task_comments where body like ?",
        (mod.REVIEW_ARM_STALE_MARKER + "%",))}
    wmarks = {r[0]: r[1] for r in con.execute(
        "select task_id, body from task_comments where body like ?",
        (mod.WCFY_ARM_STALE_MARKER + "%",))}
    con.close()

    want("B1 the review arm on CONFLICTING #413 is retired",
         state["t_r1"] == "archived", str(state))
    want("B2 the reason of B1 names CONFLICTING and the merge ref",
         "CONFLICTING" in rmarks.get("t_r1", "")
         and "merge ref" in rmarks.get("t_r1", ""), rmarks.get("t_r1", ""))
    want("B3 the WCFY arm on CONFLICTING #413 is retired",
         state["t_r2"] == "archived", str(state))
    want("B4 control: the arm on MERGEABLE #408 is alive",
         state["t_r3"] == "ready", str(state))
    want("B5 running is not touched", state["t_r4"] == "running", str(state))
    stales = [f for f in found if "rev-arm-stale" in f[0]]
    want("B6 retire findings for two cards",
         len(stales) == 2, str([f[0] for f in found]))

    # idempotency
    calls2 = []
    mod.run = make_run(db, calls2)
    mod.board_repo = lambda s: "acme/widget"
    mod._pr_review_state = lambda repo, number: _LIVE72.get(number)
    try:
        mod.review_arm_liveness_gate(slug)
    finally:
        mod.run, mod.board_repo, mod._pr_review_state = \
            real_run, real_repo, real_st
    want("B7 a repeated pass does nothing",
         not [c for c in calls2 if "comment" in c or "archive" in c],
         str(calls2))


def main():
    part_a()
    part_b()
    if FAILS:
        print("REVIEW ON CONFLICTING PR: FAILURES: %s" % ", ".join(FAILS))
        sys.exit(1)
    print("REVIEW ON CONFLICTING PR: tests green")


if __name__ == "__main__":
    main()
