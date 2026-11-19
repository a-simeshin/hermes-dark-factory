#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Milestone priority bands.

The single place of definition for board-watchdog.py and pr-hygiene.py.
Before it, the bands were constants in two places (the active milestone in
pr-hygiene and a {milestone: band} table in board-watchdog). On release day
the milestone closes, the next one keeps its lower band, the one after it
gets no band at all, and all of that without a single error in the logs.

Now the active and the next milestone are computed from GitHub on every run:
the OPEN milestones of the repository are taken; active = the open one with
the nearest due_on, on a tie or without due_on by version number; next = the
next open one by version number. Bands: active 80, next 40, other open ones
20. Closed milestones are not in the open list, so they get no band (.get()
returns None).

Fault tolerance: a gh failure reads the last successful result from disk
(CACHE_PATH, format {"repos": {repo: [milestones]}}); without it the bands
are empty and source="nodata". Consumers treat "no data" as "skip the
check", not as "nothing is admitted". The memo is per process: every cron
run is a new process, so "on every run" holds automatically.

Loading: scripts with a hyphen in their name load this file through the
hermes_sibling key (see the cycle guard in pr-hygiene.py); it can also be
imported directly, and the shared sys.modules key keeps a second copy from
drifting away from the first.
"""
import argparse
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from factory_config import cfg, hermes_home  # noqa: E402

ACTIVE_BAND = 80
NEXT_BAND = 40
OTHER_BAND = 20
CACHE_PATH = os.path.join(hermes_home(), "cache", "milestone-bands.json")

_VER_RE = re.compile(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?")
_MEMO = {}


def parse_version(title):
    """'2.4.0' -> (2, 4, 0); 'v2.4' -> (2, 4, 0). Unparseable -> None."""
    if not title:
        return None
    m = _VER_RE.search(str(title))
    if not m:
        return None
    return tuple(int(g or 0) for g in m.groups())


def gh_fetch(repo):
    """Open milestones of the repository through gh api. Failure -> None.

    An empty list is a valid answer (the repository has no open
    milestones), None means "could not find out".
    """
    try:
        done = subprocess.run(
            ["gh", "api",
             "repos/%s/milestones?state=open&per_page=100" % repo],
            capture_output=True, text=True, timeout=60)
        items = json.loads(done.stdout or "[]")
        return [{"title": m.get("title"), "due_on": m.get("due_on")}
                for m in items]
    except Exception:
        return None


def select(milestones):
    """(active, next) from the open milestones. A pure function.

    Active: the nearest due_on; without due_on or on a tie the lower
    version number (unparseable titles last, alphabetically).
    Next: the open one with the smallest version GREATER than the active
    one; if the active one has no version, the smallest version of the rest.
    """
    ms = [m for m in (milestones or []) if m.get("title")]
    if not ms:
        return None, None

    def key(m):
        v = parse_version(m["title"])
        return (m.get("due_on") is None, m.get("due_on") or "",
                v is None, v or (), m["title"])

    active = min(ms, key=key)
    av = parse_version(active["title"])
    rest = [m for m in ms if m is not active]
    by_ver = []
    for m in rest:
        v = parse_version(m["title"])
        if v is not None:
            by_ver.append((v, m["title"]))
    nxt = None
    if av is not None:
        bigger = [(v, t) for v, t in by_ver if v > av]
        if bigger:
            nxt = min(bigger)[1]
    elif by_ver:
        nxt = min(by_ver)[1]
    return active["title"], nxt


def bands_from(milestones):
    """Bands of the open milestones: active 80, next 40, others 20."""
    active, nxt = select(milestones)
    out = {}
    for m in (milestones or []):
        t = m.get("title")
        if not t:
            continue
        if t == active:
            out[t] = ACTIVE_BAND
        elif t == nxt:
            out[t] = NEXT_BAND
        else:
            out[t] = OTHER_BAND
    return out


def resolve(repo, fetch=None):
    """{active, next, bands, source} of the repository.

    fetch(repo) -> list of open milestones or None on failure; gh_fetch by
    default. source: "gh" (fresh data), "cache" (the last successful result
    from disk, gh unavailable), "nodata" (no data at all; bands are empty,
    checks are skipped).
    """
    if repo in _MEMO:
        return _MEMO[repo]
    fetch = fetch or gh_fetch
    ms = fetch(repo)
    if ms is None:
        saved_ms = _read_cache(repo)
        if saved_ms is not None:
            active, nxt = select(saved_ms)
            result = {"active": active, "next": nxt,
                      "bands": bands_from(saved_ms), "source": "cache"}
        else:
            result = {"active": None, "next": None, "bands": {},
                      "source": "nodata"}
    else:
        active, nxt = select(ms)
        result = {"active": active, "next": nxt,
                  "bands": bands_from(ms), "source": "gh"}
        if ms:
            _write_cache(repo, ms)
    _MEMO[repo] = result
    return result


def _read_cache(repo):
    try:
        with open(CACHE_PATH) as fh:
            saved = json.load(fh)
        ms = (saved.get("repos") or {}).get(repo)
        return ms if isinstance(ms, list) else None
    except Exception:
        return None


def _write_cache(repo, ms):
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        saved = {}
        try:
            with open(CACHE_PATH) as fh:
                saved = json.load(fh)
        except Exception:
            pass
        saved.setdefault("repos", {})[repo] = ms
        with open(CACHE_PATH, "w") as fh:
            json.dump(saved, fh, ensure_ascii=False)
    except Exception:
        pass


def band_of_ref(repo, number, is_pr=False, fetch=None, gh_exec=None):
    """Milestone band of an issue or a PR (the shared helper).

    For an issue: the band of its milestone. For a PR: max(bands of the
    milestones of the issues in Refs/Closes of the body + the PR's own
    milestone), the svc_priority semantics of board-watchdog. Returns an
    int band or None (no milestone / no data): the consumer must use its own
    default, the admission gate fixes it on its next run.

    Meant for every path that creates a card for an issue or PR (the GitHub
    webhook, the watchdog, pr-hygiene): the priority at create is the
    helper's output, not a constant. Origin: a webhook prompt set p80 on a
    card of an issue in the next milestone, and the card overtook the active
    band until the first hourly run of the gate.
    """
    gh_exec = gh_exec or subprocess.run
    fetch = fetch or gh_fetch

    def api(path):
        try:
            done = gh_exec(
                ["gh", "api", path], capture_output=True, text=True,
                timeout=60)
            return json.loads(done.stdout or "null")
        except Exception:
            return None

    def issue_band(n):
        it = api("repos/%s/issues/%s" % (repo, n))
        if not isinstance(it, dict):
            return None, None
        title = (it.get("milestone") or {}).get("title")
        # a PR is served by the same endpoint: pull_request in the answer
        return title, bool(it.get("pull_request"))

    res = resolve(repo, fetch=fetch)
    bands = res["bands"]
    if is_pr:
        band = None
        pr = api("repos/%s/pulls/%s" % (repo, number))
        if isinstance(pr, dict):
            title = (pr.get("milestone") or {}).get("title")
            b = bands.get(title) if title else None
            if b and (band is None or b > band):
                band = b
            for m in re.findall(r"(?:Refs|Closes)\s+#(\d+)",
                                pr.get("body") or "", re.I):
                t, _ = issue_band(m)
                b = bands.get(t) if t else None
                if b and (band is None or b > band):
                    band = b
        return band
    title, is_pr_resp = issue_band(number)
    if is_pr_resp:
        return band_of_ref(repo, number, is_pr=True, fetch=fetch,
                           gh_exec=gh_exec)
    return bands.get(title) if title else None


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Milestone priority band of an issue or PR (the shared "
                    "helper): prints the band number on stdout. No band or "
                    "a failure: prints nothing, exit code 3 (the consumer "
                    "uses its own default).")
    ap.add_argument("--repo", default=None,
                    help="owner/name; FACTORY_REPO by default")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--issue", type=int)
    g.add_argument("--pr", type=int)
    args = ap.parse_args(argv)
    band = band_of_ref(args.repo or cfg("FACTORY_REPO"), args.issue or args.pr,
                       is_pr=bool(args.pr))
    if band is None:
        return 3
    print(band)
    return 0


if __name__ == "__main__":
    sys.exit(main())
