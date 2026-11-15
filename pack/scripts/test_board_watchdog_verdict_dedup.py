#!/usr/bin/env python3
"""Verdict item dedup gate, board-watchdog.py.

Reason: a card created from an owner verdict on a PR carried a Blocking item
"Before/After" that a live [visual-delta] card of the same PR already
covered, so two coders made the same screenshots. The fixture here is that
pair of live cards.

Mechanism: a pair of live cards of one PR; the later one has a blocker item
of a known class, the earlier one has the class in its title. The gate
subtracts the item (a line "Item N (...) covers t_..."), cleans the title and
writes a marker comment. The later card yields. The body is edited only
before the first run.

Run: python3 test_board_watchdog_verdict_dedup.py
Mutant: PYTHONPATH=<scripts dir> BOARD_WATCHDOG_SCRIPT=/tmp/mutant.py \
    python3 test_board_watchdog_verdict_dedup.py
(the positive tests must fail on a mutant: a red run proves they check
something).
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

# Settings and the Hermes home come from the environment, set before the
# script is loaded (the script reads them at import time).
_HERMES_HOME = tempfile.mkdtemp(prefix="wd-home-")
atexit.register(shutil.rmtree, _HERMES_HOME, ignore_errors=True)
os.environ.update({
    "HERMES_HOME": _HERMES_HOME,
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


# ---------- fixture: a live pair, a verdict card and a visual-delta card ----------

TITLE_VERDICT = ("[coder] PR #166 rework: merge prerequisites, Before/After"
                 " + update-branch (head 7e0f9ea)")
BODY_VERDICT = (
    "Owner review verdict on PR #166 (fix(ui): reconnect after silent stream"
    " drop resets stale session before connect), head"
    " 7e0f9ea876256cecb06174baf713309649dc2c41: CHANGES"
    " REQUESTED: minor. Code is accepted: the earlier leak is closed"
    " (seeds 1431545159 and -100038162 green, 10 random-seed runs 85/85),"
    " re-review APPROVED, CI green on 7e0f9ea, WCFY present, Refs #157."
    " Only merge prerequisites remain. Owner decision: #166 and #177 merge"
    " together.\n\n"
    "## Blocking item 1: PR body needs a Before/After section\n"
    "No \"## Before / After\" section at the top of the PR body. This is a"
    " UI change. Required:\n"
    "- two screenshots of the same demo stand after a silent stream drop\n"
    "- before (base: UI stays on the dead session)\n"
    "- after (this head: reconnects with a fresh session)\n"
    "- details stay inside <details> blocks\n"
    "Edit the PR body so the section sits at the top; keep the rest of the"
    " body intact.\n\n"
    "## Blocking item 2: update-branch + green CI\n"
    "develop/2.x moved 1 commit past this head:\n"
    "- run update-branch (merge the develop/2.x tip into the PR branch)\n"
    "- wait for a green CI run on the updated head\n"
    "- report the new head in a PR comment\n\n"
    "## Acceptance criteria\n"
    "- PR body has \"## Before / After\" at the top with both screenshots"
    " (before: dead session on base; after: fresh session on this head),"
    " details collapsed\n"
    "- branch carries the develop/2.x tip (update-branch), CI green on the"
    " new head, new head reported in a PR comment\n"
    "- no production code changes (the earlier code is accepted); if"
    " update-branch conflicts, report the conflict instead of reworking"
    " code\n")

TITLE_VD = "[visual-delta] PR #166: Before / After section"
BODY_VD = (
    "PR #166 violates the visual-delta rule:\n\n"
    "- no '## Before / After' section (first, before any text)\n\n"
    "Format: the body starts with the section `## Before / After`.\n"
    "- UI diff: a table | Before | After |, one screenshot per cell;\n"
    "  one screen in one state, PNG in the assets repository (raw).\n"
    "- non-UI: a mermaid diagram of the delta (flowchart/sequence/classes).\n"
    "After the section at most 5 lines, the rest goes into details.\n"
    "Capture in one call: the screenshot helper script\n\n"
    "https://github.com/acme/widget/pull/166")

TITLE_CLEAN = ("[coder] PR #166 rework: merge prerequisites, update-branch"
               " (head 7e0f9ea)")


def make_board(tasks=(), comments=()):
    """Mini board in a temporary directory.

    tasks: dict(id, title, body, status, assignee, created_ago)
    comments: dict(task_id, body)
    """
    tmp = tempfile.mkdtemp(prefix="wdtest29-")
    slug = "testboard"
    os.makedirs(os.path.join(tmp, slug), exist_ok=True)
    db = os.path.join(tmp, slug, "kanban.db")
    con = sqlite3.connect(db)
    con.executescript("""
        create table tasks (id text primary key, title text,
            body text default '', assignee text default 'ops', status text,
            priority int default 0, created_by text default 'agent',
            created_at int, started_at int, completed_at int,
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
        create table task_runs (id integer primary key autoincrement,
            task_id text, profile text, status text, started_at int,
            ended_at int, outcome text, summary text, metadata text,
            error text);
        create table task_events (id integer primary key, task_id text,
            run_id int, kind text, payload text, created_at int);
        create table task_comments (id integer primary key, task_id text,
            body text, created_at int);
    """)
    now = time.time()
    for t in tasks:
        con.execute(
            "insert into tasks (id, title, body, status, assignee,"
            " priority, created_at) values (?,?,?,?,?,?,?)",
            (t["id"], t.get("title", ""), t.get("body", ""),
             t.get("status", "ready"), t.get("assignee", "coder"),
             t.get("priority", 80), now - t.get("created_ago", 3600)))
    for c in comments:
        con.execute(
            "insert into task_comments (task_id, body, created_at)"
            " values (?,?,?)",
            (c["task_id"], c.get("body", ""), now))
    con.commit()
    con.close()
    old_boards = mod.BOARDS
    mod.BOARDS = tmp
    return slug, tmp, old_boards


def stub_run(calls):
    """Replaces mod.run: hermes commands are recorded in calls, not executed."""
    real = mod.run

    def fake(cmd, timeout=120):
        calls.append(cmd)
        return ""
    mod.run = fake
    return real


def restore(real, old_boards, tmp):
    mod.run = real
    mod.BOARDS = old_boards
    shutil.rmtree(tmp, ignore_errors=True)


def fx_pair(**over):
    """The live pair: visual-delta earlier (28.4 h ago), verdict later
    (1 h ago)."""
    vd = {"id": "t_3be430d2", "title": TITLE_VD, "body": BODY_VD,
          "status": "running", "created_ago": 28.4 * 3600}
    ver = {"id": "t_446ecc51", "title": TITLE_VERDICT, "body": BODY_VERDICT,
           "status": "ready", "created_ago": 3600}
    for k, v in over.items():
        if k.startswith("vd_"):
            vd[k[3:]] = v
        else:
            ver[k[4:]] = v
    return [vd, ver]


def hermes_cmds(calls, verb):
    return [c for c in calls
            if len(c) > 4 and c[0].endswith("hermes") and c[4] == verb]


# ---------- tests ----------

def test_items_parsed():
    """Items of the verdict body: two block headings with numbers."""
    items = mod.verdict_blocking_items(BODY_VERDICT)
    assert len(items) == 2, items
    assert items[0]["num"] == "1" and items[1]["num"] == "2", items
    assert "Before/After" in items[0]["text"], items[0]
    assert "update-branch" in items[1]["text"], items[1]
    # block 1 ends at the heading of block 2, block 2 at Acceptance
    lines = BODY_VERDICT.split("\n")
    assert lines[items[0]["end"]].startswith("## Blocking item 2")
    assert lines[items[1]["end"]].startswith("## Acceptance criteria")


def test_scan_fires_on_fixture():
    """The fixture: the scan sees item 1, covered by t_3be430d2, and only
    it: update-branch is covered by nothing (there is no covering card for
    #166)."""
    slug, tmp, old = make_board(tasks=fx_pair())
    try:
        hits = mod.verdict_dedup_scan(slug)
        assert len(hits) == 1, hits
        h = hits[0]
        assert h["tid"] == "t_446ecc51", h
        assert h["removed"] == [("1", "Before / After section",
                                 "t_3be430d2")], h["removed"]
        assert h["ac_marked"] == 1, h
        assert h["refreshed"] == 0, h
        assert h["new_title"] == TITLE_CLEAN, h["new_title"]
        # item 2 (update-branch) stays in the body
        assert "## Blocking item 2: update-branch" in h["new_body"]
        # the duplicate block is cut out, the cover line is in place, the
        # tail tells the truth: the AC are marked by THIS SAME pass
        assert "## Blocking item 1" not in h["new_body"]
        assert "Item 1 (Before / After section) covers t_3be430d2" \
            in h["new_body"]
        assert mod.VERDICT_NEW_TAIL[0].upper() + mod.VERDICT_NEW_TAIL[1:] \
            + "." in h["new_body"], "the insert tail does not tell the truth about AC"
        assert mod.VERDICT_OLD_TAIL not in h["new_body"], \
            "the old false tail is still there"
        # the Before/After requirement is cleaned out of the acceptance criteria too
        assert '- PR body has "## Before / After" at the top' \
            not in h["new_body"]
        assert "- Item 1 (Before / After section): criterion covers" \
            " t_3be430d2" in h["new_body"]
        # uncovered criteria are untouched
        assert "branch carries the develop/2.x tip (update-branch)" \
            in h["new_body"]
        assert "no production code changes" in h["new_body"]
        # the tail of the body (the AC section) is in place exactly once
        assert h["new_body"].count("## Acceptance criteria") == 1
        # invariant: the class occurs only in "covers t_..." lines, there
        # are no requirements left anywhere
        for ln in h["new_body"].split("\n"):
            if mod.VERDICT_COVER_CLASSES[0][1].search(ln):
                assert "covers t_3be430d2" in ln, ln
    finally:
        restore(mod.run, old, tmp)


def test_gate_edits_card():
    """The gate edits the body and the title, posts a marker comment, gives
    a rank 1 finding."""
    slug, tmp, old = make_board(tasks=fx_pair())
    calls = []
    real = stub_run(calls)
    try:
        found = mod.verdict_dedup_gate(slug)
        assert len(found) == 1, found
        assert found[0][1] == 1, found
        assert "t_446ecc51" in found[0][0]
        edits = hermes_cmds(calls, "edit")
        assert len(edits) == 1, edits
        cmd = edits[0]
        assert cmd[5] == "t_446ecc51", cmd
        body = cmd[cmd.index("--body") + 1]
        assert "Item 1 (Before / After section) covers t_3be430d2" in body
        assert "## Blocking item 1" not in body
        assert "## Blocking item 2: update-branch" in body
        assert "## Acceptance criteria" in body
        # the acceptance criterion for Before/After is marked, not required
        assert "- Item 1 (Before / After section): criterion covers" \
            " t_3be430d2" in body
        assert 'PR body has "## Before / After"' not in body
        assert "--title" in cmd, cmd
        assert cmd[cmd.index("--title") + 1] == TITLE_CLEAN, cmd
        comms = hermes_cmds(calls, "comment")
        assert len(comms) == 1, comms
        assert mod.VERDICT_DEDUP_MARKER in " ".join(comms[0])
        # the covering card is not touched: no edit of t_3be430d2
        assert not any(len(c) > 5 and c[4] == "edit" and c[5] == "t_3be430d2"
                       for c in calls), calls
    finally:
        restore(real, old, tmp)


def test_gate_idempotent_second_pass():
    """After the edit the item is gone: a repeated pass is silent (the scan
    does not fire, no commands)."""
    slug, tmp, old = make_board(tasks=fx_pair())
    calls = []
    real = stub_run(calls)
    try:
        mod.verdict_dedup_gate(slug)
        edit_cmd = [c for c in calls if c[4] == "edit"][0]
        new_body = edit_cmd[edit_cmd.index("--body") + 1]
        con = sqlite3.connect(os.path.join(tmp, slug, "kanban.db"))
        con.execute("update tasks set body = ?, title = ? where id = ?",
                    (new_body, TITLE_CLEAN, "t_446ecc51"))
        con.execute(
            "insert into task_comments (task_id, body, created_at)"
            " values (?,?,?)",
            ("t_446ecc51", mod.VERDICT_DEDUP_MARKER + " marker", time.time()))
        con.commit()
        con.close()
        calls2 = []
        real2 = stub_run(calls2)
        try:
            found = mod.verdict_dedup_gate(slug)
            assert found == [], found
            assert calls2 == [], calls2
        finally:
            mod.run = real2
    finally:
        restore(real, old, tmp)


def test_negative_covering_dead():
    """A cover in done is not live: the scan is silent."""
    slug, tmp, old = make_board(tasks=fx_pair(vd_status="done"))
    try:
        assert mod.verdict_dedup_scan(slug) == []
    finally:
        restore(mod.run, old, tmp)


def test_negative_different_pr():
    """A cover of another PR does not cover: the scan is silent."""
    slug, tmp, old = make_board(tasks=fx_pair(
        vd_title="[visual-delta] PR #167: Before / After section"))
    try:
        assert mod.verdict_dedup_scan(slug) == []
    finally:
        restore(mod.run, old, tmp)


def test_negative_covering_younger():
    """The cover is younger than the verdict: there is nobody to yield to,
    the scan is silent (the reverse order is not cut: parking the
    duplicate is a separate question)."""
    slug, tmp, old = make_board(tasks=fx_pair(vd_created_ago=600,
                                              ver_created_ago=3600))
    try:
        assert mod.verdict_dedup_scan(slug) == []
    finally:
        restore(mod.run, old, tmp)


def test_negative_no_item():
    """A verdict card without a Before/After requirement, neither in items
    nor in the criteria: the cover is live, but there is nothing to
    subtract. The scan watches the AC too, not only the items."""
    body = BODY_VERDICT.replace(
        "## Blocking item 1: PR body needs a Before/After section",
        "## Blocking item 1: PR body needs a WCFY comment").replace(
        '- PR body has "## Before / After" at the top',
        "- PR body has a WCFY comment")
    slug, tmp, old = make_board(tasks=fx_pair(ver_body=body))
    try:
        assert mod.verdict_dedup_scan(slug) == []
    finally:
        restore(mod.run, old, tmp)


ITEM1_BLOCK = (
    "## Blocking item 1: PR body needs a Before/After section\n"
    "No \"## Before / After\" section at the top of the PR body. This is a"
    " UI change. Required:\n"
    "- two screenshots of the same demo stand after a silent stream drop\n"
    "- before (base: UI stays on the dead session)\n"
    "- after (this head: reconnects with a fresh session)\n"
    "- details stay inside <details> blocks\n"
    "Edit the PR body so the section sits at the top; keep the rest of the"
    " body intact.\n\n")

# The insert an earlier version of the gate left: the item is replaced, but
# the acceptance criteria still require Before/After and the insert claims
# "the criterion is removed" (the literal text of VERDICT_OLD_TAIL).
MARKER_OLD = ("Item 1 (Before / After section) covers t_3be430d2: "
              "duplicate removed by the verdict dedup gate, the work happens "
              "in that card; the acceptance criterion for the item is removed from "
              "this card.\n\n")


def test_second_pass_on_old_insert_body():
    """The body after an earlier pass (the item replaced by an insert, the
    AC still require Before/After, the insert claims "criterion removed").
    The gate must fire on the AC alone: mark the criterion line, bring the
    insert tail to the truth; the third pass is silent."""
    body = BODY_VERDICT.replace(ITEM1_BLOCK, MARKER_OLD)
    slug, tmp, old = make_board(tasks=fx_pair(ver_body=body,
                                              ver_title=TITLE_CLEAN))
    calls = []
    real = stub_run(calls)
    try:
        hits = mod.verdict_dedup_scan(slug)
        assert len(hits) == 1, hits
        h = hits[0]
        assert h["removed"] == [], h["removed"]
        assert h["ac_marked"] == 1, h
        assert h["refreshed"] == 1, h
        assert h["new_title"] == TITLE_CLEAN, h["new_title"]
        nb = h["new_body"]
        assert "- Item 1 (Before / After section): criterion covers" \
            " t_3be430d2" in nb
        assert 'PR body has "## Before / After"' not in nb
        assert mod.VERDICT_NEW_TAIL in nb and mod.VERDICT_OLD_TAIL not in nb
        assert "## Blocking item 2: update-branch" in nb
        found = mod.verdict_dedup_gate(slug)
        assert len(found) == 1, found
        edits = hermes_cmds(calls, "edit")
        assert len(edits) == 1, edits
        assert "--title" not in edits[0], edits[0]
        # idempotency: the third pass over the updated body is silent
        edit_cmd = edits[0]
        con = sqlite3.connect(os.path.join(tmp, slug, "kanban.db"))
        con.execute("update tasks set body = ? where id = ?",
                    (edit_cmd[edit_cmd.index("--body") + 1], "t_446ecc51"))
        con.execute(
            "insert into task_comments (task_id, body, created_at)"
            " values (?,?,?)",
            ("t_446ecc51", mod.VERDICT_DEDUP_MARKER + " marker", time.time()))
        con.commit()
        con.close()
        calls2 = []
        real2 = stub_run(calls2)
        try:
            assert mod.verdict_dedup_gate(slug) == []
            assert calls2 == [], calls2
        finally:
            mod.run = real2
    finally:
        restore(real, old, tmp)


def test_running_report_only():
    """The verdict card is running: there is a finding, the body is not edited."""
    slug, tmp, old = make_board(tasks=fx_pair(ver_status="running"))
    calls = []
    real = stub_run(calls)
    try:
        found = mod.verdict_dedup_gate(slug)
        assert len(found) == 1, found
        assert "manually" in found[0][2], found[0][2]
        assert hermes_cmds(calls, "edit") == [], calls
        assert hermes_cmds(calls, "comment") == [], calls
    finally:
        restore(real, old, tmp)


def test_checklist_line_covered():
    """The checklist form of an item ("- [BLOCK] update-branch") is also
    subtracted when a live card with update-branch in its title covers it."""
    body = ("Rework per owner review verdict PR #177, CHANGES REQUESTED:"
            " minor.\n\n"
            "- [BLOCK] update-branch: base moved 4 commits ahead\n"
            "- [BLOCK] add a test for the flicker\n")
    tasks = [
        {"id": "t_8db021bc",
         "title": "[coder] PR #177 rework: update-branch + green CI"
                  " (head 1d8f11f)",
         "body": "update-branch", "status": "ready",
         "created_ago": 2 * 3600},
        {"id": "t_9f000001", "title": "[coder] PR #177 rework per verdict",
         "body": body, "status": "ready", "created_ago": 600},
    ]
    slug, tmp, old = make_board(tasks=tasks)
    try:
        hits = mod.verdict_dedup_scan(slug)
        assert len(hits) == 1, hits
        h = hits[0]
        assert h["tid"] == "t_9f000001", h
        assert ("1", "update-branch / rebase", "t_8db021bc") \
            in h["removed"], h["removed"]
        assert "[BLOCK] update-branch" not in h["new_body"]
        assert "[BLOCK] add a test for the flicker" in h["new_body"]
    finally:
        restore(mod.run, old, tmp)


def main():
    tests = [
        test_items_parsed,
        test_scan_fires_on_fixture,
        test_gate_edits_card,
        test_gate_idempotent_second_pass,
        test_negative_covering_dead,
        test_negative_different_pr,
        test_negative_covering_younger,
        test_negative_no_item,
        test_second_pass_on_old_insert_body,
        test_running_report_only,
        test_checklist_line_covered,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print("ok   %s" % t.__name__)
        except AssertionError as exc:
            failed += 1
            print("FAIL %s: %r" % (t.__name__, exc))
        except Exception as exc:
            failed += 1
            print("ERR  %s: %s: %s" % (t.__name__, type(exc).__name__, exc))
    print("verdict dedup: %d/%d passed" % (len(tests) - failed, len(tests)))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
