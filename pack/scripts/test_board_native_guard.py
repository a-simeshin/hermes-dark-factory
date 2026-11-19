#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The shared helper "is the native mechanism or a live card already doing it?".

Checks:
  A. Unit semantics of native_or_live: a live key (exact and by the -rN/-pN
     family), native coverage, refusal on an empty native_checked and an
     empty why, refusal on an unknown native path, the GuardNote receipt.
  B. The runtime gate _kanban_create in board-watchdog: creating a card
     without a GuardNote receipt is refused (a plain string cannot bypass
     the rule).
  C. Static check of generators: board-watchdog.py, pr-hygiene.py and
     plan-review-gate.py have no function that creates a card or reassigns
     without calling native_or_live (or taking a guard= receipt). A new
     generator without the helper fails this test.
  D. Negative control of C: a synthetic "new generator" without the helper
     must be caught by the scanner; one with the call and one delegating
     guard= must not (no false positives).

Hermetic: reads only the sources next to it; no Hermes home is touched.
"""
import importlib.util
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="bng-home-")
os.environ.update({
    "FACTORY_REPO": "acme/widget",
    "FACTORY_REPO_PATH": "/srv/hermes/widget",
    "FACTORY_BOARD": "main",
    "FACTORY_OWNER_LOGIN": "owner-login",
    "FACTORY_OWNER_CHAT": "telegram:000000000",
    "FACTORY_BASE_BRANCHES": "main",
})

# Registered under the canonical name: board-watchdog does
# sys.modules.setdefault("board_native_guard", ...) and must get THE SAME
# copy of the module (otherwise the GuardNote class differs from
# bng.GuardNote).
_spec = importlib.util.spec_from_file_location(
    "board_native_guard", os.path.join(HERE, "board_native_guard.py"))
bng = importlib.util.module_from_spec(_spec)
sys.modules["board_native_guard"] = bng
_spec.loader.exec_module(bng)

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print("PASS %s" % name)
    else:
        print("FAIL %s %s" % (name, detail))
        FAILS.append(name)


# --- A. native_or_live semantics -------------------------------------------

covered, note = bng.native_or_live(
    "reviewer-pr379-1258c26",
    live_keys=["reviewer-pr379-1258c26-r2"],
    native_checked=[bng.NATIVE_REVIEW_LANE, bng.NATIVE_VERDICT],
    why="duplicate review incident")
check("A1 a live key of the -rN family blocks creation",
      covered and "live card" in note, note)

covered, note = bng.native_or_live(
    "reviewer-pr379-1258c26-p3",
    live_keys=["reviewer-pr379-1258c26"],
    native_checked=[bng.NATIVE_VERDICT], why="paired arm")
check("A2 a live key of the -pN family blocks creation", covered, note)

covered, note = bng.native_or_live(
    "reviewer-pr379-1258c26", live_keys=["reviewer-pr380-1258c26"],
    native_checked=[bng.NATIVE_REVIEW_LANE], why="another PR is no obstacle")
check("A3 another key does not block", not covered)

covered, note = bng.native_or_live(
    "reviewer-pr379-1258c26", native=[bng.NATIVE_REVIEW_LANE],
    native_checked=[bng.NATIVE_REVIEW_LANE], why="native review lane alive")
check("A4 the native review lane blocks creation",
      covered and "review lane" in note, note)

covered, note = bng.native_or_live(
    "coder-pr325-rework-abc1234",
    native_checked=[bng.NATIVE_VERDICT, bng.NATIVE_REVIEW_LANE],
    why="changes requested without a rework card")
check("A5 a clean key: creation allowed, GuardNote receipt",
      (not covered) and isinstance(note, bng.GuardNote)
      and "verdict on this head" in note and "changes requested" in note,
      note)

try:
    bng.native_or_live("k-x", native_checked=[], why="no check")
    check("A6 empty native_checked is refused", False)
except ValueError:
    check("A6 empty native_checked is refused", True)

try:
    bng.native_or_live("k-x", native_checked=[bng.NATIVE_VERDICT], why="  ")
    check("A7 empty why is refused", False)
except ValueError:
    check("A7 empty why is refused", True)

try:
    bng.native_or_live("k-x", native_checked=["telepathy"], why="w")
    check("A8 unknown native path is refused", False)
except ValueError:
    check("A8 unknown native path is refused", True)

try:
    bng.native_or_live("", native_checked=[bng.NATIVE_VERDICT], why="w")
    check("A9 empty key is refused", False)
except ValueError:
    check("A9 empty key is refused", True)

check("A10 a receipt cannot be forged with a string",
      not isinstance("native checked", bng.GuardNote))

check("A11 rework_key names PR and head7",
      bng.rework_key(405, "ABCDEF0123") == "coder-pr405-rework-abcdef0")
try:
    bng.rework_key(405, "abc")
    check("A12 rework_key refuses a short head", False)
except ValueError:
    check("A12 rework_key refuses a short head", True)

# --- B. runtime gate _kanban_create ----------------------------------------

_bw_spec = importlib.util.spec_from_file_location(
    "board_watchdog_guard_test", os.path.join(HERE, "board-watchdog.py"))
try:
    bw = importlib.util.module_from_spec(_bw_spec)
    sys.modules["board_watchdog_guard_test"] = bw
    _bw_spec.loader.exec_module(bw)
    LOADED_BW = True
except BaseException as exc:  # SystemExit from a missing setting included
    LOADED_BW = False
    print("FAIL board-watchdog import: %r" % (exc,))
    FAILS.append("B0 board-watchdog import")

if LOADED_BW:
    raised = None
    try:
        bw._kanban_create("b", "t", "body", "coder", 40, "k-x",
                          guard="forged string")
    except ValueError as exc:
        raised = exc
    check("B1 _kanban_create requires GuardNote, a string does not pass",
          raised is not None, str(raised))

    raised = None
    try:
        bw._kanban_create("b", "t", "body", "coder", 40, "k-x")
    except ValueError as exc:
        raised = exc
    check("B2 _kanban_create without a receipt is refused",
          raised is not None, str(raised))

    ok, note = bng.native_or_live(
        "k-test", native_checked=[bng.NATIVE_VERDICT], why="test")
    orig_run = bw.run
    bodies = []

    def fake_run(cmd, timeout=120):
        bodies.append(cmd)
        content = ""
        if "--body-file" in cmd:
            try:
                with open(cmd[cmd.index("--body-file") + 1],
                          encoding="utf-8") as fh:
                    content = fh.read()
            except OSError:
                content = ""
        bodies.append(content)
        return '{"id": "t_00000001"}'

    bw.run = fake_run
    raised = None
    try:
        bw._kanban_create("b", "t", "BODY", "coder", 40, "k-test",
                          guard=note)
    except Exception as exc:
        raised = exc
    finally:
        bw.run = orig_run
    check("B3 a real receipt passes, the guard section is in the body",
          raised is None and bodies
          and "create" in bodies[0]
          and bodies[0][bodies[0].index("--idempotency-key") + 1]
          == "k-test"
          and "## Native guard" in bodies[1]
          and "BODY" in bodies[1],
          "%r" % ([raised, bodies[:1], bodies[1:2]], ))

# --- C. static check of generators -----------------------------------------

GEN_FILES = ["board-watchdog.py", "pr-hygiene.py", "plan-review-gate.py"]
violations = bng.scan_files([os.path.join(HERE, f) for f in GEN_FILES])
check("C1 no generator without the helper in the scripts",
      not violations, "; ".join(str(v) for v in violations))

# --- D. negative control: a new generator must be caught -------------------

NEW_GEN_BAD = '''
def new_generator_violating(slug, key):
    title = "new generator"
    run([HERMES, "kanban", "--board", slug, "create", title,
         "--idempotency-key", key], timeout=90)
'''
v = bng.scan_generator_violations(NEW_GEN_BAD)
check("D1 a new generator without the helper is caught",
      any(f == "new_generator_violating" for f, _ in v), str(v))

NEW_GEN_OK = '''
def new_generator_guarded(slug, key):
    covered, guard = board_native_guard.native_or_live(
        key, live_keys=_live_keys(slug),
        native_checked=[board_native_guard.NATIVE_VERDICT],
        why="nothing native")
    if covered:
        return None
    run([HERMES, "kanban", "--board", slug, "create", title,
         "--idempotency-key", key], timeout=90)
'''
v = bng.scan_generator_violations(NEW_GEN_OK)
check("D2 a generator with the helper is not caught", not v, str(v))

NEW_GEN_DELEGATE = '''
def wrapper_card(slug, title, body, key, guard=None):
    return _kanban_create(slug, title, body, "coder", 40, key,
                          guard=guard)
'''
v = bng.scan_generator_violations(NEW_GEN_DELEGATE)
check("D3 delegating the guard= receipt is not caught", not v, str(v))

NEW_REASSIGN_BAD = '''
def new_reassigner(slug, tid):
    run([HERMES, "kanban", "--board", slug, "reassign", tid, "coder"],
        timeout=90)
'''
v = bng.scan_generator_violations(NEW_REASSIGN_BAD)
check("D4 a new reassign without the helper is caught",
      any(f == "new_reassigner" for f, _ in v), str(v))

# --- E. CLI ----------------------------------------------------------------

import contextlib  # noqa: E402
import io  # noqa: E402

out = io.StringIO()
with contextlib.redirect_stdout(out):
    rc = bng.main(["--key", "issue-7", "--native-checked", "task_links",
                   "--why", "no card for the issue"])
check("E1 CLI: clean key gives rc 0 and a receipt",
      rc == 0 and out.getvalue().startswith("RECEIPT:"), out.getvalue())
out = io.StringIO()
with contextlib.redirect_stdout(out):
    rc = bng.main(["--key", "issue-7", "--live-keys", "issue-7"])
check("E2 CLI: live key gives rc 3 and COVERED",
      rc == 3 and out.getvalue().startswith("COVERED:"), out.getvalue())
out = io.StringIO()
with contextlib.redirect_stdout(out):
    rc = bng.main(["--key", "issue-7"])
check("E3 CLI: nothing checked gives rc 2", rc == 2, out.getvalue())

# --- F. the rule is quoted in the module in full ----------------------------

for phrase in ("review lane after request_review",
               "active_pr, recent_success",
               "task_links and dependency_wait",
               "a verdict on this head",
               "issue or PR + role + head",
               "restoring things by hand past the gates"):
    check("F1 RULE_50B contains: %s" % phrase[:30],
          phrase in bng.RULE_50B)

print()
if FAILS:
    print("RESULT: %d red: %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("RESULT: all checks green")
