#!/usr/bin/env python3
"""visual-delta-shot.py with --chrome-bin must start headless Chromium ONCE
per run and always stop it in finally, including when a shot fails. Origin:
five orphaned chrome-vd-* trees (9-10 processes each) lived for hours,
because new_tab started Chromium twice per run and neither kept nor stopped
the process.

A live run on a real binary: shoot about:blank on both sides (--no-push, no
network needed), then check that no process with user-data-dir
chrome-vd-<port> is left. A second run with a selector that does not exist:
the shot raises, and the browser must still be stopped.

The binary is FACTORY_CHROME_BIN (environment) or the first of chromium,
chromium-browser, google-chrome found on PATH. Without one, or without the
websockets module, the test skips with a message.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
SHOT = os.path.join(SCRIPTS, "visual-delta-shot.py")
PORT = 9346


def find_chrome():
    env = os.environ.get("FACTORY_CHROME_BIN")
    if env and os.path.exists(env):
        return env
    for name in ("chromium", "chromium-browser", "google-chrome"):
        path = shutil.which(name)
        if path:
            return path
    return None


def chrome_vd_procs(port):
    r = subprocess.run(
        ["pgrep", "-af", "user-data-dir=.*chrome-vd-%d\\b" % port],
        capture_output=True, text=True)
    out = []
    if r.stdout:
        for line in r.stdout.splitlines():
            if "chrom" in line.lower():
                out.append(line[:120])
    return out


def run_shot(chrome, shot_arg, home):
    env = dict(os.environ, HERMES_HOME=home)
    return subprocess.run(
        [sys.executable, SHOT, "--label", "t-vd-life",
         "--url-before", "about:blank", "--url-after", "about:blank",
         "--shot", shot_arg, "--cdp-port", str(PORT),
         "--chrome-bin", chrome, "--no-push"],
        capture_output=True, text=True, timeout=240, env=env)


def check(name, ok, detail=""):
    if not ok:
        print("FAIL %s %s" % (name, detail))
        sys.exit(1)
    print("OK " + name)


def main():
    chrome = find_chrome()
    if not chrome:
        print("SKIP test_visual_delta_chrome_lifecycle (no Chromium binary;"
              " set FACTORY_CHROME_BIN)")
        return
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP test_visual_delta_chrome_lifecycle (python module"
              " websockets is not installed)")
        return
    if chrome_vd_procs(PORT):
        print("SKIP test_visual_delta_chrome_lifecycle "
              "(port %d is taken by an old process)" % PORT)
        return
    home = tempfile.mkdtemp(prefix="vd-life-home-")

    # A: a clean run, both sides are shot
    r = run_shot(chrome, "full", home)
    check("A the run finished with rc=0", r.returncode == 0,
          (r.stderr or r.stdout)[-400:])
    check("A the Before/After table is printed",
          "## Before / After" in r.stdout, r.stdout[-200:])
    time.sleep(1)
    procs = chrome_vd_procs(PORT)
    check("A no chrome-vd-%d process after a clean run" % PORT,
          not procs, "\n".join(procs))

    # B: an exception inside the shot (selector not found), browser stopped
    r = run_shot(chrome, "nope=#no-such-element-ever", home)
    check("B the run with a broken selector failed (rc!=0)",
          r.returncode != 0, "expected a failure, rc=%d" % r.returncode)
    time.sleep(1)
    procs = chrome_vd_procs(PORT)
    check("B no chrome-vd-%d process after the failed run" % PORT,
          not procs, "\n".join(procs))

    print("ALL VISUAL-DELTA CHROME LIFECYCLE TESTS PASSED")


if __name__ == "__main__":
    main()
