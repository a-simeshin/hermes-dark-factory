#!/usr/bin/env python3
"""Tests for the pre-push identity gate (prepush-identity-guard.py).

The case behind the gate: commits went out with the author Hermes Agent
<hermes@nousresearch.com>, and GitHub handed the authorship to an unrelated
third-party account. A commit whose author or committer is on
nousresearch.com must refuse the push before sending; correct addresses
pass; a subdomain and upper case are caught; deleting a branch checks
nothing; commits already on the remote are out of scope; the gate has no
bypass env; a git failure = fail closed; outside a git repository the gate
is silent.

Mutant: PREPUSH_IDENTITY_GUARD_SCRIPT=/tmp/mutant-prepush-identity.py \
  python3 test_prepush_identity_guard.py
(build the mutant with:
  sed -E 's/^([[:space:]]*)findings\\.append.*/\\1pass  # mutant/' \
  prepush-identity-guard.py > /tmp/mutant-prepush-identity.py
it silences the findings, so every push passes. The tests must fail on it:
a red run proves they check the refusal itself. Note the mutant must live
next to factory_config.py to import it.)
Run: python3 test_prepush_identity_guard.py
"""
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile

# The script reads FACTORY_BOT_EMAIL through factory_config: point the Hermes
# home at an empty temp dir so the test never sees a real installation.
HH = tempfile.mkdtemp(prefix="hermes-home-")
os.environ["HERMES_HOME"] = HH
os.environ.pop("FACTORY_BOT_EMAIL", None)
SCRIPT = os.environ.get("PREPUSH_IDENTITY_GUARD_SCRIPT") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "prepush-identity-guard.py")

spec = importlib.util.spec_from_file_location("prepush_identity_guard", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

FAILS = []
ZERO = "0" * 40
BAD = "hermes@nousresearch.com"
BAD_SUB = "x@mail.nousresearch.com"
BAD_UP = "HERMES@NOUSRESEARCH.COM"
OK = "1234567+factory-bot[bot]@users.noreply.github.com"


def check(name, cond, detail: object = ""):
    if cond:
        print("ok   %s" % name)
    else:
        print("FAIL %s %s" % (name, detail))
        FAILS.append(name)


def git(*args, cwd=None, env=None):
    e = dict(os.environ)
    if env:
        e.update(env)
    r = subprocess.run(["git"] + list(args), cwd=cwd, env=e,
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit("git %s: %s" % (args, r.stderr))
    return r.stdout.strip()


def commit(repo, files, msg="fixture", author_env=None, committer_env=None,
           ident=None):
    for path, text in files.items():
        full = os.path.join(repo, path)
        os.makedirs(os.path.dirname(full) or repo, exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(text)
        git("add", path, cwd=repo)
    env = {}
    if author_env:
        env.update(author_env)
    if committer_env:
        env.update(committer_env)
    if ident:
        env.update({"GIT_AUTHOR_NAME": ident[0], "GIT_AUTHOR_EMAIL": ident[1],
                    "GIT_COMMITTER_NAME": ident[0],
                    "GIT_COMMITTER_EMAIL": ident[1]})
    git("commit", "-m", msg, cwd=repo, env=env or None)
    return git("rev-parse", "HEAD", cwd=repo)


REPO = tempfile.mkdtemp(prefix="identity-guard-")
git("init", "-q", "-b", "main", REPO)
git("config", "user.email", "t@example.com", cwd=REPO)
git("config", "user.name", "t", cwd=REPO)
os.chdir(REPO)

C1 = commit(REPO, {"a.txt": "base\n"}, ident=("t", "t@example.com"))
git("checkout", "-q", "-b", "good", C1)
CG = commit(REPO, {"good.txt": "x\n"}, "good",
            ident=("factory-bot[bot]", OK))
git("checkout", "-q", "main")
CA = commit(REPO, {"bad-author.txt": "x\n"}, "bad author",
            ident=("Hermes Agent", BAD))
CC = commit(REPO, {"bad-committer.txt": "x\n"}, "bad committer",
            committer_env={"GIT_COMMITTER_NAME": "Hermes Agent",
                           "GIT_COMMITTER_EMAIL": BAD})
CS = commit(REPO, {"bad-subdomain.txt": "x\n"}, "bad subdomain",
            ident=("Hermes Agent", BAD_SUB))
CU = commit(REPO, {"bad-upper.txt": "x\n"}, "bad upper case",
            ident=("Hermes Agent", BAD_UP))

# --- run(): refusals and passes ----------------------------------------------
rc, found = mod.run([("refs/heads/b", CA, "refs/remotes/origin/b", ZERO)])
check("bad author rejects push", rc == 1
      and any(s == CA and r == "author" for s, r, _w in found), (rc, found))

rc, found = mod.run([("refs/heads/b", CC, "refs/remotes/origin/b", ZERO)])
check("bad committer rejects push", rc == 1
      and any(s == CC and r == "committer" for s, r, _w in found)
      and not any(s == CC and r == "author" for s, r, _w in found),
      (rc, found))

rc, found = mod.run([("refs/heads/b", CS, "refs/remotes/origin/b", ZERO)])
check("subdomain address is rejected", rc == 1 and found, (rc, found))

rc, found = mod.run([("refs/heads/b", CU, "refs/remotes/origin/b", ZERO)])
check("upper case domain is rejected", rc == 1 and found, (rc, found))

rc, found = mod.run([("refs/heads/good", CG, "refs/remotes/origin/good", ZERO)])
check("good identity passes", rc == 0 and found == [], (rc, found))

rc, found = mod.run([("refs/heads/good", CG, "refs/remotes/origin/good", CA)])
check("commits already on remote are out of scope", rc == 0 and found == [],
      (rc, found))

calls = []


def counting_sh(args):
    calls.append(args)
    return mod.sh.__wrapped__(args) if hasattr(mod.sh, "__wrapped__") \
        else _orig_sh(args)


_orig_sh = mod.sh
mod.sh = counting_sh
rc, found = mod.run([("refs/heads/b", ZERO, "refs/remotes/origin/b", CA)])
mod.sh = _orig_sh
check("branch deletion checks nothing",
      rc == 0 and found == [] and calls == [], (rc, found, calls))

# --- bad_email(): exact boundaries -------------------------------------------
check("exact domain is bad", mod.bad_email("nousresearch.com"))
check("user@domain is bad", mod.bad_email(BAD))
check("subdomain is bad", mod.bad_email(BAD_SUB))
check("mixed case is bad", mod.bad_email(BAD_UP))
check("stray suffix is not bad", not mod.bad_email("x@nousresearch.com.evil"))
check("similar domain is not bad", not mod.bad_email("x@nous-research.com"))
check("noreply github is not bad", not mod.bad_email(OK))
check("empty email is not bad", not mod.bad_email(""))

# --- main(): stdin, garbage lines, fail closed, no bypass ---------------------
rc = mod.main([], stream=io.StringIO(
    "garbage line\nrefs/heads/b %s refs/remotes/origin/b %s\n"
    % (CA, ZERO)))
check("main blocks push with bad author and skips garbage", rc == 1, rc)

rc = mod.main([], stream=io.StringIO(
    "refs/heads/b %s refs/remotes/origin/b %s\n" % (CG, C1)))
check("main allows good push", rc == 0, rc)

rc = mod.main([], stream=io.StringIO(
    "refs/heads/b %s refs/remotes/origin/b %s\n" % (ZERO, CA)))
check("main ignores deletion", rc == 0, rc)

rc = mod.main([], stream=io.StringIO(
    "refs/heads/b %s refs/remotes/origin/b %s\n" % ("f" * 40, ZERO)))
check("git failure fails closed", rc == 1, rc)

os.environ["SKIP_LINK_GUARD"] = "1"
os.environ["SKIP_STATIC_GATE"] = "1"
os.environ["SKIP_IDENTITY_GUARD"] = "1"
rc = mod.main([], stream=io.StringIO(
    "refs/heads/b %s refs/remotes/origin/b %s\n" % (CA, ZERO)))
check("no bypass env is honored", rc == 1, rc)
for k in ("SKIP_LINK_GUARD", "SKIP_STATIC_GATE", "SKIP_IDENTITY_GUARD"):
    os.environ.pop(k, None)

# --- scan-ref -----------------------------------------------------------------
rc = mod.main(["--scan-ref", "main"])
check("scan-ref finds bad commits in history", rc == 1)
rc = mod.main(["--scan-ref", "main", "--base", "main"])
check("scan-ref at own tip has nothing new", rc == 0, rc)

saved = os.getcwd()
# The temp dir may sit inside a directory that is itself a git repository:
# without a ceiling rev-parse would find it and the test would take the wrong branch.
norepo = tempfile.mkdtemp(prefix="identity-guard-norepo-")
os.environ["GIT_CEILING_DIRECTORIES"] = os.path.dirname(norepo)
os.chdir(norepo)
rc = mod.main([], stream=io.StringIO(
    "refs/heads/b %s refs/remotes/origin/b %s\n" % (CA, ZERO)))
check("outside git repo guard is silent pass", rc == 0, rc)
os.chdir(saved)
os.environ.pop("GIT_CEILING_DIRECTORIES", None)

shutil.rmtree(REPO, ignore_errors=True)
shutil.rmtree(HH, ignore_errors=True)

print()
if FAILS:
    print("FAILED: %d" % len(FAILS))
    sys.exit(1)
print("All pre-push identity gate checks are green")
