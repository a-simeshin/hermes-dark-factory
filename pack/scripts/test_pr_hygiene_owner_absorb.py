#!/usr/bin/env python3
"""pr-hygiene: a rebase is absorbed by the live owner card of the PR.

Origin: a PR (branch hermes/120-client-demo-e2e) conflicted and pr-hygiene
created a rebase card, although the branch owner (key coder-120-demo-e2e) was
alive and planned the rebase itself: two coder cards of one PR in ready.
The test pins it down: the owner is visible in live_cards_by_pr with rank 3,
and absorbs_rebase returns True for the decomposition key.

Run: python3 test_pr_hygiene_owner_absorb.py
"""
import importlib.util
import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.update({
    "HERMES_HOME": tempfile.mkdtemp(prefix="ph-owner-absorb-"),
    "FACTORY_REPO": "acme/widget",
    "FACTORY_REPO_PATH": "/srv/hermes/widget",
    "FACTORY_BOARD": "main",
    "FACTORY_OWNER_LOGIN": "owner-login",
    "FACTORY_OWNER_CHAT": "telegram:000000000",
    "FACTORY_BASE_BRANCHES": "main",
    "FACTORY_ISSUE_BRANCH_PREFIX": "hermes/",
})
SCRIPT = os.environ.get("PR_HYGIENE_SCRIPT") or os.path.join(
    HERE, "pr-hygiene.py")

spec = importlib.util.spec_from_file_location("pr_hygiene", SCRIPT)
if spec is None or spec.loader is None:
    print("SKIP: cannot load pr-hygiene.py")
    sys.exit(0)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def with_db(cards):
    """A temp board with tasks(id,title,idempotency_key,branch_name,assignee,status)."""
    tmp = tempfile.mkdtemp(prefix="prhyg-owner-")
    db = os.path.join(tmp, "kanban.db")
    con = sqlite3.connect(db)
    con.execute("create table tasks (id text, title text,"
                " idempotency_key text, branch_name text,"
                " assignee text, status text)")
    con.executemany("insert into tasks values (?,?,?,?,?,?)", cards)
    con.commit()
    con.close()
    old = mod.BOARD_DB
    mod.BOARD_DB = db
    return old, tmp


def test_owner_rank3():
    """The owner coder-120-demo-e2e is visible for PR 413 by the branch
    hermes/120-client-demo-e2e; the branch slug is longer than the key slug."""
    old, tmp = with_db([
        ("t_71ab44ac", "[coder] issue #120 C4: demo + E2E",
         "coder-120-demo-e2e", None, "coder", "ready"),
    ])
    try:
        by_pr = mod.live_cards_by_pr({413: "hermes/120-client-demo-e2e"})
        assert 413 in by_pr, "owner card not mapped: %s" % by_pr
        tid, title, rank, key, assignee = by_pr[413][0]
        assert tid == "t_71ab44ac" and rank == 3 and assignee == "coder"
    finally:
        mod.BOARD_DB = old
    print("OK test_owner_rank3")


def test_owner_absorbs():
    """A decomposition key absorbs the rebase (assignee coder); others do not."""
    assert mod.absorbs_rebase("[coder] issue #120 C4", "coder-120-demo-e2e",
                              "coder") is True
    # a rebase card absorbs as before
    assert mod.absorbs_rebase("rebase PR #9", "coder-pr9-rebase-abc1234",
                              "coder") is True
    # not coder and no key convention: no absorption
    assert mod.absorbs_rebase("[qa] visual delta #9", "", "qa") is False
    assert mod.absorbs_rebase("[coder] decomp without a key", "",
                              "coder") is False
    # a rework key (owner-review-*) and the word rework in the title, as before
    assert mod.absorbs_rebase("rework", "owner-review-9-abc1234",
                              "coder") is True
    assert mod.absorbs_rebase("PR 9 rework", "", "coder") is True
    print("OK test_owner_absorbs")


def test_sibling_layers_no_false_match():
    """Layers C1-C3 (core-capture, correlation, client-ui) do not map onto
    the C4 branch; done cards do not take part; ambiguity = no carrier."""
    old, tmp = with_db([
        ("t_c3", "[coder] issue #120 C3", "coder-120-client-ui",
         None, "coder", "done"),
        ("t_c4", "[coder] issue #120 C4", "coder-120-demo-e2e",
         None, "coder", "ready"),
    ])
    try:
        by_pr = mod.live_cards_by_pr({413: "hermes/120-client-demo-e2e"})
        tids = [c[0] for c in by_pr.get(413, [])]
        assert tids == ["t_c4"], "expected only live owner, got %s" % tids
        # two open PRs of one issue fit one slug: no carrier is counted
        by_pr2 = mod.live_cards_by_pr({
            413: "hermes/120-demo-e2e", 414: "hermes/120-x-demo-e2e"})
        assert 413 not in by_pr2 and 414 not in by_pr2, by_pr2
    finally:
        mod.BOARD_DB = old
    print("OK test_sibling_layers_no_false_match")


if __name__ == "__main__":
    test_owner_absorbs()
    test_owner_rank3()
    test_sibling_layers_no_false_match()
    print("PASS owner absorbs rebase")
