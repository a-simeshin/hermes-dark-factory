#!/usr/bin/env python3
"""Tests for the pre-push link gate (prepush-link-guard.py).

The case behind the gate: two dozen links to a nonexistent repository in
markdown went out through git push, past the gh wrapper. Added lines with a
broken link must refuse the push before sending; correct ones pass; links in
code outside comments are not checked; a failed check means fail closed;
outside a Hermes session the gate is off; deleting a branch checks nothing;
a root commit is checked (a new branch is checked in full).

Mutant: PREPUSH_LINK_GUARD_SCRIPT=/tmp/mutant-prepush.py python3 \
  test_prepush_link_guard.py
(build the mutant with:
  sed -E 's/^([[:space:]]*)findings\\.append.*/\\1pass  # mutant/'
  prepush-link-guard.py > /tmp/mutant-prepush.py
it silences the findings, so every push passes. The tests must fail on it:
a red run proves they check the refusal itself. Note the mutant must live
next to gh-link-guard.py and factory_config.py to import them.)
Run: python3 test_prepush_link_guard.py
"""
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
# The script reads FACTORY_BASE_BRANCHES through factory_config: point the
# Hermes home at an empty temp dir so the test never sees a real installation.
HH = tempfile.mkdtemp(prefix="hermes-home-")
os.environ["HERMES_HOME"] = HH
os.environ.pop("FACTORY_BASE_BRANCHES", None)
SCRIPT = os.environ.get("PREPUSH_LINK_GUARD_SCRIPT") or os.path.join(
    HERE, "prepush-link-guard.py")

spec = importlib.util.spec_from_file_location("prepush_link_guard", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

FAILS = []
ZERO = "0" * 40
REPO_OK = "repos/acme/widget"
REPO_404 = "repos/acmee/widget"
TYPOLINK = "https://github.com/acmee/widget"
GOODLINK = "https://github.com/acme/widget"


def check(name, cond, detail=""):
    if cond:
        print("ok   %s" % name)
    else:
        print("FAIL %s %s" % (name, detail))
        FAILS.append(name)


def fake_check(paths_ok, paths_404=()):
    def _check(api):
        if api in paths_404:
            return False
        if api in paths_ok:
            return True
        raise mod.GuardError("unexpected request: %s" % api)
    return _check


def git(*args, cwd=None):
    r = subprocess.run(["git"] + list(args), cwd=cwd,
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit("git %s: %s" % (args, r.stderr))
    return r.stdout.strip()


def commit(repo, files, msg="fixture"):
    for path, text in files.items():
        full = os.path.join(repo, path)
        os.makedirs(os.path.dirname(full) or repo, exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(text)
        git("add", path, cwd=repo)
    git("commit", "-m", msg, cwd=repo)
    return git("rev-parse", "HEAD", cwd=repo)


REPO = tempfile.mkdtemp(prefix="prepush-guard-")
git("init", "-q", "-b", "main", REPO)
git("config", "user.email", "t@example.com", cwd=REPO)
git("config", "user.name", "t", cwd=REPO)
os.chdir(REPO)

C1 = commit(REPO, {
    "README.md": "base, valid link %s\n" % GOODLINK,
    "src/A.java": "/** javadoc, valid: %s */\npublic class A {}\n" % GOODLINK,
})
C2 = commit(REPO, {
    "docs/bad.md": "owner typo %s and once more %s\n" % (TYPOLINK, TYPOLINK),
    "src/B.java": "/** javadoc with a typo %s */\npublic class B {}\n" % TYPOLINK,
    "src/C.java": 'public class C { String u = "%s"; }\n' % TYPOLINK,
})
C3 = commit(REPO, {"docs/plain.md": "text without links\n"})

git("checkout", "-q", "-b", "good", C1)
C5 = commit(REPO, {"docs/good2.md": "valid %s\n" % GOODLINK})
git("checkout", "-q", "main")

FAKE = fake_check([REPO_OK], [REPO_404])

# --- run(): diffs, filters, refusal -----------------------------------------
rc, found = mod.run([("refs/heads/f", C2, "refs/remotes/origin/f", ZERO)],
                    check=FAKE)
files_found = sorted({f for f, _u, _w in found})
check("new branch push with typo md is rejected", rc == 1, (rc, found))
check("typo in md reported with file", "docs/bad.md" in files_found, files_found)
check("typo in javadoc reported with file", "src/B.java" in files_found, files_found)
check("string literal typo is out of scope", "src/C.java" not in files_found,
      files_found)

rc, found = mod.run([("refs/heads/f", C3, "refs/remotes/origin/f", C2)],
                    check=FAKE)
check("incremental push without links passes", rc == 0 and found == [],
      (rc, found))

rc, found = mod.run([("refs/heads/good", C5, "refs/remotes/origin/good", ZERO)],
                    check=FAKE)
check("branch with only valid links passes", rc == 0 and found == [],
      (rc, found))

calls = []


def no_calls(api):
    calls.append(api)
    return True


rc, found = mod.run([("refs/heads/f", ZERO, "refs/remotes/origin/f", C2)],
                    check=no_calls)
check("branch deletion checks nothing", rc == 0 and found == [] and calls == [],
      (rc, found, calls))


def boom(api):
    raise mod.GuardError("network went away")


rc, found = mod.run([("refs/heads/f", C2, "refs/remotes/origin/f", ZERO)],
                    check=boom)
check("api error fails closed", rc == 1 and found and "unavailable" in found[0][2],
      (rc, found))

# --- comment filter ----------------------------------------------------------
check("trailing // comment is checked",
      mod.comment_part("x = 1; // see " + TYPOLINK) is not None)
check("code line without comment is skipped",
      mod.comment_part('s = "%s";' % TYPOLINK) is None)
check("javadoc continuation is checked", mod.comment_part(" * see " + TYPOLINK) is not None)
cl = mod.candidate_lines("src/C.java", ['s = "%s";' % TYPOLINK,
                                        '// see %s' % TYPOLINK])
check("candidate_lines keeps comment links only", cl == ["// see %s" % TYPOLINK],
      cl)

# --- GH_TOKEN for gh api (a live e2e once hit 401 without it) -----------------
FAKE_TOKEN_SH = os.path.join(tempfile.mkdtemp(prefix="fake-token-"),
                             "gh-app-token.sh")
with open(FAKE_TOKEN_SH, "w", encoding="utf-8") as fh:
    fh.write("#!/bin/sh\necho fake-token-123\n")
os.chmod(FAKE_TOKEN_SH, 0o755)
BOOM_SH = FAKE_TOKEN_SH + ".boom"
with open(BOOM_SH, "w", encoding="utf-8") as fh:
    fh.write("#!/bin/sh\nexit 3\n")
os.chmod(BOOM_SH, 0o755)
saved = os.environ.pop("GH_TOKEN", None)
try:
    mod.ensure_gh_token(FAKE_TOKEN_SH)
    check("gh token minted from script when env is empty",
          os.environ.get("GH_TOKEN") == "fake-token-123",
          str(os.environ.get("GH_TOKEN")))
    os.environ["GH_TOKEN"] = "already-here"
    mod.ensure_gh_token(BOOM_SH)
    check("existing GH_TOKEN wins, mint script not called",
          os.environ["GH_TOKEN"] == "already-here", os.environ["GH_TOKEN"])
    os.environ["GH_TOKEN"] = saved if saved else "already-here"
    if not saved:
        os.environ.pop("GH_TOKEN", None)
finally:
    if saved:
        os.environ["GH_TOKEN"] = saved

# --- root commit: a new branch without ancestors -----------------------------
REPO2 = tempfile.mkdtemp(prefix="prepush-guard-root-")
git("init", "-q", "-b", "main", REPO2)
git("config", "user.email", "t@example.com", cwd=REPO2)
git("config", "user.name", "t", cwd=REPO2)
os.chdir(REPO2)
ROOT = commit(REPO2, {"docs/root.md": "typo right from the root %s\n" % TYPOLINK})
rc, found = mod.run([("refs/heads/main", ROOT, "refs/remotes/origin/main", ZERO)],
                    check=FAKE)
check("root commit link is checked", rc == 1 and found, (rc, found))
os.chdir(REPO)

# --- main(): session, bypass, scan -------------------------------------------
saved_session = mod.gh_link_guard.in_hermes_session
saved_api = mod.gh_link_guard.gh_api_ok
try:
    mod.gh_link_guard.in_hermes_session = lambda env=None: False
    rc = mod.main([], stream=io.StringIO(
        "refs/heads/f %s refs/remotes/origin/f %s\n" % (C2, ZERO)))
    check("outside hermes session guard is off", rc == 0, rc)

    mod.gh_link_guard.in_hermes_session = lambda env=None: True
    mod.gh_link_guard.gh_api_ok = FAKE
    rc = mod.main([], stream=io.StringIO(
        "refs/heads/f %s refs/remotes/origin/f %s\n" % (C2, ZERO)))
    check("main blocks push with typo", rc == 1, rc)

    rc = mod.main([], stream=io.StringIO(
        "garbage line\nrefs/heads/good %s refs/remotes/origin/good %s\n"
        % (C5, ZERO)))
    check("main allows valid and skips garbage lines", rc == 0, rc)

    os.environ["SKIP_LINK_GUARD"] = "1"
    rc = mod.main([], stream=io.StringIO(
        "refs/heads/f %s refs/remotes/origin/f %s\n" % (C2, ZERO)))
    check("SKIP_LINK_GUARD is a conscious bypass", rc == 0, rc)
    del os.environ["SKIP_LINK_GUARD"]

    mod.gh_link_guard.in_hermes_session = lambda env=None: False
    rc = mod.main(["--scan-ref", "main"])
    check("scan-ref works outside session and finds typo", rc == 1, rc)
    rc = mod.main(["--scan-ref", "main", "--base", C1])
    check("scan-ref with explicit base finds typo", rc == 1, rc)
    rc = mod.main(["--scan-ref", "main", "--base", C3])
    check("scan-ref at own tip has nothing new", rc == 0, rc)
    rc = mod.main(["--scan-ref", "good", "--base", C1])
    check("scan-ref of good branch passes", rc == 0, rc)
finally:
    mod.gh_link_guard.in_hermes_session = saved_session
    mod.gh_link_guard.gh_api_ok = saved_api
    os.environ.pop("SKIP_LINK_GUARD", None)

shutil.rmtree(REPO, ignore_errors=True)
shutil.rmtree(REPO2, ignore_errors=True)
shutil.rmtree(HH, ignore_errors=True)

print()
if FAILS:
    print("FAILED: %d" % len(FAILS))
    sys.exit(1)
print("All pre-push link gate checks are green")
