#!/usr/bin/env python3
"""A card that only cites a PR head as context does not count as live work
on that PR, stale rework arms are retired in every status except running,
and the rework template requires a rework response.

Why: a rework of a PR stood for an hour and a half without review. The
review layer skipped the head because a running card of ANOTHER issue
quoted the PR head as the source of its screenshots ("Issue #N: Before/After
from the rework head of PR #M"). Such a card does not push to the PR and
does not request review, so it carries no review cycle. The same incident
left a blocked rework arm on the old head alive, because retiring stale
arms looked only at ready/todo. And the executor that delivered the rework
from a decomposition child never saw the card template, so it neither
answered the verdict in the PR nor requested a new review.

Checks:
  A1 a context cite (own issue + PR head as source) in running creates no
     card and gives no cycle finding (the per-head review arm is gone);
  A2 a real rework card of the PR in running: no card, no cycle finding;
  A3 the context-cite vocabulary: the live form yes, an issue prefix
     without source words no, a PR card without an issue prefix no, a
     source marker in the body yes;
  A4 a context cite next to a PASS verdict creates no card (no WCFY arm);
  B1 a blocked arm on a moved head is retired (comment + archive), the
     reason names the old and the new head;
  B2 a blocked arm on the live head with a change request stays and keeps
     its priority (the lift touches only ready/todo);
  B3 a running arm on a moved head is untouched;
  B4 a repeated pass is silent;
  C1-C3 the rework template requires a rework response for every block
     item, a re-request of review, and puts the duty on any card that
     delivers the rework, including a decomposition child.

Run: python3 test_board_watchdog_context_cite_card.py
Mutant run: BOARD_WATCHDOG_SCRIPT=/path/to/mutant.py python3 test_board_watchdog_context_cite_card.py
"""
import importlib.util
import os
import shutil
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.environ.get("BOARD_WATCHDOG_SCRIPT") or os.path.join(
    HERE, "board-watchdog.py")
os.environ["BOARD_WATCHDOG_SCRIPT"] = SCRIPT
sys.path.insert(0, HERE)

import test_board_watchdog_review_layer as rl  # noqa: E402

mod = rl.mod
assert rl.SCRIPT == SCRIPT, "the harness and the test must load one script"

H154 = rl.HEAD154
H7 = H154[:7]

FAILS = []


def check(name, cond, detail=""):
    print("%s %s%s" % ("ok  " if cond else "FAIL", name,
                       (" :: %s" % (detail,)) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


# A card of another issue #183 that quotes the head of PR #154 as the
# source of its screenshots.
CONTEXT_CITE_VD = {
    "id": "t_vd183", "status": "running", "assignee": "coder",
    "priority": 80, "key": "coder-issue183-visual-delta",
    "title": "Issue #183: Before/After from the rework head of PR #154 (%s) "
             "+ a comment in the issue" % H7,
    "body": "Issue: #183\n\nScreenshots go to issue #183, "
            "PR #154 does not move."}

# Delivery of the rework of PR #154 itself (the PR is the object of the
# work): the suppression is right.
REAL_REWORK = {
    "id": "t_rw", "status": "running", "assignee": "coder",
    "priority": 90, "key": "coder-pr154-rework-delivery",
    "title": "PR #154 (%s): full verify, push the branch, green CI" % H7,
    "body": "Delivery of the rework of PR #154 to the PR branch, head %s." % H7}


def creates(calls):
    return [c for c in calls if len(c) > 4 and c[4] == "create"]


def test_a1_context_cite_does_not_suppress_review_arm():
    found, calls, tmp = rl.with_scenario(
        {154: rl.pr_json(H154, 2 * 3600)}, tasks=[CONTEXT_CITE_VD])
    check("A1 context cite: no card is created (there is no arm)",
          creates(calls) == [],
          str(creates(calls)) + " " + str([f[0] for f in found]))
    check("A1b no cycle finding for #154",
          not any(":cycle:154:" in f[0] for f in found),
          str([f[0] for f in found]))
    shutil.rmtree(tmp)


def test_a2_real_rework_still_suppresses():
    found, calls, tmp = rl.with_scenario(
        {154: rl.pr_json(H154, 2 * 3600)}, tasks=[REAL_REWORK])
    check("A2 real rework: no card, no cycle findings",
          creates(calls) == []
          and not any(":cycle:" in f[0] for f in found),
          str(creates(calls)) + " " + str([f[0] for f in found]))
    shutil.rmtree(tmp)


def test_a3_context_cite_dictionary():
    t = CONTEXT_CITE_VD
    check("A3 the live form (issue + from the rework head of PR #)",
          mod._is_context_cite_card(t["title"], t["body"]))
    check("A3b an issue prefix without source words is NOT a context cite",
          not mod._is_context_cite_card(
              "Issue #96: text fix in PR #154", ""))
    check("A3c a PR card without an issue prefix is NOT a context cite",
          not mod._is_context_cite_card(
              "PR #154: rework by verdict", ""))
    check("A3d a source marker in the body (frames from the head of PR #)",
          mod._is_context_cite_card(
              "Issue #183: Before/After",
              "frames from the head of PR #154, head %s" % H7))


def test_a4_context_cite_does_not_suppress_wcfy_arm():
    verdict = {"body": "## Review verdict\n\n"
                       "APPROVED (PASS) on head `%s`\n\n"
                       "### Calibration\n- contract: ok\n\n"
                       "| Promise | Mutant | Test | Result |\n"
                       "|---|---|---|---|\n"
                       "| NOTICE entry registered | grep registry | "
                       "notice-check | green |" % H154[:8],
               "createdAt": rl.iso(time.time() - 2 * 3600)}
    found, calls, tmp = rl.with_scenario(
        {154: rl.pr_json(H154, 2 * 3600, comments=[verdict])},
        tasks=[CONTEXT_CITE_VD])
    check("A4 context cite: no card is created (the WCFY arm is gone)",
          creates(calls) == [], str(creates(calls)))
    shutil.rmtree(tmp)


# --- part B: retiring blocked arms (rework lift harness) --------------------

LINT = os.path.join(HERE, "test_board_watchdog_board_lint.py")
spec = importlib.util.spec_from_file_location("wd_lint_harness_cite", LINT)
h = importlib.util.module_from_spec(spec)
sys.modules["wd_lint_harness_cite"] = h
spec.loader.exec_module(h)

_LIVE = {
    # the head moved 30c4754 -> ffb36ce, no verdict on the new head
    398: {"head": "ffb36ce" + "f" * 33,
          "pr_state": "OPEN", "head_kind": None},
    402: {"head": "bbb2222" + "0" * 33,
          "pr_state": "OPEN", "head_kind": "reject"},
}

TASKS_B = [
    # a blocked arm on the old head
    {"id": "t_b1", "status": "blocked", "priority": 90,
     "title": "Fix PR #398 (head 30c4754): Apps flow hangs after "
              "sandbox-proxy-ready + 3 unpinned promises of the PR body",
     "key": "reviewer-PR398-rework-verdict-6976691960", "age": 5 * 3600},
    # blocked on the live head with a change request: not retired, the lift
    # does not touch blocked
    {"id": "t_b2", "status": "blocked", "priority": 80,
     "title": "[coder] PR #402 (head bbb2222): rework by verdict",
     "key": "coder-pr402-rework-bbb2222", "age": 3600},
    # running on the old head: untouched
    {"id": "t_b3", "status": "running", "priority": 80,
     "title": "[coder] PR #398 (head 30c4754): rework by verdict",
     "key": "coder-pr398-rework-30c4754", "age": 600},
]


def make_run(db, calls):
    def _run(cmd, timeout=120):
        calls.append(cmd)
        if cmd[:2] == ["gh", "api"]:
            return "[]"
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


def load_bw():
    sbx = tempfile.mkdtemp(prefix="bw-sbx-cite-")
    sp = importlib.util.spec_from_file_location("bw_mod_cite", SCRIPT)
    m = importlib.util.module_from_spec(sp)
    sys.modules["bw_mod_cite"] = m
    sp.loader.exec_module(m)
    m.STATE = os.path.join(sbx, "state.json")
    m.BACKLOG_CACHE = os.path.join(sbx, "backlog.json")
    if hasattr(m, "milestone_bands"):
        m.milestone_bands.CACHE_PATH = os.path.join(sbx, "ms.json")
        m.milestone_bands._MEMO = {}
    return m, sbx


def test_b_blocked_stale_arms():
    m, sbx = load_bw()
    slug, db, _ = h.make_board(tasks=TASKS_B)
    m.BOARDS = os.path.dirname(os.path.dirname(db))
    calls = []
    real_run, real_repo, real_st = m.run, m.board_repo, m._pr_review_state
    m.run = make_run(db, calls)
    m.board_repo = lambda s: "acme/demo"
    m._pr_review_state = lambda repo, number: _LIVE.get(number)
    try:
        found = m.rework_priority_lift(slug)
    finally:
        m.run, m.board_repo, m._pr_review_state = \
            real_run, real_repo, real_st
    con = sqlite3.connect(db)
    state = {t["id"]: con.execute(
        "select status, priority from tasks where id=?",
        (t["id"],)).fetchone() for t in TASKS_B}
    marks = {r[0] for r in con.execute(
        "select task_id from task_comments where body like ?",
        (m.REWORK_STALE_MARKER + "%",))}
    con.close()
    check("B1 a blocked arm on a moved head is retired",
          state["t_b1"] == ("archived", 90)
          and "t_b1" in marks, str(state) + " " + str(marks))
    st_b1 = [f[2] for f in found if "t_b1" in f[0]]
    check("B1b the reason names the old and the new head",
          any("30c4754" in t and "ffb36ce" in t for t in st_b1),
          str(st_b1))
    check("B2 blocked on the live head: alive, priority untouched",
          state["t_b2"] == ("blocked", 80) and "t_b2" not in marks,
          str(state))
    check("B3 running on the old head: untouched",
          state["t_b3"] == ("running", 80) and "t_b3" not in marks,
          str(state))
    calls2 = []
    m.run = make_run(db, calls2)
    m.board_repo = lambda s: "acme/demo"
    m._pr_review_state = lambda repo, number: _LIVE.get(number)
    try:
        m.rework_priority_lift(slug)
    finally:
        m.run, m.board_repo, m._pr_review_state = \
            real_run, real_repo, real_st
    check("B4 a repeated pass is silent",
          not [c for c in calls2 if "edit" in c or "archive" in c],
          str(calls2))
    shutil.rmtree(sbx, ignore_errors=True)


def test_c_template_requires_rework_response():
    m, sbx = load_bw()
    # svc_priority inside _rework_arm_card reads
    # BOARDS/<slug>/admission-approved.json: point BOARDS at the sandbox.
    m.BOARDS = sbx
    captured = []
    real_create = m._kanban_create

    def fake_create(slug, title, body, assignee, prio, key, skills=(),
                    guard=None):
        captured.append((title, body, key))
        return "t_new"
    m._kanban_create = fake_create
    try:
        m._rework_arm_card(
            "testboard", 154, H154,
            {"kind": "reject", "at": time.time() - 3600})
    finally:
        m._kanban_create = real_create
    body = captured[0][1] if captured else ""
    low = body.lower()
    check("C1 the template requires an answer to the verdict in the PR",
          "rework response" in low and "every" in low, body[:400])
    check("C2 the template requires a re-request of review",
          "kanban_request_review" in body, body[:400])
    check("C3 the template puts the duty on any card, including a child",
          "any card" in low and "child" in low, body[:400])
    shutil.rmtree(sbx, ignore_errors=True)


if __name__ == "__main__":
    test_a1_context_cite_does_not_suppress_review_arm()
    test_a2_real_rework_still_suppresses()
    test_a3_context_cite_dictionary()
    test_a4_context_cite_does_not_suppress_wcfy_arm()
    test_b_blocked_stale_arms()
    test_c_template_requires_rework_response()
    if FAILS:
        print("TOTAL context cite card: FAIL (%s)" % ", ".join(FAILS))
        sys.exit(1)
    print("TOTAL context cite card: OK")
