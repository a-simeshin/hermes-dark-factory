#!/usr/bin/env python3
"""Tests for cron_failure_alerts in hermes-health.py (red crons).

Reason: a cron once failed silently for 10 runs in a row, another stood in
blocked_config; the blocked_config notification arrives once and failure
streaks do not arrive at all. The watchdog must: raise an alert for
streak >= 2 at any status and for last_status error/blocked_config at any
streak, stay silent on healthy jobs, survive a broken and a missing jobs.json,
carry the profile prefix, the job name and the first line of the error.

The fixture is written to a tempdir inside the test (profiles MAIN, worker,
empty, broken). The jobs root is overridden by env CRON_JOBS_ROOT right in
hermes-health.py (like ORPHAN_PROC_ROOT for orphans), the script is overridden
by HERMES_HEALTH_SCRIPT, like in the other tests of scripts/.
"""
import atexit
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

# The script under test never touches the real Hermes home: a temp one is used.
_HOME = tempfile.mkdtemp(prefix="hh-home-")
atexit.register(shutil.rmtree, _HOME, ignore_errors=True)
os.environ["HERMES_HOME"] = _HOME
os.environ.setdefault("FACTORY_OWNER_CHAT", "telegram:000000000")

REAL = os.environ.get("HERMES_HEALTH_SCRIPT",
                      os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "hermes-health.py"))


def load_health(path=REAL):
    spec = importlib.util.spec_from_file_location("hh_cfa_%d" % int(time.time()), path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def job(name, streak=0, status="ok", err=""):
    return {"name": name, "id": "deadbeef", "failure_streak": streak,
            "last_status": status, "last_error": err,
            "last_run_at": "2026-01-01T20:30:33-04:00"}


# Positive mutants must land in the alert, negative ones stay silent.
POSITIVE = [
    job("mutant-streak3-ok", streak=3, status="ok",
        err="Script exited with code 2\nstdout: docstring"),
    job("mutant-blocked1", streak=1, status="blocked_config",
        err="delivery platform 'telegram' has no gateway credentials"),
    job("mutant-err-streak1", streak=1, status="error",
        err="Traceback (most recent call last)\nValueError: boom"),
    job("boundary-streak2-ok", streak=2, status="ok", err="exit 1"),
]
NEGATIVE = [
    job("healthy-streak1-ok", streak=1, status="ok"),
    job("healthy-zero"),
]


class TestCronFailureAlerts(unittest.TestCase):
    def setUp(self):
        self.mod = load_health()
        self.root = tempfile.mkdtemp(prefix="hh-cron-")
        main = os.path.join(self.root, "cron")
        os.makedirs(main)
        with open(os.path.join(main, "jobs.json"), "w", encoding="utf-8") as fh:
            json.dump({"jobs": POSITIVE + NEGATIVE}, fh)
        worker = os.path.join(self.root, "profiles", "worker", "cron")
        os.makedirs(worker)
        with open(os.path.join(worker, "jobs.json"), "w", encoding="utf-8") as fh:
            json.dump({"jobs": [job("worker-nightly", streak=5, status="error",
                                    err="429 insufficient quota")]}, fh)
        os.makedirs(os.path.join(self.root, "profiles", "empty"))
        broken = os.path.join(self.root, "profiles", "broken", "cron")
        os.makedirs(broken)
        with open(os.path.join(broken, "jobs.json"), "w", encoding="utf-8") as fh:
            fh.write("{not json")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def alerts(self):
        os.environ["CRON_JOBS_ROOT"] = self.root
        try:
            return self.mod.cron_failure_alerts()
        finally:
            del os.environ["CRON_JOBS_ROOT"]

    def test_red_jobs_alerted_healthy_silent(self):
        text = "\n".join(self.alerts())
        for j in POSITIVE:
            self.assertIn("/%s:" % j["name"], text,
                          "red job %s must land in the alert" % j["name"])
        for j in NEGATIVE:
            self.assertNotIn("/%s:" % j["name"], text,
                             "healthy job %s must stay silent" % j["name"])

    def test_alert_carries_name_streak_and_first_error_line(self):
        a0 = next((a for a in self.alerts() if "mutant-streak3-ok" in a), "")
        self.assertTrue(a0, "the alert for mutant-streak3-ok is lost")
        self.assertTrue(a0.startswith("MAIN/mutant-streak3-ok:"), a0)
        self.assertIn("streak=3", a0)
        self.assertIn("Script exited with code 2", a0,
                      "the alert must carry the first error line, not only the fact of failure")
        self.assertNotIn("stdout:", a0, "the second error line is not needed in the alert")

    def test_profiles_scanned_with_prefix_broken_survived(self):
        alerts = self.alerts()
        self.assertTrue(any(a.startswith("worker/worker-nightly:") for a in alerts),
                        "a job of the worker profile must carry the profile prefix")
        # a broken jobs.json and a profile without crons neither crash the
        # watchdog nor raise alerts
        self.assertFalse(any("broken" in a for a in alerts))

    def test_streak_boundary_is_two(self):
        text = "\n".join(self.alerts())
        self.assertIn("/boundary-streak2-ok:", text, "streak=2 is already alert-worthy")
        self.assertNotIn("/healthy-streak1-ok:", text, "streak=1 with ok stays silent")


class TestMutants(unittest.TestCase):
    """A mutant in cron_failure_alerts must turn the tests above red."""

    def test_m1_swallows_all_failures(self):
        # the guard condition fails open: the watchdog is silent
        old = "if streak < 2 and status not in CRON_BAD_STATUSES:"
        new = "if True:"
        src = open(REAL).read()
        self.assertIn(old, src, "the mutant pattern is not found in hermes-health.py")
        d = tempfile.mkdtemp(prefix="hh-mut-")
        mut = os.path.join(d, "hermes-health.py")
        with open(mut, "w", encoding="utf-8") as fh:
            fh.write(src.replace(old, new, 1))
        # the mutant lives outside the pack directory: it needs factory_config
        shutil.copy(os.path.join(os.path.dirname(REAL), "factory_config.py"), d)
        r = subprocess.run(
            [sys.executable, os.path.abspath(__file__),
             "TestCronFailureAlerts.test_red_jobs_alerted_healthy_silent"],
            env=dict(os.environ, HERMES_HEALTH_SCRIPT=mut),
            capture_output=True, text=True, timeout=120)
        shutil.rmtree(d, ignore_errors=True)
        self.assertNotEqual(r.returncode, 0,
                            "the silent mutant must turn the test red: otherwise the "
                            "watchdog could stay silent all day again")


if __name__ == "__main__":
    unittest.main(verbosity=2)
