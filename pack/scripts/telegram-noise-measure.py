#!/usr/bin/env python3
"""Measure the pushes delivered to the chat in the last 24 hours.

Counts the deliveries of no-agent cron jobs: files in
$HERMES_HOME/cron/output/<id>/ from the last 24 h whose body (after the
header) is non-empty and not marked [SILENT] (an empty stdout and [SILENT] are
not delivered by the gateway). The per-job breakdown is matched against
`hermes cron list` (id = the directory prefix).

Goal after the quiet-cron change: no more than 10 pushes a day on an ordinary
day.
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from factory_config import hermes_home  # noqa: E402

HOME = hermes_home()
OUT = os.path.join(HOME, "cron", "output")
WINDOW = 24 * 3600


def job_names():
    """id -> (name, deliver) from `hermes cron list`."""
    try:
        raw = subprocess.run([os.path.expanduser("~/.local/bin/hermes"),
                              "cron", "list"], capture_output=True,
                             text=True, timeout=60).stdout
    except Exception:
        return {}
    info, cur = {}, None
    for line in raw.splitlines():
        s = line.strip()
        if len(s) >= 12 and s[:12].isalnum() and "[" in s:
            cur = s.split()[0]
        elif s.startswith("Name:") and cur:
            info.setdefault(cur, [s.split(":", 1)[1].strip(), ""])[0] = \
                s.split(":", 1)[1].strip()
        elif s.startswith("Deliver:") and cur:
            info[cur][1] = s.split(":", 1)[1].strip()
    return {k: tuple(v) for k, v in info.items()}


def main():
    now = time.time()
    names = job_names()
    rows, total = [], 0
    for job_id in sorted(os.listdir(OUT)) if os.path.isdir(OUT) else []:
        d = os.path.join(OUT, job_id)
        if not os.path.isdir(d):
            continue
        sent = 0
        for fn in os.listdir(d):
            if not fn.endswith(".md"):
                continue
            path = os.path.join(d, fn)
            if os.path.getmtime(path) < now - WINDOW:
                continue
            body = open(path, encoding="utf-8").read()
            if "[SILENT]" in body or "silent (empty output)" in body:
                continue
            # the delivery header, the body after the first empty line
            lines = body.splitlines()
            while lines and not lines[0].strip():
                lines.pop(0)
            if lines:
                lines.pop(0)
            if any(x.strip() for x in lines):
                sent += 1
        if sent:
            total += sent
            rows.append((sent, names.get(job_id, (job_id, "?")), job_id))
    print("PUSHES IN THE LAST 24 H (without [SILENT], target <= 10): %d" % total)
    for sent, (name, deliver), job_id in sorted(rows, reverse=True):
        print("  %3d  %s -> %s (%s)" % (sent, name[:44], deliver or "?",
                                        job_id))
    if total > 10:
        print("EXCEEDED: more than 10, see the per-job breakdown above.")


if __name__ == "__main__":
    main()
