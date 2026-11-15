#!/usr/bin/env python3
"""Rule 3 of the backlog gate does not archive cards with live links.

Reason for the test: backlog_p0_stale used to count any p0 card in
todo/ready/triage without runs or status events for 14 days as stale, without
looking at task_links, and archived chain roots and their QA/review cards
while their implementing children were still alive.

Rule: a card with a parent or a child whose status is not done/archived
(a scheduled parking by an owner gate included) is not stale. A lone card
without live links is archived as before.

Run: python3 test_board_watchdog_p0_stale_links.py
Red on the old code: copy the old board-watchdog.py next to the other scripts
and run BOARD_WATCHDOG_SCRIPT=<copy> python3 test_board_watchdog_p0_stale_links.py
(expected: FAILED).
"""
import importlib.util
import json
import os
import re
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
SCRIPT = os.environ.get("BOARD_WATCHDOG_SCRIPT",
                        os.path.join(HERE, "board-watchdog.py"))
SLUG = "main"
D = 24 * 3600            # one day
STALE = 14 * D           # window of rule 3
FAILED = []


def check(name, cond, detail=""):
    if cond:
        print("  ok %s" % name)
    else:
        FAILED.append(name)
        print("  FAIL %s %s" % (name, detail))


def load_mod():
    spec = importlib.util.spec_from_file_location("wd_p0_links_test", SCRIPT)
    assert spec is not None and spec.loader is not None, SCRIPT
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_board(tmp, tasks=(), runs=(), events=(), links=()):
    os.makedirs(os.path.join(tmp, SLUG), exist_ok=True)
    db = os.path.join(tmp, SLUG, "kanban.db")
    con = sqlite3.connect(db)
    con.executescript("""
      create table tasks (id text primary key, title text default '',
        body text default '', assignee text default 'coder',
        status text default 'todo', priority int default 0,
        created_at int default 0, created_by text default 'agent',
        idempotency_key text);
      create table task_runs (id integer primary key autoincrement,
        task_id text, started_at int);
      create table task_events (id integer primary key autoincrement,
        task_id text, kind text, created_at int);
      create table task_comments (id integer primary key autoincrement,
        task_id text, author text, body text, created_at int);
      create table task_links (parent_id text not null,
        child_id text not null, primary key (parent_id, child_id));
    """)
    con.executemany(
        "insert into tasks (id,title,body,assignee,status,priority,"
        "created_at,created_by) values (?,?,?,?,?,?,?,?)", tasks)
    con.executemany("insert into task_runs (task_id,started_at) values (?,?)",
                    runs)
    con.executemany(
        "insert into task_events (task_id,kind,created_at) values (?,?,?)",
        events)
    con.executemany("insert into task_links (parent_id,child_id) values (?,?)",
                    links)
    con.commit()
    con.close()
    return db


class Env:
    """Patches the module to a temporary board; the CLI stub archives."""

    def __init__(self, mod, tasks=(), runs=(), events=(), links=()):
        self.mod = mod
        self.tmp = tempfile.mkdtemp(prefix="wd-p0-links-")
        self.db = make_board(self.tmp, tasks, runs, events, links)
        self.runs = []
        self._old = (mod.BOARDS, mod.run, mod.BACKLOG_HOLD,
                     mod.BACKLOG_CACHE, mod.backlog_fetch_linkage)
        mod.BOARDS = self.tmp
        mod.BACKLOG_HOLD = os.path.join(self.tmp, "hold")
        mod.BACKLOG_CACHE = os.path.join(self.tmp, "linkage.json")
        with open(mod.BACKLOG_CACHE, "w") as fh:
            json.dump({"ts": time.time(), "ms_titles": {7: "2.3.0"},
                       "ms_issues": {}, "open_pr_refs": {},
                       "direct_ms_prs": [], "owner_review_prs": []}, fh)
        mod.backlog_fetch_linkage = self._no_fetch
        mod.run = self._fake_run

    def _no_fetch(self):
        raise AssertionError("gh must not be called: the cache is fresh")

    def _fake_run(self, cmd, timeout=None):
        self.runs.append(cmd)
        verb = cmd[4] if len(cmd) > 4 else ""
        tid = cmd[5] if len(cmd) > 5 else ""
        con = sqlite3.connect(self.db)
        if verb == "archive":
            con.execute("update tasks set status='archived' where id=?",
                        (tid,))
        con.commit()
        con.close()
        return ""

    def statuses(self):
        con = sqlite3.connect(self.db)
        rows = dict(con.execute("select id,status from tasks"))
        con.close()
        return rows

    def restore(self):
        (self.mod.BOARDS, self.mod.run, self.mod.BACKLOG_HOLD,
         self.mod.BACKLOG_CACHE,
         self.mod.backlog_fetch_linkage) = self._old


def main():
    now = time.time()
    mod = load_mod()
    print("module: %s" % SCRIPT)

    print("[1] a chain with a live parent: not stale")
    env = Env(mod, tasks=[
        ("t_parent", "live parent", "", "coder", "scheduled", 0,
         now - 20 * D, "agent"),
        ("t_child", "waits for the parent for 16 days", "", "coder", "todo",
         0, now - 20 * D, "agent"),
        ("t_lone", "lone stale card", "", "coder", "todo", 0,
         now - 20 * D, "agent"),
    ], links=[("t_parent", "t_child")])
    ids = {t for t, _ in mod.backlog_p0_stale(SLUG, now)}
    check("a live parent (scheduled) saves the card",
          "t_child" not in ids, str(ids))
    check("the lone stale card is in the list", "t_lone" in ids, str(ids))
    env.restore()

    print("[2] a live child saves the root")
    env = Env(mod, tasks=[
        ("t_root", "root waits for the implementation", "", "coder", "todo",
         0, now - 20 * D, "agent"),
        ("t_impl", "implementation scheduled", "", "coder", "scheduled", 5,
         now - 20 * D, "agent"),
        ("t_root2", "root with a done child", "", "coder", "todo", 0,
         now - 20 * D, "agent"),
        ("t_done_kid", "done child", "", "coder", "done", 5,
         now - 20 * D, "agent"),
    ], links=[("t_root", "t_impl"), ("t_root2", "t_done_kid")])
    ids = {t for t, _ in mod.backlog_p0_stale(SLUG, now)}
    check("a live child (scheduled) saves the root",
          "t_root" not in ids, str(ids))
    check("a done child does not save", "t_root2" in ids, str(ids))
    env.restore()

    print("[3] a parent parked by the gate (scheduled) saves the card")
    env = Env(mod, tasks=[
        ("t_parked", "parked by the owner gate", "", "coder",
         "scheduled", 0, now - 20 * D, "agent"),
        ("t_waiter", "waits for the owner decision", "", "coder", "todo", 0,
         now - 20 * D, "agent"),
    ], links=[("t_parked", "t_waiter")])
    ids = {t for t, _ in mod.backlog_p0_stale(SLUG, now)}
    check("a card in a parking chain is untouched",
          "t_waiter" not in ids, str(ids))
    env.restore()

    print("[4] the whole gate: the chain lives, the lone card is archived")
    env = Env(mod, tasks=[
        ("t_parent", "live parent", "", "coder", "scheduled", 0,
         now - 20 * D, "agent"),
        ("t_child", "waits for the parent", "", "coder", "todo", 0,
         now - 20 * D, "agent"),
        ("t_lone", "lone stale card", "", "coder", "todo", 0,
         now - 20 * D, "agent"),
    ], links=[("t_parent", "t_child")])
    st = {}
    mod.backlog_gate(SLUG, st, now=now, ignore_hold=True)
    stt = env.statuses()
    check("the card waiting for a parent is not archived",
          stt.get("t_child") == "todo", str(stt))
    check("the lone card is archived", stt.get("t_lone") == "archived",
          str(stt))
    env.restore()

    if FAILED:
        print("FAILED: %s" % ", ".join(FAILED))
        sys.exit(1)
    print("P0 STALE LINKS: all tests green")


if __name__ == "__main__":
    main()
