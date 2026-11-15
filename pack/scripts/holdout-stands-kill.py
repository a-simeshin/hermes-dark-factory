#!/usr/bin/env python3
# holdout-stands-kill.py: kill the stands of a holdout run, by the
# bounded-procs registry.
#
# Why: a holdout run once hit its iteration limit and left its stands
# listening; the TTL killed them eventually, but the rule "background is shut
# down by step N" had been broken. A rule in prose does not work: a mechanism
# has to kill the stands right after the target's scenarios, not a general
# loop at the end.
#
# Kills ONLY registry entries of the given owner (owner prefix, default
# cron:holdout-nightly). For each entry:
#   - pid is dead: the watchdog removes the entry itself, we do nothing (dead);
#   - pid is alive and the process cmdline ends with the recorded command:
#     it is our stand (the run-bounded wrapper execs timeout, which adds a
#     prefix to the cmdline, so the comparison is by suffix) -> TERM, then
#     KILL (killed);
#   - pid is alive but the cmdline does not match: the pid was reused by a
#     foreign process, do NOT touch it (suspicious).
# Foreign owners are not touched at all (untouched).
#
# Usage:
#   holdout-stands-kill.py [--owner-prefix cron:holdout-nightly] \
#       [--registry $HERMES_HOME/cache/bounded-procs.jsonl] [--dry-run]
# Always exits 0 except on argument errors: a safety-net cron must not fail
# because of one broken registry entry.

import argparse
import json
import os
import signal
import subprocess
import sys
import time

DEFAULT_REGISTRY = os.path.join(
    os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes"),
    "cache", "bounded-procs.jsonl")
DEFAULT_PREFIX = "cron:holdout-nightly"


def read_entries(path):
    entries = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get("pid") is not None:
                    entries.append(rec)
    except OSError:
        pass
    return entries


def pid_alive(pid):
    try:
        pid = int(pid)
        os.kill(pid, 0)
    except (ProcessLookupError, ValueError, TypeError):
        return False
    except PermissionError:
        return True  # another uid, but the process exists
    # a zombie (an unreaped child) is "alive" for kill(0) but is no longer a
    # stand: state Z in /proc/<pid>/stat means dead
    try:
        with open("/proc/%d/stat" % pid, encoding="utf-8") as fh:
            stat = fh.read()
        state = stat.rsplit(")", 1)[1].split()[0]
        return state != "Z"
    except (OSError, IndexError):
        return True


def cmdline_of(pid):
    try:
        with open("/proc/%s/cmdline" % pid, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    if not raw:
        return None
    return " ".join(raw.decode("utf-8", "replace").split("\x00")).strip()


def cmdline_matches(recorded_cmd, proc_cmdline):
    """Is the pid the same process? The recorded command is the run-bounded
    wrapper's $*; after exec timeout the cmdline gets a prefix
    (timeout --kill-after=30s 50m ...), so a suffix match is enough."""
    rec = " ".join((recorded_cmd or "").split())
    proc = " ".join((proc_cmdline or "").split())
    if not rec or not proc:
        return False
    return proc == rec or proc.endswith(" " + rec)


def kill_stand(pid):
    os.kill(pid, signal.SIGTERM)
    for _ in range(20):
        if not pid_alive(pid):
            return "TERM"
        time.sleep(0.1)
    os.kill(pid, signal.SIGKILL)
    time.sleep(0.2)
    return "KILL"


def sweep(entries, prefix, dry_run=False):
    """Returns (killed, dead, suspicious, untouched). Sorts entries by
    started: older first (background builds may legitimately live)."""
    mine = [e for e in entries
            if str(e.get("owner") or "").startswith(prefix)]
    untouched = len(entries) - len(mine)
    killed, dead, suspicious = [], [], []
    for rec in sorted(mine, key=lambda e: e.get("started") or 0):
        pid = rec.get("pid")
        if not pid_alive(pid):
            dead.append(pid)
            continue
        proc = cmdline_of(pid)
        if not cmdline_matches(rec.get("cmd"), proc):
            suspicious.append((pid, (proc or "")[:60]))
            continue
        if dry_run:
            killed.append((pid, "dry-run"))
            continue
        killed.append((pid, kill_stand(int(pid))))
    return killed, dead, suspicious, untouched


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--owner-prefix", default=DEFAULT_PREFIX)
    ap.add_argument("--registry", default=DEFAULT_REGISTRY)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    entries = read_entries(args.registry)
    killed, dead, suspicious, untouched = sweep(
        entries, args.owner_prefix, args.dry_run)
    # quiet watchdog pattern: empty stdout when everything is zero (a no-agent
    # cron delivers stdout verbatim, a clean run must not wake the owner)
    if not (killed or dead or suspicious):
        return 0
    for pid, how in killed:
        print("holdout-stands-kill: killed pid %s (%s)" % (pid, how))
    for pid in dead:
        print("holdout-stands-kill: pid %s already dead, the watchdog will"
              " clean the entry" % pid)
    for pid, proc in suspicious:
        print("holdout-stands-kill: pid %s alive but cmdline does not match"
              " (%s...): pid reused, NOT touched" % (pid, proc))
    print("holdout-stands-kill: owner prefix %s: killed %d, dead %d,"
          " suspicious %d, foreign entries untouched %d"
          % (args.owner_prefix, len(killed), len(dead), len(suspicious),
             untouched))
    return 0


if __name__ == "__main__":
    sys.exit(main())
