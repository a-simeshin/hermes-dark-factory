#!/usr/bin/env python3
"""Tests for backup-verify.py: the verdict on `hermes backup` output.

The error format is taken from real runs: lines with [Errno 2] No such file
or directory on the cron/output and cron/external-workers trees, removed by
hourly cleanups during a multi-minute archive (two lines on a manual run,
over a thousand on a nightly one).

Mutants (must fail):
- M1 "any error will do": the foreign-error branch (elif m or Traceback)
  replaced with elif False -> test_verdict_other_errno_fails and
  test_verdict_traceback_fails fail;
- M2 "rc!=0 without ENOENT is normal": ok = n_enoent > 0 and not offenders
  replaced with ok = not offenders -> test_verdict_unknown_fail fails.
"""
import importlib.util
import os
import sys

SCRIPT = os.environ.get("BACKUP_VERIFY_SCRIPT") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "backup-verify.py")

spec = importlib.util.spec_from_file_location("backup_verify", SCRIPT)
if spec is None or spec.loader is None:
    print("SKIP: cannot load backup-verify.py")
    sys.exit(0)
bv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bv)


ENOENT_LINES = (
    "[Errno 2] No such file or directory: "
    "'/srv/hermes/.hermes/cron/output/cef598bf9cfa/2026-01-01_17-00-46.md'\n"
    "[Errno 2] No such file or directory: "
    "'/srv/hermes/.hermes/cron/external-workers/1234/output.log'\n"
)


def test_verdict_clean():
    ok, n, off = bv.verdict(0, "backup: 2891 files\ndone: /tmp/x.zip\n")
    assert ok and n == 0 and off == [], (ok, n, off)
    print("OK test_verdict_clean")


def test_verdict_enoent_tolerated():
    # real case: rc=1, both errors are ENOENT -> the backup is good
    ok, n, off = bv.verdict(1, "archiving...\n" + ENOENT_LINES)
    assert ok and n == 2 and off == [], (ok, n, off)
    print("OK test_verdict_enoent_tolerated")


def test_verdict_enoent_zero_rc():
    # warnings about vanished files with rc=0 do not spoil the verdict
    ok, n, off = bv.verdict(0, ENOENT_LINES + "done\n")
    assert ok and n == 2 and off == [], (ok, n, off)
    print("OK test_verdict_enoent_zero_rc")


def test_verdict_other_errno_fails():
    text = ENOENT_LINES + (
        "[Errno 13] Permission denied: '/srv/hermes/.hermes/usage-ledger/ledger.jsonl'\n")
    ok, n, off = bv.verdict(1, text)
    assert not ok and n == 2 and off, (ok, n, off)
    assert "Errno 13" in off[0], off
    print("OK test_verdict_other_errno_fails")


def test_verdict_traceback_fails():
    text = ENOENT_LINES + (
        "Traceback (most recent call last):\n"
        "  File \"backup.py\", line 9, in <module>\n"
        "OSError: [Errno 28] No space left on device\n")
    ok, n, off = bv.verdict(1, text)
    assert not ok and off, (ok, n, off)
    assert any("Traceback" in l for l in off), off
    assert any("Errno 28" in l for l in off), off
    print("OK test_verdict_traceback_fails")


def test_verdict_unknown_fail():
    # rc!=0 without a single ENOENT: an unknown failure, not "normal"
    ok, n, off = bv.verdict(1, "Error: EACCES: permission denied, open 'x'\n")
    assert not ok, (ok, n, off)
    # and a completely empty log is not good either
    ok2, _, _ = bv.verdict(1, "")
    assert not ok2, ok2
    print("OK test_verdict_unknown_fail")


def test_verdict_offenders_capped():
    # fifteen foreign errors: only the first five end up in offenders
    text = "".join("[Errno 13] Permission denied: 'f%d'\n" % i
                   for i in range(15))
    ok, n, off = bv.verdict(1, text)
    assert not ok and n == 0 and len(off) == 5, (ok, n, len(off))
    print("OK test_verdict_offenders_capped")


def test_main_exit_codes(tmpdir_prefix="/tmp/bv-test-"):
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        good = os.path.join(td, "good.log")
        with open(good, "w", encoding="utf-8") as fh:
            fh.write(ENOENT_LINES)
        rc = os.system("%s %s %s %s > /dev/null 2>&1"
                       % (sys.executable, SCRIPT, 1, good))
        assert os.waitstatus_to_exitcode(rc) == 0, rc
        bad = os.path.join(td, "bad.log")
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write("[Errno 13] Permission denied: 'x'\n")
        rc = os.system("%s %s %s %s > /dev/null 2>&1"
                       % (sys.executable, SCRIPT, 1, bad))
        assert os.waitstatus_to_exitcode(rc) == 1, rc
    print("OK test_main_exit_codes")


def main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {t.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} green")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
