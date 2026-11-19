#!/usr/bin/env python3
"""worker-api-watchdog.py signature F: orphan claims are reclaimed.

Live case: a card was `running`, its worker pid was killed together with the
gateway by the OOM killer, the heartbeat stopped a minute before. For an hour
and a half nothing picked it up: the dispatcher tick was held
(kanban-dispatch-hold, peak pause), the reclaim phase sits inside the tick
lock, and after owner_admitted + worker_bound the stale-claim reclaim hands
the run to the crash sweep of that same held tick. The CLI `kanban reclaim`
does not depend on the lock.

No real board: an isolated home, subprocess.run replaced, the /proc check
replaced by an ALIVE map (so the test also runs on macOS).

Run: python3 test_worker_api_watchdog_orphan_reclaim.py
"""
from __future__ import annotations

import atexit
import importlib.util
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
_HOME = tempfile.mkdtemp(prefix="wd-home-")
atexit.register(shutil.rmtree, _HOME, ignore_errors=True)
os.environ["HERMES_HOME"] = _HOME
SCRIPT = os.environ.get("WORKER_API_WATCHDOG_SCRIPT",
                        os.path.join(HERE, "worker-api-watchdog.py"))
spec = importlib.util.spec_from_file_location("wd_orphan_%d" % int(time.time()), SCRIPT)
assert spec and spec.loader, "cannot load the watchdog module"
wd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wd)

FAILURES = []
CALLS = []     # every subprocess.run command as strings
BOARDS = []    # HERMES_KANBAN_BOARD of every call
ALIVE = {}     # {pid: bool} replacing _our_worker_alive
FAIL_RC = [0]  # rc returned by the fake reclaim
KEY = "main:t_a:orphan-claim"


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILURES.append(name + (": " + str(detail)[:200] if detail else ""))


class FakeCompleted:
    returncode = 0
    stdout = b""
    stderr = b""


def fake_run(cmd, *a, **kw):
    cmd = [str(x) for x in cmd]
    CALLS.append(cmd)
    BOARDS.append((kw.get("env") or {}).get("HERMES_KANBAN_BOARD"))
    res = FakeCompleted()
    if cmd[:3] == ["hermes", "kanban", "reclaim"]:
        res.returncode = FAIL_RC[0]
    return res


def fake_alive(pid):
    return False if pid is None else ALIVE.get(int(pid), False)


def build_board(rows):
    """rows: [(tid, status, pid, started_age_s, hb_age_s)]; hb_age_s None = NULL."""
    root = Path(tempfile.mkdtemp(prefix="wd-orphan-", dir=_HOME))
    board = root / "kanban" / "boards" / "main"
    board.mkdir(parents=True)
    bdb = board / "kanban.db"
    con = sqlite3.connect(str(bdb))
    con.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, worker_pid INT, "
        "started_at REAL, last_heartbeat_at REAL, current_run_id INT)")
    con.execute("CREATE TABLE task_runs (id INT, task_id TEXT, started_at REAL)")
    now = time.time()
    for tid, status, pid, started_age, hb_age in rows:
        started = now - started_age if started_age is not None else None
        con.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)",
                    (tid, status, pid, started,
                     now - hb_age if hb_age is not None else None, 3685))
        con.execute("INSERT INTO task_runs VALUES (?,?,?)", (3685, tid, started))
    con.commit()
    con.close()
    return root, str(bdb)


def run_orphan(rows, state0=None, alive=None):
    state0 = dict(state0 or {})
    ALIVE.clear(); ALIVE.update(alive or {})
    root, bdb = build_board(rows)
    CALLS.clear(); BOARDS.clear()
    old = (wd.HOME, wd.STATE_FILE, wd.subprocess.run, wd._our_worker_alive)
    wd.HOME, wd.STATE_FILE = root, root / "state.json"
    wd.subprocess.run, wd._our_worker_alive = fake_run, fake_alive
    try:
        return wd.reclaim_orphan_claims(state0, [bdb]), state0
    finally:
        wd.HOME, wd.STATE_FILE, wd.subprocess.run, wd._our_worker_alive = old


def reclaimed(task):
    return any("reclaim" in c and task in c for c in CALLS)


# 1. The live case: dead pid, heartbeat stale for 90 minutes.
texts, st = run_orphan([("t_a", "running", 923880, 5400, 5400)])
check("1: dead pid + stale heartbeat: reclaim", reclaimed("t_a"), CALLS)
check("1b: the CLI call carries the board's HERMES_KANBAN_BOARD",
      BOARDS and BOARDS[0] == "main", BOARDS)
check("1c: the finding names the card and the pid",
      texts and "t_a" in texts[0] and "923880" in texts[0], texts)
check("1d: the cooldown is written to state", st.get(KEY, 0) > 0, st)
check("1e: a comment goes to the card",
      any(c[:3] == ["hermes", "kanban", "comment"] for c in CALLS), CALLS)

# 2. Live worker: left alone even with a stale heartbeat.
texts, st = run_orphan([("t_a", "running", 923880, 5400, 5400)],
                       alive={923880: True})
check("2: a live pid is not reclaimed", not reclaimed("t_a") and not texts,
      (CALLS, texts))

# 3. Fresh heartbeat (5 minutes old): not an orphan.
texts, st = run_orphan([("t_a", "running", 923880, 5400, 300)])
check("3: fresh heartbeat, silence", not reclaimed("t_a") and not texts,
      (CALLS, texts))

# 4. Fresh spawn (started 2 minutes ago, pid not visible yet): grace.
texts, st = run_orphan([("t_a", "running", 923880, 120, None)])
check("4: spawn grace, silence", not reclaimed("t_a") and not texts,
      (CALLS, texts))

# 5. A non-running status is left alone.
texts, st = run_orphan([("t_a", "review", 923880, 5400, 5400)])
check("5: a review card is left alone", not reclaimed("t_a") and not texts,
      (CALLS, texts))

# 6. Reclaim failed (rc=1): a retry finding without cooldown.
FAIL_RC[0] = 1
texts, st = run_orphan([("t_a", "running", 923880, 5400, 5400)])
check("6: failed reclaim: retry text without cooldown",
      reclaimed("t_a") and texts and "failed" in texts[0] and KEY not in st,
      (texts, st))
FAIL_RC[0] = 0

# 7. Cooldown: a recent finding is not repeated before ALERT_COOLDOWN_MIN.
texts, st = run_orphan([("t_a", "running", 923880, 5400, 5400)],
                       state0={KEY: time.time() - 60})
check("7: the cooldown silences a repeat", not reclaimed("t_a") and not texts,
      (CALLS, texts))

# 8. pid NULL (broken bookkeeping) + old start: an orphan without a pid.
texts, st = run_orphan([("t_a", "running", None, 7200, None)])
check("8: pid NULL is an orphan too", reclaimed("t_a") and texts, (CALLS, texts))

print()
if FAILURES:
    print("FAILURES: %d" % len(FAILURES))
    for f in FAILURES:
        print(" - " + f)
    sys.exit(1)
print("ALL PASS")
