#!/usr/bin/env python3
"""Stuck active_pr guard after an owner decision.

A worker blocks a card with a comment that links a PR; the owner answers with
a decision and unblocks the card. The dispatcher core
(_respawn_blocker) keeps the card under respawn_guarded(active_pr) until a
handoff event AFTER the PR comment: an unblock and a verdict do not lift the
guard, and the factory stands with empty slots.

The gate active_pr_handoff_gate (board-watchdog.py) must:
  t1  guard for 30 ticks + a decision AFTER the PR comment -> reassign through
      the bounce profile and back (a handoff event) + a reason comment with
      the prefix "Watchdog:"; a repeated pass is silent (idempotency by the
      assigned event after the PR comment, not by state);
  t2  guard for 30 ticks, silence after the PR comment -> do NOT touch: the
      guard against a duplicate PR is right;
  t3  guard was there, the handoff is already set -> do NOT touch twice;
  t4  young guard (5 ticks) with a live decision -> do NOT touch;
  t5  the owner decision is EARLIER than the PR comment (verdict first, then
      the worker opened a PR and linked it), nothing after the link -> do NOT
      touch. The decision stamp is an ISO string before T0: INTEGER affinity
      leaves it alone, the SQL window created_at > pr_time lets it through
      (SQLite: TEXT is always greater than INTEGER), so the only guard is the
      python recheck x > pr_time;
  t6  symmetric to t5 for an unblocked event earlier than the PR comment.

Mutants turn both branches red: three remove protective skips (t2, t3, t4
would go to reassign), two disable the action (no reassign / no comment is
sent), two drop the python time recheck in the decision checks (t5, t6: a TEXT
stamp before the PR comment counts as a decision and the guard is lifted by
mistake). Mutation 4 is also caught by the second pass: without assigned
events the gate repeats its actions.
"""
import json
import os
import sqlite3
import sys
import tempfile
import time
import types
from datetime import datetime

TESTED = "board-watchdog.py"
BW_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       TESTED)
HOME = tempfile.mkdtemp(prefix="apd-home-")
os.environ["HERMES_HOME"] = HOME
os.environ["FACTORY_REPO"] = "acme/widget"
os.environ["FACTORY_REPO_PATH"] = "/srv/hermes/widget"
os.environ["FACTORY_BOARD"] = "main"
os.environ["FACTORY_OWNER_LOGIN"] = "owner-login"
os.environ["FACTORY_OWNER_CHAT"] = "telegram:000000000"
os.environ["FACTORY_BASE_BRANCHES"] = "main"
SLUG = "apdfix"
NOW = int(time.time())
T0 = NOW - 5400            # the PR comment, 90 minutes ago
PR_URL = ("https://github.com/acme/widget/pull/178"
          "#issuecomment-6552079009")
# the gate takes the owner from the repository of the board (git remote)
OWNER = "acme"

MUTANTS = {
    "M_TICKS": (
        "            if guard_count < ACTIVE_PR_STUCK_TICKS:",
        "            if guard_count < ACTIVE_PR_STUCK_TICKS and False:"),
    "M_DECISION": (
        "            if not unblocked and not owner_decided:",
        "            if not unblocked and not owner_decided and False:"),
    "M_HANDOFF": (
        "            if handoff:",
        "            if handoff and False:"),
    "M_NOREASSIGN": (
        "        for target in (bounce, c[\"assignee\"]):",
        "        for target in ():"),
    "M_COMMENT": (
        "        run([HERMES, \"kanban\", \"--board\", slug, \"comment\", c[\"tid\"],",
        "        run([HERMES, \"kanban\", \"--board\", slug, \"edit\", c[\"tid\"],"),
    "M_OWNER_EARLY": (
        "                    if x and x > pr_time and owner in (body or \"\"):",
        "                    if x and owner in (body or \"\"):"),
    "M_UNBLOCK_EARLY": (
        "                if x and x > pr_time:\n"
        "                    unblocked += 1",
        "                if x:\n"
        "                    unblocked += 1"),
}


class Recorder(object):
    """run() of the gate: git remote get-url from a canned answer, the hermes
    CLI is applied to the fixture database as the live CLI does it: reassign
    changes the assignee and writes an assigned event with from, comment
    writes a row into task_comments."""

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
            if "get-url" in cmd:
                return "https://github.com/acme/widget.git"
            return ""
        if "reassign" in cmd:
            i = cmd.index("reassign")
            tid, prof = cmd[i + 1], cmd[i + 2]
            con = sqlite3.connect(self.db)
            prev = con.execute("SELECT assignee FROM tasks WHERE id = ?",
                               (tid,)).fetchone()[0]
            con.execute("UPDATE tasks SET assignee = ? WHERE id = ?",
                        (prof, tid))
            con.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload,"
                " created_at) VALUES (?, NULL, 'assigned', ?, ?)",
                (tid, json.dumps({"assignee": prof, "from": prev}),
                 self._stamp()))
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
    """Fixture: board SLUG with six cards. Times: PR comment at T0, the
    decision about 20 minutes later, the guard every ~2 minutes after the
    decision."""
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

    def ev(tid, kind, at, payload="{}"):
        con.execute("INSERT INTO task_events (task_id, run_id, kind, payload,"
                    " created_at) VALUES (?, NULL, ?, ?, ?)",
                    (tid, kind, payload, at))

    def cmt(tid, author, body, at):
        con.execute("INSERT INTO task_comments (task_id, author, body,"
                    " created_at) VALUES (?, ?, ?, ?)",
                    (tid, author, body, at))

    def guarded(tid, n):
        for i in range(n):
            ev(tid, "respawn_guarded", T0 + 1140 + i * 120,
               json.dumps({"reason": "active_pr"}))

    def card(tid, title):
        con.execute("INSERT INTO tasks (id, title, assignee, status,"
                    " priority, claim_lock, created_at) VALUES"
                    " (?, ?, 'coder', 'ready', 80, NULL, ?)",
                    (tid, title, T0 - 600))
        cmt(tid, "coder",
            "A fork for the owner: %s options A/B/C." % PR_URL, T0)
        ev(tid, "blocked", T0 + 5)

    # t1: the stuck case, the guard is not lifted yet.
    card("t1", "Port the blocks of #154 into #178 (stuck guard)")
    ev("t1", "unblocked", T0 + 1140)
    cmt("t1", "github",
        "GitHub @%s in PR #178: owner decision, C + B, not A." % OWNER,
        T0 + 1260)
    guarded("t1", 30)

    # t2: silence after the PR comment (waits for a decision): the guard is
    # right.
    card("t2", "Port the blocks of #154 into #179 (waits for a decision)")
    guarded("t2", 30)

    # t3: the owner already lifted it by hand -> do not touch a second time.
    card("t3", "Port the blocks of #154 into #180 (already lifted by hand)")
    ev("t3", "unblocked", T0 + 1140)
    cmt("t3", "github",
        "GitHub @%s in PR #180: owner decision, C + B." % OWNER,
        T0 + 1260)
    guarded("t3", 30)
    ev("t3", "assigned", T0 + 4700,
       json.dumps({"assignee": "architect", "from": "coder"}))
    ev("t3", "assigned", T0 + 4710,
       json.dumps({"assignee": "coder", "from": "architect"}))

    # t4: a young guard (5 ticks) with a live decision -> wait.
    card("t4", "Port the blocks of #154 into #181 (young guard)")
    ev("t4", "unblocked", T0 + 1140)
    cmt("t4", "github",
        "GitHub @%s in PR #181: owner decision, C + B." % OWNER,
        T0 + 1260)
    guarded("t4", 5)

    # t5: the owner decision is EARLIER than the PR comment (verdict first,
    # then the worker opened a PR and linked it), nothing after the link.
    # The decision stamp is an ISO string before T0: INTEGER affinity leaves
    # it alone, the SQL window lets it through (TEXT is greater than
    # INTEGER), the python recheck x > pr_time must drop it.
    card("t5", "Port the blocks of #154 into #182 (decision before the link)")
    con.execute("INSERT INTO task_comments (task_id, author, body,"
                " created_at) VALUES (?, 'github', ?, ?)",
                ("t5", "GitHub @%s in PR #182: owner decision,"
                 " C + B, not A." % OWNER,
                 datetime.fromtimestamp(T0 - 300).isoformat()))
    guarded("t5", 30)

    # t6: the symmetric case for the unblocked branch: unblock before the link.
    card("t6", "Port the blocks of #154 into #183 (unblock before the link)")
    con.execute("INSERT INTO task_events (task_id, run_id, kind, payload,"
                " created_at) VALUES (?, NULL, 'unblocked', '{}', ?)",
                ("t6", datetime.fromtimestamp(T0 - 600).isoformat()))
    guarded("t6", 30)

    con.commit()
    con.close()
    # board_repo: default_workdir must exist, the remote goes through the
    # Recorder (canned acme/widget.git).
    workdir = os.path.join(root, "repo")
    os.makedirs(workdir, exist_ok=True)
    with open(os.path.join(bdir, "board.json"), "w") as fh:
        json.dump({"default_workdir": workdir}, fh)
    # profile_live: architect and coder are live (a config.yaml marker).
    for p in ("architect", "coder"):
        os.makedirs(os.path.join(root, "profiles", p), exist_ok=True)
        open(os.path.join(root, "profiles", p, "config.yaml"), "w").close()
    return db


def load_module(root):
    mod = types.ModuleType("bw_apd")
    mod.__file__ = BW_PATH
    code = compile(open(BW_PATH, encoding="utf-8").read(), BW_PATH, "exec")
    exec(code, mod.__dict__)
    mod.BOARDS = os.path.join(root, "boards")
    mod.PROFILES_ROOT = os.path.join(root, "profiles")
    return mod


def side_calls(rec):
    return [c for c in rec.calls if c[0].endswith("/hermes")]


def reassign_calls(rec):
    return [(c[c.index("reassign") + 1], c[c.index("reassign") + 2])
            for c in rec.calls if "reassign" in c]


def comment_calls(rec):
    return [(c[c.index("comment") + 1], c[c.index("comment") + 2])
            for c in rec.calls if "comment" in c]


def full_run(mod, rec):
    problems = []
    rec.calls = []
    findings = mod.active_pr_handoff_gate(SLUG, apply=False)
    keys = [f[0].rsplit(":", 1)[1] for f in findings]
    if keys != ["t1"]:
        problems.append("dry-run findings %s, expected [t1]" % keys)
    if side_calls(rec):
        problems.append("dry-run touches the world: %s" % side_calls(rec))

    rec.calls = []
    findings = mod.active_pr_handoff_gate(SLUG)
    keys = [f[0].rsplit(":", 1)[1] for f in findings]
    if keys != ["t1"]:
        problems.append("apply: findings %s, expected [t1]" % keys)
    rr = reassign_calls(rec)
    if rr != [("t1", "architect"), ("t1", "coder")]:
        problems.append("reassign is not t1 / not a bounce back: %s" % rr)
    cc = comment_calls(rec)
    if len(cc) != 1 or cc[0][0] != "t1":
        problems.append("the comment is not one / not on t1: %s" % cc)
    if cc and not cc[0][1].startswith("Watchdog:"):
        problems.append("the comment has no \"Watchdog:\" prefix")
    if cc and "active_pr" not in cc[0][1]:
        problems.append("the comment has no reason (active_pr)")
    if findings and "coder->architect->coder" not in findings[0][2]:
        problems.append("the report has no handoff trace coder->architect->coder")
    if findings and "30 respawn_guarded ticks" not in findings[0][2]:
        problems.append("the report has no guard tick counter")

    rec.calls = []
    findings2 = mod.active_pr_handoff_gate(SLUG)
    if findings2 or side_calls(rec):
        problems.append("repeated pass is not silent: %s / %s"
                        % ([f[0] for f in findings2], side_calls(rec)))

    con = sqlite3.connect(rec.db)
    ass = con.execute("SELECT assignee FROM tasks WHERE id = 't1'"
                      ).fetchone()[0]
    if ass != "coder":
        problems.append("t1 assignee after the bounce: %s, expected coder"
                        % ass)
    h = con.execute("SELECT COUNT(*) FROM task_events WHERE task_id = 't1'"
                    " AND kind = 'assigned'").fetchone()[0]
    if h != 2:
        problems.append("t1 assigned events %d, expected 2" % h)
    con.close()
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
        problems = full_run(mod, rec)
        if problems:
            print("FAIL: clean run:")
            for p in problems:
                print("  - %s" % p)
            return 1
        print("OK: t1 lifted by reassign coder->architect->coder with a"
              " \"Watchdog:\" comment; t2/t3/t4 untouched; repeat is silent")

    failed = 0
    for name, (old, new) in MUTANTS.items():
        if src.count(new) or src.count(old) != 1:
            print("SKIP: mutant %s changes the anchor" % name)
            failed += 1
            continue
        with tempfile.TemporaryDirectory() as tmp:
            db = make_board(tmp)
            rec = Recorder(db)
            mod = types.ModuleType("bw_apd_%s" % name)
            mod.__file__ = BW_PATH
            mutated = src.replace(old, new, 1)
            exec(compile(mutated, BW_PATH + ":" + name, "exec"),
                 mod.__dict__)
            mod.BOARDS = os.path.join(tmp, "boards")
            mod.PROFILES_ROOT = os.path.join(tmp, "profiles")
            mod.run = rec
            try:
                problems = full_run(mod, rec)
            except Exception as exc:
                problems = ["crashed: %s: %s" % (type(exc).__name__, exc)]
            if problems:
                print("OK  %s: mutant caught (%s)"
                      % (name, problems[0][:96]))
            else:
                print("FAIL %s: the mutant survived, the test is blind to"
                      " this branch" % name)
                failed += 1
    if failed:
        print("RESULT: FAIL, problems %d" % failed)
        return 1
    print("RESULT: OK")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        import shutil
        shutil.rmtree(HOME, ignore_errors=True)
