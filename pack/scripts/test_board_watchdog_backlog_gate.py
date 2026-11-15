#!/usr/bin/env python3
"""Tests of the backlog owner gate in board-watchdog.py (backlog_gate).

Run: python3 test_board_watchdog_backlog_gate.py
Mutant runs: copy board-watchdog.py (with the mutation) next to the other
scripts and run BOARD_WATCHDOG_SCRIPT=<copy> python3 test_board_watchdog_backlog_gate.py
A mutant must fail at least one check.
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
D = 24 * 3600            # one day
STALE = 14 * D           # window of rule 3
FAILED = []


def check(name, cond, detail=""):
    if cond:
        print("  ok %s" % name)
    else:
        FAILED.append(name)
        print("  FAIL %s %s" % (name, detail))


def load_mod():
    spec = importlib.util.spec_from_file_location("wd_backlog_test", SCRIPT)
    assert spec is not None and spec.loader is not None, SCRIPT
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_board(tmp, tasks=(), runs=(), events=()):
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
    con.executemany("insert into task_runs (task_id,started_at) values (?,?)",
                    runs)
    con.executemany(
        "insert into task_events (task_id,kind,created_at) values (?,?,?)",
        events)
    con.commit()
    con.close()
    return db


LINKAGE = {
    "ts": time.time(),
    "ms_titles": {7: "2.3.0"},
    "ms_issues": {229: "open", 262: "closed"},
    "open_pr_refs": {245: [229], 202: [190], 166: []},
    "direct_ms_prs": [166],
}


# Only milestone 2.3.0 (number 3) is open: the issues of the closed
# milestones are absent from the linkage altogether, so their cards get parked.
LINKAGE_ONE_OPEN = {
    "ts": time.time(),
    "ms_titles": {3: "2.3.0"},
    "ms_issues": {251: "open"},
    "open_pr_refs": {292: [251], 202: [190]},
    "direct_ms_prs": [],
    "owner_review_prs": [],
}

LINKAGE_EMPTY = {
    "ts": time.time(),
    "ms_titles": {},
    "ms_issues": {},
    "open_pr_refs": {},
    "direct_ms_prs": [],
    "owner_review_prs": [],
}


class Env:
    """Patches the module to a temporary board and a CLI stub.

    The stub reproduces the kanban core semantics: comment changes nothing,
    block blocks, a repeated block of the same kind after unblock routes to
    triage (BLOCK_RECURRENCE_LIMIT=2), archive archives.
    """

    def __init__(self, mod, tasks=(), runs=(), events=()):
        self.mod = mod
        self.tmp = tempfile.mkdtemp(prefix="wd-backlog-")
        self.db = make_board(self.tmp, tasks, runs, events)
        self.runs = []
        self.blocks = {}
        self._old = (mod.BOARDS, mod.BACKLOG_HOLD, mod.BACKLOG_CACHE,
                     mod.run, mod.backlog_fetch_linkage)
        mod.BOARDS = self.tmp
        mod.BACKLOG_HOLD = os.path.join(self.tmp, "hold")
        mod.BACKLOG_CACHE = os.path.join(self.tmp, "linkage.json")
        json.dump(LINKAGE, open(mod.BACKLOG_CACHE, "w"))
        mod.backlog_fetch_linkage = self._no_fetch
        mod.run = self._fake_run

    def _no_fetch(self):
        raise AssertionError("gh must not be called: the cache is fresh")

    def _fake_run(self, cmd, timeout=None):
        self.runs.append(cmd)
        verb = cmd[4] if len(cmd) > 4 else ""
        tid = cmd[5] if len(cmd) > 5 else ""
        con = sqlite3.connect(self.db)
        if verb == "block":
            con.execute("update tasks set status='blocked' where id=?",
                        (tid,))
        elif verb == "unblock":
            con.execute("update tasks set status='ready' where id=?", (tid,))
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

    def restore(self):
        (self.mod.BOARDS, self.mod.BACKLOG_HOLD, self.mod.BACKLOG_CACHE,
         self.mod.run, self.mod.backlog_fetch_linkage) = self._old


def classify_case(mod, title, body, assignee="coder"):
    return mod.backlog_classify({"title": title, "body": body,
                                 "assignee": assignee}, LINKAGE)


def main():
    now = time.time()
    mod = load_mod()
    print("module: %s" % SCRIPT)

    print("[1] classification, rule 2")
    check("ops", classify_case(mod, "check nodes", "", assignee="ops")
          == ("keep", "ops", None))
    check("ms issue open", classify_case(mod, "#229 task", "")
          [0:2] == ("keep", "milestone"))
    check("ms issue closed", classify_case(mod, "#262 task", "")
          == ("keep", "milestone_stale_closed", [262]))
    check("pr via refs", classify_case(mod, "PR #245", "")
          == ("keep", "milestone", [245]))
    check("no refs", classify_case(mod, "estimate the cost", "")
          == ("demote", "no_ref", None))
    check("pr not ms", classify_case(mod, "PR #202", "")
          == ("demote", "pr_not_milestone", [202]))
    check("pr ms direct = keep", classify_case(mod, "PR #166", "")
          == ("keep", "milestone", [166]))
    check("issue not ms", classify_case(mod, "#999 task", "")
          == ("demote", "issue_not_milestone", [999]))

    print("[2] p0 without movement: comments are not movement")
    env = Env(mod, tasks=[
        ("t_old", "old p0", "", "coder", "todo", 0, now - 20 * D, "user"),
        ("t_freshc", "old p0 fresh comments", "", "coder", "todo", 0,
         now - 20 * D, "user"),
        ("t_run", "old p0 fresh run", "", "coder", "todo", 0,
         now - 20 * D, "user"),
        ("t_move", "old p0 fresh status", "", "coder", "ready", 0,
         now - 20 * D, "user"),
        ("t_young", "young p0", "", "coder", "todo", 0, now - 10 * D,
         "user"),
        ("t_p5", "old p5", "", "coder", "todo", 5, now - 20 * D, "user"),
        ("t_bl", "old p0 blocked", "", "coder", "blocked", 0,
         now - 20 * D, "user"),
    ], runs=[("t_run", now - 2 * D)],
        events=[("t_freshc", "commented", now - 1 * D),
                ("t_move", "promoted", now - 2 * D)])
    ids = {t for t, _ in mod.backlog_p0_stale(SLUG, now)}
    check("old without movement", "t_old" in ids)
    check("a comment is not movement", "t_freshc" in ids)
    check("a run is movement", "t_run" not in ids)
    check("a status change is movement", "t_move" not in ids)
    check("young is skipped", "t_young" not in ids)
    check("non-p0 is skipped", "t_p5" not in ids)
    check("blocked is skipped", "t_bl" not in ids)
    env.restore()

    print("[3] hold and slug")
    env = Env(mod, tasks=[("t_a", "#229 work", "", "coder", "ready", 80,
                           now, "agent")])
    open(mod.BACKLOG_HOLD, "w").close()
    check("hold keeps it silent",
          mod.backlog_gate(SLUG, {}, now=now) == [])
    os.remove(mod.BACKLOG_HOLD)
    check("another slug is silent",
          mod.backlog_gate("other-board", {}, now=now) == [])
    env.restore()

    print("[4] parking ready through schedule")
    env = Env(mod, tasks=[
        ("t_keep", "#229 work", "", "coder", "ready", 80, now, "agent"),
        ("t_ops", "check nodes", "", "ops", "ready", 60, now, "agent"),
        ("t_bad", "#999 out of scope", "", "coder", "ready", 70, now,
         "agent"),
    ])
    st = {}
    out = mod.backlog_gate(SLUG, st, now=now)
    keys = [k for k, _ in out]
    stt = env.statuses()
    check("parked to scheduled", stt.get("t_bad") == "scheduled", str(stt))
    check("milestone card stays", stt.get("t_keep") == "ready", str(stt))
    check("ops stays", stt.get("t_ops") == "ready", str(stt))
    check("sweep key", any(k.startswith("backlog-sweep:") for k in keys),
          str(keys))
    check("schedule without block",
          any(c[4] == "schedule" and c[5] == "t_bad" for c in env.runs)
          and not any(c[4] == "block" for c in env.runs),
          str([c[4:] for c in env.runs]))
    check("marker in the parking reason",
          any(c[4] == "schedule" and any(
              mod.BACKLOG_PARK_MARKER in str(x) for x in c)
              for c in env.runs))
    check("category in state",
          (st.get("backlog_swept") or {}).get("t_bad", {}).get("cat")
          == "issue_not_milestone")
    env.restore()

    print("[5] cap of 40 per run")
    tasks = [("t_b%02d" % i, "#999 junk", "", "coder", "ready", 80, now,
              "agent") for i in range(45)]
    env = Env(mod, tasks=tasks)
    st = {}
    mod.backlog_gate(SLUG, st, now=now)
    n_sched = sum(1 for s in env.statuses().values() if s == "scheduled")
    check("no more than 40 per run", n_sched == 40, str(n_sched))
    check("swept in state is 40", len(st.get("backlog_swept") or {}) == 40)
    env.restore()

    print("[6] p0 archive")
    env = Env(mod, tasks=[
        ("t_p0old", "old p0", "", "coder", "todo", 0, now - 20 * D,
         "user"),
    ])
    st = {}
    out = mod.backlog_gate(SLUG, st, now=now)
    stt = env.statuses()
    check("archived", stt.get("t_p0old") == "archived", str(stt))
    check("p0 key", any(k.startswith("backlog-p0:") for k, _ in out))
    check("comment before the archive",
          any(c[4] == "comment" and c[5] == "t_p0old" for c in env.runs))
    check("archive recorded in state",
          "t_p0old" in (st.get("backlog_archived") or {}))
    check("archive in the digest",
          any("archived in the last day" in t and "t_p0old" in t
              for _, t in out))
    env.restore()

    print("[7] digest once a day")
    env = Env(mod, tasks=[
        ("t_d1", "#999 junk", "", "coder", "ready", 80, now, "agent"),
    ])
    st = {}
    first = [k for k, _ in mod.backlog_gate(SLUG, st, now=now)]
    second = [k for k, _ in mod.backlog_gate(SLUG, st, now=now)]
    check("first run carries a digest",
          any(k.startswith("backlog-digest:") for k in first), str(first))
    check("second run the same day has no digest",
          not any(k.startswith("backlog-digest:") for k in second),
          str(second))
    check("digest_day in state",
          (st.get("backlog") or {}).get("digest_day") is not None)
    st2 = json.loads(json.dumps(st))
    st2.setdefault("backlog", {}).pop("digest_day", None)
    check("digest text carries the id and the category",
          any("t_d1" in t and "issue_not_milestone" in t
              for _, t in mod.backlog_gate(
                  SLUG, st2, now=now + 3600, apply=False,
                  ignore_hold=True)))
    st3 = json.loads(json.dumps(st))
    st3["backlog_swept"] = {}
    st3.setdefault("backlog", {}).pop("digest_day", None)
    check("digest comes from the marker, not from state",
          any("t_d1" in t and "issue_not_milestone" in t
              for _, t in mod.backlog_gate(
                  SLUG, st3, now=now + 3600, apply=False,
                  ignore_hold=True)))
    env.restore()

    print("[8] a dry run does not mutate")
    env = Env(mod, tasks=[
        ("t_d2", "#999 junk", "", "coder", "ready", 80, now, "agent"),
        ("t_p0d", "old p0", "", "coder", "todo", 0, now - 20 * D, "user"),
    ])
    st = {}
    mod.backlog_gate(SLUG, st, now=now, apply=False, ignore_hold=True)
    stt = env.statuses()
    check("no CLI mutation", env.runs == [],
          str([c[4:] for c in env.runs]))
    check("statuses untouched",
          stt.get("t_d2") == "ready" and stt.get("t_p0d") == "todo",
          str(stt))
    check("state untouched", st == {}, str(st))
    env.restore()

    print("[9] a dry run goes through hold, apply stays silent")
    env = Env(mod, tasks=[
        ("t_d3", "#999 junk", "", "coder", "ready", 80, now, "agent"),
    ])
    open(mod.BACKLOG_HOLD, "w").close()
    check("apply under hold is silent",
          mod.backlog_gate(SLUG, {}, now=now) == [])
    dry = mod.backlog_gate(SLUG, {}, now=now, apply=False,
                           ignore_hold=True)
    check("dry run through hold sees the sweep",
          any(k.startswith("backlog-sweep:") for k, _ in dry), str(dry))
    check("dry run did not mutate", env.runs == [])
    os.remove(mod.BACKLOG_HOLD)
    env.restore()

    print("[10] the review layer does not re-arm a scheduled card")
    env = Env(mod, tasks=[
        ("t_rev", "review of PR #202", "#202 head", "reviewer", "scheduled",
         100, now, "watchdog"),
    ])
    con = sqlite3.connect(env.db)
    con.execute("update tasks set idempotency_key='reviewer-pr202-abcdef1'"
                " where id='t_rev'")
    con.commit()
    con.close()
    armed_calls = []
    patches = {
        "board_repo": lambda s: "x/y",
        "run": lambda cmd, timeout=None: json.dumps(
            [{"number": 202, "isDraft": False}]),
        "_pr_review_state": lambda repo, n: {
            "head": "abcdef1234", "age": 2 * 3600, "verdict": False,
            "info": None, "wcfy_at": None},
        "_review_arm_card": lambda *a, **k: armed_calls.append(a) or "t_new",
    }
    saved = {k: getattr(mod, k) for k in patches}
    for k, v in patches.items():
        setattr(mod, k, v)
    findings = mod.review_layer_check(SLUG)
    check("a scheduled reviewer card is alive for the review layer",
          not armed_calls, str(armed_calls))
    check("no repeated arm in findings",
          not any("revlayer:armed" in (f[0] if isinstance(f, tuple) else f)
                  for f in findings), str(findings)[:200])
    for k, v in saved.items():
        setattr(mod, k, v)
    env.restore()

    print("[11] the hints and commands of the watchdog exist")
    import subprocess
    src = open(SCRIPT, encoding="utf-8").read()
    valid = None
    cli_broken = ""
    # `hermes kanban --help` sometimes answers "no dependency environment is
    # committed" while an installation updates itself. That is an external
    # cause, not a watchdog defect: 3 retries, then an honest SKIP, not FAIL.
    for attempt in range(3):
        try:
            hout = subprocess.run([mod.HERMES, "kanban", "--help"],
                                  capture_output=True, text=True, timeout=60)
            m = re.search(r"\{([a-z,\-]+)\}", hout.stdout or "")
            if m:
                valid = set(m.group(1).split(","))
                break
            cli_broken = (hout.stderr or "empty stdout").strip()[:120]
        except FileNotFoundError:
            # No Hermes on this machine (a CI container): same honest SKIP.
            cli_broken = "hermes is not installed"
            break
        except Exception as exc:
            check("hermes kanban --help is available", False, str(exc))
        time.sleep(2)
    if valid is None and cli_broken:
        print("  SKIP command list: hermes CLI unavailable (%s)" % cli_broken)
    else:
        check("command list recognized", valid is not None
              and {"schedule", "promote", "complete"} <= (valid or set()),
              str(sorted(valid or []))[:120])
    if valid is not None:
        cands = set()
        verb_re = re.compile(r"hermes kanban\s+(?:--board\s+\S+\s+)?"
                             r"([a-z][a-z\-]*)")
        for lit in re.findall(r'"((?:[^"\\]|\\.)*)"', src):
            cands.update(verb_re.findall(lit))
        for a, b in re.findall(
                r'"((?:[^"\\]|\\.)*)"\s*\n?\s*"((?:[^"\\]|\\.)*)"', src):
            cands.update(verb_re.findall(a + " " + b))
        for sl in re.findall(r"\[HERMES,\s*\"kanban\"[^\]]*\]", src):
            toks = [t for t in re.findall(r'"([^"]+)"', sl)
                    if t != "kanban" and not t.startswith("-")]
            if toks:
                cands.add(toks[0])
        bad = cands - valid
        check("all hinted commands exist", not bad,
              "nonexistent: %s (checked %d)" % (sorted(bad), len(cands)))
        check("the move command is no longer advised", "move" not in cands)

    print("[12] only milestone 2.3.0 is open, names come from GitHub")
    # classify: an issue of the new milestone passes, closed ones do not
    check("issue of 2.3.0 passes",
          mod.backlog_classify({"title": "#251 work", "body": "",
                                "assignee": "coder"}, LINKAGE_ONE_OPEN)
          == ("keep", "milestone", [251]))
    check("PR of 2.3.0 passes through Refs",
          mod.backlog_classify({"title": "PR #292", "body": "",
                                "assignee": "coder"}, LINKAGE_ONE_OPEN)
          == ("keep", "milestone", [292]))
    check("issue of a closed milestone is parked",
          mod.backlog_classify({"title": "#229 work", "body": "",
                                "assignee": "coder"}, LINKAGE_ONE_OPEN)
          == ("demote", "issue_not_milestone", [229]))
    check("PR on an issue of a closed milestone is parked",
          mod.backlog_classify({"title": "PR #202", "body": "",
                                "assignee": "coder"}, LINKAGE_ONE_OPEN)
          == ("demote", "pr_not_milestone", [202]))
    # gate: an issue without a milestone is parked, the reason carries the
    # name taken from GitHub
    env = Env(mod, tasks=[
        ("t_46in", "#251 work 2.3.0", "", "coder", "ready", 80, now,
         "agent"),
        ("t_46out", "#999 out of focus", "", "coder", "ready", 70, now,
         "agent"),
    ])
    json.dump(LINKAGE_ONE_OPEN, open(mod.BACKLOG_CACHE, "w"))
    st = {}
    mod.backlog_gate(SLUG, st, now=now)
    stt = env.statuses()
    check("issue of 2.3.0 stays in ready", stt.get("t_46in") == "ready",
          str(stt))
    check("issue without a milestone is parked",
          stt.get("t_46out") == "scheduled", str(stt))
    sched = [c for c in env.runs if c[4] == "schedule"]
    check("the reason names 2.3.0",
          any("2.3.0" in " ".join(map(str, c)) for c in sched),
          str([c[6:] for c in sched])[:160])
    check("the reason has no names of old releases",
          not any(("1.2.0" in " ".join(map(str, c))
                   or "2.2.0" in " ".join(map(str, c))) for c in sched),
          str([c[6:] for c in sched])[:160])
    env.restore()

    print("[13] no open milestones, nothing is parked")
    env = Env(mod, tasks=[
        ("t_46e", "#999 junk", "", "coder", "ready", 80, now, "agent"),
    ])
    json.dump(LINKAGE_EMPTY, open(mod.BACKLOG_CACHE, "w"))
    st = {}
    out = mod.backlog_gate(SLUG, st, now=now)
    stt = env.statuses()
    check("nothing parked", stt.get("t_46e") == "ready", str(stt))
    check("no schedule call",
          not any(c[4] == "schedule" for c in env.runs),
          str([c[4:] for c in env.runs]))
    check("backlog-nomilestones key",
          any(k == "backlog-nomilestones" and "parks nothing" in t
              for k, t in out), str([k for k, _ in out]))
    env.restore()

    print("[14] the linkage reads state=open, no name filter")
    real_fetch = mod.backlog_fetch_linkage
    env = Env(mod)
    os.remove(mod.BACKLOG_CACHE)
    gh_calls = []

    def gh_run(cmd, timeout=None):
        gh_calls.append(cmd)
        j = " ".join(cmd)
        if "milestones?state=open" in j:
            return json.dumps([{"number": 3, "title": "2.3.0",
                                "state": "open"}])
        if "milestones?state=all" in j:
            # what a mutant with the old query would get: closed ones too
            return json.dumps([
                {"number": 1, "title": "1.2.0 (final)", "state": "closed"},
                {"number": 2, "title": "2.2.0 (parity)", "state": "closed"},
                {"number": 3, "title": "2.3.0", "state": "open"}])
        if "milestone=3" in j:
            return json.dumps([{"number": 251, "state": "open"}])
        if cmd[:3] == ["gh", "pr", "list"]:
            return json.dumps([
                {"number": 292, "body": "Refs #251", "latestReviews": []},
                {"number": 202, "body": "Refs #190", "latestReviews": []}])
        return ""

    saved_run = mod.run
    mod.run = gh_run
    mod.backlog_fetch_linkage = real_fetch  # the real one, not the Env stub
    data = mod.backlog_fetch_linkage()
    mod.run = saved_run
    check("milestones requested with state=open",
          any("milestones?state=open" in " ".join(c) for c in gh_calls),
          str(gh_calls)[:160])
    check("state=all is not requested",
          not any("milestones?state=all" in " ".join(c)
                  for c in gh_calls))
    check("ms_titles from the open ones",
          (data or {}).get("ms_titles") == {3: "2.3.0"}, str(data)[:160])
    check("issue of the open milestone in the linkage",
          (data or {}).get("ms_issues") == {251: "open"}, str(data)[:160])
    check("PR of the new milestone passes on fresh data",
          mod.backlog_classify({"title": "PR #292", "body": "",
                                "assignee": "coder"},
                               data)[0:2] == ("keep", "milestone"))
    cache = json.load(open(mod.BACKLOG_CACHE))
    check("cache of the new format is written", "ms_titles" in cache
          and cache.get("ms_titles") == {"3": "2.3.0"}, str(cache)[:120])
    env.restore()

    print("[15] a cache of the old format is unusable, a refresh follows")
    env = Env(mod, tasks=[("t_46x", "#251 work", "", "coder", "ready",
                           80, now, "agent")])
    old_format = dict(LINKAGE_ONE_OPEN)
    old_format.pop("ms_titles")
    json.dump(old_format, open(mod.BACKLOG_CACHE, "w"))
    fetches = []

    def spy_fetch():
        fetches.append(1)
        return dict(LINKAGE_ONE_OPEN, ts=time.time())

    saved_fetch = mod.backlog_fetch_linkage
    mod.backlog_fetch_linkage = spy_fetch
    data, fresh = mod.backlog_linkage(now)
    mod.backlog_fetch_linkage = saved_fetch
    check("old cache re-read through fetch", len(fetches) == 1,
          str(len(fetches)))
    check("data is fresh and has ms_titles", fresh is True
          and (data or {}).get("ms_titles") == {3: "2.3.0"}, str(data)[:80])
    env.restore()

    if FAILED:
        print("FAILED: %s" % ", ".join(FAILED))
        sys.exit(1)
    print("BACKLOG GATE: all tests green")


if __name__ == "__main__":
    main()
