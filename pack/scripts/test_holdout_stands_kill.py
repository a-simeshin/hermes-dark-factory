#!/usr/bin/env python3
# test_holdout_stands_kill.py: tests for killing holdout stands. Live
# processes: our owner is killed, a foreign owner is not touched, a dead pid
# is skipped, a reused pid (cmdline does not match) is not touched.
# Mutant: owner filter removed -> the foreign one is killed -> red.
# Linux only: the script reads /proc and run-bounded.sh needs GNU timeout.

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "holdout-stands-kill.py")
RUN_BOUNDED = os.path.join(HERE, "run-bounded.sh")
PREFIX = "cron:holdout-nightly"


def alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        with open("/proc/%d/stat" % pid, encoding="utf-8") as fh:
            stat = fh.read()
        return stat.rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


def wait_dead(pid, timeout=8):
    for _ in range(int(timeout / 0.1)):
        if not alive(pid):
            return True
        time.sleep(0.1)
    return False


def test_owner_filter_and_reuse():
    mine = subprocess.Popen(["sleep", "300"])
    other = subprocess.Popen(["sleep", "300"])
    reused = subprocess.Popen(["sleep", "300"])
    corpse = subprocess.Popen(["sleep", "1"])
    time.sleep(1.5)  # the corpse dies by itself
    tmp = tempfile.mkdtemp()
    reg = os.path.join(tmp, "bounded-procs.jsonl")
    now = int(time.time())
    with open(reg, "w", encoding="utf-8") as fh:
        for pid, owner, cmd in (
                (mine.pid, PREFIX + ":pr1", "sleep 300"),
                (other.pid, "session:local-abc123", "sleep 300"),
                (reused.pid, PREFIX + ":pr1", "java -jar stand.jar"),
                (corpse.pid, PREFIX + ":pr1", "sleep 1")):
            fh.write(json.dumps({"pid": pid, "owner": owner,
                                 "started": now - 60, "deadline": now + 600,
                                 "cmd": cmd}) + "\n")
    try:
        out = subprocess.run(
            [sys.executable, SCRIPT, "--registry", reg],
            capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, (out.returncode, out.stdout, out.stderr)
        txt = out.stdout
        assert wait_dead(mine.pid), "our stand not killed: %s" % txt
        assert alive(other.pid), "foreign owner touched: %s" % txt
        assert alive(reused.pid), "reused pid touched: %s" % txt
        assert not alive(corpse.pid), "corpse must be dead: %s" % txt
        assert "killed pid %s (TERM" % mine.pid in txt, txt
        assert "already dead" in txt and str(corpse.pid) in txt, txt
        assert "cmdline does not match" in txt and str(reused.pid) in txt, txt
        assert "foreign entries untouched 1" in txt, txt
        assert "killed 1, dead 1, suspicious 1" in txt, txt
    finally:
        for p in (mine, other, reused):
            if p.poll() is None:
                p.kill()
                p.wait()
        shutil.rmtree(tmp, ignore_errors=True)
    print("OK test_owner_filter_and_reuse")


def test_dry_run_kills_nothing():
    mine = subprocess.Popen(["sleep", "300"])
    tmp = tempfile.mkdtemp()
    reg = os.path.join(tmp, "bounded-procs.jsonl")
    with open(reg, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"pid": mine.pid, "owner": PREFIX,
                             "started": 1, "deadline": 2,
                             "cmd": "sleep 300"}) + "\n")
    try:
        out = subprocess.run(
            [sys.executable, SCRIPT, "--registry", reg, "--dry-run"],
            capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, (out.stdout, out.stderr)
        assert alive(mine.pid), "dry-run killed the process: %s" % out.stdout
        assert "killed 1" in out.stdout and "dry-run" in out.stdout, \
            out.stdout
    finally:
        mine.kill()
        mine.wait()
        shutil.rmtree(tmp, ignore_errors=True)
    print("OK test_dry_run_kills_nothing")


def test_timeout_wrapper_suffix_match():
    """A stand started through run-bounded.sh (exec timeout ...): the process
    cmdline starts with timeout, the recorded command is a suffix."""
    tmp = tempfile.mkdtemp()
    reg = os.path.join(tmp, "bounded-procs.jsonl")
    wrap = subprocess.Popen(
        [RUN_BOUNDED, "--registry", reg,
         "--owner", PREFIX + ":test", "--ttl-min", "5", "--",
         "sleep", "300"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # the wrapper execs timeout: the pid stays with timeout, the child is sleep
    time.sleep(1.0)
    # the wrapper writes the registry entry itself; read its last line
    rec = None
    for _ in range(20):
        time.sleep(0.5)
        if os.path.exists(reg):
            lines = [l for l in open(reg, encoding="utf-8")
                     if l.strip()]
            for line in reversed(lines):
                r = json.loads(line)
                if r.get("owner") == PREFIX + ":test":
                    rec = r
                    break
        if rec:
            break
    assert rec is not None, "run-bounded did not write the registry"
    try:
        out = subprocess.run(
            [sys.executable, SCRIPT, "--registry", reg],
            capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, (out.stdout, out.stderr)
        assert wait_dead(wrap.pid), "wrapper/stand not killed: %s" % out.stdout
        assert "killed 1" in out.stdout, out.stdout
    finally:
        if wrap.poll() is None:
            wrap.kill()
            wrap.wait()
        shutil.rmtree(tmp, ignore_errors=True)
    print("OK test_timeout_wrapper_suffix_match")


def main():
    test_owner_filter_and_reuse()
    test_dry_run_kills_nothing()
    test_timeout_wrapper_suffix_match()
    print("ALL HOLDOUT-STANDS-KILL TESTS PASSED")


if __name__ == "__main__":
    main()
