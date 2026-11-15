#!/usr/bin/env python3
"""The class of a verdict is read from the deciding line, not from mentions
in the history of the body.

Live shape of the bug: a reviewer's "PASS on head X (re-review after CHANGES
REQUESTED on Y; ...)" was classified as reject because the classifier took
the first 240 characters of the body with reject first, so a passive mention
of the past CR in parentheses turned the PASS into a reject, the paired
branch did not create the WCFY card, and the PR with a PASS stood for hours.
The symmetric case: "## Review verdict: CHANGES REQUESTED ... (iteration 2; iteration
1 APPROVE was on `25d39931`...)" mentions a past APPROVE and stays reject.

The bodies below keep the structure and the deciding lines of the live
verdicts; long evidence sections are shortened. A mutant that searches the
whole body must fail the PASS-with-a-mention tests
(BOARD_WATCHDOG_SCRIPT=<copy with the mutation, placed next to the scripts>).
"""
import atexit
import importlib.util
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
_HOME = tempfile.mkdtemp(prefix="wd-home-")
atexit.register(shutil.rmtree, _HOME, True)
os.environ.update({
    "HERMES_HOME": _HOME,
    "FACTORY_REPO": "acme/widget",
    "FACTORY_REPO_PATH": "/srv/hermes/widget",
    "FACTORY_BOARD": "main",
    "FACTORY_OWNER_LOGIN": "owner-login",
    "FACTORY_OWNER_CHAT": "telegram:000000000",
    "FACTORY_BASE_BRANCHES": "develop/1.x,develop/2.x",
})
# Mutation runs use a COPY of the script; the production file is not touched.
SCRIPT = os.environ.get("BOARD_WATCHDOG_SCRIPT") or os.path.join(
    HERE, "board-watchdog.py")

spec = importlib.util.spec_from_file_location("board_watchdog", SCRIPT)
if spec is None or spec.loader is None:
    print("SKIP: cannot load board-watchdog.py")
    sys.exit(0)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

HEAD291 = "d4c776a24bec79840b6bb32a131c407a501ffb04"
HEAD292 = "8e03016de0f86c82e6f2152b13fb985142b4b11d"

BODY_291_PASS = (
    "## Review verdict\n\n"
    "PASS on head `d4c776a24bec79840b6bb32a131c407a501ffb04` (re-review "
    "after CHANGES REQUESTED on `0d272f9`; the owner's review was on "
    "`0f1cb149`, and the head is three rework commits plus the "
    "`develop/2.x` update-branch merge past that return). Class: "
    "**critical** (default-enabled enrichment in both starters plus a new "
    "public config property; human merge only).\n\n"
    "Everything below was verified by me in a detached worktree at the "
    "exact head; nothing is taken from the author's report.\n\n"
    "### Prior findings: all closed, verified on the exact head\n\n"
    "**Owner BLOCK 2, multi-line messages: CLOSED.** The detector keeps "
    "the full message sections. Proof the new tests pin the old bug: with "
    "the pre-fix detector against the head test suite, exactly three "
    "tests go red; restored, 37/37 green.\n\n"
    "**Owner BLOCK 3, message-only candidates: CLOSED.** The "
    "message-only branch is removed; the new negative test is red on the "
    "pre-fix detector and green on the head.\n\n"
    "### State\n\n"
    "- CI: 5/5 green on the exact head.\n"
    "- Mergeable: MERGEABLE / CLEAN.\n"
    "- WCFY: the published one still sits on the pre-fix head; per the "
    "delivery rules it is published once, on the final head, after PASS.\n\n"
    "## Calibration\n"
    "- contract: all three owner blockers re-measured closed on the exact "
    "head (code reading plus a red run on the base)\n"
    "- mutation: three mutations on this head, each turns exactly the "
    "expected tests red and is green again after the revert\n"
    "- twin: N/A: the owner decided there is no twin on the older line\n"
    "- ui-input: N/A: the diff touches no UI surface\n"
)

BODY_292_CR = (
    "## Review verdict: CHANGES REQUESTED\n\n"
    "Re-review of head `8e03016de0f86c82e6f2152b13fb985142b4b11d` (iteration "
    "2; iteration 1 APPROVE was on `25d39931`, the owner CHANGES REQUESTED "
    "came after it on the same head). The bot cannot submit a formal "
    "GitHub review on its own PR, so this comment is the machine-readable "
    "verdict.\n\n"
    "CI: 5/5 green on this head.\n\n"
    "### Closed since the owner review (verified, with mutations)\n\n"
    "1. Owner finding 1 (join by response identity, not by text): CLOSED. "
    "The lookup now matches the response frame by JSON-RPC request id; a "
    "repeated failing call never borrows the older call's note.\n"
)

BODY_240_PASS = (
    "## Review verdict\n\n"
    "**PASS** on head `a18a55fae1946842c3a457f8da069ce3a7efa699` (merge of "
    "`develop/2.x`@`264ec308` into the branch, i.e. the update-branch "
    "follow-up to the PASS on `2a2861c`).\n\n"
    "CI: 5 of 5 green on this exact head. Merge state MERGEABLE/CLEAN.\n\n"
    "### Merge prerequisites from the previous verdict: both closed\n\n"
    "1. `## Before / After` is now the first section of the body.\n"
    "2. update-branch: the merge resolution is reviewed below; CI green on "
    "the new head.\n"
)

BODY_240_PASS_MENTION = (
    "## Review verdict\n\n"
    "**PASS** on head `2a2861cad47d6cf41e472d0e6598551baa6b9b8b` "
    "(re-review after the owner's CHANGES REQUESTED on `a39b498`).\n\n"
    "### Owner BLOCK closure: initialize wait budget source\n\n"
    "The owner required the silent contract change to be either reverted "
    "or made explicit. The rework made it explicit and pinned it with "
    "tests on both stacks.\n\n"
    "Mutation proof, production lines, exact head (my own runs, reverted "
    "after each):\n\n"
    "| Mutation | Result |\n|---|---|\n"
    "| M1a webmvc controller reverted to always use the request budget | "
    "3 red |\n"
)

BODY_291_OWNER_CR = (
    "## Review verdict\n\n"
    "CHANGES REQUESTED: minor (owner, head d4c776a)\n\n"
    "The code is accepted. All three blockers of my review on 0f1cb14 "
    "are closed, and I checked them on this head with my own mutants:\n"
    "- only the first `Caused by:` kept instead of the deepest: 6 of 37 "
    "red;\n"
    "- message continuation lines dropped: 3 of 37 red;\n"
    "- message-only logs treated as candidates again: 2 of 37 red.\n\n"
    "On the prototype note: I do not require a separate \"prototype "
    "accepted\" comment for #251 here, the live demo stays with #292.\n\n"
    "### Blocking\n"
    "- [BLOCK] \"What changes for you\" is bound to 0f1cb14. Publish it for "
    "head d4c776a. This is the only item left.\n\n"
    "I will merge #291 once that comment is on this head. No code change "
    "and no new head are needed.\n"
)

BODY_OWNER_CR_FREE = ("Owner verdict: CHANGES REQUESTED: minor. The code "
                      "will be revisited after the formalities, the review "
                      "is not finished.")


def test_291_pass_with_cr_mention():
    """PASS with a mention of the past CR in parentheses on the deciding
    line = accept (before the fix: reject)."""
    kind = mod._verdict_kind(BODY_291_PASS)
    assert kind == "accept", "got %s" % kind


def test_291_latest_verdict_accept():
    """The same text through the _latest_verdict pipeline (head sha in the
    body, Calibration present, createdAt after CALIBRATION_REQUIRED_SINCE):
    the paired branch must see accept, not reject."""
    data = {"reviews": [], "comments": [
        {"body": BODY_291_PASS, "createdAt": "2026-11-01T23:45:56Z"}]}
    info = mod._latest_verdict(data, HEAD291)
    assert info is not None, "no verdict"
    assert info["kind"] == "accept", "got %s" % info["kind"]


def test_header_cr_with_past_approve_mention():
    """CR in the header; a mention of the past APPROVE to the right of the
    deciding line does not flip it: reject."""
    kind = mod._verdict_kind(BODY_292_CR)
    assert kind == "reject", "got %s" % kind


def test_240_pass_on_head():
    """A PASS without mentions and a PASS with a mention of the owner's CR
    in parentheses are both accept."""
    assert mod._verdict_kind(BODY_240_PASS) == "accept"
    assert mod._verdict_kind(BODY_240_PASS_MENTION) == "accept"


def test_owner_cr_minor():
    """The owner's CR (the verdict on the first line under the header, "The
    code is accepted" below it) and the owner's free form without a header:
    both reject."""
    assert mod._verdict_kind(BODY_291_OWNER_CR) == "reject"
    assert mod._verdict_kind(BODY_OWNER_CR_FREE) == "reject"


def test_no_deciding_line_is_not_accept():
    """Negative: a body without a deciding line carrying a verdict word is
    neither accept nor reject because of mentions in the history."""
    deferred = ("## Review verdict\n\nDeferred: CI on this head is still "
                "running, no decision yet. History: the first iteration was returned "
                "on the old head.")
    handoff = "Pushed head `%s`, awaiting the review verdict." % HEAD292
    assert mod._verdict_kind(deferred) == "other", (
        mod._verdict_kind(deferred))
    assert mod._verdict_kind(handoff) == "other"


def main():
    test_291_pass_with_cr_mention()
    test_291_latest_verdict_accept()
    test_header_cr_with_past_approve_mention()
    test_240_pass_on_head()
    test_owner_cr_minor()
    test_no_deciding_line_is_not_accept()
    print("verdict class tests: all green")


if __name__ == "__main__":
    main()
