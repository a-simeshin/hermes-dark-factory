#!/usr/bin/env python3
"""Tests of visual_delta in pr-hygiene.py.

The validator visual_delta_verdict (format of the Before / After section) is
covered rule by rule, and each test is proven red on a mutant (a copy of
pr-hygiene.py via PR_HYGIENE_SCRIPT):
  - no section -> violation (mutant: ba_section never returns None);
  - section not first -> violation (mutant: the `first` check is dropped);
  - an image does not answer 200 -> violation (mutant: status != 999);
  - Before and After are byte-identical -> violation
    (mutant: the comparison is disabled);
  - non-UI diff without mermaid -> violation (mutant: the requirement is
    dropped).

Visual delta lives in the issue, not in the PR: the PR branch of
visual_delta_check was removed (mutant: a return to the old signature
(prs, issues) breaks test_issue_scope_and_dry_run with duplicated numbers),
the issue branch checks our issues (label hermes*): mermaid or >= 2 images,
the section first.

Run: python3 test_pr_hygiene_visual_delta.py
"""
import importlib.util
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
# Settings are read at import time; the Hermes home is a temporary directory.
os.environ.update({
    "HERMES_HOME": tempfile.mkdtemp(prefix="ph-vdelta-"),
    "FACTORY_REPO": "acme/widget",
    "FACTORY_REPO_PATH": "/srv/hermes/widget",
    "FACTORY_BOARD": "main",
    "FACTORY_OWNER_LOGIN": "owner-login",
    "FACTORY_OWNER_CHAT": "telegram:000000000",
    "FACTORY_BASE_BRANCHES": "main",
})
SCRIPT = os.environ.get("PR_HYGIENE_SCRIPT") or os.path.join(
    HERE, "pr-hygiene.py")

spec = importlib.util.spec_from_file_location("pr_hygiene", SCRIPT)
if spec is None or spec.loader is None:
    print("SKIP: cannot load pr-hygiene.py")
    sys.exit(0)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
# Open milestones without gh: the first one is active, the cache lives in a
# temp directory.
if hasattr(mod, "_ms_open_milestones"):
    mod._ms_open_milestones = lambda repo: [
        {"title": "2.3.0", "due_on": "2026-11-23T00:00:00Z"},
        {"title": "2.4.0", "due_on": "2026-12-20T00:00:00Z"}]
    mod.milestone_bands.CACHE_PATH = os.path.join(
        tempfile.mkdtemp(prefix="phvd-mscache-"), "ms.json")

RAW = "https://raw.githubusercontent.com/o/r/hermes-assets/screenshots/%s.png"

UI_OK = """## Before / After

| Before | After |
|---|---|
| ![before](%s) | ![after](%s) |

After adds a leak badge next to the error badge.
""" % (RAW % "timeline-before", RAW % "timeline-after")

NON_UI_OK = """## Before / After

```mermaid
flowchart LR
  A[tool error] --> B{detector}
```

The detector flags the error, the UI shows a badge.
"""


def fetch_ok(url, timeout=12):
    return 200, url.encode()


def fetch_404(url, timeout=12):
    return 404, None


def fetch_same(url, timeout=12):
    return 200, b"identical-bytes"


def test_missing_section():
    # Mutant: `return None, False` -> `return "", False` in ba_section.
    bad = mod.visual_delta_verdict("## What\nA wall of text without a delta",
                                   ui=True, fetcher=fetch_ok)
    assert any("no '## Before / After' section" in v for v in bad), bad
    print("OK test_missing_section")


def test_section_must_be_first():
    # Mutant: delete the block `if not first: bad.append(...)`.
    body = "A wall of text first.\n\n" + UI_OK
    bad = mod.visual_delta_verdict(body, ui=True, fetcher=fetch_ok)
    assert any("FIRST" in v for v in bad), bad
    print("OK test_section_must_be_first")


def test_ui_images_must_answer_200():
    # Mutant: `if status != 200:` -> `if status != 999:`.
    bad = mod.visual_delta_verdict(UI_OK, ui=True, fetcher=fetch_404)
    assert any("HTTP 200" in v for v in bad), bad
    print("OK test_ui_images_must_answer_200")


def test_before_after_must_differ_bytes():
    # Mutant: `blobs[0] == blobs[1]` -> `False` (comparison disabled).
    bad = mod.visual_delta_verdict(UI_OK, ui=True, fetcher=fetch_same)
    assert any("byte-identical" in v for v in bad), bad
    print("OK test_before_after_must_differ_bytes")


def test_non_ui_needs_mermaid():
    # Mutant: `if not re.search(r"```mermaid"...)` -> `if False:`.
    body = "## Before / After\n\nText instead of a diagram.\n"
    bad = mod.visual_delta_verdict(body, ui=False, fetcher=fetch_ok)
    assert any("mermaid" in v for v in bad), bad
    print("OK test_non_ui_needs_mermaid")


def test_ui_ok_full_table():
    bad = mod.visual_delta_verdict(UI_OK, ui=True, fetcher=fetch_ok)
    assert bad == [], bad
    print("OK test_ui_ok_full_table")


def test_non_ui_mermaid_ok():
    bad = mod.visual_delta_verdict(NON_UI_OK, ui=False, fetcher=fetch_ok)
    assert bad == [], bad
    print("OK test_non_ui_mermaid_ok")


def test_prs_not_checked():
    # The PR branch is removed, the function takes one list of issues.
    # Mutant: a return to the old signature (prs, issues) fails with
    # TypeError on the second positional argument below.
    ok, lines, cards, numbers = mod.visual_delta_check([], dry_run=True)
    assert (ok, lines, cards, numbers) == (0, [], [], []), (ok, numbers)
    try:
        mod.visual_delta_check([], [], dry_run=True)
    except TypeError:
        pass
    else:
        raise AssertionError("visual_delta_check still takes two "
                             "positional lists (the old PR branch)")
    print("OK test_prs_not_checked")


def test_issue_scope_and_dry_run():
    # Our issues (label hermes*) are checked, others are not; dry-run creates
    # no cards; a violation is a missing section. Mutant: a return to the old
    # PR loop fails the test with duplicated numbers ([11, 11]).
    # UI filter: the fixtures carry the ui label (without it the issue falls
    # outside the rule entirely).
    issues = [
        {"number": 9, "labels": [{"name": "hermes"}, {"name": "ui"}],
         "body": NON_UI_OK, "title": "ours", "url": "u9"},
        {"number": 11, "labels": [{"name": "hermes"}, {"name": "ui"}],
         "body": "no section", "title": "ours second", "url": "u11"},
        {"number": 10, "labels": [{"name": "bug"}],
         "body": "no section", "title": "someone else's", "url": "u10"},
    ]
    ok, lines, cards, numbers = mod.visual_delta_check(issues, dry_run=True)
    assert ok == 1, ok                       # only issue 9 complies
    joined = "\n".join(lines)
    assert "issue #11" in joined, lines
    assert "issue #10" not in joined, lines
    assert cards == [], cards
    assert numbers == [11], numbers
    print("OK test_issue_scope_and_dry_run")


def test_card_only_when_section_missing():
    # A [visual-delta] card is created ONLY when the section is missing
    # entirely. A malformed section (present but not first / without mermaid
    # and images) stays a report finding without a card.
    # Mutant: `if not dry_run and section is None:` -> `if not dry_run:`
    # creates a card for a malformed section and fails the first assert.
    made = []
    orig_card = mod.visual_delta_card
    mod.visual_delta_card = lambda *a, **k: (made.append(a) or (True, "t_x"))
    try:
        # malformed section: not first in the body (ui label: UI filter)
        issues = [{"number": 21, "labels": [{"name": "hermes"},
                                            {"name": "ui"}],
                   "body": "intro text\n\n## Before / After\n```mermaid\n"
                           "flowchart LR\n  a-->b\n```\n",
                   "title": "malformed", "url": "u21"}]
        ok, lines, cards, numbers = mod.visual_delta_check(
            issues, dry_run=False)
        assert cards == [] and made == [], (cards, made)
        assert numbers == [21] and "issue #21" in "\n".join(lines)
        # no section at all -> a card is created (the issue is in the
        # active milestone, otherwise admission before create stops it)
        issues2 = [{"number": 22, "labels": [{"name": "hermes"},
                                             {"name": "ui"}],
                    "body": "just text without a section", "title": "empty",
                    "url": "u22", "milestone": {"title": "2.3.0"}}]
        ok2, lines2, cards2, numbers2 = mod.visual_delta_check(
            issues2, dry_run=False)
        assert made and made[0][1] == 22, made
        assert numbers2 == [22]
    finally:
        mod.visual_delta_card = orig_card
    print("OK test_card_only_when_section_missing")


def test_is_ui_path_removed():
    # The is_ui_path predicate (UI-ness by *.tsx/*.css) was removed as dead:
    # file paths do not make an issue a UI one, the criterion is a real
    # visible change on the screen. A mutant that brings the function back
    # fails the assert.
    assert not hasattr(mod, "is_ui_path"), \
        "is_ui_path is back: file extensions decide UI-ness again"
    print("OK test_is_ui_path_removed")


def main():
    test_missing_section()
    test_section_must_be_first()
    test_ui_images_must_answer_200()
    test_before_after_must_differ_bytes()
    test_non_ui_needs_mermaid()
    test_ui_ok_full_table()
    test_non_ui_mermaid_ok()
    test_prs_not_checked()
    test_issue_scope_and_dry_run()
    test_card_only_when_section_missing()
    test_is_ui_path_removed()
    print("ALL VISUAL-DELTA TESTS PASSED")


if __name__ == "__main__":
    main()
