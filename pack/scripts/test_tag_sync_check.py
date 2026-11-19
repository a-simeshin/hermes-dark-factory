#!/usr/bin/env python3
"""tag-sync-check.sh must catch a local tag that disagrees with origin.

A plain fetch never overwrites an existing local tag, so a clone can keep a
stale release tag. Cases:
  in sync  : lightweight and annotated tags, exit 0, no false positive;
  STALE    : a tag moved on origin, plain fetch keeps the old one, exit 1 and
             a STALE line; --force repairs it, exit 0;
  MISSING  : a new tag on origin not fetched yet, exit 1; --force repairs it;
  GONE     : a tag deleted on origin, still local, exit 1 (and --force does
             not delete it).
Red before green: each case also runs against a copy of the script with the
check it covers mutated away, and must fail there.

Sandbox: a bare origin and a clone in a temp dir, a temp HOME, HERMES_HOME and
an empty global git config. Local paths only, no network.
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "tag-sync-check.sh")
TMP = tempfile.mkdtemp(prefix="tag-sync-")
GITCFG = os.path.join(TMP, "gitconfig")
open(GITCFG, "w").close()
ENV = dict(os.environ, HOME=TMP, HERMES_HOME=os.path.join(TMP, "hermes"),
           GIT_CONFIG_GLOBAL=GITCFG, GIT_CONFIG_NOSYSTEM="1",
           GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
           GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")


def git(*args, cwd):
    r = subprocess.run(["git", *args], cwd=cwd, env=ENV, capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 0, (args, r.stderr[-500:])
    return r.stdout.strip()


def commit(work, name):
    with open(os.path.join(work, name), "w") as fh:
        fh.write(name + "\n")
    git("add", "-A", cwd=work)
    git("commit", "-qm", name, cwd=work)


def sandbox():
    """origin (bare), work (pushes to origin), clone (the checked repo)."""
    root = tempfile.mkdtemp(dir=TMP)
    origin, work, clone = (os.path.join(root, n) for n in ("origin", "work", "clone"))
    git("init", "-q", "--bare", "-b", "main", origin, cwd=root)
    git("clone", "-q", origin, work, cwd=root)
    commit(work, "a")
    git("tag", "v1.0", cwd=work)
    git("tag", "-a", "v2.0", "-m", "release", cwd=work)
    git("push", "-q", "origin", "HEAD:main", "--tags", cwd=work)
    git("clone", "-q", origin, clone, cwd=root)
    return work, clone


def run(script, clone, *extra):
    r = subprocess.run(["bash", script, clone, *extra], env=ENV,
                       capture_output=True, text=True, timeout=60)
    return r.returncode, r.stdout + r.stderr


def case_in_sync(script):
    _, clone = sandbox()
    rc, out = run(script, clone)
    assert rc == 0 and "in sync" in out, (rc, out)


def case_stale(script):
    work, clone = sandbox()
    commit(work, "b")
    git("tag", "-f", "v1.0", cwd=work)
    git("push", "-q", "--force", "origin", "HEAD:main", "refs/tags/v1.0", cwd=work)
    git("fetch", "-q", "origin", cwd=clone)  # plain fetch keeps the old tag
    assert git("rev-parse", "v1.0", cwd=clone) != git("rev-parse", "v1.0", cwd=work)
    rc, out = run(script, clone)
    assert rc == 1 and "STALE: v1.0" in out, (rc, out)
    assert "v2.0" not in out, out
    rc, out = run(script, clone, "--force")
    assert rc == 0 and "in sync" in out, (rc, out)
    assert git("rev-parse", "v1.0", cwd=clone) == git("rev-parse", "v1.0", cwd=work)


def case_missing(script):
    work, clone = sandbox()
    git("tag", "v3.0", cwd=work)
    git("push", "-q", "origin", "refs/tags/v3.0", cwd=work)
    rc, out = run(script, clone)
    assert rc == 1 and "MISSING locally: v3.0" in out, (rc, out)
    rc, out = run(script, clone, "--force")
    assert rc == 0 and "in sync" in out, (rc, out)


def case_gone(script):
    work, clone = sandbox()
    git("push", "-q", "origin", ":refs/tags/v2.0", cwd=work)
    rc, out = run(script, clone)
    assert rc == 1 and "GONE on origin: v2.0" in out, (rc, out)
    rc, out = run(script, clone, "--force")
    assert rc == 1 and "GONE on origin: v2.0" in out, (rc, out)


# each mutant removes the check that its case covers
MUTANTS = {
    case_stale: ('if [ "$lpeel" != "$rpeel" ]; then', 'if false; then'),
    case_missing: ('if [ -z "$lhash" ]; then\n    echo "MISSING locally: $tag (origin $rhash)"\n    bad=1',
                   'if [ -z "$lhash" ]; then\n    :'),
    case_gone: ('if [ -z "$(remote_hash "$lref")" ]; then', 'if false; then'),
}


def mutant(src, old, new):
    text = open(SCRIPT).read()
    assert text.count(old) == 1, old
    path = os.path.join(tempfile.mkdtemp(dir=TMP), "tag-sync-check.sh")
    with open(path, "w") as fh:
        fh.write(text.replace(old, new))
    return path


def main():
    try:
        for case in (case_in_sync, case_stale, case_missing, case_gone):
            case(SCRIPT)
            print("ok  ", case.__name__)
        for case, (old, new) in MUTANTS.items():
            try:
                case(mutant(SCRIPT, old, new))
            except AssertionError:
                print("ok  ", case.__name__, "red on mutant")
            else:
                raise AssertionError("%s stays green on its mutant" % case.__name__)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print("PASS test_tag_sync_check")


if __name__ == "__main__":
    sys.exit(main())
