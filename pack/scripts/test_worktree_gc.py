#!/usr/bin/env python3
"""Tests for the .wt-keep marker in worktree-gc.py (worktrees outside a card).

A scheduled worktree-gc once removed worktrees in the middle of a capture
round: they had no card, no PR and no dirty files. A .wt-keep marker with a
date extends the life; an expired marker does not protect. The integration
test builds a temporary repository with a bare origin and three worktrees:
a fresh marker, no marker, an expired marker.

Never touches the real Hermes home: HERMES_HOME points to a temp directory
holding a one-card board.
"""
import importlib.util
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REAL = os.environ.get("WORKTREE_GC_SCRIPT", os.path.join(HERE, "worktree-gc.py"))

# The script reads its configuration when it is imported.
TEST_HOME = tempfile.mkdtemp(prefix="wtk-home-")
os.makedirs(os.path.join(TEST_HOME, "kanban", "boards", "main"))
_db = sqlite3.connect(os.path.join(TEST_HOME, "kanban", "boards", "main", "kanban.db"))
_db.execute("create table tasks(id text, status text)")
_db.execute("insert into tasks values('t_00000001', 'done')")
_db.commit()
_db.close()
os.environ["HERMES_HOME"] = TEST_HOME
os.environ["FACTORY_REPO_PATH"] = os.path.join(TEST_HOME, "no-such-repo")
sys.path.insert(0, HERE)


def load_gc(path=REAL):
    spec = importlib.util.spec_from_file_location("wtgc_%d" % id(path), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TmpRepo:
    """main repo + bare origin + three worktrees: keep / plain / expired."""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="wtk-int-")
        self.repo = os.path.join(self.root, "repo")
        self.origin = os.path.join(self.root, "origin.git")
        env = dict(os.environ,
                   GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")

        os.makedirs(self.repo)
        def git(*args, cwd=None):
            subprocess.run(["git"] + list(args), cwd=cwd or self.repo,
                           env=env, check=True, capture_output=True)

        git("init", "-q", "-b", "main")
        open(os.path.join(self.repo, "f.txt"), "w").write("x")
        git("add", "f.txt"); git("commit", "-qm", "init")
        subprocess.run(["git", "clone", "-q", "--bare", self.repo, self.origin],
                       check=True, capture_output=True)
        git("remote", "add", "origin", self.origin)
        git("push", "-q", "origin", "main")
        for name in ("keep", "plain", "expired"):
            git("worktree", "add", "-q", "-b", "wtk-" + name,
                os.path.join(self.root, name), "main")
            git("push", "-q", "origin", "wtk-" + name)

    def path(self, name):
        return os.path.join(self.root, name)

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


class TestKeepUntilUnit(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="wtk-unit-")
        self.gc = load_gc()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_u1_no_marker_gives_none(self):
        self.assertIsNone(self.gc.keep_until(self.d))

    def test_u2_future_date_parsed(self):
        open(os.path.join(self.d, ".wt-keep"), "w").write("2099-01-01\n")
        self.assertEqual(str(self.gc.keep_until(self.d)), "2099-01-01")

    def test_u3_garbage_gives_none(self):
        open(os.path.join(self.d, ".wt-keep"), "w").write("tomorrow\n")
        self.assertIsNone(self.gc.keep_until(self.d))


class TestKeepIntegration(unittest.TestCase):
    def setUp(self):
        self.repo = TmpRepo()
        # The cleanup report goes to the digest, not to stdout; the tests
        # redirect the digest to a temp directory (read by the child process
        # at import time).
        self.digest_dir = tempfile.mkdtemp(prefix="wtk-digest-")

    def tearDown(self):
        self.repo.cleanup()
        shutil.rmtree(self.digest_dir, ignore_errors=True)

    def run_gc(self, script=REAL):
        # PYTHONPATH lets a mutated copy living in another directory find
        # factory_config and cron_quiet next to the real script.
        pypath = os.pathsep.join(filter(None, [HERE, os.environ.get("PYTHONPATH")]))
        r = subprocess.run(
            [sys.executable, script],
            env=dict(os.environ, HERMES_HOME=TEST_HOME,
                     FACTORY_REPO_PATH=self.repo.repo,
                     CRON_DIGEST_DIR=self.digest_dir, PYTHONPATH=pypath),
            capture_output=True, text=True, timeout=300)
        return r

    def test_i1_marker_protects_expired_and_plain_removed(self):
        open(self.repo.path("keep") + "/.wt-keep", "w").write("2099-01-01\n")
        open(self.repo.path("expired") + "/.wt-keep", "w").write("2020-01-01\n")
        r = self.run_gc()
        self.assertEqual(r.returncode, 0, r.stderr[-300:])
        self.assertTrue(os.path.isdir(self.repo.path("keep")),
                        "a fresh marker must protect the worktree")
        self.assertFalse(os.path.exists(self.repo.path("plain")),
                         "a worktree without marker, card and PR is removed as before")
        self.assertFalse(os.path.exists(self.repo.path("expired")),
                         "an expired marker does not protect")
        # stdout is empty (quiet); the cleanup report is in the daily digest.
        self.assertEqual(r.stdout, "")
        digest = os.path.join(self.digest_dir,
                              time.strftime("%Y-%m-%d") + ".md")
        self.assertIn(".wt-keep", open(digest).read())

    def test_m1_marker_check_removed(self):
        src = open(REAL).read()
        old = "if keep and keep >= datetime.date.today():"
        self.assertIn(old, src)
        mutated = os.path.join(tempfile.mkdtemp(prefix="wtk-mut-"),
                               "worktree-gc.py")
        open(mutated, "w").write(src.replace(old, "if False:", 1))
        open(self.repo.path("keep") + "/.wt-keep", "w").write("2099-01-01\n")
        r = self.run_gc(script=mutated)
        self.assertFalse(os.path.isdir(self.repo.path("keep")),
                         "the mutant without the marker check must remove the protected worktree")


if __name__ == "__main__":
    unittest.main(verbosity=2)
