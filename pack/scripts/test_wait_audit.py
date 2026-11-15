#!/usr/bin/env python3
"""Tests of the waiting-discipline audit (the WAITING section of
metrics-evening.py).

Checks:
  - the sleep threshold: sleep 30 is allowed, 31 and above is not;
    time.sleep(N) is caught too;
  - dedup by tool_call_id: a display copy of a call does not double the time
    or the count;
  - the api_call_count>5 filter: small snapshot sessions stay out of the
    statistics;
  - the waiting share is deduplicated tool time / wall time;
  - render without violations says the target is held, with violations it
    lists them.

Run: python3 test_wait_audit.py
Mutant: WAIT_AUDIT_SCRIPT=/path/to/mutant.py python3 test_wait_audit.py
(the tests must fail on a mutant: a red run proves they check something).
"""
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# Never read or write the real Hermes home; neutral project values.
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="wait-audit-home-")
os.environ["FACTORY_REPO"] = "acme/widget"
os.environ["FACTORY_REPO_PATH"] = "/srv/hermes/widget"
os.environ["FACTORY_BOARD"] = "main"
os.environ["FACTORY_OWNER_LOGIN"] = "owner-login"
os.environ["FACTORY_OWNER_CHAT"] = "telegram:000000000"

SCRIPT = os.environ.get("WAIT_AUDIT_SCRIPT") or os.path.join(
    HERE, "metrics-evening.py")

spec = importlib.util.spec_from_file_location("wait_audit", SCRIPT)
if spec is None or spec.loader is None:
    print("SKIP: cannot load metrics-evening.py")
    sys.exit(0)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

FAILED = []


def check(name, cond):
    print(("  ok  " if cond else "  FAIL ") + name)
    if not cond:
        FAILED.append(name)


def make_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    con = sqlite3.connect(path)
    con.executescript("""
        create table sessions(
            id text primary key, started_at real, ended_at real,
            last_activity_at real, api_call_count integer);
        create table messages(
            id integer primary key autoincrement, session_id text,
            role text, tool_calls text, tool_call_id text, timestamp real);
    """)
    t0 = time.time() - 3600
    # a worker session: sleep 400 (violation) + a normal call + display duplicates
    con.execute("insert into sessions values('s1',?,?,?,10)",
                (t0, t0 + 800, t0 + 800))
    calls = [
        ("c1", "terminal", {"command": "sleep 400; gh pr checks 9"}, t0 + 10, t0 + 410),
        ("c2", "terminal", {"command": "sleep 30 && gh run list"}, t0 + 420, t0 + 450),
        ("c3", "terminal", {"command": "python3 -c 'import time; time.sleep(45)'"},
         t0 + 460, t0 + 505),
    ]
    mid = 0
    for cid, name, args, ts_a, ts_t in calls:
        mid += 1
        con.execute("insert into messages(session_id,role,tool_calls,timestamp)"
                    " values('s1','assistant',?,?)",
                    (json.dumps([{"id": cid, "function": {
                        "name": name, "arguments": json.dumps(args)}}]), ts_a))
        con.execute("insert into messages(session_id,role,tool_call_id,timestamp)"
                    " values('s1','tool',?,?)", (cid, ts_t))
        # display copy: the same tool_call_id, the same timestamps, inserted later
        mid += 2
        con.execute("insert into messages(session_id,role,tool_calls,timestamp)"
                    " values('s1','assistant',?,?)",
                    (json.dumps([{"id": cid, "function": {
                        "name": name, "arguments": json.dumps(args)}}]), ts_a))
        con.execute("insert into messages(session_id,role,tool_call_id,timestamp)"
                    " values('s1','tool',?,?)", (cid, ts_t))
    # a small snapshot session: excluded by the api_call_count>5 filter
    con.execute("insert into sessions values('s2',?,?,?,2)",
                (t0, t0 + 100, t0 + 100))
    con.execute("insert into messages(session_id,role,tool_calls,timestamp)"
                " values('s2','assistant',?,?)",
                (json.dumps([{"id": "x1", "function": {
                    "name": "terminal",
                    "arguments": json.dumps({"command": "sleep 99"})}}]), t0))
    con.execute("insert into messages(session_id,role,tool_call_id,timestamp)"
                " values('s2','tool',?,?)", ("x1", t0 + 99))
    # a build session: a targeted foreground build, a background mvn
    # (background=true), process_manage wait, an edit, two full verifies
    # with no edit between them
    con.execute("insert into sessions values('s3',?,?,?,10)",
                (t0, t0 + 460, t0 + 460))
    extra = [
        ("c4", "terminal",
         {"command": "./mvnw -o -q -pl core -am -Dtest=X test"}, t0 + 10, t0 + 70),
        ("c5", "terminal",
         {"command": "./mvnw -pl core verify 2>&1 | tail -5",
          "background": True}, t0 + 80, t0 + 85),
        ("c6", "process_manage", {"action": "wait", "session_id": "p1"},
         t0 + 80, t0 + 140),
        ("c7", "patch", {"path": "a.py"}, t0 + 150, t0 + 155),
        ("c8", "terminal", {"command": "./mvnw verify 2>&1 | tail -20"},
         t0 + 160, t0 + 300),
        ("c9", "terminal", {"command": "mvn -q verify"}, t0 + 310, t0 + 450),
    ]
    for cid, name, args, ts_a, ts_t in extra:
        con.execute("insert into messages(session_id,role,tool_calls,timestamp)"
                    " values('s3','assistant',?,?)",
                    (json.dumps([{"id": cid, "function": {
                        "name": name, "arguments": json.dumps(args)}}]), ts_a))
        con.execute("insert into messages(session_id,role,tool_call_id,timestamp)"
                    " values('s3','tool',?,?)", (cid, ts_t))
    con.commit()
    con.close()
    return path


def main():
    print("sleep_hits, threshold 30 s:")
    check("sleep 30 is not a violation", mod.sleep_hits("sleep 30 && gh run list") == [])
    check("sleep 31 is a violation", mod.sleep_hits("sleep 31; date") == [31.0])
    check("sleep 420 waiting for CI", mod.sleep_hits("sleep 420 && gh pr checks 9") == [420.0])
    check("time.sleep(45) is a violation", mod.sleep_hits("time.sleep(45)") == [45.0])
    check("sleep 5 is not a violation", mod.sleep_hits("for i in 1..5; do sleep 5; done") == [])
    check("broken json does not crash", mod.cmd_of("terminal", "not json") == "")

    print("mvn_class, position of -pl:")
    check("./mvnw verify without -pl is full", mod.mvn_class("./mvnw verify") == "full-verify")
    check("mvn -q verify is full", mod.mvn_class("mvn -q verify") == "full-verify")
    check("-pl BEFORE verify is targeted", mod.mvn_class("./mvnw -pl core -am verify") == "pl-verify")
    check("-pl AFTER verify is targeted too", mod.mvn_class("./mvnw verify -pl core") == "pl-verify")
    check("--projects is targeted too", mod.mvn_class("mvn --projects core verify") == "pl-verify")
    check("-pl with test is targeted", mod.mvn_class("./mvnw -pl core -Dtest=X test") == "targeted")
    check("mvn test without -pl is bare", mod.mvn_class("./mvnw test") == "bare")
    check("not mvn", mod.mvn_class("gh pr checks 9") is None)

    print("bg_of, background detection:")
    check("background=true is background", mod.bg_of("terminal",
          json.dumps({"command": "./mvnw verify 2>&1 | tail", "background": True})))
    check("2>&1 without the flag is NOT background", not mod.bg_of("terminal",
          json.dumps({"command": "./mvnw verify 2>&1 | tail"})))
    check("process_manage is background", mod.bg_of("process_manage", "{}"))
    check("broken json is not background", not mod.bg_of("terminal", "not json"))

    print("overlap_minutes, across and inside a profile:")
    t = 1000.0
    check("two profiles 10 min -> 10", abs(mod.overlap_minutes(
        [(t, t + 600, "coder"), (t + 300, t + 1200, "reviewer")])) - 10 < 1e-6)
    check("one profile two intervals, cross -> 0",
          mod.overlap_minutes([(t, t + 600, "coder"),
                               (t + 300, t + 900, "coder")]) == 0)
    check("one profile two intervals, same -> 5 min",
          abs(mod.overlap_minutes([(t, t + 600, "coder"),
                                   (t + 300, t + 900, "coder")],
                                  cross=False)) - 5 < 1e-6)
    check("a single interval -> 0", mod.overlap_minutes(
        [(t, t + 600, "coder")], cross=False) == 0)
    check("three in a row without overlap -> 0", mod.overlap_minutes(
        [(t, t + 60, "a"), (t + 100, t + 160, "b"),
         (t + 200, t + 260, "c")]) == 0)

    print("repeat_verifies, only an edit resets:")
    check("verify,edit,verify -> 0", mod.repeat_verifies(
        [(1, "verify"), (2, "edit"), (3, "verify")]) == 0)
    check("verify,other,verify -> 1", mod.repeat_verifies(
        [(1, "verify"), (2, "other"), (3, "verify")]) == 1)
    check("verify x3 -> 2", mod.repeat_verifies(
        [(1, "verify"), (2, "verify"), (3, "verify")]) == 2)
    check("a single verify -> 0", mod.repeat_verifies([(1, "verify")]) == 0)

    print("profile_stats on a synthetic database:")
    db = make_db()
    st = mod.profile_stats(db, time.time() - 7200, time.time() + 60)
    os.unlink(db)
    # tool time: s1 475 s (deduplicated) + s3 410 s; s2 is excluded
    check("tool_t deduplicated = 885 s, not 1770", abs(st["tool_t"] - 885.0) < 1e-6)
    check("wall = 800+460 s", abs(st["wall"] - 1260.0) < 1e-6)
    secs = sorted(v["sec"] for v in st["viol"])
    check("sleep violations: 400 and 45 (30 is not in, s2 is excluded)",
          secs == [45.0, 400.0])
    cls = sorted(b["cls"] for b in st["builds"])
    check("builds: targeted + 2 full-verify (a background spawn is not in builds)",
          cls == ["full-verify", "full-verify", "targeted"])
    check("bg_spawns = 1 (background=true, not 2>&1)", st["bg_spawns"] == 1)
    check("waits = 1 process_manage interval", len(st["waits"]) == 1)
    check("repeat of a full verify without an edit = 1", st["repeats"] == 1)

    print("render:")
    base = dict(tool_t=0.0, wall=0.0, builds=[], bg_spawns=0, repeats=0,
                waits=[])
    data = {"coder": dict(viol=[{"sec": 400.0, "t0": time.time(),
                                 "cmd": "sleep 400; gh pr checks 9"}],
                          tool_t=400.0, wall=1000.0, builds=[], bg_spawns=0,
                          repeats=0, waits=[]),
            "reviewer": dict(base, viol=[]),
            "tester": dict(base, viol=[]),
            "architect": dict(base, viol=[]),
            "ops": dict(base, viol=[])}
    lines = "\n".join(mod.render(data, 24))
    check("violations are listed", "coder: 1" in lines and "2.9" not in lines)
    check("waiting share 40%", "40%" in lines)
    check("local builds line is present", "local builds" in lines
          and "median rework run" not in lines)
    clean = {"coder": dict(base, viol=[], tool_t=100.0, wall=1000.0)}
    for p in mod.PROFILES[1:]:
        clean[p] = dict(base, viol=[])
    lines2 = "\n".join(mod.render(clean, 24))
    check("without violations: target held", "target held" in lines2)
    check("no long dashes in the output", chr(0x2014) not in lines and chr(0x2014) not in lines2)

    if FAILED:
        print("\nFAILED: %d" % len(FAILED))
        sys.exit(1)
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
