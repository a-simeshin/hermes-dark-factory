#!/usr/bin/env python3
"""The milestone priority band applies on every path that creates cards.

Why: an owner comment on an issue of the NEXT milestone created a card
through the webhook with the top band (p80), and a worker took it within
seconds. The admission gate fixed priorities only in ready/todo/review/
scheduled, so the running card kept p80 to the end. At the same time three
issues of the ACTIVE milestone had no executor and the watchdog was silent:
the unclaimed check counted done cards of the last 14 days as coverage, and
wait-for-triage issues were excluded entirely.

Checks:

  A. An issue of the active milestone without a LIVE card for longer than
     24 h -> finding admission:no-executor (a fresh issue of 2 h, a live
     card, an open PR and the next milestone suppress the finding).
  B. A done card (2 h ago) is NOT coverage: the finding still stands.
  C. wait-for-triage in the active milestone for longer than 24 h -> a
     separate finding admission:triage-stuck (a question to the owner).
  D. A running card outside its band (p80, issue of the next milestone) ->
     the gate fixes the priority to 40 without preempting the run.
  E. The helper milestone_bands.py --issue/--pr gives the band (40 for an
     issue of the next milestone, 80 for the active one), rc 3 without a
     band, rc 2 without arguments.

The board harness is imported from test_board_watchdog_board_lint.py.

Run: python3 test_board_watchdog_milestone_band_paths.py
"""
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.join(HERE, "test_board_watchdog_board_lint.py")
spec = importlib.util.spec_from_file_location("wd_lint_harness_bands", HARNESS)
if spec is None or spec.loader is None:
    print("SKIP: cannot load harness")
    sys.exit(0)
h = importlib.util.module_from_spec(spec)
sys.modules["wd_lint_harness_bands"] = h
spec.loader.exec_module(h)

_sbx = tempfile.mkdtemp(prefix="bw-sbx-bands-")
h.mod.BACKLOG_CACHE = os.path.join(_sbx, "backlog-cache.json")
h.mod.STATE = os.path.join(_sbx, "watchdog-state.json")
mod = h.mod

FAILS = []


def check(name, cond, detail=""):
    print("%s %s%s" % ("ok  " if cond else "FAIL", name,
                       (" :: %s" % (detail,)) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


NOW = time.time()
ACTIVE, NEXT = "1.3.0", "1.4.0"
OPEN_MILESTONES = json.dumps([
    {"title": ACTIVE, "due_on": "2026-11-22T00:00:00Z"},
    {"title": NEXT, "due_on": "2026-12-20T00:00:00Z"},
])

ISSUES = json.dumps([
    # A: active milestone, no card, 30 h -> no-executor
    {"number": 401, "milestone": {"title": ACTIVE},
     "labels": [{"name": "hermes"}], "updatedAt": _iso(NOW - 30 * 3600)},
    # C: active, wait-for-triage, 40 h -> triage-stuck
    {"number": 402, "milestone": {"title": ACTIVE},
     "labels": [{"name": "wait-for-triage"}],
     "updatedAt": _iso(NOW - 40 * 3600)},
    # A: active, but a live card exists -> silence
    {"number": 403, "milestone": {"title": ACTIVE},
     "labels": [{"name": "hermes"}], "updatedAt": _iso(NOW - 30 * 3600)},
    # A: active, but activity 2 h ago -> silence (age < 24 h)
    {"number": 404, "milestone": {"title": ACTIVE},
     "labels": [{"name": "hermes"}], "updatedAt": _iso(NOW - 2 * 3600)},
    # A: next milestone -> silence (the check is about the active one only)
    {"number": 405, "milestone": {"title": NEXT},
     "labels": [{"name": "hermes"}], "updatedAt": _iso(NOW - 40 * 3600)},
    # A: active, but an open PR exists (Closes #406) -> silence
    {"number": 406, "milestone": {"title": ACTIVE},
     "labels": [{"name": "hermes"}], "updatedAt": _iso(NOW - 40 * 3600)},
    # B: active, the only card is done 2 h ago -> finding stands
    {"number": 407, "milestone": {"title": ACTIVE},
     "labels": [{"name": "hermes"}], "updatedAt": _iso(NOW - 40 * 3600)},
    # D: next milestone for a running card outside its band
    {"number": 408, "milestone": {"title": NEXT},
     "labels": [{"name": "hermes"}], "updatedAt": _iso(NOW - 3600)},
])

PRS = json.dumps([
    {"number": 90, "body": "Closes #406", "milestone": None,
     "latestReviews": []},
])

slug, db, old_boards = h.make_board(tasks=[
    # live card of #403 (in band, the gate does not touch it)
    {"id": "t_403", "status": "ready", "priority": 80,
     "title": "[coder] issue #403", "body": "work on #403",
     "assignee": "coder", "created_by": "user"},
    # running p80 on an issue of the next milestone
    {"id": "t_run", "status": "running", "priority": 80,
     "title": "[coder] issue #408: work", "body": "work on #408",
     "assignee": "coder", "created_by": "user"},
])
# B: a done card of #407 closed 2 h ago (done refs window: 14 days)
con = sqlite3.connect(db)
con.execute(
    "insert into tasks (id, title, body, status, priority, assignee,"
    " created_by, created_at) values ('t_dn','Land PR #250 (issue #407)',"
    "'refs #407','done',80,'coder','user',?)", (int(NOW - 86400),))
con.commit()
con.close()

mod.board_repo = lambda s: "acme/demo"
edits = []
real_run = mod.run


def _run(cmd, timeout=120):
    if cmd[:2] == ["gh", "api"]:
        return OPEN_MILESTONES
    if cmd[:2] == ["gh", "issue"] and "list" in cmd:
        return ISSUES
    if cmd[:2] == ["gh", "pr"] and "list" in cmd:
        return PRS
    if len(cmd) > 5 and cmd[1] == "kanban":
        action, tid = cmd[4], cmd[5]
        con = sqlite3.connect(db, timeout=10)
        try:
            if action == "schedule":
                con.execute("update tasks set status='scheduled' "
                            "where id=?", (tid,))
            elif action == "comment":
                con.execute(
                    "insert into task_comments (task_id, body, created_at)"
                    " values (?,?,?)", (tid, " ".join(cmd[6:]),
                                        int(time.time())))
            elif action == "edit" and "--priority" in cmd:
                p = cmd[cmd.index("--priority") + 1]
                edits.append((tid, int(p)))
                con.execute("update tasks set priority=? where id=?",
                            (int(p), tid))
            con.commit()
        finally:
            con.close()
        return "ok"
    return ""


mod.run = _run
findings = mod.admission_gate(slug)
mod.run = real_run
mod.BOARDS = old_boards

keys = {k for k, _, _ in findings}
con = sqlite3.connect(db)
prios = dict(con.execute("select id, priority from tasks"))
con.close()

check("A1 #401 (active, 30 h, no card) -> no-executor",
      any(":admission:no-executor:401" in k for k in keys), sorted(keys))
check("A2 #403 (live card) and #404 (2 h) are silent",
      not any(":no-executor:403" in k or ":no-executor:404" in k
              or ":triage-stuck:403" in k or ":triage-stuck:404" in k
              for k in keys), sorted(keys))
check("A3 #405 (next milestone) and #406 (open PR) are silent",
      not any(":no-executor:405" in k or ":no-executor:406" in k
              for k in keys), sorted(keys))
check("B #407: a done card 2 h ago is not coverage",
      any(":admission:no-executor:407" in k for k in keys), sorted(keys))
check("C #402 (wait-for-triage, 40 h) -> triage-stuck",
      any(":admission:triage-stuck:402" in k for k in keys), sorted(keys))
check("D1 running t_run (p80, next milestone issue) moved to band 40",
      prios.get("t_run") == 40, "prio=%s edits=%s" % (prios.get("t_run"),
                                                      edits))
check("D2 a live card in its band is untouched (t_403 = 80)",
      prios.get("t_403") == 80, "prio=%s" % prios.get("t_403"))

# --- E: helper (hermetic: band_of_ref with stubbed gh_exec and fetch) ----
mb_path = os.path.join(HERE, "milestone_bands.py")
if not os.path.exists(mb_path):
    check("E helper exists", False, "milestone_bands.py is missing")
else:
    mb_spec = importlib.util.spec_from_file_location("msb_bands", mb_path)
    mb = importlib.util.module_from_spec(mb_spec)
    sys.modules["msb_bands"] = mb
    mb_spec.loader.exec_module(mb)
    mb.CACHE_PATH = os.path.join(_sbx, "ms.json")
    mb._MEMO = {}

    _API = {
        "repos/acme/demo/issues/236":
            {"number": 236, "milestone": {"title": ACTIVE}},
        "repos/acme/demo/issues/377":
            {"number": 377, "milestone": {"title": NEXT}},
        "repos/acme/demo/issues/1": {"number": 1, "milestone": None},
        "repos/acme/demo/pulls/90":
            {"number": 90, "milestone": None, "body": "Closes #236"},
    }

    def _gh_exec(cmd, **kw):
        class R:
            stdout = json.dumps(_API.get(cmd[2], {}))
        return R()

    def fetch(r):
        return json.loads(OPEN_MILESTONES)

    band236 = mb.band_of_ref("acme/demo", 236, gh_exec=_gh_exec, fetch=fetch)
    band377 = mb.band_of_ref("acme/demo", 377, gh_exec=_gh_exec, fetch=fetch)
    bandpr = mb.band_of_ref("acme/demo", 90, is_pr=True, gh_exec=_gh_exec,
                            fetch=fetch)
    band1 = mb.band_of_ref("acme/demo", 1, gh_exec=_gh_exec, fetch=fetch)
    check("E1 helper: active issue -> 80, next milestone issue -> 40",
          band236 == 80 and band377 == 40, (band236, band377))
    check("E2 helper: PR by Closes #236 (active) -> 80",
          bandpr == 80, bandpr)
    check("E3 helper: no milestone -> None", band1 is None, band1)
    # CLI contract without network: arguments are required, rc 2 (usage).
    cli = subprocess.run([sys.executable, mb_path],
                         capture_output=True, text=True, timeout=60)
    check("E4 CLI requires --issue/--pr (rc 2)", cli.returncode == 2,
          (cli.returncode, cli.stderr[:60]))

if FAILS:
    print("\nRED: %d checks failed: %s" % (len(FAILS), FAILS))
    sys.exit(1)
print("\nOK: the milestone band applies on every card creation path")
