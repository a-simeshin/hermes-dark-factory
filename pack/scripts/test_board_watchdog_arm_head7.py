#!/usr/bin/env python3
"""Head named only in the card body, and the verdict form "REQUEST CHANGES".

Why: a rework card named the PR head ONLY in its body ("Rework of PR #N
(branch ..., head 1228c709)"); retiring stale arms looked for the head in
the key and the title and did not see the card. In that case the rework
had been done by a run of the SAME card (it pushed a new head and its own
comment names it), so it must not be retired: hence the shield "the card
claims its own push". A second case: a verdict "**REQUEST CHANGES**, one
merge-order blocker..." parsed as "other", because the reject regex knew
only the passive form "CHANGES REQUESTED"; the CR rework generator did not
see it and created no card.

Rules:
  1. One helper arm_head7: key, title, then body (forms "head <sha>",
     "@ <sha>"). One helper for retiring rework, review and WCFY arms
     (keys without a sha are classified by the prefix reviewer-prN /
     coder-prN-wcfy).
  2. A live head claimed by the card's own text (comments, run summaries,
     the reason of a scheduled parking) is NOT retired: that is its own
     delivery cycle, not a foreign duplicate.
  3. The head is named nowhere + the head changed by an executor commit
     AFTER the card was created + the PR holds an answer to the reject
     verdict = the rework was done by another run: the card is retired.
  4. The reject regex knows both forms: "CHANGES REQUESTED" and
     "REQUEST CHANGES"; a bold verdict token beats the history left of it.

Part D runs in-process mutants of the script text (written to a temp dir
next to copies of its helpers) and requires each to turn the battery red.

Run: python3 test_board_watchdog_arm_head7.py
"""
import importlib.util
import json
import os
import re
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.update({
    "HERMES_HOME": tempfile.mkdtemp(prefix="wd-home-"),
    "FACTORY_REPO": "acme/widget",
    "FACTORY_REPO_PATH": "/srv/hermes/widget",
    "FACTORY_BOARD": "main",
    "FACTORY_OWNER_LOGIN": "owner-login",
    "FACTORY_OWNER_CHAT": "telegram:000000000",
    "FACTORY_BASE_BRANCHES": "main",
})
SCRIPT = os.environ.get("BOARD_WATCHDOG_SCRIPT") or os.path.join(
    HERE, "board-watchdog.py")
SCRIPT_DIR = os.path.dirname(os.path.abspath(SCRIPT))

FAILS = []


def check(name, cond, detail=""):
    print("%s %s%s" % ("ok  " if cond else "FAIL", name,
                       (" :: %s" % (detail,)) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def load_bw(src=None, tag="ah7"):
    """board-watchdog module from the script file or from the mutated text src."""
    import shutil
    sbx = tempfile.mkdtemp(prefix="bw-sbx-arm-head7-")
    path = SCRIPT if src is None else os.path.join(sbx, "wd-mut.py")
    if src is not None:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(src)
        for nb in ("milestone_bands.py", "board_native_guard.py",
                   "factory_config.py", "metrics-evening.py"):
            if os.path.exists(os.path.join(SCRIPT_DIR, nb)):
                shutil.copy(os.path.join(SCRIPT_DIR, nb), os.path.join(sbx, nb))
    sp = importlib.util.spec_from_file_location("bw_mod_%s" % tag, path)
    m = importlib.util.module_from_spec(sp)
    sys.modules["bw_mod_%s" % tag] = m
    sp.loader.exec_module(m)
    m.STATE = os.path.join(sbx, "state.json")
    m.BACKLOG_CACHE = os.path.join(sbx, "backlog.json")
    if hasattr(m, "milestone_bands"):
        m.milestone_bands.CACHE_PATH = os.path.join(sbx, "ms.json")
        m.milestone_bands._MEMO = {}
    return m, sbx


# Head prefixes: PR #407 moved 1228c709 -> 1882991a.
HEAD_407_OLD = "1228c709" + "0" * 32
HEAD_407 = "1882991a" + "0" * 32
HEAD_406 = "b7383008" + "0" * 32
NOW = time.time()


def part_a_helpers(bw):
    """Part A: the arm_head7 helper and the verdict parser."""
    key = "coder-PR407-wildcard-rework"
    title = ("PR #407: remove the wildcard semantics of an empty "
             "clients() (blocker 2, return 2)")
    body = ("Rework of PR #407 (branch hermes/120-client-correlation, "
            "head 1228c709) by the REQUEST CHANGES verdict of round 2.")
    check("A1 head from the card body",
          bw.arm_head7(key, title, body) == "1228c70",
          repr(bw.arm_head7(key, title, body)))
    check("A2 without a body no head is found",
          bw.arm_head7(key, title, "") is None)
    check("A3 form 'head <sha>' in the body",
          bw.arm_head7("", "", "rebase done, head 3816ece fast") == "3816ece")
    check("A4 form '@ <sha>' in the body",
          bw.arm_head7("", "", "the head under review @ b7383008") == "b738300")
    check("A5 the key beats the body",
          bw.arm_head7("coder-pr9-rework-abc1234", "", "head fff0000")
          == "abc1234")
    # Verdict parser: both reject forms, PASS with history stays accept.
    line_408 = ("**Head: `%s`** (base `main`, six commits, no prior "
                "verdict on this PR): **REQUEST CHANGES**, one merge-order "
                "blocker plus one major finding." % HEAD_407_OLD[:9])
    check("A6 'REQUEST CHANGES' = reject",
          bw._verdict_kind(line_408) == "reject",
          bw._verdict_kind(line_408))
    check("A7 'CHANGES REQUESTED' = reject (owner form)",
          bw._verdict_kind("CHANGES REQUESTED: minor (owner)") == "reject")
    check("A8 PASS with a REQUEST CHANGES history = accept",
          bw._verdict_kind(
              "PASS on head `%s` (re-review after REQUEST CHANGES on "
              "`0d272f9`)" % HEAD_407[:9]) == "accept")
    data = {"reviews": [], "comments": [
        {"body": "## Review verdict\n\n%s\n\n### Calibration\n- ok"
                 % line_408,
         "createdAt": time.strftime(
             "%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 7200))}]}
    info = bw._latest_verdict(data, HEAD_407_OLD)
    check("A9 _latest_verdict sees the reject on the exact head",
          info and info.get("kind") == "reject", repr(info))
    # The history "previous verdict REQUEST CHANGES" stands LEFT of the
    # bold **PASS** in the deciding line.
    line_407r3 = ("**Head: `%s`** (base `main`, one rework commit "
                  "on top of the round-2 head `1228c709`; previous "
                  "verdict REQUEST CHANGES, issuecomment-6995622084): "
                  "**PASS**. The standing blocker is closed."
                  % HEAD_407[:9])
    check("A10 PASS with REQUEST CHANGES mentioned to the left: accept",
          bw._verdict_kind(line_407r3) == "accept",
          bw._verdict_kind(line_407r3))
    check("A11 bold **REQUEST CHANGES** = reject",
          bw._verdict_kind("(base x): **REQUEST CHANGES**, blocker.")
          == "reject")


def part_b_lift(bw):
    """Part B: retiring rework arms, the own-push shield, done elsewhere."""
    slug, db, _old = _make_lift_board()
    live = {
        407: {"head": HEAD_407, "pr_state": "OPEN", "head_kind": "reject",
              "head_commit_at": NOW - 1800,
              "head_by_pr_author": True,
              "reply_after_reject": NOW - 1500},
        409: None,   # gh failure: conservatively alive
    }
    calls = []
    real_run, real_repo, real_st = bw.run, bw.board_repo, bw._pr_review_state
    bw.BOARDS = os.path.dirname(os.path.dirname(db))
    bw.run = _make_run(db, calls)
    bw.board_repo = lambda s: "acme/demo"
    bw._pr_review_state = lambda repo, number: live.get(number)
    try:
        bw.rework_priority_lift(slug)
    except Exception as exc:
        FAILS.append("B0: rework_priority_lift crashed: %s: %s"
                     % (type(exc).__name__, exc))
        return
    finally:
        bw.run, bw.board_repo, bw._pr_review_state = \
            real_run, real_repo, real_st
        bw.BOARDS = _old
    con = sqlite3.connect(db)
    state = {tid: con.execute(
        "select status from tasks where id=?", (tid,)).fetchone()
        for tid in ("t_dup", "t_live", "t_headless", "t_own_headless",
                    "t_ghfail")}
    marks = {r[0]: r[1] for r in con.execute(
        "select task_id, body from task_comments where body like ? "
        "or body like ?",
        (bw.REWORK_STALE_MARKER + "%",
         bw.REWORK_DONE_ELSEWHERE_MARKER + "%"))}
    con.close()
    check("B1 foreign push, head in the body: retired as stale",
          state["t_dup"] == ("archived",)
          and any(bw.REWORK_STALE_MARKER in b and "1228c70" in b
                  and "1882991" in b for b in [marks.get("t_dup", "")]),
          str(state.get("t_dup")) + " " + str(marks.get("t_dup", ""))[:80])
    check("B2 own push (the card claims the live head): ALIVE, own-push "
          "shield",
          state["t_live"] == ("scheduled",) and "t_live" not in marks,
          str(state.get("t_live")) + " " + str(marks.get("t_live", ""))[:80])
    check("B3 head named nowhere, the work was done by another run: "
          "retired",
          state["t_headless"] == ("archived",)
          and any(bw.REWORK_DONE_ELSEWHERE_MARKER in b
                  for b in [marks.get("t_headless", "")]),
          str(state.get("t_headless")) + " "
          + str(marks.get("t_headless", ""))[:80])
    check("B4 head not named, but the card claims the live head: ALIVE",
          state["t_own_headless"] == ("ready",)
          and "t_own_headless" not in marks,
          str(state.get("t_own_headless")))
    check("B5 gh failure: conservatively alive",
          state["t_ghfail"] == ("ready",) and "t_ghfail" not in marks,
          str(state.get("t_ghfail")))


def _make_lift_board():
    """Board with the card forms of this test (body/key/comments)."""
    import test_board_watchdog_board_lint as h
    tasks = [
        # Head only in the body, foreign push (the card did not claim the
        # new head) -> stale through the body.
        {"id": "t_dup", "status": "ready", "priority": 90,
         "title": "PR #407: remove the wildcard semantics of an empty "
                  "clients() (blocker 2, return 2)",
         "key": "coder-PR407-wildcard-rework",
         "body": "Rework of PR #407 (branch hermes/120-client-correlation, "
                 "head 1228c709) by the verdict of round 2.",
         "age": 3600},
        # The same form, but a comment of the card's run names the new head
        # (own push) -> shield.
        {"id": "t_live", "status": "scheduled", "priority": 90,
         "title": "PR #407: remove the wildcard semantics of an empty "
                  "clients() (blocker 2, return 2)",
         "key": "coder-PR407-wildcard-rework",
         "body": "Rework of PR #407 (branch hermes/120-client-correlation, "
                 "head 1228c709) by the verdict of round 2.",
         "age": 3600},
        # Head named nowhere, the work was done by another run -> retired.
        {"id": "t_headless", "status": "ready", "priority": 80,
         "title": "PR #407: fix by the verdict of round 2",
         "key": "coder-pr407-rework-verdict-6995622084",
         "body": "Close the items of verdict issuecomment-6995622084 in "
                 "the PR branch.", "age": 3600},
        # Head not named, but the scheduled reason names the live head.
        {"id": "t_own_headless", "status": "ready", "priority": 80,
         "title": "PR #407: fix by the verdict of round 2",
         "key": "coder-pr407-rework-verdict-6995622085",
         "body": "Close the items of verdict issuecomment-6995622085.",
         "age": 3600},
        # gh failure.
        {"id": "t_ghfail", "status": "ready", "priority": 80,
         "title": "PR #409: rework by verdict",
         "key": "coder-pr409-rework-aaa1111", "age": 3600},
    ]
    comments = [
        # t_live: own push.
        ("t_live", "Rework of blocker 2 pushed to the branch of PR #407: "
                   "commit 1882991a (fast-forward on top of 1228c709), CI "
                   "started."),
    ]
    slug, db, _old = h.make_board(tasks=tasks, comments=comments)
    con = sqlite3.connect(db)
    con.execute(
        "insert into task_events (task_id, kind, payload, created_at)"
        " values ('t_own_headless', 'scheduled',"
        " 'SCHEDULED_UNTIL=... CI run 44430248358 on head 1882991a',"
        " ?)", (int(NOW - 600),))
    con.commit()
    con.close()
    return slug, db, os.path.dirname(os.path.dirname(db))


def part_c_arms(bw):
    """Part C: retiring review/WCFY arms whose key has no sha."""
    import test_board_watchdog_board_lint as h
    tasks = [
        # A review arm whose key has no head: the head is in the body.
        {"id": "t_r1", "status": "ready", "assignee": "reviewer",
         "priority": 100,
         "title": "[reviewer] PR #407: review of a head without a verdict",
         "key": "reviewer-pr407-wildcard",
         "body": "Review of the exact head (head 1228c709) for the watchdog arm.",
         "age": 1800},
        # WCFY key without a sha, body "@ <sha>", live head with a fresh WCFY.
        {"id": "t_r2", "status": "ready", "assignee": "coder",
         "priority": 95,
         "title": "[coder] PR #406: fresh \"What changes for you\"",
         "key": "coder-pr406-wcfy",
         "body": "Publish the WCFY for the head @ b7383008.",
         "age": 1800},
        # Control: key without a sha, head named nowhere -> alive.
        {"id": "t_r3", "status": "ready", "assignee": "reviewer",
         "priority": 100,
         "title": "[reviewer] PR #409: head review",
         "key": "reviewer-pr409-open", "body": "a body without a sha",
         "age": 1800},
    ]
    slug, db, _old = h.make_board(tasks=tasks)
    live = {
        407: {"head": HEAD_407, "pr_state": "OPEN", "verdict": False,
              "head_kind": None},
        406: {"head": HEAD_406, "pr_state": "OPEN", "verdict": True,
              "head_kind": "accept", "wcfy_at": NOW - 300, "age": 7200},
    }
    calls = []
    real_run, real_repo, real_st = bw.run, bw.board_repo, bw._pr_review_state
    bw.BOARDS = os.path.dirname(os.path.dirname(db))
    bw.run = _make_run(db, calls)
    bw.board_repo = lambda s: "acme/demo"
    bw._pr_review_state = lambda repo, number: live.get(number)
    try:
        bw.review_arm_liveness_gate(slug)
    except Exception as exc:
        FAILS.append("C0: review_arm_liveness_gate crashed: %s: %s"
                     % (type(exc).__name__, exc))
        return
    finally:
        bw.run, bw.board_repo, bw._pr_review_state = \
            real_run, real_repo, real_st
        bw.BOARDS = _old
    con = sqlite3.connect(db)
    state = {tid: con.execute(
        "select status from tasks where id=?", (tid,)).fetchone()
        for tid in ("t_r1", "t_r2", "t_r3")}
    marks = {r[0] for r in con.execute(
        "select task_id from task_comments where body like ? or body like ?",
        (bw.REVIEW_ARM_STALE_MARKER + "%",
         bw.WCFY_ARM_STALE_MARKER + "%"))}
    con.close()
    check("C1 review arm, key without a sha, the head from the body moved: retired",
          state["t_r1"] == ("archived",) and "t_r1" in marks, str(state))
    check("C2 WCFY arm, key without a sha, '@ <sha>', fresh WCFY: retired",
          state["t_r2"] == ("archived",) and "t_r2" in marks, str(state))
    check("C3 control: head named nowhere -> conservatively alive",
          state["t_r3"] == ("ready",) and "t_r3" not in marks, str(state))


def part_d_mutation():
    """Part D: every mutation must turn the battery red."""
    with open(SCRIPT, encoding="utf-8") as fh:
        src = fh.read()

    def run_mut(name, old, new):
        mut = src.replace(old, new, 1)
        if mut == src:
            FAILS.append("D0: mutant %s did not apply (anchor missing)" % name)
            return
        m, _sbx = load_bw(mut, tag=name)
        before = len(FAILS)
        part_a_helpers(m)
        part_b_lift(m)
        part_c_arms(m)
        new_fails = FAILS[before:]
        if not new_fails:
            FAILS.append("MUT: the battery did not turn red (%s)" % name)
        else:
            # Failures on a mutant are the expected red: they do not go
            # into the final verdict, the line below is enough.
            print("     mutant %s turned red: %s"
                  % (name, "; ".join(new_fails[:4])))
            del FAILS[before:]

    # MUT1: head extraction from the body switched off.
    run_mut("no-body-head",
            "for rx in _REWORK_HEAD_BODY_RES:",
            "for rx in ():")
    # MUT2: the own-push shield switched off: a live delivery cycle is killed.
    run_mut("no-own-claim",
            "if why_stale and live_head7 \\\n"
            "                and _card_claims_head(db, tid, live_head7):",
            "if False and why_stale and live_head7 \\\n"
            "                and _card_claims_head(db, tid, live_head7):")
    # MUT3: the old reject regex (the REQUEST CHANGES form is lost).
    run_mut("old-reject-re",
            'r"(?i)(?:changes\\s+requested|request(?:ed|s)?\\s+changes)")',
            'r"(?i)changes\\s+requested")')
    # MUT4: bold priority switched off: the PASS with history flips into a
    # reject (a false rework arm on a PASS).
    run_mut("no-bold",
            "    bold = _VERDICT_BOLD_RE.search(line)",
            "    bold = None")


def _make_run(db, calls):
    def _run(cmd, timeout=120):
        calls.append(cmd)
        if cmd[:2] == ["gh", "api"]:
            return "[]"
        if len(cmd) > 5 and cmd[1] == "kanban":
            action, tid = cmd[4], cmd[5]
            con = sqlite3.connect(db, timeout=10)
            try:
                if action == "comment":
                    con.execute(
                        "insert into task_comments (task_id, body,"
                        " created_at) values (?,?,?)",
                        (tid, cmd[6], int(time.time())))
                elif action == "archive":
                    con.execute(
                        "update tasks set status='archived' where id=?",
                        (tid,))
                elif action == "edit" and "--priority" in cmd:
                    con.execute(
                        "update tasks set priority=? where id=?",
                        (int(cmd[cmd.index("--priority") + 1]), tid))
                con.commit()
            finally:
                con.close()
        return "ok"
    return _run


def main():
    sys.path.insert(0, HERE)
    bw, _sbx = load_bw()
    print("== part A: helpers ==")
    part_a_helpers(bw)
    print("== part B: retiring rework arms ==")
    part_b_lift(bw)
    print("== part C: review/WCFY arms ==")
    part_c_arms(bw)
    print("== part D: mutations ==")
    part_d_mutation()
    print("\nTOTAL arm_head7: %s" % ("OK" if not FAILS
                                   else "FAIL (%s)" % ", ".join(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
