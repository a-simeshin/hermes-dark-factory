#!/usr/bin/env python3
"""A PASS verdict without a promises table in Calibration does not count.

A reviewer loaded the adversarial-review skill but built a single mutant per
fix line, and a prose Calibration block ("- contract: every PR-body promise
mapped ...") closed a PASS with two PR-body promises that had no mutants.
Gate: a PASS verdict published since PROMISES_TABLE_REQUIRED_SINCE must carry
a "promise / mutant / test / result" table, otherwise it is not counted.
A return (CHANGES REQUESTED) without the table is fine (the safe direction).
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

HEAD = "a06ce4490f11c22e33d4455a6677889900aabbccd"

PASS_HEADER = "## Review verdict: PASS on exact head `%s`" % HEAD
CAL_NO_TABLE = (
    "## Calibration\n"
    "- contract: every PR-body promise mapped (History entry: IT; "
    "re-render: jest)\n"
    "- mutation: kill-check repeated on exact head, 3/3 RED\n"
    "- pr-texts: no promise found without evidence")
CAL_WITH_TABLE = (
    "## Calibration\n"
    "- mutation: kill-check on the fix line RED 3/3\n"
    "\n"
    "| Promise | Mutant | Test | Result |\n"
    "|---|---|---|---|\n"
    "| read re-issued on Refresh | early return on cache hit | "
    "App.resourceRefresh #1 | killed RED |\n"
    "| error recorded in History | no error payload to cache | "
    "App.resourceRefresh #3 | killed RED |")


def comment(body, at):
    return {"body": body, "createdAt": at}


def test_prose_calibration_is_not_a_table():
    assert not mod._calibration_has_promises_table(CAL_NO_TABLE)
    assert not mod._calibration_has_promises_table(
        PASS_HEADER + "\n\n" + CAL_NO_TABLE)
    print("OK test_prose_calibration_is_not_a_table")


def test_table_detected_header_variants():
    assert mod._calibration_has_promises_table(CAL_WITH_TABLE)
    # the port recognises only the English header (the original also took a
    # second language): a bold header must still count
    bold = ("## Calibration\n"
            "| **Promise** | **Mutant** | Test | Result |\n"
            "|---|---|---|---|\n"
            "| cache hit renders | remove setResourceContent | jest #2 | RED |")
    assert mod._calibration_has_promises_table(bold)
    # a table exists but the word mutant is not in the header: not counted
    weird = ("## Calibration\n"
             "| Promise | Change | Test | Result |\n"
             "|---|---|---|---|\n"
             "| x | y | z | RED |")
    assert not mod._calibration_has_promises_table(weird)
    print("OK test_table_detected_header_variants")


def test_pass_without_table_not_counted():
    data = {"reviews": [], "comments": [
        comment(PASS_HEADER + "\n\n" + CAL_NO_TABLE,
                "2026-11-14T02:28:00Z")]}
    assert mod._latest_verdict(data, HEAD) is None
    print("OK test_pass_without_table_not_counted")


def test_pass_with_table_counted():
    data = {"reviews": [], "comments": [
        comment(PASS_HEADER + "\n\n" + CAL_WITH_TABLE,
                "2026-11-14T02:28:00Z")]}
    v = mod._latest_verdict(data, HEAD)
    assert v and v["kind"] == "accept", v
    print("OK test_pass_with_table_counted")


def test_reject_without_table_still_counted():
    body = ("## Review verdict: CHANGES REQUESTED on exact head `%s`\n"
            "blocker: a promise without a pinning test" % HEAD)
    data = {"reviews": [], "comments": [comment(body,
                                                 "2026-11-13T21:28:00Z")]}
    # without a Calibration block the verdict is invalid by the older rule;
    # with a prose Calibration and no table a reject is valid
    data2 = {"reviews": [], "comments": [
        comment(body + "\n\n" + CAL_NO_TABLE, "2026-11-13T21:28:00Z")]}
    v = mod._latest_verdict(data2, HEAD)
    assert v and v["kind"] == "reject", v
    assert mod._latest_verdict(data, HEAD) is None
    print("OK test_reject_without_table_still_counted")


def test_pass_before_cutoff_grandfathered():
    data = {"reviews": [], "comments": [
        comment(PASS_HEADER + "\n\n" + CAL_NO_TABLE,
                "2026-11-13T00:00:00Z")]}
    v = mod._latest_verdict(data, HEAD)
    assert v and v["kind"] == "accept", v
    print("OK test_pass_before_cutoff_grandfathered")


if __name__ == "__main__":
    test_prose_calibration_is_not_a_table()
    test_table_detected_header_variants()
    test_pass_without_table_not_counted()
    test_pass_with_table_counted()
    test_reject_without_table_still_counted()
    test_pass_before_cutoff_grandfathered()
    print("ALL PROMISES TABLE TESTS OK")
