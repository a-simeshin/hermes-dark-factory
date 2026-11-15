#!/usr/bin/env python3
"""A live executor card in the regular review cycle suppresses the
watchdog's own reviewer card (no duplicated work on one PR head).

Failure this guards: the watchdog armed a reviewer card a minute before the
executor card was handed to the regular review cycle. The verdict then went
through both channels: changes_requested returned the executor card to the
coder, and the reviewer, following the deadlock rule of the review skill,
created a parallel rework card. One fix was made twice and reviewed twice on
the same head.

The check: a live executor card (running/ready/review) that names this PR and
this head (in the body, a comment, an event or a run summary) suppresses the
auto-arm and does not hold the review layer (the parking of ready cards).
When the card is closed or the head differs, the arm works as before (self
recovery after the regular cycle died).

The harness is imported from test_board_watchdog_review_layer.py, including
its mutation loop through BOARD_WATCHDOG_SCRIPT.
"""
import importlib.util
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.join(HERE, "test_board_watchdog_review_layer.py")
spec = importlib.util.spec_from_file_location("wd_revlayer_harness", HARNESS)
if spec is None or spec.loader is None:
    print("SKIP: cannot load harness")
    sys.exit(0)
h = importlib.util.module_from_spec(spec)
sys.modules["wd_revlayer_harness"] = h
spec.loader.exec_module(h)

HEAD154 = h.HEAD154
H7 = HEAD154[:7]
OTHER_HEAD = "0011223344556677889900aabbccddeeff001122"


def test_cycle_review_card_comment_no_arm():
    """Live case: the card body does not name the PR, the number and the head
    are only in the hand-over comment. The arm is suppressed, finding cycle."""
    tasks = [{"id": "t_cycle", "status": "review", "assignee": "coder",
              "title": "Flaky timing assertion in RequestHandlerTests:2282"}]
    comments = [("t_cycle",
                 "Handing over to review: PR #154 is open, head %s, WCFY "
                 "updated." % H7)]
    found, calls, tmp = h.with_scenario(
        {154: h.pr_json(HEAD154, 2 * 3600)}, tasks=tasks,
        comments=comments)
    assert h.creates(calls) == [], calls
    assert any(f[0].endswith("cycle:154:%s" % H7) and "t_cycle" in f[2]
               and "[review]" in f[2] for f in found), found
    assert not any(":arm:" in f[0] for f in found), found
    shutil.rmtree(tmp)
    print("OK test_cycle_review_card_comment_no_arm")


def test_cycle_running_body_no_arm_and_no_park():
    """A running card with the PR and the head in the body suppresses the arm
    AND does not hold the layer: a foreign ready p10 card is not parked (no
    schedule calls)."""
    tasks = [
        {"id": "t_run", "status": "running", "assignee": "coder",
         "title": "Fix from #154 on head %s" % H7},
        {"id": "t_low", "status": "ready", "assignee": "coder",
         "priority": 10, "title": "low priority"},
    ]
    found, calls, tmp = h.with_scenario(
        {154: h.pr_json(HEAD154, 2 * 3600)}, tasks=tasks)
    assert h.creates(calls) == [], calls
    assert not any("schedule" in c for c in calls), calls
    assert any(f[0].endswith("cycle:154:%s" % H7) and "t_run" in f[2]
               for f in found), found
    shutil.rmtree(tmp)
    print("OK test_cycle_running_body_no_arm_and_no_park")


def test_ready_after_changes_requested_no_arm():
    """changes_requested returned the card to the executor (ready): the
    regular cycle is alive, the watchdog does not raise a rework card."""
    tasks = [{"id": "t_fix", "status": "ready", "assignee": "coder",
              "title": "finishing touches"}]
    comments = [("t_fix",
                 "Review iteration 1: changes requested on PR #154, head %s, "
                 "fixing the twin." % H7)]
    found, calls, tmp = h.with_scenario(
        {154: h.pr_json(HEAD154, 2 * 3600)}, tasks=tasks,
        comments=comments)
    assert h.creates(calls) == [], calls
    assert any(f[0].endswith("cycle:154:%s" % H7) and "t_fix" in f[2]
               for f in found), found
    shutil.rmtree(tmp)
    print("OK test_ready_after_changes_requested_no_arm")


def test_other_head_in_card_still_arms():
    """The card names the PR but ANOTHER head: there is no regular cycle for
    this head, the arm works (mutation sensitivity)."""
    tasks = [{"id": "t_old", "status": "review", "assignee": "coder",
              "title": "old head of PR #154 %s" % OTHER_HEAD[:7]}]
    found, calls, tmp = h.with_scenario(
        {154: h.pr_json(HEAD154, 2 * 3600)}, tasks=tasks)
    made = h.creates(calls)
    assert len(made) == 1, calls
    key = made[0][made[0].index("--idempotency-key") + 1]
    assert key == "reviewer-pr154-%s" % H7, key
    assert not any(":cycle:" in f[0] for f in found), found
    shutil.rmtree(tmp)
    print("OK test_other_head_in_card_still_arms")


def test_done_cycle_card_still_arms():
    """The cycle card is closed (done): no signal, the watchdog must arm the
    self recovery after the regular cycle died."""
    tasks = [{"id": "t_gone", "status": "done", "assignee": "coder",
              "title": "closed after pushing PR #154 %s" % H7}]
    found, calls, tmp = h.with_scenario(
        {154: h.pr_json(HEAD154, 2 * 3600)}, tasks=tasks)
    assert len(h.creates(calls)) == 1, calls
    shutil.rmtree(tmp)
    print("OK test_done_cycle_card_still_arms")


def test_reviewer_card_no_cycle_finding():
    """A reviewer card with the PR and the head is matched by the old path,
    no cycle finding appears (separation of roles)."""
    tasks = [{"id": "t_rev", "status": "ready", "assignee": "reviewer",
              "title": "[reviewer] PR #154 %s review of the head" % H7}]
    found, calls, tmp = h.with_scenario(
        {154: h.pr_json(HEAD154, 2 * 3600)}, tasks=tasks)
    assert h.creates(calls) == [], calls
    assert not any(":cycle:" in f[0] for f in found), found
    shutil.rmtree(tmp)
    print("OK test_reviewer_card_no_cycle_finding")


def main():
    test_cycle_review_card_comment_no_arm()
    test_cycle_running_body_no_arm_and_no_park()
    test_ready_after_changes_requested_no_arm()
    test_other_head_in_card_still_arms()
    test_done_cycle_card_still_arms()
    test_reviewer_card_no_cycle_finding()
    print("ALL CYCLE-DEDUP TESTS PASSED")


if __name__ == "__main__":
    main()
