#!/usr/bin/env python3
"""Tests for the evidence window of check_quota_death in hermes-health.py.

Reason: after the quota came back (probe 200, stop released, incident
removed), two later ticks reopened the incident on a 429 1310 line written
BEFORE the recovery: cutoff = now-45min does not know the quota already
answered, so the pre-recovery line counted as evidence for 45 minutes. Each
tick without an incident set a new false stop and sent the owner a signal.

Contract: quota_recover writes the marker cache/quota-recovered.json (the
moment of recovery), the window floor = max(now-45min, recovered_at). A line
older than the recovery does not open an incident; a fresher line does; without
a marker the window is the usual 45 minutes; a broken marker does not blind the
detector.

The root of the quota state is overridden by env QUOTA_STATE_ROOT right in
hermes-health.py (like CRON_JOBS_ROOT for crons), the script is overridden by
HERMES_HEALTH_SCRIPT, like in the other tests of scripts/.
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
    spec = importlib.util.spec_from_file_location("hh_qr_%d" % int(time.time()), path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def quota_line(age_s):
    """An errors.log line as seen in production: 429 with code 1310."""
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - age_s))
    return ("%s ERROR agent.chat_completion_helpers: Streaming failed before "
            "delivery: Error code: 429 - {'error': {'code': '1310', "
            "'message': 'Limit Exhausted'}}\n" % ts)


class QuotaHarness(unittest.TestCase):
    def setUp(self):
        self.mod = load_health()
        self.root = tempfile.mkdtemp(prefix="hh-quota-")
        os.makedirs(os.path.join(self.root, "logs"))
        os.makedirs(os.path.join(self.root, "cache"))
        self.stops = []
        self.mod.quota_probe = lambda: False  # do not touch the network
        self.mod.quota_stop = self._stop  # do not touch holders/crons/send
        self.mod.run = lambda cmd, timeout=60, **kw: (0, "")
        self.mod.hold_owner_stop = lambda: None

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _stop(self, reset, code):
        self.stops.append((reset, code))
        return {"code": code, "reset": reset, "paused": [], "ts": int(time.time())}

    def check(self):
        os.environ["QUOTA_STATE_ROOT"] = self.root
        try:
            self.mod.check_quota_death()
        finally:
            del os.environ["QUOTA_STATE_ROOT"]

    def write_log(self, name, ages):
        with open(os.path.join(self.root, "logs", name), "w", encoding="utf-8") as fh:
            for age in ages:
                fh.write(quota_line(age))

    def write_marker(self, ts):
        with open(os.path.join(self.root, "cache", "quota-recovered.json"),
                  "w", encoding="utf-8") as fh:
            json.dump({"ts": ts, "code": "1310"}, fh)


class TestQuotaRecoveredWindow(QuotaHarness):
    def test_line_before_recovered_marker_is_not_evidence(self):
        # the defect itself: the line is younger than 45 minutes but OLDER than the recovery
        self.write_marker(int(time.time()) - 20 * 60)
        self.write_log("errors.log", [30 * 60])
        self.check()
        self.assertEqual(self.stops, [],
                         "a line older than the quota recovery must "
                         "stay silent: it is the one that reopened the stop twice")
        self.assertFalse(os.path.exists(
            os.path.join(self.root, "cache", "quota-death.json")))

    def test_line_after_recovered_marker_opens_incident(self):
        # a fresh death after the recovery: the marker must not hide it
        self.write_marker(int(time.time()) - 30 * 60)
        self.write_log("errors.log", [10 * 60])
        self.check()
        self.assertEqual(len(self.stops), 1,
                         "a line fresher than the recovery must open an incident")
        self.assertEqual(self.stops[0][1], "1310")

    def test_without_marker_window_is_still_45_minutes(self):
        # a pre-recovery line without a marker (legacy state) is visible as before
        self.write_log("errors.log", [30 * 60])
        self.check()
        self.assertEqual(len(self.stops), 1,
                         "without a marker the 45-minute window does not narrow")

    def test_line_outside_45min_window_stays_silent(self):
        self.write_log("errors.log", [50 * 60])
        self.check()
        self.assertEqual(self.stops, [], "a line older than the window is silent even without a marker")

    def test_broken_marker_does_not_blind_detector(self):
        # a broken marker (no ts) must not raise the cutoff into the future
        with open(os.path.join(self.root, "cache", "quota-recovered.json"),
                  "w", encoding="utf-8") as fh:
            json.dump({"code": "1310"}, fh)
        self.write_log("errors.log", [30 * 60])
        self.check()
        self.assertEqual(len(self.stops), 1,
                         "a broken marker must be read as no marker")

    def test_recover_writes_marker_and_removes_incident(self):
        # probe 200 with an open incident: recover must write the moment of
        # recovery, otherwise the next tick reopens the stop on the old line
        inc = {"code": "1310", "reset": "r", "paused": [], "ts": int(time.time())}
        with open(os.path.join(self.root, "cache", "quota-death.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(inc, fh)
        self.mod.quota_probe = lambda: True
        self.check()
        marker = os.path.join(self.root, "cache", "quota-recovered.json")
        self.assertTrue(os.path.exists(marker),
                        "quota_recover must write quota-recovered.json")
        with open(marker, encoding="utf-8") as fh:
            m = json.load(fh)
        self.assertAlmostEqual(m["ts"], time.time(), delta=10,
                               msg="the marker must carry the moment of recovery")
        self.assertEqual(m["code"], "1310")
        self.assertFalse(os.path.exists(
            os.path.join(self.root, "cache", "quota-death.json")),
            "the incident is removed after recover")


class TestMutants(unittest.TestCase):
    """Mutants in the evidence window and the marker must turn the tests above red."""

    def run_case(self, old, new, test):
        src = open(REAL).read()
        self.assertIn(old, src, "the mutant pattern is not found in hermes-health.py")
        d = tempfile.mkdtemp(prefix="hh-qmut-")
        mut = os.path.join(d, "hermes-health.py")
        with open(mut, "w", encoding="utf-8") as fh:
            fh.write(src.replace(old, new, 1))
        # the mutant lives outside the pack directory: it needs factory_config
        shutil.copy(os.path.join(os.path.dirname(REAL), "factory_config.py"), d)
        try:
            r = subprocess.run(
                [sys.executable, os.path.abspath(__file__),
                 "TestQuotaRecoveredWindow." + test],
                env=dict(os.environ, HERMES_HEALTH_SCRIPT=mut),
                capture_output=True, text=True, timeout=120)
        finally:
            shutil.rmtree(d, ignore_errors=True)
        self.assertNotEqual(
            r.returncode, 0,
            "the mutant passed the tests unnoticed: %s turns %s red" % (new, test))

    def test_m1_cutoff_ignores_recovered_marker(self):
        # rollback: cutoff is again only now-45min, the marker is not read
        self.run_case(
            "    recovered = quota_recovered_at()\n"
            "    if recovered:\n"
            "        cutoff = max(cutoff, recovered)",
            "    recovered = None",
            "test_line_before_recovered_marker_is_not_evidence")

    def test_m2_recover_forgets_marker(self):
        # recover does not write the moment of recovery: no marker, the fix is dead
        self.run_case(
            'json.dump({"ts": int(time.time()), "code": inc.get("code", "?")},',
            'json.dump({"code": inc.get("code", "?")},',
            "test_recover_writes_marker_and_removes_incident")


if __name__ == "__main__":
    unittest.main(verbosity=2)
