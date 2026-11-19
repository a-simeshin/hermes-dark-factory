#!/usr/bin/env python3
"""An issue counts as covered by a live card only through an anchor reference.

Why: an issue of the active milestone sat without an executor because a
summary card mentioned it loosely ("#800/#801" in its title). The executor
poll, the active-milestone orphans report and the admission no-executor
finding all counted that mention as a live executor. Separately, the
keep-open label hid an issue from pr-hygiene issues_without_cards, although
it only means "do not close automatically".

Checks:
  A. issue_executor_poll: a loose mention does not stop the poll from
     creating an executor; an anchored live card ("issue #802") does.
  B. active_milestone_orphans: a loose mention does not hide an orphan; an
     anchored card does.
  C. admission_gate: the no-executor finding fires despite a loose mention.
  D. executor_cover_refs: anchors versus loose mentions.
  E. pr-hygiene issues_without_cards: keep-open no longer hides an issue,
     wait-for-triage still does.

Red before green: mutants inside the test revert the helper to the loose
"#N" regex, revert the admission check to live_refs, and put keep-open back
into the pr-hygiene skip set; each must turn the battery red.

Run: python3 test_board_watchdog_executor_anchor_cover.py
"""
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.join(HERE, "test_board_watchdog_board_lint.py")
spec = importlib.util.spec_from_file_location("wd_lint_harness_anchor",
                                              HARNESS)
if spec is None or spec.loader is None:
    print("SKIP: cannot load harness")
    sys.exit(0)
h = importlib.util.module_from_spec(spec)
sys.modules["wd_lint_harness_anchor"] = h
spec.loader.exec_module(h)

os.environ["FACTORY_OWNER_LOGIN"] = "owner-login"
os.environ["FACTORY_BOT_LOGINS"] = "widget-bot"

BW_PATH = os.environ.get("BOARD_WATCHDOG_SCRIPT") or os.path.join(
    HERE, "board-watchdog.py")
PH_PATH = os.path.join(HERE, "pr-hygiene.py")
SIBLINGS = ("factory_config.py", "milestone_bands.py",
            "board_native_guard.py")
NOW = time.time()
FAILS = []


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


OPEN_MILESTONES = json.dumps([
    {"title": "1.3.0", "due_on": "2026-12-20T00:00:00Z"},
    {"title": "1.4.0", "due_on": "2027-01-31T00:00:00Z"},
])
# #801 is mentioned loosely by a summary card; #802 has an anchored live
# card; #803 is under triage.
ISSUES_POLL = json.dumps([
    {"number": 801, "milestone": {"title": "1.3.0"},
     "labels": [{"name": "hermes"}], "body": "Show version badge",
     "updatedAt": _iso(NOW - 10 * 60)},
    {"number": 802, "milestone": {"title": "1.3.0"},
     "labels": [], "body": "x", "updatedAt": _iso(NOW - 10 * 60)},
    {"number": 803, "milestone": {"title": "1.3.0"},
     "labels": [{"name": "wait-for-triage"}], "body": "x",
     "updatedAt": _iso(NOW - 10 * 60)},
])
# Admission: the same #801, older than the 24 h no-executor threshold.
ISSUES_ADM = json.dumps([
    {"number": 801, "milestone": {"title": "1.3.0"},
     "labels": [], "body": "Show version badge",
     "updatedAt": _iso(NOW - 3 * 86400)},
])
VIEWS = {
    "801": {"number": 801, "title": "Show version badge", "body": "x",
            "author": {"login": "owner-login"}},
}
BOARD_TASKS = [
    {"id": "t_summary", "status": "todo", "priority": 80,
     "assignee": "architect", "created_by": "auto-decomposer",
     "title": "Summarize readiness verdict and priorities #800/#801",
     "body": "Goal: a verdict. Approach: a recommendation on #801."},
    {"id": "t_work", "status": "ready", "priority": 80,
     "assignee": "coder", "created_by": "watchdog",
     "title": "[coder] issue #802", "body": "Refs: #802 work"},
]


def load_bw(src=None, tag="green"):
    sbx = tempfile.mkdtemp(prefix="bw-sbx-anchor-")
    path = BW_PATH
    if src is not None:
        path = os.path.join(sbx, "wd-mut.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(src)
        for nb in SIBLINGS:
            shutil.copy(os.path.join(HERE, nb), os.path.join(sbx, nb))
    sp = importlib.util.spec_from_file_location("bw_anchor_%s" % tag, path)
    m = importlib.util.module_from_spec(sp)
    sys.modules["bw_anchor_%s" % tag] = m
    sp.loader.exec_module(m)
    m.STATE = os.path.join(sbx, "state.json")
    m.BACKLOG_CACHE = os.path.join(sbx, "backlog.json")
    if hasattr(m, "milestone_bands"):
        m.milestone_bands.CACHE_PATH = os.path.join(sbx, "ms.json")
        m.milestone_bands._MEMO = {}
    return m


def make_env(m, issues):
    slug, db, _ = h.make_board(tasks=BOARD_TASKS)
    m.BOARDS = os.path.dirname(os.path.dirname(db))
    created = {}

    def fake_run(cmd, timeout=120):
        if cmd[:2] == ["gh", "api"]:
            url = cmd[2] if len(cmd) > 2 else ""
            if "state=closed" in url or "/timeline" in url \
                    or "/events" in url:
                return "[]"
            return OPEN_MILESTONES
        if cmd[:2] == ["gh", "issue"] and "list" in cmd:
            return issues
        if cmd[:2] == ["gh", "issue"] and "view" in cmd:
            return json.dumps(VIEWS.get(cmd[3], {}))
        if cmd[:2] == ["gh", "pr"] and "list" in cmd:
            return "[]"
        if len(cmd) > 4 and cmd[1] == "kanban" and cmd[4] == "create":
            body = open(cmd[cmd.index("--body-file") + 1],
                        encoding="utf-8").read()
            key = cmd[cmd.index("--idempotency-key") + 1]
            tid = "t_new_%s" % key
            created[key] = tid
            con = sqlite3.connect(db, timeout=10)
            con.execute(
                "insert into tasks (id, title, body, status, priority,"
                " assignee, created_by, created_at, idempotency_key)"
                " values (?,?,?,'ready',?,'coder','watchdog',?,?)",
                (tid, cmd[5], body, int(cmd[cmd.index("--priority") + 1]),
                 int(time.time()), key))
            con.commit()
            con.close()
            return json.dumps({"id": tid})
        return "ok"

    m.run = fake_run
    m.board_repo = lambda s: "acme/demo"
    return slug, created


def battery(m):
    problems = []

    def guard(label, fn):
        try:
            return fn()
        except Exception as exc:
            problems.append("%s crashed: %s: %s"
                            % (label, type(exc).__name__, exc))
            return None

    # A: the poll
    slug, created = make_env(m, ISSUES_POLL)
    plan = guard("A", m.issue_executor_poll)
    if plan is not None:
        nums = sorted(t[1] for t in plan)
        if nums != [801]:
            problems.append("A: poll plan %s, want [801]" % nums)

    # B: orphans report
    slug, _ = make_env(m, ISSUES_POLL)
    orphans = guard("B", lambda: m.active_milestone_orphans(slug))
    if orphans is not None:
        if 801 not in orphans:
            problems.append("B: loose mention hid orphan #801 (%s)"
                            % orphans)
        if 802 in orphans:
            problems.append("B: anchored card did not cover #802")

    # C: admission no-executor
    slug, _ = make_env(m, ISSUES_ADM)
    findings = guard("C", lambda: m.admission_gate(slug))
    if findings is not None:
        keys = [f[0] for f in findings]
        if not any(":admission:no-executor:801" in k for k in keys):
            problems.append("C: no-executor:801 missing (%s)"
                            % [k for k in keys if "801" in k])

    # D: the predicate
    ecr = getattr(m, "executor_cover_refs", None)
    if ecr is None:
        problems.append("D: executor_cover_refs is missing")
    else:
        if 801 in ecr("Summarize priorities #800/#801",
                      "a recommendation on #801"):
            problems.append("D: loose #800/#801 counted as coverage")
        if 802 not in ecr("[coder] issue #802", "x"):
            problems.append("D: 'issue #802' not recognized")
        if 79 not in ecr("t", "Issue: #79\n\nGoal"):
            problems.append("D: 'Issue: #79' not recognized")
        if ecr("x", "Closes #12, fixes #13, refs #14") != {12, 13, 14}:
            problems.append("D: Closes/fixes/refs not recognized")
    return problems


def load_ph(src=None, tag="green"):
    path = PH_PATH
    if src is not None:
        sbx = tempfile.mkdtemp(prefix="ph-sbx-anchor-")
        path = os.path.join(sbx, "pr-hygiene.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(src)
        shutil.copy(os.path.join(HERE, "factory_config.py"),
                    os.path.join(sbx, "factory_config.py"))
    sp = importlib.util.spec_from_file_location("ph_anchor_%s" % tag, path)
    m = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(m)
    tmp = tempfile.mkdtemp(prefix="ph-db-anchor-")
    db = os.path.join(tmp, "kanban.db")
    con = sqlite3.connect(db)
    con.execute("create table tasks (id text primary key, title text,"
                " body text default '', status text, assignee text)")
    con.commit()
    con.close()
    m.BOARD_DB = db
    return m


def battery_ph(ph):
    problems = []
    if not ph.issues_without_cards(
            [{"number": 805, "labels": [{"name": "keep-open"}]}], {}):
        problems.append("E: keep-open hid the issue from"
                        " issues_without_cards")
    if ph.issues_without_cards(
            [{"number": 806, "labels": [{"name": "wait-for-triage"}]}], {}):
        problems.append("E: wait-for-triage no longer hides the issue")
    return problems


MUT_HELPER = (
    '        r"(?i)\\b(?:issues?|refs|closes|fixes)\\s*:?\\s*#(\\d{1,5})",'
    ' text)}',
    '        r"#(\\d+)", text)}',
)
MUT_ADM = (
    "            if milestone != active_ms or n in live_anchor:",
    "            if milestone != active_ms or n in live_refs:",
)
MUT_PH = (
    '        if {"wait-for-triage", "blocked-upstream",',
    '        if {"wait-for-triage", "keep-open", "blocked-upstream",',
)


def mutate(path, *pairs):
    src = open(path, encoding="utf-8").read()
    for old, new in pairs:
        if src.count(old) != 1:
            raise SystemExit("mutant anchor not unique (%d): %r"
                             % (src.count(old), old))
        src = src.replace(old, new)
    return src


def report(label, problems, want_red, must=None):
    if want_red:
        caught = bool(problems) and (
            must is None or any(p.startswith(must) for p in problems))
        print("%s red(%s): %d problem(s)" % ("ok  " if caught else "FAIL",
                                             label, len(problems)))
        if not caught:
            FAILS.append("mutant %s not caught" % label)
    else:
        for p in problems:
            print("FAIL green: %s" % p)
        if problems:
            FAILS.append("green %s" % label)
        else:
            print("ok   green(%s)" % label)


def main():
    report("watchdog", battery(load_bw()), False)
    report("pr-hygiene", battery_ph(load_ph()), False)
    report("helper", battery(load_bw(mutate(BW_PATH, MUT_HELPER),
                                     "mut_helper")), True, "A:")
    report("admission", battery(load_bw(mutate(BW_PATH, MUT_ADM),
                                        "mut_adm")), True, "C:")
    report("pr-hygiene keep-open",
           battery_ph(load_ph(mutate(PH_PATH, MUT_PH), "mut_ph")), True)
    print("\n%s" % ("PASS" if not FAILS else "FAIL: %s" % "; ".join(FAILS)))
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
