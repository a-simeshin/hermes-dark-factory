#!/usr/bin/env python3
"""A chain whose issue was closed as a duplicate is rebound, not retired.

Why: an issue was closed as a duplicate of another open issue ("Duplicate
of #N") while a chain of six cards lived with its anchor on the closed
number. Two defects of the layer:

  1. The band. The admission gate computed the band from ANY #N in the
     body, including side mentions of PRs: a PR of the active milestone
     mentioned in the prose lifted a chain of the next milestone to the
     top band, and its coder link took a slot of the release band.
  2. Retire instead of rebind. The tail gate sees the anchors correctly
     (the Issue: line, not prose), but a close-as-duplicate meant the death
     of the work for it: after the grace period it would retire the whole
     chain.

Rules:
  A. A card's anchor and band come from its OWN issue: #N in the title or
     the "Issue:" line (plus the same anchors of its ancestors: a chain
     inherits the root's issue). A side #N in prose gives no band. One
     anchor definition for all gates: closed_tail_anchor_refs;
     milestone_bands reads only Refs/Closes for a PR.
  B. Closing an issue with "Duplicate of #N" where #N is OPEN rebinds the
     chain to the original (closed_duplicate_rebind): titles and Issue:
     lines are rewritten #duplicate -> #original, a marker on every link,
     statuses and runs are untouched. A closed original or no duplicate
     link -> the tail gate semantics apply.
  C. The rebind grace equals the tail gate's: one hour.

Checks: A (band), B (rebind, idempotency, dead original, grace), Z
(wiring in main). Mutants must turn the battery red, and a copy of the
source with all fixes reverted (an in-process mutant) must be red too.

Run: python3 test_board_watchdog_closed_duplicate_rebind.py
"""
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.join(HERE, "test_board_watchdog_board_lint.py")
spec = importlib.util.spec_from_file_location("wd_lint_harness_dup_rebind", HARNESS)
if spec is None or spec.loader is None:
    print("SKIP: cannot load harness")
    sys.exit(0)
h = importlib.util.module_from_spec(spec)
sys.modules["wd_lint_harness_dup_rebind"] = h
spec.loader.exec_module(h)

NOW = time.time()
FAILS = []


def check(name, cond, detail=""):
    print("%s %s%s" % ("ok  " if cond else "FAIL", name,
                       (" :: %s" % (detail,)) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


OPEN_MILESTONES = json.dumps([
    {"title": "1.0.0", "due_on": "2026-11-22T00:00:00Z"},
    {"title": "1.1.0", "due_on": "2026-12-20T00:00:00Z"},
])

# --- A: band fixtures (the chain after the rebind) ------------------------
# #385 is open in 1.1.0 (band 40), #383 is open in 1.0.0 (80), PR #384 is
# open and Refs #383; nothing is merged.
ISSUES_A = json.dumps([
    {"number": 385, "milestone": {"title": "1.1.0"}, "labels": [],
     "body": "demo E2E flake", "updatedAt": _iso(NOW - 3600)},
    {"number": 383, "milestone": {"title": "1.0.0"}, "labels": [],
     "body": "Flaky ProxyTests", "updatedAt": _iso(NOW - 3600)},
])
PRS_A = json.dumps([
    {"number": 384, "body": "Refs #383 (test-only change in core)",
     "milestone": None, "latestReviews": []},
])
MERGED_A = json.dumps([])

BW_PATH = os.path.join(HERE, "board-watchdog.py")


def load_bw(src=None, tag="dup_rebind"):
    """The board-watchdog module from the real file or from the mutated src."""
    sbx = tempfile.mkdtemp(prefix="bw-sbx-dup-rebind-")
    path = BW_PATH if src is None else os.path.join(sbx, "wd-mut.py")
    if src is not None:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(src)
        for nb in ("milestone_bands.py", "board_native_guard.py"):
            shutil.copy(os.path.join(HERE, nb), os.path.join(sbx, nb))
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


def make_run(db, issues, prs, merged, calls):
    """A run() stub: gh answers from fixtures, kanban edit/comment/
    schedule/archive are applied to the test database."""

    def _run(cmd, timeout=120):
        calls.append(cmd)
        if cmd[:2] == ["gh", "api"]:
            return OPEN_MILESTONES
        if cmd[:2] == ["gh", "pr"] and "merged" in cmd:
            return merged
        if cmd[:2] == ["gh", "pr"]:
            return prs
        if cmd[:2] == ["gh", "issue"]:
            return issues
        if len(cmd) > 5 and cmd[1] == "kanban":
            action, tid = cmd[4], cmd[5]
            con = sqlite3.connect(db, timeout=10)
            try:
                if action == "comment":
                    con.execute(
                        "insert into task_comments (task_id, body,"
                        " created_at) values (?,?,?)",
                        (tid, cmd[6], int(time.time())))
                elif action == "edit":
                    if "--title" in cmd:
                        con.execute(
                            "update tasks set title=? where id=?",
                            (cmd[cmd.index("--title") + 1], tid))
                    if "--body" in cmd:
                        con.execute(
                            "update tasks set body=? where id=?",
                            (cmd[cmd.index("--body") + 1], tid))
                    if "--priority" in cmd:
                        con.execute(
                            "update tasks set priority=? where id=?",
                            (int(cmd[cmd.index("--priority") + 1]), tid))
                elif action == "schedule":
                    con.execute(
                        "update tasks set status='scheduled' where id=?",
                        (tid,))
                elif action == "archive":
                    con.execute(
                        "update tasks set status='archived' where id=?",
                        (tid,))
                con.commit()
            finally:
                con.close()
        return "ok"

    return _run


def battery(mod):
    """Battery A/B/Z on the module mod. Returns the list of problems."""
    problems = []

    def want(name, cond, detail=""):
        if not cond:
            problems.append("%s :: %s" % (name, detail))

    # --- A: the band comes from the card's own issue, not from prose ------
    slug, db, _ = h.make_board(tasks=[
        # a 1.1.0 card with prose about PR #384 (Refs #383)
        {"id": "t_ch", "status": "ready", "priority": 80,
         "title": "Flaky-e2e: ResourceRacePaneUiIT (issue #385)",
         "body": "Issue: #385\nIncidents: prep #381, PR #384 run"
                 " 44011074939 attempts 1 and 3; neighbour race #383;"
                 " guarantee #357/#344",
         "assignee": "coder", "created_by": "user"},
        # a service card: the PR in the TITLE (anchor) -> the service band lives
        {"id": "t_rev", "status": "ready", "priority": 42,
         "title": "[reviewer] PR #384 (head abc1234): review of the fix",
         "body": "review PR #384", "assignee": "reviewer",
         "created_by": "user"},
        # a neighbour: its own 1.0.0 issue, band 80 already set -> untouched
        {"id": "t_solo", "status": "ready", "priority": 80,
         "title": "neighbour work", "body": "Issue: #383",
         "assignee": "coder", "created_by": "user"},
    ])
    mod.BOARDS = os.path.dirname(os.path.dirname(db))
    calls = []
    real_run = mod.run
    mod.run = make_run(db, ISSUES_A, PRS_A, MERGED_A, calls)
    mod.board_repo = lambda s: "acme/widget"
    try:
        mod.admission_gate(slug)
    except Exception as exc:
        problems.append("A: admission_gate failed: %s: %s"
                        % (type(exc).__name__, exc))
    finally:
        mod.run = real_run
    con = sqlite3.connect(db)
    prio = {tid: con.execute("select priority from tasks where id=?",
                             (tid,)).fetchone()[0]
            for tid in ("t_ch", "t_rev", "t_solo")}
    status = {tid: con.execute("select status from tasks where id=?",
                               (tid,)).fetchone()[0]
              for tid in ("t_ch", "t_rev", "t_solo")}
    con.close()
    want("A1 the band from the own issue: 80 -> 40, not 80 from the prose PR #384",
         prio["t_ch"] == 40, "prio=%s" % prio)
    want("A2 the service band by the PR in the title lives: 42 -> 80",
         prio["t_rev"] == 80, "prio=%s" % prio)
    want("A3 the 1.0.0 card with its own band is untouched",
         prio["t_solo"] == 80, "prio=%s" % prio)
    want("A4 nobody is parked", all(s == "ready" for s in status.values()),
         str(status))
    edit_ch = [c for c in calls if "t_ch" in c and "edit" in c]
    want("A5 exactly one edit of t_ch (the priority only)",
         len(edit_ch) == 1 and "--priority" in edit_ch[0]
         and "40" in edit_ch[0], str(edit_ch))

    # --- B: rebinding a duplicate ----------------------------------------
    if not hasattr(mod, "closed_duplicate_rebind"):
        problems.append("B0: closed_duplicate_rebind is missing"
                        " (code predates the rule)")
        return problems
    slug, db, _ = h.make_board(tasks=[
        {"id": "t_tri", "status": "todo", "priority": 80,
         "title": "Flaky-e2e: X (issue #386)",
         "body": "Issue: #386\nincidents: prep #381, PR #384 run 4401",
         "assignee": "architect"},
        {"id": "t_code", "status": "running", "priority": 80,
         "title": "Root-cause flake and determinize the IT",
         "body": "Goal: ... (issue #386). Incidents: PR #384 run 4401;"
                 " guarantee #357/#344.", "assignee": "coder"},
        {"id": "t_apply", "status": "todo", "priority": 80,
         "title": "Apply determinized IT + policy for #386",
         "body": "Goal: apply the decision on #386 to the IT.",
         "assignee": "coder"},
        {"id": "t_solo", "status": "todo", "priority": 40,
         "title": "neighbour work", "body": "Issue: #383",
         "assignee": "coder"},
    ], links=[("t_tri", "t_code"), ("t_tri", "t_apply")])

    def fetch_stub(closed, opens):
        return lambda now: (dict(closed), set(opens))

    dup_stub = lambda n: 385 if n == 386 else None
    mod.BOARDS = os.path.dirname(os.path.dirname(db))
    calls = []
    mod.run = make_run(db, "[]", "[]", "[]", calls)
    try:
        findings = mod.closed_duplicate_rebind(
            slug, apply=True, now=NOW,
            fetch=fetch_stub({386: NOW - 7200}, {385, 383}),
            dup_of=dup_stub)
    except Exception as exc:
        findings = []
        problems.append("B: closed_duplicate_rebind failed: %s: %s"
                        % (type(exc).__name__, exc))
    finally:
        mod.run = real_run
    keys = [k for k, _r, _t in findings]
    con = sqlite3.connect(db)
    rows = {tid: con.execute("select title, body, status from tasks"
                             " where id=?", (tid,)).fetchone()
            for tid in ("t_tri", "t_code", "t_apply", "t_solo")}
    marks = con.execute(
        "select count(*) from task_comments where task_id='t_tri' and"
        " body like 'closed-dup-rebind:%'").fetchone()[0]
    con.close()

    want("B1 the root anchor is rebound: title+Issue: #385, the prose #384 lives",
         "#385" in rows["t_tri"][0] and "#386" not in rows["t_tri"][0]
         and "Issue: #385" in rows["t_tri"][1]
         and "#384" in rows["t_tri"][1], str(rows["t_tri"]))
    want("B2 the anchor in the child title is rebound",
         "#385" in rows["t_apply"][0]
         and "#386" not in rows["t_apply"][0], str(rows["t_apply"]))
    want("B3 statuses untouched, nothing archived",
         rows["t_tri"][2] == "todo" and rows["t_code"][2] == "running"
         and rows["t_apply"][2] == "todo", str({k: v[2] for k, v in
                                                rows.items()}))
    want("B4 the prose of the running link is not rewritten (no anchors)",
         "(issue #386)" in rows["t_code"][1], str(rows["t_code"]))
    want("B5 the neighbour card is untouched",
         not any("t_solo" in " ".join(map(str, c)) for c in calls),
         str(calls))
    want("B6 the rebind marker is on the root", marks == 1, str(marks))
    want("B7 closedtail-rebind findings on every chain link",
         all(any(k == "closedtail-rebind:%s:%s" % (slug, t)
                 for k in keys)
             for t in ("t_tri", "t_code", "t_apply")), str(keys))
    want("B8 the neighbour outside the chain has no finding",
         not any("t_solo" in k for k in keys), str(keys))

    # idempotency
    calls2 = []
    mod.run = make_run(db, "[]", "[]", "[]", calls2)
    try:
        findings2 = mod.closed_duplicate_rebind(
            slug, apply=True, now=NOW,
            fetch=fetch_stub({386: NOW - 7200}, {385, 383}),
            dup_of=dup_stub)
    finally:
        mod.run = real_run
    want("B9 the second run is silent and does not mutate",
         not calls2 and not findings2,
         "calls=%d findings=%s" % (len(calls2), findings2))

    # the original is closed -> no rebind, the tail gate works
    slug, db, _ = h.make_board(tasks=[
        {"id": "t_tri2", "status": "todo", "priority": 80,
         "title": "Flaky-e2e: X (issue #386)", "body": "Issue: #386",
         "assignee": "architect"},
    ])
    mod.BOARDS = os.path.dirname(os.path.dirname(db))
    calls3 = []
    mod.run = make_run(db, "[]", "[]", "[]", calls3)
    try:
        quiet = mod.closed_duplicate_rebind(
            slug, apply=True, now=NOW,
            fetch=fetch_stub({386: NOW - 7200, 385: NOW - 7200}, {383}),
            dup_of=dup_stub)
        tail_gate = mod.closed_issue_tail_gate(
            slug, apply=True, now=NOW,
            fetch=fetch_stub({386: NOW - 7200, 385: NOW - 7200}, {383}))
    finally:
        mod.run = real_run
    con = sqlite3.connect(db)
    st_tail = con.execute("select status from tasks where id='t_tri2'"
                       ).fetchone()[0]
    con.close()
    want("B10 a dead original: the rebind is silent, the tail gate retires",
         not quiet and st_tail == "archived" and tail_gate,
         "quiet=%s st=%s" % (quiet, st_tail))

    # grace: the duplicate was closed 5 minutes ago -> no rebind
    slug, db, _ = h.make_board(tasks=[
        {"id": "t_tri3", "status": "todo", "priority": 80,
         "title": "Flaky-e2e: X (issue #386)", "body": "Issue: #386",
         "assignee": "architect"},
    ])
    mod.BOARDS = os.path.dirname(os.path.dirname(db))
    calls4 = []
    mod.run = make_run(db, "[]", "[]", "[]", calls4)
    try:
        early = mod.closed_duplicate_rebind(
            slug, apply=True, now=NOW,
            fetch=fetch_stub({386: NOW - 300}, {385, 383}),
            dup_of=dup_stub)
    finally:
        mod.run = real_run
    want("B11 closed 5 minutes ago -> no retire and no rebind",
         not early and not calls4,
         "early=%s calls=%d" % (early, len(calls4)))
    return problems


BW_MUTANTS = {
    "band-from-issue-prose": (
        "band = band_of(n for n in approved if n in anchor)",
        "band = band_of(approved)"),
    "svc-band-from-prose": (
        "if n not in anchor:\n"
        "                    # a side PR from prose gives no band\n"
        "                    continue",
        "if False:\n"
        "                    # a side PR from prose gives no band\n"
        "                    continue"),
    "rebind-disabled": (
        "if t and t in opens:\n"
        "                pairs[n] = t",
        "if False:\n"
        "                pairs[n] = t"),
    "rebind-without-grace": (
        "and CLOSED_TAIL_GRACE_S < now - closed[n]",
        "and 0 < now - closed[n]"),
}


def main():
    bw_src = open(BW_PATH, encoding="utf-8").read()
    for name, (old, _new) in BW_MUTANTS.items():
        if bw_src.count(old) != 1:
            print("FAIL: the anchor of mutant %s occurs %d times"
                  % (name, bw_src.count(old)))
            return 1
    if "closed_duplicate_rebind(slug)" not in bw_src:
        print("FAIL: main() does not call closed_duplicate_rebind (wiring)")
        return 1

    mod, _ = load_bw()
    problems = battery(mod)
    for p in problems:
        print("FAIL %s" % p)
    if problems:
        print("\nRED: clean run, problems %d" % len(problems))
        return 1
    print("OK: clean run (A band, B rebind)")

    failed = 0
    for name, (old, new) in BW_MUTANTS.items():
        mut = load_bw(bw_src.replace(old, new, 1), tag=name)[0]
        try:
            probs = battery(mut)
        except Exception as exc:
            probs = ["failed: %s: %s" % (type(exc).__name__, exc)]
        if probs:
            print("OK   %s: the mutant is red (%s)"
                  % (name, " | ".join(p[:70] for p in probs[:3])))
        else:
            print("FAIL %s: the mutant survived" % name)
            failed += 1

    # Red before the fix: the code before the fix is rebuilt in-process as
    # a copy of the source with all fixes reverted (every BW_MUTANTS pair
    # at once), never from git history.
    refix_bw = bw_src
    for _n, (old, new) in BW_MUTANTS.items():
        refix_bw = refix_bw.replace(old, new, 1)
    mut = load_bw(refix_bw, tag="refix_dup_rebind")[0]
    try:
        probs = battery(mut)
    except Exception as exc:
        probs = ["failed: %s: %s" % (type(exc).__name__, exc)]
    if probs:
        print("OK   the fix rollback (code before the fix, mutant) is red: %s"
              % probs[0][:80])
    else:
        print("FAIL the fix rollback is green: the test is blind before the fix")
        failed += 1

    if failed:
        print("\nTOTAL: FAIL, problems %d" % failed)
        return 1
    print("\nTOTAL: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
