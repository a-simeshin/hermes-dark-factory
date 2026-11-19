#!/usr/bin/env python3
"""An eternal initial_status block on the children of an accepted plan.

Why: an architect created the children of a decomposition with
`create --initial-status blocked` + `link <root> <child>` and handed the plan
to the stock review lane (kanban_request_review). The reviewer closed the
root as ACCEPTED, but the children stayed blocked. The cause is in the core:
recompute_ready treats any blocked status as sticky (the last event of kind
'blocked' means "only an explicit kanban unblock"), an event with
reason='initial_status' satisfies that, so the child is never lifted. The
plan review gate does not lead roots of the stock review lane.

Rule (initial_status_unstick_gate): a card in blocked whose last
blocked/unblocked event carries reason='initial_status', that has parents
by links and ALL of them terminal (done/archived) -> unblock, the core
re-gates by parents (ready). An open parent is not touched (a stock wait
through a link), a card without parents is not lifted (finding only), a
foreign reason is not touched, a repeated pass is silent.

The gate must:
  c1   child of a closed root -> unblock + one "Watchdog:" comment;
  c2   child of an OPEN predecessor (chain C1 -> C2) -> NOT touched (waits
       through a stock link);
  t_op operator block (another reason) under a closed root -> NOT touched;
  t_np initial_status block WITHOUT parents -> only a finding in the
       report, no action;
  t_ar child of an archived root -> unblock (archive is terminal);
  a repeated pass over the unblocked c1 -> silence (idempotency);
  dry-run -> findings without a single CLI call.

Mutants turn it red: losing the reason check (t_op is unblocked), losing
the re-gate by open parents (c2 goes), losing the "no parents" branch (t_np
is unblocked), a replaced unblock (c1 stays blocked), terminal statuses
narrowed to done (t_ar is not lifted).

Run: python3 test_board_watchdog_initial_status_unstick.py
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import types

# Settings and Hermes home are fixed before the script is loaded.
_HOME = tempfile.mkdtemp(prefix="wd-home-")
os.environ["HERMES_HOME"] = _HOME
os.environ.update({
    "FACTORY_REPO": "acme/widget",
    "FACTORY_REPO_PATH": "/srv/hermes/widget",
    "FACTORY_BOARD": "main",
    "FACTORY_OWNER_LOGIN": "owner-login",
    "FACTORY_OWNER_CHAT": "telegram:000000000",
    "FACTORY_BASE_BRANCHES": "main",
})

BW_PATH = os.environ.get("BOARD_WATCHDOG_SCRIPT") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "board-watchdog.py")
SLUG = "unstickfix"
NOW = int(time.time())

MUTANTS = {
    "M_REASON": (
        "            if payload.get(\"reason\") != \"initial_status\":\n"
        "                continue          # an operator/work block: not ours",
        "            if payload.get(\"reason\") != \"xx_never\":\n"
        "                continue          # an operator/work block: not ours"),
    "M_OPEN_PARENT": (
        "            if any(p[0] not in (\"done\", \"archived\") for p in parents):\n"
        "                continue          # waits for a parent through a stock link",
        "            if False:\n"
        "                continue          # waits for a parent through a stock link"),
    "M_NO_PARENT": (
        "            if not parents:\n"
        "                findings.append((key, 1,\n"
        "                                 \"    Eternal initial_status block without \"",
        "            if False:\n"
        "                findings.append((key, 1,\n"
        "                                 \"    Eternal initial_status block without \""),
    "M_UNBLOCK": (
        "            run([HERMES, \"kanban\", \"--board\", slug,\n"
        "                 \"unblock\", tid], timeout=90)",
        "            run([HERMES, \"kanban\", \"--board\", slug,\n"
        "                 \"show\", tid], timeout=90)"),
    "M_TERMINAL": (
        "            if any(p[0] not in (\"done\", \"archived\") for p in parents):\n"
        "                continue          # waits for a parent through a stock link",
        "            if any(p[0] not in (\"done\",) for p in parents):\n"
        "                continue          # waits for a parent through a stock link"),
}


class Recorder(object):
    """run() of the gate: unblock is applied to the fixture database like
    the live one (status ready + an 'unblocked' event), comment writes a row
    to task_comments with author watchdog."""

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


def _mk_task(con, tid, title, status, assignee="coder"):
    con.execute(
        "INSERT INTO tasks (id, title, assignee, status, priority,"
        " claim_lock, created_at) VALUES (?, ?, ?, ?, 50, NULL, ?)",
        (tid, title, assignee, status, NOW - 7200))


def _block_event(con, tid, reason, kind="blocked"):
    con.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload,"
        " created_at) VALUES (?, NULL, ?, ?, ?)",
        (tid, kind, json.dumps({"reason": reason}), NOW - 3600))


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
        CREATE TABLE task_links (parent_id TEXT, child_id TEXT);
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
            run_id INTEGER, kind TEXT, payload TEXT, created_at INTEGER);
        CREATE TABLE task_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
            author TEXT, body TEXT, created_at INTEGER);
    """)
    # The plan root is done (ACCEPTED in the stock lane), a chain of
    # children C1 -> C2 created with --initial-status blocked.
    _mk_task(con, "t_root", "issue #120 plan (ACCEPTED)", "done", "architect")
    _mk_task(con, "c1", "issue #120 C1", "blocked")
    _block_event(con, "c1", "initial_status")
    _mk_task(con, "c2", "issue #120 C2", "blocked")
    _block_event(con, "c2", "initial_status")
    con.executemany("INSERT INTO task_links VALUES (?,?)", [
        ("t_root", "c1"), ("c1", "c2")])
    # An operator block under a closed root: not ours.
    _mk_task(con, "t_op", "the owner decided to wait", "blocked")
    _block_event(con, "t_op", "awaiting owner decision on PR #400")
    con.execute("INSERT INTO task_links VALUES ('t_root', 't_op')")
    # An initial_status block without parents: finding only.
    _mk_task(con, "t_np", "waits for an external PR", "blocked")
    _block_event(con, "t_np", "initial_status")
    # A parent in archived is terminal: the child is lifted.
    _mk_task(con, "t_ar_root", "old root", "archived", "architect")
    _mk_task(con, "t_ar", "tail of an archived root", "blocked")
    _block_event(con, "t_ar", "initial_status")
    con.execute("INSERT INTO task_links VALUES ('t_ar_root', 't_ar')")
    con.commit()
    con.close()
    return db


def load_module(root, src=None, tag="bw_unstick"):
    mod = types.ModuleType(tag)
    mod.__file__ = BW_PATH
    code = src if src is not None else open(BW_PATH, encoding="utf-8").read()
    exec(compile(code, BW_PATH, "exec"), mod.__dict__)
    mod.BOARDS = os.path.join(root, "boards")
    mod.PROFILES_ROOT = os.path.join(root, "profiles")
    return mod


def side_calls(rec):
    return [c for c in rec.calls if c[0].endswith("/hermes")]


def unblock_calls(rec):
    return sorted(c[c.index("unblock") + 1] for c in rec.calls
                  if "unblock" in c)


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
                     " AND body LIKE 'Watchdog%'", (tid,)).fetchone()[0]
    un = con.execute("SELECT COUNT(*) FROM task_events WHERE task_id = ?"
                     " AND kind = 'unblocked'", (tid,)).fetchone()[0]
    con.close()
    return st, wc, un


def full_run(mod, rec, db):
    problems = []

    rec.calls = []
    findings = mod.initial_status_unstick_gate(SLUG, apply=False)
    if _keys(findings) != ["c1", "t_ar", "t_np"]:
        problems.append("dry-run findings %s, expected [c1, t_ar, t_np]"
                        % _keys(findings))
    if side_calls(rec):
        problems.append("dry-run touches the world: %s" % side_calls(rec))

    rec.calls = []
    findings = mod.initial_status_unstick_gate(SLUG)
    if _keys(findings) != ["c1", "t_ar", "t_np"]:
        problems.append("apply: findings %s, expected [c1, t_ar, t_np]"
                        % _keys(findings))
    ub = unblock_calls(rec)
    if ub != ["c1", "t_ar"]:
        problems.append("unblock is not [c1, t_ar]: %s" % ub)
    cc = comment_calls(rec)
    if len(cc) != 2 or sorted(x[0] for x in cc) != ["c1", "t_ar"]:
        problems.append("comments are not on c1/t_ar: %s" % cc)
    for tid, body in cc:
        if not body.startswith("Watchdog:"):
            problems.append("comment on %s has no \"Watchdog:\" prefix" % tid)
    for f in findings:
        if f[0].endswith(":c1") or f[0].endswith(":t_ar"):
            if "Unblocked" not in f[2]:
                problems.append("the action report has no \"Unblocked\": %s"
                                % f[0])
        if f[0].endswith(":t_np"):
            if "needs_input" not in f[2]:
                problems.append("the t_np finding has no path (needs_input)")

    rec.calls = []
    findings2 = mod.initial_status_unstick_gate(SLUG)
    # t_np (no parents) is a report-only alert: it repeats every pass until
    # a human moves it to needs_input or closes it; there must be no action.
    if _keys(findings2) != ["t_np"] or side_calls(rec):
        problems.append("repeated pass: findings %s / actions %s"
                        % (_keys(findings2), side_calls(rec)))

    # c1 lifted, c2 untouched (parent c1 alive), t_op/t_np untouched.
    for tid, want in (("c1", "ready"), ("t_ar", "ready"),
                      ("c2", "blocked"), ("t_op", "blocked"),
                      ("t_np", "blocked")):
        st, wc, un = _card_state(db, tid)
        exp_un = 1 if want == "ready" else 0
        exp_wc = 1 if want == "ready" else 0
        if st != want or wc != exp_wc or un != exp_un:
            problems.append("%s: status=%s watchdog comments=%d"
                            " unblocked=%d (expected %s/%d/%d)"
                            % (tid, st, wc, un, want, exp_wc, exp_un))
    return problems


def main():
    src = open(BW_PATH, encoding="utf-8").read()
    for name, (old, _new) in MUTANTS.items():
        if src.count(old) != 1:
            print("FAIL: mutant anchor %s occurs %d times"
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
        print("OK: c1 lifted (root done, ACCEPTED), c2 waits for its open "
              "parent, operator and parentless blocks untouched, an "
              "archived root lifts its tail, repeat and dry-run are silent")

    failed = 0
    for name, (old, new) in MUTANTS.items():
        if src.count(new) or src.count(old) != 1:
            print("SKIP: mutant %s changes the anchor" % name)
            failed += 1
            continue
        with tempfile.TemporaryDirectory() as tmp:
            db = make_board(tmp)
            rec = Recorder(db)
            mod = load_module(tmp, src.replace(old, new, 1),
                              tag="bw_unstick_%s" % name)
            mod.run = rec
            try:
                problems = full_run(mod, rec, db)
            except Exception as exc:
                problems = ["crashed: %s: %s" % (type(exc).__name__, exc)]
            if problems:
                print("OK  %s: mutant caught (%s)" % (name, problems[0][:96]))
            else:
                print("FAIL %s: mutant survived, the test is blind to this "
                      "branch" % name)
                failed += 1
    if failed:
        print("TOTAL: FAIL, problems %d" % failed)
        return 1
    print("TOTAL: OK")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(_HOME, ignore_errors=True)
