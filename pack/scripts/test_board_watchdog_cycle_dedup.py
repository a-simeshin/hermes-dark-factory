#!/usr/bin/env python3
"""A live executor card in the regular review cycle means no duplicated work
on one PR head.

Failure this guarded: the watchdog armed a reviewer card a minute before the
executor card was handed to the regular review cycle. The verdict then went
through both channels: changes_requested returned the executor card to the
coder, and the reviewer, following the deadlock rule of the review skill,
created a parallel rework card. One fix was made twice and reviewed twice on
the same head.

Since "one PR = one card" the watchdog arms no reviewer card at all: review
lives on the executor card. The checks pin that no card is created in any of
the old cases (a live executor card in running/ready/review naming the PR and
the head, a card naming another head, a closed card, a reviewer card), that
the layer does not park ready cards, and that no "cycle" finding (removed
together with the arm) appears. An unmerged PR of a closed card is caught by
the owner-merge close gate instead.

The harness is imported from test_board_watchdog_review_layer.py, including
its mutation loop through BOARD_WATCHDOG_SCRIPT.

Run: python3 test_board_watchdog_cycle_dedup.py
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
    """The card body does not name the PR, the number and the head are only
    in the hand-over comment. No arm, no cycle finding."""
    tasks = [{"id": "t_cycle", "status": "review", "assignee": "coder",
              "title": "Flaky timing assertion in RequestHandlerTests:2282"}]
    comments = [("t_cycle",
                 "Handing over to review: PR #154 is open, head %s, WCFY "
                 "updated." % H7)]
    found, calls, tmp = h.with_scenario(
        {154: h.pr_json(HEAD154, 2 * 3600)}, tasks=tasks,
        comments=comments)
    assert h.creates(calls) == [], calls
    # The cycle findings of the review layer are removed with the arm.
    assert not any("cycle" in f[0] for f in found), found
    assert not any(":arm:" in f[0] for f in found), found
    shutil.rmtree(tmp)
    print("OK test_cycle_review_card_comment_no_arm")


def test_cycle_running_body_no_arm_and_no_park():
    """A running card with the PR and head in the title: no arm, and the
    layer holds nothing: a foreign ready p10 is not parked (no schedule)."""
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
    assert not any("cycle" in f[0] for f in found), found
    shutil.rmtree(tmp)
    print("OK test_cycle_running_body_no_arm_and_no_park")


def test_ready_after_changes_requested_no_arm():
    """changes_requested returned the card to the executor (ready): the
    regular cycle is alive, the watchdog raises no rework card."""
    tasks = [{"id": "t_fix", "status": "ready", "assignee": "coder",
              "title": "follow-up"}]
    comments = [("t_fix",
                 "Review round 1: changes requested on PR #154, head %s, "
                 "fixing the twin." % H7)]
    found, calls, tmp = h.with_scenario(
        {154: h.pr_json(HEAD154, 2 * 3600)}, tasks=tasks,
        comments=comments)
    assert h.creates(calls) == [], calls
    # No cycle findings: the rework lives on the executor card.
    assert not any("cycle" in f[0] for f in found), found
    shutil.rmtree(tmp)
    print("OK test_ready_after_changes_requested_no_arm")


def test_other_head_in_card_still_arms():
    """The card names another head: there is still no arm (no card is
    created in any case) and no cycle finding."""
    tasks = [{"id": "t_old", "status": "review", "assignee": "coder",
              "title": "old head of PR #154 %s" % OTHER_HEAD[:7]}]
    found, calls, tmp = h.with_scenario(
        {154: h.pr_json(HEAD154, 2 * 3600)}, tasks=tasks)
    assert h.creates(calls) == [], calls
    assert not any(":cycle:" in f[0] for f in found), found
    shutil.rmtree(tmp)
    print("OK test_other_head_in_card_still_arms")


def test_done_cycle_card_still_arms():
    """A closed cycle card does not start self-recovery through an arm;
    an unmerged PR is caught by the owner-merge close gate."""
    tasks = [{"id": "t_gone", "status": "done", "assignee": "coder",
              "title": "closed after pushing PR #154 %s" % H7}]
    found, calls, tmp = h.with_scenario(
        {154: h.pr_json(HEAD154, 2 * 3600)}, tasks=tasks)
    assert h.creates(calls) == [], calls
    shutil.rmtree(tmp)
    print("OK test_done_cycle_card_still_arms")


def test_reviewer_card_no_cycle_finding():
    """A reviewer card with the PR and the head: no create, no cycle
    finding (separation of roles)."""
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
    print("ALL CYCLE DEDUP TESTS PASSED")


if __name__ == "__main__":
    main()
