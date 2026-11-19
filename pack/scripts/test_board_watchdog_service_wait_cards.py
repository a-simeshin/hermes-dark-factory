#!/usr/bin/env python3
"""Short verdict sha and service cards in the review layer.

Why: a PR had an accept verdict on a short sha ("APPROVED ... on head
`36242cdf`") and stood for two days with no next step. The suspicion
"the watchdog matches only a full sha" was wrong; the real cause was the
only live card on the PR, a service card "[issue-close] close issue #N
after the merge of PR #M" (blocked, waits for the merge). It carried the
PR number in the title and the head in the body, so the check "a live
coder card already works on this head" counted it as live work.

Rule: a service card that waits for a merge or for the owner (issue-close,
hold and the like) is not live work on a PR: not for the review layer, not
for the active-milestone orphan finding, not for the rework gaps.

Pins:
  - a short sha (7+ chars, a prefix of the head) is a verdict on the head,
    the same as a full sha (regression pin);
  - a service card does not suppress anything (with one PR = one card the
    layer creates no arms on an accept verdict at all);
  - a real coder card on the PR still covers it (negative);
  - a service card is not live work for the orphan finding;
  - a service card is not live work for the rework gaps: a CR verdict on
    the head gets a rework card.

Uses the review layer harness of test_board_watchdog_review_layer.py.
Run: python3 test_board_watchdog_service_wait_cards.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import test_board_watchdog_review_layer as rl  # noqa: E402

mod = rl.mod
H154 = rl.HEAD154


def verdict_short_sha(short_n=8):
    """Accept verdict with a short sha, Calibration h3, dated before the
    promises table became required (no table)."""
    return {"body": "## Review verdict\n\n"
                    "APPROVED (PASS) on head `%s`\n\n"
                    "### Calibration\n- contract: ok"
                    % H154[:short_n],
            "createdAt": "2026-11-13T17:38:00Z"}


# A service card that waits for the merge.
SERVICE_ISSUE_CLOSE = {
    "id": "t_svc1", "status": "blocked", "assignee": "coder",
    "priority": 30, "key": None,
    "title": "[issue-close] close issue #75 after the merge of PR #154",
    "body": "Head ff6dcdf. The card waits for the merge of PR #154, no steps "
            "before the merge. Release condition: PR #154 merged."}

# The same class, but the PR number comes first: _title_owns_pr treats the
# title as the owner of #154 (no stop word within 24 chars before it).
SERVICE_OWNS_TITLE = {
    "id": "t_svc2", "status": "blocked", "assignee": "coder",
    "priority": 30, "key": None,
    "title": "[issue-close] PR #154: close issue #75 after merge",
    "body": "waits for the merge of PR #154"}


def created_key(made):
    assert len(made) == 1, made
    return made[0][made[0].index("--idempotency-key") + 1]


def test_short_sha_verdict_is_verdict_on_head():
    data = {"head": H154, "base": "develop/2.x",
            "commits": [{"committedDate":
                         rl.iso(rl.time.time() - 2 * 3600)}],
            "reviews": [], "comments": [verdict_short_sha(8)]}
    info = mod._latest_verdict(data, H154)
    assert info is not None and info["kind"] == "accept", info
    # Layer level: no creates at all.
    found, calls, tmp = rl.with_scenario(
        {154: rl.pr_json(H154, 2 * 3600,
                         comments=[verdict_short_sha(8)])})
    assert rl.creates(calls) == [], calls
    rl.shutil.rmtree(tmp)
    print("OK test_short_sha_verdict_is_verdict_on_head")


def test_service_wait_card_does_not_suppress_anything():
    found, calls, tmp = rl.with_scenario(
        {154: rl.pr_json(H154, 2 * 3600,
                         comments=[verdict_short_sha(8)])},
        tasks=[SERVICE_ISSUE_CLOSE])
    assert rl.creates(calls) == [], (rl.creates(calls), found)
    rl.shutil.rmtree(tmp)
    print("OK test_service_wait_card_does_not_suppress_anything")


def test_real_work_card_still_covers():
    tasks = [{"id": "t_work", "status": "ready", "assignee": "coder",
              "priority": 95, "key": None,
              "title": "[coder] PR #154 What changes for you",
              "body": "head ff6dcdf"}]
    found, calls, tmp = rl.with_scenario(
        {154: rl.pr_json(H154, 2 * 3600,
                         comments=[verdict_short_sha(8)])},
        tasks=tasks)
    assert rl.creates(calls) == [], calls
    rl.shutil.rmtree(tmp)
    print("OK test_real_work_card_still_covers")


def test_service_wait_card_not_live_work_for_orphans():
    found, calls, tmp = rl.with_scenario(
        {154: rl.pr_json(H154, 2 * 3600,
                         comments=[verdict_short_sha(8),
                                   rl.wcfy_comment(2 * 3600)])},
        tasks=[SERVICE_OWNS_TITLE], keep_orphans=True)
    assert rl.creates(calls) == [], calls
    assert [f for f in found if ":r36:ms-orphan:154" in f[0]], found
    rl.shutil.rmtree(tmp)
    print("OK test_service_wait_card_not_live_work_for_orphans")


def test_service_wait_card_not_live_work_for_rework_gaps():
    cr = {"body": "## Review verdict: CHANGES REQUESTED on head `%s`\n\n"
                  "### Calibration\n- contract: ok" % H154[:7],
          "createdAt": rl.iso(rl.time.time() - 2 * 3600)}
    found, calls, tmp = rl.with_scenario(
        {154: rl.pr_json(H154, 2 * 3600, comments=[cr])},
        tasks=[SERVICE_OWNS_TITLE])
    made = rl.creates(calls)
    assert len(made) == 1, (made, found)
    assert created_key(made) == "coder-pr154-rework-ff6dcdf", made
    rl.shutil.rmtree(tmp)
    print("OK test_service_wait_card_not_live_work_for_rework_gaps")


def main():
    test_short_sha_verdict_is_verdict_on_head()
    test_service_wait_card_does_not_suppress_anything()
    test_real_work_card_still_covers()
    test_service_wait_card_not_live_work_for_orphans()
    test_service_wait_card_not_live_work_for_rework_gaps()
    print("ALL SERVICE WAIT CARD TESTS PASSED")


if __name__ == "__main__":
    main()
