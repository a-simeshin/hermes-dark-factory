#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the cron_quiet digest (quiet channel for no-agent crons).

Policy under test: a push goes out only for an owner action or a real stop;
routine events go into a daily digest file that the evening report prints.

  digest add writes the file, report returns a section once (a second call
  is empty), a new section is visible again; empty sections are skipped;
  reporting marks reported.json;
  pr-risk label sync: no changes = empty stdout; a relabel = empty stdout plus
    a section in the digest; a gh failure = the only push;
  source check: worktree-gc routes its report to the digest.

Mutants that turn this red: replacing the dedup in digest_report (a repeated
section is not empty), dropping the reported.json write, removing the silence
in pr-risk (t_label_nochange / t_label_change catch non-empty stdout).
"""
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
# Never read or write the real Hermes home; neutral project values for the
# scripts that require them at import.
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="quiet-home-")
os.environ["FACTORY_REPO"] = "acme/widget"
os.environ["FACTORY_REPO_PATH"] = "/srv/hermes/widget"
os.environ["FACTORY_OWNER_CHAT"] = "telegram:000000000"


def load(name, path, env=None):
    save = {}
    for k, v in (env or {}).items():
        save[k] = os.environ.get(k)
        os.environ[k] = v
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        for k, v in save.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return mod


_QUIET = None


def bind_quiet(digest_dir):
    """Load cron_quiet once and point its digest at a test temp directory.

    Scripts import `from cron_quiet import digest_add` inside functions, so
    the module is registered in sys.modules and DIGEST_DIR/_REPORTED are
    switched for every test (the functions read the globals at call time).
    """
    global _QUIET
    if _QUIET is None:
        _QUIET = load("cron_quiet", os.path.join(SCRIPTS, "cron_quiet.py"),
                      {"CRON_DIGEST_DIR": digest_dir})
    os.makedirs(digest_dir, exist_ok=True)
    _QUIET.DIGEST_DIR = digest_dir
    _QUIET._REPORTED = os.path.join(digest_dir, "reported.json")
    sys.modules["cron_quiet"] = _QUIET
    os.environ["CRON_DIGEST_DIR"] = digest_dir  # in case of a fresh import
    return _QUIET


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="quiet-")
        self.digest_dir = os.path.join(self.tmp, "digest")
        bind_quiet(self.digest_dir)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def digest_body(self):
        path = os.path.join(self.digest_dir,
                            time.strftime("%Y-%m-%d") + ".md")
        return open(path).read() if os.path.exists(path) else ""


class TestDigest(Base):
    def test_add_then_report_once(self):
        self._quiet.digest_add("pr-hygiene", "section A")
        out = self._quiet.digest_report()
        self.assertIn("pr-hygiene", out)
        self.assertIn("section A", out)
        # dedup: the same section is not returned a second time
        self.assertEqual(self._quiet.digest_report(), "")
        # a new section is visible again
        self._quiet.digest_add("pr-hygiene", "section B")
        self.assertIn("section B", self._quiet.digest_report())

    def test_empty_section_skipped(self):
        self._quiet.digest_add("x", "")
        self.assertEqual(self._quiet.digest_report(), "")

    def test_reported_marks_file(self):
        self._quiet.digest_add("x", "body")
        self._quiet.digest_report()
        reported = json.load(open(os.path.join(self.digest_dir,
                                               "reported.json")))
        self.assertTrue(reported)

    def setUp(self):
        super().setUp()
        self._quiet = bind_quiet(self.digest_dir)


class TestPrRiskSilence(Base):
    @classmethod
    def setUpClass(cls):
        cls.mod = load("pr_risk", os.path.join(SCRIPTS, "pr-risk.py"),
                       {"CRON_DIGEST_DIR": os.path.join(
                           tempfile.mkdtemp(prefix="quiet-prr-"), "digest")})
        cls.mod.classify = lambda files, cfg: ("major", [])

    def setUp(self):
        super().setUp()
        self.mod = type(self).mod
        self.mod.LABEL_HOLD = os.path.join(self.tmp, "no-hold")
        self._ok = type("R", (), {"returncode": 0, "stderr": ""})
        self._bad = type("R", (), {"returncode": 1, "stderr": "boom"})
        self.mod.subprocess = type("S", (), {"run": None})

    def gh(self, numbers=None, retcode=0):
        ret = self._ok if retcode == 0 else self._bad
        self.mod.subprocess.run = staticmethod(
            lambda cmd, **kw: ret())
        self.mod.gh_json = lambda args: [
            {"number": numbers[0] if numbers else 7,
             "labels": [{"name": "risk:minor"}]}]
        self.mod.pr_files = lambda n: []

    def test_label_nochange_silent(self):
        self.mod.gh_json = lambda args: [
            {"number": 1, "labels": [{"name": "risk:major"}]}]
        self.mod.pr_files = lambda n: []
        cap = io.StringIO()
        with redirect_stdout(cap):
            rc = self.mod.cmd_label_sync()
        self.assertEqual((rc, cap.getvalue()), (0, ""))

    def test_label_change_goes_to_digest(self):
        self.gh(numbers=[7])
        cap = io.StringIO()
        with redirect_stdout(cap):
            rc = self.mod.cmd_label_sync()
        self.assertEqual((rc, cap.getvalue()), (0, ""))
        self.assertIn("#7: risk:major -> ok", self.digest_body())

    def test_label_error_pushes(self):
        self.gh(numbers=[8], retcode=1)
        cap = io.StringIO()
        with redirect_stdout(cap):
            self.mod.cmd_label_sync()
        self.assertIn("ERROR", cap.getvalue())


class TestSourceSilence(unittest.TestCase):
    def test_worktree_routes_to_digest(self):
        src = open(os.path.join(SCRIPTS, "worktree-gc.py")).read()
        self.assertIn("digest_add(", src)


class Test(unittest.TestCase):
    def test_all(self):
        suite = unittest.TestSuite()
        loader = unittest.TestLoader()
        for cls in (TestDigest, TestPrRiskSilence, TestSourceSilence):
            for name in loader.getTestCaseNames(cls):
                if name.startswith("test_"):
                    suite.addTest(cls(name))
        res = unittest.TextTestRunner(verbosity=2).run(suite)
        self.assertEqual(res.failures + res.errors, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
