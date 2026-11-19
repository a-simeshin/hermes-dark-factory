#!/usr/bin/env python3
"""Tests of the scripts/ drift watchdog and the green-run stamp.

The gap: an edit of scripts/*.py,*.sh that bypasses teach-snapshot.sh (a
cron job, a manual edit, a worker) is seen by nobody until the next snapshot,
and a RED snapshot is committed anyway. Same class as a check that is silent
about its own breakage.

Contract of check_scripts_drift in hermes-health.py:
  - the scripts/ digest equals the stamp of the last green run: quiet tick,
    run-tests.py is not started;
  - a mismatch runs run-tests.py --audit; RED gives exactly one alert with the
    file name and the reason, a repeated tick without changes is silent and
    does not rerun the suite;
  - a green run on a mismatch: no alert, the stamp is refreshed;
  - a missing stamp (first start) is handled as a mismatch.

Contract of run-tests.py: a full green run with --audit writes the stamp with
the scripts/ digest; a sandbox suite (TESTS_DIR) writes no stamp.

Roots are overridden by env (SCRIPTS_DIR, SCRIPTS_GREEN_STAMP,
SCRIPTS_DRIFT_STATE, SCRIPTS_RUN_TESTS, TESTS_DIR). The live Hermes home and
the network are never touched.
"""
import atexit
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

_HOME = tempfile.mkdtemp(prefix="hh-home-")
atexit.register(shutil.rmtree, _HOME, ignore_errors=True)
os.environ["HERMES_HOME"] = _HOME
os.environ.setdefault("FACTORY_OWNER_CHAT", "telegram:000000000")

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
HEALTH = os.environ.get("HERMES_HEALTH_SCRIPT",
                        os.path.join(SCRIPTS, "hermes-health.py"))
RUN_TESTS = os.path.join(SCRIPTS, "run-tests.py")


def load_health():
    spec = importlib.util.spec_from_file_location(
        "hh_drift_%d" % int(time.time()), HEALTH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class DriftHarness(unittest.TestCase):
    def setUp(self):
        self.mod = load_health()
        self.root = tempfile.mkdtemp(prefix="drift-sbx-")
        self.sdir = os.path.join(self.root, "scripts")
        os.makedirs(self.sdir)
        # a minimal scripts/: a stub runner and two files
        self.runner = os.path.join(self.sdir, "run-tests.py")
        with open(self.runner, "w") as fh:
            fh.write("#!/usr/bin/env python3\n")
        with open(os.path.join(self.sdir, "a.py"), "w") as fh:
            fh.write("A = 1\n")
        with open(os.path.join(self.sdir, "b.sh"), "w") as fh:
            fh.write("#!/usr/bin/env bash\n")
        self.stamp = os.path.join(self.root, "green.json")
        self.state = os.path.join(self.root, "drift.json")
        self.calls = []
        self.result = (0, "TEST TOTAL scripts/: 1/1 green")
        self.mod.run = self._run_stub
        os.environ["SCRIPTS_DIR"] = self.sdir
        os.environ["SCRIPTS_GREEN_STAMP"] = self.stamp
        os.environ["SCRIPTS_DRIFT_STATE"] = self.state
        os.environ["SCRIPTS_RUN_TESTS"] = self.runner
        self._problems = list(self.mod.problems)
        self.mod.problems.clear()

    def _run_stub(self, cmd, timeout=60, **kw):
        self.calls.append(cmd)
        return self.result

    def tearDown(self):
        self.mod.problems.extend(self._problems)
        for k in ("SCRIPTS_DIR", "SCRIPTS_GREEN_STAMP",
                  "SCRIPTS_DRIFT_STATE", "SCRIPTS_RUN_TESTS"):
            os.environ.pop(k, None)

    def write_stamp(self, digest=None):
        d = digest or self.mod.scripts_digest.scripts_digest(self.sdir)
        json.dump({"digest": d, "ts": time.time()}, open(self.stamp, "w"))

    def drift_notes(self):
        return [p for p in self.mod.problems
                if p[0].startswith("scripts-drift")]

    def test_match_quiet(self):
        """The stamp equals the digest: quiet tick, the runner did not start."""
        self.write_stamp()
        self.mod.check_scripts_drift()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.drift_notes(), [])

    def test_red_one_alert_repeat_quiet(self):
        """RED: exactly one alert with the file name; a repeat is silent and does not rerun."""
        self.write_stamp(digest="0" * 64)      # mismatch
        self.result = (1, "FAIL test_dummy.py rc=1 0.1s\n"
                          "     | assert False\n"
                          "RED: test_dummy.py\n")
        self.mod.check_scripts_drift()
        notes = self.drift_notes()
        self.assertEqual(len(notes), 1)
        self.assertIn("test_dummy.py", notes[0][1])
        self.assertIn("RED: test_dummy.py", notes[0][1])
        self.assertEqual(len(self.calls), 1)
        st = json.load(open(self.state))
        self.assertEqual(st["verdict"], "red")
        # a repeated tick without changes: silent, the suite is not rerun
        self.mod.problems.clear()
        self.mod.check_scripts_drift()
        self.assertEqual(self.drift_notes(), [])
        self.assertEqual(len(self.calls), 1)

    def test_green_updates_stamp(self):
        """A green run on a mismatch: no alert, the stamp is refreshed."""
        self.write_stamp(digest="0" * 64)
        self.mod.check_scripts_drift()
        self.assertEqual(self.drift_notes(), [])
        st = json.load(open(self.stamp))
        self.assertEqual(
            st["digest"], self.mod.scripts_digest.scripts_digest(self.sdir))
        # the next tick: quiet, no run
        self.mod.check_scripts_drift()
        self.assertEqual(len(self.calls), 1)

    def test_missing_stamp_bootstraps(self):
        """No stamp: a mismatch, the run starts (first start)."""
        self.mod.check_scripts_drift()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.drift_notes(), [])   # green: silent


class StampWriteTest(unittest.TestCase):
    """run-tests.py writes the stamp only on a full green run of the real suite.

    The runner is loaded as a module and write_green_stamp() is called
    directly: running the full suite from inside the suite is a recursion of
    run-tests into run-tests (every nested run starts this test again).
    """

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="stamp-sbx-")
        self.stamp = os.path.join(self.root, "green.json")
        self.saved = {k: os.environ.get(k) for k in
                      ("SCRIPTS_GREEN_STAMP", "TESTS_DIR")}
        os.environ["SCRIPTS_GREEN_STAMP"] = self.stamp

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _load_runner(self):
        spec = importlib.util.spec_from_file_location(
            "rt_stamp_%d" % int(time.time()), RUN_TESTS)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_default_dir_writes_stamp(self):
        """The real suite (TESTS_DIR unset): a stamp with the scripts/ digest."""
        os.environ.pop("TESTS_DIR", None)
        mod = self._load_runner()
        mod.write_green_stamp()
        self.assertTrue(os.path.exists(self.stamp))
        data = json.load(open(self.stamp))
        self.assertEqual(data["digest"],
                         mod.scripts_digest.scripts_digest(SCRIPTS))

    def test_sandbox_tests_no_stamp(self):
        """A TESTS_DIR sandbox is green but writes no stamp: not the real suite."""
        os.environ["TESTS_DIR"] = os.path.join(self.root, "tests")
        mod = self._load_runner()
        mod.write_green_stamp()
        self.assertFalse(os.path.exists(self.stamp))

    def test_discovery_override(self):
        """The suite is discovered in TESTS_DIR, not in the real scripts/."""
        os.environ["TESTS_DIR"] = os.path.join(self.root, "tests")
        os.makedirs(os.environ["TESTS_DIR"])
        with open(os.path.join(self.root, "tests", "test_trivial.py"),
                  "w") as fh:
            fh.write("print('ok')\n")
        mod = self._load_runner()
        # no need to run main(): it is enough to prove the suite list comes
        # from the sandbox (that is what protects the test above from recursion)
        found = [f for f in os.listdir(mod.TESTS_DIR)
                 if f.startswith("test_") and f.endswith(".py")]
        self.assertEqual(found, ["test_trivial.py"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
