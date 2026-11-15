#!/usr/bin/env python3
"""Parallel rework of one PR is counterproductive: children of one PR run
in a chain.

When an umbrella card closes on a verdict, the platform releases ALL its
children at once, and two coders push and rebase one branch in parallel.
Rule: one verdict on a PR = one coder card in progress; children of one PR
are chained with link <earlier> <later> (rebase/WCFY as the last link);
only cards of different PRs run in parallel.

Checks:
  1. two released children of one PR are chained, the second waits; a
     repeated pass is idempotent (no duplicate links or comments);
  2. cards of different PRs are not linked (parallel is allowed);
  3. both links running: no mutation, only a race alert (class owner);
  4. a rebase card is the last link regardless of created;
  5. a live umbrella is not part of the chain (it is a parent, not a peer);
  6. I1: before a blocked parent is removed, the children of one PR are
     chained, the parent is removed from both, a parallel release is
     ruled out.

The harness is imported from test_board_watchdog_board_lint.py; mutation
runs go through BOARD_WATCHDOG_SCRIPT.

Run: python3 test_board_watchdog_serial_rework.py
"""
import importlib.util
import os
import shutil
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.join(HERE, "test_board_watchdog_board_lint.py")
spec = importlib.util.spec_from_file_location("wd_lint_harness", HARNESS)
if spec is None or spec.loader is None:
    print("SKIP: cannot load harness")
    sys.exit(0)
h = importlib.util.module_from_spec(spec)
sys.modules["wd_lint_harness"] = h
spec.loader.exec_module(h)
mod = h.mod

MARK = mod.SERIAL_REWORK_MARKER


def with_serial(tasks=(), links=(), comments=()):
    """Board + run that understands kanban link (inserts the link, a ready
    child goes to todo, like the real CLI)."""
    slug, db, old_boards = h.make_board(tasks, links=links, comments=comments)
    calls = []
    base = h.fake_run(db, calls)
    old_run = mod.run

    def _run(cmd, timeout=120):
        if len(cmd) > 6 and cmd[1] == "kanban" and cmd[4] == "link":
            calls.append(cmd)
            con = sqlite3.connect(db, timeout=10)
            try:
                con.execute("insert into task_links values (?,?)",
                            (cmd[5], cmd[6]))
                con.execute("update tasks set status='todo'"
                            " where id=? and status='ready'", (cmd[6],))
                con.commit()
            finally:
                con.close()
            return "ok"
        return base(cmd, timeout)

    mod.run = _run
    mod.board_repo = lambda s: "test/repo"

    def restore():
        mod.run = old_run
        mod.BOARDS = old_boards
        shutil.rmtree(os.path.dirname(db), ignore_errors=True)

    return slug, db, calls, restore


def links_in_db(db):
    con = sqlite3.connect(db)
    rows = sorted(con.execute("select parent_id, child_id from task_links"))
    con.close()
    return rows


def comments_in_db(db, tid):
    con = sqlite3.connect(db)
    rows = [r[0] for r in con.execute(
        "select body from task_comments where task_id=?", (tid,))]
    con.close()
    return rows


def test_two_children_of_done_umbrella_chained():
    tasks = [
        {"id": "t_um", "title": "PR #346 rework (owner verdict)",
         "status": "done", "key": "owner-review-346-445d45e"},
        {"id": "t_b1", "title": "PR #346 block1: remove min-w-0",
         "status": "ready", "key": "owner-review-346-445d45e-block1",
         "age": 3600},
        {"id": "t_b2", "title": "PR #346 minor: tests, rebase",
         "status": "ready", "key": "owner-review-346-445d45e-minor",
         "age": 3500},
    ]
    slug, db, calls, restore = with_serial(
        tasks, links=[("t_um", "t_b1"), ("t_um", "t_b2")])
    try:
        found = mod.serial_rework_gate(slug)
        assert any(":serial:346:t_b1-t_b2" in f[0] for f in found), found
        assert ("t_b1", "t_b2") in links_in_db(db), links_in_db(db)
        # the marker comment is on the later link
        assert any(MARK in b for b in comments_in_db(db, "t_b2")), calls
        # the earlier link is untouched, the later one waits
        con = sqlite3.connect(db)
        st = dict(con.execute("select id, status from tasks"
                              " where id in ('t_b1','t_b2')"))
        con.close()
        assert st["t_b1"] == "ready" and st["t_b2"] == "todo", st
        # classification: routine, not a push
        assert mod.classify_finding(
            "testboard:serial:346:t_b1-t_b2") == "digest"
        # idempotency: the second pass adds no links or comments
        n_links = len([c for c in calls if c[4] == "link"])
        n_comm = len([c for c in calls if c[4] == "comment"])
        mod.serial_rework_gate(slug)
        assert len([c for c in calls if c[4] == "link"]) == n_links, calls
        assert len([c for c in calls if c[4] == "comment"]) == n_comm, calls
        print("OK test_two_children_of_done_umbrella_chained")
    finally:
        restore()


def test_different_prs_parallel_ok():
    tasks = [
        {"id": "t_a", "title": "PR #346 block1", "status": "ready",
         "key": "owner-review-346-445d45e-block1"},
        {"id": "t_b", "title": "PR #347 minor", "status": "ready",
         "key": "owner-review-347-99aa88b-minor"},
    ]
    slug, db, calls, restore = with_serial(tasks)
    try:
        assert mod.serial_rework_scan(slug) == []
        assert mod.serial_rework_gate(slug) == []
        assert links_in_db(db) == []
        print("OK test_different_prs_parallel_ok")
    finally:
        restore()


def test_race_alert_only_no_mutation():
    tasks = [
        {"id": "t_r1", "title": "PR #346 block1", "status": "running",
         "key": "owner-review-346-445d45e-block1", "age": 3600},
        {"id": "t_r2", "title": "PR #346 minor, rebase", "status": "running",
         "key": "owner-review-346-445d45e-minor", "age": 3500},
    ]
    slug, db, calls, restore = with_serial(tasks)
    try:
        found = mod.serial_rework_gate(slug)
        assert any(f[0].endswith(":race") for f in found), found
        assert "fetch" in h.texts(found), found
        assert links_in_db(db) == []
        assert not any(c[4] in ("link", "comment", "move", "edit")
                       for c in calls), calls
        # a race is an owner decision, not a digest item
        assert mod.classify_finding(
            "testboard:serial:346:t_r1-t_r2:race") == "owner"
        print("OK test_race_alert_only_no_mutation")
    finally:
        restore()


def test_rebase_card_goes_last():
    tasks = [
        # the rebase card is older but must become the last link
        {"id": "t_reb", "title": "PR #346 rebase onto develop/2.x",
         "status": "ready", "key": "rebase-346-445d45e", "age": 5000},
        {"id": "t_blk", "title": "PR #346 block1: min-w-0", "status": "ready",
         "key": "owner-review-346-445d45e-block1", "age": 1000},
    ]
    slug, db, calls, restore = with_serial(tasks)
    try:
        found = mod.serial_rework_gate(slug)
        assert any(":serial:346:t_blk-t_reb" in f[0] for f in found), found
        assert ("t_blk", "t_reb") in links_in_db(db), links_in_db(db)
        print("OK test_rebase_card_goes_last")
    finally:
        restore()


def test_live_umbrella_not_in_chain():
    tasks = [
        {"id": "t_um", "title": "PR #346 rework (owner verdict)",
         "status": "ready", "key": "owner-review-346-445d45e"},
        {"id": "t_b1", "title": "PR #346 block1", "status": "todo",
         "key": "owner-review-346-445d45e-block1", "age": 3600},
        {"id": "t_b2", "title": "PR #346 minor", "status": "todo",
         "key": "owner-review-346-445d45e-minor", "age": 3500},
    ]
    slug, db, calls, restore = with_serial(
        tasks, links=[("t_um", "t_b1"), ("t_um", "t_b2")])
    try:
        groups = mod.serial_rework_scan(slug)
        members = {c["id"] for _, cards in groups for c in cards}
        assert members == {"t_b1", "t_b2"}, groups
        found = mod.serial_rework_gate(slug)
        assert any(":serial:346:t_b1-t_b2" in f[0] for f in found), found
        rows = links_in_db(db)
        # the umbrella stays a parent (it is alive) but is not in the chain:
        # the children are linked to each other and run in sequence
        assert ("t_b1", "t_b2") in rows and ("t_um", "t_b1") in rows \
            and ("t_um", "t_b2") in rows, rows
        assert not any(p == "t_um" and c in ("t_um",) for p, c in rows), rows
        assert not any(c == "t_um" for _, c in rows), rows
        print("OK test_live_umbrella_not_in_chain")
    finally:
        restore()


def test_i1_same_pr_children_chained_before_unlink():
    tasks = [
        {"id": "t_p", "title": "PR #346 rework (owner verdict)",
         "status": "blocked", "key": "owner-review-346-445d45e"},
        {"id": "t_c1", "title": "PR #346 block1: min-w-0", "status": "todo",
         "key": "owner-review-346-445d45e-block1", "age": 3600},
        {"id": "t_c2", "title": "PR #346 minor: tests, rebase",
         "status": "todo", "key": "owner-review-346-445d45e-minor",
         "age": 3500},
    ]
    slug, db, calls, restore = with_serial(
        tasks, links=[("t_p", "t_c1"), ("t_p", "t_c2")])
    try:
        found = mod.board_lint(slug)
        txt = h.texts(found)
        assert "I1: children of one PR #346 are chained t_c1 -> t_c2" in txt, txt
        assert "I1 FIXED" in txt, txt
        rows = links_in_db(db)
        # the parent is removed from both, the children are chained
        assert ("t_c1", "t_c2") in rows, rows
        assert ("t_p", "t_c1") not in rows and ("t_p", "t_c2") not in rows, rows
        assert any(MARK in b for b in comments_in_db(db, "t_c2")), calls
        # a repeated pass adds nothing
        n = len(rows)
        mod.board_lint(slug)
        assert links_in_db(db) == rows, (links_in_db(db), rows)
        print("OK test_i1_same_pr_children_chained_before_unlink")
    finally:
        restore()


def test_i1_different_pr_children_unchanged():
    """I1 regression: children of different PRs are still unlinked
    independently, with no extra chain (the original lint behaviour)."""
    tasks = [
        {"id": "t_p", "title": "coder PR #186", "status": "blocked"},
        {"id": "t_c", "title": "Rework PR #186 per owner review",
         "status": "todo", "key": "owner-review-186-a75b92e", "priority": 80},
        {"id": "t_twin", "title": "[coder][1.x] fix (issue #170, 1.x twin)",
         "status": "todo"},
    ]
    slug, db, calls, restore = with_serial(
        tasks, links=[("t_p", "t_c"), ("t_p", "t_twin")])
    try:
        found = mod.board_lint(slug)
        txt = h.texts(found)
        assert "I1 FIXED" in txt, txt
        rows = links_in_db(db)
        assert ("t_p", "t_c") not in rows and ("t_p", "t_twin") not in rows, rows
        assert not any(p in ("t_c", "t_twin") for p, _ in rows), rows
        print("OK test_i1_different_pr_children_unchanged")
    finally:
        restore()


if __name__ == "__main__":
    test_two_children_of_done_umbrella_chained()
    test_different_prs_parallel_ok()
    test_race_alert_only_no_mutation()
    test_rebase_card_goes_last()
    test_live_umbrella_not_in_chain()
    test_i1_same_pr_children_chained_before_unlink()
    test_i1_different_pr_children_unchanged()
    print("ALL SERIAL REWORK TESTS PASSED")
