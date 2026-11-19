#!/usr/bin/env python3
"""Rework of an open PR goes before new work of the same band.

Why: two PRs stood with a CHANGES REQUESTED verdict while dispatcher slots
went to cards opening new PRs. Inside one band the core orders by created_at
(ORDER BY priority DESC, created_at ASC); there is no stock way to order
inside a priority, so new work created earlier overtakes the rework.

Rules:

  A. rework_priority_lift: live ready/todo coder rework cards of an open PR
     (a key with "rework": coder-prN-rework-*, reviewer-PRN-rework-verdict-*;
     a title "rework by verdict" / "Fix PR #N") are raised to
     svc_priority (default 90) inside the active band. Idempotent, never
     lowers, does not touch new work or cards outside the class.
  B. An inactive band: svc below the current priority -> the card is not
     touched.
  C. Creation: the watchdog's _rework_arm_card and pr-hygiene's
     rework_card_priority create the card with 90 right away (svc_priority /
     band_of_ref), not with the band 80.
  D. Queue order after the lift: by the dispatcher query the reworks go
     before new work.

Mutants must turn the battery red: REWORK_LIFT_PRIO=80, default 90->80 in
_rework_arm_card, 90->80 in pr-hygiene rework_card_priority.

Run: python3 test_board_watchdog_rework_priority_lift.py
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
spec = importlib.util.spec_from_file_location("wd_lint_harness_lift", HARNESS)
if spec is None or spec.loader is None:
    print("SKIP: cannot load harness")
    sys.exit(0)
h = importlib.util.module_from_spec(spec)
sys.modules["wd_lint_harness_lift"] = h
spec.loader.exec_module(h)

NOW = time.time()
FAILS = []


def check(name, cond, detail=""):
    print("%s %s%s" % ("ok  " if cond else "FAIL", name,
                       (" :: %s" % (detail,)) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


OPEN_MILESTONES = json.dumps([
    {"title": "2.4.0", "due_on": "2026-12-01T00:00:00Z"},   # active, 80
    {"title": "2.5.0", "due_on": "2027-06-01T00:00:00Z"},   # next, 40
])

BW_PATH = os.path.join(HERE, "board-watchdog.py")
PH_PATH = os.path.join(HERE, "pr-hygiene.py")


def load_bw(src=None, tag="lift"):
    """board-watchdog module from the real file or from the mutated source src."""
    sbx = tempfile.mkdtemp(prefix="bw-sbx-lift-")
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


def make_run(db, calls, milestones=OPEN_MILESTONES):
    """run stub: gh milestones from the fixture, kanban edits are applied to
    the test database (the re-read verification sees the real effect)."""

    def _run(cmd, timeout=120):
        calls.append(cmd)
        if cmd[:2] == ["gh", "api"]:
            return milestones
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
                    if "--priority" in cmd:
                        con.execute(
                            "update tasks set priority=? where id=?",
                            (int(cmd[cmd.index("--priority") + 1]), tid))
                con.commit()
            finally:
                con.close()
        return "ok"

    return _run


def write_admission(sbx, slug, issues, pr_issues, pr_milestones=None):
    """Admission cache of the gate (svc_priority reads exactly this one)."""
    with open(os.path.join(sbx, slug, "admission-approved.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"updated": "test", "issues": issues,
                   "prs": sorted(int(n) for n in pr_issues),
                   "pr_issues": pr_issues,
                   "pr_milestones": pr_milestones or {}}, fh)


INCIDENT_TASKS = [
    # the incident shape: rework of #397 (an arm of the rework generator), ready p80
    {"id": "t_rw", "status": "ready", "priority": 80,
     "title": "[coder] PR #397 (head 7b6f2f9): rework by verdict "
              "(watchdog auto-arm)",
     "key": "coder-pr397-rework-7b6f2f9", "assignee": "coder",
     "age": 13 * 3600},
    # a reviewer fix for #398: the key is NOT of the coder-prN-rework family
    {"id": "t_fix", "status": "ready", "priority": 80,
     "title": "Fix PR #398 (head 30c4754): Apps flow hangs after "
              "sandbox-proxy-restart",
     "key": "reviewer-PR398-rework-verdict-6976691960",
     "assignee": "coder", "age": 8 * 3600},
    # new big work, created EARLIER than the reworks (the core of the incident)
    {"id": "t_new", "status": "ready", "priority": 80,
     "title": "[coder] issue #120 C1: client traffic capture core",
     "key": "coder-120-core-capture", "assignee": "coder",
     "age": 15 * 3600},
    # a running rework: the slot is taken already, no priority needed
    {"id": "t_run", "status": "running", "priority": 80,
     "title": "[coder] PR #401 (head aaa1111): rework by verdict",
     "key": "coder-pr401-rework-aaa1111", "assignee": "coder",
     "age": 3600},
    # a WCFY card: another class, the priority is 95 anyway
    {"id": "t_wcfy", "status": "ready", "priority": 95,
     "title": "[coder] PR #402 WCFY after PASS",
     "key": "coder-pr402-wcfy-bbb2222", "assignee": "coder",
     "age": 7200},
    # rework of a PR of an inactive band (issue of 2.5.0 -> band 40)
    {"id": "t_low", "status": "ready", "priority": 80,
     "title": "[coder] PR #410 (head ccc3333): rework by verdict",
     "key": "coder-pr410-rework-ccc3333", "assignee": "coder",
     "age": 6 * 3600},
]


def battery(mod, tag="lift"):
    """A (lift + class + idempotency), B (inactive band), C (creation with
    90), D (queue order). Returns a list of problems."""
    problems = []

    def want(name, cond, detail=""):
        print("%s %s%s" % ("ok  " if cond else "FAIL", name,
                           (" :: %s" % (detail,)) if detail and not cond
                           else ""))
        if not cond:
            problems.append(name)

    slug, db, _ = h.make_board(tasks=INCIDENT_TASKS)
    mod.BOARDS = os.path.dirname(os.path.dirname(db))
    write_admission(mod.BOARDS, slug,
                    issues={"500": "2.4.0", "501": "2.4.0", "510": "2.5.0"},
                    pr_issues={"397": ["500"], "398": ["501"],
                               "401": ["500"], "410": ["510"]})
    calls = []
    real_run = mod.run
    mod.run = make_run(db, calls)
    mod.board_repo = lambda s: "acme/demo"
    try:
        found = mod.rework_priority_lift(slug)
    except Exception as exc:
        problems.append("A0: rework_priority_lift crashed: %s: %s"
                        % (type(exc).__name__, exc))
        return problems
    finally:
        mod.run = real_run

    con = sqlite3.connect(db)
    prio = {tid: (con.execute("select priority from tasks where id=?",
                              (tid,)).fetchone() or [None])[0]
            for tid in ("t_rw", "t_fix", "t_new", "t_run", "t_wcfy",
                        "t_low")}
    # --- A: lift inside the active band -------------------------------
    want("A1 rework of #397 (key coder-prN-rework) 80 -> 90",
         prio["t_rw"] == 90, "prio=%s" % prio)
    want("A2 fix of #398 (key reviewer-PRN-rework-verdict) 80 -> 90",
         prio["t_fix"] == 90, "prio=%s" % prio)
    want("A3 new work #120 untouched: 80",
         prio["t_new"] == 80, "prio=%s" % prio)
    want("A4 running rework untouched",
         prio["t_run"] == 80 and prio["t_wcfy"] == 95, "prio=%s" % prio)
    edit_rw = [c for c in calls if "t_rw" in c and "edit" in c]
    want("A5 exactly one edit of t_rw with --priority 90",
         len(edit_rw) == 1 and "--priority" in edit_rw[0]
         and "90" in edit_rw[0], str(edit_rw))
    marks = {r[0] for r in con.execute(
        "select task_id from task_comments where body like ?",
        (mod.REWORK_LIFT_MARKER + "%",))}
    want("A6 marker comment on both raised cards",
         marks == {"t_rw", "t_fix"}, str(marks))
    lifted = [f for f in found if "t_rw" in f[0] or "t_fix" in f[0]]
    want("A7 findings for both cards", len(lifted) == 2, str(found))

    # --- idempotency ----------------------------------------------------
    calls2 = []
    mod.run = make_run(db, calls2)
    try:
        mod.rework_priority_lift(slug)
    finally:
        mod.run = real_run
    want("A8 a repeated pass is silent (idempotency)",
         not [c for c in calls2 if "edit" in c], str(calls2))

    # --- B: an inactive band is not touched -------------------------------
    want("B1 PR #410 in band 40 is neither raised nor lowered",
         prio["t_low"] == 80, "prio=%s" % prio)

    # --- D: queue order by the dispatcher query --------------------------
    order = [r[0] for r in con.execute(
        "select id from tasks where status = 'ready' and assignee = 'coder'"
        " order by priority desc, created_at asc")]
    con.close()
    want("D1 reworks of #397/#398 before new work #120",
         order.index("t_rw") < order.index("t_new")
         and order.index("t_fix") < order.index("t_new"), str(order))

    # --- C: created with 90 right away -------------------------------------
    made = {}
    real_create = mod._kanban_create

    def fake_create(slug_, title, body, assignee, priority, key,
                    skills=(), guard=None):
        made[key] = priority
        return "t_made"

    mod._kanban_create = fake_create
    try:
        mod._rework_arm_card(slug, 397, "7b6f2f9abcdef", {"at": NOW})
    except Exception as exc:
        problems.append("C0: _rework_arm_card crashed: %s: %s"
                        % (type(exc).__name__, exc))
    finally:
        mod._kanban_create = real_create
    want("C1 _rework_arm_card creates with priority 90 (active band)",
         made.get("coder-pr397-rework-7b6f2f9") == 90, str(made))
    return problems


def ph_battery(ph_mod):
    """C continued: pr-hygiene rework_card_priority."""
    problems = []

    class _MB(object):
        ACTIVE_BAND = 80

        def __init__(self, band):
            self.band = band

        def band_of_ref(self, repo, number, is_pr=False, fetch=None,
                        gh_exec=None):
            return self.band

    real_mb = ph_mod.milestone_bands
    try:
        ph_mod.milestone_bands = _MB(None)
        p_none = ph_mod.rework_card_priority(397)
        ph_mod.milestone_bands = _MB(80)
        p_active = ph_mod.rework_card_priority(397)
        ph_mod.milestone_bands = _MB(40)
        p_next = ph_mod.rework_card_priority(397)
    finally:
        ph_mod.milestone_bands = real_mb
    ok = (p_none == 90 and p_active == 90 and p_next == 40)
    print("%s C2 pr-hygiene: no data 90, active 90, next 40 "
          "(%s/%s/%s)" % ("ok  " if ok else "FAIL", p_none, p_active,
                          p_next))
    if not ok:
        problems.append("C2 pr-hygiene rework_card_priority")
    return problems


def main():
    mod, _ = load_bw()
    problems = battery(mod)
    src = open(BW_PATH, encoding="utf-8").read()

    # mutant 1: the lift is off (target 80 = the current band)
    mut = src.replace("REWORK_LIFT_PRIO = 90", "REWORK_LIFT_PRIO = 80")
    assert mut != src, "mutant 1 was not applied"
    m1, _ = load_bw(mut, tag="m1")
    if not battery(m1, tag="m1"):
        problems.append("MUT1: the battery did not turn red (REWORK_LIFT_PRIO=80)")

    # mutant 2: arm creation went back to band 80
    mut = src.replace("svc_priority(slug, number, 90), key,",
                      "svc_priority(slug, number, 80), key,")
    assert mut != src, "mutant 2 was not applied"
    m2, _ = load_bw(mut, tag="m2")
    if not battery(m2, tag="m2"):
        problems.append("MUT2: the battery did not turn red (_rework_arm_card 80)")

    # pr-hygiene
    ph_spec = importlib.util.spec_from_file_location("pr_hygiene_lift",
                                                     PH_PATH)
    ph = importlib.util.module_from_spec(ph_spec)
    ph_spec.loader.exec_module(ph)
    problems += ph_battery(ph)
    ph_src = open(PH_PATH, encoding="utf-8").read()
    mut = re.sub(r"(def rework_card_priority.*?)return 90",
                 r"\1return 80", ph_src, count=1, flags=re.S)
    assert mut != ph_src, "mutant 3 was not applied"
    sbx = tempfile.mkdtemp(prefix="ph-mut-lift-")
    mut_path = os.path.join(sbx, "pr-hygiene.py")
    with open(mut_path, "w", encoding="utf-8") as fh:
        fh.write(mut)
    sp = importlib.util.spec_from_file_location("pr_hygiene_lift_m", mut_path)
    phm = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(phm)
    if not ph_battery(phm):
        problems.append("MUT3: the pr-hygiene battery did not turn red (90->80)")

    print("\nRESULT rework priority lift: %s" % ("OK" if not problems
                                   else "FAIL %s" % "; ".join(problems)))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
