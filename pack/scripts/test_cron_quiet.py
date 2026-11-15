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
  board-watchdog classify: stop/owner/digest taxonomy;
  board-watchdog main: routine is silent and goes to the digest; a stop
    finding (no owner items) prints without the header, first line
    "Dispatcher is stopped: N pipeline-stop finding(s)"; an owner item prints
    the "KANBAN NEEDS ATTENTION" header; stop plus owner together give the
    header and the stop line in one report; stranded_in_ready is pushed only
    on the 3rd run in a row; a repeat within a day is silent (REPEAT_AFTER);
  source checks: volatile pr-hygiene keys are gone, the nooutput key is
    daily, worktree-gc and metrics-evening write and print the digest.

Mutants that turn this red: replacing classify (owner goes to the digest),
replacing the dedup in digest_report (a repeated section is not empty),
dropping the reported.json write, removing the silence in pr-risk
(test_label_nochange_silent / test_label_change_goes_to_digest catch
non-empty stdout), removing STUCK_TICKS (stranded is pushed on the first
run), bringing the volatile keys back (test_pr_hygiene_keys_stable_and_routed).
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
os.environ["FACTORY_OWNER_LOGIN"] = "owner-login"
os.environ["FACTORY_BOARD"] = "main"


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


def make_bw():
    mod = load("bw", os.path.join(SCRIPTS, "board-watchdog.py"),
               {"CRON_DIGEST_DIR": os.path.join(
                   tempfile.mkdtemp(prefix="quiet-load-"), "digest")})
    mod.board_slugs = lambda: ["test"]
    mod.max_in_progress_gate = lambda s, n: None
    mod.code_newer_than_gateway = lambda: None
    mod.holdout_guard = lambda now: []
    mod.conflict_debt_gate = lambda slug, s, n: None
    mod.backlog_gate = lambda slug, s, now=None: []
    mod.stacked_rework_gate = lambda slug: []
    mod.needs_input_tech_gate = lambda slug: []
    mod.holdout_card_lint = lambda slug: []
    return mod


class TestClassify(Base):
    @classmethod
    def setUpClass(cls):
        cls.mod = make_bw()

    def test_class(self):
        f = self.mod.classify_finding
        # stop: a real stop or silence of the pipeline
        for key in ("mcp:nooutput:2026-11-15", "mcp:silent:3",
                    "mcp:stalled:abc", "massfail:2026-11-15",
                    "mcp:diag:t_x:stranded_in_ready",
                    "mcp:diag:t_y:repeated_failures"):
            self.assertEqual(f(key), "stop", key)
        # owner: a decision of the owner
        for key in ("mcp:cap:t_1", "mcp:budget:t_2", "mcp:budget-gate:t_3",
                    "staleblock:t_4", "samepr:t_5", "dup:t_6",
                    "mcp:diag:t_7:stuck_in_blocked",
                    "mcp:diag:t_8:block_unblock_cycling"):
            self.assertEqual(f(key), "owner", key)
        # digest: routine and a broken detector
        for key in ("mcp:idem:t_9", "mcp:lint:err", "mcp:revlayer:err",
                    "mcp:diag:t_10:whatever", "mcp:hold:x"):
            self.assertEqual(f(key), "digest", key)


class TestMainSilence(Base):
    def setUp(self):
        super().setUp()
        self.mod = make_bw()
        self.mod.STATE = os.path.join(self.tmp, "bw-state.json")
        if os.path.exists(self.mod.STATE):
            os.remove(self.mod.STATE)
        self.cap = io.StringIO()

    def findings(self, rows):
        # check(slug, state=None) in the script; the tests ignore both
        self.mod.check = lambda slug, state=None: rows

    def out_and_digest(self):
        self.cap = io.StringIO()
        with redirect_stdout(self.cap):
            self.mod.main()
        return self.cap.getvalue(), self.digest_body()

    def test_routine_goes_to_digest(self):
        self.findings([("t1:idem:abc", 2, "  recreated")])
        out, body = self.out_and_digest()
        self.assertEqual(out, "")
        self.assertIn("recreated", body)

    def test_stop_line_without_header(self):
        # stop without owner items = no header, only the line
        # "Dispatcher is stopped: ..." inside the report.
        self.findings([("t1:silent:3", 0, "  is silent")])
        out, _ = self.out_and_digest()
        self.assertTrue(out.startswith("Dispatcher is stopped: 1 pipeline-stop finding(s)"))
        self.assertNotIn("KANBAN NEEDS ATTENTION", out)
        self.assertIn("is silent", out)

    def test_owner_header(self):
        self.findings([("staleblock:t1", 1, "  waits for you")])
        out, _ = self.out_and_digest()
        self.assertTrue(out.startswith("KANBAN NEEDS ATTENTION"))
        self.assertNotIn("Dispatcher is stopped:", out)
        self.assertIn("waits for you", out)

    def test_stop_and_owner_combined(self):
        # the header comes with an owner item, the stop is a line inside
        self.findings([
            ("t1:silent:3", 0, "  is silent"),
            ("staleblock:t1", 1, "  waits for you")])
        out, _ = self.out_and_digest()
        self.assertTrue(out.startswith("KANBAN NEEDS ATTENTION\n"))
        self.assertIn("Dispatcher is stopped: 1 pipeline-stop finding(s)", out)
        self.assertIn("is silent", out)
        self.assertIn("waits for you", out)

    def test_repeat_is_silent_within_day(self):
        self.findings([("t1:idem:abc", 2, "  recreated")])
        out, _ = self.out_and_digest()
        self.assertEqual(out, "")
        # second run: the finding is already in state, the digest does not grow
        self.findings([])
        cap = io.StringIO()
        with redirect_stdout(cap):
            self.mod.main()
        self.assertEqual(cap.getvalue(), "")

    def test_stranded_needs_three_ticks(self):
        key = "t1:diag:t_x:stranded_in_ready"
        self.findings([(key, 0, "  ready is not empty")])
        for _ in range(2):
            out, body = self.out_and_digest()
            self.assertEqual(out, "")
            self.assertNotIn("ready is not empty", body)
        out, _ = self.out_and_digest()
        self.assertTrue(out.startswith("Dispatcher is stopped: "))
        self.assertNotIn("KANBAN NEEDS ATTENTION", out)
        # after the push, a day of silence
        out, body = self.out_and_digest()
        self.assertEqual(out, "")
        self.assertNotIn("ready is not empty", body)

    def test_empty_is_silent(self):
        self.findings([])
        out, _ = self.out_and_digest()
        self.assertEqual(out, "")


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
    def test_pr_hygiene_keys_stable_and_routed(self):
        src = open(os.path.join(SCRIPTS, "pr-hygiene.py")).read()
        # volatile keys (lists inside the key) are gone: they reset REPEAT_AFTER
        for volatile in ('inreview:%s', 'unreviewed:%s', 'nosummary:%s',
                         'libcontract:%s', 'ready:%s', 'pairs:%s',
                         'obsolete:%s', 'orphan-issues:%s',
                         'mergeflow-debt:%d', 'mergeflow-metric:conf'):
            self.assertNotIn(volatile, src, volatile)
        self.assertIn("digest_add(", src)

    def test_board_nooutput_key_daily(self):
        src = open(os.path.join(SCRIPTS, "board-watchdog.py")).read()
        self.assertNotIn('nooutput:%s" % (slug, time.strftime("%Y-%m-%d-%H"))',
                         src)
        self.assertIn('nooutput:%s" % (slug, time.strftime("%Y-%m-%d"))', src)

    def test_worktree_routes_to_digest(self):
        src = open(os.path.join(SCRIPTS, "worktree-gc.py")).read()
        self.assertIn("digest_add(", src)

    def test_evening_carries_digest(self):
        src = open(os.path.join(SCRIPTS, "metrics-evening.py")).read()
        self.assertIn("DAILY DIGEST", src)
        self.assertIn("digest_report", src)


class Test(unittest.TestCase):
    def test_all(self):
        suite = unittest.TestSuite()
        loader = unittest.TestLoader()
        for cls in (TestDigest, TestClassify, TestMainSilence,
                    TestPrRiskSilence, TestSourceSilence):
            for name in loader.getTestCaseNames(cls):
                if name.startswith("test_"):
                    suite.addTest(cls(name))
        res = unittest.TextTestRunner(verbosity=2).run(suite)
        self.assertEqual(res.failures + res.errors, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
