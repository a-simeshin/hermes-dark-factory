#!/usr/bin/env python3
"""Digest of the contents of scripts/ for the drift watchdog.

The watchdog in hermes-health.py compares the digest of every top-level *.py
and *.sh in scripts/ with the stamp of the last green run of
run-tests.py --audit. An edit of a script that bypasses teach-snapshot.sh (a
cron job, a manual edit, a worker) changes the digest before a snapshot and is
caught by the next watchdog tick.

A separate module, not a copy of the function: run-tests.py and
hermes-health.py must compute the digest the same way; diverging algorithms
mean false alarms or a hole.
"""
import hashlib
import os


def scripts_digest(directory: str) -> str:
    """sha256 over the sorted list of (name, content) of *.py and *.sh."""
    h = hashlib.sha256()
    for name in sorted(os.listdir(directory)):
        if not name.endswith((".py", ".sh")):
            continue
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            continue
        h.update(name.encode("utf-8"))
        h.update(b"\0")
        with open(path, "rb") as fh:
            h.update(fh.read())
        h.update(b"\0")
    return h.hexdigest()
