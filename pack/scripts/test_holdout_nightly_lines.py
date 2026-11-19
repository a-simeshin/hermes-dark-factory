#!/usr/bin/env python3
"""holdout-nightly-prep.sh must not die as a whole when one base branch
disappears from origin (an end-of-life line deleted on its last day). The
missing branch is skipped with a note on stderr and null in the JSON, the run
goes on, and the head of the other branch is computed as usual.

Sandbox: a local origin with only the branches the case needs, a gh stub
(no milestones, no PRs), a copy of the prep script next to a stub
holdout-stands-kill.py, and a temporary Hermes home with factory.env. Never
touches a real Hermes home or repository.

Case C covers FACTORY_HOLDOUT_PRODUCT_EXT: unset, any changed file counts as a
product change (the PR gets an artifact); set to an extension the PR does not
touch, the PR needs no build (artifact null).
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PREP = os.path.join(HERE, "holdout-nightly-prep.sh")
OLD, NEW = "release/1.x", "release/2.x"


def sh(cmd, cwd=None, env=None):
    r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True,
                       text=True, timeout=120)
    assert r.returncode == 0, (cmd, r.returncode, r.stderr[-500:])
    return r.stdout.strip()


def make_sandbox(with_old, pr=None, extra_env=""):
    tmp = tempfile.mkdtemp(prefix="holdout-lines-")
    origin = os.path.join(tmp, "origin")
    os.makedirs(origin)
    sh(["git", "init", "-q", "-b", "main", origin], cwd=origin)
    with open(os.path.join(origin, "f.txt"), "w") as fh:
        fh.write("x\n")
    sh(["git", "add", "-A"], cwd=origin)
    sh(["git", "-c", "user.name=t", "-c", "user.email=t@t",
        "commit", "-qm", "init"], cwd=origin)
    sh(["git", "branch", NEW], cwd=origin)
    if with_old:
        sh(["git", "branch", OLD], cwd=origin)
    if pr:
        # an open PR: refs/pull/<pr>/head on origin, one commit with a .py change
        sh(["git", "checkout", "-q", "-b", "feature"], cwd=origin)
        with open(os.path.join(origin, "tool.py"), "w") as fh:
            fh.write("print(1)\n")
        sh(["git", "add", "-A"], cwd=origin)
        sh(["git", "-c", "user.name=t", "-c", "user.email=t@t",
            "commit", "-qm", "pr"], cwd=origin)
        sh(["git", "update-ref", "refs/pull/%d/head" % pr, "HEAD"], cwd=origin)
        sh(["git", "checkout", "-q", "main"], cwd=origin)
    clone = os.path.join(tmp, "clone")
    sh(["git", "clone", "-q", origin, clone])
    sh(["git", "fetch", "-q", "origin"], cwd=clone)

    stubbin = os.path.join(tmp, "bin")
    os.makedirs(stubbin)
    with open(os.path.join(stubbin, "gh"), "w") as fh:
        fh.write('#!/bin/sh\n'
                 'if [ "$1" = "api" ]; then echo "[]"; exit 0; fi\n'
                 '# pr list: the PR of the case, or empty output\n'
                 + ('echo %d\n' % pr if pr else '') +
                 'exit 0\n')
    os.chmod(os.path.join(stubbin, "gh"), 0o755)

    scripts = os.path.join(tmp, "scripts")
    os.makedirs(scripts)
    shutil.copy(PREP, os.path.join(scripts, "holdout-nightly-prep.sh"))
    shutil.copy(os.path.join(HERE, "wt-keep.sh"), os.path.join(scripts, "wt-keep.sh"))
    if pr:
        # a ready artifact in the PR worktree: the case needs no real build
        wt = os.path.join(clone, ".worktrees", "holdout-pr%d" % pr)
        sh(["git", "fetch", "-q", "origin",
            "+refs/pull/%d/head:refs/holds/pr%d" % (pr, pr)], cwd=clone)
        sh(["git", "worktree", "add", "-q", "--detach", wt,
            "refs/holds/pr%d" % pr], cwd=clone)
        os.makedirs(os.path.join(wt, "target"))
        with open(os.path.join(wt, "target", "app.jar"), "w") as fh:
            fh.write("artifact\n")
    with open(os.path.join(scripts, "holdout-stands-kill.py"), "w") as fh:
        fh.write("#!/bin/sh\nexit 0\n")
    os.chmod(os.path.join(scripts, "holdout-stands-kill.py"), 0o755)

    home = os.path.join(tmp, "home")
    os.makedirs(home)
    with open(os.path.join(home, "factory.env"), "w") as fh:
        fh.write("FACTORY_REPO=acme/widget\n"
                 "FACTORY_REPO_PATH=%s\n"
                 "FACTORY_BASE_BRANCHES=%s,%s\n"
                 "FACTORY_HOLDOUT_BUILD_CMD=true\n"
                 "FACTORY_HOLDOUT_ARTIFACT_GLOB=target/*.jar\n"
                 % (clone, OLD, NEW) + extra_env)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("FACTORY_")}
    env["HERMES_HOME"] = home
    env["PATH"] = stubbin + os.pathsep + env.get("PATH", "")
    return clone, env, tmp, os.path.join(scripts, "holdout-nightly-prep.sh")


def run_prep(script, env):
    return subprocess.run(["bash", script], env=env, capture_output=True,
                          text=True, timeout=180)


def check(name, ok, detail=""):
    if not ok:
        print("FAIL %s %s" % (name, detail))
        sys.exit(1)
    print("OK " + name)


def main():
    # A: the old line is gone from origin
    clone, env, tmp, script = make_sandbox(with_old=False)
    try:
        sha_new = sh(["git", "rev-parse", "--short", "origin/" + NEW],
                     cwd=clone)
        r = run_prep(script, env)
        check("A rc=0 (the run did not die)", r.returncode == 0,
              (r.stderr or "")[-400:])
        check("A old line is null in the JSON",
              '"%s":null' % OLD in r.stdout, r.stdout)
        check("A new line head %s computed" % sha_new,
              '"%s":"%s"' % (NEW, sha_new) in r.stdout, r.stdout)
        check("A skip note on stderr",
              OLD in r.stderr and "skipped" in r.stderr, r.stderr[-300:])
        check("A JSON is valid with empty targets",
              '"targets":[]}' in r.stdout, r.stdout)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # B: both lines exist, the base path does not regress
    clone, env, tmp, script = make_sandbox(with_old=True)
    try:
        sha_old = sh(["git", "rev-parse", "--short", "origin/" + OLD],
                     cwd=clone)
        r = run_prep(script, env)
        check("B rc=0", r.returncode == 0, (r.stderr or "")[-400:])
        check("B old line head %s computed" % sha_old,
              '"%s":"%s"' % (OLD, sha_old) in r.stdout, r.stdout)
        check("B no skip notes", "skipped" not in r.stderr,
              r.stderr[-300:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # C: product extensions are configuration, no built-in stack
    for ext, want_artifact in (("", True), ("FACTORY_HOLDOUT_PRODUCT_EXT=java\n", False)):
        clone, env, tmp, script = make_sandbox(with_old=True, pr=7, extra_env=ext)
        try:
            r = run_prep(script, env)
            label = "unset" if not ext else "java only"
            check("C %s rc=0" % label, r.returncode == 0, (r.stderr or "")[-400:])
            got = '"jar":null' not in r.stdout and "app.jar" in r.stdout
            check("C %s: a .py change %s" % (
                label, "needs a build" if want_artifact else "needs no build"),
                got == want_artifact, r.stdout)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    print("ALL HOLDOUT-NIGHTLY LINES TESTS PASSED")


if __name__ == "__main__":
    main()
