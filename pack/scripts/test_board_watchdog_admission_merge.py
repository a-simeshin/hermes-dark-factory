#!/usr/bin/env python3
"""The backlog gate's admission duties live in admission_gate.

Why: a milestone was assigned to an issue while its card was parked by the
backlog gate marker and the gate's linkage cache was stale, so the card
stayed parked. The next admission_gate run must reset the backlog linkage
cache when the milestones change and return a parking by EITHER marker
(admission or backlog). Also checked: service priority bands, the
merge-unblocker priority, ops cards, the unassigned finding and the refusal
of _kanban_create without an assignee.

Run: python3 test_board_watchdog_admission_merge.py
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
FAILED = []


def check(name, cond, detail=""):
    if cond:
        print("  ok %s" % name)
    else:
        FAILED.append(name)
        print("  FAIL %s %s" % (name, detail))


def load_mod():
    spec = importlib.util.spec_from_file_location("wd_admission_merge_test", SCRIPT)
    assert spec is not None and spec.loader is not None, SCRIPT
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_board(tmp, tasks=(), comments=(), links=()):
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
        "created_at) values (?,?,?,?,?,?,?)", tasks)
    con.executemany(
        "insert into task_comments (task_id,author,body,created_at)"
        " values (?,?,?,?)", comments)
    # The contract of hermes kanban schedule: a task_events row
    # (kind='scheduled', payload {"reason": ...}) plus a comment
    # "SCHEDULED: <reason>". The gate reads the latest reason from
    # task_events, so the fixture seeds both consistently.
    for tid, _author, body, cat in comments:
        if body.startswith("SCHEDULED: "):
            con.execute(
                "insert into task_events (task_id,kind,payload,created_at)"
                " values (?,?,?,?)",
                (tid, "scheduled",
                 json.dumps({"reason": body[len("SCHEDULED: "):]}), cat))
    con.executemany(
        "insert into task_links (parent_id,child_id) values (?,?)", links)
    con.commit()
    con.close()
    return db


PARK_BL = ("backlog: outside milestone (backlog gate, rule 2, category:"
           " issue_not_milestone): no binding to the open milestones"
           " (1.0.0); back to ready with unblock after the owner's"
           " decision")
PARK_ADM = "admission gate: not admitted (#316 without a milestone)."


class Env:
    """A board plus gh/run stubs for admission_gate."""

    def __init__(self, mod, issues, prs, tasks=(), comments=(), links=(),
                 backlog_cache=None):
        self.mod = mod
        self.tmp = tempfile.mkdtemp(prefix="wd-admission-merge-")
        self.db = make_board(self.tmp, tasks, comments, links)
        self.runs = []
        self._old = (mod.BOARDS, mod.board_repo, mod.run,
                     mod.open_issue_states, mod.open_pr_linkage,
                     mod.BACKLOG_CACHE)
        mod.BOARDS = self.tmp
        mod.board_repo = lambda slug: "acme/widget"
        mod.open_issue_states = lambda repo: issues
        mod.open_pr_linkage = lambda repo, st: prs
        mod.run = self._fake_run
        # Milestone bands come from gh api milestones; the milestone_bands
        # cache goes to the run's sandbox, never to a real Hermes home.
        self._mb = getattr(mod, "milestone_bands", None)
        if self._mb is not None:
            self._old_mb = (self._mb.CACHE_PATH, dict(self._mb._MEMO))
            self._mb.CACHE_PATH = os.path.join(self.tmp, "ms-cache.json")
            self._mb._MEMO.clear()
        # a stale backlog cache: the milestone of #316 is not in it yet
        mod.BACKLOG_CACHE = os.path.join(self.tmp, "backlog-linkage.json")
        if backlog_cache is None:
            backlog_cache = {"ts": time.time(), "ms_titles": {7: "0.9.0"},
                             "ms_issues": {229: "open"}}
        json.dump(backlog_cache, open(mod.BACKLOG_CACHE, "w"))

    def _fake_run(self, cmd, timeout=None):
        self.runs.append(cmd)
        if cmd[:2] == ["gh", "api"] and any("milestones" in t for t in cmd):
            # Canned open milestones: 1.0.0 is active by due_on, 1.1.0
            # is next, so the bands are 80/40.
            return json.dumps([
                {"title": "1.0.0", "due_on": "2026-11-23T00:00:00Z",
                 "state": "open", "number": 1},
                {"title": "1.1.0", "due_on": "2026-12-20T00:00:00Z",
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

    def prios(self):
        con = sqlite3.connect(self.db)
        rows = dict(con.execute("select id,coalesce(priority,-1) from tasks"))
        con.close()
        return rows

    def restore(self):
        (self.mod.BOARDS, self.mod.board_repo, self.mod.run,
         self.mod.open_issue_states, self.mod.open_pr_linkage,
         self.mod.BACKLOG_CACHE) = self._old
        if self._mb is not None:
            (self._mb.CACHE_PATH, self._mb._MEMO) = self._old_mb
            self._mb._MEMO.clear()
            self._mb._MEMO.update(self._old_mb[1])


def main():
    now = time.time()
    mod = load_mod()
    print("module: %s" % SCRIPT)

    print("[1] RED CASE: a fresh milestone takes effect at once")
    # gh is live: #316 is already in 1.0.0, the backlog cache is stale
    issues = {316: ("1.0.0", set())}
    prs = {"pr_issues": {}, "pr_milestones": {}, "owner_review_prs": []}
    env = Env(mod, issues, prs, tasks=[
        # a backlog parking the admission gate used not to return
        ("t_43c", "visual-delta #316", "works on #316", "coder", "scheduled",
         80, now - 3 * 3600),
    ], comments=[("t_43c", "watchdog", PARK_BL, int(now) - 3600)])
    findings = mod.admission_gate(SLUG)
    stt = env.statuses()
    check("the backlog parking is returned", stt.get("t_43c") == "ready",
          str(stt))
    check("the backlog cache is reset when the milestones change",
          not os.path.exists(mod.BACKLOG_CACHE))
    check("a return finding",
          any("return" in k for k, _, _ in findings), str(findings)[:200])
    env.restore()

    print("[2] the return also understands the admission marker")
    env = Env(mod, issues, prs, tasks=[
        ("t_adm", "work on #316", "works on #316", "coder", "scheduled",
         80, now - 3 * 3600),
    ], comments=[("t_adm", "watchdog", PARK_ADM, int(now) - 3600)])
    mod.admission_gate(SLUG)
    check("the admission parking is returned",
          env.statuses().get("t_adm") == "ready")
    env.restore()

    print("[3] the backlog cache lives on while milestones do not change")
    env = Env(mod, issues, prs, tasks=[
        ("t_ok", "work on #316", "works on #316", "coder", "ready",
         80, now),
    ], backlog_cache={"ts": time.time(), "ms_titles": {7: "1.0.0"},
                      "ms_issues": {316: "open"}})
    mod.admission_gate(SLUG)
    check("the cache is untouched without changes",
          os.path.exists(mod.BACKLOG_CACHE))
    env.restore()

    print("[4] service bands: a review of the next milestone gets 40, not 100")
    # PR #321 Refs #182, issue #182 in 1.1.0; a review card at p100
    issues4 = {182: ("1.1.0", set())}
    prs4 = {"pr_issues": {321: [182]}, "pr_milestones": {},
            "owner_review_prs": []}
    env = Env(mod, issues4, prs4, tasks=[
        ("t_rev24", "review of PR #321", "#321 head", "reviewer", "review",
         100, now),
    ])
    findings = mod.admission_gate(SLUG)
    check("the review priority is brought to band 40",
          env.prios().get("t_rev24") == 40,
          str(env.prios()))
    check("a band finding",
          any("band" in k for k, _, _ in findings), str(findings)[:200])
    env.restore()

    print("[5] the top band keeps 100 (finish to merge in the active milestone)")
    issues5 = {316: ("1.0.0", set())}
    prs5 = {"pr_issues": {330: [316]}, "pr_milestones": {},
            "owner_review_prs": []}
    env = Env(mod, issues5, prs5, tasks=[
        ("t_rev23", "review of PR #330", "#330 head", "reviewer", "review",
         100, now),
    ])
    mod.admission_gate(SLUG)
    check("100 is kept in the top band",
          env.prios().get("t_rev23") == 100, str(env.prios()))
    env.restore()

    print("[6] a merge-unblocker priority is not touched")
    env = Env(mod, issues4, prs4, tasks=[
        ("t_mu", "merge PR #321", "#321", "coder", "ready", 100, now),
    ], comments=[("t_mu", "watchdog",
                  mod.MERGE_UNBLOCKER_MARKER + " final", int(now))])
    mod.admission_gate(SLUG)
    check("p100 merge-unblocker is not lowered",
          env.prios().get("t_mu") == 100, str(env.prios()))
    env.restore()

    print("[7] service return from parking: a direct milestone on the PR")
    # a card without issue links, PR #321 with a direct 1.1.0 milestone,
    # parked by the backlog gate marker
    issues7 = {190: ("1.1.0", set())}
    prs7 = {"pr_issues": {321: []}, "pr_milestones": {321: "1.1.0"},
            "owner_review_prs": []}
    env = Env(mod, issues7, prs7, tasks=[
        ("t_svc", "visual-delta PR #321", "PR #321", "coder", "scheduled",
         40, now - 3 * 3600),
    ], comments=[("t_svc", "watchdog", PARK_BL, int(now) - 3600)])
    findings = mod.admission_gate(SLUG)
    stt = env.statuses()
    check("the service parking is returned", stt.get("t_svc") == "ready", str(stt))
    check("a return-svc finding",
          any("return-svc" in k for k, _, _ in findings),
          str(findings)[:200])
    env.restore()

    print("[8] ops cards are not parked")
    env = Env(mod, {316: ("1.0.0", set())}, prs, tasks=[
        ("t_ops", "node check", "maintenance", "ops", "ready", 60, now),
    ])
    mod.admission_gate(SLUG)
    check("ops stays in ready",
          env.statuses().get("t_ops") == "ready")
    check("no schedule on the ops card",
          not any(len(c) > 4 and c[4] == "schedule" and c[5] == "t_ops"
                  for c in env.runs))
    env.restore()

    print("[9] finding: an admitted card without an assignee")
    env = Env(mod, issues, prs, tasks=[
        ("t_noa", "work on #316", "works on #316", None, "ready", 80, now),
    ])
    findings = mod.admission_gate(SLUG)
    check("an unassigned finding",
          any("unassigned:t_noa" in k for k, _, _ in findings),
          str(findings)[:200])
    env.restore()

    print("[10] _kanban_create refuses without an assignee")
    raised = False
    try:
        mod._kanban_create(SLUG, "x", "y", "", 80, "key-x")
    except ValueError:
        raised = True
    check("ValueError on an empty assignee", raised)

    if FAILED:
        print("FAILED: %s" % ", ".join(FAILED))
        sys.exit(1)
    print("ADMISSION MERGE: all tests green")


if __name__ == "__main__":
    main()
