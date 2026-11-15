#!/usr/bin/env python3
"""Mutant discipline for the owner-question detector of board-watchdog.py.

A plan review gate stood blocked for two weeks with an open question to the
owner (blocked, kind=null, not needs_input) and never reached the "needs
attention" report, while the board kept moving, so the stock checks stayed
silent:
  - :stalled: needs a full stop of the board (queued>0, moving==0);
  - stale_blocks needs finished parents, the gate has none;
  - the core (_rule_stuck_in_blocked) resets its stale timer on ANY comment
    after the block, and the reviewer kept appending corrections itself;
  - the waited exception ("Release condition") would subtract the card from
    the checks.

The layer detector owner_question_pending (board-watchdog.py) closes the hole:
blocked + last comment older than 24 h + no "Release condition:". The scenario
part is covered by test_board_watchdog_ownerq.py. This file adds mutants: every
threshold of the detector must be visible to the test, plus the wiring into
check() and the classification.

  t_gate   the main case: blocked 13 days, the question in the last comment
           13 days old, board running -> finding ownerq:SLUG:t_gate, class
           owner, the text has the number, age, assignee and gist;
  t_recent old block, but the comment is 5 h old (work is going on) -> silent;
  t_beacon blocked 100 h, "Release condition:" -> silent (waited);
  t_unblk  was blocked with a comment 39 h ago, now ready -> silent;
  t_done_q the same, but done -> silent.

Mutants turn the test red: the 24 h threshold (t_recent appears), the waited
exception (t_beacon appears), the status check (t_unblk appears), the owner
classification (falls into the digest), the gist excerpt. The wiring into
check() is checked by an anchor in the source.
"""
import atexit
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import types

_HOME_TMP = tempfile.mkdtemp(prefix="wd-home-")
atexit.register(shutil.rmtree, _HOME_TMP, ignore_errors=True)
# Isolated Hermes home and neutral project settings, set before the script is
# executed (it reads them at import time).
os.environ.update({
    "HERMES_HOME": _HOME_TMP,
    "FACTORY_REPO": "acme/widget",
    "FACTORY_REPO_PATH": "/srv/hermes/widget",
    "FACTORY_BOARD": "main",
    "FACTORY_OWNER_LOGIN": "owner-login",
    "FACTORY_OWNER_CHAT": "telegram:000000000",
    "FACTORY_BASE_BRANCHES": "main",
})

TESTED = "board-watchdog.py"
BW_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       TESTED)
SLUG = "ownerqfix"
NOW = int(time.time())
T_GATE_CMT = NOW - 13 * 86400       # question to the owner, 13 days old
T_GATE_BLK = T_GATE_CMT - 3600
T_RECENT_CMT = NOW - 5 * 3600       # comment 5 h ago: work is going on
T_RECENT_BLK = NOW - 30 * 3600
T_BEACON = NOW - 100 * 3600
T_UNBLK = NOW - 39 * 3600

GATE_COMMENT = ("Verdict published. Waiting for the owner's decision:"
                " (1) duplicates archiving; (2) priorities of the 2.3.0 children.")

MUTANTS = {
    "M_AGE": (
        "        if last_ts is None or now - last_ts[0]"
        " < OWNERQ_AFTER_H * 3600:",
        "        if last_ts is None or now - last_ts[0]"
        " < OWNERQ_AFTER_H * 3600 - 86400:"),
    "M_WAITED": (
        "        if task[\"status\"] != \"blocked\" or task_id in waited:",
        "        if task[\"status\"] != \"blocked\" or False:"),
    "M_STATUS": (
        "        if task[\"status\"] != \"blocked\" or task_id in waited:",
        "        if False or task_id in waited:"),
    "M_CLASSIFY": (
        "    if key.startswith(\"ownerq:\"):",
        "    if key.startswith(\"ownerq:\") and False:"),
    "M_EXCERPT": (
        "               \" \".join(last_body.split())[:160])))",
        "               \"\")))"),
}

WIRE = "found.extend(owner_question_pending(slug, tasks, waited))"


class Recorder(object):
    """run() of the gate: only git remote get-url from a canned answer, the world is not touched."""

    def __init__(self, db):
        self.db = db
        self.calls = []

    def __call__(self, cmd, timeout=120):
        self.calls.append(list(cmd))
        if cmd[0] == "git" and "get-url" in cmd:
            return "https://github.com/acme/widget.git"
        return ""


def make_board(root):
    """Fixture: board SLUG, the main case t_gate and four negatives.
    Schema for graph(): tasks (with body), task_events, task_comments,
    task_links, task_runs."""
    bdir = os.path.join(root, "boards", SLUG)
    os.makedirs(bdir)
    db = os.path.join(bdir, "kanban.db")
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE tasks (
        id TEXT PRIMARY KEY, title TEXT, assignee TEXT,
        status TEXT NOT NULL, priority INTEGER DEFAULT 0,
        claim_lock TEXT, created_at INTEGER NOT NULL,
        body TEXT DEFAULT '')""")
    con.execute("""CREATE TABLE task_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
        run_id INTEGER, kind TEXT NOT NULL, payload TEXT,
        created_at INTEGER NOT NULL)""")
    con.execute("""CREATE TABLE task_comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
        author TEXT NOT NULL, body TEXT NOT NULL,
        created_at INTEGER NOT NULL)""")
    con.execute("""CREATE TABLE task_links (
        parent_id TEXT NOT NULL, child_id TEXT NOT NULL)""")
    con.execute("""CREATE TABLE task_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
        outcome TEXT, started_at INTEGER, ended_at INTEGER)""")

    def card(tid, title, status, assignee, at):
        con.execute("INSERT INTO tasks (id, title, assignee, status,"
                    " priority, claim_lock, created_at, body) VALUES"
                    " (?, ?, ?, ?, 80, NULL, ?, '')",
                    (tid, title, assignee, status, at))

    def ev(tid, kind, at, payload="{}"):
        con.execute("INSERT INTO task_events (task_id, run_id, kind,"
                    " payload, created_at) VALUES (?, NULL, ?, ?, ?)",
                    (tid, kind, payload, at))

    def blocked(tid, at, reason):
        ev(tid, "blocked", at, json.dumps({"reason": reason,
                                           "kind": None}))

    def cmt(tid, author, body, at):
        con.execute("INSERT INTO task_comments (task_id, author, body,"
                    " created_at) VALUES (?, ?, ?, ?)",
                    (tid, author, body, at))

    # t_gate: blocked 13 days, the question is in the last comment, no
    # parents, and the board is moving meanwhile (t_busy running).
    card("t_gate", "Plan review #236 (gate)", "blocked", "reviewer",
         T_GATE_BLK - 3600)
    blocked("t_gate", T_GATE_BLK, GATE_COMMENT)
    cmt("t_gate", "reviewer", GATE_COMMENT, T_GATE_CMT)
    card("t_busy", "Tool Health 2.4.0", "running", "coder", NOW - 7200)
    con.execute("INSERT INTO task_runs (task_id, outcome, started_at,"
                " ended_at) VALUES ('t_busy', NULL, ?, NULL)", (NOW - 300,))

    # t_recent: old block, but the comment is 5 h old: work is going on.
    card("t_recent", "fresh comment on old block", "blocked", "coder",
         T_RECENT_BLK - 60)
    blocked("t_recent", T_RECENT_BLK, "preparing the question")
    cmt("t_recent", "coder", "working on it, preparing the question", T_RECENT_CMT)

    # t_beacon: a sanctioned wait for an external event.
    card("t_beacon", "beacon", "blocked", "tester", T_BEACON - 60)
    blocked("t_beacon", T_BEACON,
            "Alert beacon of the gate. Release condition: manually when"
            " the season closes.")
    cmt("t_beacon", "default",
        "Release condition: manually when the season closes.", T_BEACON + 60)

    # t_unblk: there was a question, the block is lifted, the card is back in work.
    card("t_unblk", "unblocked after answer", "ready", "coder",
         T_UNBLK - 3600)
    blocked("t_unblk", T_UNBLK, "question to the owner")
    cmt("t_unblk", "coder", "answer received, working", T_UNBLK + 600)
    ev("t_unblk", "unblocked", NOW - 3600)

    # t_done_q: there was a question, the card is closed.
    card("t_done_q", "done with old question", "done", "coder",
         T_UNBLK - 3600)
    blocked("t_done_q", T_UNBLK, "question to the owner")
    cmt("t_done_q", "reviewer", GATE_COMMENT, T_UNBLK + 600)
    ev("t_done_q", "completed", NOW - 1800)

    con.commit()
    con.close()
    workdir = os.path.join(root, "repo")
    os.makedirs(workdir, exist_ok=True)
    with open(os.path.join(bdir, "board.json"), "w") as fh:
        json.dump({"default_workdir": workdir}, fh)
    return db


def load_module(root, src=None):
    mod = types.ModuleType("bw_ownerq_mutants")
    mod.__file__ = BW_PATH
    code = src if src is not None else open(BW_PATH,
                                            encoding="utf-8").read()
    exec(compile(code, BW_PATH, "exec"), mod.__dict__)
    mod.BOARDS = os.path.join(root, "boards")
    mod.PROFILES_ROOT = os.path.join(root, "profiles")
    return mod


def full_run(mod, rec):
    problems = []
    tasks, kids, blockers, last_end, waited, recent = mod.graph(SLUG)
    findings = mod.owner_question_pending(SLUG, tasks, waited)
    if [f[0] for f in findings] != ["ownerq:%s:t_gate" % SLUG]:
        problems.append("findings %s, expected [ownerq:%s:t_gate]"
                        % ([f[0] for f in findings], SLUG))
        return problems
    if findings[0][1] != 1:
        problems.append("finding rank %s, expected 1" % findings[0][1])
    text = findings[0][2]
    for needle, what in (("t_gate", "card number"),
                         ("Question to the owner", "question header"),
                         ("reviewer", "assignee"),
                         ("duplicates archiving", "gist of the question")):
        if needle not in text:
            problems.append("the report has no %s (%s)" % (what, needle))
    age_h = int((NOW - T_GATE_CMT) / 3600)
    if ("%d h" % age_h) not in text:
        problems.append("the report has no age of the question (%d h)" % age_h)
    if mod.classify_finding("ownerq:%s:t_gate" % SLUG) != "owner":
        problems.append("classification of ownerq is not owner")
    if mod.classify_finding("%s:ownerq:err" % SLUG) != "digest":
        problems.append("the gate error key is not digest: %s"
                        % mod.classify_finding("%s:ownerq:err" % SLUG))
    return problems


def main():
    src = open(BW_PATH, encoding="utf-8").read()
    for name, (old, _new) in MUTANTS.items():
        if src.count(old) != 1:
            print("FAIL: the anchor of mutant %s occurs %d times"
                  % (name, src.count(old)))
            return 1
    if src.count(WIRE) != 1:
        print("FAIL: the gate is not wired into check(): the anchor '%s' occurs"
              " %d times" % (WIRE, src.count(WIRE)))
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        db = make_board(tmp)
        rec = Recorder(db)
        mod = load_module(tmp)
        mod.run = rec
        problems = full_run(mod, rec)
        if problems:
            print("FAIL: clean run:")
            for p in problems:
                print("  - %s" % p)
            return 1
        print("OK: t_gate (question 13 days old, kind=null, board moving) is in"
              " the owner report with number, age, assignee and gist;"
              " t_recent/t_beacon/t_unblk/t_done_q are silent")

    failed = 0
    for name, (old, new) in MUTANTS.items():
        if src.count(new) or src.count(old) != 1:
            print("SKIP: mutant %s changes the anchor" % name)
            failed += 1
            continue
        with tempfile.TemporaryDirectory() as tmp:
            db = make_board(tmp)
            rec = Recorder(db)
            mod = load_module(tmp, src.replace(old, new, 1))
            mod.run = rec
            try:
                problems = full_run(mod, rec)
            except Exception as exc:
                problems = ["crashed: %s: %s" % (type(exc).__name__, exc)]
            if problems:
                print("OK  %s: mutant killed (%s)"
                      % (name, problems[0][:96]))
            else:
                print("FAIL %s: mutant survived, the test is blind to this branch"
                      % name)
                failed += 1
    if failed:
        print("RESULT: FAIL, problems %d" % failed)
        return 1
    print("RESULT: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
