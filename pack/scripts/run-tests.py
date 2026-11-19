#!/usr/bin/env python3
"""Hermetic runner of the scripts/test_*.py suite.

Goal: the tests never touch the live Hermes home (kanban.db, boards/, cache/,
logs/) and never go to the network. sitecustomize.py next to this file
installs an audit hook that writes violations to the file named by
HERMES_TEST_AUDIT_LOG.

Run: python3 scripts/run-tests.py [--audit]
Without --audit violations are not checked (only green or red); with --audit
a test is also red when it breaks hermeticity.

The last line is the contract with teach-snapshot.sh and with the agents'
reports: "TEST TOTAL scripts/: N/M green". Quote it verbatim, never "green on
a sample".

test_peak_pause.py runs with an isolated HERMES_HOME: it refuses to work on a
live home itself (that is its contract).
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import scripts_digest  # noqa: E402
from factory_config import hermes_home  # noqa: E402

# TESTS_DIR is only for the runner's own tests (a hermetic suite of trivial
# tests); in production the suite always lives in scripts/ itself.
TESTS_DIR = os.environ.get("TESTS_DIR") or HERE
TIMEOUT = int(os.environ.get("RUN_TESTS_TIMEOUT") or 600)
TOTAL_PREFIX = "TEST TOTAL scripts/:"


def green_stamp_path() -> str:
    return os.environ.get("SCRIPTS_GREEN_STAMP") or os.path.join(
        hermes_home(), "cron", "scripts-tests.green.json")


def write_green_stamp() -> None:
    """Stamp of a green run for the drift watchdog in hermes-health.py.

    Written only after a fully green run with --audit and only when the
    production suite ran (TESTS_DIR not overridden): a trivial sandbox suite
    proves nothing about the live scripts/. The stamp supports the watchdog,
    it is not a gate of the run: a write error does not fail the tests.
    """
    if os.path.abspath(TESTS_DIR) != os.path.abspath(HERE):
        return
    try:
        os.makedirs(os.path.dirname(green_stamp_path()), exist_ok=True)
        with open(green_stamp_path(), "w", encoding="utf-8") as fh:
            json.dump({"digest": scripts_digest.scripts_digest(HERE),
                       "ts": time.time()}, fh)
    except OSError:
        pass


def run_one(path, audit_log):
    env = dict(os.environ)
    env["PYTHONPATH"] = HERE + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("PYTEST_CURRENT_TEST", None)
    name = os.path.basename(path)
    if audit_log:
        env["HERMES_TEST_AUDIT_LOG"] = audit_log
    if name == "test_peak_pause.py":
        # peak_pause contract: a sandbox HERMES_HOME, a live home is refused.
        env["HERMES_HOME"] = tempfile.mkdtemp(prefix="peak-pause-sbx-")
    t0 = time.time()
    # Each test runs in its own process group. subprocess.run(timeout=)
    # kills ONLY the direct child: orphaned grandchildren (a test that
    # re-spawned itself in an endless chain once grew to 8.8 GB and got the
    # gateway OOM-killed) survived the runner's timeout and kept multiplying.
    p = subprocess.Popen(
        [sys.executable, path], cwd=os.path.dirname(path), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        start_new_session=True)
    try:
        out, _ = p.communicate(timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        p.communicate()
        return 1, "TIMEOUT %ds" % TIMEOUT, float(TIMEOUT)
    return p.returncode, out.decode("utf-8", "replace"), time.time() - t0


def violations_for(audit_log, name):
    """Audit log lines that belong to test ``name``."""
    out = []
    try:
        with open(audit_log, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return out
    for ln in lines:
        try:
            test, kind, detail = ln.split("\t", 2)
        except ValueError:
            continue
        if test == name:
            out.append("%s: %s" % (kind, detail))
    return out


def main():
    unknown = [a for a in sys.argv[1:] if a != "--audit"]
    if unknown:
        # a typo or --help must not start the whole suite
        print("usage: run-tests.py [--audit]  (unknown: %s)" % " ".join(unknown))
        return 2
    audit = "--audit" in sys.argv
    tests = sorted(
        f for f in os.listdir(TESTS_DIR)
        if f.startswith("test_") and f.endswith(".py"))
    audit_log = None
    if audit:
        fd, audit_log = tempfile.mkstemp(prefix="hermes-audit-")
        os.close(fd)
        os.unlink(audit_log)
    passed, failed = [], []
    for name in tests:
        rc, out, dur = run_one(os.path.join(TESTS_DIR, name), audit_log)
        vio = violations_for(audit_log, name) if audit_log else []
        if rc == 0 and not vio:
            passed.append(name)
            print("PASS %-46s %5.1fs" % (name, dur))
        else:
            failed.append(name)
            print("FAIL %-46s rc=%d %5.1fs" % (name, rc, dur))
            for v in vio[:10]:
                print("     NOT HERMETIC %s" % v)
            for line in [l for l in out.splitlines() if l.strip()][-12:]:
                print("     | %s" % line)
    print()
    print("%s %d/%d green" % (TOTAL_PREFIX, len(passed), len(tests)))
    if audit_log and os.path.exists(audit_log):
        os.unlink(audit_log)
    if failed:
        print("RED: %s" % ", ".join(failed))
        return 1
    if audit:
        write_green_stamp()
    return 0


if __name__ == "__main__":
    sys.exit(main())
