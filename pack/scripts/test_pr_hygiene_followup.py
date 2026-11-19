#!/usr/bin/env python3
"""pr-hygiene: a follow-up promise without a number is a finding.

Origin: a PR body had a section "Known limitation: ... which is tracked as a
follow-up", and no such issue existed: the owner had to file it himself.
The detector (followup_untracked_promise, the same mechanism as
eol_twin_promise) must fire on a phrase about tracking a follow-up without a
number and stay silent when #N or t_xxxxxxxx is nearby, and on neutral
"follow-up" mentions outside a promise context.

Run: python3 test_pr_hygiene_followup.py
"""
import importlib.util
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.update({
    "HERMES_HOME": tempfile.mkdtemp(prefix="ph-followup-"),
    "FACTORY_REPO": "acme/widget",
    "FACTORY_REPO_PATH": "/srv/hermes/widget",
    "FACTORY_BOARD": "main",
    "FACTORY_OWNER_LOGIN": "owner-login",
    "FACTORY_OWNER_CHAT": "telegram:000000000",
    "FACTORY_BASE_BRANCHES": "main",
})
SCRIPT = os.environ.get("PR_HYGIENE_SCRIPT") or os.path.join(
    HERE, "pr-hygiene.py")
spec = importlib.util.spec_from_file_location("pr_hygiene_followup", SCRIPT)
if spec is None or spec.loader is None:
    print("SKIP: cannot load pr-hygiene.py")
    sys.exit(0)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# The line from the original PR body (Known limitation section).
BODY_PR = """## Known limitation

The per-connection budget is not shared across transports yet, which is
tracked as a follow-up.
"""

BODY_ISSUE = """Known limitation: the budget is per-connection only.

Deferred to a follow-up.
"""

BODY_WITH_NUMBER = """## Known limitation

The per-connection budget is not shared yet, which is tracked as a
follow-up in #377 (2.4.0).
"""

BODY_WITH_CARD = """Known limitation: limited to SSE, tracked as a
follow-up (t_bca2cfce).
"""

# A neutral mention without a tracking promise: silent.
BODY_NEUTRAL = """Follow-up PRs from review: both merged.
The next follow-up lands after the release train.
"""

# A promise plus a number in a neighbouring line: silent.
BODY_NUMBER_NEXT_LINE = """Known limitation: not shared across
transports; tracked as a follow-up.

#377 tracks the follow-up work.
"""


def test_pr_body_fires():
    hits = mod.followup_untracked_hits(BODY_PR)
    assert any("tracked as a follow-up" in h for h in hits), hits


def test_issue_body_deferred_fires():
    hits = mod.followup_untracked_hits(BODY_ISSUE)
    assert any("follow-up" in h.lower() for h in hits), hits


def test_issue_number_silences():
    assert mod.followup_untracked_hits(BODY_WITH_NUMBER) == []


def test_card_ref_silences():
    assert mod.followup_untracked_hits(BODY_WITH_CARD) == []


def test_neutral_mention_silences():
    assert mod.followup_untracked_hits(BODY_NEUTRAL) == []


def test_number_next_line_silences():
    assert mod.followup_untracked_hits(BODY_NUMBER_NEXT_LINE) == []


def test_aggregation_kinds():
    lines, kinds = mod.followup_untracked_promise(
        [{"number": 361, "body": BODY_PR}],
        [{"number": 400, "body": BODY_ISSUE}])
    assert kinds == {"pr": [361], "issue": [400]}, (kinds, lines)
    assert any("PR #361" in ln for ln in lines), lines
    assert any("issue #400" in ln for ln in lines), lines

    lines, kinds = mod.followup_untracked_promise(
        [{"number": 361, "body": BODY_WITH_NUMBER}], [])
    assert kinds == {"pr": [], "issue": []}, (kinds, lines)
    assert lines == [], lines


def test_empty_and_none():
    assert mod.followup_untracked_hits("") == []
    assert mod.followup_untracked_hits(None) == []
    assert mod.followup_untracked_promise([], []) == (
        [], {"pr": [], "issue": []})


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok %s" % name)
    print("OK test_pr_hygiene_followup")
