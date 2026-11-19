#!/usr/bin/env python3
"""Signature E of worker-api-watchdog.py: a duplicate review of one PR head.

Timeline of the live incident: the watchdog armed a reviewer card for a PR
head (run A started), 12 minutes later the implementer delivered with
kanban_request_review and the native review lane started a second reviewer of
the same head (run B): two review sessions of one head in parallel. The board
watchdog already refuses to arm a review when the native one is running; this
covers the reverse order: the arm is running and the implementer asks for the
native review.

Rule: one head is reviewed by ONE session, the earlier one. The duplicate
(review lane on the implementer's card while a live review run of an armed
card for the same PR and head exists, started earlier, fresh heartbeat) is
put out: reclaim of the late run, the implementer's card on the non-profile
rev-hold (the review lane does not spawn non-profiles). The return is native:
a verdict of the armed family puts the card back on reviewer.

Why the fix is on the implementer side: over 7 days a watchdog gate "do not
arm a head with a live implementer" would have suppressed 8 of 84 arms, and in
6 of those 8 the verdict came from the arm (the implementer never requested a
review). Suppression would have cost 6 verdicts to save 2 duplicates.

  t1  the live timeline: duplicate caught, reclaim + assign rev-hold;
  t2  the arm's heartbeat is dead: the duplicate is NOT put out (it may be
      the only review);
  t3  the arm started LATER than the duplicate: not put out;
  t4  a run of a non-reviewer profile: no signature;
  t5  the run's card is itself an arm (family key): no signature;
  t6  the head is not named in review_requested: no signature;
  t7  restore: a verdict of the armed family on the head returns the card to
      reviewer;
  t8  restore without a verdict marker in the summary: the card stays parked;
  t9  evaluate(force_alive=True) (selftest) takes no action.

Run: python3 test_worker_api_watchdog_duplicate_review.py
"""
import atexit
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
_HOME = tempfile.mkdtemp(prefix="wd-home-")
atexit.register(shutil.rmtree, _HOME, ignore_errors=True)
os.environ["HERMES_HOME"] = _HOME
SCRIPT = os.environ.get("WORKER_API_WATCHDOG_SCRIPT",
                        os.path.join(HERE, "worker-api-watchdog.py"))
spec = importlib.util.spec_from_file_location("wd_dup_%d" % int(time.time()), SCRIPT)
assert spec and spec.loader, "cannot load the watchdog module"
wd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wd)

FAILURES = []
HEAD = "626b46a4912e038107d68f34249e8db515b64bae"
H7 = HEAD[:7]


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILURES.append(name)


def make_board():
    db = os.path.join(tempfile.mkdtemp(prefix="wd-dup-", dir=_HOME), "kanban.db")
    con = sqlite3.connect(db)
    con.executescript("""
        create table tasks (id text primary key, assignee text,
            status text, idempotency_key text);
        create table task_runs (id integer primary key, task_id text,
            profile text, status text, started_at real,
            last_heartbeat_at real, ended_at real, summary text);
        create table task_events (id integer primary key,
            task_id text, run_id int, kind text, payload text,
            created_at real);
    """)
    con.commit()
    con.close()
    return db


def seed_live_incident(db, *, hb_arm=None, arm_started=None,
                       dup_started=None, dup_task_key=None,
                       req_head=True, arm_pr=384):
    """The live timeline: arm run 3530 earlier, duplicate run 3531 later."""
    now = time.time()
    arm_started = 1794888257 if arm_started is None else arm_started
    dup_started = 1794888988 if dup_started is None else dup_started
    hb_arm = (now - 60) if hb_arm is None else hb_arm
    con = sqlite3.connect(db)
    con.execute("insert into tasks values (?,?,?,?)",
                ("t_arm", "reviewer", "running",
                 "reviewer-pr%d-%s" % (arm_pr, H7)))
    con.execute("insert into tasks values (?,?,?,?)",
                ("t_impl", "reviewer", "running", dup_task_key))
    con.execute("insert into task_runs values (?,?,?,?,?,?,?,?)",
                (3530, "t_arm", "reviewer", "running", arm_started,
                 hb_arm, None, None))
    con.execute("insert into task_runs values (?,?,?,?,?,?,?,?)",
                (3531, "t_impl", "reviewer", "running", dup_started,
                 now - 30, None, None))
    payload = json.dumps({"summary": "opened PR #%d (head %s)"
                          % (arm_pr, HEAD if req_head else "")})
    con.execute("insert into task_events (task_id, kind, payload,"
                " created_at) values (?,?,?,?)",
                ("t_impl", "review_requested", payload, dup_started - 60))
    con.execute("insert into task_events (task_id, run_id, kind, payload,"
                " created_at) values (?,?,?,?,?)",
                ("t_impl", 3531, "claimed",
                 json.dumps({"source_status": "review"}), dup_started))
    con.commit()
    con.close()
    return {"id": 3531, "task_id": "t_impl", "profile": "reviewer",
            "worker_pid": None, "started_at": dup_started,
            "last_heartbeat_at": now - 30, "board": "testboard"}


def with_actions(fn):
    calls = []

    class R:  # noqa: N801
        returncode = 0
        stdout = ""

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return R()

    real = wd.subprocess.run
    wd.subprocess.run = fake_run
    try:
        res = fn()
    finally:
        wd.subprocess.run = real
    return res, calls


def seed_parked(db, summary):
    now = time.time()
    con = sqlite3.connect(db)
    con.execute("insert into tasks values (?,?,?,?)",
                ("t_parked", wd.REVIEW_HOLD_PROFILE, "review", "issue-383-x"))
    con.execute("insert into tasks values (?,?,?,?)",
                ("t_arm", "reviewer", "done", "reviewer-pr384-%s" % H7))
    con.execute("insert into task_runs values (?,?,?,?,?,?,?,?)",
                (3530, "t_arm", "reviewer", "ended", now - 7200,
                 None, now - 6000, summary))
    con.execute("insert into task_events (task_id, kind, payload,"
                " created_at) values (?,?,?,?)",
                ("t_parked", "review_requested",
                 json.dumps({"summary": "PR #384 head %s" % HEAD}),
                 now - 3000))
    con.commit()
    con.close()


def main():
    db = make_board()
    run = seed_live_incident(db)
    res, calls = with_actions(lambda: wd.duplicate_review_lane(run, db, act=True))
    check("t1 the signature fires", res is not None and res[0] == "review-dup")
    check("t1 the text names both sessions",
          res and "3530" in res[1] and "3531" in res[1] and H7 in res[1])
    check("t1 the duplicate is reclaimed",
          any(len(c) > 3 and c[2] == "reclaim" and c[3] == "t_impl" for c in calls))
    check("t1 the card is parked on rev-hold",
          any(len(c) > 4 and c[2] == "assign" and c[3] == "t_impl"
              and c[4] == wd.REVIEW_HOLD_PROFILE for c in calls))

    db = make_board()
    run = seed_live_incident(db, hb_arm=time.time() - 3600)
    res, calls = with_actions(lambda: wd.duplicate_review_lane(run, db, act=True))
    check("t2 dead arm: no signature", res is None and calls == [])

    db = make_board()
    run = seed_live_incident(db, arm_started=1794889000, dup_started=1794888988)
    res, calls = with_actions(lambda: wd.duplicate_review_lane(run, db, act=True))
    check("t3 arm later than the duplicate: no signature", res is None and calls == [])

    db = make_board()
    run = seed_live_incident(db)
    run["profile"] = "coder"
    res, calls = with_actions(lambda: wd.duplicate_review_lane(run, db, act=True))
    check("t4 non-reviewer profile: no signature", res is None and calls == [])

    db = make_board()
    run = seed_live_incident(db, dup_task_key="reviewer-pr384-%s" % H7)
    res, calls = with_actions(lambda: wd.duplicate_review_lane(run, db, act=True))
    check("t5 the run's card is an arm: no signature", res is None and calls == [])

    db = make_board()
    run = seed_live_incident(db, req_head=False)
    res, calls = with_actions(lambda: wd.duplicate_review_lane(run, db, act=True))
    check("t6 head not named: no signature", res is None and calls == [])

    db = make_board()
    seed_parked(db, "Review of PR #384 at head %s finished with verdict PASS:"
                    " issuecomment-1" % HEAD)
    res, calls = with_actions(lambda: wd.restore_parked_review(db, act=True))
    check("t7 restore puts the card back on reviewer",
          any(len(c) > 4 and c[2] == "assign" and c[3] == "t_parked"
              and c[4] == "reviewer" for c in calls))
    check("t7 restore is reported", len(res) == 1 and "t_parked" in res[0])

    db = make_board()
    seed_parked(db, "run finished, no verdict on the head")
    res, calls = with_actions(lambda: wd.restore_parked_review(db, act=True))
    check("t8 without a verdict the card stays parked", res == [] and calls == [])

    db = make_board()
    run = seed_live_incident(db)
    res, calls = with_actions(lambda: wd.duplicate_review_lane(run, db, act=False))
    check("t9 selftest (act=False) takes no action", res is not None and calls == [])

    if FAILURES:
        print("FAILURES: %s" % ", ".join(FAILURES))
        return 1
    print("OK duplicate review: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
