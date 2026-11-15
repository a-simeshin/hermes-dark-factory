#!/usr/bin/env python3
"""Verdict on the output of `hermes backup`: an ENOENT race is normal, anything else fails.

A nightly `hermes backup` takes several minutes and exits with rc=1 if a file
vanished from under the archiver in the meantime. Hourly cleanups (worktree
garbage collection and similar) start at the top of the hour and remove the
cron/output and cron/external-workers trees right under the archiver: one night
produced over a thousand "[Errno 2]" lines. The archive itself is intact
(`unzip -t` passes), but with `set -e` in hermes-backup.sh the zip rotation and
the removal of legacy tar files were skipped. The owner's rule:

  - a vanished file ([Errno 2] No such file or directory) = a normal race;
  - any other error ([Errno N!=2], Traceback, an unknown failure without a
    single ENOENT) = the run really failed.

Exit: 0 = the backup is good (rc was 0, or rc!=0 and every error is ENOENT),
1 = there are foreign errors (prints them and the log tail).
Usage: backup-verify.py <rc-of-hermes-backup> <path-to-log>
"""
import re
import sys

# [Errno 2] in a line = a file vanished under the archiver; other Errno values
# and Traceback are foreign errors.
ERRNO_RE = re.compile(r"\[Errno \d+\]")


def verdict(rc: int, text: str) -> tuple[bool, int, list]:
    """(good, n_enoent, offenders) from the command rc and the text of its log."""
    lines = (text or "").splitlines()
    if rc == 0:
        # rc==0 with [Errno 2] lines happens: skipped files are printed as a
        # warning and do not change the exit code
        n_enoent = sum(1 for l in lines if "[Errno 2]" in l)
        return True, n_enoent, []
    n_enoent, offenders = 0, []
    for line in lines:
        m = ERRNO_RE.search(line)
        if m and m.group(0) == "[Errno 2]":
            n_enoent += 1
        elif m or "Traceback (most recent call last)" in line:
            offenders.append(line.strip())
    # rc!=0 without a single ENOENT = an unknown failure; good only if every
    # error is explained by the race
    ok = n_enoent > 0 and not offenders
    return ok, n_enoent, offenders[:5]


def main(argv: list) -> int:
    if len(argv) != 3:
        print("usage: backup-verify.py <rc> <log>", file=sys.stderr)
        return 2
    rc = int(argv[1])
    try:
        text = open(argv[2], encoding="utf-8", errors="replace").read()
    except OSError as exc:
        print("FAIL: log is not readable: %s" % exc, file=sys.stderr)
        return 2
    ok, n_enoent, offenders = verdict(rc, text)
    if ok:
        print("OK: rc=%d, ENOENT races %d (normal), no foreign errors"
              % (rc, n_enoent))
        return 0
    print("FAIL: rc=%d, ENOENT races %d, foreign errors: %d"
          % (rc, n_enoent, len(offenders)))
    for line in offenders:
        print("  foreign: %s" % line[:200])
    print("--- log tail ---")
    for line in text.splitlines()[-20:]:
        print(line[:200])
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
