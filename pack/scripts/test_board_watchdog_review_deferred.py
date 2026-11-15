#!/usr/bin/env python3
"""The review layer does not lock itself behind a backlog parking.

Open PR heads without a verdict whose reviewer cards sit in scheduled with a
reason that is NOT the review-layer hold ("backlog: outside the milestone")
cannot start without a decision of the owner. review_layer_check used to
count these heads as unchecked, so the layer was never empty and kept every
ready card below p50 parked forever: a circle "the backlog parks the review
of heads, the layer waits for that same review".

Rule: a head whose review is deferred by a decision (a reviewer card in
scheduled whose latest reason is not the review-layer hold) holds no slot.

Run: python3 test_board_watchdog_review_deferred.py
Mutant run: BOARD_WATCHDOG_SCRIPT=/path/to/mutant.py python3 test_board_watchdog_review_deferred.py
A mutant must turn at least one test red.
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
assert rl.SCRIPT == SCRIPT, "the harness and the test must use one script"

H290 = "d32e3290000000000000000000000000000000000"
H214 = "415ef500000000000000000000000000000000000"
H203 = "245093f0000000000000000000000000000000000"
BACKLOG = ("SCHEDULED: backlog: outside the milestone (backlog rule 2, "
           "category: pr_not_milestone): not bound to milestones "
           "1.2.0/2.2.0; return to ready with unblock after the owner "
           "decides")


def held_card(tid, prio=30, assignee="coder"):
    """A card parked by the review layer itself (marker in a comment)."""
    return ({"id": tid, "status": "scheduled", "assignee": assignee,
             "priority": prio, "key": None,
             "title": "work " + tid},
            (tid, "SCHEDULED: %s: unchecked heads 3"
             % mod.REVIEW_PARK_MARKER))


def reviewer_card(tid, number, head, status, comment=BACKLOG):
    t = {"id": tid, "status": status, "assignee": "reviewer",
         "priority": 100,
         "key": "reviewer-pr%d-%s" % (number, head[:7]),
         "title": "[reviewer] PR #%d (head %s): review of head without verdict"
                  % (number, head[:7]),
         "body": "review of head %s" % head[:7]}
    c = (tid, comment) if comment is not None else None
    return t, c


def scenario(prs, cards):
    tasks = [c[0] for c in cards]
    comments = [c[1] for c in cards if c[1]]
    return rl.with_scenario(prs, tasks=tasks, comments=comments)


def verbs(calls, verb):
    return [c for c in calls if len(c) > 4 and c[4] == verb]


def test_live_deadlock_released():
    """Three heads with reviewer cards parked in the backlog: the cards
    carrying the review-layer hold marker must return to ready."""
    prs = {290: rl.pr_json(H290, 86 * 3600),
           214: rl.pr_json(H214, 102 * 3600),
           203: rl.pr_json(H203, 101 * 3600)}
    cards = [reviewer_card("t_4dcca70d", 290, H290, "scheduled"),
             reviewer_card("t_3543b6d0", 214, H214, "scheduled"),
             reviewer_card("t_941ed815", 203, H203, "scheduled")]
    for i in range(3):
        cards.append(held_card("t_park%d" % i))
    found, calls, tmp = scenario(prs, cards)
    un = verbs(calls, "unblock")
    assert sorted(c[5] for c in un) == ["t_park0", "t_park1", "t_park2"], calls
    assert verbs(calls, "schedule") == [], calls
    assert rl.creates(calls) == [], calls
    assert any("emptied" in f[2] for f in found), found
    df = [f for f in found if "revlayer:deferred" in f[0]]
    assert len(df) == 1 and all(
        x in df[0][2] for x in ("#290", "#214", "#203", "t_4dcca70d",
                                "t_3543b6d0", "t_941ed815")), found
    rl.shutil.rmtree(tmp)
    print("OK test_live_deadlock_released")


def test_ready_or_running_reviewer_card_holds():
    """Negative: a reviewer card in ready or running holds the layer as
    before, the parking is not released."""
    for status in ("ready", "running"):
        prs = {290: rl.pr_json(H290, 86 * 3600)}
        cards = [reviewer_card("t_rev290", 290, H290, status),
                 held_card("t_park0")]
        found, calls, tmp = scenario(prs, cards)
        assert verbs(calls, "unblock") == [], (status, calls)
        assert rl.creates(calls) == [], (status, calls)
        assert not any("emptied" in f[2] for f in found), (status, found)
        rl.shutil.rmtree(tmp)
    print("OK test_ready_or_running_reviewer_card_holds")


def test_blocked_reviewer_card_holds_and_alerts():
    """Negative: a reviewer card in blocked does not release the layer and
    raises the stuck alert, as before."""
    prs = {290: rl.pr_json(H290, 86 * 3600)}
    cards = [reviewer_card("t_rev290", 290, H290, "blocked"),
             held_card("t_park0")]
    found, calls, tmp = scenario(prs, cards)
    assert verbs(calls, "unblock") == [], calls
    assert any("revlayer:stuck:t_rev290" in f[0] and "t_rev290" in f[2]
               for f in found), found
    rl.shutil.rmtree(tmp)
    print("OK test_blocked_reviewer_card_holds_and_alerts")


def test_no_card_arms_and_parks_as_before():
    """Negative: a head without a card gets an auto-arm and a ready card
    below p50 is parked, as before."""
    prs = {290: rl.pr_json(H290, 86 * 3600)}
    low = {"id": "t_low", "status": "ready", "assignee": "coder",
           "priority": 30}
    park = held_card("t_park0")
    found, calls, tmp = rl.with_scenario(
        prs, tasks=[low, park[0]], comments=[park[1]])
    made = rl.creates(calls)
    assert len(made) == 1, calls
    key = made[0][made[0].index("--idempotency-key") + 1]
    assert key == "reviewer-pr290-d32e329", key
    sched = verbs(calls, "schedule")
    assert len(sched) == 1 and sched[0][5] == "t_low", calls
    assert mod.REVIEW_PARK_MARKER in " ".join(sched[0]), sched
    assert verbs(calls, "unblock") == [], calls
    rl.shutil.rmtree(tmp)
    print("OK test_no_card_arms_and_parks_as_before")


def test_self_parked_marker_does_not_defer():
    """Edge: the reviewer card is parked by the review-layer hold itself
    (not by someone else's decision). That is not a deferral, otherwise the
    layer would lock itself again: the layer holds, no release."""
    prs = {290: rl.pr_json(H290, 86 * 3600)}
    cards = [reviewer_card(
        "t_rev290", 290, H290, "scheduled",
        comment="SCHEDULED: %s: unchecked heads 1"
                % mod.REVIEW_PARK_MARKER),
             held_card("t_park0")]
    found, calls, tmp = scenario(prs, cards)
    assert verbs(calls, "unblock") == [], calls
    assert not any("revlayer:deferred" in f[0] for f in found), found
    rl.shutil.rmtree(tmp)
    print("OK test_self_parked_marker_does_not_defer")


def test_scheduled_without_reason_holds():
    """Edge: scheduled without any SCHEDULED reason is not classified, the
    layer conservatively holds."""
    prs = {290: rl.pr_json(H290, 86 * 3600)}
    cards = [reviewer_card("t_rev290", 290, H290, "scheduled",
                           comment=None),
             held_card("t_park0")]
    found, calls, tmp = scenario(prs, cards)
    assert verbs(calls, "unblock") == [], calls
    rl.shutil.rmtree(tmp)
    print("OK test_scheduled_without_reason_holds")


def test_partial_deferral_still_holds():
    """Edge: two heads are deferred, the third is in ready: the layer is not
    empty, no release."""
    prs = {290: rl.pr_json(H290, 86 * 3600),
           214: rl.pr_json(H214, 102 * 3600),
           203: rl.pr_json(H203, 101 * 3600)}
    cards = [reviewer_card("t_a", 290, H290, "scheduled"),
             reviewer_card("t_b", 214, H214, "scheduled"),
             reviewer_card("t_c", 203, H203, "ready"),
             held_card("t_park0")]
    found, calls, tmp = scenario(prs, cards)
    assert verbs(calls, "unblock") == [], calls
    assert not any("emptied" in f[2] for f in found), found
    rl.shutil.rmtree(tmp)
    print("OK test_partial_deferral_still_holds")


def main():
    test_live_deadlock_released()
    test_ready_or_running_reviewer_card_holds()
    test_blocked_reviewer_card_holds_and_alerts()
    test_no_card_arms_and_parks_as_before()
    test_self_parked_marker_does_not_defer()
    test_scheduled_without_reason_holds()
    test_partial_deferral_still_holds()
    print("ALL REVIEW-DEFERRED TESTS PASSED")


if __name__ == "__main__":
    main()
