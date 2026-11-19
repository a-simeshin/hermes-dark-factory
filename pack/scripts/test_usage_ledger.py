#!/usr/bin/env python3
"""Tests for usage-ledger.py.

  1  FACTORY_PRICES unset: a new card row is reported as unpriced, the total
     says "unknown", not zero credits and not a guessed number
  2  FACTORY_PRICES set: the same row is priced by the configured numbers
  3  attribution: a kanban session whose title has no card id is tied to its
     card through the board's owner_admitted event; one without an event
     stays "other"; rows carry source, st and en
  4  a second run does not grow the ledger; a legacy row of a session inside
     the rebuilt window is replaced by a fresh, attributed one
  5  comments: done cards found on the first board listing are a baseline
     (no comment); a run killed in the middle of the comment loop does not
     repeat the comments it already posted

Each case runs the script in a temporary HERMES_HOME with synthetic state.db
and kanban.db files. Case 1-2 have no `hermes` on PATH, so the board calls
fail quietly; cases 3-5 put a fake `hermes` first on PATH that prints a
board listing and records comments in a file.

The battery 3-5 then runs against mutated copies of the script, written to a
temp dir; each mutant must turn it red.
Run: python3 test_usage_ledger.py
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "usage-ledger.py")
FAILED = []
NOW = time.time()

FAKE_HERMES = r'''#!%s
import os, sys
home = os.environ["HERMES_HOME"]
args = sys.argv[1:]
if "list" in args:
    print(open(os.path.join(home, "fake-list.txt")).read())
elif "comment" in args:
    log = os.path.join(home, "comments.log")
    n = len(open(log).read().split()) if os.path.exists(log) else 0
    kill = os.path.join(home, "kill-at")
    if os.path.exists(kill) and n + 1 >= int(open(kill).read()):
        os.remove(kill)
        os.kill(os.getppid(), 9)          # the run dies during this comment
        sys.exit(1)
    with open(log, "a") as fh:
        fh.write(args[args.index("comment") + 1] + "\n")
'''


def check(name, cond, detail="", fails=None):
    fails = FAILED if fails is None else fails
    if fails is FAILED:
        print(("  ok   " if cond else "  FAIL ") + name + (" " + detail if detail and not cond else ""))
    if not cond:
        fails.append(name)


def make_home():
    home = tempfile.mkdtemp(prefix="usage-ledger-home-")
    con = sqlite3.connect(os.path.join(home, "state.db"))
    con.execute("create table sessions (id text, source text, title text, model text,"
                " input_tokens int, output_tokens int, cache_read_tokens int,"
                " reasoning_tokens int, started_at real, ended_at real, profile_name text)")
    con.execute("insert into sessions values ('s1', 'kanban', 'work on t_0000abcd',"
                " 'model-a', 10000, 1000, 0, 0, 1000, 2000, 'coder')")
    con.commit()
    con.close()
    return home


def run(prices):
    home = make_home()
    env = {"HERMES_HOME": home, "PATH": os.path.dirname(sys.executable),
           "FACTORY_OWNER_LOGIN": "owner-login", "FACTORY_OWNER_CHAT": "telegram:0"}
    if prices is not None:
        env["FACTORY_PRICES"] = prices
    try:
        done = subprocess.run([sys.executable, SCRIPT], env=env, capture_output=True,
                              text=True, timeout=120)
    finally:
        shutil.rmtree(home, ignore_errors=True)
    return done.returncode, done.stdout + done.stderr


# ---------- battery 3-5: attribution, rebuild, comment protection ----------

def add_sessions(home, rows):
    os.makedirs(os.path.join(home, "profiles", "coder"), exist_ok=True)
    con = sqlite3.connect(os.path.join(home, "profiles", "coder", "state.db"))
    con.execute("create table if not exists sessions (id text primary key, source text,"
                " title text, model text, input_tokens int, output_tokens int,"
                " cache_read_tokens int, reasoning_tokens int, started_at real,"
                " ended_at real, profile_name text)")
    con.executemany("insert into sessions values (?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()


def admit(home, tid, sid):
    bdir = os.path.join(home, "kanban", "boards", "main")
    os.makedirs(bdir, exist_ok=True)
    con = sqlite3.connect(os.path.join(bdir, "kanban.db"))
    con.execute("create table if not exists task_events (id integer primary key"
                " autoincrement, task_id text, run_id integer, kind text,"
                " payload text, created_at real)")
    con.execute("insert into task_events (task_id, run_id, kind, payload, created_at)"
                " values (?,?,?,?,?)",
                (tid, 1, "owner_admitted",
                 json.dumps({"db": "profiles/coder/state.db", "session_id": sid}),
                 NOW - 7000))
    con.commit()
    con.close()


def sess(sid, source, title, st):
    return (sid, source, title, "model-a", 10000, 1000, 0, 0, st, st + 600, "coder")


def battery(script):
    """Cases 3-5 against ``script``; returns the names of failed checks."""
    fails = []
    sbx = tempfile.mkdtemp(prefix="usage-ledger-r-")
    home = os.path.join(sbx, "home")
    bindir = os.path.join(sbx, "bin")
    os.makedirs(home)
    os.makedirs(bindir)
    with open(os.path.join(bindir, "hermes"), "w") as fh:
        fh.write(FAKE_HERMES % sys.executable)
    os.chmod(os.path.join(bindir, "hermes"), 0o755)
    env = {"HERMES_HOME": home, "FACTORY_BOARD": "main",
           "FACTORY_PRICES": "model-a=10/1/100",
           "FACTORY_OWNER_LOGIN": "owner-login", "FACTORY_OWNER_CHAT": "telegram:0",
           "PATH": bindir + os.pathsep + os.path.dirname(sys.executable)}
    ledger = os.path.join(home, "usage-ledger", "ledger.jsonl")

    def go():
        return subprocess.run([sys.executable, script], env=env, capture_output=True,
                              text=True, timeout=120)

    def rows():
        if not os.path.exists(ledger):
            return []
        return [json.loads(ln) for ln in open(ledger) if ln.strip()]

    def comments():
        p = os.path.join(home, "comments.log")
        return open(p).read().split() if os.path.exists(p) else []

    def listing(*tids):
        with open(os.path.join(home, "fake-list.txt"), "w") as fh:
            fh.write("\n".join("%s  done  coder  card %s" % (t, t) for t in tids))

    try:
        # 3) attribution through owner_admitted
        add_sessions(home, [
            sess("k-old", "kanban", "Fix the drift in the timeline", NOW - 9000),
            sess("k-none", "kanban", "untitled worker session", NOW - 8000),
            sess("c-1", "cron", "nightly stand · run", NOW - 7000),
            sess("o-1", "oneshot", "ping", NOW - 6000),
        ])
        admit(home, "t_0000aaaa", "k-old")
        listing("t_0000aaaa")
        go()
        by = {r["session"]: r for r in rows()}
        r = by.get("k-old", {})
        check("3a title without id, admitted: card t_0000aaaa",
              r.get("kind") == "card" and r.get("ref") == "t_0000aaaa", str(r), fails)
        check("3b rows carry source, st, en",
              r.get("source") == "kanban" and r.get("st") and r.get("en"), str(r), fails)
        check("3c kanban without event: other/kanban",
              (by.get("k-none", {}).get("kind"), by.get("k-none", {}).get("ref"))
              == ("other", "kanban"), str(by.get("k-none")), fails)
        check("3d cron: research", by.get("c-1", {}).get("kind") == "research",
              str(by.get("c-1")), fails)
        check("5a done card on the first listing is a baseline: no comment",
              comments() == [], str(comments()), fails)

        # 4) idempotent; a legacy row inside the window is rebuilt
        n1 = len(rows())
        go()
        check("4a second run does not grow the ledger", len(rows()) == n1,
              "%d -> %d" % (n1, len(rows())), fails)
        with open(ledger, "a") as fh:
            fh.write(json.dumps({"ts": int(NOW - 8400), "profile": "coder",
                                 "session": "k-old", "kind": "other", "ref": "kanban",
                                 "model": "model-a", "input_tokens": 1,
                                 "output_tokens": 1, "cache_read_tokens": 1}) + "\n")
        go()
        old = [r for r in rows() if r["session"] == "k-old"]
        check("4b legacy row replaced by the attributed one",
              len(old) == 1 and old[0]["kind"] == "card", str(old), fails)
        check("4c ledger size unchanged after rebuild", len(rows()) == n1,
              "%d -> %d" % (n1, len(rows())), fails)

        # 5) three new done cards; the run dies on the second comment
        new = [("t_0000bbb1", "k-b1"), ("t_0000bbb2", "k-b2"), ("t_0000bbb3", "k-b3")]
        add_sessions(home, [sess(s, "kanban", "card work", NOW - 3000) for _, s in new])
        for t, s in new:
            admit(home, t, s)
        listing("t_0000aaaa", *[t for t, _ in new])
        with open(os.path.join(home, "kill-at"), "w") as fh:
            fh.write("2")
        go()
        check("5b killed run posted the first comment only",
              comments() == ["t_0000bbb1"], str(comments()), fails)
        go()
        got = comments()
        check("5c rerun posts the rest, nothing twice",
              sorted(got) == ["t_0000bbb1", "t_0000bbb2", "t_0000bbb3"], str(got), fails)
        go()
        check("5d third run posts nothing", len(comments()) == 3, str(comments()), fails)
    finally:
        shutil.rmtree(sbx, ignore_errors=True)
    return fails


MUTANTS = {
    "no-board-event": ("if sid and sid in (cards or {}):", "if False:"),
    "no-rebuild": ('if r["en"] >= cutoff', 'if r["en"] >= cutoff + 1e12'),
    "no-baseline": ('if listed and "done_baseline" not in state:', "if False:"),
    "no-save-per-comment": (
        "            # saved at once: a run killed mid-loop must not repeat comments\n"
        "            save_state(state, seen, commented)\n", ""),
}


def mutants():
    src = open(SCRIPT, encoding="utf-8").read()
    for name, (old, new) in MUTANTS.items():
        mut = src.replace(old, new, 1)
        if mut == src:
            check("mutant %s applies" % name, False)
            continue
        d = tempfile.mkdtemp(prefix="usage-ledger-mut-")
        try:
            for f in ("factory_config.py", "metrics-evening.py"):
                shutil.copy(os.path.join(HERE, f), d)
            path = os.path.join(d, "usage-ledger.py")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(mut)
            red = battery(path)
        finally:
            shutil.rmtree(d, ignore_errors=True)
        check("mutant %s turns the battery red" % name, bool(red))
        if red:
            print("     (red on: %s)" % ", ".join(red))


def main():
    print("1. FACTORY_PRICES unset")
    rc, out = run(None)
    check("exit 0", rc == 0, out[-300:])
    check("unpriced row reported as unknown",
          "new credits ~0 + unknown (1 unpriced rows)" in out, out[-300:])

    print("2. FACTORY_PRICES set")
    rc, out = run("model-a=10/1/100")
    # (10000 * 10 + 1000 * 100) / 1e4 = 20
    check("priced by the configured numbers", "new credits ~20;" in out, out[-300:])
    check("nothing unknown", "unknown" not in out, out[-300:])

    print("3-5. attribution, rebuild, comment protection")
    for name in battery(SCRIPT):
        check(name, False)
    if not FAILED:
        print("  ok   battery green")

    print("mutants")
    mutants()

    if FAILED:
        print("\nFAILED: %d" % len(FAILED))
        sys.exit(1)
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
