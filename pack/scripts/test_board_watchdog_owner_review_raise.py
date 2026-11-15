#!/usr/bin/env python3
"""Owner review lifts a PR out of the backlog parking and out of the verdict
dedup cover (board-watchdog.py).

Rule: the owner's formal review (APPROVED/CHANGES_REQUESTED) is the owner's
decision to work on the PR. The backlog gate does not park the cards of such
a PR and lifts the ones already parked as pr_not_milestone. The verdict dedup
gate does not count a parked (scheduled) card as cover: either it is lifted,
or the item stays in the verdict card. Reason for the test: a Before/After
item was handed to a parked visual-delta card nobody would pick up, and the
merge stood still over a formality.

Run: python3 test_board_watchdog_owner_review_raise.py
Mutant runs: copy board-watchdog.py (with the mutation) next to the other
scripts and run BOARD_WATCHDOG_SCRIPT=<copy> python3 test_board_watchdog_owner_review_raise.py
Mutants that must fail: classify ignores owner_review_prs; the raise step is
removed from the gate; the dedup counts scheduled cards as cover.
"""
import importlib.util
import json
import os
import re
import sqlite3
import sys
import tempfile
import time

# Settings and Hermes home are fixed before the script is loaded.
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="wd-home-")
os.environ.update({
    "FACTORY_REPO": "acme/widget",
    "FACTORY_REPO_PATH": "/srv/hermes/widget",
    "FACTORY_BOARD": "main",
    "FACTORY_OWNER_LOGIN": "owner-login",
    "FACTORY_OWNER_CHAT": "telegram:000000000",
    "FACTORY_BASE_BRANCHES": "main",
})

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.environ.get("BOARD_WATCHDOG_SCRIPT",
                        os.path.join(HERE, "board-watchdog.py"))
SLUG = "main"
D = 24 * 3600
FAILED = []


def check(name, cond, detail=""):
    if cond:
        print("  ok %s" % name)
    else:
        FAILED.append(name)
        print("  FAIL %s %s" % (name, detail))


def load_mod():
    spec = importlib.util.spec_from_file_location("wd_owner_review_test", SCRIPT)
    assert spec is not None and spec.loader is not None, SCRIPT
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_board(tmp, tasks=(), comments=()):
    os.makedirs(os.path.join(tmp, SLUG), exist_ok=True)
    db = os.path.join(tmp, SLUG, "kanban.db")
    con = sqlite3.connect(db)
    con.executescript("""
      create table tasks (id text primary key, title text default '',
        body text default '', assignee text default 'coder',
        status text default 'todo', priority int default 0,
        created_at int default 0, created_by text default 'agent',
        idempotency_key text);
      create table task_runs (id integer primary key autoincrement,
        task_id text, started_at int);
      create table task_events (id integer primary key autoincrement,
        task_id text, kind text, created_at int);
      create table task_comments (id integer primary key autoincrement,
        task_id text, author text, body text, created_at int);
    """)
    con.executemany(
        "insert into tasks (id,title,body,assignee,status,priority,"
        "created_at,created_by) values (?,?,?,?,?,?,?,?)", tasks)
    con.executemany(
        "insert into task_comments (task_id,author,body,created_at)"
        " values (?,?,?,?)", comments)
    con.commit()
    con.close()
    return db



# Linkage fixture: PR #278 has a review of the owner; #202 has none; #245 is
# tied by Refs to issue 229 of the milestone (the milestone path is primary).
LINKAGE38 = {
    "ts": time.time(),
    "ms_titles": {3: "2.3.0"},
    "ms_issues": {229: "open"},
    "open_pr_refs": {278: [], 202: [], 245: [229]},
    "direct_ms_prs": [],
    "owner_review_prs": [278],
}

PARK_REASON = ("SCHEDULED: backlog: outside milestone (backlog gate, rule 2,"
               " category: %s): no link to the open milestones (2.3.0);"
               " return to ready with the unblock command after the owner"
               " decides")

# Dedup fixture: a verdict card and an earlier visual-delta card of one PR.
TITLE_VD = "[visual-delta] PR #278: Before / After section"
BODY_VD = (
    "PR #278 violates the visual-delta rule:\n\n"
    "- no '## Before / After' section (first, before any text)\n\n"
    "Format: the body starts with the `## Before / After` section.\n"
    "https://github.com/acme/widget/pull/278")

TITLE_VERDICT = ("[coder] PR #278 rework: merge prerequisites, Before/After"
                 " + update-branch (head dd4cf30)")
BODY_VERDICT = (
    "Owner review verdict on PR #278, head dd4cf30."
    " CHANGES REQUESTED: minor, code accepted. Only merge prerequisites"
    " remain.\n\n"
    "## Blocking item 1: PR body needs a Before/After section\n"
    "No \"## Before / After\" section at the top of the PR body. This is a"
    " UI change. Required: two screenshots, details inside <details>"
    " blocks.\n\n"
    "## Blocking item 2: update-branch + green CI\n"
    "- run update-branch (merge the base branch tip into the PR branch)\n"
    "- wait for a green CI run on the updated head\n"
    "- report the new head in a PR comment\n\n"
    "## Acceptance criteria\n"
    "- PR body has \"## Before / After\" at the top with both screenshots,"
    " details collapsed\n"
    "- branch carries the base branch tip (update-branch), CI green on the"
    " new head, new head reported in a PR comment\n")


class Env:
    """Patches the module to a temporary board and a CLI stub.

    unblock imitates the core (kanban_db.unblock_task): scheduled -> ready,
    the reason is written as a comment before the lift.
    """

    def __init__(self, mod, tasks=(), comments=()):
        self.mod = mod
        self.tmp = tempfile.mkdtemp(prefix="wd-owner-review-")
        self.db = make_board(self.tmp, tasks, comments)
        self.runs = []
        self._old = (mod.BOARDS, mod.BACKLOG_HOLD, mod.BACKLOG_CACHE,
                     mod.run, mod.backlog_fetch_linkage)
        mod.BOARDS = self.tmp
        mod.BACKLOG_HOLD = os.path.join(self.tmp, "hold")
        mod.BACKLOG_CACHE = os.path.join(self.tmp, "linkage.json")
        json.dump(LINKAGE38, open(mod.BACKLOG_CACHE, "w"))
        mod.backlog_fetch_linkage = self._no_fetch
        mod.run = self._fake_run

    def _no_fetch(self):
        raise AssertionError("gh must not be called: the cache is fresh")

    def _fake_run(self, cmd, timeout=None):
        self.runs.append(cmd)
        verb = cmd[4] if len(cmd) > 4 else ""
        tid = cmd[5] if len(cmd) > 5 else ""
        con = sqlite3.connect(self.db)
        if verb == "unblock":
            reason = " ".join(cmd[7:]) if len(cmd) > 7 else ""
            con.execute("update tasks set status='ready' where id=?", (tid,))
            con.execute("insert into task_comments (task_id, author, body,"
                        " created_at) values (?,?,?,?)",
                        (tid, "watchdog", reason, int(time.time())))
        elif verb == "schedule":
            reason = " ".join(cmd[6:]) if len(cmd) > 6 else ""
            con.execute("update tasks set status='scheduled' where id=?",
                        (tid,))
            con.execute("insert into task_comments (task_id, author, body,"
                        " created_at) values (?,?,?,?)",
                        (tid, "watchdog", reason, int(time.time())))
        elif verb == "archive":
            con.execute("update tasks set status='archived' where id=?",
                        (tid,))
        con.commit()
        con.close()
        if verb == "schedule":
            return "Scheduled %s" % tid
        return ""

    def statuses(self):
        con = sqlite3.connect(self.db)
        rows = dict(con.execute("select id,status from tasks"))
        con.close()
        return rows

    def set_status(self, tid, status):
        con = sqlite3.connect(self.db)
        con.execute("update tasks set status=? where id=?", (status, tid))
        con.commit()
        con.close()

    def restore(self):
        (self.mod.BOARDS, self.mod.BACKLOG_HOLD, self.mod.BACKLOG_CACHE,
         self.mod.run, self.mod.backlog_fetch_linkage) = self._old


def classify_case(mod, title, body, assignee="coder"):
    return mod.backlog_classify({"title": title, "body": body,
                                 "assignee": assignee}, LINKAGE38)


def main():
    now = time.time()
    mod = load_mod()
    print("module: %s" % SCRIPT)

    print("[1] classification: an owner review is keep")
    check("PR with an owner CR = keep pr_owner_review",
          classify_case(mod, TITLE_VD, BODY_VD)
          == ("keep", "pr_owner_review", [278]))
    check("PR without a review = demote pr_not_milestone",
          classify_case(mod, "PR #202: rework", "")
          == ("demote", "pr_not_milestone", [202]))
    check("milestone through Refs beats the review",
          classify_case(mod, "PR #245: rework", "")
          == ("keep", "milestone", [245]))
    check("ops stays keep",
          classify_case(mod, "check nodes", "", assignee="ops")
          == ("keep", "ops", None))

    print("[2] the gate does not park the card of a PR with an owner review")
    env = Env(mod, tasks=[
        ("t_vd78", TITLE_VD, BODY_VD, "coder", "ready", 80, now, "agent"),
        ("t_bad", "#999 junk", "", "coder", "ready", 70, now, "agent"),
    ])
    st = {}
    mod.backlog_gate(SLUG, st, now=now)
    stt = env.statuses()
    check("visual-delta #278 is not parked", stt.get("t_vd78") == "ready",
          str(stt))
    check("control: junk is parked", stt.get("t_bad") == "scheduled",
          str(stt))
    check("schedule was not called for #278",
          not any(c[4] == "schedule" and c[5] == "t_vd78"
                  for c in env.runs), str([c[4:] for c in env.runs]))
    env.restore()

    print("[3] the gate lifts a card parked as pr_not_milestone")
    env = Env(
        mod,
        tasks=[
            ("t_15e6c5ca", TITLE_VD, BODY_VD, "coder", "scheduled", 80,
             now - D, "watchdog"),
            ("t_p202", "[visual-delta] PR #202: Before / After section", "",
             "coder", "scheduled", 80, now - D, "watchdog"),
            ("t_issue", "#229 and #278 task", "", "coder", "scheduled", 80,
             now - D, "watchdog"),
        ],
        comments=[
            ("t_15e6c5ca", "watchdog", PARK_REASON % "pr_not_milestone",
             now - D),
            ("t_p202", "watchdog", PARK_REASON % "pr_not_milestone",
             now - D),
            ("t_issue", "watchdog", PARK_REASON % "issue_not_milestone",
             now - D),
        ])
    st = {}
    out = mod.backlog_gate(SLUG, st, now=now)
    stt = env.statuses()
    check("t_15e6c5ca lifted to ready", stt.get("t_15e6c5ca") == "ready",
          str(stt))
    check("unblock with a reason and the PR number",
          any(c[4] == "unblock" and c[5] == "t_15e6c5ca"
              and "#278" in " ".join(c[6:])
              and mod.BACKLOG_RAISE_MARKER in " ".join(c[6:])
              for c in env.runs), str([c[4:] for c in env.runs]))
    check("without a review it stays scheduled", stt.get("t_p202") == "scheduled",
          str(stt))
    check("issue_not_milestone is not lifted",
          stt.get("t_issue") == "scheduled", str(stt))
    check("finding backlog-raise",
          any(k.startswith("backlog-raise:1") for k, _ in out), str(out))
    # idempotency: the second run lifts and parks nothing
    env.runs.clear()
    mod.backlog_gate(SLUG, st, now=now)
    check("a repeated run has no unblock/schedule",
          not any(c[4] in ("unblock", "schedule") for c in env.runs),
          str([c[4:] for c in env.runs]))
    env.restore()

    print("[4] a dry run does not mutate")
    env = Env(
        mod,
        tasks=[("t_d78", TITLE_VD, BODY_VD, "coder", "scheduled", 80,
                now - D, "watchdog")],
        comments=[("t_d78", "watchdog", PARK_REASON % "pr_not_milestone",
                   now - D)])
    st = {}
    out = mod.backlog_gate(SLUG, st, now=now, apply=False,
                           ignore_hold=True)
    check("no CLI mutation", env.runs == [],
          str([c[4:] for c in env.runs]))
    check("status untouched", env.statuses().get("t_d78") == "scheduled")
    check("the dry run sees the lift",
          any(k.startswith("backlog-raise:1") for k, _ in out), str(out))
    env.restore()

    print("[5] dedup: a scheduled card is not cover, after the lift it is")
    env = Env(mod, tasks=[
        ("t_15e6c5ca", TITLE_VD, BODY_VD, "coder", "scheduled", 80,
         now - 28.4 * 3600, "watchdog"),
        ("t_707fd10f", TITLE_VERDICT, BODY_VERDICT, "coder", "ready", 80,
         now - 3600, "agent"),
    ])
    hits = mod.verdict_dedup_scan(SLUG)
    check("a scheduled visual-delta is not cover", hits == [], str(hits)[:300])
    # a lift (the backlog gate or by hand) brings the cover back: the item
    # is subtracted from the verdict card again
    env.set_status("t_15e6c5ca", "ready")
    hits = mod.verdict_dedup_scan(SLUG)
    check("after the lift dedup works",
          len(hits) == 1 and hits[0]["tid"] == "t_707fd10f"
          and hits[0]["removed"]
          == [("1", "Before / After section", "t_15e6c5ca")],
          str(hits)[:400])
    check("item 2 stays with the verdict",
          "## Blocking item 2: update-branch" in hits[0]["new_body"])
    env.restore()

    if FAILED:
        print("FAILED: %s" % ", ".join(FAILED))
        sys.exit(1)
    print("OWNER REVIEW RAISE: all tests green")


if __name__ == "__main__":
    main()
