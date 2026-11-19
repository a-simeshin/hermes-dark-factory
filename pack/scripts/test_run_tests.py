#!/usr/bin/env python3
"""Tests of run-tests.py on a temporary suite of dummy tests.

- the total line has the exact contract text "TEST TOTAL scripts/: N/M green";
- a red test is listed on the "RED:" line and the exit code is 1;
- a test that leaves a grandchild sleeping past the timeout: the timeout kills
  the whole process group, the grandchild does not survive;
- --audit marks a test that reads a file outside the allowed roots;
- a sandbox suite (TESTS_DIR overridden) never writes the green stamp.

Never runs the real suite: TESTS_DIR points to a temp directory, so there is
no self-spawn of this file.
"""
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
RUNNER = os.path.join(HERE, "run-tests.py")
FAILS = []


def check(name, cond, detail=""):
    print("%s %s %s" % ("PASS" if cond else "FAIL", name, detail))
    if not cond:
        FAILS.append(name)


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # a zombie still answers kill 0; ps tells the truth
    st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                        capture_output=True, text=True).stdout.strip()
    return bool(st) and not st.startswith("Z")


def run(tests_dir, *args, timeout_s="3"):
    env = dict(os.environ, TESTS_DIR=tests_dir, RUN_TESTS_TIMEOUT=timeout_s,
               HERMES_HOME=tempfile.mkdtemp(prefix="rt-home-"),
               SCRIPTS_GREEN_STAMP=os.path.join(tests_dir, "stamp.json"))
    p = subprocess.run([sys.executable, RUNNER] + list(args), env=env,
                       capture_output=True, text=True, timeout=60)
    return p.returncode, p.stdout


def main():
    d = tempfile.mkdtemp(prefix="rt-suite-")
    pidfile = os.path.join(d, "grandchild.pid")
    with open(os.path.join(d, "test_ok.py"), "w") as fh:
        fh.write("print('ok')\n")
    with open(os.path.join(d, "test_bad.py"), "w") as fh:
        fh.write("raise SystemExit(3)\n")
    with open(os.path.join(d, "test_hang.py"), "w") as fh:
        fh.write(
            "import subprocess, sys, time\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
            "open(%r, 'w').write(str(p.pid))\n"
            "time.sleep(120)\n" % pidfile)

    t0 = time.time()
    rc, out = run(d)
    check("red suite exits 1", rc == 1, "rc=%s" % rc)
    check("total line contract", "TEST TOTAL scripts/: 1/3 green" in out, out[-300:])
    check("red line names both", "RED: test_bad.py, test_hang.py" in out)
    check("timeout reported", "TIMEOUT 3s" in out or "rc=1" in out)
    check("runner bounded by timeout", time.time() - t0 < 30)
    gpid = int(open(pidfile).read()) if os.path.exists(pidfile) else None
    time.sleep(0.5)
    check("grandchild killed with the group", gpid is not None and not alive(gpid),
          "pid=%s" % gpid)

    d2 = tempfile.mkdtemp(prefix="rt-audit-")
    outside = os.path.expanduser("~/.rt-audit-probe-%d" % os.getpid())
    with open(os.path.join(d2, "test_leak.py"), "w") as fh:
        fh.write("try:\n    open(%r).read()\nexcept OSError:\n    pass\n" % outside)
    with open(os.path.join(d2, "test_clean.py"), "w") as fh:
        fh.write("import tempfile\nopen(tempfile.mktemp(), 'w').write('x')\n")
    rc, out = run(d2, "--audit")
    check("audit marks leak", rc == 1 and "NOT HERMETIC" in out
          and "FAIL test_leak.py" in out, out[-400:])
    check("audit passes clean", "PASS test_clean.py" in out)

    d3 = tempfile.mkdtemp(prefix="rt-green-")
    with open(os.path.join(d3, "test_ok.py"), "w") as fh:
        fh.write("pass\n")
    rc, out = run(d3, "--audit")
    check("green suite exits 0", rc == 0 and "1/1 green" in out, out)
    check("sandbox suite writes no stamp",
          not os.path.exists(os.path.join(d3, "stamp.json")))

    print("ALL OK" if not FAILS else "FAILED: %s" % ", ".join(FAILS))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
