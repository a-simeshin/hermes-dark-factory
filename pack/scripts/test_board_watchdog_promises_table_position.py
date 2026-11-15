#!/usr/bin/env python3
"""The position of the promises table in a verdict comment does not matter.

The gate of test_board_watchdog_promises_table.py first required the table
strictly INSIDE the Calibration block. A real PASS carried the table in a
section ABOVE Calibration, the watchdog did not count it, and the reviewer
re-ran the whole mutant battery for nothing. The table now counts anywhere
in the verdict comment (a header row with promise and mutant in one line).
A PASS with no table at all is still not counted.
"""
import atexit
import importlib.util
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
_HOME = tempfile.mkdtemp(prefix="wdtest-home-")
atexit.register(shutil.rmtree, _HOME, ignore_errors=True)
os.environ["HERMES_HOME"] = _HOME
os.environ.update({
    "FACTORY_REPO": "acme/widget",
    "FACTORY_REPO_PATH": "/srv/hermes/widget",
    "FACTORY_BOARD": "main",
    "FACTORY_OWNER_LOGIN": "owner-login",
    "FACTORY_OWNER_CHAT": "telegram:000000000",
})
SCRIPT = os.environ.get("BOARD_WATCHDOG_SCRIPT") or os.path.join(
    HERE, "board-watchdog.py")

spec = importlib.util.spec_from_file_location("board_watchdog", SCRIPT)
if spec is None or spec.loader is None:
    print("SKIP: cannot load board-watchdog.py")
    sys.exit(0)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

HEAD = "ba7b37b5c3d4e5f60718293a4b5c6d7e8f9012ab"

PASS_HEADER = "## Review verdict: PASS on exact head `%s`" % HEAD
CAL_NO_TABLE = (
    "## Calibration\n"
    "- contract: every PR-body promise mapped (History entry: IT; "
    "re-render: jest)\n"
    "- mutation: kill-check repeated on exact head, 3/3 RED\n"
    "- pr-texts: no promise found without evidence")
# Layout of the real verdict: the table is a section ABOVE Calibration.
TABLE_ABOVE_CAL = (
    "| Promise | Mutant | Test | Result |\n"
    "|---|---|---|---|\n"
    "| sync flag computed before queue flush | skip flush entirely | "
    "FlowIT #2 | killed RED |\n"
    "| stale entry evicted | evict skipped | FlowIT #3 | killed RED |\n"
    "\n"
    "## Calibration\n"
    "- mutation: kill-check on the fix line RED 3/3")
# Same layout under its own heading and a bold header row.
TABLE_ABOVE_CAL_HEADED = (
    "### Promise check\n"
    "| **Promise** | **Mutant** | Test | Result |\n"
    "|---|---|---|---|\n"
    "| flag is computed before the queue flush | do not flush the queue | "
    "FlowIT #2 | RED |\n"
    "\n"
    "## Calibration\n"
    "- mutation: kill-check RED 3/3")


def comment(body, at):
    return {"body": body, "createdAt": at}


def test_table_above_calibration_counted():
    assert mod._calibration_has_promises_table(TABLE_ABOVE_CAL), "plain failed"
    assert mod._calibration_has_promises_table(TABLE_ABOVE_CAL_HEADED), \
        "headed failed"
    print("OK test_table_above_calibration_counted")


def test_pass_with_table_above_calibration_counted():
    data = {"reviews": [], "comments": [
        comment(PASS_HEADER + "\n\n" + TABLE_ABOVE_CAL,
                "2026-11-14T23:31:00Z")]}
    v = mod._latest_verdict(data, HEAD)
    assert v and v["kind"] == "accept", v
    print("OK test_pass_with_table_above_calibration_counted")


def test_pass_without_table_still_not_counted():
    data = {"reviews": [], "comments": [
        comment(PASS_HEADER + "\n\n" + CAL_NO_TABLE,
                "2026-11-14T23:31:00Z")]}
    assert mod._latest_verdict(data, HEAD) is None
    print("OK test_pass_without_table_still_not_counted")


def test_prose_and_header_without_mutant_still_not_counted():
    # prose about promises is not a table
    assert not mod._calibration_has_promises_table(CAL_NO_TABLE)
    # a header without a mutant column is not counted
    weird = ("| Promise | Change | Test | Result |\n"
             "|---|---|---|---|\n"
             "| x | y | z | RED |")
    assert not mod._calibration_has_promises_table(weird)
    # mutant mentioned in prose below, not in the header row: not counted
    prose = ("| Promise | Change | Test | Result |\n"
             "|---|---|---|---|\n"
             "| x | y | z | RED |\n"
             "\nthe mutant line was checked in a separate run")
    assert not mod._calibration_has_promises_table(prose)
    print("OK test_prose_and_header_without_mutant_still_not_counted")


def test_reject_without_table_counted_anywhere():
    body = ("## Review verdict: CHANGES REQUESTED on exact head `%s`\n"
            "blocker: a promise without a pinning test\n\n" % HEAD
            + CAL_NO_TABLE)
    data = {"reviews": [], "comments": [comment(body,
                                                "2026-11-14T21:28:00Z")]}
    v = mod._latest_verdict(data, HEAD)
    assert v and v["kind"] == "reject", v
    print("OK test_reject_without_table_counted_anywhere")


if __name__ == "__main__":
    test_table_above_calibration_counted()
    test_pass_with_table_above_calibration_counted()
    test_pass_without_table_still_not_counted()
    test_prose_and_header_without_mutant_still_not_counted()
    test_reject_without_table_counted_anywhere()
    print("ALL PROMISES TABLE POSITION TESTS OK")
