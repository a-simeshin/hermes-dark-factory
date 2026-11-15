#!/usr/bin/env python3
"""Tests of the holdout leak detector (holdout_guard).

Run: python3 test_board_watchdog_holdout_guard.py
Mutants: BOARD_WATCHDOG_SCRIPT=/tmp/mutant.py python3 test_board_watchdog_holdout_guard.py
A mutant must fail at least one test.

The signal is split by strength:
  HIGH (holdout-access:...): a tool call carrying a PATH of the hidden zone
  (cache/holdout, cache/scratch/rN, profiles/tester/cache|holdout);
  low (holdout-mention:...): a hidden-zone path in text, a tool result or
  reasoning, without a tool access.
The word holdout without a path (card titles [holdout-...], stand names
holdout-8311, worktree holdout-pr154, a profile's own scratch) is NOT a leak:
a routine board listing once gave 28 alert lines of which one was real. One
item is issued per session, and HIGH absorbs low.
"""
import atexit
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
_HOME = tempfile.mkdtemp(prefix="wd-home-")
atexit.register(shutil.rmtree, _HOME, True)
os.environ.update({
    "HERMES_HOME": _HOME,
    "FACTORY_REPO": "acme/widget",
    "FACTORY_REPO_PATH": "/srv/hermes/widget",
    "FACTORY_BOARD": "main",
    "FACTORY_OWNER_LOGIN": "owner-login",
    "FACTORY_OWNER_CHAT": "telegram:000000000",
})
SCRIPT = os.environ.get("BOARD_WATCHDOG_SCRIPT",
                        os.path.join(HERE, "board-watchdog.py"))
D = 24 * 3600
FAILED = []


def check(name, cond, detail=""):
    if cond:
        print("  ok %s" % name)
    else:
        FAILED.append(name)
        print("  FAIL %s %s" % (name, detail))


def load_mod():
    spec = importlib.util.spec_from_file_location("wd9_test", SCRIPT)
    assert spec is not None and spec.loader is not None, SCRIPT
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_profile_db(root, prof, rows):
    d = os.path.join(root, prof)
    os.makedirs(d, exist_ok=True)
    con = sqlite3.connect(os.path.join(d, "state.db"))
    con.executescript("""
      drop table if exists messages;
      create table messages (
        id integer primary key autoincrement,
        session_id text, role text, content text, tool_call_id text,
        tool_calls text, tool_name text, timestamp real,
        active integer default 1, reasoning text);
    """)
    con.executemany(
        "insert into messages (session_id, role, content, tool_calls,"
        " tool_name, timestamp, active, reasoning)"
        " values (?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()


def tc(name, args, ts_off=3600, sid="s-leak-0001"):
    return (sid, "assistant", None,
            json.dumps([{"function": {"name": name, "arguments": args}}]),
            name, time.time() - ts_off, 1, None)


# --- accesses to the hidden zone (HIGH) ----------------------------------
TOOLCALL_ROW = tc("read_file", {"path":
                   "/srv/hermes/.hermes/profiles/tester/holdout/2.2.0/154.md"})
# a grep over a holdout report
GREP_REPORT_ROW = tc("terminal", {"command":
                     "grep 'auth-token|server.port|exec.jar'"
                     " /srv/hermes/.hermes/cache/holdout/"
                     "report-2026-10-30.md"}, ts_off=5400, sid="s-leak-0004")
# the shared scratch of holdout rounds
SCRATCH_R21_ROW = tc("terminal", {"command":
                     "ls /srv/hermes/.hermes/cache/scratch/r21/"},
                     ts_off=5600, sid="s-leak-0005")
# a second access from the same session: the dedup must absorb it
TOOLCALL_DUP_ROW = tc("terminal", {"command":
                      "cat /srv/hermes/.hermes/cache/holdout/latest.md"})

# --- the path surfaced but nothing was accessed (low) ---------------------
TOOLRESULT_ROW = (
    "s-leak-0002", "tool",
    "cat: ~/.hermes/profiles/tester/holdout/2.2.0/189.md: scenario S1...",
    None, "terminal", time.time() - 7200, 1, None)

TEXT_ROW = (
    "s-leak-0003", "assistant",
    "checking the scenarios: ls profiles/tester/holdout/2.2.0/",
    None, None, time.time() - 1800, 1, None)

# --- noise: the word holdout without a hidden-zone path (silence) ---------
NOISE_TITLE_ROW = (            # kanban list output with a holdout card title
    "s-noise-01", "tool",
    "t_00000001 [done] coder | [holdout-189-af185b9] [release-blocker]"
    " mergeRequestHeaders (useConnection...",
    None, "hermes", time.time() - 900, 1, None)

NOISE_STAND_ROW = (
    "s-noise-02", "assistant", "stand holdout-8311 is up, port 8311",
    None, None, time.time() - 800, 1, None)

NOISE_WORKTREE_ROW = tc(      # worktree of a public PR, not the hidden zone
    "terminal", {"command": "git -C repo/.worktrees/holdout-pr154 log -1"},
    ts_off=700, sid="s-noise-03")

NOISE_OWN_SCRATCH_ROW = tc(   # a profile's own scratch, not the tester's
    "read_file", {"path":
                  "/srv/hermes/.hermes/profiles/coder/cache/scratch/"
                  "pr-body-v2.md"}, ts_off=600, sid="s-noise-04")

# a profile's own ROUND scratch (r177/r189/r205 of the reviewer, r4b1f of the
# coder): a worker's working directory, not the hidden zone (a first version
# of the regex gave 4 false HIGH out of 6 on live data)
NOISE_OWN_ROUND_ROW = tc(
    "terminal", {"command":
                 "cd /srv/hermes/.hermes/profiles/reviewer/cache/scratch/"
                 "r189 && git diff a296a53c..af185b91 -- starter-webmvc"},
    ts_off=500, sid="s-noise-05")

NOISE_OWN_ROUND_TILDE_ROW = tc(
    "execute_code", {"code":
                     "cd ~/.hermes/profiles/coder/cache/scratch/r4b1f;"
                     " ls after-connected.png"},
    ts_off=400, sid="s-noise-06")

NOISE_ROWS = [NOISE_TITLE_ROW, NOISE_STAND_ROW, NOISE_WORKTREE_ROW,
              NOISE_OWN_SCRATCH_ROW, NOISE_OWN_ROUND_ROW,
              NOISE_OWN_ROUND_TILDE_ROW]

OLD_ROW = (
    "s-old-0009", "assistant", "mentioned cache/holdout/report.md long ago",
    None, None, time.time() - 8 * D, 1, None)

INACTIVE_ROW = (
    "s-inact-01", "assistant", "cache/holdout in an inactive row",
    None, None, time.time() - 600, 0, None)

CLEAN_ROW = (
    "s-clean-01", "assistant", "ordinary work on the code",
    json.dumps([{"function": {"name": "terminal", "arguments": {
        "command": "mvn -pl :demo-webmvc verify"}}}]),
    "terminal", time.time() - 300, 1, None)


def main():
    print("holdout_guard, two signal levels")
    mod = load_mod()
    tmp = tempfile.mkdtemp(prefix="wd9-")

    # 1. coder: HIGH on a tool call; all the word noise is silent
    make_profile_db(tmp, "coder", [TOOLCALL_ROW, TOOLCALL_DUP_ROW,
                                   CLEAN_ROW] + NOISE_ROWS)
    make_profile_db(tmp, "reviewer", [CLEAN_ROW])
    out = mod.holdout_guard(time.time(), profiles_root=tmp)
    keys = [k for k, _ in out]
    check("a tool call is caught as HIGH",
          keys == ["holdout-access:coder:s-leak-0001"], str(keys))
    check("dedup: the second access of the same session is absorbed",
          len(out) == 1, str(keys))
    check("the alert carries the word HIGH and the path",
          "HIGH" in out[0][1] and "tester/holdout" in out[0][1],
          out[0][1] if out else "empty")
    check("noise (title/stand/worktree/own scratch) is silent",
          not any("s-noise" in k for k in keys), str(keys))

    # 2. cache/holdout and cache/scratch/rN in arguments
    make_profile_db(tmp, "coder", [GREP_REPORT_ROW, SCRATCH_R21_ROW,
                                   CLEAN_ROW] + NOISE_ROWS)
    make_profile_db(tmp, "reviewer", [CLEAN_ROW])
    out = mod.holdout_guard(time.time(), profiles_root=tmp)
    keys = sorted(k for k, _ in out)
    check("a grep of a holdout report and scratch r21 are HIGH",
          keys == ["holdout-access:coder:s-leak-0004",
                   "holdout-access:coder:s-leak-0005"], str(keys))
    check("HIGH comes first in the list",
          all("HIGH" in t for _, t in out), str(out))

    # 3. reviewer: a path in a tool result and in text is low, not an access
    make_profile_db(tmp, "coder", [CLEAN_ROW])
    make_profile_db(tmp, "reviewer", [TOOLRESULT_ROW, TEXT_ROW])
    out = mod.holdout_guard(time.time(), profiles_root=tmp)
    keys = sorted(k for k, _ in out)
    check("reviewer: two leak channels, both low",
          keys == ["holdout-mention:reviewer:s-leak-0002",
                   "holdout-mention:reviewer:s-leak-0003"], str(keys))
    check("the alert names the channel and the level",
          any("low" in t and "tool result" in t for _, t in out)
          and any("assistant message" in t for _, t in out), str(out))

    # 4. window: old and inactive rows are silent, a wider window sees them
    make_profile_db(tmp, "coder", [TOOLCALL_ROW, CLEAN_ROW, OLD_ROW,
                                   INACTIVE_ROW])
    make_profile_db(tmp, "reviewer", [CLEAN_ROW])
    out = mod.holdout_guard(time.time(), profiles_root=tmp)
    old_reported = any("s-old" in k or "s-inact" in k for k, _ in out)
    check("old and inactive rows are silent",
          not old_reported and len(out) == 1,
          str([k for k, _ in out]))
    out_short = mod.holdout_guard(time.time(), window_days=10,
                                  profiles_root=tmp)
    check("a wider window sees the old row",
          any("s-old-0009" in k for k, _ in out_short), str(out_short))

    # 5. both databases clean: zero findings, the watchdog is silent
    tmp2 = tempfile.mkdtemp(prefix="wd9-")
    make_profile_db(tmp2, "coder", [CLEAN_ROW])
    make_profile_db(tmp2, "reviewer", [CLEAN_ROW])
    out = mod.holdout_guard(time.time(), profiles_root=tmp2)
    check("clean databases: silence", out == [], str(out))

    # 6. a broken database does not crash the detector, it returns a finding
    tmp3 = tempfile.mkdtemp(prefix="wd9-")
    os.makedirs(os.path.join(tmp3, "coder"))
    open(os.path.join(tmp3, "coder", "state.db"), "wb").write(b"not sqlite")
    make_profile_db(tmp3, "reviewer", [CLEAN_ROW])
    out = mod.holdout_guard(time.time(), profiles_root=tmp3)
    check("broken database -> holdout-scan-fail",
          any(k.startswith("holdout-scan-fail:coder") for k, _ in out),
          str(out))

    print()
    if FAILED:
        print("FAIL: %d" % len(FAILED))
        sys.exit(1)
    print("OK: all holdout_guard tests are green")


if __name__ == "__main__":
    main()
