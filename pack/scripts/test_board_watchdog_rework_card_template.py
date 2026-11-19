#!/usr/bin/env python3
"""The rework card template does not make the worker poll for a verdict.

Why: a worker delivered a rework and then waited for the verdict inside its
own run for hours, polling the PR every few minutes. The source was an
ambiguous sentence in the template of _rework_arm_card ("do not close the
card without a new verdict"), which the worker read as "stay in the run
until the verdict".

Rule: the template names the stock handoff (kanban_request_review and
leaving the run) and says explicitly that the verdict is NOT awaited in the
run; the ambiguous ban must be gone.

Run: python3 test_board_watchdog_rework_card_template.py
Mutant: BOARD_WATCHDOG_SCRIPT=<copy> python3 test_board_watchdog_rework_card_template.py
(a mutant of the template must fail at least one check).
"""
import importlib.util
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
_HOME = tempfile.mkdtemp(prefix="wd-home-")
os.environ.update({
    "HERMES_HOME": _HOME,
    "FACTORY_REPO": "acme/widget",
    "FACTORY_REPO_PATH": "/srv/hermes/widget",
    "FACTORY_BOARD": "main",
    "FACTORY_OWNER_LOGIN": "owner-login",
    "FACTORY_OWNER_CHAT": "telegram:000000000",
    "FACTORY_BASE_BRANCHES": "main",
})
SCRIPT = os.environ.get("BOARD_WATCHDOG_SCRIPT") or os.path.join(
    HERE, "board-watchdog.py")
spec = importlib.util.spec_from_file_location("board_watchdog_rct", SCRIPT)
if spec is None or spec.loader is None:
    print("SKIP: cannot load board-watchdog.py")
    sys.exit(0)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

FAILURES = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILURES.append(name)


captured = {}
orig_create = mod._kanban_create
# Hermetic: _rework_arm_card calls svc_priority and board_repo through
# BOARDS; an empty temp board means no caches, default priority, no bands.
_boards_tmp = tempfile.mkdtemp(prefix="wd-rct-boards-")
os.makedirs(os.path.join(_boards_tmp, "main"), exist_ok=True)
orig_boards = mod.BOARDS
mod.BOARDS = _boards_tmp


def fake_create(slug, title, body, assignee, prio, key, skills, guard=None):
    captured.update(title=title, body=body, assignee=assignee, key=key,
                    prio=prio)
    return "t_test"


mod._kanban_create = fake_create
try:
    mod._rework_arm_card("main", 239, "c64ec482" + "0" * 32,
                         {"at": 1794700000})
finally:
    mod._kanban_create = orig_create
    mod.BOARDS = orig_boards
    shutil.rmtree(_boards_tmp, ignore_errors=True)
    shutil.rmtree(_HOME, ignore_errors=True)

body = captured.get("body", "")
check("card created through _kanban_create", bool(captured))
check("template names kanban_request_review", "kanban_request_review" in body)
check("template requires leaving the run", "leave the run" in body)
check("template explicitly forbids waiting for the verdict in the run",
      "Do NOT wait" in body)
check("verdict arrives as an event", "arrives as an event" in body)
check("done is not marked before the verdict",
      "not mark the card done" in body)
check("ambiguous ban 'do not close the card' is gone",
      "do not close the card" not in body.lower())
check("watchdog signal about a reject is kept", "watchdog finding" in body)

print()
if FAILURES:
    print("%d FAIL: %s" % (len(FAILURES), FAILURES))
    sys.exit(1)
print("8/8 green")
