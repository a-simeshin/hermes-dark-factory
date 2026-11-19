#!/usr/bin/env python3
"""The self-review loop: a coder reviewing its own delivery.

Live case: kanban_request_review without reviewer= does not reassign the card
(on the first review the prior reviewer is None and the assignee stays), and
the native review dispatch spawns the assigned profile from the review
column. The coder (assignee) picked up its own delivery as the reviewer three
times in a row; no verdict ever came.

Checked:
1. The rework-arm card template of board-watchdog (_rework_arm_card, the
   source of the loop) tells the worker to call kanban_request_review with
   reviewer=reviewer and warns about self-review.
2. Signature D (self_review_loop in worker-api-watchdog.py): a run claimed
   from the review column by the same profile as the implementer of the last
   delivery (reviewer empty or equal to it) is a level-2 finding; a reviewer
   run, a run from another column and a delivery with an explicit reviewer
   are silent.

Hermetic: board databases are temporary files, HERMES_HOME is a temp dir.
Run: python3 test_worker_api_watchdog_self_review.py
Mutants: WORKER_API_WATCHDOG_SCRIPT=/tmp/m1.py BOARD_WATCHDOG_SCRIPT=/tmp/m2.py
"""
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="wd-sr-home-")
os.environ.update({
    "FACTORY_REPO": "acme/widget",
    "FACTORY_REPO_PATH": "/srv/hermes/widget",
    "FACTORY_BOARD": "main",
    "FACTORY_OWNER_LOGIN": "owner-login",
    "FACTORY_OWNER_CHAT": "telegram:000000000",
    "FACTORY_BASE_BRANCHES": "main",
})
FAILURES = []

WW = os.environ.get("WORKER_API_WATCHDOG_SCRIPT") or os.path.join(
    HERE, "worker-api-watchdog.py")
BW = os.environ.get("BOARD_WATCHDOG_SCRIPT") or os.path.join(
    HERE, "board-watchdog.py")


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILURES.append(name)


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ww = load_module(WW, "ww_self_review")
bw = load_module(BW, "bw_self_review")

# --- 1. the _rework_arm_card template must name reviewer=reviewer ----------
captured = {}
_boards_tmp = tempfile.mkdtemp(prefix="wd-sr-boards-")
os.makedirs(os.path.join(_boards_tmp, "main"), exist_ok=True)
orig_boards = bw.BOARDS
bw.BOARDS = _boards_tmp  # type: ignore[attr-defined]
orig_create = bw._kanban_create
bw._kanban_create = (  # type: ignore[attr-defined]
    lambda *a, **k: captured.update(body=a[2]) or "t_test0001")
try:
    bw._rework_arm_card("main", 239, "ff0323b0" + "0" * 32, {"at": 1794805931})
except Exception as exc:  # report, do not crash: part 2 is independent
    print("FAIL _rework_arm_card raised %r" % (exc,))
    FAILURES.append("_rework_arm_card call")
finally:
    bw._kanban_create = orig_create  # type: ignore[attr-defined]
    bw.BOARDS = orig_boards  # type: ignore[attr-defined]
body = captured.get("body", "")
check("template names kanban_request_review", "kanban_request_review" in body)
check("template requires reviewer=reviewer explicitly",
      "reviewer=reviewer" in body)
check("template warns about the self-review loop", "self-review" in body)

# --- 2. signature D: self_review_loop on a replay of the events -------------


def make_db(events):
    fd, path = tempfile.mkstemp(prefix="wd-sr-", suffix=".db")
    os.close(fd)
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " task_id TEXT, run_id INTEGER, kind TEXT, payload TEXT,"
        " created_at REAL)")
    con.executemany(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at)"
        " VALUES (?,?,?,?,?)", events)
    con.commit()
    con.close()
    return path


def claimed(run_id, source, ts):
    return ("t_x", run_id, "claimed",
            json.dumps({"source_status": source}), ts)


def review_req(run_id, implementer, reviewer, ts):
    return ("t_x", run_id, "review_requested",
            json.dumps({"implementer": implementer, "reviewer": reviewer,
                        "summary": "s"}), ts)


# Positive, the live case verbatim: a coder delivery without a reviewer,
# then a coder run from the review column.
db_pos = make_db([
    review_req(3440, "coder", None, 1794805704),
    claimed(3445, "review", 1794805787),
])
run_coder = {"id": 3445, "task_id": "t_x", "profile": "coder",
             "started_at": 1794805787}
res = ww.self_review_loop(run_coder, db_pos)
check("D: coder from the review column after its own delivery = finding",
      bool(res) and res[0] == "self-review")
check("D: the finding names the card and the fix",
      bool(res) and "t_x" in res[1] and "reassign" in res[1])
os.unlink(db_pos)

# Negative 1: the run took the reviewer profile (the fix).
db_rev = make_db([
    review_req(3445, "coder", None, 1794805931),
    claimed(3447, "review", 1794806150),
])
run_reviewer = {"id": 3447, "task_id": "t_x", "profile": "reviewer",
                "started_at": 1794806150}
check("D: reviewer from the review column = silence",
      ww.self_review_loop(run_reviewer, db_rev) is None)
os.unlink(db_rev)

# Negative 2: the run is not from the review column (source_status=ready).
db_ready = make_db([
    review_req(3440, "coder", None, 1794805704),
    claimed(3445, "ready", 1794805787),
])
check("D: run from the ready column = silence",
      ww.self_review_loop(run_coder, db_ready) is None)
os.unlink(db_ready)

# Negative 3: the delivery named reviewer=reviewer explicitly: even if the
# coder took the card, the reviewer in the event is set, this is not the
# nameless-reviewer loop.
db_explicit = make_db([
    review_req(3440, "coder", "reviewer", 1794805704),
    claimed(3445, "review", 1794805787),
])
check("D: delivery with an explicit reviewer=reviewer = silence",
      ww.self_review_loop(run_coder, db_explicit) is None)
os.unlink(db_explicit)

TOTAL = 8
print()
if FAILURES:
    print("%d FAIL: %s" % (len(FAILURES), FAILURES))
    sys.exit(1)
print("%d/%d green" % (TOTAL, TOTAL))
