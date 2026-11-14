#!/usr/bin/env python3
"""Tests for the review loop guard. Run: <hermes venv>/bin/python test_guard.py"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import __init__ as guard  # noqa: E402


def _board(rounds, status="review", comment=None):
    """Temporary board with the given number of returns."""
    d = tempfile.mkdtemp()
    db = os.path.join(d, "kanban.db")
    con = sqlite3.connect(db)
    con.execute("create table tasks (id text primary key, status text)")
    con.execute("create table task_events (task_id text, kind text)")
    con.execute("create table task_comments (task_id text, body text)")
    con.execute("insert into tasks values ('t_x', ?)", (status,))
    for _ in range(rounds):
        con.execute("insert into task_events values ('t_x', 'changes_requested')")
    con.execute("insert into task_events values ('t_x', 'completed')")
    if comment:
        con.execute("insert into task_comments values ('t_x', ?)", (comment,))
    con.commit()
    con.close()
    return db


def test_counts_only_returns():
    db = _board(2)
    rounds, status = guard._rounds_and_status(db, "t_x")
    assert rounds == 2, rounds
    assert status == "review", status
    print("OK: counts only returns, other events are ignored")


def test_threshold():
    assert guard._rounds_and_status(_board(2), "t_x")[0] < guard.DEFAULT_LIMIT
    assert guard._rounds_and_status(_board(3), "t_x")[0] >= guard.DEFAULT_LIMIT
    print("OK: two returns pass, three stop the card")


def test_escalation_is_once():
    plain = _board(3)
    assert guard._already_escalated(plain, "t_x") is False
    marked = _board(3, comment="Review loop stopped: 3 returns")
    assert guard._already_escalated(marked, "t_x") is True
    print("OK: does not block twice")


def test_unknown_board_is_noop():
    assert guard._board_db(None) is None
    assert guard._board_db("no-such-board") is None
    guard._on_claimed(task_id="t_x", board=None)
    print("OK: silent and does not crash without a board")


if __name__ == "__main__":
    test_counts_only_returns()
    test_threshold()
    test_escalation_is_once()
    test_unknown_board_is_noop()
    print("all checks passed")
