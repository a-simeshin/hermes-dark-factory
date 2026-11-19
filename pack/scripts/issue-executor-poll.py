#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cron wrapper: every issue of the active milestone gets an executor card
within one polling cycle.

The logic itself (including the native-or-live guard and the band from
milestone_bands) lives in board-watchdog.issue_executor_poll: this wrapper
only loads the module and prints the result for no-agent delivery. A quiet
run (nothing created) prints nothing and wakes nobody.

--check-issue N mode for a webhook prompt (issues opened/milestoned/labeled):
rc 3 COVERED when the work on the issue is already delivered (a merged PR
after the last scope change, or a done card with a merged PR). Origin: three
duplicate cards in twenty minutes for an issue whose PR had already merged.
The output tells a release chore (waits for the owner's decision until the
tag) from an ordinary delivered issue (a question to the owner "what is
left?").

Run: python3 issue-executor-poll.py [--dry-run]
     python3 issue-executor-poll.py --check-issue N [--repo R] [--board B]
--repo and --board default to FACTORY_REPO and FACTORY_BOARD.
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from factory_config import cfg  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "board_watchdog_poll", os.path.join(HERE, "board-watchdog.py"))
if spec is None or spec.loader is None:
    print("SKIP: board-watchdog.py does not load")
    sys.exit(0)
wd = importlib.util.module_from_spec(spec)
sys.modules["board_watchdog_poll"] = wd
spec.loader.exec_module(wd)


def _arg(argv, name):
    try:
        return argv[argv.index(name) + 1]
    except (ValueError, IndexError):
        return None


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--check-issue" in argv:
        n = _arg(argv, "--check-issue")
        repo = _arg(argv, "--repo") or cfg("FACTORY_REPO")
        board = _arg(argv, "--board") or cfg("FACTORY_BOARD", "main")
        if not n:
            print("usage: --check-issue N [--repo R] [--board B]")
            return 2
        delivered, detail, chore = wd.issue_delivered(
            board, repo, int(n))
        if delivered:
            tail = ("; a release chore stays open until the tag: the owner"
                    " decides, do not create a card"
                    if chore else
                    "; delivered but not a chore: ask the owner"
                    " \"what is left on #%s?\"" % n)
            print("COVERED: work on issue #%s is already delivered: %s%s"
                  % (n, detail, tail))
            return 3
        print("NOT-DELIVERED: no coverage found, a card is appropriate")
        return 0
    made = wd.issue_executor_poll(dry_run="--dry-run" in argv)
    for slug, n, card in made:
        if card:
            print("[%s] issue #%d -> executor card %s (issue poll)"
                  % (slug, n, card))
    return 0


if __name__ == "__main__":
    sys.exit(main())
