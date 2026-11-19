#!/usr/bin/env python3
"""Owner review lifts a PR out of the parking and out of the verdict dedup
cover (board-watchdog.py).

Rule: the owner's formal review (APPROVED/CHANGES_REQUESTED) is the owner's
decision to work on the PR. The admission gate treats it as a service
admission: the cards of such a PR are not parked, and cards already parked as
pr_not_milestone return through the service fallback. The verdict dedup gate
does not count a parked (scheduled) card as cover: either it is lifted, or
the item stays in the verdict card. Reason for the test: a Before/After item
was handed to a parked visual-delta card nobody would pick up, and the merge
stood still over a two-line formality.

Run: python3 test_board_watchdog_owner_review_raise.py
"""
import importlib.util
import json
import os
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
        completed_at int default 0, idempotency_key text);
      create table task_comments (id integer primary key autoincrement,
        task_id text, author text, body text, created_at int);
      create table task_links (id integer primary key autoincrement,
        parent_id text, child_id text);
      create table task_events (id integer primary key autoincrement,
        task_id text not null, run_id int, kind text not null,
        payload text, created_at int not null);
    """)
    con.executemany(
        "insert into tasks (id,title,body,assignee,status,priority,"
        "created_at,created_by) values (?,?,?,?,?,?,?,?)", tasks)
    con.executemany(
        "insert into task_comments (task_id,author,body,created_at)"
        " values (?,?,?,?)", comments)
    # The production contract of `hermes kanban schedule`: a task_events row
    # (kind='scheduled', payload {"reason": ...}) plus a comment
    # "SCHEDULED: <reason>"; the gate reads the reason from task_events.
    for tid, _author, body, cat in comments:
        if body.startswith("SCHEDULED: "):
            con.execute(
                "insert into task_events (task_id,kind,payload,created_at)"
                " values (?,?,?,?)",
                (tid, "scheduled",
                 json.dumps({"reason": body[len("SCHEDULED: "):]}), cat))
    con.commit()
    con.close()
    return db


# Fixture: PR #278 with an owner CHANGES_REQUESTED review; #202 and #245
# without reviews; #245 references issue #229 of the active milestone (the
# milestone path is the main one).
def issues_fixture():
    return {229: ("2.3.0", set()), 190: (None, set())}


def prs_fixture():
    return {"pr_issues": {278: [], 202: [], 245: [229]},
            "pr_milestones": {}, "owner_review_prs": [278]}


PARK_REASON = ("SCHEDULED: backlog: outside milestone (backlog gate, rule 2,"
               " category: %s): no link to the open milestones;"
               " return to ready with the unblock command after the owner"
               " decides")

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
    """Board plus gh/run stubs for admission_gate."""

    def __init__(self, mod, tasks=(), comments=()):
        self.mod = mod
        self.tmp = tempfile.mkdtemp(prefix="wd-owner-review-")
        self.db = make_board(self.tmp, tasks, comments)
        self.runs = []
        self._old = (mod.BOARDS, mod.board_repo, mod.run,
                     mod.open_issue_states, mod.open_pr_linkage)
        mod.BOARDS = self.tmp
        mod.board_repo = lambda slug: "acme/widget"
        mod.open_issue_states = lambda repo: issues_fixture()
        mod.open_pr_linkage = lambda repo, st: prs_fixture()
        mod.run = self._fake_run
        # Milestone bands come from gh api milestones; the milestone_bands
        # cache is moved into the sandbox of the run.
        self._mb = getattr(mod, "milestone_bands", None)
        if self._mb is not None:
            self._old_mb = (self._mb.CACHE_PATH, dict(self._mb._MEMO))
            self._mb.CACHE_PATH = os.path.join(self.tmp, "ms-cache.json")
            self._mb._MEMO.clear()

    def _fake_run(self, cmd, timeout=None):
        self.runs.append(cmd)
        if cmd[:2] == ["gh", "api"] and any("milestones" in t for t in cmd):
            # Open milestones: 2.3.0 is active by due_on, 2.4.0 is next,
            # so the bands are 80/40.
            return json.dumps([
                {"title": "2.3.0", "due_on": "2026-11-23T00:00:00Z",
                 "state": "open", "number": 1},
                {"title": "2.4.0", "due_on": "2026-12-20T00:00:00Z",
                 "state": "open", "number": 2}])
        verb = cmd[4] if len(cmd) > 4 else ""
        tid = cmd[5] if len(cmd) > 5 else ""
        con = sqlite3.connect(self.db)
        if verb == "comment":
            text = cmd[6] if len(cmd) > 6 else ""
            con.execute("insert into task_comments (task_id,author,body,"
                        "created_at) values (?,?,?,?)",
                        (tid, "watchdog", text, int(time.time())))
        elif verb == "unblock":
            con.execute("update tasks set status='ready' where id=?", (tid,))
            reason = " ".join(cmd[7:]) if len(cmd) > 7 else ""
            con.execute("insert into task_comments (task_id,author,body,"
                        "created_at) values (?,?,?,?)",
                        (tid, "watchdog", reason, int(time.time())))
        elif verb == "schedule":
            con.execute("update tasks set status='scheduled' where id=?",
                        (tid,))
            reason = cmd[6] if len(cmd) > 6 else ""
            con.execute("insert into task_comments (task_id,author,body,"
                        "created_at) values (?,?,?,?)",
                        (tid, "watchdog", "SCHEDULED: %s" % reason,
                         int(time.time())))
            con.execute(
                "insert into task_events (task_id,kind,payload,created_at)"
                " values (?,?,?,?)",
                (tid, "scheduled", json.dumps({"reason": reason}),
                 int(time.time())))
        elif verb == "archive":
            con.execute("update tasks set status='archived' where id=?",
                        (tid,))
        elif verb == "edit":
            for i, a in enumerate(cmd):
                if a == "--priority":
                    con.execute("update tasks set priority=? where id=?",
                                (int(cmd[i + 1]), tid))
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
        (self.mod.BOARDS, self.mod.board_repo, self.mod.run,
         self.mod.open_issue_states, self.mod.open_pr_linkage) = self._old
        if self._mb is not None:
            self._mb.CACHE_PATH, _ = self._old_mb
            self._mb._MEMO.clear()
            self._mb._MEMO.update(self._old_mb[1])


def main():
    now = time.time()
    mod = load_mod()
    print("module: %s" % SCRIPT)

    print("[1] service admission: a card of a PR with an owner review is not parked")
    env = Env(mod, tasks=[
        ("t_vd78", TITLE_VD, BODY_VD, "coder", "ready", 80, now, "agent"),
        ("t_245", "PR #245: rework", "", "coder", "ready", 80, now,
         "agent"),
        ("t_bad", "#999 junk", "", "coder", "ready", 70, now, "agent"),
    ])
    mod.admission_gate(SLUG)
    stt = env.statuses()
    check("visual-delta #278 not parked (service: owner review)",
          stt.get("t_vd78") == "ready", str(stt))
    check("milestone through Refs is stronger than review (main path)",
          stt.get("t_245") == "ready", str(stt))
    check("control: junk is parked", stt.get("t_bad") == "scheduled",
          str(stt))
    check("schedule was not called for #278",
          not any(len(c) > 4 and c[4] == "schedule" and c[5] == "t_vd78"
                  for c in env.runs), str([c[4:] for c in env.runs]))
    env.restore()

    print("[2] service return: a parked pr_not_milestone card is lifted")
    env = Env(
        mod,
        tasks=[
            ("t_15e6c5ca", TITLE_VD, BODY_VD, "coder", "scheduled", 80,
             now - D, "watchdog"),
            ("t_p202", "[visual-delta] PR #202: Before / After section", "",
             "coder", "scheduled", 80, now - D, "watchdog"),
            ("t_issue", "#190 task", "", "coder", "scheduled", 80,
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
    findings = mod.admission_gate(SLUG)
    stt = env.statuses()
    check("t_15e6c5ca lifted to ready (service: owner review)",
          stt.get("t_15e6c5ca") == "ready", str(stt))
    check("unblock carries the admission gate marker",
          any(len(c) > 4 and c[4] == "unblock" and c[5] == "t_15e6c5ca"
              and mod.ADMISSION_MARKER in " ".join(c[6:])
              for c in env.runs), str([c[4:] for c in env.runs]))
    check("without a review it stays in scheduled",
          stt.get("t_p202") == "scheduled", str(stt))
    check("an issue without a milestone is not lifted",
          stt.get("t_issue") == "scheduled", str(stt))
    check("return-svc finding",
          any("return-svc:t_15e6c5ca" in k for k, _, _ in findings),
          str(findings)[:200])
    # idempotency: a second run neither lifts nor parks anything
    env.runs.clear()
    mod.admission_gate(SLUG)
    check("repeated run without unblock/schedule",
          not any(len(c) > 4 and c[4] in ("unblock", "schedule")
                  for c in env.runs),
          str([c[4:] for c in env.runs]))
    env.restore()

    print("[3] an owner COMMENTED review is not a decision")
    # Head semantics are conservative: only a formal review counts. Here
    # the owner only COMMENTED and there is no review, so the card is parked.
    env = Env(mod, tasks=[
        ("t_c278", "PR #278 minor", "#278", "coder", "ready", 80, now,
         "agent"),
    ])
    env.mod.open_pr_linkage = (
        lambda repo, st: {"pr_issues": {278: []},
                          "pr_milestones": {}, "owner_review_prs": []})
    mod.admission_gate(SLUG)
    check("COMMENTED only: the card is parked",
          env.statuses().get("t_c278") == "scheduled",
          str(env.statuses()))
    env.restore()

    print("[4] dedup: scheduled is not cover, after the lift it is")
    env = Env(mod, tasks=[
        ("t_15e6c5ca", TITLE_VD, BODY_VD, "coder", "scheduled", 80,
         now - 28.4 * 3600, "watchdog"),
        ("t_707fd10f", TITLE_VERDICT, BODY_VERDICT, "coder", "ready", 80,
         now - 3600, "agent"),
    ])
    hits = mod.verdict_dedup_scan(SLUG)
    check("a scheduled visual-delta card is not cover", hits == [],
          str(hits)[:300])
    # The lift (service admission or by hand) brings the cover back: the
    # item is subtracted from the verdict card again.
    env.set_status("t_15e6c5ca", "ready")
    hits = mod.verdict_dedup_scan(SLUG)
    check("after the lift the dedup works",
          len(hits) == 1 and hits[0]["tid"] == "t_707fd10f"
          and hits[0]["removed"]
          == [("1", "Before / After section", "t_15e6c5ca")],
          str(hits)[:400])
    check("item 2 stays with the verdict",
          bool(hits) and "## Blocking item 2: update-branch"
          in hits[0]["new_body"])
    env.restore()

    if FAILED:
        print("FAILED: %s" % ", ".join(FAILED))
        sys.exit(1)
    print("OWNER REVIEW (service admission): all tests green")


if __name__ == "__main__":
    main()
