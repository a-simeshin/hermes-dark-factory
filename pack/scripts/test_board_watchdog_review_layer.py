#!/usr/bin/env python3
"""Test of the review layer in board-watchdog.py.

An open PR whose head is older than 30 minutes, has no verdict on this sha
and no live reviewer card gets a card from the watchdog (key
reviewer-pr<N>-<head7>, p100). While such heads exist, ready cards below p50
are parked in scheduled and returned with unblock when the layer is empty.

Blocker cases: an executor card that closed without a reviewer card must get
one from the watchdog; a coder handoff comment "pushed head X, awaiting
verdict" is not a verdict.

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


def with_scenario(prs, tasks=(), comments=(), hold35=False,
                  protection=None, checks=None):
    slug, tmp, old = make_board(tasks, comments)
    calls = []
    real_run, real_repo = mod.run, mod.board_repo
    mod.run = fake_run(prs, calls, protection=protection, checks=checks)
    mod.board_repo = lambda s: "acme/repo"
    # PAIR_WCFY_HOLD is pinned into tmp: the real hold file of a host must
    # not affect the tests; hold35=True switches on the dry-run branches.
    old_hold = mod.PAIR_WCFY_HOLD
    hold_path = os.path.join(tmp, "pair-wcfy-hold")
    if hold35:
        open(hold_path, "w").close()
    mod.PAIR_WCFY_HOLD = hold_path
    try:
        found = mod.review_layer_check(slug)
    finally:
        mod.run = real_run
        mod.board_repo = real_repo
        mod.BOARDS = old
        mod.PAIR_WCFY_HOLD = old_hold
    return found, calls, tmp


def creates(calls):
    return [c for c in calls if len(c) > 4 and c[4] == "create"]


def test_arm_creates_card():
    found, calls, tmp = with_scenario({154: pr_json(HEAD154, 2 * 3600)})
    made = creates(calls)
    assert len(made) == 1, calls
    cmd = made[0]
    key = cmd[cmd.index("--idempotency-key") + 1]
    assert key == "reviewer-pr154-ff6dcdf", key
    assert cmd[cmd.index("--priority") + 1] == "100"
    assert cmd[cmd.index("--assignee") + 1] == "reviewer"
    assert cmd[cmd.index("--created-by") + 1] == "watchdog"
    assert "PR #154" in cmd[5] and "ff6dcdf" in cmd[5], cmd[5]
    assert any(f[0].endswith("arm:154:ff6dcdf") and "t_new1" in f[2]
               for f in found), found
    shutil.rmtree(tmp)
    print("OK test_arm_creates_card")


def test_verdict_comment_on_head_no_arm():
    body = "## Re-review verdict: PASS on head %s" % HEAD154[:7]
    found, calls, tmp = with_scenario(
        {154: pr_json(HEAD154, 2 * 3600,
                      comments=[body, wcfy_comment(2 * 3600)])})
    assert creates(calls) == [], calls
    assert found == [], found
    shutil.rmtree(tmp)
    print("OK test_verdict_comment_on_head_no_arm")


def test_full_sha_verdict_no_arm():
    body = "## Review verdict: PASS\n\non exact head `%s`" % HEAD154
    found, calls, tmp = with_scenario(
        {154: pr_json(HEAD154, 3 * 3600,
                      comments=[body, wcfy_comment(3 * 3600)])})
    assert creates(calls) == [] and found == []
    shutil.rmtree(tmp)
    print("OK test_full_sha_verdict_no_arm")


def test_formal_approval_no_arm():
    found, calls, tmp = with_scenario({154: pr_json(
        HEAD154, 2 * 3600, comments=[wcfy_comment(2 * 3600)],
        reviews=[{"state": "APPROVED", "commit": {"oid": HEAD154}}])})
    assert creates(calls) == [] and found == []
    shutil.rmtree(tmp)
    print("OK test_formal_approval_no_arm")


def test_formal_changes_requested_on_head_is_verdict():
    """A formal CHANGES_REQUESTED of the owner on the exact head is a verdict
    on that sha: an arm would be a duplicate."""
    found, calls, tmp = with_scenario({223: pr_json(
        "371f50f1111111111111111111111111111111111", 2 * 3600,
        reviews=[{"state": "CHANGES_REQUESTED",
                  "commit": {"oid": "371f50f1111111111111111111111111111111111"}}])})
    assert creates(calls) == [] and found == [], (calls, found)
    shutil.rmtree(tmp)
    print("OK test_formal_changes_requested_on_head_is_verdict")


def test_formal_changes_requested_on_other_sha_still_arms():
    found, calls, tmp = with_scenario({223: pr_json(
        "371f50f1111111111111111111111111111111111", 2 * 3600,
        reviews=[{"state": "CHANGES_REQUESTED",
                  "commit": {"oid": "befea041111111111111111111111111111111111"}}])})
    assert len(creates(calls)) == 1, calls
    shutil.rmtree(tmp)
    print("OK test_formal_changes_requested_on_other_sha_still_arms")


def test_coder_handoff_is_not_verdict():
    """"Pushed head, awaiting verdict" without a verdict header: arm needed."""
    body = ("Rework done, pushed head %s, awaiting review verdict from "
            "reviewer" % HEAD154[:7])
    found, calls, tmp = with_scenario(
        {154: pr_json(HEAD154, 2 * 3600, comments=[body])})
    assert len(creates(calls)) == 1, calls
    shutil.rmtree(tmp)
    print("OK test_coder_handoff_is_not_verdict")


def test_verdict_on_other_sha_still_arms():
    body = "## Review verdict: CHANGES REQUESTED on head deadbee"
    found, calls, tmp = with_scenario(
        {154: pr_json(HEAD154, 2 * 3600, comments=[body])})
    assert len(creates(calls)) == 1, calls
    shutil.rmtree(tmp)
    print("OK test_verdict_on_other_sha_still_arms")


def test_live_card_by_key_no_arm():
    tasks = [{"id": "t_rev1", "status": "todo", "assignee": "reviewer",
              "priority": 100, "key": "reviewer-pr154-ff6dcdf"}]
    found, calls, tmp = with_scenario(
        {154: pr_json(HEAD154, 2 * 3600)}, tasks=tasks)
    assert creates(calls) == [], calls
    assert not any(":arm:" in f[0] for f in found), found
    shutil.rmtree(tmp)
    print("OK test_live_card_by_key_no_arm")


def test_live_manual_reviewer_card_no_arm():
    tasks = [{"id": "t_rev2", "status": "blocked", "assignee": "reviewer",
              "priority": 100, "key": None,
              "title": "[reviewer] PR #154 final review",
              "body": "head ff6dcdf"}]
    found, calls, tmp = with_scenario(
        {154: pr_json(HEAD154, 2 * 3600)}, tasks=tasks)
    assert creates(calls) == [], calls
    assert any("t_rev2" in f[2] and "blocked" in f[2] for f in found), found
    shutil.rmtree(tmp)
    print("OK test_live_manual_reviewer_card_no_arm")


def test_fresh_head_skipped():
    found, calls, tmp = with_scenario({154: pr_json(HEAD154, 10 * 60)})
    assert creates(calls) == [] and found == []
    shutil.rmtree(tmp)
    print("OK test_fresh_head_skipped")


def test_ancient_head_skipped():
    found, calls, tmp = with_scenario(
        {154: pr_json(HEAD154, 20 * 24 * 3600)})
    assert creates(calls) == [] and found == []
    shutil.rmtree(tmp)
    print("OK test_ancient_head_skipped")


def test_cap_five_creates():
    prs = {n: pr_json("abcdef%d1234567890abcdef1234567890abcdef" % n,
                      2 * 3600 + n)
           for n in (150, 151, 152, 153, 154, 155)}
    tasks = [{"id": "t_low", "status": "ready", "assignee": "coder",
              "priority": 30}]
    found, calls, tmp = with_scenario(prs, tasks=tasks)
    assert len(creates(calls)) == mod.REVIEW_ARM_MAX_CREATE, len(creates(calls))
    assert any("limit of" in f[2] for f in found), found
    sched = [c for c in calls if len(c) > 4 and c[4] == "schedule"]
    assert sched, "low-prio ready must be parked: %s" % calls
    shutil.rmtree(tmp)
    print("OK test_cap_five_creates")


def test_park_low_prio_ready_only():
    tasks = [
        {"id": "t_low", "status": "ready", "assignee": "coder",
         "priority": 30},
        {"id": "t_high", "status": "ready", "assignee": "coder",
         "priority": 100},
        {"id": "t_noas", "status": "ready", "assignee": "",
         "priority": 10},
        {"id": "t_run", "status": "running", "assignee": "coder",
         "priority": 0},
    ]
    found, calls, tmp = with_scenario(
        {154: pr_json(HEAD154, 2 * 3600)}, tasks=tasks)
    sched = [c for c in calls if len(c) > 4 and c[4] == "schedule"]
    assert len(sched) == 1 and sched[0][5] == "t_low", calls
    assert any(mod.REVIEW_PARK_MARKER in " ".join(c) for c in sched), sched
    assert not any(c[5] in ("t_high", "t_noas") for c in sched), sched
    assert any("t_run" in f[2] and "no preemption" in f[2] for f in found), found
    assert any("t_low(p30)" in f[2] for f in found), found
    shutil.rmtree(tmp)
    print("OK test_park_low_prio_ready_only")


def test_release_when_layer_empty():
    tasks = [{"id": "t_parked", "status": "scheduled", "assignee": "coder",
              "priority": 30}]
    comments = [("t_parked",
                 "SCHEDULED: %s: unchecked heads 1" % mod.REVIEW_PARK_MARKER)]
    verdict = "## Review verdict: PASS on head %s" % HEAD186[:7]
    found, calls, tmp = with_scenario(
        {186: pr_json(HEAD186, 2 * 3600,
                      comments=[verdict, wcfy_comment(2 * 3600)])},
        tasks=tasks, comments=comments)
    unblocks = [c for c in calls if len(c) > 4 and c[4] == "unblock"]
    assert len(unblocks) == 1 and unblocks[0][5] == "t_parked", calls
    assert any("t_parked" in f[2] and "emptied" in f[2] for f in found), found
    assert creates(calls) == [], calls
    shutil.rmtree(tmp)
    print("OK test_release_when_layer_empty")


def test_gh_failure_silent():
    found, calls, tmp = with_scenario({154: ""})
    assert found == [] and creates(calls) == [], calls
    shutil.rmtree(tmp)
    print("OK test_gh_failure_silent")


def test_done_card_without_verdict_arms_r2():
    """The reviewer closed a card carrying the head key without a verdict.
    The base key is deduplicated against the done card, so the watchdog must
    raise an alert and create a new card under the key suffix -r2."""
    tasks = [{"id": "t_revdone", "status": "done", "assignee": "reviewer",
              "priority": 100, "key": "reviewer-pr154-ff6dcdf",
              "title": "[reviewer] PR #154 (head ff6dcdf): head review",
              "body": "head ff6dcdf"}]
    found, calls, tmp = with_scenario(
        {154: pr_json(HEAD154, 2 * 3600)}, tasks=tasks)
    made = creates(calls)
    assert len(made) == 1, calls
    cmd = made[0]
    key = cmd[cmd.index("--idempotency-key") + 1]
    assert key == "reviewer-pr154-ff6dcdf-r2", key
    assert any(":revlayer:closed:154:ff6dcdf" in f[0] and "t_revdone" in f[2]
               and "WITHOUT a verdict" in f[2] for f in found), found
    assert any(f[0].endswith("arm:154:ff6dcdf") and "r2" in f[2] for f in found), found
    shutil.rmtree(tmp)
    print("OK test_done_card_without_verdict_arms_r2")


def test_done_r2_family_arms_r3():
    """A second done card in a row without a verdict: the next suffix is r3."""
    tasks = [
        {"id": "t_revdone1", "status": "done", "assignee": "reviewer",
         "priority": 100, "key": "reviewer-pr154-ff6dcdf"},
        {"id": "t_revdone2", "status": "done", "assignee": "reviewer",
         "priority": 100, "key": "reviewer-pr154-ff6dcdf-r2"},
    ]
    found, calls, tmp = with_scenario(
        {154: pr_json(HEAD154, 2 * 3600)}, tasks=tasks)
    made = creates(calls)
    assert len(made) == 1, calls
    key = made[0][made[0].index("--idempotency-key") + 1]
    assert key == "reviewer-pr154-ff6dcdf-r3", key
    shutil.rmtree(tmp)
    print("OK test_done_r2_family_arms_r3")


def test_live_r2_keyed_card_no_arm():
    """A live card under the suffixed key -r2 (text without #154): no
    duplicate may be created, the family is alive."""
    tasks = [{"id": "t_revr2", "status": "ready", "assignee": "reviewer",
              "priority": 100, "key": "reviewer-pr154-ff6dcdf-r2",
              "title": "head review", "body": "in progress"}]
    found, calls, tmp = with_scenario(
        {154: pr_json(HEAD154, 2 * 3600)}, tasks=tasks)
    assert creates(calls) == [], calls
    assert not any(":revlayer:closed:" in f[0] for f in found), found
    shutil.rmtree(tmp)
    print("OK test_live_r2_keyed_card_no_arm")


def test_live_pair_key_card_no_arm():
    """A double arm of one head is forbidden.

    A live card of the PAIRED family (key reviewer-pr<N>-<head7>-pM, body
    without #154 and without head7, so the text match does not see it) on
    the same head: the main arm is not created. Mutant: drop the
    `startswith(base + "-p")` branch in fam_live_keys and the card is
    created."""
    tasks = [{"id": "t_pair", "status": "ready", "assignee": "reviewer",
              "priority": 100, "key": "reviewer-pr154-ff6dcdf-p178",
              "title": "repeated pair review", "body": "no number"}]
    found, calls, tmp = with_scenario(
        {154: pr_json(HEAD154, 2 * 3600)}, tasks=tasks)
    assert creates(calls) == [], calls
    shutil.rmtree(tmp)
    print("OK test_live_pair_key_card_no_arm")


def test_done_card_with_verdict_silent():
    """A done card with the key but a verdict on the head already exists: silence."""
    tasks = [{"id": "t_revdone", "status": "done", "assignee": "reviewer",
              "priority": 100, "key": "reviewer-pr154-ff6dcdf"}]
    verdict = "## Review verdict: PASS on head %s" % HEAD154[:7]
    found, calls, tmp = with_scenario(
        {154: pr_json(HEAD154, 2 * 3600,
                      comments=[verdict, wcfy_comment(2 * 3600)])},
        tasks=tasks)
    assert creates(calls) == [] and found == [], (calls, found)
    shutil.rmtree(tmp)
    print("OK test_done_card_with_verdict_silent")


def test_verdict_without_calibration_after_cutoff_arms():
    """A verdict dated after CALIBRATION_REQUIRED_SINCE without a Calibration
    block does not count: the watchdog arms as if there were no verdict."""
    verdict = "## Review verdict PASS on exact head `%s`" % HEAD154
    found, calls, tmp = with_scenario({154: pr_json(
        HEAD154, 2 * 3600,
        comments=[{"body": verdict,
                   "createdAt": "2026-11-01T05:00:00Z"}])})
    made = creates(calls)
    assert len(made) == 1, calls
    key = made[0][made[0].index("--idempotency-key") + 1]
    assert key == "reviewer-pr154-ff6dcdf", key
    shutil.rmtree(tmp)
    print("OK test_verdict_without_calibration_after_cutoff_arms")


def test_verdict_with_calibration_block_no_arm():
    verdict = ("## Review verdict PASS on exact head `%s`\n\n"
               "## Calibration\n"
               "- contract: issue #190 closed, all criteria are in the diff\n"
               "- mutation: the owner's blocker was reproduced and killed by a test\n"
               "- twin: N/A, the PR has no twin\n"
               "- ui-input: live 375px, the button is clickable\n"
               "- pr-texts: WCFY checked against the UI behaviour" % HEAD154)
    found, calls, tmp = with_scenario({154: pr_json(
        HEAD154, 2 * 3600,
        comments=[{"body": verdict, "createdAt": "2026-11-01T05:00:00Z"},
                  wcfy_comment(2 * 3600)])})
    assert creates(calls) == [] and found == [], (calls, found)
    shutil.rmtree(tmp)
    print("OK test_verdict_with_calibration_block_no_arm")


def test_old_verdict_without_calibration_grandfathered():
    verdict = "## Review verdict PASS on exact head `%s`" % HEAD154
    found, calls, tmp = with_scenario({154: pr_json(
        HEAD154, 2 * 3600,
        comments=[{"body": verdict, "createdAt": "2026-10-30T00:00:00Z"},
                  wcfy_comment(2 * 3600)])})
    assert creates(calls) == [] and found == [], (calls, found)
    shutil.rmtree(tmp)
    print("OK test_old_verdict_without_calibration_grandfathered")


def test_newest_verdict_decides_calibration():
    """An old verdict without the block, then a fresh one with it: counted.
    A fresh one without the block on top of an old one with it: re-arm."""
    old = {"body": "## Review verdict PASS on head `%s`" % HEAD154[:7],
           "createdAt": "2026-10-30T00:00:00Z"}
    fresh_ok = {"body": "## Review verdict PASS on head `%s`\n\n"
                        "## Calibration\n- contract: ok" % HEAD154,
                "createdAt": "2026-11-02T00:00:00Z"}
    fresh_bad = {"body": "## Review verdict PASS on head `%s`" % HEAD154,
                 "createdAt": "2026-11-02T00:00:00Z"}
    found, calls, tmp = with_scenario({154: pr_json(
        HEAD154, 2 * 3600,
        comments=[old, fresh_ok, wcfy_comment(2 * 3600)])})
    assert creates(calls) == [], calls
    shutil.rmtree(tmp)
    found, calls, tmp = with_scenario({154: pr_json(
        HEAD154, 2 * 3600, comments=[fresh_ok, fresh_bad])})
    assert len(creates(calls)) == 1, calls
    shutil.rmtree(tmp)
    print("OK test_newest_verdict_decides_calibration")


# --- Twin pairs and WCFY ----------------------------------------------------
CR_PAIR = ("## Review verdict CHANGES REQUESTED (fifth pass) on head `%s`.\n"
           "The single blocker below is about the pair, not this branch's "
           "own code: no once-per-death guard on the twin #154. Push a new "
           "head on #154, then re-request review here." % HEAD178)
TWIN_PASS = ("## Review verdict APPROVED (PASS) on exact head `%s` against "
             "develop/2.x. This closes the pair blocker." % HEAD154B)


def pair_scenario(cr_at, pass_at, extra_154=(), extra_178=()):
    cr = {"body": CR_PAIR, "createdAt": cr_at}
    twin_pass = {"body": TWIN_PASS, "createdAt": pass_at}
    return {178: pr_json(HEAD178, 3 * 3600, comments=list(extra_178) + [cr]),
            154: pr_json(HEAD154B, 3 * 3600,
                         comments=list(extra_154) + [twin_pass])}


def test_pair_cr_twin_pass_newer_no_arm_twin_era_over():
    """No twins exist, TWIN_ERA=False. A change request on #178 that refers
    to the twin #154 blocker, with a twin PASS fresher than the request, no
    longer arms a repeated review: the paired arm is off. Only the owner may
    switch TWIN_ERA back on."""
    assert mod.TWIN_ERA is False, "TWIN_ERA is controlled by the owner only"
    found, calls, tmp = with_scenario(
        pair_scenario("2026-10-31T06:00:00Z", "2026-10-31T07:00:00Z",
                      extra_154=[wcfy_comment(3 * 3600)]))
    made = creates(calls)
    assert made == [], made
    assert found == [], found
    shutil.rmtree(tmp)
    print("OK test_pair_cr_twin_pass_newer_no_arm_twin_era_over")


def test_pair_cr_twin_reject_no_arm():
    """The blocker is not closed: the twin's latest verdict is a change
    request too. No arm, both by the twin-era rule (no PASS) and with
    TWIN_ERA=False (the paired arm is off altogether)."""
    twin_cr = {"body": "## Review verdict CHANGES REQUESTED on exact head "
                       "`%s`" % HEAD154B,
               "createdAt": "2026-10-31T08:00:00Z"}
    prs = pair_scenario("2026-10-31T06:00:00Z", "2026-10-31T07:00:00Z")
    prs[154] = pr_json(HEAD154B, 3 * 3600,
                       comments=[twin_cr, wcfy_comment(3 * 3600)])
    found, calls, tmp = with_scenario(prs)
    assert creates(calls) == [] and found == [], (calls, found)
    shutil.rmtree(tmp)
    print("OK test_pair_cr_twin_reject_no_arm")


def test_pair_twin_pass_older_than_cr_no_arm():
    """The twin PASS is OLDER than the change request: the reviewer already
    saw it and rejected for another reason, a repeat would loop the arms.
    The time guard is mandatory. (Branch A is off with TWIN_ERA=False: no
    arm in any order of times.)"""
    prs = pair_scenario("2026-10-31T08:00:00Z", "2026-10-31T07:00:00Z",
                        extra_154=[wcfy_comment(3 * 3600)])
    found, calls, tmp = with_scenario(prs)
    assert creates(calls) == [] and found == [], (calls, found)
    shutil.rmtree(tmp)
    print("OK test_pair_twin_pass_older_than_cr_no_arm")


def test_pair_cr_without_refs_no_arm():
    """A change request without a #N reference: the paired blocker cannot be
    found mechanically, no arm."""
    cr = {"body": "## Review verdict CHANGES REQUESTED on head `%s`.\n"
                  "BLOCK: missing tests for the fallback." % HEAD178,
          "createdAt": "2026-10-31T06:00:00Z"}
    prs = {178: pr_json(HEAD178, 3 * 3600, comments=[cr]),
           154: pr_json(HEAD154B, 3 * 3600,
                        comments=[{"body": TWIN_PASS,
                                   "createdAt": "2026-10-31T07:00:00Z"},
                                  wcfy_comment(3 * 3600)])}
    found, calls, tmp = with_scenario(prs)
    assert creates(calls) == [] and found == [], (calls, found)
    shutil.rmtree(tmp)
    print("OK test_pair_cr_without_refs_no_arm")


def test_pair_ref_without_pair_word_no_arm():
    """A change request refers to another PR but the blocker is its own and
    the pair word is absent. The PASS of the other PR is fresher than the
    request. No arm: the PASS of #189 does not close the symlink blocker of
    #188."""
    cr = {"body": "## Review verdict CHANGES REQUESTED on head `%s`.\n"
                  "### BLOCK: a machine-specific node_modules symlink is "
                  "committed.\nNote: PR #189 carries the same blob on top "
                  "of this head, so it needs the same removal."
                  % HEAD178,
          "createdAt": "2026-10-31T06:00:00Z"}
    prs = {178: pr_json(HEAD178, 3 * 3600, comments=[cr]),
           189: pr_json(HEAD154B, 3 * 3600,
                        comments=[{"body": TWIN_PASS.replace("twin", "PR"),
                                   "createdAt": "2026-10-31T07:00:00Z"},
                                  wcfy_comment(3 * 3600)])}
    found, calls, tmp = with_scenario(prs)
    assert creates(calls) == [] and found == [], (calls, found)
    shutil.rmtree(tmp)
    print("OK test_pair_ref_without_pair_word_no_arm")


def test_pair_rearm_after_done_family_escalates():
    """The pair card was closed without a verdict: a fresh key -r2 would be
    needed (dedup against the done card). With TWIN_ERA=False the paired arm
    is off, the family escalation -r2 does not fire: no twin is required, no
    arms at all."""
    tasks = [{"id": "t_pairdone", "status": "done", "assignee": "reviewer",
              "priority": 100, "key": "reviewer-pr178-06fd93d-p154"}]
    found, calls, tmp = with_scenario(
        pair_scenario("2026-10-31T06:00:00Z", "2026-10-31T07:00:00Z",
                      extra_154=[wcfy_comment(3 * 3600)]), tasks=tasks)
    made = creates(calls)
    assert made == [], made
    shutil.rmtree(tmp)
    print("OK test_pair_rearm_after_done_family_escalates")


def test_pair_live_key_card_no_arm():
    tasks = [{"id": "t_pairlive", "status": "ready", "assignee": "reviewer",
              "priority": 100, "key": "reviewer-pr178-06fd93d-p154"}]
    found, calls, tmp = with_scenario(
        pair_scenario("2026-10-31T06:00:00Z", "2026-10-31T07:00:00Z",
                      extra_154=[wcfy_comment(3 * 3600)]), tasks=tasks)
    assert creates(calls) == [], calls
    shutil.rmtree(tmp)
    print("OK test_pair_live_key_card_no_arm")


def test_pair_live_manual_reviewer_card_no_arm():
    """A live manual repeated-review card without a key: no duplicate."""
    tasks = [{"id": "t_manual", "status": "ready", "assignee": "reviewer",
              "priority": 100, "key": None,
              "title": "[reviewer] PR #178 repeated pair review",
              "body": "head 06fd93d, twin #154"}]
    found, calls, tmp = with_scenario(
        pair_scenario("2026-10-31T06:00:00Z", "2026-10-31T07:00:00Z",
                      extra_154=[wcfy_comment(3 * 3600)]), tasks=tasks)
    assert creates(calls) == [], calls
    shutil.rmtree(tmp)
    print("OK test_pair_live_manual_reviewer_card_no_arm")


def test_pass_without_wcfy_arms_coder_card():
    """A PASS on the head and no WCFY comment at all: a coder card p95."""
    verdict = "## Review verdict PASS on head `%s`" % HEAD154
    found, calls, tmp = with_scenario(
        {154: pr_json(HEAD154, 2 * 3600,
                      comments=[{"body": verdict,
                                 "createdAt": "2026-10-30T00:00:00Z"}])})
    made = creates(calls)
    assert len(made) == 1, calls
    cmd = made[0]
    key = cmd[cmd.index("--idempotency-key") + 1]
    assert key == "coder-pr154-wcfy-ff6dcdf", key
    assert cmd[cmd.index("--assignee") + 1] == "coder"
    assert cmd[cmd.index("--priority") + 1] == "95"
    assert "pr-deliverable" in cmd, cmd
    assert "What changes for you" in cmd[5], cmd[5]
    assert any(f[0].endswith("wcfy:154:ff6dcdf") for f in found), found
    shutil.rmtree(tmp)
    print("OK test_pass_without_wcfy_arms_coder_card")


def test_pass_stale_wcfy_arms():
    """A WCFY exists but is older than the head (it describes an old form of
    the change): a card is needed."""
    verdict = "## Review verdict PASS on head `%s`" % HEAD154
    found, calls, tmp = with_scenario(
        {154: pr_json(HEAD154, 2 * 3600,
                      comments=[{"body": verdict,
                                 "createdAt": "2026-10-30T00:00:00Z"},
                                wcfy_comment(2 * 3600, fresh=False)])})
    made = creates(calls)
    assert len(made) == 1, calls
    key = made[0][made[0].index("--idempotency-key") + 1]
    assert key == "coder-pr154-wcfy-ff6dcdf", key
    shutil.rmtree(tmp)
    print("OK test_pass_stale_wcfy_arms")


def test_wcfy_rearm_after_done_family_escalates():
    """A coder WCFY card closed without publishing the comment: -r2, not a
    dedup against the done key (the SQL collects coder-pr%-wcfy-% among done
    cards too)."""
    tasks = [{"id": "t_wcfydone", "status": "done", "assignee": "coder",
              "priority": 95, "key": "coder-pr154-wcfy-ff6dcdf"}]
    verdict = "## Review verdict PASS on head `%s`" % HEAD154
    found, calls, tmp = with_scenario(
        {154: pr_json(HEAD154, 2 * 3600,
                      comments=[{"body": verdict,
                                 "createdAt": "2026-10-30T00:00:00Z"}])},
        tasks=tasks)
    made = creates(calls)
    assert len(made) == 1, calls
    key = made[0][made[0].index("--idempotency-key") + 1]
    assert key == "coder-pr154-wcfy-ff6dcdf-r2", key
    shutil.rmtree(tmp)
    print("OK test_wcfy_rearm_after_done_family_escalates")


def test_wcfy_live_manual_coder_card_no_arm():
    tasks = [{"id": "t_wcodelive", "status": "ready", "assignee": "coder",
              "priority": 95, "key": None,
              "title": "[coder] PR #154 What changes for you",
              "body": "head ff6dcdf"}]
    verdict = "## Review verdict PASS on head `%s`" % HEAD154
    found, calls, tmp = with_scenario(
        {154: pr_json(HEAD154, 2 * 3600,
                      comments=[{"body": verdict,
                                 "createdAt": "2026-10-30T00:00:00Z"}])},
        tasks=tasks)
    assert creates(calls) == [], calls
    shutil.rmtree(tmp)
    print("OK test_wcfy_live_manual_coder_card_no_arm")


def test_formal_cr_newer_than_comment_pass_wins():
    """Mixed carriers: a formal change request of the owner on top of a
    reviewer PASS comment: the latest by time decides, the WCFY arm does not
    fire."""
    verdict = {"body": "## Review verdict APPROVED (PASS) on exact head "
                       "`%s`\n\n## Calibration\n- contract: ok" % HEAD154B,
               "createdAt": iso(time.time() - 3 * 3600 + 60)}
    found, calls, tmp = with_scenario({154: pr_json(
        HEAD154B, 3 * 3600,
        comments=[verdict, wcfy_comment(3 * 3600, fresh=False)],
        reviews=[{"state": "CHANGES_REQUESTED", "commit": {"oid": HEAD154B},
                  "submittedAt": iso(time.time() - 3 * 3600 + 120),
                  "body": "minor: only WCFY remains"}])})
    assert creates(calls) == [] and found == [], (calls, found)
    shutil.rmtree(tmp)
    print("OK test_formal_cr_newer_than_comment_pass_wins")


def test_comment_pass_newer_than_formal_cr_wins():
    """Reverse order: the PASS comment is fresher than the formal change
    request: the code is accepted, the WCFY arm works."""
    verdict = {"body": "## Review verdict APPROVED (PASS) on exact head "
                       "`%s`\n\n## Calibration\n- contract: ok\n\n"
                       "| Promise | Mutant | Test | Result |\n"
                       "|---|---|---|---|\n"
                       "| guard restored | drop guard | jest #1 | RED |"
                       % HEAD154B,
               "createdAt": iso(time.time() - 3 * 3600 + 120)}
    found, calls, tmp = with_scenario({154: pr_json(
        HEAD154B, 3 * 3600,
        comments=[verdict],
        reviews=[{"state": "CHANGES_REQUESTED", "commit": {"oid": HEAD154B},
                  "submittedAt": iso(time.time() - 3 * 3600 + 60),
                  "body": "minor: only WCFY remains"}])})
    made = creates(calls)
    assert len(made) == 1, calls
    key = made[0][made[0].index("--idempotency-key") + 1]
    assert key == "coder-pr154-wcfy-ecac1b2", key
    shutil.rmtree(tmp)
    print("OK test_comment_pass_newer_than_formal_cr_wins")


def test_pair_wcfy_hold_dry_no_create():
    """Hold file: the pair and WCFY branches report a plan and create no cards."""
    verdict = "## Review verdict PASS on head `%s`" % HEAD154
    found, calls, tmp = with_scenario(
        {154: pr_json(HEAD154, 2 * 3600,
                      comments=[{"body": verdict,
                                 "createdAt": "2026-10-30T00:00:00Z"}])},
        hold35=True)
    assert creates(calls) == [], calls
    assert any("wcfydry" in f[0] and "hold" in f[2].lower() for f in found), (
        found)
    shutil.rmtree(tmp)
    print("OK test_pair_wcfy_hold_dry_no_create")


# --- An owner change request on top of a reviewer PASS ----------------------
HEAD291 = "0f1cb149b23694766daaf5a2ea0c2473e8257e1a"
OLD291 = "1111111111111111111111111111111111111111"
OWNER_CR = ("## Review verdict\n\nCHANGES REQUESTED: normal (owner): three "
            "code blockers (leak, missing IT, perf regression)")


def test_owner_cr_same_head_after_reviewer_pass_no_wcfy():
    """A reviewer PASS on the head, then a formal owner change request with
    code blockers, then nothing. No WCFY card: the head will change its sha
    under the fix and the comment would go stale for nothing."""
    age = 56 * 3600
    reviewer_pass = {"body": "NOTE: token matches the PR author.\n\n"
                             "## Review verdict: APPROVE\n\nVerified on "
                             "head `%s`\n\n## Calibration\n- ok" % HEAD291,
                     "createdAt": iso(time.time() - age + 600)}
    found, calls, tmp = with_scenario({291: pr_json(
        HEAD291, age, comments=[reviewer_pass],
        reviews=[{"state": "CHANGES_REQUESTED",
                  "commit": {"oid": HEAD291},
                  "submittedAt": iso(time.time() - 20 * 3600),
                  "body": OWNER_CR}])})
    assert creates(calls) == [] and found == [], (calls, found)
    shutil.rmtree(tmp)
    print("OK test_owner_cr_same_head_after_reviewer_pass_no_wcfy")


def test_owner_cr_other_head_after_reviewer_pass_no_wcfy():
    """An owner change request on ANOTHER (old) head, fresher than the
    reviewer PASS on the current one: a filter by oid==head would not see it
    at all. A request on a PR is not lifted by a moving head: no WCFY."""
    reviewer_pass = {"body": "## Review verdict: APPROVE\n\nVerified on "
                             "head `%s`\n\n## Calibration\n- ok\n\n"
                             "| Promise | Mutant | Test | Result |\n"
                             "|---|---|---|---|\n"
                             "| scope guard | drop guard | jest #1 | RED |"
                             % HEAD291,
                     "createdAt": iso(time.time() - 3 * 3600 + 120)}
    found, calls, tmp = with_scenario({291: pr_json(
        HEAD291, 3 * 3600, comments=[reviewer_pass],
        reviews=[{"state": "CHANGES_REQUESTED",
                  "commit": {"oid": OLD291},
                  "submittedAt": iso(time.time() - 2 * 3600),
                  "body": OWNER_CR}])})
    assert creates(calls) == [] and found == [], (calls, found)
    shutil.rmtree(tmp)
    print("OK test_owner_cr_other_head_after_reviewer_pass_no_wcfy")


def test_pair_owner_cr_other_head_no_arm_twin_era_over():
    """Branch A with extended info: an owner change request on an old head
    names a paired blocker, the twin PASS is fresher than the request. With
    TWIN_ERA=False the paired arm is off: no arm whatever the source of the
    request (comment, review, another head)."""
    owner_body = ("CHANGES REQUESTED (owner): blocker is about the pair, "
                  "twin #154 lacks the guard. Fix there, then re-review "
                  "here.")
    t0 = time.time() - 3 * 3600
    prs = {178: pr_json(
        HEAD178, 3 * 3600,
        comments=[{"body": CR_PAIR + "\n\n## Calibration\n- ok",
                   "createdAt": iso(t0 + 200)},
                  wcfy_comment(3 * 3600)],
        reviews=[{"state": "CHANGES_REQUESTED",
                  "commit": {"oid": OLD291},
                  "submittedAt": iso(t0 + 300),
                  "body": owner_body}]),
           154: pr_json(HEAD154B, 3 * 3600,
                        comments=[{"body": TWIN_PASS + "\n\n## Calibration"
                                             "\n- ok\n\n"
                                             "| Promise | Mutant | Test | Result |\n"
                                             "|---|---|---|---|\n"
                                             "| guard on twin | drop guard | jest #1 | RED |",
                                   "createdAt": iso(t0 + 600)},
                                  wcfy_comment(3 * 3600)])}
    found, calls, tmp = with_scenario(prs)
    made = creates(calls)
    assert made == [], made
    shutil.rmtree(tmp)
    print("OK test_pair_owner_cr_other_head_no_arm_twin_era_over")


# --- Live work inside the PR suppresses the paired arm of a head -----------
# A change request held a single blocker "no e2e IT in the PR itself" while a
# live work card was doing it; the paired arm fired on the owner's approval of
# another PR, the reviewer then occupied a slot for nothing and returned the
# same request. The twin word was matched from a negation line of Calibration
# ("no 1.x twin exists").
HEAD292 = "8e03016de0f86c82e6f2152b13fb985142b4b11d"
HEAD291 = "d4c776a24bec79840b6bb32a131c407a501ffb04"
CR292_R2 = (
    "## Review verdict: CHANGES REQUESTED\n\n"
    "Re-review of head `" + HEAD292 + "` (second pass; the first pass "
    "APPROVE was on `25d39931`). CI 5/5 green on this head.\n\n"
    "### Closed since the owner review\n"
    "1. Owner finding 1 (join by response identity): CLOSED.\n"
    "2. Owner finding 3 (upstream pointer): CLOSED in 22c8a9cf.\n\n"
    "### Open blocker\n\n"
    "[BLOCK] Owner finding 2 is NOT closed on this head: there is no e2e "
    "check (UiIT style) that calls the demo leaking tool and sees "
    "the badge and the APP LOG link. This is acceptance criterion 1 of "
    "#251. Minimal closure: after #291 lands, update this branch, add the "
    "UiIT-style check, CI green on the new head.\n\n"
    "## Calibration\n"
    "- contract: issue #251 acceptance criterion 1 is not met on this head "
    "(no e2e badge test)\n"
    "- mutation: three live mutations on production lines, each reverted\n"
    "- twin: N/A, UI-only PR on the 2.x line, no 1.x twin exists for "
    "issue #251\n"
    "- ui-input: a live badge probe is impossible on this head without the "
    "engine: #291 is not in develop/2.x\n")


def pair292_scenario():
    return {292: pr_json(HEAD292, 8 * 3600,
                         comments=[{"body": CR292_R2,
                                    "createdAt": "2026-11-02T00:18:50Z"}]),
            291: pr_json(HEAD291, 30 * 3600,
                         comments=[{
                             "body": "## Review verdict\nPASS on head `"
                                     + HEAD291 + "` (re-review after CHANGES "
                                       "REQUESTED on `0d272f9`).\n\n"
                                       "## Calibration\n- ok",
                             "createdAt": "2026-11-01T23:45:56Z"},
                            wcfy_comment(30 * 3600)],
                         reviews=[{"state": "APPROVED",
                                   "submittedAt": "2026-11-02T06:52:20Z",
                                   "body": ""}])}


REWORK292 = {"id": "t_f196b7ec", "status": "blocked", "assignee": "coder",
             "priority": 60,
             "title": "[rework] PR #292: e2e IT of the leak badge on the demo "
                      "leaking tool (UiIT)",
             "body": "waits for the merge of #291 (needs_input), then the "
                     "e2e in PR #292 itself"}


def test_live_rework_card_suppresses_pair_rearm():
    """The blocker of the verdict is new work in the PR itself and a live
    rework card exists. With TWIN_ERA=False the paired arm is off, branch A
    is unreachable: no arms and no pairwork findings at all, no suppression
    needed. The scenario stays as a pin of the era."""
    found, calls, tmp = with_scenario(pair292_scenario(), tasks=[REWORK292])
    assert creates(calls) == [], calls
    assert found == [], found
    shutil.rmtree(tmp)
    print("OK test_live_rework_card_suppresses_pair_rearm")


def test_twin_rework_card_does_not_suppress():
    """Negative: the rework card belongs to the TWIN #154. With TWIN_ERA=False
    branch A is off, suppression is not reached: no arms with any rework
    card. The scenario stays as a pin of the era."""
    tasks = [{"id": "t_twinwork", "status": "ready", "assignee": "coder",
              "priority": 60,
              "title": "[rework] PR #154: once-per-death guard",
              "body": "fix in the twin #154, then re-request on #178"}]
    found, calls, tmp = with_scenario(
        pair_scenario("2026-10-31T06:00:00Z", "2026-10-31T07:00:00Z",
                      extra_154=[wcfy_comment(3 * 3600)]), tasks=tasks)
    made = creates(calls)
    assert made == [], made
    shutil.rmtree(tmp)
    print("OK test_twin_rework_card_does_not_suppress")


# --- A PASS on a red head is not a PASS -------------------------------------
# A reviewer verdict "PASS on this PR's diff" on a head with a red MANDATORY
# check (required context of the base protection) was read by branch B as an
# accept and a WCFY card was created for a head waiting for a stack rebuild.
# Mutation recipe:
#   sed -E 's/^([[:space:]]*)if not ci_ok:/\1if False:  # mutant/' \
#     board-watchdog.py > /tmp/mutant-ci.py
#   BOARD_WATCHDOG_SCRIPT=/tmp/mutant-ci.py python3 \
#     test_board_watchdog_review_layer.py
# With the gate off, test_accept_red_mandatory_check_no_wcfy_318,
# test_protection_unreadable_any_red_blocks, test_checks_unreadable_no_arm,
# test_pending_mandatory_check_blocks and test_required_check_missing_no_arm
# turn red (the arm returns); the negatives stay green.
HEAD318 = "b92253f72082aa5d19bcffe43b96e7c024294762"
# The deciding line (first non-empty line under the header) of the verdict.
# The word "approved" in it describes another head, but the parser honestly
# sees an accept: a pin of the wording that fired the arm.
VERDICT_318 = (
    "## Review verdict\n"
    "\n"
    "Reviewed at stack head `%s` (this PR is the top of a stack: "
    "#313 client layer carried at 2e43431b with production code "
    "identical to the approved #313 head 386b5cb, delta is test-only; "
    "#315 UI at 97ae3c41; #317 demo fixture at 819e741f).\n"
    "\n"
    "**Verdict: PASS on this PR's diff.** The stack's merge gate stays "
    "blocked by the pre-existing #315 layout regression; it is not "
    "attributable to this diff.\n"
    "\n"
    "## Calibration\n"
    "- mutation: drop the session-scope guard -> 1/11 red, revert -> green\n"
    "\n"
    "| Promise | Mutant | Test | Result |\n"
    "|---|---|---|---|\n"
    "| session scope | drop the session-scope guard | jest #1 | RED |"
    % HEAD318)
# One mandatory check red, the others green (gh pr checks).
RED_WEBMVC = json.dumps(
    [{"name": n,
      "state": "FAILURE" if n == "e2e web-a (JDK 17)"
      else "SUCCESS"} for n in REQ_2X])


def _pr318():
    return {318: pr_json(HEAD318, 3 * 3600,
                         comments=[{"body": VERDICT_318,
                                    "createdAt": iso(time.time() - 3600)}])}


def test_accept_red_mandatory_check_no_wcfy_318():
    """An accept verdict on a head with a red MANDATORY check does not arm
    WCFY, the wcfyci finding names the red job. Without the gate the quota
    would be wasted."""
    found, calls, tmp = with_scenario(_pr318(), checks={318: RED_WEBMVC})
    made = creates(calls)
    assert made == [], made
    ci = [f for f in found if "wcfyci:318:b92253f" in f[0]]
    assert len(ci) == 1, found
    assert "e2e web-a (JDK 17)=FAILURE" in ci[0][2], ci[0][2]
    shutil.rmtree(tmp)
    print("OK test_accept_red_mandatory_check_no_wcfy_318")


def test_accept_green_ci_arms_wcfy_as_before():
    """Negative: the same verdict on the same head, all mandatory checks
    green: WCFY is armed as before."""
    found, calls, tmp = with_scenario(_pr318())
    made = creates(calls)
    assert len(made) == 1, calls
    key = made[0][made[0].index("--idempotency-key") + 1]
    assert key == "coder-pr318-wcfy-b92253f", key
    assert any(f[0].endswith("wcfy:318:b92253f") for f in found), found
    shutil.rmtree(tmp)
    print("OK test_accept_green_ci_arms_wcfy_as_before")


def test_red_optional_check_does_not_block():
    """Precision of the rule: only a red MANDATORY check blocks. A job
    outside the required contexts is red: the arm works, "CI: N of M green"
    from the reviewer is not a merge gate."""
    prot = {"develop/2.x": json.dumps({"required_status_checks": {
        "contexts": ["build (JDK 17)", "build (JDK 21)"]}})}
    red_opt = json.dumps(
        [{"name": n, "state": "SUCCESS"}
         for n in ("build (JDK 17)", "build (JDK 21)")] +
        [{"name": "e2e web-a (JDK 17)", "state": "FAILURE"}])
    found, calls, tmp = with_scenario(
        _pr318(), protection=prot, checks={318: red_opt})
    assert len(creates(calls)) == 1, calls
    shutil.rmtree(tmp)
    print("OK test_red_optional_check_does_not_block")


def test_protection_unreadable_any_red_blocks():
    """Branch protection cannot be read (network, permission): the mandatory
    set is unknown, so conservatively ALL checks of the head must be green.
    Any red blocks the arm: the error goes only in the direction of "do not
    arm"."""
    found, calls, tmp = with_scenario(
        _pr318(), protection={"develop/2.x": ""}, checks={318: RED_WEBMVC})
    assert creates(calls) == [], calls
    assert any("wcfyci:318:b92253f" in f[0] for f in found), found
    shutil.rmtree(tmp)
    print("OK test_protection_unreadable_any_red_blocks")


def test_checks_unreadable_no_arm():
    """gh pr checks is unavailable: the state of the head is unknown, no arm,
    the next hourly pass repeats (no arming blind)."""
    found, calls, tmp = with_scenario(_pr318(), checks={318: ""})
    assert creates(calls) == [], calls
    assert any("wcfyci:318:b92253f" in f[0] for f in found), found
    shutil.rmtree(tmp)
    print("OK test_checks_unreadable_no_arm")


def test_pending_mandatory_check_blocks():
    """A mandatory check is still running (PENDING): that is not SUCCESS, no
    arm. The next hourly pass sees the final state and arms by itself."""
    pending = json.dumps(
        [{"name": n,
          "state": "PENDING" if n == "e2e web-a (JDK 17)"
          else "SUCCESS"} for n in REQ_2X])
    found, calls, tmp = with_scenario(_pr318(), checks={318: pending})
    assert creates(calls) == [], calls
    assert any("e2e web-a (JDK 17)=PENDING" in f[2] for f in found), (
        found)
    shutil.rmtree(tmp)
    print("OK test_pending_mandatory_check_blocks")


def test_required_check_missing_no_arm():
    """A mandatory check has no run on the head at all (did not start): for
    branch protection that is not green, no arm."""
    partial = json.dumps(
        [{"name": n, "state": "SUCCESS"}
         for n in ("build (JDK 17)", "build (JDK 21)")])
    found, calls, tmp = with_scenario(_pr318(), checks={318: partial})
    assert creates(calls) == [], calls
    assert any("no run" in f[2] for f in found), found
    shutil.rmtree(tmp)
    print("OK test_required_check_missing_no_arm")


def main():
    test_arm_creates_card()
    test_verdict_comment_on_head_no_arm()
    test_full_sha_verdict_no_arm()
    test_formal_approval_no_arm()
    test_formal_changes_requested_on_head_is_verdict()
    test_formal_changes_requested_on_other_sha_still_arms()
    test_coder_handoff_is_not_verdict()
    test_verdict_on_other_sha_still_arms()
    test_live_card_by_key_no_arm()
    test_live_manual_reviewer_card_no_arm()
    test_fresh_head_skipped()
    test_ancient_head_skipped()
    test_cap_five_creates()
    test_park_low_prio_ready_only()
    test_release_when_layer_empty()
    test_gh_failure_silent()
    test_done_card_without_verdict_arms_r2()
    test_done_r2_family_arms_r3()
    test_live_r2_keyed_card_no_arm()
    test_live_pair_key_card_no_arm()
    test_done_card_with_verdict_silent()
    test_verdict_without_calibration_after_cutoff_arms()
    test_verdict_with_calibration_block_no_arm()
    test_old_verdict_without_calibration_grandfathered()
    test_newest_verdict_decides_calibration()
    test_pair_cr_twin_pass_newer_no_arm_twin_era_over()
    test_pair_cr_twin_reject_no_arm()
    test_pair_twin_pass_older_than_cr_no_arm()
    test_pair_cr_without_refs_no_arm()
    test_pair_ref_without_pair_word_no_arm()
    test_pair_rearm_after_done_family_escalates()
    test_pair_live_key_card_no_arm()
    test_pair_live_manual_reviewer_card_no_arm()
    test_pass_without_wcfy_arms_coder_card()
    test_pass_stale_wcfy_arms()
    test_wcfy_rearm_after_done_family_escalates()
    test_wcfy_live_manual_coder_card_no_arm()
    test_formal_cr_newer_than_comment_pass_wins()
    test_comment_pass_newer_than_formal_cr_wins()
    test_pair_wcfy_hold_dry_no_create()
    test_owner_cr_same_head_after_reviewer_pass_no_wcfy()
    test_owner_cr_other_head_after_reviewer_pass_no_wcfy()
    test_pair_owner_cr_other_head_no_arm_twin_era_over()
    test_live_rework_card_suppresses_pair_rearm()
    test_twin_rework_card_does_not_suppress()
    test_accept_red_mandatory_check_no_wcfy_318()
    test_accept_green_ci_arms_wcfy_as_before()
    test_red_optional_check_does_not_block()
    test_protection_unreadable_any_red_blocks()
    test_checks_unreadable_no_arm()
    test_pending_mandatory_check_blocks()
    test_required_check_missing_no_arm()
    print("ALL REVIEW-LAYER TESTS PASSED")


if __name__ == "__main__":
    main()
