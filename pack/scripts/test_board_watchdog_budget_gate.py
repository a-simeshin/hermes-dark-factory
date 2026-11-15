#!/usr/bin/env python3
"""Test of the gate for two consecutive budget burnouts in board-watchdog.py.

Data shapes:
  completed between burnouts: timed_out(budget) -> review_requested -> completed  (NOT a trigger)
  gave_up(budget) -> timed_out(budget)                                            (trigger, tail=2)
The blocker tests must fail if the detector is rolled back.
"""
import atexit
import importlib.util
import os
import shutil
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# Isolated Hermes home and neutral project settings, set before the script is loaded.
_HOME_TMP = tempfile.mkdtemp(prefix="wd-home-")
atexit.register(shutil.rmtree, _HOME_TMP, ignore_errors=True)
os.environ.update({
    "HERMES_HOME": _HOME_TMP,
    "FACTORY_REPO": "acme/widget",
    "FACTORY_REPO_PATH": "/srv/hermes/widget",
    "FACTORY_BOARD": "main",
    "FACTORY_OWNER_LOGIN": "owner-login",
    "FACTORY_OWNER_CHAT": "telegram:000000000",
    "FACTORY_BASE_BRANCHES": "main",
})
# BOARD_WATCHDOG_SCRIPT lets a mutation run test a modified copy of the script.
SCRIPT = os.environ.get("BOARD_WATCHDOG_SCRIPT") or os.path.join(
    HERE, "board-watchdog.py")

spec = importlib.util.spec_from_file_location("board_watchdog", SCRIPT)
if spec is None or spec.loader is None:
    print("SKIP: cannot load board-watchdog.py")
    sys.exit(0)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)



BUDGET = "Iteration budget exhausted (200/200) - task could not complete"


def make_board(tasks=(), runs=()):
    """Builds a mini board in a temporary directory. Returns (slug, tmpdir, old BOARDS)."""
    tmp = tempfile.mkdtemp(prefix="wdtest-")
    slug = "testboard"
    os.makedirs(os.path.join(tmp, "testboard"), exist_ok=True)
    db = os.path.join(tmp, "testboard", "kanban.db")
    con = sqlite3.connect(db)
    con.executescript("""
        create table tasks (id text primary key, title text, body text default '',
            assignee text default 'ops', status text, priority int default 0,
            created_by text default 'agent', created_at int,
            started_at int, completed_at int,
            workspace_kind text, workspace_path text, branch_name text,
            project_id text, claim_lock text, claim_expires int,
            tenant text, result text, idempotency_key text,
            consecutive_failures int default 0, worker_pid int,
            last_failure_error text, max_runtime_seconds int default null,
            last_heartbeat_at int, current_run_id int,
            workflow_template_id text, current_step_key text, skills text,
            model_override text, provider_override text,
            reasoning_effort text, max_retries int,
            goal_mode int default 0, goal_max_turns int,
            session_id text, block_kind text default null,
            block_recurrences int default 0);
        create table task_links (parent_id text, child_id text);
        create table task_runs (id integer primary key, task_id text,
            profile text, status text, started_at int, ended_at int,
            outcome text, summary text, metadata text, error text);
        create table task_events (id integer primary key, task_id text,
            run_id int, kind text, payload text, created_at int);
        create table task_comments (id integer primary key, task_id text,
            body text, created_at int);
    """)
    for t in tasks:
        con.execute("insert into tasks (id, title, status, goal_mode, block_kind)"
                    " values (?,?,?,?,?)", t)
    now = time.time()
    for i, (task_id, outcome, error, offset) in enumerate(runs):
        started = now - 6 * 24 * 3600 + offset
        con.execute(
            "insert into task_runs (task_id, started_at, ended_at, outcome, error)"
            " values (?,?,?,?,?)",
            (task_id, started, started + 3600, outcome, error))
    con.commit()
    con.close()
    old_boards = mod.BOARDS
    mod.BOARDS = tmp
    return slug, tmp, old_boards


def test_two_consecutive_burns():
    """gave_up(budget), timed_out(budget) -> tail=2, trigger."""
    tasks = [("t_burn2", "Rework PR #999 owner review", "ready", 0, None)]
    runs = [
        ("t_burn2", "gave_up", BUDGET, 100),
        ("t_burn2", "timed_out", BUDGET, 200),
    ]
    slug, tmp, old = make_board(tasks, runs)
    try:
        out = mod.consecutive_budget_gave_up(slug)
        assert len(out) == 1, "expected 1 hit, got %s" % out
        assert out[0][0] == "t_burn2"
        assert out[0][3] == 2, "tail must be 2, got %s" % out[0][3]
        print("OK test_two_consecutive_burns")
    finally:
        mod.BOARDS = old
        shutil.rmtree(tmp)


def test_single_burn_no_trigger():
    """One burnout is the budget_exhausted signal, not the gate."""
    tasks = [("t_one", "Solo", "ready", 0, None)]
    runs = [("t_one", "timed_out", BUDGET, 100)]
    slug, tmp, old = make_board(tasks, runs)
    try:
        out = mod.consecutive_budget_gave_up(slug)
        assert out == [], "single burn must not trigger, got %s" % out
        print("OK test_single_burn_no_trigger")
    finally:
        mod.BOARDS = old
        shutil.rmtree(tmp)


def test_completed_resets_series():
    """burn -> review_requested -> completed -> burn: tail=1."""
    tasks = [("t_hist", "History with a successful hand-in", "ready", 0, None)]
    runs = [
        ("t_hist", "timed_out", BUDGET, 100),
        ("t_hist", "review_requested", "", 200),
        ("t_hist", "completed", "", 300),
        ("t_hist", "timed_out", BUDGET, 400),
    ]
    slug, tmp, old = make_board(tasks, runs)
    try:
        out = mod.consecutive_budget_gave_up(slug)
        assert out == [], "completed must reset the series, got %s" % out
        print("OK test_completed_resets_series")
    finally:
        mod.BOARDS = old
        shutil.rmtree(tmp)


def test_intermediate_outcomes_do_not_reset():
    """burn -> crashed -> burn: the series is alive, none reached completed."""
    tasks = [("t_mix", "Mixed failures", "ready", 0, None)]
    runs = [
        ("t_mix", "timed_out", BUDGET, 100),
        ("t_mix", "crashed", "boom", 200),
        ("t_mix", "gave_up", BUDGET, 300),
    ]
    slug, tmp, old = make_board(tasks, runs)
    try:
        out = mod.consecutive_budget_gave_up(slug)
        assert len(out) == 1 and out[0][3] == 2, \
            "crashed between burns must not reset, got %s" % out
        print("OK test_intermediate_outcomes_do_not_reset")
    finally:
        mod.BOARDS = old
        shutil.rmtree(tmp)


def test_done_and_goal_skipped():
    """The gate does not touch closed cards and cards in goal mode."""
    tasks = [
        ("t_done", "Done", "done", 0, None),
        ("t_goal", "Goal", "ready", 1, None),
    ]
    runs = [
        ("t_done", "gave_up", BUDGET, 100),
        ("t_done", "timed_out", BUDGET, 200),
        ("t_goal", "gave_up", BUDGET, 300),
        ("t_goal", "timed_out", BUDGET, 400),
    ]
    slug, tmp, old = make_board(tasks, runs)
    try:
        out = mod.consecutive_budget_gave_up(slug)
        assert out == [], "done/goal must be skipped, got %s" % out
        print("OK test_done_and_goal_skipped")
    finally:
        mod.BOARDS = old
        shutil.rmtree(tmp)


def test_needs_input_not_retriggered():
    """A card already in the gate (needs_input): the detector is silent, no repeat comments."""
    tasks = [("t_gated", "Already blocked", "blocked", 0, "needs_input")]
    runs = [
        ("t_gated", "gave_up", BUDGET, 100),
        ("t_gated", "timed_out", BUDGET, 200),
    ]
    slug, tmp, old = make_board(tasks, runs)
    try:
        out = mod.consecutive_budget_gave_up(slug)
        assert out == [], "gated card must not retrigger, got %s" % out
        print("OK test_needs_input_not_retriggered")
    finally:
        mod.BOARDS = old
        shutil.rmtree(tmp)


def test_check_wires_block_and_violation_flag():
    """check() on the third burnout: block --kind needs_input + GATE VIOLATION."""
    tasks = [("t_three", "Three in a row", "ready", 0, None)]
    runs = [
        ("t_three", "gave_up", BUDGET, 100),
        ("t_three", "timed_out", BUDGET, 200),
        ("t_three", "gave_up", BUDGET, 300),
    ]
    slug, tmp, old = make_board(tasks, runs)
    calls = []
    real_run = mod.run
    mod.run = lambda cmd, timeout=120: calls.append(cmd) or ""
    try:
        found = mod.check(slug)
    finally:
        mod.run = real_run
        mod.BOARDS = old
    keys = [f[0] for f in found]
    assert "%s:budget-gate:t_three" % slug in keys, "no budget-gate entry: %s" % keys
    text = [f[2] for f in found if f[0].endswith("budget-gate:t_three")][0]
    assert "GATE VIOLATION" in text, "tail=3 must flag gate violation:\n%s" % text
    blocks = [c for c in calls if len(c) > 4 and c[1] == "kanban" and c[4] == "block"]
    assert any("t_three" in c and "needs_input" in c for c in blocks), \
        "no needs_input block call: %s" % calls
    comments = [c for c in calls if len(c) > 4 and c[1] == "kanban" and c[4] == "comment"]
    assert any("t_three" in c for c in comments), "no comment call: %s" % calls
    shutil.rmtree(tmp)
    print("OK test_check_wires_block_and_violation_flag")


def test_check_blocks_on_second_burn_without_violation():
    """check() on the second burnout: block present, no GATE VIOLATION."""
    tasks = [("t_two", "Two in a row", "ready", 0, None)]
    runs = [
        ("t_two", "gave_up", BUDGET, 100),
        ("t_two", "timed_out", BUDGET, 200),
    ]
    slug, tmp, old = make_board(tasks, runs)
    calls = []
    real_run = mod.run
    mod.run = lambda cmd, timeout=120: calls.append(cmd) or ""
    try:
        found = mod.check(slug)
    finally:
        mod.run = real_run
        mod.BOARDS = old
    text = [f[2] for f in found if f[0].endswith("budget-gate:t_two")]
    assert text, "no budget-gate entry in %s" % [f[0] for f in found]
    assert "GATE VIOLATION" not in text[0], "tail=2 is normal gate work:\n%s" % text[0]
    assert any(len(c) > 4 and c[4] == "block" and "t_two" in c
               and "needs_input" in c for c in calls), \
        "no needs_input block call: %s" % calls
    shutil.rmtree(tmp)
    print("OK test_check_blocks_on_second_burn_without_violation")


def test_check_already_blocked_no_reblock():
    """A card already blocked: the watchdog does not duplicate block/comment
    and does not claim in the alert text that it blocked the card."""
    tasks = [("t_blk", "Already blocked by another gate", "blocked", 0, "")]
    runs = [
        ("t_blk", "gave_up", BUDGET, 100),
        ("t_blk", "timed_out", BUDGET, 200),
    ]
    slug, tmp, old = make_board(tasks, runs)
    calls = []
    real_run = mod.run
    mod.run = lambda cmd, timeout=120: calls.append(cmd) or ""
    try:
        found = mod.check(slug)
    finally:
        mod.run = real_run
        mod.BOARDS = old
    text = [f[2] for f in found if f[0].endswith("budget-gate:t_blk")]
    assert text, "budget-gate entry must exist for blocked card too"
    assert "already stands blocked" in text[0], "alert must not claim a fresh block:\n%s" % text[0]
    assert not any(len(c) > 4 and c[4] == "block" and "t_blk" in c
                   for c in calls), "no block for already-blocked: %s" % calls
    # The progress comment after a burnout is a DIFFERENT marker
    # (BURN_PROGRESS_MARKER) and is written on a blocked card too: the metric
    # wants every burnout to carry "done/left". The budget gate itself does
    # not duplicate its block comment.
    non_progress = [c for c in calls if len(c) > 4 and c[4] == "comment"
                    and "t_blk" in c
                    and mod.BURN_PROGRESS_MARKER not in " ".join(c)]
    assert not non_progress, "budget-gate comment duplicated: %s" % non_progress
    shutil.rmtree(tmp)
    print("OK test_check_already_blocked_no_reblock")


def test_unblock_resets_series():
    """Manual unblock: the series counts only fresh burnouts.

    Otherwise after an unblock the watchdog would block the card again on the
    next tick from the old runs, before any new run.
    """
    tasks = [("t_re", "Unblocked by a human", "ready", 0, None)]
    runs = [
        ("t_re", "gave_up", BUDGET, 100),
        ("t_re", "timed_out", BUDGET, 200),
    ]
    slug, tmp, old = make_board(tasks, runs)
    try:
        db = os.path.join(tmp, "testboard", "kanban.db")
        con = sqlite3.connect(db)
        # the unblock is LATER than both burnouts
        con.execute(
            "insert into task_events (task_id, run_id, kind, payload, created_at)"
            " values ('t_re', null, 'unblocked', '{}', ?)",
            (time.time() - 6 * 24 * 3600 + 300,))
        con.commit()
        con.close()
        out = mod.consecutive_budget_gave_up(slug)
        assert out == [], "unblock must reset series, got %s" % out
        # ...but two NEW burnouts after the unblock trigger the gate again
        con = sqlite3.connect(db)
        now = time.time()
        for off in (400, 500):
            con.execute(
                "insert into task_runs (task_id, started_at, ended_at, outcome, error)"
                " values ('t_re', ?, ?, 'gave_up', ?)",
                (now - 6 * 24 * 3600 + off, now - 6 * 24 * 3600 + off + 3600, BUDGET))
        con.commit()
        con.close()
        out = mod.consecutive_budget_gave_up(slug)
        assert len(out) == 1 and out[0][3] == 2, \
            "two fresh burns after unblock must retrigger, got %s" % out
        print("OK test_unblock_resets_series")
    finally:
        mod.BOARDS = old
        shutil.rmtree(tmp)


if __name__ == "__main__":
    test_two_consecutive_burns()
    test_single_burn_no_trigger()
    test_completed_resets_series()
    test_intermediate_outcomes_do_not_reset()
    test_done_and_goal_skipped()
    test_needs_input_not_retriggered()
    test_check_wires_block_and_violation_flag()
    test_check_blocks_on_second_burn_without_violation()
    test_check_already_blocked_no_reblock()
    test_unblock_resets_series()
    print("ALL OK")
