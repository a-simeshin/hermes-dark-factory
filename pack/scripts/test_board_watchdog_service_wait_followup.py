#!/usr/bin/env python3
"""A follow-up card that works "after the merge of PR #N" is a service wait.

Case: a PR with an accept verdict on its head and no further step. The only
live coder card mentioning the PR was a follow-up for ANOTHER issue,
"(issue #96, after the merge of PR #154)", blocked until that merge. It
quotes the PR number and its head as context (verdict, head) but works in a
FUTURE separate PR and never moves the head of #154. The service-wait
vocabulary did not recognise "after the merge of PR #" and "blocked until
the merge", so the card was counted as live work on the PR.

Rule: a card whose work stands AFTER the merge of a PR, or that is blocked
until the merge, is a service wait: its trigger is off the board and it is
not live work on the PR. "Before the merge" without a block marker does NOT
match: "land X before the merge of PR #N" is real pre-merge work.

Pins:
- the follow-up "after the merge of PR #N" with the head in its body does
  not make the watchdog create anything (one PR = one card: there is no WCFY
  arm any more, the follow-up must not trigger a card either);
- _is_service_wait_card recognises the title and the body forms separately;
- a real working card with the head in its body and no merge-wait words
  creates nothing either (negative);
- "before the merge of PR #" without a block marker stays a working card
  (negative against an over-wide match).

Uses the review layer harness (test_board_watchdog_review_layer.py).

Run: python3 test_board_watchdog_service_wait_followup.py
Mutant: BOARD_WATCHDOG_SCRIPT=<copy> python3 test_board_watchdog_service_wait_followup.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.environ.get("BOARD_WATCHDOG_SCRIPT") or os.path.join(
    HERE, "board-watchdog.py")
os.environ["BOARD_WATCHDOG_SCRIPT"] = SCRIPT
sys.path.insert(0, HERE)

import test_board_watchdog_review_layer as rl  # noqa: E402

mod = rl.mod
assert rl.SCRIPT == SCRIPT, "the harness and the test must load one script"

H154 = rl.HEAD154


def verdict_accept():
    """APPROVED on the head, short sha, a Calibration h3 with a promises
    table (verdicts after PROMISES_TABLE_REQUIRED_SINCE without one do not
    count)."""
    return {"body": "## Review verdict\n\n"
                    "APPROVED (PASS) on head `%s`\n\n"
                    "### Calibration\n- contract: ok\n\n"
                    "| Promise | Mutant | Test | Result |\n"
                    "|---|---|---|---|\n"
                    "| NOTICE entry registered | grep registry | "
                    "notice-check | green |" % H154[:8],
            "createdAt": rl.iso(rl.time.time() - 2 * 3600)}


# A follow-up for ANOTHER issue, blocked until the merge of the PR; the head
# and the verdict of the PR are quoted in the body as context.
FOLLOWUP_AFTER_MERGE = {
    "id": "t_fup1", "status": "blocked", "assignee": "coder",
    "priority": 40, "key": "coder-issue96-notice-wording",
    "title": "coder: fix stale session id wording in "
             "NOTICE.d/keep-alive-history.txt "
             "(issue #96, after the merge of PR #154)",
    "body": "Issue: #96\n\n"
            "A one-line documentation fix in NOTICE.d. The card is "
            "blocked until the merge of PR #154, see the release "
            "condition.\n\n"
            "## Context (checked by the architect; PR #154 head = %s)\n\n"
            "PR #154 has \"## Review verdict: APPROVED (head %s)\": its "
            "head must not move." % (H154[:8], H154[:8])}


# A real working card (negative): work on THIS PR before the merge.
WORK_BEFORE_MERGE = {
    "id": "t_work", "status": "ready", "assignee": "coder",
    "priority": 95, "key": None,
    "title": "[coder] PR #154: rework by verdict",
    "body": "Head %s. Land it before the merge of PR #154." % H154[:7]}


def test_followup_after_merge_creates_nothing():
    """Accept on the head, the only live coder card is a follow-up "after
    the merge of PR #154" (blocked, head in the body). One PR = one card:
    no arm is created and the follow-up does not cause one."""
    found, calls, tmp = rl.with_scenario(
        {154: rl.pr_json(H154, 2 * 3600, comments=[verdict_accept()])},
        tasks=[FOLLOWUP_AFTER_MERGE])
    assert rl.creates(calls) == [], (rl.creates(calls), found)
    rl.shutil.rmtree(tmp)
    print("OK test_followup_after_merge_creates_nothing")


def test_service_wait_texts():
    """The service-wait vocabulary recognises the title form "after the
    merge of PR #" and the body form "blocked until the merge" separately.
    Without "after"/"blocked" the same words do not match."""
    t = FOLLOWUP_AFTER_MERGE
    assert mod._is_service_wait_card(t["title"], ""), t["title"]
    assert mod._is_service_wait_card("", t["body"]), t["body"]
    # "before the merge" without a block marker or "after" is not a wait:
    assert not mod._is_service_wait_card(
        "coder: rework PR #154", "Land it before the merge of PR #154")
    print("OK test_service_wait_texts")


def test_real_work_card_creates_nothing():
    """Negative: a real live coder card on this PR and head (no merge-wait
    words) creates nothing, as before."""
    found, calls, tmp = rl.with_scenario(
        {154: rl.pr_json(H154, 2 * 3600, comments=[verdict_accept()])},
        tasks=[WORK_BEFORE_MERGE])
    assert rl.creates(calls) == [], calls
    rl.shutil.rmtree(tmp)
    print("OK test_real_work_card_creates_nothing")


def main():
    test_followup_after_merge_creates_nothing()
    test_service_wait_texts()
    test_real_work_card_creates_nothing()
    print("ALL SERVICE WAIT FOLLOW-UP TESTS PASSED")


if __name__ == "__main__":
    main()
