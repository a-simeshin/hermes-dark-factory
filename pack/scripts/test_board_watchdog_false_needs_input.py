#!/usr/bin/env python3
"""False needs_input blocks on agent-only steps.

A card blocked with kind='needs_input' whose condition requires only agent
steps (update the branch, wait for required checks, rerun) is a false block:
the owner's only part in a PR is the merge. The gate needs_input_tech_gate
returns such a card to its assignee (unblock) with a "Watchdog:" comment.
Fail closed: any sign of an owner event in the condition (merge, approval,
decision, milestone, acceptance, budget, rights, question) disables the gate.

The gate must:
  t1  a long live-style text that mentions MERGED / merge -> leave alone
      (the condition legitimately waits for a merge);
  t2  the same text without merge phrases, owner steps are only "update the
      branch, wait for required checks" -> unblock + exactly one comment
      "Watchdog: false needs_input block", with no github.com URL;
  t3  a plain wait for a merge (PR MERGED) -> leave alone;
  t4  technical step plus a decision (wait for CI, then prototype
      acceptance) -> leave alone (fail closed);
  t5  the text of t2 but kind='dependency' -> leave alone (not needs_input);
  t2b a repeated false block of t2 with the same text: only a finding in the
      report, NO second unblock or comment (anti ping-pong).

Mutants must turn the test red: no technical marker check (t3 goes to
unblock), no fail-closed owner check (t1/t4 go), kind check skipped (t5
goes), unblock replaced (t2 stays blocked), comment replaced (t2 without
the watchdog text), anti ping-pong removed (t2b makes a second unblock).
"""
import json
import os
import sqlite3
import sys
import tempfile
import time
import types

# Project settings are read at import time: set neutral values first.
_HOME = tempfile.mkdtemp(prefix="bw-test-home-")
os.environ["HERMES_HOME"] = _HOME
os.environ.update({
    "FACTORY_REPO": "acme/widget",
    "FACTORY_REPO_PATH": "/srv/hermes/widget",
    "FACTORY_BOARD": "main",
    "FACTORY_OWNER_LOGIN": "owner-login",
    "FACTORY_OWNER_CHAT": "telegram:000000000",
    "FACTORY_BASE_BRANCHES": "develop/1.x,develop/2.x",
})

TESTED = "board-watchdog.py"
BW_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       TESTED)
SLUG = "main"
NOW = int(time.time())

# t1: a live-style block reason (about 600 characters) that waits for a stack
# of PRs to be MERGED, with the update-branch and checks steps inside.
LIVE_REASON = (
    "Release condition: the stack #313/#315/#317/#318 is all MERGED. "
    "Right now 3 of 4 are merged (the last, #315, at 06:22 UTC on the "
    "verdict head b0a399d2), #318 is left (OPEN, b92253f7, direct merge "
    "CONFLICTING). The owner steps are published in PR #318 "
    "(issuecomment-6597233769): update the branch from develop/2.x (the "
    "conflict disappears, the union tree is verified, IT 13/13 green), wait "
    "for the green required checks, merge. The merge is for a human. After "
    "the block is lifted: a live check of gh pr view 313 315 317 318 and "
    "closing issue #253 by the six steps from comment 6421, then "
    "kanban_complete root.")

# t2: same story, but the condition needs ONLY agent steps (the merge is no
# longer mentioned) -> false block.
TECH_ONLY_REASON = (
    "Release condition: the PR branch is green. The owner steps are "
    "published in PR #318 (issuecomment-6597233769): update the branch from "
    "develop/2.x (the conflict disappears, the union tree is verified, IT "
    "13/13 green), wait for the green required checks. After the block is "
    "lifted: closing issue #253 by the six steps from comment 6421.")

MERGE_WAIT_REASON = "Release condition: PR #290 MERGED (the merge is for a human)."

TECH_PLUS_DECISION_REASON = (
    "Release condition: wait for green CI, then the owner accepts the "
    "prototype.")

MUTANTS = {
    "M_TECH": (
        "            if not NEEDS_INPUT_TECH_RE.search(cond):\n"
        "                continue          # no technical steps in the condition",
        "            if True:\n"
        "                continue          # no technical steps in the condition"),
    "M_OWNER": (
        "            if NEEDS_INPUT_OWNER_RE.search(cond):\n"
        "                continue   # an owner event is present: the block is "
        "not false",
        "            if False:\n"
        "                continue"),
    "M_KIND": (
        "\"AND coalesce(block_kind, '') = 'needs_input'\").fetchall()",
        "\"AND coalesce(block_kind, '') != 'xx'\").fetchall()"),
    "M_UNBLOCK": (
        "            run([HERMES, \"kanban\", \"--board\", slug, \"unblock\","
        " tid],\n"
        "                timeout=90)",
        "            run([HERMES, \"kanban\", \"--board\", slug, \"show\","
        " tid],\n"
        "                timeout=90)"),
    "M_COMMENT": (
        "            run([HERMES, \"kanban\", \"--board\", slug, \"comment\","
        " tid,\n"
        "                 \"Watchdog: false needs_input block: the condition "
        "requires \"",
        "            run([HERMES, \"kanban\", \"--board\", slug, \"edit\","
        " tid, \"--priority\", \"0\",\n"
        "                 \"Watchdog: false needs_input block: the condition "
        "requires \""),
    "M_LOOP": (
        "            if already:",
        "            if False:"),
}


class Recorder(object):
    """run() of the gate: the hermes CLI is applied to the fixture database
    as the live one would: unblock sets ready and writes an unblocked event,
    comment writes a row to task_comments by the author watchdog."""

    def __init__(self, db):
        self.db = db
        self.calls = []
        self._tick = NOW

    def _stamp(self):
        self._tick += 1
        return self._tick

    def __call__(self, cmd, timeout=120):
        self.calls.append(list(cmd))
        if cmd[0] == "git":
            return ""
        if "unblock" in cmd:
            tid = cmd[cmd.index("unblock") + 1]
            con = sqlite3.connect(self.db)
            con.execute("UPDATE tasks SET status = 'ready' WHERE id = ?",
                        (tid,))
            con.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload,"
                " created_at) VALUES (?, NULL, 'unblocked', '{}', ?)",
                (tid, self._stamp()))
            con.commit()
            con.close()
            return "ok"
        if "comment" in cmd:
            i = cmd.index("comment")
            tid, body = cmd[i + 1], cmd[i + 2]
            con = sqlite3.connect(self.db)
            con.execute(
                "INSERT INTO task_comments (task_id, author, body,"
                " created_at) VALUES (?, 'watchdog', ?, ?)",
                (tid, body, self._stamp()))
            con.commit()
            con.close()
            return "ok"
        return "ok"


def make_board(root):
    boards = os.path.join(root, "boards", SLUG)
    os.makedirs(boards, exist_ok=True)
    db = os.path.join(boards, "kanban.db")
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT,
            priority INTEGER, block_kind TEXT, claim_lock INTEGER,
            created_at INTEGER);
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
            run_id INTEGER, kind TEXT, payload TEXT, created_at INTEGER);
        CREATE TABLE task_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
            author TEXT, body TEXT, created_at INTEGER);
        CREATE TABLE task_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
            profile TEXT, status TEXT, outcome TEXT, summary TEXT,
            started_at INTEGER, ended_at INTEGER);
    """)
    cards = [
        ("t1", "root stack", "architect", LIVE_REASON, "needs_input"),
        ("t2", "rework PR #318", "coder", TECH_ONLY_REASON, "needs_input"),
        ("t3", "merge tail #290", "coder", MERGE_WAIT_REASON,
         "needs_input"),
        ("t4", "prototype gate", "tester", TECH_PLUS_DECISION_REASON,
         "needs_input"),
        ("t5", "dependency twin", "coder", TECH_ONLY_REASON, "dependency"),
    ]
    for n, (tid, title, assignee, reason, kind) in enumerate(cards):
        con.execute(
            "INSERT INTO tasks (id, title, assignee, status, priority,"
            " block_kind, claim_lock, created_at) VALUES (?, ?, ?,"
            " 'blocked', 50, ?, NULL, ?)",
            (tid, title, assignee, kind, NOW - 3600 + n))
        con.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload,"
            " created_at) VALUES (?, NULL, 'blocked', ?, ?)",
            (tid, json.dumps({"reason": reason}, ensure_ascii=False),
             NOW - 3500 + n))
        con.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome,"
            " summary, started_at, ended_at) VALUES (?, ?, 'completed',"
            " 'blocked', ?, ?, ?)",
            (tid, assignee, reason, NOW - 3600 + n, NOW - 3500 + n))
    con.commit()
    con.close()
    return db


def load_module(root):
    mod = types.ModuleType("bw_false_needs_input")
    mod.__file__ = BW_PATH
    code = compile(open(BW_PATH, encoding="utf-8").read(), BW_PATH, "exec")
    exec(code, mod.__dict__)
    mod.BOARDS = os.path.join(root, "boards")
    mod.PROFILES_ROOT = os.path.join(root, "profiles")
    return mod


def side_calls(rec):
    return [c for c in rec.calls if c[0].endswith("/hermes")]


def unblock_calls(rec):
    return [c[c.index("unblock") + 1] for c in rec.calls
            if "unblock" in c]


def comment_calls(rec):
    return [(c[c.index("comment") + 1], c[c.index("comment") + 2])
            for c in rec.calls if "comment" in c]


def _keys(findings):
    return sorted(f[0].rsplit(":", 1)[1] for f in findings)


def _card_state(db, tid):
    con = sqlite3.connect(db)
    st = con.execute("SELECT status FROM tasks WHERE id = ?",
                     (tid,)).fetchone()[0]
    wc = con.execute("SELECT COUNT(*) FROM task_comments WHERE task_id = ?"
                     " AND body LIKE 'Watchdog:%'", (tid,)).fetchone()[0]
    un = con.execute("SELECT COUNT(*) FROM task_events WHERE task_id = ?"
                     " AND kind = 'unblocked'", (tid,)).fetchone()[0]
    con.close()
    return st, wc, un


def full_run(mod, rec, db):
    problems = []

    rec.calls = []
    findings = mod.needs_input_tech_gate(SLUG, apply=False)
    if _keys(findings) != ["t2"]:
        problems.append("dry-run findings %s, expected [t2] (the live text "
                        "t1 with MERGED is left alone)" % _keys(findings))
    if side_calls(rec):
        problems.append("dry-run touches the world: %s" % side_calls(rec))

    rec.calls = []
    findings = mod.needs_input_tech_gate(SLUG)
    if _keys(findings) != ["t2"]:
        problems.append("apply: findings %s, expected [t2]"
                        % _keys(findings))
    ub = unblock_calls(rec)
    if ub != ["t2"]:
        problems.append("unblock is not t2: %s" % ub)
    cc = comment_calls(rec)
    if len(cc) != 1 or cc[0][0] != "t2":
        problems.append("comment is not single / not on t2: %s" % cc)
    if cc and not cc[0][1].startswith("Watchdog:"):
        problems.append("comment without the prefix 'Watchdog:'")
    if cc and "false needs_input block" not in cc[0][1]:
        problems.append("the comment has no false-block marker")
    if cc and "only part in a PR is the merge" not in cc[0][1]:
        problems.append("the comment has no 'only the merge' rule")
    if cc and ("github.com" in cc[0][1] or "/pull/" in cc[0][1]):
        problems.append("the watchdog comment contains a PR URL")
    if findings and "Unblocked" not in findings[0][2]:
        problems.append("the report has no trace of the application")

    rec.calls = []
    findings2 = mod.needs_input_tech_gate(SLUG)
    if findings2 or side_calls(rec):
        problems.append("the repeat pass is not silent: %s / %s"
                        % ([f[0] for f in findings2], side_calls(rec)))

    for tid in ("t1", "t3", "t4", "t5"):
        st, wc, un = _card_state(db, tid)
        if st != "blocked" or wc or un:
            problems.append("%s touched: status=%s watchdog comments=%d"
                            " unblocked=%d (expected blocked/0/0)"
                            % (tid, st, wc, un))
    st, wc, un = _card_state(db, "t2")
    if st != "ready" or wc != 1 or un != 1:
        problems.append("t2 after apply: status=%s comments=%d"
                        " unblocked=%d (expected ready/1/1)" % (st, wc, un))

    # t2b: the worker falsely blocks t2 again with the same text. The gate
    # reports, but makes no second unblock or comment.
    con = sqlite3.connect(db)
    con.execute("UPDATE tasks SET status = 'blocked' WHERE id = 't2'")
    con.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload,"
        " created_at) VALUES ('t2', NULL, 'blocked', ?, ?)",
        (json.dumps({"reason": TECH_ONLY_REASON}, ensure_ascii=False),
         NOW + 10))
    con.commit()
    con.close()
    rec.calls = []
    findings3 = mod.needs_input_tech_gate(SLUG)
    if _keys(findings3) != ["t2"]:
        problems.append("the repeat false block is not reported: %s"
                        % _keys(findings3))
    if findings3 and "Repeat" not in findings3[0][2]:
        problems.append("the report has no 'Repeat' mark")
    if side_calls(rec):
        problems.append("anti ping-pong is broken: a second unblock or "
                        "comment goes out: %s" % side_calls(rec))
    st, wc, un = _card_state(db, "t2")
    if st != "blocked" or wc != 1 or un != 1:
        problems.append("t2 after the repeat block: status=%s comments=%d "
                        "unblocked=%d (expected blocked/1/1)"
                        % (st, wc, un))
    return problems


def main():
    src = open(BW_PATH, encoding="utf-8").read()
    for name, (old, _new) in MUTANTS.items():
        if src.count(old) != 1:
            print("FAIL: the anchor of mutant %s occurs %d times"
                  % (name, src.count(old)))
            return 1

    with tempfile.TemporaryDirectory() as tmp:
        db = make_board(tmp)
        rec = Recorder(db)
        mod = load_module(tmp)
        mod.run = rec
        problems = full_run(mod, rec, db)
        if problems:
            print("FAIL: clean run:")
            for p in problems:
                print("  - %s" % p)
            return 1
        print("OK: the live-style text (MERGED in the condition) is left "
              "alone; t2 (only update-branch + waiting for CI) is unblocked "
              "with a 'Watchdog:' comment; t3/t4/t5 are protected; the "
              "repeat and anti ping-pong are silent")

    failed = 0
    for name, (old, new) in MUTANTS.items():
        if src.count(new) or src.count(old) != 1:
            print("SKIP: mutant %s changes the anchor" % name)
            failed += 1
            continue
        with tempfile.TemporaryDirectory() as tmp:
            db = make_board(tmp)
            rec = Recorder(db)
            mod = types.ModuleType("bw_false_needs_input_%s" % name)
            mod.__file__ = BW_PATH
            mutated = src.replace(old, new, 1)
            if mutated == src:
                print("FAIL %s: the mutation changed nothing" % name)
                failed += 1
                continue
            exec(compile(mutated, BW_PATH + ":" + name, "exec"),
                 mod.__dict__)
            mod.BOARDS = os.path.join(tmp, "boards")
            mod.PROFILES_ROOT = os.path.join(tmp, "profiles")
            mod.run = rec
            try:
                problems = full_run(mod, rec, db)
            except Exception as exc:
                problems = ["crashed: %s: %s" % (type(exc).__name__, exc)]
            if problems:
                print("OK  %s: mutant killed (%s)"
                      % (name, problems[0][:96]))
            else:
                print("FAIL %s: the mutant survived, the test is blind to "
                      "this branch" % name)
                failed += 1
    if failed:
        print("RESULT: FAIL, problems %d" % failed)
        return 1
    print("RESULT: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
