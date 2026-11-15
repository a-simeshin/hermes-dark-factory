#!/usr/bin/env python3
"""Tests for close_blockers in pr-hygiene.py: why it is too early to close an issue."""
import importlib.util
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
# Settings are read at import time; the Hermes home is a temporary directory.
os.environ.update({
    "HERMES_HOME": tempfile.mkdtemp(prefix="ph-close-"),
    "FACTORY_REPO": "acme/widget",
    "FACTORY_REPO_PATH": "/srv/hermes/widget",
    "FACTORY_BOARD": "main",
    "FACTORY_OWNER_LOGIN": "owner-login",
    "FACTORY_OWNER_CHAT": "telegram:000000000",
    "FACTORY_BASE_BRANCHES": "main",
})
# PR_HYGIENE_SCRIPT points at a mutated copy (mutation runs).
SCRIPT = os.environ.get("PR_HYGIENE_SCRIPT") or os.path.join(
    HERE, "pr-hygiene.py")

spec = importlib.util.spec_from_file_location("pr_hygiene", SCRIPT)
if spec is None or spec.loader is None:
    print("SKIP: cannot load pr-hygiene.py")
    sys.exit(0)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
close_blockers = mod.close_blockers


def test_unchecked_ac():
    """Two unchecked criteria: exactly one reason."""
    issue = {"number": 79, "body": "## Acceptance criteria\n- [ ] UI badge\n- [ ] Notice\n- [x] Backend done"}
    cards = []
    blockers = close_blockers(issue, cards)
    assert len(blockers) == 1, "expected 1 blocker, got %s" % blockers
    assert "unchecked acceptance criteria: 2" in blockers[0]
    print("OK test_unchecked_ac")


def test_live_cards():
    """Board cards: todo for #79, blocked for #790 and #7, archived for #79."""
    issue = {"number": 79, "body": "- [x] All done"}
    cards = [
        {"id": "t_done", "status": "done", "title": "Done", "body": "fixed #79"},
        {"id": "t_todo", "status": "todo", "title": "Todo", "body": "work on #79"},
        {"id": "t_blocked", "status": "blocked", "title": "Blocked", "body": "see #790 and #7"},
        {"id": "t_archived", "status": "archived", "title": "Old", "body": "was #79"},
    ]
    blockers = close_blockers(issue, cards)
    assert len(blockers) == 1, "expected 1 blocker, got %s" % blockers
    assert "t_todo" in blockers[0]
    assert "t_blocked" not in blockers[0]
    assert "t_archived" not in blockers[0]
    print("OK test_live_cards")


def test_no_blockers():
    """All criteria ticked, board empty: safe to close."""
    issue = {"number": 79, "body": "- [x] Done"}
    cards = [{"id": "t_done", "status": "done", "title": "Done", "body": "fixed #79"}]
    blockers = close_blockers(issue, cards)
    assert blockers == [], "expected no blockers, got %s" % blockers
    print("OK test_no_blockers")


def test_pull_ref_not_issue_ref():
    """pull/79 without a hash is not a reference to issue #79."""
    issue = {"number": 79, "body": "- [x] Done"}
    cards = [
        {"id": "t_pr", "status": "todo", "title": "Fix", "body": "see pull/79"},
    ]
    blockers = close_blockers(issue, cards)
    assert blockers == [], "expected no blockers, got %s" % blockers
    print("OK test_pull_ref_not_issue_ref")


def test_mixed():
    """Unchecked criteria and live cards: both reasons."""
    issue = {"number": 79, "body": "- [ ] Badge\n- [x] Backend"}
    cards = [
        {"id": "t_todo", "status": "todo", "title": "Todo", "body": "work on #79"},
    ]
    blockers = close_blockers(issue, cards)
    assert len(blockers) == 2, "expected 2 blockers, got %s" % blockers
    assert "unchecked acceptance criteria: 1" in blockers[0]
    assert "t_todo" in blockers[1]
    print("OK test_mixed")


if __name__ == "__main__":
    test_unchecked_ac()
    test_live_cards()
    test_no_blockers()
    test_pull_ref_not_issue_ref()
    test_mixed()
    print("ALL PASS")
