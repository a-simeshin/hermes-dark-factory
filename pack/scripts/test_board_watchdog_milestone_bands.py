#!/usr/bin/env python3
"""Milestone priority bands are computed from GitHub on every run.

Why: with static bands (an active milestone constant in pr-hygiene, a band
table in board-watchdog) a release day passes silently: the released
milestone closes, the next one stays on 40, the one after it gets no band at
all, without a single error in the logs.

Checks:

  A. The released milestone is closed (absent from the open list) and the
     open 2.4.0, 2.5.0 give bands 2.4.0 = 80 (active), 2.5.0 = 40 (next).
  B. Pure logic of milestone_bands.select/bands_from: the nearest due_on is
     active, without due_on by version, equal due_on by version, a third
     open milestone = 20, next = the smallest version above the active one.
  C. board-watchdog integration: ms_bands through a patched run() returns
     the same bands; gen_admission admits a PR on an issue of 2.4.0 and
     refuses a PR on an issue of 2.5.0 ("outside the active").
  D. pr-hygiene integration: visual_delta creates a card for an issue of
     2.4.0 and refuses one for an issue of 2.5.0 (the card would be below
     p50).

Run: python3 test_board_watchdog_milestone_bands.py
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import atexit

# Settings and Hermes home are fixed before the scripts are loaded.
_HOME = tempfile.mkdtemp(prefix="msb-home-")
atexit.register(shutil.rmtree, _HOME, ignore_errors=True)
os.environ["HERMES_HOME"] = _HOME
os.environ.update({
    "FACTORY_REPO": "acme/widget",
    "FACTORY_REPO_PATH": "/srv/hermes/widget",
    "FACTORY_BOARD": "main",
    "FACTORY_OWNER_LOGIN": "owner-login",
    "FACTORY_OWNER_CHAT": "telegram:000000000",
    "FACTORY_BASE_BRANCHES": "main",
})

HERE = os.path.dirname(os.path.abspath(__file__))
WD_SRC = os.environ.get("BOARD_WATCHDOG_SCRIPT") or os.path.join(
    HERE, "board-watchdog.py")
PH_SRC = os.environ.get("PR_HYGIENE_SCRIPT") or os.path.join(
    HERE, "pr-hygiene.py")

# Release day of 2.3.0: the milestone is closed, state=open does not list it.
OPEN_MILESTONES = [
    {"title": "2.4.0", "due_on": "2026-12-20T00:00:00Z"},
    {"title": "2.5.0", "due_on": "2027-01-15T00:00:00Z"},
]


def _load(path, key):
    spec = importlib.util.spec_from_file_location(key, path)
    if spec is None or spec.loader is None:
        raise AssertionError("cannot load %s" % path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


def _open_milestones(repo):
    return OPEN_MILESTONES


def part_a_bands(wd):
    """Bands from the live code with 2.4.0/2.5.0 open (2.3.0 closed)."""
    if hasattr(wd, "MILESTONE_PRIORITY_BANDS"):
        # old code: static bands, GitHub is not asked
        return dict(wd.MILESTONE_PRIORITY_BANDS)
    wd.run = lambda cmd, timeout=120: (
        json.dumps(OPEN_MILESTONES)
        if cmd[:2] == ["gh", "api"] else "[]")
    wd.board_repo = lambda slug: "test-owner/repo"
    res = wd.ms_bands(repo="test-owner/repo")
    assert res["source"] == "gh", res
    return res["bands"]


def main():
    fails = []

    def check(name, fn):
        try:
            fn()
            print("ok   %s" % name)
        except AssertionError as e:
            fails.append(name)
            print("FAIL %s: %s" % (name, e))

    # --- B: pure logic of milestone_bands ---------------------------------
    mb_path = os.path.join(HERE, "milestone_bands.py")
    if not os.path.exists(mb_path):
        def no_module():
            raise AssertionError("milestone_bands.py does not exist")
        check("B:milestone_bands module", no_module)
    else:
        mb = _load(mb_path, "msb_acceptance")

        def b_logic():
            a, n = mb.select(OPEN_MILESTONES)
            assert a == "2.4.0" and n == "2.5.0", (a, n)
            b = mb.bands_from(OPEN_MILESTONES)
            assert b == {"2.4.0": 80, "2.5.0": 40}, b
            # without due_on: the active one by version number
            a, n = mb.select([{"title": "2.5.0", "due_on": None},
                              {"title": "2.4.0", "due_on": None}])
            assert a == "2.4.0" and n == "2.5.0", (a, n)
            # equal due_on: the smaller version is active
            a, _ = mb.select([{"title": "2.5.0", "due_on": "2026-12-20T00:00:00Z"},
                              {"title": "2.4.0", "due_on": "2026-12-20T00:00:00Z"}])
            assert a == "2.4.0", a
            # only one has due_on: it is active even with a bigger version;
            # "next" = a version above the active one, none -> None
            a, n = mb.select([{"title": "2.4.0", "due_on": None},
                              {"title": "2.5.0", "due_on": "2026-12-01T00:00:00Z"}])
            assert a == "2.5.0" and n is None, (a, n)
            # a third open milestone = 20
            b = mb.bands_from([
                {"title": "2.4.0", "due_on": "2026-12-20T00:00:00Z"},
                {"title": "2.5.0", "due_on": "2027-01-15T00:00:00Z"},
                {"title": "2.6.0", "due_on": None}])
            assert b == {"2.4.0": 80, "2.5.0": 40, "2.6.0": 20}, b
            assert mb.bands_from(None) == {}
            assert mb.select([]) == (None, None)
        check("B:select/bands_from", b_logic)

    # --- A/C: board-watchdog ----------------------------------------------
    tmp = tempfile.mkdtemp(prefix="msb-")
    atexit.register(shutil.rmtree, tmp, ignore_errors=True)
    wd = _load(WD_SRC, "msb_wd")
    if hasattr(wd, "milestone_bands"):
        wd.milestone_bands.CACHE_PATH = os.path.join(tmp, "ms-cache.json")

    def a_case():
        bands = part_a_bands(wd)
        assert bands.get("2.4.0") == 80, \
            "2.4.0 must become active (80), bands: %s" % bands
        assert bands.get("2.5.0") == 40, \
            "2.5.0 must become next (40), bands: %s" % bands
        assert "2.3.0" not in bands, \
            "the closed 2.3.0 must have no band: %s" % bands
    check("A:2.4.0=80, 2.5.0=40 (2.3.0 closed)", a_case)

    def c_case():
        if hasattr(wd, "MILESTONE_PRIORITY_BANDS"):
            raise AssertionError("gen_admission on static bands: old code")
        # admission cache: PR 10 -> issue 5 (2.4.0), PR 11 -> issue 6 (2.5.0)
        cache = {"prs": [10, 11],
                 "issues": {"5": "2.4.0", "6": "2.5.0"},
                 "pr_issues": {"10": [5], "11": [6]},
                 "pr_milestones": {}}
        bdir = os.path.join(tmp, "board")
        os.makedirs(bdir, exist_ok=True)
        with open(os.path.join(bdir, "admission-approved.json"), "w") as fh:
            json.dump(cache, fh)
        wd.BOARDS = os.path.dirname(bdir)
        wd.board_repo = lambda slug: "test-owner/repo"
        wd.run = lambda cmd, timeout=120: (
            json.dumps(OPEN_MILESTONES)
            if cmd[:2] == ["gh", "api"] else "[]")
        ok, why = wd.gen_admission(os.path.basename(bdir), 10)
        assert ok, "a PR on an issue of the active 2.4.0 must pass: %s" % why
        ok, why = wd.gen_admission(os.path.basename(bdir), 11)
        assert not ok and "outside the active" in (why or ""), \
            "a PR on an issue of 2.5.0 must not pass: %r" % why
    check("C:gen_admission admits 2.4.0, refuses 2.5.0", c_case)

    # --- D: pr-hygiene -----------------------------------------------------
    ph = _load(PH_SRC, "msb_ph")
    if hasattr(ph, "milestone_bands"):
        ph.milestone_bands.CACHE_PATH = os.path.join(tmp, "ph-ms-cache.json")

    def d_case():
        if hasattr(ph, "_ms_open_milestones"):
            ph._ms_open_milestones = _open_milestones
        made = []
        ph.visual_delta_card = lambda kind, number, bad, url: \
            made.append(number) or "t_vd%d" % number
        issues = [
            {"number": 5, "title": "i5", "url": "u5",
             "labels": [{"name": "hermes-ui"}],
             "body": "text without a section", "milestone": {"title": "2.4.0"}},
            {"number": 6, "title": "i6", "url": "u6",
             "labels": [{"name": "hermes-ui"}],
             "body": "text without a section", "milestone": {"title": "2.5.0"}},
        ]
        _ok, lines, cards, _numbers = ph.visual_delta_check(
            issues, dry_run=False)
        assert made == [5], \
            "no card for the issue of 2.4.0 (active), created: %s" % made
        assert any("card not created" in ln and "2.5.0" in ln
                   and "2.4.0" in ln for ln in lines), lines
    check("D:visual-delta creates for 2.4.0, refuses 2.5.0", d_case)

    if fails:
        print("\nRED: %d/%d checks failed: %s" % (len(fails), 4, fails))
        sys.exit(1)
    print("\nOK: milestone bands come from GitHub")


if __name__ == "__main__":
    main()
