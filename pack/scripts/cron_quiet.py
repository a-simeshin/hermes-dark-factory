#!/usr/bin/env python3
"""Quiet channel for no-agent crons.

Policy: push a chat notification only for what needs the owner to act or
means a real stop (provider quota exhausted, gateway down, dispatcher idle
with a non-empty ready queue for N ticks in a row, a new card waiting for an
owner decision, a new PR ready for the owner's review). Everything else
(card recreations, park/unpark, auto-arms, worktree cleanup, label
recolouring, hygiene with no changes) goes into the evening report, not into
chat.

Mechanism: the stock cron --no-agent idiom "empty stdout = quiet tick"
(hermes cron create --help; the scheduler does not deliver empty output).
This module adds exactly one thing the gateway does not have: a daily digest
file where scripts drop routine events and which the evening report script
(metrics-evening) prints. Repeat suppression (once a day) stays in the
scripts' own state files.

Files: $HERMES_HOME/cron/digest/YYYY-MM-DD.md (local date), sections of the form
  ## <source> <HH:MM>
  <text>
"""
import hashlib
import json
import os
import time

HOME = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
# CRON_DIGEST_DIR: tests point the digest at a temp directory so they never
# write into the live one.
DIGEST_DIR = os.environ.get("CRON_DIGEST_DIR") or os.path.join(
    HOME, "cron", "digest")
_REPORTED = os.path.join(DIGEST_DIR, "reported.json")
_SECTION_MARK = "## "


def _day_file(now=None):
    stamp = time.localtime(now if now is not None else time.time())
    return os.path.join(
        DIGEST_DIR, time.strftime("%Y-%m-%d", stamp) + ".md")


def digest_add(source, text, now=None):
    """Append a routine-event section to the daily digest file.

    Call this ONLY for events that are not pushed; the calling script keeps
    the dedup in its state (the same finding is written here at most once a
    day). Events (a worktree removal, a label recolour) are written as is.
    """
    text = (text or "").strip()
    if not text:
        return
    stamp = time.localtime(now if now is not None else time.time())
    os.makedirs(DIGEST_DIR, exist_ok=True)
    with open(_day_file(now), "a", encoding="utf-8") as fh:
        fh.write("%s%s %s\n%s\n\n"
                 % (_SECTION_MARK, source,
                    time.strftime("%H:%M", stamp), text))


def _sections_of(path):
    try:
        raw = open(path, encoding="utf-8").read()
    except OSError:
        return []
    out, header, body = [], None, []
    for line in raw.splitlines():
        if line.startswith(_SECTION_MARK):
            if header is not None:
                out.append((header, "\n".join(body).strip()))
            header, body = line[len(_SECTION_MARK):].strip(), []
        elif header is not None:
            body.append(line)
    if header is not None:
        out.append((header, "\n".join(body).strip()))
    return [(h, b) for h, b in out if b]


def digest_report(now=None, max_age_s=26 * 3600):
    """Unreported sections of the last day, as one string for the report.

    Reads the digest files for today and yesterday, returns the sections whose
    signature (sha256 of the header without the time plus the body) is not yet
    marked in reported.json, and marks them: the evening report prints each
    section exactly once.
    """
    now = time.time() if now is None else now
    paths = sorted({_day_file(now), _day_file(now - max_age_s)})
    fresh = []
    for path in paths:
        for header, body in _sections_of(path):
            if os.path.getmtime(path) < now - max_age_s:
                continue
            name = header.split(" ", 1)[0] if " " in header else header
            sig = hashlib.sha256(
                (name + "\n" + body).encode("utf-8")).hexdigest()[:16]
            fresh.append((sig, name, header, body))
    try:
        reported = json.load(open(_REPORTED, encoding="utf-8"))
    except (OSError, ValueError):
        reported = {}
    unseen = [(n, h, b) for sig, n, h, b in fresh if sig not in reported]
    if not unseen:
        return ""
    for sig, _n, _h, _b in fresh:
        reported[sig] = int(now)
    # signatures older than two days are no longer needed
    horizon = int(now) - 2 * 24 * 3600
    reported = {k: v for k, v in reported.items() if v >= horizon}
    os.makedirs(DIGEST_DIR, exist_ok=True)
    json.dump(reported, open(_REPORTED, "w", encoding="utf-8"))
    parts = []
    for name, _header, body in unseen:
        parts.append("[%s]\n%s" % (name, body))
    return "\n\n".join(parts)
