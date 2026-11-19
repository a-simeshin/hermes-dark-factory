#!/usr/bin/env python3
"""A card with a delivered PR is not sent back to triage and not decomposed.

Work that continues after a delivered PR goes into a new card for the
remainder, with a body checked against the PR. The fixture replays a real
sequence: a checkpoint comment that delivers PR #376, a second transient
block, block_loop_detected, then the auto-decomposer splits the stale body
into four children. Three phases of the board:

  A: the root is in triage after block_loop and the PR is already
     delivered -> a "prloop" finding (before the auto_decompose tick);
  B: decomposed + four live children -> a "decpr" finding, prloop is silent
     (the root is no longer in triage);
  C: sorted out (root done, children archived) -> the gate is silent forever.

Negatives: a block loop without a PR link (t_noref), a decomposition after
changes_requested (t_verdict, the legitimate rework path), a PR link that
appeared AFTER decomposed (t_late).

Mutants (each must turn the test red):
  M_LOOP    the "after block_loop" precondition is off -> t_verdict shouts;
  M_REF     the PR #N link regex accepts any number -> t_noref shouts;
  M_SILENCE the "already sorted out" suppression is off -> phase C shouts.

Run: python3 test_board_watchdog_decomposed_after_delivery.py
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

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
BW_PATH = os.environ.get("BOARD_WATCHDOG_SCRIPT") or os.path.join(
    SCRIPTS, "board-watchdog.py")
SLUG = "decprfix"
NOW = int(time.time())

# Event times of the replayed case, shifted to NOW.
T0 = NOW - 6 * 3600            # the card is created
T_PR = NOW - 3600 * 0.5 - 400  # checkpoint "delivered PR #376"
T_LOOP = T_PR + 30             # block_loop_detected
T_DEC = T_LOOP + 190           # decomposed

KIDS = ["t_68d1470a", "t_6178a87a", "t_5234f3ce", "t_4945484e"]

BODY_ROOT = ("Mirror of issue #374 (active milestone, opened by the owner). "
             "A push run failed in three unrelated jobs, the rerun is green.")

CHECKPOINT = ("Run 2: delivered PR #376 (fix/374-flaky-tests, heads 947aa8c7 "
              "and 857128f4): SSE buffer race and /config URL overwrite, "
              "CI 5/5, merge is up to the owner. Remainder: AuthOwnerScopeIT.")


def make_board(root):
    bdir = os.path.join(root, "boards", SLUG)
    os.makedirs(bdir, exist_ok=True)
    con = sqlite3.connect(os.path.join(bdir, "kanban.db"))
    con.execute("""CREATE TABLE tasks (
        id TEXT PRIMARY KEY, title TEXT, body TEXT, assignee TEXT,
        status TEXT NOT NULL, priority INTEGER DEFAULT 0,
        created_by TEXT, created_at INTEGER NOT NULL,
        started_at INTEGER, completed_at INTEGER)""")
    con.execute("""CREATE TABLE task_comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
        author TEXT NOT NULL, body TEXT NOT NULL,
        created_at INTEGER NOT NULL)""")
    con.execute("""CREATE TABLE task_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
        run_id TEXT, kind TEXT NOT NULL, payload TEXT,
        created_at INTEGER NOT NULL)""")
    con.execute("""CREATE TABLE task_links (
        parent_id TEXT NOT NULL, child_id TEXT NOT NULL)""")

    def task(tid, body, status, created_by="user", ts=T0):
        con.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,NULL,NULL)",
                    (tid, "card " + tid, body, "coder", status, 80,
                     created_by, ts))

    def event(tid, kind, ts, payload=None):
        con.execute("INSERT INTO task_events (task_id, run_id, kind, payload,"
                    " created_at) VALUES (?,?,?,?,?)",
                    (tid, None, kind,
                     json.dumps(payload) if payload else "", int(ts)))

    def comment(tid, body, ts):
        con.execute("INSERT INTO task_comments (task_id, author, body,"
                    " created_at) VALUES (?,?,?,?)",
                    (tid, "coder", body, int(ts)))

    # The root of the replayed case.
    task("t_root", BODY_ROOT, "triage")
    event("t_root", "created", T0)
    event("t_root", "blocked", T0 + 100)
    event("t_root", "unblocked", T0 + 130)
    event("t_root", "blocked", T_LOOP - 10)
    event("t_root", "block_loop_detected", T_LOOP)
    comment("t_root", CHECKPOINT, T_PR)

    # Negative 1: a block loop without a PR link.
    task("t_noref", "A clean card without a PR, issue #374 is mentioned",
         "todo")
    event("t_noref", "created", T0)
    event("t_noref", "blocked", T0 + 50)
    event("t_noref", "blocked", T0 + 80)
    event("t_noref", "block_loop_detected", T0 + 90)
    event("t_noref", "decomposed", T0 + 95,
          {"child_ids": ["t_noref_k1"], "root_assignee": "architect"})
    task("t_noref_k1", "child", "ready", created_by="auto-decomposer",
         ts=T0 + 95)

    # Negative 2: decomposition after changes_requested (legitimate rework).
    task("t_verdict", "Rework by the verdict on PR #376", "todo")
    event("t_verdict", "created", T0)
    event("t_verdict", "review_requested", T0 + 40)
    event("t_verdict", "changes_requested", T0 + 60)
    event("t_verdict", "decomposed", T0 + 95,
          {"child_ids": ["t_verdict_k1"], "root_assignee": "architect"})
    comment("t_verdict", "Verdict on PR #376: two blockers", T0 + 50)
    task("t_verdict_k1", "child", "ready", created_by="auto-decomposer",
         ts=T0 + 95)

    # Negative 3: the PR link appeared AFTER decomposed.
    task("t_late", "A card with nothing delivered at decomposition time",
         "todo")
    event("t_late", "created", T0)
    event("t_late", "blocked", T0 + 50)
    event("t_late", "block_loop_detected", T0 + 90)
    event("t_late", "decomposed", T0 + 95,
          {"child_ids": ["t_late_k1"], "root_assignee": "architect"})
    comment("t_late", "PR #999 mentioned later", T_DEC + 600)
    task("t_late_k1", "child", "ready", created_by="auto-decomposer",
         ts=T0 + 95)

    con.commit()
    con.close()
    return bdir


def load_bw(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def prepare(root, src=BW_PATH, name="bw_decpr"):
    bdir = make_board(root)
    mod = load_bw(src, name)
    mod.BOARDS = os.path.join(root, "boards")
    return mod, bdir


def phase_b(con):
    """decomposed by the stale body, the root went back to todo."""
    con.execute("UPDATE tasks SET status='todo' WHERE id='t_root'")
    con.execute("INSERT INTO task_events (task_id, run_id, kind, payload,"
                " created_at) VALUES (?,?,?,?,?)",
                ("t_root", None, "decomposed",
                 json.dumps({"child_ids": KIDS, "root_assignee": "architect"}),
                 int(T_DEC)))
    for kid in KIDS:
        con.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,NULL,NULL)",
                    (kid, "child " + kid, "child body", "coder",
                     "ready" if kid == KIDS[0] else "todo", 0,
                     "auto-decomposer", int(T_DEC)))
        con.execute("INSERT INTO task_links VALUES (?,?)", ("t_root", kid))
    con.commit()


def keys(findings):
    return sorted(f[0] for f in findings)


def main():
    failures = []
    checks = 0
    root = tempfile.mkdtemp(prefix="decpr-")
    try:
        mod, bdir = prepare(os.path.join(root, "main"))
        db = os.path.join(bdir, "kanban.db")

        # Phase A: triage after block_loop with a delivered PR -> prloop.
        found = mod.decomposed_after_delivery(SLUG)
        checks += 1
        if keys(found) != ["prloop:%s:t_root" % SLUG]:
            failures.append("phase A: keys=%s, expected prloop" % keys(found))
        checks += 1
        if "PR #376" not in (found and found[0][2] or ""):
            failures.append("phase A: the text has no PR #376")

        # Phase B: decomposed + live children -> decpr, prloop silent.
        con = sqlite3.connect(db)
        phase_b(con)
        con.close()
        found = mod.decomposed_after_delivery(SLUG)
        checks += 1
        if keys(found) != ["decpr:%s:t_root" % SLUG]:
            failures.append("phase B: keys=%s, expected decpr" % keys(found))
        checks += 1
        text = found and found[0][2] or ""
        if not all(kid in text for kid in KIDS):
            failures.append("phase B: not all live children are listed")

        # Phase C: sorted out -> the gate is silent.
        con = sqlite3.connect(db)
        con.execute("UPDATE tasks SET status='done' WHERE id='t_root'")
        con.execute("UPDATE tasks SET status='archived' WHERE id IN (%s)"
                    % ",".join("?" * len(KIDS)), KIDS)
        con.commit()
        con.close()
        found = mod.decomposed_after_delivery(SLUG)
        checks += 1
        if found:
            failures.append("phase C: the gate shouts after sorting out: %s"
                            % keys(found))

        # Mutants: in-process copies of the script with one change each.
        src = open(BW_PATH).read()
        mutants = [
            ("M_LOOP",
             "        routing = _last_routing_before(db, task_id, ts)\n"
             "        if not routing or routing[0] != \"block_loop_detected\":",
             "        routing = _last_routing_before(db, task_id, ts)\n"
             "        if False:",
             "t_verdict (legitimate rework) does not shout"),
            ("M_REF",
             "_PR_REF_RE = re.compile(r\"PR #(\\d+)\")",
             "_PR_REF_RE = re.compile(r\"(\\d+)\")",
             "t_noref (no delivered PR) does not shout"),
            ("M_SILENCE",
             "        if statuses.get(task_id) in (\"done\", \"archived\")"
             " and not live:\n"
             "            continue",
             "        if False:\n            continue",
             "phase C is silent after sorting out"),
        ]
        for name, old, new, why in mutants:
            if old not in src:
                failures.append("%s: mutant anchor not found" % name)
                continue
            # The copy lives next to the original so it finds its helpers.
            mdir = os.path.join(root, "mut_%s" % name)
            os.makedirs(mdir)
            for helper in ("factory_config.py", "milestone_bands.py",
                           "board_native_guard.py", "metrics-evening.py"):
                hp = os.path.join(SCRIPTS, helper)
                if os.path.exists(hp):
                    shutil.copy(hp, mdir)
            mpath = os.path.join(mdir, "board-watchdog.py")
            open(mpath, "w").write(src.replace(old, new, 1))
            mroot = os.path.join(root, "m_%s" % name)
            mod_m, bdir_m = prepare(mroot, mpath, "bw_decpr_%s" % name)
            con_m = sqlite3.connect(os.path.join(bdir_m, "kanban.db"))
            phase_b(con_m)
            con_m.execute("UPDATE tasks SET status='done'"
                          " WHERE id='t_root'")
            con_m.execute("UPDATE tasks SET status='archived'"
                          " WHERE id IN (%s)" % ",".join("?" * len(KIDS)),
                          KIDS)
            con_m.commit()
            con_m.close()
            got = keys(mod_m.decomposed_after_delivery(SLUG))
            checks += 1
            if not got:
                failures.append("%s: mutant survived (%s)" % (name, why))
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(os.environ["HERMES_HOME"], ignore_errors=True)

    for f in failures:
        print("FAIL %s" % f)
    print("%d/%d green" % (checks - len(failures), checks))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
