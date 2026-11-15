#!/usr/bin/env python3
"""REPEAT_AFTER filter for the sources of main() that run before the
per-board loop.

Reason: holdout_guard returned the same leak every hour. main() wrote the
keys holdout-access: / holdout-mention: into kanban/watchdog-state.json but
did not read them: only the findings of check(slug) inside the board loop
passed the freshness filter, so every hourly run delivered the holdout block
to the owner although the docstring of holdout_guard promised "no more than
once a day".

The fix: the report sources before the loop pass the same filter
now - state.get(key, 0) > REPEAT_AFTER. max_in_progress_gate is
deliberately NOT wrapped in a second filter: it dedups itself with the same
REPEAT_AFTER inside, and the pair-of-parallel-cards dump has its own 6 hour
window, a double filter would stretch its cadence to a day; the test checks
the self-dedup in the source. The text "Leak detector failed" is
deliberately not filtered: a failure must repeat in every delivered report,
it has no key.

The sources before the board loop (holdout, stale, detector failure) are
routine and go to the daily digest, not to the push. The push message
carries the header "KANBAN NEEDS ATTENTION" only for an item that needs an
owner action; a real dispatcher stop is printed as the line "Dispatcher is
stopped: ..." inside the push. So scenarios 1-3 check the digest file and a
quiet stdout, not the push.

Run: python3 test_board_watchdog_repeat_filter.py
Mutant: BOARD_WATCHDOG_SCRIPT=/tmp/mutant.py \\
  python3 test_board_watchdog_repeat_filter.py
(a mutant without the filter must turn red: the second run of main() on the
same fixture appends the holdout block to the digest again).
"""
import importlib.util
import inspect
import io
import json
import os
import sqlite3
import sys
import tempfile
import time
from contextlib import redirect_stdout

# Project settings are read at import time: set neutral values first. The
# Hermes home is a temp directory: the test never touches ~/.hermes.
HERMES_HOME = tempfile.mkdtemp(prefix="bw-test-home-")
os.environ["HERMES_HOME"] = HERMES_HOME
os.environ.update({
    "FACTORY_REPO": "acme/widget",
    "FACTORY_REPO_PATH": "/srv/hermes/widget",
    "FACTORY_BOARD": "main",
    "FACTORY_OWNER_LOGIN": "owner-login",
    "FACTORY_OWNER_CHAT": "telegram:000000000",
    "FACTORY_BASE_BRANCHES": "main",
})

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.environ.get("BOARD_WATCHDOG_SCRIPT") or os.path.join(
    HERE, "board-watchdog.py")

spec = importlib.util.spec_from_file_location("board_watchdog", SCRIPT)
if spec is None or spec.loader is None:
    print("SKIP: cannot load board-watchdog.py")
    sys.exit(0)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

FAILED = []


def check(name, cond, detail=""):
    if cond:
        print("  ok %s" % name)
    else:
        FAILED.append(name)
        print("  FAIL %s %s" % (name, detail))


DIGEST_DIR = None


def bind_digest(digest_dir):
    """Routine of the sources goes to the cron_quiet digest, not to the push.

    cron_quiet is registered in sys.modules with a temporary DIGEST_DIR
    (board-watchdog imports it inside main), otherwise the test would write
    sections into a live digest.
    """
    global DIGEST_DIR
    os.makedirs(digest_dir, exist_ok=True)
    qspec = importlib.util.spec_from_file_location(
        "cron_quiet", os.path.join(HERE, "cron_quiet.py"))
    quiet = importlib.util.module_from_spec(qspec)
    qspec.loader.exec_module(quiet)
    quiet.DIGEST_DIR = digest_dir
    quiet._REPORTED = os.path.join(digest_dir, "reported.json")
    sys.modules["cron_quiet"] = quiet
    os.environ["CRON_DIGEST_DIR"] = digest_dir
    DIGEST_DIR = digest_dir


def digest_body():
    path = os.path.join(DIGEST_DIR, time.strftime("%Y-%m-%d") + ".md")
    return open(path).read() if os.path.exists(path) else ""


ACCESS_TOOL = ('[{"name":"Read","args":{"path":'
               '"/srv/hermes/.hermes/cache/holdout/'
               'report-2026-10-30.md"}}]')
MENTION_CONTENT = ("looking at the evidence from "
                   "/srv/hermes/.hermes/profiles/tester/cache/scratch/"
                   "probe.jsonl, it shows")


def make_profiles():
    """coder/state.db with two signals: HIGH (a tool call into the hidden
    area) and low (a path in the text)."""
    root = tempfile.mkdtemp(prefix="wd-repeat-profiles-")
    os.makedirs(os.path.join(root, "coder"))
    con = sqlite3.connect(os.path.join(root, "coder", "state.db"))
    con.executescript("""
        create table messages (id integer primary key, session_id text,
            role text, content text, tool_calls text, tool_name text,
            reasoning text, timestamp real, active int);
    """)
    now = time.time()
    con.execute(
        "insert into messages (session_id, role, content, tool_calls,"
        " tool_name, timestamp, active) values (?,?,?,?,?,?,1)",
        ("local-aaaa1111XX", "assistant", "", ACCESS_TOOL, "Read",
         now - 3600))
    con.execute(
        "insert into messages (session_id, role, content, tool_calls,"
        " tool_name, timestamp, active) values (?,?,?,?,?,?,1)",
        ("local-bbbb2222XX", "assistant", MENTION_CONTENT, "",
         "", now - 3600))
    con.commit()
    con.close()
    return root


def run_main():
    buf = io.StringIO()
    with redirect_stdout(buf):
        mod.main()
    return buf.getvalue()


def age_state(path, delta=None):
    """Shift all state keys past the REPEAT_AFTER border."""
    delta = mod.REPEAT_AFTER + 60 if delta is None else delta
    st = json.load(open(path))
    for k in st:
        st[k] = time.time() - delta
    json.dump(st, open(path, "w"))


def prepare(state_path, profiles_root):
    """Isolation of main(): its own state, empty boards, a holdout fixture,
    the live parallelism and core-freshness gates are off, the digest is in
    a temp dir (otherwise the test would write routine into a live digest)."""
    bind_digest(os.path.join(os.path.dirname(state_path), "digest"))
    saved = (mod.max_in_progress_gate, mod.code_newer_than_gateway,
             mod.holdout_guard, mod.STATE, mod.BOARDS)
    real_guard = mod.holdout_guard
    mod.STATE = state_path
    mod.BOARDS = tempfile.mkdtemp(prefix="wd-repeat-boards-")
    mod.max_in_progress_gate = lambda state, now: None
    mod.code_newer_than_gateway = lambda: None
    mod.holdout_guard = (lambda now, **kw:
                         real_guard(now, profiles_root=profiles_root))

    def restore():
        (mod.__setattr__("max_in_progress_gate", saved[0]),
         mod.__setattr__("code_newer_than_gateway", saved[1]),
         mod.__setattr__("holdout_guard", saved[2]),
         mod.__setattr__("STATE", saved[3]),
         mod.__setattr__("BOARDS", saved[4]))
    return restore


def scenario_holdout():
    print("scenario 1: holdout_guard, two runs of main() in a row")
    root = make_profiles()
    tmp = tempfile.mkdtemp(prefix="wd-repeat-")
    state_path = os.path.join(tmp, "watchdog-state.json")
    restore = prepare(state_path, root)
    try:
        out1 = run_main()
        body1 = digest_body()
        check("first run: stdout is quiet, the holdout block is in the digest",
              out1 == "" and "[holdout] HIGH" in body1
              and "[holdout] low" in body1,
              repr(out1[:200]) + " | " + repr(body1[:200]))
        check("no push headers (holdout = routine)",
              "KANBAN STOPPED" not in out1
              and "KANBAN NEEDS ATTENTION" not in out1,
              repr(out1[:200]))
        st = json.load(open(state_path))
        hk = sorted(k for k in st if k.startswith("holdout-"))
        check("holdout keys are written to state",
              len(hk) == 2 and all(st[k] > 0 for k in hk), str(hk))
        out2 = run_main()
        body2 = digest_body()
        check("second run in a row: silence, holdout is not repeated",
              out2 == "" and body2 == body1, repr(out2[:200]))
        age_state(state_path)
        out3 = run_main()
        body3 = digest_body()
        check("after REPEAT_AFTER holdout is in the digest again",
              out3 == "" and "[holdout] HIGH" in body3
              and "[holdout] low" in body3 and len(body3) > len(body2),
              repr(out3[:200]) + " | " + repr(body3[:200]))
    finally:
        restore()


def scenario_stale():
    print("scenario 2: the stale block of code_newer_than_gateway, same filter")
    tmp = tempfile.mkdtemp(prefix="wd-repeat-")
    state_path = os.path.join(tmp, "watchdog-state.json")
    restore = prepare(state_path, tempfile.mkdtemp(prefix="wd-repeat-empty-"))
    pair = ("code-newer-than-gateway",
            "  GATE: a core edit was not picked up by the gateway.\n    test")
    mod.code_newer_than_gateway = lambda: pair
    try:
        out1 = run_main()
        body1 = digest_body()
        check("stale block in the digest on the first run, stdout is quiet",
              out1 == "" and "GATE: a core edit was not picked up" in body1,
              repr(out1[:200]) + " | " + repr(body1[:200]))
        out2 = run_main()
        body2 = digest_body()
        check("the stale block is not repeated by the second run",
              out2 == "" and body2 == body1, repr(out2[:200]))
        age_state(state_path)
        out3 = run_main()
        body3 = digest_body()
        check("after REPEAT_AFTER stale is in the digest again",
              out3 == "" and "GATE: a core edit was not picked up" in body3,
              repr(out3[:200]) + " | " + repr(body3[:200]))
    finally:
        restore()


def scenario_crash():
    print("scenario 3: a detector failure gets no key and is not filtered")
    tmp = tempfile.mkdtemp(prefix="wd-repeat-")
    state_path = os.path.join(tmp, "watchdog-state.json")
    restore = prepare(state_path, tempfile.mkdtemp(prefix="wd-repeat-empty-"))

    def boom(now, **kw):
        raise RuntimeError("db locked")
    mod.holdout_guard = boom
    pair = ("code-newer-than-gateway", "  GATE: test")
    mod.code_newer_than_gateway = lambda: pair
    try:
        out1 = run_main()
        body1 = digest_body()
        check("first run: the 'failed' text is in the digest, stdout is quiet",
              out1 == "" and "Leak detector failed" in body1,
              repr(out1[:200]) + " | " + repr(body1[:200]))
        age_state(state_path)
        out2 = run_main()
        body2 = digest_body()
        check("repeat run: 'failed' is in the digest again (not deduped)",
              "Leak detector failed" in body2,
              repr(body2[:200]))
        st = json.load(open(state_path))
        check("there is no key for 'failed' in state (a failure is not deduped)",
              not [k for k in st if "holdout" in k], str(sorted(st)))
    finally:
        restore()


def scenario_sources():
    print("scenario 4: sources before the loop, checked in the source")
    src_gate = inspect.getsource(mod.max_in_progress_gate)
    check("max_in_progress_gate dedups itself with the same REPEAT_AFTER",
          "REPEAT_AFTER" in src_gate and "state.get(key, 0)" in src_gate,
          "no internal dedup")
    src_main = inspect.getsource(mod.main)
    check("main() filters holdout findings by state.get + REPEAT_AFTER",
          "now - state.get(key9, 0) > REPEAT_AFTER" in src_main,
          "the filter is not found in main()")
    check("main() filters stale with the same filter",
          "now - state.get(pair_pre[0], 0) > REPEAT_AFTER" in src_main,
          "the stale block has no filter")


def main():
    print("REPEAT_AFTER for the sources of main() before the board loop")
    scenario_holdout()
    scenario_stale()
    scenario_crash()
    scenario_sources()
    print()
    if FAILED:
        print("FAIL: %d" % len(FAILED))
        sys.exit(1)
    print("OK: all tests are green")


if __name__ == "__main__":
    main()
