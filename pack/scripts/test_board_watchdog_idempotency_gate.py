#!/usr/bin/env python3
"""Test of the idempotency gate in board-watchdog.py.

The gate does not just block an agent card that has no idempotency_key: it
recreates the card with a deterministic key and archives the original (the
owner's rule: the gate that catches it also fixes it). The blocker tests must
fail if the gate is rolled back to "comment + needs_input + silence".
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


def make_board(tasks=(), links=()):
    """Builds a mini board in a temporary directory. Returns (slug, tmpdir, old BOARDS).

    tasks: (id, title, status, created_by, idempotency_key, age_seconds)
    links: (parent_id, child_id)
    """
    tmp = tempfile.mkdtemp(prefix="wdtest-ikeygate-")
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
    now = time.time()
    for tid, title, status, created_by, key, age in tasks:
        con.execute(
            "insert into tasks (id, title, status, created_by, created_at,"
            " idempotency_key, body, assignee, priority) values (?,?,?,?,?,?,?,?,?)",
            (tid, title, status, created_by, int(now - age), key,
             "Card body %s" % tid, "coder", 42))
    for parent, child in links:
        con.execute("insert into task_links (parent_id, child_id) values (?,?)",
                    (parent, child))
    con.commit()
    con.close()
    old_boards = mod.BOARDS
    mod.BOARDS = tmp
    return slug, tmp, old_boards


def fake_hermes(calls, new_id="t_recreated"):
    """Mock of run(): create --json answers with a fresh card, the rest ''.
    The --body-file content is read at call time: the gate deletes the
    file right after create."""
    holder = {}
    def _run(cmd, timeout=120):
        calls.append(cmd)
        if len(cmd) > 4 and cmd[4] == "create":
            holder["body"] = open(cmd[cmd.index("--body-file") + 1]).read()
            return json.dumps({"id": new_id, "status": "todo"})
        return ""
    _run.holder = holder
    return _run


def test_ready_card_recreated():
    """coder, ready, older than 1 h, no key: create with a key + archive of
    the original + comments on both cards. No block."""
    slug, tmp, old = make_board([
        ("t_agent", "Port formRefs in PR #186", "ready", "coder", "", 2 * 3600)])
    calls = []
    real_run = mod.run
    mock = fake_hermes(calls)
    mod.run = mock
    try:
        found = mod.idempotency_gate(slug)
    finally:
        mod.run = real_run
        mod.BOARDS = old
    creates = [c for c in calls if len(c) > 4 and c[4] == "create"]
    assert creates, "no create call: %s" % calls
    cmd = creates[0]
    assert "--idempotency-key" in cmd, "create without key: %s" % cmd
    key = cmd[cmd.index("--idempotency-key") + 1]
    assert key == "coder-pr186-t_agent-ikeygate", key
    assert "Port formRefs in PR #186" in cmd, "title lost: %s" % cmd
    assert "--created-by" in cmd and cmd[cmd.index("--created-by") + 1] == "coder"
    assert "--priority" in cmd and cmd[cmd.index("--priority") + 1] == "42"
    assert "--assignee" in cmd and cmd[cmd.index("--assignee") + 1] == "coder"
    # The body starts with the native guard receipt (native-or-live rule),
    # then the original body follows unchanged.
    body = mock.holder.get("body") or ""
    assert body.endswith("Card body t_agent"), mock.holder
    assert body.startswith("## Native guard (native-or-live rule)"), mock.holder
    assert "native checked, does not cover" in body, mock.holder
    archives = [c for c in calls if len(c) > 4 and c[4] == "archive"]
    assert any("t_agent" in c for c in archives), "no archive: %s" % calls
    comments = [c for c in calls if len(c) > 4 and c[4] == "comment"]
    assert any("t_agent" in c and "t_recreated" in " ".join(c) for c in comments)
    assert any("t_recreated" in c for c in comments), "no provenance comment"
    assert not any(len(c) > 4 and c[4] == "block" for c in calls), \
        "block must not be issued when recreate works: %s" % calls
    assert any(f[0] == "ikeygate:t_agent" and "t_recreated" in f[2]
               for f in found), found
    shutil.rmtree(tmp)
    print("OK test_ready_card_recreated")


def test_parents_carried_over():
    """The parents of the original move into create through --parent."""
    slug, tmp, old = make_board(
        tasks=[("t_kid", "rework #154", "todo", "coder", "", 3 * 3600)],
        links=[("t_par1", "t_kid"), ("t_par2", "t_kid")])
    calls = []
    real_run = mod.run
    mod.run = fake_hermes(calls)
    try:
        mod.idempotency_gate(slug)
    finally:
        mod.run = real_run
        mod.BOARDS = old
    create = [c for c in calls if len(c) > 4 and c[4] == "create"][0]
    parents = [create[i + 1] for i, a in enumerate(create) if a == "--parent"]
    assert sorted(parents) == ["t_par1", "t_par2"], parents
    shutil.rmtree(tmp)
    print("OK test_parents_carried_over")


def test_user_card_untouched():
    """created_by=user: the gate does not touch it."""
    slug, tmp, old = make_board([
        ("t_user", "Human card", "ready", "user", "", 5 * 3600)])
    calls = []
    real_run = mod.run
    mod.run = fake_hermes(calls)
    try:
        found = mod.idempotency_gate(slug)
    finally:
        mod.run = real_run
        mod.BOARDS = old
    assert found == [], "user card must not trigger: %s" % found
    assert calls == [], "no CLI calls expected: %s" % calls
    shutil.rmtree(tmp)
    print("OK test_user_card_untouched")


def test_auto_decomposer_untouched():
    """auto-decomposer is exempt by an owner decision: not touched."""
    slug, tmp, old = make_board([
        ("t_adec", "Decomposition children", "todo", "auto-decomposer", "", 5 * 3600)])
    calls = []
    real_run = mod.run
    mod.run = fake_hermes(calls)
    try:
        found = mod.idempotency_gate(slug)
    finally:
        mod.run = real_run
        mod.BOARDS = old
    assert found == []
    assert calls == []
    shutil.rmtree(tmp)
    print("OK test_auto_decomposer_untouched")


def test_blocked_without_gate_comment_reported_not_touched():
    """blocked without a gate comment (blocked by another rule): the gate
    does not recreate and makes no CLI calls, but reports a finding."""
    slug, tmp, old = make_board([
        ("t_blk", "Already blocked", "blocked", "coder", "", 5 * 3600)])
    calls = []
    real_run = mod.run
    mod.run = fake_hermes(calls)
    try:
        found = mod.idempotency_gate(slug)
    finally:
        mod.run = real_run
        mod.BOARDS = old
    assert calls == []
    assert len(found) == 1, found
    assert found[0][0] == "ikeygate-blocked:t_blk", found
    assert "is blocked by another rule" in found[0][2], found
    assert "not recreated" in found[0][2], found
    shutil.rmtree(tmp)
    print("OK test_blocked_without_gate_comment_reported_not_touched")


def test_blocked_by_gate_recreated():
    """blocked with a gate comment (stuck on the old behaviour): recreated
    and archived, block is not issued again."""
    slug, tmp, old = make_board([
        ("t_stuck", "Rebase PR #188", "blocked", "reviewer", "", 30 * 3600)])
    db = os.path.join(tmp, slug, "kanban.db")
    con = sqlite3.connect(db)
    con.execute(
        "insert into task_comments (task_id, body, created_at) values (?,?,?)",
        ("t_stuck", mod.IKEYGATE_COMMENT_PREFIX + " card without "
         "idempotency-key, the repeat check does not see it.", int(time.time())))
    con.commit()
    con.close()
    calls = []
    real_run = mod.run
    mod.run = fake_hermes(calls)
    try:
        found = mod.idempotency_gate(slug)
    finally:
        mod.run = real_run
        mod.BOARDS = old
    assert any(len(c) > 4 and c[4] == "create" and "t_stuck" not in c
               and "--idempotency-key" in c for c in calls), calls
    assert any(len(c) > 4 and c[4] == "archive" and "t_stuck" in c
               for c in calls), calls
    assert not any(len(c) > 4 and c[4] == "block" for c in calls), calls
    comments = [c for c in calls if len(c) > 5 and c[4] == "comment"
                and c[5] == "t_stuck"]
    assert comments == [], "duplicate comment on gate-touched card: %s" % calls
    assert any(f[0] == "ikeygate:t_stuck" and "t_recreated" in f[2]
               for f in found), found
    shutil.rmtree(tmp)
    print("OK test_blocked_by_gate_recreated")


def test_young_card_untouched():
    """Younger than an hour: the creator still has a window to recreate it."""
    slug, tmp, old = make_board([
        ("t_young", "Fresh card", "ready", "coder", "", 10 * 60)])
    calls = []
    real_run = mod.run
    mod.run = fake_hermes(calls)
    try:
        found = mod.idempotency_gate(slug)
    finally:
        mod.run = real_run
        mod.BOARDS = old
    assert found == []
    assert calls == []
    shutil.rmtree(tmp)
    print("OK test_young_card_untouched")


def test_repass_after_failed_archive_no_noise():
    """create went through, archive failed (the card stays in the selection),
    second pass: create is idempotent with the same key, no second gate
    comment on the original."""
    slug, tmp, old = make_board([
        ("t_half", "Half done PR #190", "ready", "coder", "", 4 * 3600)])
    db = os.path.join(tmp, slug, "kanban.db")
    con = sqlite3.connect(db)
    con.execute(
        "insert into task_comments (task_id, body, created_at) values (?,?,?)",
        ("t_half", mod.IKEYGATE_COMMENT_PREFIX + " card without "
         "idempotency-key, the repeat check does not see it. Recreated "
         "by the gate as t_recreated", int(time.time())))
    con.commit()
    con.close()
    calls = []
    real_run = mod.run
    mod.run = fake_hermes(calls)
    try:
        mod.idempotency_gate(slug)
    finally:
        mod.run = real_run
        mod.BOARDS = old
    creates = [c for c in calls if len(c) > 4 and c[4] == "create"]
    assert creates, "idempotent create retry expected: %s" % calls
    key = creates[0][creates[0].index("--idempotency-key") + 1]
    assert key == "coder-pr190-t_half-ikeygate", key
    comments = [c for c in calls if len(c) > 5 and c[4] == "comment"
                and c[5] == "t_half"]
    assert comments == [], "duplicate gate comment on re-pass: %s" % calls
    shutil.rmtree(tmp)
    print("OK test_repass_after_failed_archive_no_noise")


def test_recreate_failure_falls_back_to_block():
    """create is silent (CLI error): fall back to the earlier behaviour for
    ready: comment, block needs_input, gate-alert escalation."""
    slug, tmp, old = make_board([
        ("t_fail", "Block will fail", "ready", "coder", "", 3 * 3600)])
    calls = []
    real_run = mod.run

    def _silent(cmd, timeout=120):
        calls.append(cmd)
        return ""
    mod.run = _silent
    try:
        found = mod.idempotency_gate(slug, state={}, now=time.time())
    finally:
        mod.run = real_run
        mod.BOARDS = old
    text = [f[2] for f in found if f[0] == "ikeygate:t_fail"][0]
    assert "NOT" in text, "failure not flagged:\n%s" % text
    blocks = [c for c in calls if len(c) > 4 and c[4] == "block"]
    assert any("t_fail" in c and "needs_input" in c for c in blocks), calls
    creates = [c for c in calls if len(c) > 4 and c[4] == "create"]
    assert any(mod.GATE_ALERT_KEY in c for c in creates), \
        "no gate-alert beacon create: %s" % calls
    shutil.rmtree(tmp)
    print("OK test_recreate_failure_falls_back_to_block")


def test_recreate_failure_todo_no_block():
    """create is silent for todo: block is not called (the core cannot),
    comment + gate-alert escalation."""
    slug, tmp, old = make_board([
        ("t_ftodo", "Todo and create fails", "todo", "coder", "", 3 * 3600)])
    calls = []
    real_run = mod.run

    def _silent(cmd, timeout=120):
        calls.append(cmd)
        return ""
    mod.run = _silent
    try:
        found = mod.idempotency_gate(slug, state={}, now=time.time())
    finally:
        mod.run = real_run
        mod.BOARDS = old
    assert not any(len(c) > 4 and c[4] == "block" for c in calls), calls
    assert any(len(c) > 4 and c[4] == "comment" and "t_ftodo" in c
               for c in calls), calls
    creates = [c for c in calls if len(c) > 4 and c[4] == "create"]
    assert any(mod.GATE_ALERT_KEY in c for c in creates), calls
    text = [f[2] for f in found if f[0] == "ikeygate:t_ftodo"][0]
    assert "todo" in text
    shutil.rmtree(tmp)
    print("OK test_recreate_failure_todo_no_block")


def test_three_first_touch_todo_one_escalation():
    """3 first-touch todo cards without a key and a silent create give ONE
    _gate_board_comment escalation per pass, not three (ikeygate-recreatefail
    is aggregated after the loop)."""
    slug, tmp, old = make_board([
        ("t_ft1", "First todo", "todo", "coder", "", 3 * 3600),
        ("t_ft2", "Second todo", "todo", "coder", "", 3 * 3600),
        ("t_ft3", "Third todo", "todo", "coder", "", 3 * 3600)])
    calls = []
    real_run = mod.run

    def _silent(cmd, timeout=120):
        calls.append(cmd)
        return ""
    esc = []
    real_gbc = mod._gate_board_comment
    mod._gate_board_comment = (
        lambda key, state, now, body: esc.append((key, body)))
    mod.run = _silent
    try:
        found = mod.idempotency_gate(slug, state={}, now=time.time())
    finally:
        mod.run = real_run
        mod._gate_board_comment = real_gbc
        mod.BOARDS = old
    assert len(esc) == 1, "expected 1 escalation, got %d: %s" % (
        len(esc), esc)
    key, body = esc[0]
    assert key == "ikeygate-recreatefail", key
    for tid in ("t_ft1", "t_ft2", "t_ft3"):
        assert tid in body, "%s missing from aggregated body: %s" % (
            tid, body)
    assert len(found) == 3, found
    shutil.rmtree(tmp)
    print("OK test_three_first_touch_todo_one_escalation")


def test_three_ready_blockfail_one_escalation():
    """Ready path: 3 cards with a silent create and a block that did not
    apply give one aggregated ikeygate-blockfail."""
    slug, tmp, old = make_board([
        ("t_bf%d" % i, "Ready will fail %d" % i, "ready", "coder", "", 3 * 3600)
        for i in range(3)])
    calls = []
    real_run = mod.run

    def _silent(cmd, timeout=120):
        calls.append(cmd)
        return ""
    esc = []
    real_gbc = mod._gate_board_comment
    mod._gate_board_comment = (
        lambda key, state, now, body: esc.append((key, body)))
    mod.run = _silent
    try:
        mod.idempotency_gate(slug, state={}, now=time.time())
    finally:
        mod.run = real_run
        mod._gate_board_comment = real_gbc
        mod.BOARDS = old
    assert len(esc) == 1, esc
    key, body = esc[0]
    assert key == "ikeygate-blockfail", key
    for tid in ("t_bf0", "t_bf1", "t_bf2"):
        assert tid in body, body
    shutil.rmtree(tmp)
    print("OK test_three_ready_blockfail_one_escalation")


def test_check_wires_gate():
    """check() calls the gate: a card without a key is recreated."""
    slug, tmp, old = make_board([
        ("t_wire", "Through check()", "ready", "reviewer", "", 2 * 3600)])
    calls = []
    real_run = mod.run
    mod.run = fake_hermes(calls)
    try:
        found = mod.check(slug)
    finally:
        mod.run = real_run
        mod.BOARDS = old
    assert any(f[0] == "ikeygate:t_wire" for f in found), \
        "check() does not wire idempotency_gate: %s" % [f[0] for f in found]
    assert any(len(c) > 4 and c[4] == "archive" and "t_wire" in c
               for c in calls), calls
    shutil.rmtree(tmp)
    print("OK test_check_wires_gate")


if __name__ == "__main__":
    test_ready_card_recreated()
    test_parents_carried_over()
    test_user_card_untouched()
    test_auto_decomposer_untouched()
    test_blocked_without_gate_comment_reported_not_touched()
    test_blocked_by_gate_recreated()
    test_young_card_untouched()
    test_repass_after_failed_archive_no_noise()
    test_recreate_failure_falls_back_to_block()
    test_recreate_failure_todo_no_block()
    test_three_first_touch_todo_one_escalation()
    test_three_ready_blockfail_one_escalation()
    test_check_wires_gate()
    print("ALL OK")
