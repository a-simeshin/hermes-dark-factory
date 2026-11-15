#!/usr/bin/env python3
"""Conflict-debt gate of board-watchdog.py.

CONFLICTING > 3 -> new milestone features (ready, coder, p < 60) are parked
in scheduled and do not start; debt <= 3 -> they come back by unblock. Covers
the selection boundaries, idempotency, silence on a stale cache and the dry
run.

Run: python3 test_board_watchdog_conflict_debt_gate.py
Mutant: BOARD_WATCHDOG_SCRIPT=/tmp/mutant.py python3 test_board_watchdog_conflict_debt_gate.py
"""
import atexit
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time

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
SCRIPT = os.environ.get("BOARD_WATCHDOG_SCRIPT") or os.path.join(
    HERE, "board-watchdog.py")

spec = importlib.util.spec_from_file_location("board_watchdog", SCRIPT)
if spec is None or spec.loader is None:
    print("SKIP: cannot load board-watchdog.py")
    sys.exit(0)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

SLUG = "main"


def make_board(tasks=()):
    """A mini board of the gate's slug in a temporary BOARDS directory."""
    tmp = tempfile.mkdtemp(prefix="wdtest5-")
    os.makedirs(os.path.join(tmp, SLUG), exist_ok=True)
    db = os.path.join(tmp, SLUG, "kanban.db")
    con = sqlite3.connect(db)
    con.executescript("""
        create table tasks (id text primary key, title text, body text default '',
            assignee text default 'ops', status text, priority int default 0,
            idempotency_key text);
        create table task_links (parent_id text, child_id text);
        create table task_runs (id integer primary key autoincrement,
            task_id text, profile text, status text, started_at int,
            ended_at int, outcome text, summary text, metadata text,
            error text);
        create table task_events (id integer primary key, task_id text,
            run_id int, kind text, payload text, created_at int);
        create table task_comments (id integer primary key, task_id text,
            body text, created_at int);
    """)
    for t in tasks:
        con.execute(
            "insert into tasks (id, title, body, status, assignee,"
            " priority, idempotency_key) values (?,?,?,?,?,?,?)",
            (t["id"], t.get("title", ""), t.get("body", ""),
             t.get("status", "ready"), t.get("assignee", "coder"),
             t.get("priority", 80), t.get("key")))
    con.commit()
    con.close()
    old_boards, old_cache, old_hold = mod.BOARDS, mod.DEBT_CACHE, \
        mod.MERGEFLOW_HOLD
    mod.BOARDS = tmp
    mod.DEBT_CACHE = os.path.join(tmp, "debt.json")
    mod.MERGEFLOW_HOLD = os.path.join(tmp, "hold-absent")  # hold lifted
    return tmp, (old_boards, old_cache, old_hold)


def restore(saved):
    mod.BOARDS, mod.DEBT_CACHE, mod.MERGEFLOW_HOLD = saved


def write_debt(count, age_s=600):
    json.dump({"count": count, "ts": time.time() - age_s,
               "lines": {"develop/2.x": "06e1b0a"}},
              open(mod.DEBT_CACHE, "w"))


def stub_hermes(db):
    """run() emulates schedule/unblock directly on the mini board."""
    calls = []

    def fake(cmd, timeout=120):
        calls.append(cmd)
        if len(cmd) >= 4 and cmd[1] == "kanban" and cmd[4] == "schedule":
            tid = cmd[5]
            con = sqlite3.connect(db)
            con.execute("update tasks set status='scheduled' where id=?",
                        (tid,))
            con.commit()
            con.close()
            return "scheduled"
        if len(cmd) >= 4 and cmd[1] == "kanban" and cmd[4] == "unblock":
            tid = cmd[5]
            con = sqlite3.connect(db)
            con.execute("update tasks set status='ready' where id=?", (tid,))
            con.commit()
            con.close()
            return "ready"
        return ""
    mod.run = fake
    return calls


def board_tasks():
    return [
        {"id": "t_feat30", "priority": 30, "title": "new feature A"},
        {"id": "t_feat40", "priority": 40, "title": "coding from the plan B"},
        {"id": "t_fix80", "priority": 80, "title": "fix for an open PR"},
        {"id": "t_wave92", "priority": 92, "title": "twin wave"},
        {"id": "t_rebase96", "priority": 96, "title": "rebase PR #210",
         "key": "coder-pr210-rebase-06e1b0a"},
        {"id": "t_arch20", "priority": 20, "assignee": "architect",
         "title": "plan"},
        {"id": "t_feat59", "priority": 59, "title": "feature on the boundary"},
    ]


def statuses(db):
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    rows = dict(con.execute("select id, status from tasks"))
    con.close()
    return rows


def test_parks_only_features_above_limit():
    """Debt 18: only coder features with p < 60 and no rebase key are parked."""
    tmp, saved = make_board(board_tasks())
    try:
        db = os.path.join(tmp, SLUG, "kanban.db")
        write_debt(18)
        calls = stub_hermes(db)
        state = {}
        out = mod.conflict_debt_gate(SLUG, state, apply=True)
        assert out and out[0] == "mergeflow-debtgate:18", out
        st = statuses(db)
        assert st["t_feat30"] == "scheduled", st
        assert st["t_feat40"] == "scheduled", st
        assert st["t_feat59"] == "scheduled", st
        assert st["t_fix80"] == "ready", st
        assert st["t_wave92"] == "ready", st
        assert st["t_rebase96"] == "ready", st
        assert st["t_arch20"] == "ready", st
        assert set(state["conflict_debt_parked"]) == {
            "t_feat30", "t_feat40", "t_feat59"}, state
        sched = [c for c in calls if c[4] == "schedule"]
        assert len(sched) == 3, calls
    finally:
        restore(saved)
    print("OK test_parks_only_features_above_limit")


def test_stable_state_is_silent():
    """Everything is parked, no new candidates: the gate is silent."""
    tmp, saved = make_board([{"id": "t_feat30", "priority": 30,
                              "status": "scheduled"}])
    try:
        write_debt(18)
        stub_hermes(os.path.join(tmp, SLUG, "kanban.db"))
        state = {"conflict_debt_parked": {"t_feat30": time.time()}}
        assert mod.conflict_debt_gate(SLUG, state) is None
    finally:
        restore(saved)
    print("OK test_stable_state_is_silent")


def test_returns_below_limit():
    """Debt is back within the limit: our card returns, a foreign scheduled
    card stays where it is."""
    tmp, saved = make_board([
        {"id": "t_ours", "priority": 30, "status": "scheduled"},
        {"id": "t_theirs", "priority": 30, "status": "scheduled"},
    ])
    try:
        db = os.path.join(tmp, SLUG, "kanban.db")
        write_debt(2)
        calls = stub_hermes(db)
        state = {"conflict_debt_parked": {"t_ours": time.time()}}
        out = mod.conflict_debt_gate(SLUG, state)
        assert out and out[0] == "mergeflow-debtgate-release:2", out
        st = statuses(db)
        assert st["t_ours"] == "ready", st
        assert st["t_theirs"] == "scheduled", st
        assert state["conflict_debt_parked"] == {}, state
        unblocks = [c for c in calls if c[4] == "unblock"]
        assert len(unblocks) == 1 and "t_ours" in unblocks[0], calls
    finally:
        restore(saved)
    print("OK test_returns_below_limit")


def test_stale_cache_is_silent():
    """Cache older than the TTL: no parking on a stale number."""
    tmp, saved = make_board(board_tasks())
    try:
        write_debt(18, age_s=4 * 3600)
        stub_hermes(os.path.join(tmp, SLUG, "kanban.db"))
        assert mod.conflict_debt_gate(SLUG, {}) is None
        st = statuses(os.path.join(tmp, SLUG, "kanban.db"))
        assert all(s == "ready" for s in st.values()), st
    finally:
        restore(saved)
    print("OK test_stale_cache_is_silent")


def test_dry_apply_false_touches_nothing():
    """apply=False: prints the plan, changes neither statuses nor state."""
    tmp, saved = make_board(board_tasks())
    try:
        write_debt(18)
        stub_hermes(os.path.join(tmp, SLUG, "kanban.db"))
        state = {}
        out = mod.conflict_debt_gate(SLUG, state, apply=False)
        assert out and "dry-run" in out[1], out
        st = statuses(os.path.join(tmp, SLUG, "kanban.db"))
        assert all(s == "ready" for s in st.values()), st
        assert "conflict_debt_parked" not in state, state
    finally:
        restore(saved)
    print("OK test_dry_apply_false_touches_nothing")


def test_other_board_ignored():
    """The gate is bound to the configured board: other boards are untouched."""
    tmp, saved = make_board(board_tasks())
    try:
        write_debt(18)
        stub_hermes(os.path.join(tmp, SLUG, "kanban.db"))
        assert mod.conflict_debt_gate("product", {}) is None
    finally:
        restore(saved)
    print("OK test_other_board_ignored")


def test_boundary_priority_60():
    """p59 is parked, p60 is already work on an open PR: not parked."""
    tmp, saved = make_board(board_tasks())
    try:
        ids = {tid for tid, _, _ in mod.milestone_feature_cards(SLUG)}
        assert "t_feat59" in ids and "t_fix80" not in ids, ids
        assert "t_rebase96" not in ids and "t_arch20" not in ids, ids
    finally:
        restore(saved)
    print("OK test_boundary_priority_60")


def test_hold_file_silences_gate():
    """The hold file: the gate is silent at any debt and parks nothing."""
    tmp, saved = make_board(board_tasks())
    try:
        write_debt(18)
        stub_hermes(os.path.join(tmp, SLUG, "kanban.db"))
        mod.MERGEFLOW_HOLD = os.path.join(tmp, "mergeflow-hold")
        open(mod.MERGEFLOW_HOLD, "w").write("hold")
        assert mod.conflict_debt_gate(SLUG, {}) is None
        st = statuses(os.path.join(tmp, SLUG, "kanban.db"))
        assert all(s == "ready" for s in st.values()), st
    finally:
        restore(saved)
    print("OK test_hold_file_silences_gate")


def test_release_blocker_not_parked():
    """A ready feature marked [release-blocker] is not parked even at debt
    18 (a base-branch defect outranks rebases)."""
    tasks = board_tasks() + [
        {"id": "t_blocker30", "priority": 30,
         "title": "[release-blocker] base branch defect p30"},
    ]
    tmp, saved = make_board(tasks)
    try:
        db = os.path.join(tmp, SLUG, "kanban.db")
        write_debt(18)
        calls = stub_hermes(db)
        ids = {tid for tid, _, _ in mod.milestone_feature_cards(SLUG)}
        assert "t_blocker30" not in ids and "t_feat30" in ids, ids
        state = {}
        out = mod.conflict_debt_gate(SLUG, state, apply=True)
        assert out and out[0] == "mergeflow-debtgate:18", out
        st = statuses(db)
        assert st["t_blocker30"] == "ready", st
        assert "t_blocker30" not in state["conflict_debt_parked"], state
        sched = [c for c in calls if c[4] == "schedule"]
        assert all("t_blocker30" not in c for c in sched), calls
    finally:
        restore(saved)
    print("OK test_release_blocker_not_parked")


def test_release_blocker_parked_returned_early():
    """A marked card parked BEFORE the rule came in is returned early while
    the debt is above the limit."""
    tmp, saved = make_board([
        {"id": "t_blk", "priority": 30, "status": "scheduled",
         "title": "[release-blocker] base branch defect"},
        {"id": "t_feat30", "priority": 30, "title": "feature A"},
    ])
    try:
        db = os.path.join(tmp, SLUG, "kanban.db")
        write_debt(18)
        calls = stub_hermes(db)
        state = {"conflict_debt_parked": {"t_blk": time.time()}}
        out = mod.conflict_debt_gate(SLUG, state, apply=True)
        assert out and out[0] == "mergeflow-debtgate:18", out
        assert "t_blk" in out[1] and "returned early" in out[1], out[1]
        st = statuses(db)
        assert st["t_blk"] == "ready", st
        assert st["t_feat30"] == "scheduled", st
        assert "t_blk" not in state["conflict_debt_parked"], state
        unblocks = [c for c in calls if c[4] == "unblock"]
        assert len(unblocks) == 1 and "t_blk" in unblocks[0], calls
    finally:
        restore(saved)
    print("OK test_release_blocker_parked_returned_early")


def test_no_marker_no_early_return():
    """Control: without the marker there is no early return, only at debt <= 3."""
    tmp, saved = make_board([
        {"id": "t_plain", "priority": 30, "status": "scheduled",
         "title": "feature without a marker"},
    ])
    try:
        db = os.path.join(tmp, SLUG, "kanban.db")
        write_debt(18)
        calls = stub_hermes(db)
        state = {"conflict_debt_parked": {"t_plain": time.time()}}
        out = mod.conflict_debt_gate(SLUG, state, apply=True)
        st = statuses(db)
        assert st["t_plain"] == "scheduled", st
        unblocks = [c for c in calls if c[4] == "unblock"]
        assert not unblocks, calls
        assert out is None or "t_plain" not in (out[1] if out else ""), out
    finally:
        restore(saved)
    print("OK test_no_marker_no_early_return")


if __name__ == "__main__":
    test_parks_only_features_above_limit()
    test_stable_state_is_silent()
    test_returns_below_limit()
    test_stale_cache_is_silent()
    test_dry_apply_false_touches_nothing()
    test_other_board_ignored()
    test_boundary_priority_60()
    test_hold_file_silences_gate()
    test_release_blocker_not_parked()
    test_release_blocker_parked_returned_early()
    test_no_marker_no_early_return()
    print("ALL PASS")
