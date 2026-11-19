#!/usr/bin/env python3
"""A ready p>=80 card held silently under respawn_guarded gets a finding with its cause.

Why: a p80 rework card sat in ready for hours while the dispatcher wrote
respawn_guarded(active_pr) every two minutes. A previous run had left its
OWN handoff comment with an issuecomment URL of the PR in this card; the
core reads a URL in a fresh comment as "this worker already opened a PR"
and holds the card for 24 h. The foreign-link branch of the active_pr gate
deliberately does not touch the case author == assignee (fail closed), and
the generic branch needs 12 ticks plus a human decision, so both stayed
silent. Meanwhile lint I3 kept fixing the symptom: it reclaimed a p40 card
from the slot, the dispatcher respawned it seconds later, and runs burned.

Rule: a ready card with p>=80 whose current event streak (after the last
spawned/assigned) consists of respawn_guarded and lasts longer than 30
minutes is a finding WITH the cause taken from the events (counts by
reason, who gave the PR link within 24 h, how many own runs). No actions:
lifting the guard for an own comment stays a human decision. Prevention:
the handoff comment without a PR URL (number and head sha) is pinned in
the _rework_arm_card template, which this test also checks.

guarded_ready_lint must:
  t1  ready p80, zero own runs, own handoff comment with a URL, 8 active_pr
      ticks, a 40 min streak -> finding with "active_pr x8",
      "assignee-self", "own runs 0";
  t2  the same at p40 -> silent (layer p>=80);
  t3  streak of only 5 min -> silent (30 min threshold);
  t4  a spawned event after the ticks (streak broken) -> silent;
  t5  own completed run plus own PR comment (a legitimate guard against a
      duplicate PR) -> a finding too, classified "own runs 1": reading the
      cause, not an automatic action.

The rework card template must carry no github URL and must carry the rule
"until the first run the PR is mentioned in comments only by number".

In-process mutants (a modified copy of the script text, executed in this
process) must turn the test red: the priority threshold, the 30 min
threshold, the streak break, the cause classification, the template.

Run: python3 test_board_watchdog_guarded_ready_lint.py
"""
import json
import os
import sqlite3
import sys
import tempfile
import time
import types

TESTED = "board-watchdog.py"
HERE = os.path.dirname(os.path.abspath(__file__))
BW_PATH = os.environ.get("BOARD_WATCHDOG_SCRIPT") or os.path.join(HERE, TESTED)
os.environ.update({
    "HERMES_HOME": tempfile.mkdtemp(prefix="wd-home-"),
    "FACTORY_REPO": "acme/widget",
    "FACTORY_REPO_PATH": "/srv/hermes/widget",
    "FACTORY_BOARD": "main",
    "FACTORY_OWNER_LOGIN": "owner-login",
    "FACTORY_OWNER_CHAT": "telegram:000000000",
    "FACTORY_BASE_BRANCHES": "main",
})
sys.path.insert(0, HERE)
SLUG = "guardedboard"
NOW = int(time.time())
T0 = NOW - 3 * 3600          # handoff comment 3 h ago
PR_URL = ("https://github.com/acme/widget/pull/357"
          "#issuecomment-6918467506")

MUTANTS = {
    "M_PRIO": (
        "GUARDED_READY_PRIO = 80",
        "GUARDED_READY_PRIO = 0"),
    "M_MIN": (
        "            if now - first_at < GUARDED_READY_MIN_S:\n"
        "                continue",
        "            if False:\n"
        "                continue"),
    "M_STREAK": (
        "            for kind, payload, at in events:\n"
        "                if kind != \"respawn_guarded\":\n"
        "                    break",
        "            for kind, payload, at in events:\n"
        "                if kind != \"respawn_guarded\":\n"
        "                    continue"),
    "M_CAUSE": (
        "                if at and ACTIVE_PR_PR_URL_RE.search(body or \"\"):",
        "                if False and ACTIVE_PR_PR_URL_RE.search(body or \"\"):"),
    "M_TMPL": (
        "Comment hygiene: until the first run of this card, the PR is ",
        "Comment hygiene: the PR is "),
}


def make_board(root):
    bdir = os.path.join(root, "boards", SLUG)
    os.makedirs(bdir)
    db = os.path.join(bdir, "kanban.db")
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE tasks (
        id TEXT PRIMARY KEY, title TEXT, assignee TEXT,
        status TEXT NOT NULL, priority INTEGER DEFAULT 0,
        claim_lock TEXT, created_at INTEGER NOT NULL)""")
    con.execute("""CREATE TABLE task_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
        run_id INTEGER, kind TEXT NOT NULL, payload TEXT,
        created_at INTEGER NOT NULL)""")
    con.execute("""CREATE TABLE task_comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
        author TEXT NOT NULL, body TEXT NOT NULL,
        created_at INTEGER NOT NULL)""")
    con.execute("""CREATE TABLE task_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
        outcome TEXT)""")

    def ev(tid, kind, at, payload="{}"):
        con.execute("INSERT INTO task_events (task_id, run_id, kind, payload,"
                    " created_at) VALUES (?, NULL, ?, ?, ?)",
                    (tid, kind, payload, at))

    def cmt(tid, author, body, at):
        con.execute("INSERT INTO task_comments (task_id, author, body,"
                    " created_at) VALUES (?, ?, ?, ?)", (tid, author, body, at))

    def run_row(tid, outcome):
        con.execute("INSERT INTO task_runs (task_id, outcome)"
                    " VALUES (?, ?)", (tid, outcome))

    def guarded(tid, n, first=None, step=120):
        t = first if first is not None else NOW - 40 * 60
        for i in range(n):
            ev(tid, "respawn_guarded", t + i * step,
               json.dumps({"reason": "active_pr"}))

    def card(tid, prio, title):
        con.execute("INSERT INTO tasks (id, title, assignee, status,"
                    " priority, claim_lock, created_at) VALUES"
                    " (?, ?, 'coder', 'ready', ?, NULL, ?)",
                    (tid, title, prio, T0 - 600))

    # t1: own handoff comment with a PR URL, zero own runs (only a
    # scheduled stub), 8 ticks within 40 min.
    card("t1", 80, "Rework PR #357 @ 902037e: ungated webflux leg red")
    run_row("t1", "scheduled")
    cmt("t1", "coder",
        "Handing over the evidence of the previous run (rework already "
        "pushed and reviewed). %s" % PR_URL, T0)
    guarded("t1", 8)

    # t2: the same at p40: outside the p>=80 layer.
    card("t2", 40, "pinning tests")
    run_row("t2", "scheduled")
    cmt("t2", "coder", "Link: %s" % PR_URL, T0)
    guarded("t2", 8)

    # t3: a young streak (5 min).
    card("t3", 80, "Rework PR #359 @ abcdef0")
    run_row("t3", "scheduled")
    cmt("t3", "coder", "Link: %s" % PR_URL, T0)
    guarded("t3", 3, first=NOW - 5 * 60)

    # t4: ticks, then spawned: the streak is broken.
    card("t4", 80, "Rework PR #361 @ 1234567")
    run_row("t4", "scheduled")
    cmt("t4", "coder", "Link: %s" % PR_URL, T0 - 7200)
    guarded("t4", 8, first=NOW - 3 * 3600)
    ev("t4", "spawned", NOW - 10 * 60)

    # t5: a legitimate guard: own completed run, own PR comment.
    # The finding stays (diagnostics), the cause says "own runs 1".
    card("t5", 80, "Rework PR #363 @ 9876543 after its own run")
    run_row("t5", "completed")
    cmt("t5", "coder", "Opened PR: %s" % PR_URL, T0)
    guarded("t5", 8)

    con.commit()
    con.close()
    return db


def load_module(root, src=None):
    mod = types.ModuleType("bw_guarded")
    mod.__file__ = BW_PATH
    code = src if src is not None else open(BW_PATH, encoding="utf-8").read()
    exec(compile(code, BW_PATH, "exec"), mod.__dict__)
    mod.BOARDS = os.path.join(root, "boards")
    return mod


def full_run(mod):
    problems = []
    findings = mod.guarded_ready_lint(SLUG)
    by_tid = {}
    for key, rank, text in findings:
        by_tid[key.rsplit(":", 1)[1]] = text
    if sorted(by_tid) != ["t1", "t5"]:
        problems.append("findings %s, expected [t1, t5]" % sorted(by_tid))
        return problems
    t1 = by_tid["t1"]
    for marker in ("active_pr x8", "assignee-self", "own runs 0",
                   "READY-UNDER-GUARD", "t1 (p80)"):
        if marker not in t1:
            problems.append("t1: marker %r missing" % marker)
    t5 = by_tid["t5"]
    if "own runs 1" not in t5:
        problems.append("t5: classification 'own runs 1' missing")
    if "assignee-self" not in t5:
        problems.append("t5: link author (assignee-self) missing")
    return problems


def template_run(mod):
    """_rework_arm_card: a body without a github URL, with the hygiene rule."""
    problems = []
    captured = {}

    def fake_create(slug, title, body, assignee, prio, key, skills,
                    guard=None):
        captured["body"] = body
        captured["title"] = title
        return "t_new"

    mod.svc_priority = lambda slug, number, default: default
    mod._kanban_create = fake_create
    mod._rework_arm_card(SLUG, 357, "902037ee", {"at": NOW})
    body = captured.get("body") or ""
    if "github.com" in body or "/pull/" in body:
        problems.append("the rework arm template contains a PR URL")
    for marker in ("Comment hygiene", "until the first run", "active_pr"):
        if marker not in body:
            problems.append("template: marker %r missing" % marker)
    return problems


def main():
    src = open(BW_PATH, encoding="utf-8").read()
    for name, (old, _new) in MUTANTS.items():
        if src.count(old) != 1:
            print("FAIL: mutant anchor %s occurs %d times"
                  % (name, src.count(old)))
            return 1

    with tempfile.TemporaryDirectory() as tmp:
        make_board(tmp)
        mod = load_module(tmp)
        problems = full_run(mod) + template_run(mod)
        if problems:
            print("FAIL: clean run:")
            for p in problems:
                print("  - %s" % p)
            return 1
        print("OK: t1 found with its cause (active_pr x8, assignee-self, "
              "0 runs); t5 classified (1 run); t2/t3/t4 silent; the rework "
              "arm template has no URL and carries the hygiene rule")

    failed = 0
    for name, (old, new) in MUTANTS.items():
        if src.count(new) or src.count(old) != 1:
            print("FAIL: mutant %s does not change its anchor" % name)
            failed += 1
            continue
        with tempfile.TemporaryDirectory() as tmp:
            make_board(tmp)
            mod = load_module(tmp, src.replace(old, new, 1))
            try:
                problems = full_run(mod) + template_run(mod)
            except Exception as exc:
                problems = ["crashed: %s: %s" % (type(exc).__name__, exc)]
            if problems:
                print("OK  %s: mutant killed (%s)"
                      % (name, problems[0][:96]))
            else:
                print("FAIL %s: mutant survived, the test is blind to this branch" % name)
                failed += 1
    if failed:
        print("TOTAL: FAIL, problems %d" % failed)
        return 1
    print("TOTAL: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
