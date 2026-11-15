#!/usr/bin/env python3
"""False active_pr guard caused by somebody else's PR link.

Case: a rebase card (p96, coder, ready) with no run of its own. A reviewer
cross-posted into the card a verdict that carries a PR link. The dispatcher
core (_respawn_blocker, case 4) checks only that a fresh comment contains a
PR URL, not who wrote it, so the card hung under respawn_guarded(active_pr)
for a hundred ticks with empty slots while the rebase stood. The decision-based
branch (see test_board_watchdog_active_pr_decision.py) was silent here: after
the link there is no decision at all.

Rule: a PR link in a comment of another profile does not mean the card's work
is done. A card that the active_pr guard holds for more than two ticks in a
row (> 2, that is >= 3) with zero runs of its own is taken out of the guard by
the built-in core move (bounce reassign -> a handoff event AFTER the link) and
the watchdog reports it. A card with a run of its own and a PR is still held;
a link from the executor itself (author == assignee) with zero runs is not
touched either: fail closed.

The gate active_pr_handoff_gate (board-watchdog.py) must:
  t1  the live case: ready, coder, zero own runs (one scheduled stub), the link
      from reviewer, three guard ticks, silence after the link -> bounce
      reassign coder->architect->coder + a "Watchdog:" comment WITHOUT a PR URL
      (a URL would re-arm the guard for 24 hours) + a report; a repeated pass
      is silent;
  t2  an own completed run + a link from reviewer -> do NOT touch (real_runs >
      0: the guard may be right);
  t3  zero runs, the link from the executor itself (coder) -> do NOT touch
      (author == assignee: fail closed);
  t4  a link from reviewer, zero runs, but only 2 ticks -> do NOT touch
      (threshold > 2 ticks);
  t5  an own completed run and the executor writes the link -> do NOT touch.

Mutants turn the test red: tick threshold (t4 goes to reassign), author check
(t3 goes), runs check (t2 goes), the action (reassign / comment is not sent ->
no handoff event / no comment).
"""
import json
import os
import sqlite3
import sys
import tempfile
import time
import types

TESTED = "board-watchdog.py"
BW_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       TESTED)
HOME = tempfile.mkdtemp(prefix="apf-home-")
os.environ["HERMES_HOME"] = HOME
os.environ["FACTORY_REPO"] = "acme/widget"
os.environ["FACTORY_REPO_PATH"] = "/srv/hermes/widget"
os.environ["FACTORY_BOARD"] = "main"
os.environ["FACTORY_OWNER_LOGIN"] = "owner-login"
os.environ["FACTORY_OWNER_CHAT"] = "telegram:000000000"
os.environ["FACTORY_BASE_BRANCHES"] = "main"
SLUG = "apffix"
NOW = int(time.time())
T0 = NOW - 5400            # the reviewer's PR comment, 90 minutes ago
PR_URL = ("https://github.com/acme/widget/pull/203"
          "#issuecomment-6578154492")

MUTANTS = {
    "M_TICKS49": (
        "            if guard_count < ACTIVE_PR_FOREIGN_TICKS:",
        "            if guard_count < ACTIVE_PR_FOREIGN_TICKS and False:"),
    "M_AUTHOR": (
        "            if pr_author and pr_author != assignee:",
        "            if pr_author:"),
    "M_RUNS": (
        "                if not real_runs:",
        "                if not real_runs or True:"),
    "M_REASSIGN": (
        "        for target in (bounce, c[\"assignee\"]):",
        "        for target in ():"),
    "M_COMMENT": (
        "        run([HERMES, \"kanban\", \"--board\", slug, \"comment\", c[\"tid\"],",
        "        run([HERMES, \"kanban\", \"--board\", slug, \"edit\", c[\"tid\"],"),
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
    """Fixture: board SLUG, the live case (t1) and four negatives. Times: the
    reviewer's link, an unblock before it, the guard ticks AFTER the link.
    task_runs with scheduled stubs and completed runs."""
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
                    " created_at) VALUES (?, ?, ?, ?)",
                    (tid, author, body, at))

    def run_row(tid, outcome):
        con.execute("INSERT INTO task_runs (task_id, outcome)"
                    " VALUES (?, ?)", (tid, outcome))

    def guarded(tid, n):
        for i in range(n):
            ev(tid, "respawn_guarded", T0 + 300 + i * 120,
               json.dumps({"reason": "active_pr"}))

    def card(tid, title):
        con.execute("INSERT INTO tasks (id, title, assignee, status,"
                    " priority, claim_lock, created_at) VALUES"
                    " (?, ?, 'coder', 'ready', 96, NULL, ?)",
                    (tid, title, T0 - 600))

    # t1: the live case. Before the link: a scheduled stub and an unblock
    # event (they do not count: strictly nothing AFTER the link). Exactly
    # three guard ticks.
    card("t1", "rebase PR #203 onto develop/2.x@06e1b0a")
    run_row("t1", "scheduled")
    cmt("t1", "default",
        "SCHEDULED: backlog: outside the milestone (rule 2)", T0 - 7200)
    cmt("t1", "default",
        "UNBLOCK: back to ready", T0 - 300)
    ev("t1", "unblocked", T0 - 240)
    cmt("t1", "reviewer",
        "Reviewer verdict on PR #203 published on head 245093f: "
        "CHANGES REQUESTED (%s). The \"PR + verdict\" pair is ready, the "
        "rebase goes first." % PR_URL, T0)
    guarded("t1", 3)

    # t2: an own completed run + a foreign link -> hold (real_runs > 0).
    card("t2", "rebase PR #209 (own run, foreign link)")
    run_row("t2", "completed")
    cmt("t2", "reviewer", "Verdict on PR #209: %s" % PR_URL, T0)
    guarded("t2", 30)

    # t3: zero runs, the link from the executor itself -> hold.
    card("t3", "rebase PR #214 (link from the executor)")
    run_row("t3", "scheduled")
    cmt("t3", "coder", "Opened a PR: %s" % PR_URL, T0)
    guarded("t3", 30)

    # t4: a foreign link, zero runs, but 2 ticks (not more than two) ->
    # hold: the guard is young.
    card("t4", "rebase PR #218 (two ticks)")
    run_row("t4", "scheduled")
    cmt("t4", "reviewer", "Verdict on PR #218: %s" % PR_URL, T0)
    guarded("t4", 2)

    # t5: an own run AND the executor writes the link -> hold.
    card("t5", "rebase PR #221 (own run, own link)")
    run_row("t5", "completed")
    cmt("t5", "coder", "Pushed, PR: %s" % PR_URL, T0)
    guarded("t5", 30)

    con.commit()
    con.close()
    workdir = os.path.join(root, "repo")
    os.makedirs(workdir, exist_ok=True)
    with open(os.path.join(bdir, "board.json"), "w") as fh:
        json.dump({"default_workdir": workdir}, fh)
    for p in ("architect", "coder"):
        os.makedirs(os.path.join(root, "profiles", p), exist_ok=True)
        open(os.path.join(root, "profiles", p, "config.yaml"), "w").close()
    return db


def load_module(root):
    mod = types.ModuleType("bw_apf")
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
    if cc and ("github.com" in cc[0][1] or "/pull/" in cc[0][1]):
        problems.append("the watchdog comment contains a PR URL: it would "
                        "re-arm the core guard for 24 hours")
    if findings and "@reviewer" not in findings[0][2]:
        problems.append("the report has no author of the foreign link "
                        "(@reviewer)")
    if findings and "coder->architect->coder" not in findings[0][2]:
        problems.append("the report has no handoff trace coder->architect->coder")
    if findings and "3 respawn_guarded ticks" not in findings[0][2]:
        problems.append("the report has no guard tick counter")
    if findings and "no runs of its own" not in findings[0][2]:
        problems.append("the report has no reason (zero own runs)")

    rec.calls = []
    findings2 = mod.active_pr_handoff_gate(SLUG)
    if findings2 or side_calls(rec):
        problems.append("repeated pass is not silent: %s / %s"
                        % ([f[0] for f in findings2], side_calls(rec)))

    con = sqlite3.connect(rec.db)
    for tid in ("t1",):
        ass = con.execute("SELECT assignee FROM tasks WHERE id = ?",
                          (tid,)).fetchone()[0]
        if ass != "coder":
            problems.append("%s assignee after the bounce: %s, expected coder"
                            % (tid, ass))
        h = con.execute("SELECT COUNT(*) FROM task_events WHERE task_id = ?"
                        " AND kind = 'assigned'", (tid,)).fetchone()[0]
        if h != 2:
            problems.append("%s assigned events %d, expected 2" % (tid, h))
    for tid in ("t2", "t3", "t4", "t5"):
        st, ass = con.execute(
            "SELECT status, assignee FROM tasks WHERE id = ?",
            (tid,)).fetchone()
        if ass != "coder" or st != "ready":
            problems.append("%s touched: status=%s assignee=%s"
                            % (tid, st, ass))
        h = con.execute("SELECT COUNT(*) FROM task_events WHERE task_id = ?"
                        " AND kind = 'assigned'", (tid,)).fetchone()[0]
        if h:
            problems.append("%s got assigned events: %d" % (tid, h))
        wc = con.execute("SELECT COUNT(*) FROM task_comments WHERE"
                         " task_id = ? AND author = 'watchdog'",
                         (tid,)).fetchone()[0]
        if wc:
            problems.append("%s got a watchdog comment: %d" % (tid, wc))
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
        print("OK: t1 (live case: 0 own runs, link from reviewer, "
              "3 ticks) lifted by bounce reassign coder->architect->coder "
              "with a \"Watchdog:\" comment without a URL; t2/t3/t4/t5 stay "
              "guarded; repeat is silent")

    failed = 0
    for name, (old, new) in MUTANTS.items():
        if src.count(new) or src.count(old) != 1:
            print("SKIP: mutant %s changes the anchor" % name)
            failed += 1
            continue
        with tempfile.TemporaryDirectory() as tmp:
            db = make_board(tmp)
            rec = Recorder(db)
            mod = types.ModuleType("bw_apf_%s" % name)
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
