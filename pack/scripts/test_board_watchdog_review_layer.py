#!/usr/bin/env python3
"""Test of the review layer in board-watchdog.py after the switch to
"one PR = one card".

The watchdog no longer arms reviewer cards per PR head, nor paired or WCFY
cards: review lives on the executor card (kanban_request_review with
reviewer=reviewer), and the executor writes the WCFY itself. The layer keeps
only the rework arm for a change request without a rework card and the
return of legacy review parkings. Checks:

  L0/L1 the removed hold file and arm helpers are gone;
  N1 a head without a verdict and without a card is not armed;
  N2 a PASS verdict on the head creates no WCFY card;
  N4 a legacy review-layer parking returns to ready when the layer is empty.

The rework arm on a change request without a rework card is covered by its
own test battery.

Run: python3 test_board_watchdog_review_layer.py
Mutant run: BOARD_WATCHDOG_SCRIPT=/path/to/mutant.py python3 test_board_watchdog_review_layer.py
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
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
# A fake Hermes home and neutral project settings, set before the script is
# loaded: the script reads them through factory_config at import time.
_HOME = tempfile.mkdtemp(prefix="wdtest-home-")
atexit.register(shutil.rmtree, _HOME, True)
os.environ["HERMES_HOME"] = _HOME
os.environ["FACTORY_REPO"] = "acme/widget"
os.environ["FACTORY_REPO_PATH"] = "/srv/hermes/widget"
os.environ["FACTORY_BOARD"] = "main"
os.environ["FACTORY_OWNER_LOGIN"] = "owner-login"
os.environ["FACTORY_OWNER_CHAT"] = "telegram:000000000"
os.environ["FACTORY_BASE_BRANCHES"] = "develop/1.x,develop/2.x"
sys.path.insert(0, HERE)

# The path can be overridden through BOARD_WATCHDOG_SCRIPT: mutation runs
# test a COPY of the script with a mutation, never the real file.
SCRIPT = os.environ.get("BOARD_WATCHDOG_SCRIPT") or os.path.join(
    HERE, "board-watchdog.py")

spec = importlib.util.spec_from_file_location("board_watchdog", SCRIPT)
if spec is None or spec.loader is None:
    print("SKIP: cannot load board-watchdog.py")
    sys.exit(0)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

HEAD154 = "ff6dcdfde6d1ba5d2e19c3906771b31f66492dda"
HEAD186 = "919de9d3631c77976b4048b22aec9f015fb7f978"
# A pair of twin PRs: #178 (develop/1.x) and #154 (develop/2.x); the change
# request on #178 referred to a blocker of the twin #154.
HEAD178 = "06fd93d5f66566cc8e7454d3a4c5887fd2cd5f0d"
HEAD154B = "ecac1b21cff6b75fc08e7d8d153ac26b6d6f162a"


def iso(ts):
    return datetime.fromtimestamp(
        ts, timezone.utc).isoformat().replace("+00:00", "Z")


def wcfy_comment(age_s, fresh=True):
    """WCFY comment with a dynamic date: fresh=True is newer than the head
    (age_s/2), False is older than the head (age_s + one hour). Fixed dates
    rot as the clock moves, so only relative ones are used."""
    ts = time.time() - (age_s / 2 if fresh else age_s + 3600)
    return {"body": "## What changes for you\n- live marker for the head",
            "createdAt": iso(ts)}


def make_board(tasks=(), comments=()):
    """Mini board. tasks: dict(id, title, status, assignee, priority, key, body)."""
    tmp = tempfile.mkdtemp(prefix="wdtest-revlayer-")
    slug = "testboard"
    os.makedirs(os.path.join(tmp, slug), exist_ok=True)
    con = sqlite3.connect(os.path.join(tmp, slug, "kanban.db"))
    con.executescript("""
        create table tasks (id text primary key, title text, body text default '',
            assignee text default 'ops', status text, priority int default 0,
            created_by text default 'agent', created_at int,
            started_at int, completed_at int,
            workspace_kind text, workspace_path text, branch_name text,
            project_id text, claim_lock text, claim_expires int,
            tenant text, result text, idempotency_key text,
            consecutive_failures int default 0, worker_pid int,
            last_failure_error text, max_runtime_seconds int default null,
            last_heartbeat_at int, current_run_id int,
            workflow_template_id text, current_step_key text, skills text,
            model_override text, provider_override text,
            reasoning_effort text, max_retries int,
            goal_mode int default 0, goal_max_turns int,
            session_id text, block_kind text default null,
            block_recurrences int default 0);
        create table task_links (parent_id text, child_id text);
        create table task_runs (id integer primary key, task_id text,
            profile text, status text, started_at int, ended_at int,
            outcome text, summary text, metadata text, error text);
        create table task_events (id integer primary key, task_id text,
            run_id int, kind text, payload text, created_at int);
        create table task_comments (id integer primary key, task_id text,
            body text, created_at int);
    """)
    now = time.time()
    for t in tasks:
        con.execute(
            "insert into tasks (id, title, status, assignee, priority,"
            " created_by, created_at, idempotency_key, body)"
            " values (?,?,?,?,?,?,?,?,?)",
            (t["id"], t.get("title", "t " + t["id"]), t.get("status", "todo"),
             t.get("assignee", "coder"), t.get("priority", 42),
             t.get("created_by", "agent"), int(now - t.get("age", 3600)),
             t.get("key"), t.get("body", "body " + t["id"])))
    for task_id, body in comments:
        con.execute("insert into task_comments (task_id, body, created_at)"
                    " values (?,?,?)", (task_id, body, int(now)))
    # The production contract of `hermes kanban schedule` is a task_events
    # row (kind='scheduled', payload {"reason": ...}) plus a comment
    # "SCHEDULED: <reason>". Return candidates are found by the comment, the
    # decision is made by the latest scheduled event: the fixture seeds both
    # consistently, otherwise a review-layer parking looks "reasonless" and
    # never returns.
    for task_id, body in comments:
        if body.startswith("SCHEDULED: "):
            con.execute(
                "insert into task_events (task_id, kind, payload,"
                " created_at) values (?,?,?,?)",
                (task_id, "scheduled",
                 json.dumps({"reason": body[len("SCHEDULED: "):]}),
                 int(now)))
    con.commit()
    con.close()
    old_boards = mod.BOARDS
    mod.BOARDS = tmp
    return slug, tmp, old_boards


def pr_json(head, age_s, comments=(), reviews=(), base="develop/2.x"):
    committed = datetime.fromtimestamp(
        time.time() - age_s, timezone.utc).isoformat().replace("+00:00", "Z")
    # a string is a comment body dated before CALIBRATION_REQUIRED_SINCE
    # (old verdicts are grandfathered); a dict sets createdAt explicitly
    items = []
    for c in comments:
        if isinstance(c, dict):
            items.append({"body": c["body"],
                          "createdAt": c.get(
                              "createdAt", "2026-10-30T00:00:00Z")})
        else:
            items.append({"body": c, "createdAt": "2026-10-30T00:00:00Z"})
    return json.dumps({
        "headRefOid": head,
        "baseRefName": base,
        "commits": [{"committedDate": committed}],
        "reviews": list(reviews),
        "comments": items,
    })


# The default CI scenario is green; the mandatory set mirrors the required
# contexts of the base branch protection.
REQ_2X = ["build (JDK 17)", "build (JDK 21)", "e2e web-a (JDK 17)",
          "e2e web-b (JDK 17)"]
GREEN_CHECKS = json.dumps([{"name": n, "state": "SUCCESS"} for n in REQ_2X])


def fake_run(prs, calls, new_ids=None, protection=None, checks=None):
    """gh list/view/checks/api follow the scenario, hermes create/schedule/unblock are recorded.

    protection: {base: protection json string, or "" for a read failure}.
    checks: {PR number: gh pr checks json string, or "" for a read failure}.
    """
    new_ids = new_ids or ["t_new1", "t_new2", "t_new3", "t_new4", "t_new5",
                          "t_new6", "t_new7", "t_new8"]

    def _run(cmd, timeout=120):
        calls.append(cmd)
        if cmd[0] == "gh":
            if len(cmd) > 1 and cmd[1] == "api":
                b = cmd[2].split("/branches/", 1)[1].rsplit(
                    "/protection", 1)[0]
                return (protection or {}).get(
                    b, json.dumps({"required_status_checks":
                                   {"contexts": REQ_2X}}))
            if "checks" in cmd:
                return (checks or {}).get(int(cmd[3]), GREEN_CHECKS)
            if "list" in cmd:
                return json.dumps([{"number": n, "isDraft": False}
                                   for n in sorted(prs)])
            if "view" in cmd:
                return prs.get(int(cmd[3]), "")
            return ""
        if len(cmd) > 4 and cmd[4] == "create":
            return json.dumps({"id": new_ids.pop(0), "status": "ready"})
        if len(cmd) > 4 and cmd[4] == "schedule":
            return "Scheduled %s" % cmd[5]
        if len(cmd) > 4 and cmd[4] == "unblock":
            return "Unblocked %s" % cmd[5]
        return ""
    return _run


def with_scenario(prs, tasks=(), comments=(),
                  protection=None, checks=None, keep_orphans=False):
    """Runs review_layer_check on a fixture; gh/hermes calls go to calls."""
    slug, tmp, old = make_board(tasks, comments)
    calls = []
    real_run, real_repo = mod.run, mod.board_repo
    mod.run = fake_run(prs, calls, protection=protection, checks=checks)
    mod.board_repo = lambda s: "acme/repo"
    try:
        found = mod.review_layer_check(slug)
    finally:
        mod.run = real_run
        mod.board_repo = real_repo
        mod.BOARDS = old
    # The finding "PR of the active milestone without a card" is legitimate
    # in mock scenarios; it has its own tests.
    if not keep_orphans:
        found = [f for f in found if ":r36:ms-orphan:" not in f[0]]
    return found, calls, tmp


def creates(calls):
    return [c for c in calls if len(c) > 4 and c[4] == "create"]


FAILS = []


def check(name, cond, detail=""):
    if cond:
        print("ok  %s" % name)
    else:
        FAILS.append(name)
        print("FAIL %s :: %s" % (name, detail))


def main():
    # "One PR = one card": the per-head reviewer arm, the paired arm, the
    # WCFY arm and their hold file are removed (owner decision).

    check("L0 the paired-arm hold file is gone",
          not hasattr(mod, "PAIR_WCFY_HOLD")
          and not hasattr(mod, "LESSON35_HOLD"),
          "attribute present: code from before one PR = one card")
    check("L1 _review_arm_card/_pair_rearm_card/_wcfy_arm_card removed",
          not any(hasattr(mod, n) for n in
                  ("_review_arm_card", "_pair_rearm_card", "_wcfy_arm_card")),
          "old arm helpers still present")

    # N1: a 2 h head without a verdict and without a card: NO create.
    prs = {301: pr_json(HEAD154, 2 * 3600)}
    found, calls, tmp = with_scenario(prs, tasks=[])
    check("N1 a head without a verdict is not armed", not creates(calls),
          str(creates(calls)[:2]))
    shutil.rmtree(tmp, ignore_errors=True)

    # N2: a PASS verdict on the head: no create (the WCFY arm is removed).
    prs = {302: pr_json(HEAD186, 2 * 3600,
                        comments=[{"body": "## Review verdict: PASS",
                                   "createdAt": None}])}
    found, calls, tmp = with_scenario(prs, tasks=[])
    check("N2 PASS on the head creates no WCFY card", not creates(calls),
          str(creates(calls)[:2]))
    shutil.rmtree(tmp, ignore_errors=True)

    # N4: a legacy parking returns when the layer is empty.
    sched = ("SCHEDULED: %s: unchecked heads 1" % mod.REVIEW_PARK_MARKER)
    prs = {304: pr_json(HEAD154, 2 * 3600)}
    found, calls, tmp = with_scenario(
        prs, tasks=[{"id": "t_par", "title": "minor", "status": "scheduled",
                     "priority": 10, "age": 7200}],
        comments=[("t_par", sched)])
    check("N4 a review-layer parking returns when the layer is empty",
          any(len(c) > 4 and c[4] == "unblock" and "t_par" in c
              for c in calls), str([c for c in calls if "unblock" in c]))
    shutil.rmtree(tmp, ignore_errors=True)

    print("\n%s" % ("ALL OK" if not FAILS else "FAILURES: %s"
                    % ", ".join(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
