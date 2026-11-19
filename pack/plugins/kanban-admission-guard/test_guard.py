#!/usr/bin/env python3
"""Tests for the admission claim guard. Run: <hermes venv>/bin/python test_guard.py

The scenario: a follow-up card references an issue the owner has not
approved, the platform promotes it to ready, and the dispatcher claims it
before the scheduled admission gate runs. The guard must take it off the
queue at claim time.
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import __init__ as guard  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print("%s %s%s" % ("ok" if cond else "FAIL", name,
                       (" :: " + str(detail)) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


tmp = tempfile.mkdtemp(prefix="admission-guard-test-")
board = "testboard"
bdir = os.path.join(tmp, board)
os.makedirs(bdir)
db = os.path.join(bdir, "kanban.db")
cache_path = os.path.join(bdir, "admission-approved.json")
con = sqlite3.connect(db)
con.executescript("""
CREATE TABLE tasks (id text primary key, title text, body text,
                    status text, priority int);
CREATE TABLE task_comments (task_id text, body text, created_at int);
CREATE TABLE task_links (parent_id text, child_id text);
""")
con.executemany("insert into tasks values (?,?,?,?,?)", [
    ("t_leak", "follow-up for issue #330", "issue #330", "ready", 80),
    ("t_ok", "work on issue #316", "issue #316", "ready", 80),
    ("t_pr", "review PR #321", "conflicts", "ready", 80),
    ("t_run", "work", "issue #330", "running", 80),
    ("t_nolink", "decomposer child without refs", "", "ready", 40),
    ("t_kid", "subtask", "", "ready", 40),
])
con.execute("insert into task_links values ('t_leak','t_kid')")
con.commit()
con.close()


def write_cache(age=0):
    with open(cache_path, "w") as fh:
        json.dump({"updated": time.strftime(
                       "%Y-%m-%d %H:%M:%S", time.localtime(time.time() - age)),
                   "issues": {"316": "next"}, "prs": [321]}, fh)


calls = []


def fake_run(cmd, **kw):
    calls.append(cmd)

    class R:
        returncode = 0
        stdout = "ok"
        stderr = ""
    return R()


guard.RUN = fake_run
guard.BOARDS = tmp
write_cache()


def scheduled(task_id):
    return any(c[4] == "schedule" and c[5] == task_id for c in calls)


guard._on_claimed(task_id="t_leak", board=board)
check("G1 unapproved #330 is taken off the queue at claim", scheduled("t_leak"), calls)

calls.clear()
guard._on_claimed(task_id="t_ok", board=board)
check("G2 approved #316 is untouched", not calls, calls)

guard._on_claimed(task_id="t_pr", board=board)
check("G3 approved PR #321 is untouched", not calls, calls)

guard._on_claimed(task_id="t_run", board=board)
check("G4 running is not preempted", not calls, calls)

guard._on_claimed(task_id="t_nolink", board=board)
check("G5 no references: the guard stays silent", not calls, calls)

con = sqlite3.connect(db)
con.execute("insert into task_comments values ('t_leak','issue-link: #330',0)")
con.commit()
con.close()
calls.clear()
guard._on_claimed(task_id="t_kid", board=board)
check("G6 child of an unapproved root is parked via the ancestor's references",
      scheduled("t_kid"), calls)

write_cache(age=4 * 3600)
calls.clear()
guard._on_claimed(task_id="t_leak", board=board)
check("G7 stale cache: allow", not calls, calls)

os.remove(cache_path)
guard._on_claimed(task_id="t_leak", board=board)
check("G8 no cache: allow", not calls, calls)

guard._on_claimed(task_id="t_leak", board="no-such-board")
check("G9 no board: silent", not calls, calls)

shutil.rmtree(tmp)
print("\n%s" % ("ALL OK" if not FAILS else "FAILURES: %s" % ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
