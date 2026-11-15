#!/usr/bin/env python3
"""pr-hygiene: has_user_summary reads the heading of the summary section.

A mention of "What changes for you" in the prose of another text is not a
summary: reviewer verdicts saying "the `## What changes for you` comment is
published by the coder after this PASS" were counted as the summary by a
substring search, and the missing-summary finding stayed silent on a PR with
no real summary.

Rule: a comment starts with the heading line SUMMARY_MARKER; a PR body
contains a section whose heading line is exactly that ("##", nothing after it
on the line).

Run: python3 test_pr_hygiene_summary_heading.py
Mutation run: PR_HYGIENE_SCRIPT=<copy with a substring search> python3 ...
"""
import importlib.util
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
# Settings are read at import time; the Hermes home is a temporary directory.
os.environ.update({
    "HERMES_HOME": tempfile.mkdtemp(prefix="ph-summary-"),
    "FACTORY_REPO": "acme/widget",
    "FACTORY_REPO_PATH": "/srv/hermes/widget",
    "FACTORY_BOARD": "main",
    "FACTORY_OWNER_LOGIN": "owner-login",
    "FACTORY_OWNER_CHAT": "telegram:000000000",
    "FACTORY_BASE_BRANCHES": "main",
})
SCRIPT = os.environ.get("PR_HYGIENE_SCRIPT") or os.path.join(
    HERE, "pr-hygiene.py")
spec = importlib.util.spec_from_file_location("pr_hygiene_summary", SCRIPT)
if spec is None or spec.loader is None:
    print("SKIP: cannot load pr-hygiene.py")
    sys.exit(0)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# A reviewer verdict that mentions the heading in prose (the live shape that
# a substring search mistook for the summary).
VERDICT_MENTION = '''## Review verdict

**PASS** on branch head `819e741fdd19ce94baadce77cf49e3f972caa671`.

CI: 5 of 5 green on the head. PR is MERGEABLE / mergeStateStatus CLEAN.

### What was verified

- Wire shape against the protocol spec: all fields match the live IT assertions.
- No deleted tests in the diff.

Next step per convention: the `## What changes for you` comment is published by the coder after this PASS, once, on the final head.'''

# A real summary comment: it starts with the heading.
REAL_SUMMARY = '''## What changes for you

1. **Timeout errors now show which phase timed out.** Instead of a generic "Connection timed out" banner, you now see whether the transport connect or the initialize step exceeded its budget.

2. **Actionable hint.** A clickable link scrolls to the timeout input so you can adjust it right away.

**Evidence:**
- 27 error tests (up from 17)
- All UI tests pass, TypeScript compiles, lint clean'''


def with_payload(payload):
    real = mod.pr_payload
    mod.pr_payload = lambda n: payload
    try:
        return mod.has_user_summary(1)
    finally:
        mod.pr_payload = real


def test_live_verdict_mention_is_not_summary():
    """A verdict that mentions the heading in prose is not a summary."""
    assert not with_payload({"body": "",
                             "comments": [{"body": VERDICT_MENTION}]})
    assert not with_payload({"body": VERDICT_MENTION, "comments": []})


def test_real_summary_comment_is_summary():
    """A real summary comment (starts with the heading) is recognized."""
    assert with_payload({"body": "",
                         "comments": [{"body": REAL_SUMMARY}]})


def test_body_section_is_summary():
    body = "A short intro about the reason for the PR.\n\n" \
           "## What changes for you\n- timeouts show the phase"
    assert with_payload({"body": body, "comments": []})


def test_header_must_be_own_line_and_level():
    """The heading must stand on its OWN line and be exactly "## ...":
    an inline mention in backticks and "###" do not count."""
    inline = "The `## What changes for you` comment is due after PASS."
    assert not with_payload({"body": inline, "comments": []})
    assert not with_payload(
        {"body": "### What changes for you\n- text", "comments": []})


def test_comment_must_start_with_header():
    """A comment whose first non-empty line is not the heading is not a
    summary."""
    body = "Note: the summary follows.\n\n## What changes for you\n- x"
    assert not with_payload({"body": "", "comments": [{"body": body}]})


def main():
    test_live_verdict_mention_is_not_summary()
    test_real_summary_comment_is_summary()
    test_body_section_is_summary()
    test_header_must_be_own_line_and_level()
    test_comment_must_start_with_header()
    print("PR-HYGIENE SUMMARY HEADING TESTS PASSED")


if __name__ == "__main__":
    main()
