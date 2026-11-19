#!/usr/bin/env python3
"""The issue poll window counts from the moment an issue entered the active
milestone, and the idle-factory lint sees issues without cards.

Failure this guards: after a release the next milestone became active, but
its issues were created months earlier. The 5-minute issue poll filtered
them by a 48 h window on updatedAt (a milestone change does not touch the
issues themselves), the no-executor finding (24 h) had not matured yet, and
idle_factory_lint stayed silent because it counted only cards as backlog,
and there were no cards at all.

Checks:

  A. The poll window counts from the moment the issue ENTERED the active
     milestone, not only from updatedAt. Entering = a milestoned event of
     the issue (timeline) OR the activation of the milestone itself (closed_at
     of the last closed milestone: "the milestone became active after a
     release").
    A1 release 26 h ago, old issues -> cards are created;
    A2 release 30 days ago, old issue, no transfer -> silence (old issues
       are left to the no-executor finding); a transfer into the active
       milestone 2 h ago -> card; a transfer into ANOTHER milestone -> silence;
       a fresh updatedAt -> card (the base behaviour of the poll);
    a mirror of a LIVE card is filtered, a mirror of a closed card is a
    regular candidate.
  B. idle_factory_lint: the admissible backlog includes open issues of the
     active milestone without a live card (active_milestone_orphans). An
     hour passed -> a finding naming issue #N (class owner); the first tick
     is silent; no orphans -> silence (old behaviour).
  C. active_milestone_orphans applies the same filters as the poll (live
     card, open PR, wait-for-triage, bot mirror of a live card, another
     milestone).

Mutants (in-process copies of the script): the window back to bare
updatedAt, release activation ignored, milestoned transfer ignored, the poll
and the helper suppressing dead mirrors too, lint orphans switched off, the
helper without the milestone filter, and all of them together (the code
before the fix) must each turn the battery red.

Run: python3 test_board_watchdog_active_milestone_window.py
"""
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time

# Settings and Hermes home are fixed before the script is loaded.
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="wd-home-")
os.environ.update({
    "FACTORY_REPO": "acme/widget",
    "FACTORY_REPO_PATH": "/srv/hermes/widget",
    "FACTORY_BOARD": "main",
    "FACTORY_OWNER_LOGIN": "owner-login",
    "FACTORY_OWNER_CHAT": "telegram:000000000",
    "FACTORY_BASE_BRANCHES": "main",
})

HERE = os.path.dirname(os.path.abspath(__file__))
BW_PATH = os.environ.get("BOARD_WATCHDOG_SCRIPT",
                         os.path.join(HERE, "board-watchdog.py"))

NOW = time.time()
FAILS = []


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def make_board(tasks=()):
    """Mini board in a temp dir. Returns (slug, boards_dir, db)."""
    tmp = tempfile.mkdtemp(prefix="wdtest-msw-")
    slug = "testboard"
    os.makedirs(os.path.join(tmp, slug), exist_ok=True)
    db = os.path.join(tmp, slug, "kanban.db")
    con = sqlite3.connect(db)
    con.executescript("""
        create table tasks (id text primary key, title text, body text default '',
            assignee text default 'ops', status text, priority int default 0,
            created_by text default 'agent', created_at int,
            started_at int, completed_at int, idempotency_key text,
            block_kind text default null, current_run_id int);
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
        con.execute(
            "insert into tasks (id, title, status, assignee, priority,"
            " created_by, created_at, idempotency_key, body)"
            " values (?,?,?,?,?,?,?,?,?)",
            (t["id"], t.get("title", "t " + t["id"]), t.get("status", "todo"),
             t.get("assignee", "coder"), t.get("priority", 42),
             t.get("created_by", "agent"), int(NOW - t.get("age", 3600)),
             t.get("key"), t.get("body", "body " + t["id"])))
    con.commit()
    con.close()
    return slug, tmp, db


OPEN_MILESTONES = json.dumps([
    {"title": "2.4.0", "due_on": "2026-12-20T00:00:00Z"},
    {"title": "2.5.0", "due_on": "2027-01-31T00:00:00Z"},
])

# Active milestone 2.4.0 (nearest due_on), band 80.
ISSUES = json.dumps([
    # A1/A2 candidate: release activation, old updatedAt
    {"number": 701, "milestone": {"title": "2.4.0"},
     "labels": [{"name": "hermes"}], "body": "Outbound client observability",
     "updatedAt": _iso(NOW - 3 * 86400)},
    # transfer candidates
    {"number": 703, "milestone": {"title": "2.4.0"},
     "labels": [{"name": "hermes"}], "body": "Tasks tab slivers",
     "updatedAt": _iso(NOW - 3 * 86400)},
    {"number": 704, "milestone": {"title": "2.4.0"},
     "labels": [{"name": "hermes"}], "body": "Milestoned into other",
     "updatedAt": _iso(NOW - 3 * 86400)},
    # fresh updatedAt: the base behaviour of the poll
    {"number": 705, "milestone": {"title": "2.4.0"},
     "labels": [{"name": "hermes"}], "body": "Fresh crash",
     "updatedAt": _iso(NOW - 10 * 60)},
    # mirror of the LIVE card t_604: filtered even with a fresh updatedAt
    {"number": 706, "milestone": {"title": "2.4.0"},
     "labels": [{"name": "hermes"}],
     "body": "Mirrored from kanban card `t_604`",
     "updatedAt": _iso(NOW - 10 * 60)},
    # mirror of a CLOSED card (t_dead99 is not on the board): a regular
    # candidate
    {"number": 707, "milestone": {"title": "2.4.0"},
     "labels": [{"name": "hermes"}],
     "body": "Mirrored from kanban card `t_dead99`",
     "updatedAt": _iso(NOW - 3 * 86400)},
])
TIMELINES = {
    "703": [{"event": "milestoned", "created_at": _iso(NOW - 2 * 3600),
             "milestone": {"title": "2.4.0"}}],
    "704": [{"event": "milestoned", "created_at": _iso(NOW - 2 * 3600),
             "milestone": {"title": "2.3.0"}}],
}
VIEWS = {
    "701": {"number": 701, "title": "Outbound client observability",
            "body": "x", "author": {"login": "owner-login"}},
    "703": {"number": 703, "title": "Tasks tab slivers",
            "body": "x", "author": {"login": "owner-login"}},
    "704": {"number": 704, "title": "Milestoned into other",
            "body": "x", "author": {"login": "owner-login"}},
    "705": {"number": 705, "title": "Fresh crash",
            "body": "x", "author": {"login": "owner-login"}},
    "707": {"number": 707, "title": "Dead mirror work",
            "body": "x", "author": {"login": "owner-login"}},
}

# Fixtures of the active_milestone_orphans helper
ISSUES_C = json.dumps([
    {"number": 602, "milestone": {"title": "2.4.0"},
     "labels": [], "body": "Mirrored from kanban card `t_604`",
     "updatedAt": _iso(NOW - 60)},
    {"number": 603, "milestone": {"title": "2.4.0"},
     "labels": [{"name": "wait-for-triage"}], "body": "x",
     "updatedAt": _iso(NOW - 60)},
    {"number": 604, "milestone": {"title": "2.4.0"},
     "labels": [], "body": "x", "updatedAt": _iso(NOW - 60)},
    {"number": 605, "milestone": {"title": "2.5.0"},
     "labels": [], "body": "x", "updatedAt": _iso(NOW - 60)},
    {"number": 608, "milestone": {"title": "2.4.0"},
     "labels": [], "body": "x", "updatedAt": _iso(NOW - 60)},
    {"number": 611, "milestone": {"title": "2.4.0"},
     "labels": [], "body": "Mirrored from kanban card `t_dead99`",
     "updatedAt": _iso(NOW - 3600)},
    {"number": 701, "milestone": {"title": "2.4.0"},
     "labels": [], "body": "x", "updatedAt": _iso(NOW - 3600)},
    {"number": 710, "milestone": {"title": "2.4.0"},
     "labels": [], "body": "x", "updatedAt": _iso(NOW - 3600)},
])
PRS_C = json.dumps([
    {"number": 92, "body": "Closes #608", "milestone": None,
     "latestReviews": []},
])

LIVE_604 = {"id": "t_604", "status": "ready", "priority": 80,
            "title": "[coder] issue #604", "body": "work on #604",
            "assignee": "coder", "created_by": "user"}


def load_bw(src=None, tag="msw"):
    """The board-watchdog module from the real file or from a mutated src."""
    sbx = tempfile.mkdtemp(prefix="bw-sbx-msw-")
    path = BW_PATH if src is None else os.path.join(sbx, "wd-mut.py")
    if src is not None:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(src)
        for nb in ("milestone_bands.py", "board_native_guard.py",
                   "factory_config.py"):
            shutil.copy(os.path.join(HERE, nb), os.path.join(sbx, nb))
    sp = importlib.util.spec_from_file_location("bw_mod_%s" % tag, path)
    m = importlib.util.module_from_spec(sp)
    sys.modules["bw_mod_%s" % tag] = m
    sp.loader.exec_module(m)
    m.STATE = os.path.join(sbx, "state.json")
    if hasattr(m, "milestone_bands"):
        m.milestone_bands.CACHE_PATH = os.path.join(sbx, "ms.json")
        m.milestone_bands._MEMO = {}
    return m, sbx


def poll_scenario(mod, closed_iso, created_ref):
    """One issue_executor_poll run against a factory of gh answers.

    closed_iso: closed milestones (closed_at matters).
    created_ref: dict that receives the created cards {issue: id}.
    Returns the plan.
    """
    slug, boards, db = make_board(tasks=[LIVE_604])
    old_boards = mod.BOARDS
    mod.BOARDS = boards

    def run_x(cmd, timeout=120):
        if cmd[:2] == ["gh", "api"]:
            url = cmd[2] if len(cmd) > 2 else ""
            if "state=closed" in url:
                return closed_iso
            if "/timeline" in url:
                n = url.split("/issues/")[-1].split("/")[0]
                return json.dumps(TIMELINES.get(n, []))
            return OPEN_MILESTONES
        if cmd[:2] == ["gh", "issue"] and "list" in cmd:
            return ISSUES
        if cmd[:2] == ["gh", "issue"] and "view" in cmd:
            return json.dumps(VIEWS.get(cmd[3], {}))
        if cmd[:2] == ["gh", "pr"] and "list" in cmd:
            return "[]"
        if len(cmd) > 4 and cmd[1] == "kanban" and cmd[4] == "create":
            body_file = cmd[cmd.index("--body-file") + 1]
            body = open(body_file, encoding="utf-8").read()
            key = cmd[cmd.index("--idempotency-key") + 1]
            tid = "t_new_%s" % key
            n_key = int(key.replace("owner-issue-", ""))
            con = sqlite3.connect(db, timeout=10)
            con.execute(
                "insert into tasks (id, title, body, status, priority,"
                " assignee, created_by, created_at, idempotency_key)"
                " values (?,?,?,'ready',?,'coder','watchdog',?,?)",
                (tid, cmd[5], body,
                 int(cmd[cmd.index("--priority") + 1]), int(time.time()),
                 key))
            con.commit()
            con.close()
            created_ref[n_key] = tid
            return json.dumps({"id": tid})
        return "ok"

    real_run, real_repo = mod.run, mod.board_repo
    mod.run = run_x
    mod.board_repo = lambda s: "acme/demo"
    try:
        plan = mod.issue_executor_poll()
    finally:
        mod.run = real_run
        mod.board_repo = real_repo
        mod.BOARDS = old_boards
        shutil.rmtree(boards, ignore_errors=True)
    return plan


def battery(mod):
    problems = []

    def want(name, cond, detail=""):
        print("%s %s%s" % ("ok  " if cond else "FAIL", name,
                           (" :: %s" % (detail,)) if detail and not cond
                           else ""))
        if not cond:
            problems.append("%s :: %s" % (name, detail))

    # --- A: the poll window from the moment of entering ----------------
    closed_fresh = json.dumps(
        [{"title": "2.3.0", "closed_at": _iso(NOW - 26 * 3600)}])
    closed_stale = json.dumps(
        [{"title": "2.2.0", "closed_at": _iso(NOW - 30 * 86400)}])

    made = {}
    plan = poll_scenario(mod, closed_fresh, made)
    got = sorted(n for _, n, _ in plan)
    # With a fresh activation, entering = activation for ALL issues of the
    # milestone (including those milestoned into another one); the
    # isolation of a foreign transfer is visible in A2.
    want("A1 release 26 h ago: #701/#703/#704/#707 (old) + #705"
         " (fresh) -> cards; #706 (live mirror) skipped",
         got == [701, 703, 704, 705, 707], str(got))
    want("A1b cards created for every candidate except the mirror",
         set(made) == {701, 703, 704, 705, 707}, str(sorted(made)))

    made2 = {}
    plan2 = poll_scenario(mod, closed_stale, made2)
    got2 = sorted(n for _, n, _ in plan2)
    want("A2 release 30 days ago: only #703 (transfer) and #705 (updatedAt)",
         got2 == [703, 705], str(got2))

    # --- B: idle_factory_lint with orphans -----------------------------
    real_orphans = mod.active_milestone_orphans
    mod.active_milestone_orphans = lambda slug, repo=None: [701, 120]
    old_boards = mod.BOARDS
    try:
        slug, boards, db = make_board(tasks=[
            {"id": "t_wait", "status": "todo", "assignee": "coder",
             "priority": 40, "title": "waits for the parent"},
            {"id": "t_parent", "status": "scheduled", "assignee": "coder",
             "priority": 40, "title": "parent"},
        ])
        mod.BOARDS = boards
        tasks = {"t_wait": {"status": "todo", "title": "waits for the parent",
                            "assignee": "coder", "body": ""},
                 "t_parent": {"status": "scheduled", "title": "parent",
                              "assignee": "coder", "body": ""}}
        blockers = {"t_wait": ["t_parent"]}
        st = {"idlefactory:" + slug: {"since": NOW - 2 * 3600}}
        found = mod.idle_factory_lint(slug, st, tasks, blockers)
        want("B1 an hour passed, orphans exist -> finding for the owner",
             len(found) == 1 and "issue #701" in found[0][2]
             and "issue #120" in found[0][2],
             (found[0][2][:120] if found else "silence"))
        if found:
            want("B1b class owner", mod.classify_finding(found[0][0])
                 == "owner", found[0][0])
        st2 = {}
        found2 = mod.idle_factory_lint(slug, st2, tasks, blockers)
        want("B2 the first tick is silent (timer only)",
             found2 == [] and ("idlefactory:" + slug) in st2, str(found2))

        mod.active_milestone_orphans = lambda slug, repo=None: []
        st3 = {"idlefactory:" + slug: {"since": NOW - 3 * 3600}}
        found3 = mod.idle_factory_lint(slug, st3, tasks, blockers)
        want("B3 no orphans -> silence (old behaviour)",
             found3 == [], str(found3))
    finally:
        mod.active_milestone_orphans = real_orphans
        mod.BOARDS = old_boards
        shutil.rmtree(boards, ignore_errors=True)

    # --- C: the active_milestone_orphans helper ------------------------
    slug_c, boards_c, db_c = make_board(tasks=[LIVE_604])
    mod.BOARDS = boards_c
    real_run, real_repo = mod.run, mod.board_repo

    def run_c(cmd, timeout=120):
        if cmd[:2] == ["gh", "api"]:
            return OPEN_MILESTONES
        if cmd[:2] == ["gh", "issue"] and "list" in cmd:
            return ISSUES_C
        if cmd[:2] == ["gh", "pr"] and "list" in cmd:
            return PRS_C
        return "ok"

    mod.run = run_c
    mod.board_repo = lambda s: "acme/demo"
    try:
        orphans = mod.active_milestone_orphans(slug_c)
    finally:
        mod.run = real_run
        mod.board_repo = real_repo
        mod.BOARDS = old_boards
        shutil.rmtree(boards_c, ignore_errors=True)
    want("C orphans = [#611 (dead mirror), #701, #710] (the rest"
         " filtered)", orphans == [611, 701, 710], str(orphans))
    return problems


MUTANTS = {
    "window-back-to-updatedAt": (
        "if not entered or now - entered > ISSUE_POLL_WINDOW_S:\n"
        "                    continue",
        "if not upd or now - upd > ISSUE_POLL_WINDOW_S:\n"
        "                    continue"),
    "release-activation-ignored": (
        "active_since = closed_milestone_last(repo)",
        "active_since = None"),
    "milestoned-transfer-ignored": (
        'if e.get("event") != "milestoned":',
        "if True:"),
    "poll-suppresses-dead-mirror": (
        "src = mirror_source(body or \"\")\n"
        "            if src and src in live_ids:",
        "src = mirror_source(body or \"\")\n"
        "            if src:"),
    "helper-suppresses-dead-mirror": (
        "live_src = mirror_source(body or \"\")\n"
        "        if live_src and live_src in live_ids:",
        "live_src = mirror_source(body or \"\")\n"
        "        if live_src:"),
    "lint-orphans-off": (
        "orphans = active_milestone_orphans(slug)",
        "orphans = []"),
    "helper-without-milestone-filter": (
        "if milestone != active or n in live_refs or n in pr_of_issue:",
        "if n in live_refs or n in pr_of_issue:"),
}


def main():
    bw_src = open(BW_PATH, encoding="utf-8").read()
    for name, (old, _new) in MUTANTS.items():
        if bw_src.count(old) != 1:
            print("FAIL: anchor of mutant %s occurs %d times"
                  % (name, bw_src.count(old)))
            return 1

    mod, _ = load_bw()
    problems = battery(mod)
    if problems:
        print("\nRED: clean run, %d problem(s)" % len(problems))
        return 1
    print("OK: clean run (A window, B lint, C helper)")

    failed = 0
    for name, (old, new) in MUTANTS.items():
        mut = load_bw(bw_src.replace(old, new, 1), tag=name)[0]
        try:
            probs = battery(mut)
        except Exception as exc:
            probs = ["crashed: %s: %s" % (type(exc).__name__, exc)]
        if probs:
            print("OK   %s: mutant killed (%s)" % (name, probs[0][:80]))
        else:
            print("FAIL %s: mutant survived" % name)
            failed += 1

    # Red before the fix: the code before the fix is rebuilt in-process by
    # applying all mutant pairs at once.
    refix_bw = bw_src
    for _n, (old, new) in MUTANTS.items():
        refix_bw = refix_bw.replace(old, new, 1)
    mut = load_bw(refix_bw, tag="refix")[0]
    try:
        probs = battery(mut)
    except Exception as exc:
        probs = ["crashed: %s: %s" % (type(exc).__name__, exc)]
    if probs:
        print("OK   rollback of the fix (code before the fix) turns red: %s"
              % probs[0][:80])
    else:
        print("FAIL rollback of the fix is green: the test is blind")
        failed += 1

    if failed:
        print("\nTOTAL: FAIL, %d problem(s)" % failed)
        return 1
    print("\nTOTAL: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
