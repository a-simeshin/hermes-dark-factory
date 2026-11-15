#!/usr/bin/env python3
"""Rework card size gate and burnout progress comment, board-watchdog.py.

Data shapes covered:
  - ready+coder rework card with 5 BLOCK items, two budget burnouts in the
    history: the gate must intercept it before the first run;
  - rework card with a long body but no BLOCK items: not intercepted (body
    length is context, not amount of work);
  - a 3-BLOCK package: borderline, intercepted (a false positive is
    acceptable: the architect closes the decomposition with a verdict);
  - a burnout with no comment after the run: the watchdog writes done/left
    from the artifact, once per run.

Run: python3 test_board_watchdog_rework_gate.py
Mutant: PYTHONPATH=<scripts dir> BOARD_WATCHDOG_SCRIPT=/tmp/mutant.py \
    python3 test_board_watchdog_rework_gate.py
(the tests must fail on a mutant: a red run proves they check something).
"""
import atexit
import importlib.util
import os
import shutil
import sqlite3
import subprocess
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

BUDGET = "Iteration budget exhausted (200/200) - task could not complete"


def make_board(tasks=(), runs=(), comments=()):
    """Mini board in a temporary directory.

    tasks: dict(id, title, body, status, assignee, priority, key, ws, branch)
    runs: dict(task_id, error, ended_ago, outcome)
    comments: dict(task_id, body, ago)
    """
    tmp = tempfile.mkdtemp(prefix="wdtest4-")
    slug = "testboard"
    os.makedirs(os.path.join(tmp, slug), exist_ok=True)
    db = os.path.join(tmp, slug, "kanban.db")
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
            "insert into tasks (id, title, body, status, assignee, priority,"
            " idempotency_key, workspace_path, branch_name, created_at)"
            " values (?,?,?,?,?,?,?,?,?,?)",
            (t["id"], t.get("title", ""), t.get("body", ""),
             t.get("status", "ready"), t.get("assignee", "coder"),
             t.get("priority", 80), t.get("key"), t.get("ws"),
             t.get("branch"), t.get("created_at", now)))
    for r in runs:
        ended = now - r.get("ended_ago", 3600)
        con.execute(
            "insert into task_runs (task_id, profile, started_at, ended_at,"
            " outcome, error) values (?,?,?,?,?,?)",
            (r["task_id"], r.get("profile", "coder"),
             ended - 5400, ended, r.get("outcome", "gave_up"),
             r.get("error", BUDGET)))
    for c in comments:
        con.execute(
            "insert into task_comments (task_id, body, created_at)"
            " values (?,?,?)",
            (c["task_id"], c.get("body", "progress: done X"),
             now - c.get("ago", 0)))
    con.commit()
    con.close()
    old_boards = mod.BOARDS
    mod.BOARDS = tmp
    return slug, tmp, old_boards


def stub_run(calls):
    """Replaces mod.run: hermes commands are recorded in calls and not
    executed, git commands (the burnout artifact) run for real."""
    real = mod.run

    def fake(cmd, timeout=120):
        if cmd and cmd[0] == "git":
            return real(cmd, timeout=timeout)
        calls.append(cmd)
        return ""
    mod.run = fake
    return real


def make_repo():
    """Mini git repository: base develop/2.x plus a branch with a commit
    mentioning BLOCK 1 and an uncommitted file. Returns the path."""
    d = tempfile.mkdtemp(prefix="wsrepo-")

    def g(*args):
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "-C", d] + list(args), check=True, capture_output=True)
    g("init", "-q", "-b", "develop/2.x")
    open(os.path.join(d, "base.txt"), "w").write("base\n")
    g("add", "-A")
    g("commit", "-q", "-m", "base")
    g("checkout", "-q", "-b", "feature/ws1")
    open(os.path.join(d, "fix.txt"), "w").write("fix\n")
    g("add", "-A")
    g("commit", "-q", "-m", "fix BLOCK 1: redraw the frame")
    open(os.path.join(d, "dirty.txt"), "w").write("dirty\n")
    return d


BODY_5BLOCK = (
    "PR #210 owner review: 5 findings.\n"
    "[BLOCK] 1: wrong frame\n[BLOCK] 2: flicker\n[BLOCK] 3: no scrolling\n"
    "[BLOCK] 4: colors\n[BLOCK] 5: caption\nminor: typo\n")

BODY_2BLOCK = "[BLOCK] 1: small thing\n[BLOCK] 2: another small thing\ndo it and hand in\n"

BODY_LONG = "\n".join("line %d of a body without package markers" % i
                      for i in range(1, 27))  # 26 lines

BODY_25 = "\n".join("line %d" % i for i in range(1, 26))  # exactly 25

BODY_3BLOCK = "[BLOCK] 1: frame\n[BLOCK] 2: flicker\n[BLOCK] 3: caption\n"

BODY_BURN = (
    "Rework PR #210 per the owner review, base develop/2.x.\n"
    "BLOCK 1: redraw the frame\nBLOCK 2: add a test for the flicker\n")

# Blockers are counted by items, not by occurrences of the word. Real body at
# the time of a false interception: 2 blockers as lines BLOCK 1 / BLOCK 2 and
# a recap "2 BLOCK, one card" in the header. Counting the word gave 3.
TITLE_REAL = ("[coder] PR #284 rework per verdict (head c178d32): "
              "What-changes-for-you + update-branch develop/1.x")
BODY_REAL = (
    "Rework per owner review verdict PR #284 (owner-login, head "
    "c178d32b08260e9fc8728062fc78fdf10d7423c7), CHANGES REQUESTED: minor. "
    "2 BLOCK, one card.\n\n"
    "Context: port parity with #245 accepted, owner decision: #284 and #245 "
    "merge together (twin rule), do not close the branch "
    "hermes/retry-1x.\n\n"
    "BLOCK 1: the PR has no \"What changes for you\" comment.\n"
    "BLOCK 2: develop/1.x moved 2 commits ahead (40905e6, 7b533c3): "
    "update-branch, then a green CI on the updated head.\n\n"
    "Acceptance criteria:\n"
    "1. The \"What changes for you\" comment is published on PR #284.\n"
    "2. The PR branch is updated by merging develop/1.x, mergeStateStatus "
    "CLEAN.\n"
    "3. CI is green on the new head.\n"
    "4. A reply in the PR with the new head SHA.\n")

# The owner verdict as a checklist: the source of truth.
BODY_VERDICT = (
    "Rework per owner review verdict PR #290, CHANGES REQUESTED: minor. "
    "3 BLOCK, one card.\n\n"
    "- [BLOCK] no \"What changes for you\" comment\n"
    "- [BLOCK] update-branch: base moved 2 commits ahead\n"
    "- [BLOCK] CI is red on the head\n")


def with_board(tasks=(), runs=(), comments=()):
    slug, tmp, old = make_board(tasks, runs, comments)
    return slug, tmp, old


# ---------- size gate: interception before the first run ----------

def test_gate_intercepts_5block_package():
    """ready+coder, 5 BLOCK. The scan finds it, the gate comments,
    reassigns to the architect and creates a decomposition with the key
    decompose-<task_id>."""
    tasks = [dict(id="t_big", title="Rework PR #210 owner review",
                  body=BODY_5BLOCK, status="ready", assignee="coder")]
    slug, tmp, old = with_board(tasks)
    calls = []
    real = stub_run(calls)
    try:
        hits = mod.rework_oversize_scan(slug)
        assert [h[0] for h in hits] == ["t_big"], "scan: %s" % hits
        found = mod.rework_oversize_gate(slug)
    finally:
        mod.run = real
        mod.BOARDS = old
    keys = [f[0] for f in found]
    assert "%s:rework-oversize:t_big" % slug in keys, "finding: %s" % keys
    comments = [c for c in calls if len(c) > 4 and c[4] == "comment"]
    assert any("t_big" in c and mod.REWORK_GATE_MARKER in " ".join(c)
               for c in comments), "no marker comment: %s" % calls
    reass = [c for c in calls if len(c) > 4 and c[4] == "reassign"]
    assert any("t_big" in c and "architect" in c for c in reass), \
        "no reassign to architect: %s" % calls
    creates = [c for c in calls if len(c) > 4 and c[4] == "create"]
    assert any("--idempotency-key" in c
               and "decompose-t_big" in c[c.index("--idempotency-key") + 1]
               for c in creates), "no decomposition with the key: %s" % calls
    shutil.rmtree(tmp)
    print("OK test_gate_intercepts_5block_package")


def test_gate_small_package_not_touched():
    """2 BLOCK, 4 lines: an ordinary rework, the gate stays silent."""
    tasks = [dict(id="t_ok", title="Rework PR #213 owner review",
                  body=BODY_2BLOCK)]
    slug, tmp, old = with_board(tasks)
    calls = []
    real = stub_run(calls)
    try:
        assert mod.rework_oversize_scan(slug) == []
        assert mod.rework_oversize_gate(slug) == []
    finally:
        mod.run = real
        mod.BOARDS = old
    assert not calls, "the gate acted on a small package: %s" % calls
    shutil.rmtree(tmp)
    print("OK test_gate_small_package_not_touched")


def test_gate_idempotent_by_decompose_key():
    """A live card with the key decompose-t_big: no second pass."""
    tasks = [dict(id="t_big", title="Rework PR #210 owner review",
                  body=BODY_5BLOCK),
             dict(id="t_dec", title="[architect] Decomposition t_big",
                  body="...", status="todo", assignee="architect",
                  key="decompose-t_big")]
    slug, tmp, old = with_board(tasks)
    calls = []
    real = stub_run(calls)
    try:
        found = mod.rework_oversize_gate(slug)
    finally:
        mod.run = real
        mod.BOARDS = old
    assert found == [], "repeat with a live key: %s" % found
    assert not calls, "actions with a live key: %s" % calls
    shutil.rmtree(tmp)
    print("OK test_gate_idempotent_by_decompose_key")


def test_gate_non_coder_untouched():
    """ready+reviewer with 5 BLOCK: not gated (the threshold is for coder only)."""
    tasks = [dict(id="t_rev", title="Review PR #210",
                  body=BODY_5BLOCK, assignee="reviewer")]
    slug, tmp, old = with_board(tasks)
    try:
        assert mod.rework_oversize_scan(slug) == []
    finally:
        mod.BOARDS = old
    shutil.rmtree(tmp)
    print("OK test_gate_non_coder_untouched")


def test_gate_boundary_exactly_3_and_2_blocks():
    """BLOCK boundary: exactly 3 is intercepted, exactly 2 is not. Catches
    the boundary mutant >= -> > (it stayed green on tests without a
    boundary case)."""
    tasks = [dict(id="t_3", title="Rework PR #214 owner review",
                  body=BODY_3BLOCK),
             dict(id="t_2", title="Rework PR #215 owner review",
                  body=BODY_2BLOCK)]
    slug, tmp, old = with_board(tasks)
    calls = []
    real = stub_run(calls)
    try:
        hits = mod.rework_oversize_scan(slug)
        assert [h[0] for h in hits] == ["t_3"], \
            "exactly 3 caught, exactly 2 not: %s" % [h[0] for h in hits]
        found = mod.rework_oversize_gate(slug)
    finally:
        mod.run = real
        mod.BOARDS = old
    assert any(f[0].endswith("rework-oversize:t_3") for f in found), \
        "finding for t_3: %s" % found
    assert not any("t_2" in " ".join(c) for c in calls), \
        "actions on the 2-BLOCK card: %s" % calls
    shutil.rmtree(tmp)
    print("OK test_gate_boundary_exactly_3_and_2_blocks")


def test_gate_body_lines_not_a_criterion():
    """Body length is NOT an interception criterion. Lines are context, not
    amount of work: a card with 26+ lines and fewer than 3 BLOCK goes to a
    run as is. Catches the mutant "lines are back in the criterion"."""
    tasks = [dict(id="t_26", title="Rework PR #211 owner review",
                  body=BODY_LONG),
             dict(id="t_25", title="Rework PR #212 owner review",
                  body=BODY_25),
             dict(id="t_46", title="Rework PR #213 owner review",
                  body=BODY_LONG + "\n" + BODY_2BLOCK)]
    slug, tmp, old = with_board(tasks)
    calls = []
    real = stub_run(calls)
    try:
        hits = mod.rework_oversize_scan(slug)
        assert hits == [], \
            "lines without 3+ BLOCK are not intercepted: %s" % (hits,)
        found = mod.rework_oversize_gate(slug)
        assert found == [], "the gate is silent on long bodies: %s" % (found,)
    finally:
        mod.run = real
        mod.BOARDS = old
    assert calls == [], "no actions expected: %s" % (calls,)
    shutil.rmtree(tmp)
    print("OK test_gate_body_lines_not_a_criterion")


# ---------- blockers are counted by items, not by words ----------

def test_gate_real_body_two_blockers_not_intercepted():
    """Real body: 2 blockers as lines BLOCK 1 / BLOCK 2 plus a recap
    "2 BLOCK, one card" in the header. 2 items, no threshold, the gate is
    silent. Counting occurrences of the word gave 3 and intercepted the
    smallest piece of work of the day."""
    assert len(mod.blocker_items(BODY_REAL)) == 2, \
        "blocker items must be 2: %s" % mod.blocker_items(BODY_REAL)
    tasks = [dict(id="t_real", title=TITLE_REAL, body=BODY_REAL)]
    slug, tmp, old = with_board(tasks)
    calls = []
    real = stub_run(calls)
    try:
        assert mod.rework_oversize_scan(slug) == [], \
            "false interception of 2 blockers"
        assert mod.rework_oversize_gate(slug) == []
    finally:
        mod.run = real
        mod.BOARDS = old
    assert not calls, "actions on a 2-block card: %s" % calls
    shutil.rmtree(tmp)
    print("OK test_gate_real_body_two_blockers_not_intercepted")


def test_gate_verdict_checklist_intercepts_no_double_count():
    """Verdict checklist "- [BLOCK]" x3 plus a recap "3 BLOCK" in the
    header: exactly 3 items (the recap is not counted a second time), the
    gate intercepts and creates a decomposition."""
    assert len(mod.blocker_items(BODY_VERDICT)) == 3, \
        "checklist of 3 items: %s" % mod.blocker_items(BODY_VERDICT)
    tasks = [dict(id="t_verdict", title="Rework PR #290 owner review",
                  body=BODY_VERDICT)]
    slug, tmp, old = with_board(tasks)
    calls = []
    real = stub_run(calls)
    try:
        hits = mod.rework_oversize_scan(slug)
        assert [h[0] for h in hits] == ["t_verdict"], "scan: %s" % hits
        assert hits[0][2] == 3, \
            "3 items, the recap does not count: %s" % (hits,)
        found = mod.rework_oversize_gate(slug)
    finally:
        mod.run = real
        mod.BOARDS = old
    assert any(f[0].endswith("rework-oversize:t_verdict") for f in found), \
        "finding: %s" % found
    assert any(len(c) > 4 and c[4] == "reassign" and "architect" in c
               for c in calls), "no reassign to architect: %s" % calls
    shutil.rmtree(tmp)
    print("OK test_gate_verdict_checklist_intercepts_no_double_count")


def test_gate_prose_retell_forms_never_count():
    """A BLOCK word at the start of a line in a prose recap is not an item:
    "2 BLOCK, one card", "BLOCK 1 and BLOCK 2 are closed" (no colon),
    "BLOCKER:". Real markers are counted: "BLOCK:", "[BLOCK] x"."""
    prose = ("2 BLOCK, one card\n"
             "BLOCK 1 and BLOCK 2 are closed\n"
             "BLOCKER: this is not an item\n"
             "3 BLOCK left in an old recap\n")
    assert mod.blocker_items(prose) == [], \
        "prose is not items: %s" % mod.blocker_items(prose)
    real = ("BLOCK: urgent fix\n"
            "[BLOCK] without a number\n"
            "**[BLOCK]** with bold formatting\n")
    assert len(mod.blocker_items(real)) == 3, \
        "real forms: %s" % mod.blocker_items(real)
    print("OK test_gate_prose_retell_forms_never_count")


# ---------- automatic return on the verdict "fits in one run" ----------

DEC_BODY = ("Parent: t_par. A verdict is mandatory, a machine reads it: "
            "the package really fits into one run -> close with a comment "
            "FITS-IN-ONE-RUN.")


def _dec_board(parent_status="blocked", parent_assignee="architect",
               dec_status="done", dec_body=DEC_BODY, comments=()):
    """Board with a closed decomposition decompose-t_par and a parent t_par."""
    tasks = [
        dict(id="t_par", title="Rework PR #210 owner review",
             body=BODY_2BLOCK, status=parent_status,
             assignee=parent_assignee),
        dict(id="t_dec", title="[architect] Decomposition t_par (size gate)",
             body=dec_body, status=dec_status, assignee="architect",
             key="decompose-t_par"),
    ]
    return with_board(tasks, comments=comments)


def test_auto_return_on_fits_verdict():
    """The decomposition is closed with the verdict "fits in one run", the
    parent is blocked with the architect: the gate comments, unblocks and
    returns it to coder. No owner involved."""
    slug, tmp, old = _dec_board(
        comments=[dict(task_id="t_dec", body="Verdict: the package "
                                              "fits-in-one-run, no children needed")])
    calls = []
    real = stub_run(calls)
    try:
        found = mod.rework_gate_auto_return(slug)
    finally:
        mod.run = real
        mod.BOARDS = old
    assert any(f[0].endswith("rework-fit-return:t_par") for f in found), found
    j = " ".join
    assert any("comment" in c and "t_par" in c
               and mod.REWORK_RETURN_MARKER in j(c) for c in calls), calls
    assert any("unblock" in c and "t_par" in c for c in calls), calls
    assert any("reassign" in c and "t_par" in c and "coder" in c
               for c in calls), calls
    ci = next(i for i, c in enumerate(calls) if "comment" in c)
    ui = next(i for i, c in enumerate(calls) if "unblock" in c)
    ri = next(i for i, c in enumerate(calls) if "reassign" in c)
    assert ci < ui < ri, "order comment->unblock->reassign: %s" % (calls,)
    shutil.rmtree(tmp)
    print("OK test_auto_return_on_fits_verdict")


def test_auto_return_verdict_in_run_summary():
    """The verdict in the summary of the decomposition run also works (the
    architect closes the card with a run summary and writes no comment)."""
    slug, tmp, old = _dec_board(parent_status="ready")
    con = sqlite3.connect(os.path.join(tmp, slug, "kanban.db"))
    now = time.time()
    con.execute(
        "insert into task_runs (task_id, profile, started_at, ended_at,"
        " outcome, summary) values ('t_dec','architect',?,?,'done',"
        "'checked the package: fits-in-one-run, decomposition not needed')",
        (now - 600, now - 60))
    con.commit()
    con.close()
    calls = []
    real = stub_run(calls)
    try:
        found = mod.rework_gate_auto_return(slug)
    finally:
        mod.run = real
        mod.BOARDS = old
    assert any(f[0].endswith("rework-fit-return:t_par") for f in found), found
    assert any("reassign" in c and "coder" in c for c in calls), calls
    assert not any("unblock" in c for c in calls), calls
    shutil.rmtree(tmp)
    print("OK test_auto_return_verdict_in_run_summary")


def test_auto_return_ignores_split_and_instruction():
    """No "fits" verdict: (1) the architect split the package into
    children; (2) the decomposition body contains the verdict word from
    the gate instruction, but there is no comment and no summary. The gate
    is silent in both cases."""
    for label, comments in (("split", [dict(
            task_id="t_dec", body="SPLIT: three children created, "
                                  "the parent waits for them to be delivered")]),
                            ("instruction-in-body", ())):
        slug, tmp, old = _dec_board(comments=comments)
        calls = []
        real = stub_run(calls)
        try:
            found = mod.rework_gate_auto_return(slug)
        finally:
            mod.run = real
            mod.BOARDS = old
        assert found == [], "%s: no findings: %s" % (label, found)
        assert calls == [], "%s: no actions: %s" % (label, calls)
        shutil.rmtree(tmp)
    print("OK test_auto_return_ignores_split_and_instruction")


def test_auto_return_idempotent_and_marker_guard():
    """(1) The parent is already with coder: no repeated actions. (2) The
    parent is still with the architect, but the return marker comment is
    already there (a failure between steps): no new actions."""
    slug, tmp, old = _dec_board(
        parent_assignee="coder",
        comments=[dict(task_id="t_dec", body="the package fits-in-one-run")])
    calls = []
    real = stub_run(calls)
    try:
        found = mod.rework_gate_auto_return(slug)
    finally:
        mod.run = real
        mod.BOARDS = old
    assert found == [] and calls == [], (found, calls)
    shutil.rmtree(tmp)

    slug, tmp, old = _dec_board(
        comments=[dict(task_id="t_dec", body="the package fits-in-one-run"),
                  dict(task_id="t_par", body=mod.REWORK_RETURN_MARKER +
                       " the return already happened, reassign was cut off")])
    calls = []
    real = stub_run(calls)
    try:
        found = mod.rework_gate_auto_return(slug)
    finally:
        mod.run = real
        mod.BOARDS = old
    assert found == [] and calls == [], (found, calls)
    shutil.rmtree(tmp)
    print("OK test_auto_return_idempotent_and_marker_guard")


def test_auto_return_dry_run_touches_nothing():
    """apply=False: there is a finding, the board is untouched (no calls)."""
    slug, tmp, old = _dec_board(
        comments=[dict(task_id="t_dec", body="the package fits-in-one-run")])
    calls = []
    real = stub_run(calls)
    try:
        found = mod.rework_gate_auto_return(slug, apply=False)
    finally:
        mod.run = real
        mod.BOARDS = old
    assert any(f[0].endswith("rework-fit-return:t_par") for f in found), found
    assert "dry-run" in found[0][2], found[0][2]
    assert calls == [], calls
    shutil.rmtree(tmp)
    print("OK test_auto_return_dry_run_touches_nothing")


# ---------- progress comment after a burnout ----------

def test_burn_comment_written_with_artifact():
    """A burnout an hour ago, no comments: the watchdog writes done/left
    from the artifact: branch, git log base..HEAD, status, BLOCK items with
    marks."""
    repo = make_repo()
    tasks = [dict(id="t_burn", title="Rework PR #210 per the owner review",
                  body=BODY_BURN, ws=repo, branch="feature/ws1")]
    runs = [dict(task_id="t_burn", ended_ago=3600)]
    slug, tmp, old = with_board(tasks, runs)
    calls = []
    real = stub_run(calls)
    try:
        pending = mod.burnout_progress_scan(slug)
        assert len(pending) == 1 and pending[0]["task_id"] == "t_burn", \
            "scan: %s" % pending
        found = mod.burnout_progress_comment(slug)
    finally:
        mod.run = real
        mod.BOARDS = old
    comments = [" ".join(c) for c in calls if len(c) > 4 and c[4] == "comment"]
    assert len(comments) == 1, "one comment expected: %s" % calls
    body = comments[0][comments[0].index("Watchdog"):]
    assert mod.BURN_PROGRESS_MARKER in body, "no marker:\n%s" % body
    assert "Branch: feature/ws1" in body, "no branch:\n%s" % body
    assert "git log develop/2.x..HEAD" in body, "no log from the base:\n%s" % body
    assert "fix BLOCK 1" in body, "no commit in the log:\n%s" % body
    assert "[+] BLOCK 1" in body, "BLOCK 1 not marked done:\n%s" % body
    assert "[ ] BLOCK 2" in body, "BLOCK 2 not marked left:\n%s" % body
    assert "?? dirty.txt" in body, "no uncommitted file:\n%s" % body
    assert "Left (from the body): 1 BLOCK" in body, "no summary:\n%s" % body
    assert found and found[0][0].startswith(
        "%s:burn-progress:t_burn:" % slug), "finding: %s" % found
    shutil.rmtree(tmp)
    shutil.rmtree(repo)
    print("OK test_burn_comment_written_with_artifact")


def test_burn_idempotent_when_comment_after_burn_exists():
    """A comment after the end of the burnout already exists (worker, owner
    or watchdog): the watchdog does not write a second one. Idempotency per
    run."""
    tasks = [dict(id="t_burn", title="Rework PR #210", body=BODY_BURN)]
    runs = [dict(task_id="t_burn", ended_ago=3600)]
    comments = [dict(task_id="t_burn", body="out of time: BLOCK 1 committed",
                     ago=1800)]  # half an hour after the end of the run
    slug, tmp, old = with_board(tasks, runs, comments)
    calls = []
    real = stub_run(calls)
    try:
        assert mod.burnout_progress_scan(slug) == []
        assert mod.burnout_progress_comment(slug) == []
    finally:
        mod.run = real
        mod.BOARDS = old
    assert not calls, "comment duplicated: %s" % calls
    shutil.rmtree(tmp)
    print("OK test_burn_idempotent_when_comment_after_burn_exists")


def test_burn_comment_before_burn_does_not_close():
    """A comment BEFORE the burnout (a handoff in the middle of the run)
    does not count as recorded progress: the watchdog writes its own."""
    tasks = [dict(id="t_burn", title="Rework PR #210", body=BODY_BURN)]
    runs = [dict(task_id="t_burn", ended_ago=3600)]
    comments = [dict(task_id="t_burn", body="started, working on BLOCK 1",
                     ago=7200)]  # an hour BEFORE the end of the run
    slug, tmp, old = with_board(tasks, runs, comments)
    calls = []
    real = stub_run(calls)
    try:
        pending = mod.burnout_progress_scan(slug)
        assert len(pending) == 1, \
            "a run without recorded progress must be listed: %s" % pending
    finally:
        mod.run = real
        mod.BOARDS = old
    shutil.rmtree(tmp)
    print("OK test_burn_comment_before_burn_does_not_close")


def test_burn_done_card_skipped():
    """The card was closed after the burnout by a second run: no comment
    needed, the work is delivered."""
    tasks = [dict(id="t_done", title="Rework PR #210", body=BODY_BURN,
                  status="done")]
    runs = [dict(task_id="t_done", ended_ago=3600)]
    slug, tmp, old = with_board(tasks, runs)
    calls = []
    real = stub_run(calls)
    try:
        assert mod.burnout_progress_scan(slug) == []
    finally:
        mod.run = real
        mod.BOARDS = old
    assert not calls, "comment on a closed card: %s" % calls
    shutil.rmtree(tmp)
    print("OK test_burn_done_card_skipped")


def test_burn_blocked_card_gets_comments():
    """blocked (the budget gate blocked it after two burnouts): the watchdog
    still writes progress, once per run. After an unblock a new run without
    done/left would start from scratch; the block comment and the progress
    comment are different markers."""
    tasks = [dict(id="t_blk", title="Rework PR #210", body=BODY_BURN,
                  status="blocked")]
    runs = [dict(task_id="t_blk", ended_ago=3600),
            dict(task_id="t_blk", ended_ago=1800)]
    slug, tmp, old = with_board(tasks, runs)
    calls = []
    real = stub_run(calls)
    try:
        pending = mod.burnout_progress_scan(slug)
        assert len(pending) == 2, "both burnouts await progress: %s" % pending
        found = mod.burnout_progress_comment(slug)
        # the real CLI writes the comment to the database; the stub does
        # not, so insert the marker by hand as it would look after a real run
        con = sqlite3.connect(os.path.join(tmp, slug, "kanban.db"))
        for rid in (1, 2):
            con.execute(
                "insert into task_comments (task_id, body, created_at)"
                " values (?,?,?)",
                ("t_blk", "%s (run #%d)" % (mod.BURN_PROGRESS_MARKER, rid),
                 time.time()))
        con.commit()
        con.close()
        again = mod.burnout_progress_comment(slug)
    finally:
        mod.run = real
        mod.BOARDS = old
    comments = [" ".join(c) for c in calls if len(c) > 4 and c[4] == "comment"]
    assert len(comments) == 2, "one per run: %s" % comments
    assert all(mod.BURN_PROGRESS_MARKER in c for c in comments), \
        "not the progress marker: %s" % comments
    assert "run #1" in comments[0] and "run #2" in comments[1], \
        "run numbers: %s" % comments
    assert again == [], "the second pass wrote duplicates: %s" % again
    assert len(found) == 2, "findings: %s" % found
    shutil.rmtree(tmp)
    print("OK test_burn_blocked_card_gets_comments")


def test_burn_system_block_comment_does_not_close():
    """The budget gate blocked the card with ITS OWN comment after the
    burnout: that is not recorded progress, the watchdog still writes
    "done / left". The markers differ, a block does not close itself."""
    tasks = [dict(id="t_sys", title="Rework PR #216", body=BODY_BURN,
                  status="blocked")]
    runs = [dict(task_id="t_sys", ended_ago=3600)]
    comments = [
        dict(task_id="t_sys",
             body="BLOCKED: Two consecutive budget burnouts (2 x Iteration "
                  "budget). Release condition: decomposition.",
             ago=3500),
        dict(task_id="t_sys",
             body="Watchdog: 2 consecutive runs with an exhausted iteration "
                  "budget. Auto-retry stopped.",
             ago=3400),
    ]
    slug, tmp, old = with_board(tasks, runs, comments)
    calls = []
    real = stub_run(calls)
    try:
        pending = mod.burnout_progress_scan(slug)
        assert len(pending) == 1, \
            "system comments do not close the need: %s" % pending
        found = mod.burnout_progress_comment(slug)
    finally:
        mod.run = real
        mod.BOARDS = old
    posted = [" ".join(c) for c in calls if len(c) > 4 and c[4] == "comment"]
    assert len(posted) == 1 and mod.BURN_PROGRESS_MARKER in posted[0], \
        "progress not written: %s" % calls
    assert found, "no finding"
    shutil.rmtree(tmp)
    print("OK test_burn_system_block_comment_does_not_close")


def test_burn_no_workspace_degrades():
    """The worktree is missing: the comment is still written, the artifact
    is marked unavailable, BLOCK items from the body are listed without marks."""
    tasks = [dict(id="t_burn", title="Rework PR #210", body=BODY_BURN,
                  ws="/nonexistent/ws", branch="feature/gone")]
    runs = [dict(task_id="t_burn", ended_ago=3600)]
    slug, tmp, old = with_board(tasks, runs)
    calls = []
    real = stub_run(calls)
    try:
        found = mod.burnout_progress_comment(slug)
    finally:
        mod.run = real
        mod.BOARDS = old
    comments = [" ".join(c) for c in calls if len(c) > 4 and c[4] == "comment"]
    assert len(comments) == 1, "comment not written: %s" % calls
    body = comments[0][comments[0].index("Watchdog"):]
    assert "Artifact unavailable" in body, "no note:\n%s" % body
    assert "Branch: feature/gone" in body, "branch lost:\n%s" % body
    assert "BLOCK 1" in body and "BLOCK 2" in body, "no BLOCK:\n%s" % body
    assert found, "no finding"
    shutil.rmtree(tmp)
    print("OK test_burn_no_workspace_degrades")


def test_check_wires_rework_gate_mechanisms():
    """check() runs both mechanisms: the gate interception is visible in the
    findings, exceptions do not drop the other checks."""
    tasks = [dict(id="t_big", title="Rework PR #210 owner review",
                  body=BODY_5BLOCK)]
    slug, tmp, old = with_board(tasks)
    calls = []
    real = stub_run(calls)
    try:
        found = mod.check(slug)
    finally:
        mod.run = real
        mod.BOARDS = old
    keys = [f[0] for f in found]
    assert "%s:rework-oversize:t_big" % slug in keys, \
        "the gate is not in the findings: %s" % keys
    assert any(c[4] == "reassign" and "t_big" in c for c in calls), \
        "reassign not called: %s" % calls
    shutil.rmtree(tmp)
    print("OK test_check_wires_rework_gate_mechanisms")


if __name__ == "__main__":
    test_gate_intercepts_5block_package()
    test_gate_boundary_exactly_3_and_2_blocks()
    test_gate_body_lines_not_a_criterion()
    test_gate_real_body_two_blockers_not_intercepted()
    test_gate_verdict_checklist_intercepts_no_double_count()
    test_gate_prose_retell_forms_never_count()
    test_auto_return_on_fits_verdict()
    test_auto_return_verdict_in_run_summary()
    test_auto_return_ignores_split_and_instruction()
    test_auto_return_idempotent_and_marker_guard()
    test_auto_return_dry_run_touches_nothing()
    test_gate_small_package_not_touched()
    test_gate_idempotent_by_decompose_key()
    test_gate_non_coder_untouched()
    test_burn_comment_written_with_artifact()
    test_burn_idempotent_when_comment_after_burn_exists()
    test_burn_comment_before_burn_does_not_close()
    test_burn_done_card_skipped()
    test_burn_blocked_card_gets_comments()
    test_burn_system_block_comment_does_not_close()
    test_burn_no_workspace_degrades()
    test_check_wires_rework_gate_mechanisms()
    print("ALL REWORK GATE TESTS PASSED")
