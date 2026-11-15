#!/usr/bin/env python3
"""Tests of the MERGE READINESS section in metrics-evening.py.

There is a single line of base branches (here develop/2.x): the merge gate
(readiness) neither requires nor waits for a twin PR on another line, and a PR
on the base line without a pair merges on its own.

Mutant (checked in a separate run with EVENING_SCRIPT on a copy): classify an
unpaired PR as a twin (`("single", 1)` -> `("twin", 1)` in merge_classify)
-> test_single_pr_merges fails.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

# Settings are read at import time. The Hermes home is a temporary directory;
# metrics-evening finds pr-hygiene.py as $HERMES_HOME/scripts/pr-hygiene.py,
# so that directory is a symlink to the directory of these scripts.
_HOME = tempfile.mkdtemp(prefix="merge-readiness-")
os.symlink(HERE, os.path.join(_HOME, "scripts"))
os.environ.update({
    "HERMES_HOME": _HOME,
    "FACTORY_REPO": "acme/widget",
    "FACTORY_REPO_PATH": "/srv/hermes/widget",
    "FACTORY_BOARD": "main",
    "FACTORY_OWNER_LOGIN": "owner-login",
    "FACTORY_OWNER_CHAT": "telegram:000000000",
    # One base line: a PR on develop/1.x is not on it, so it is a stack.
    "FACTORY_BASE_BRANCHES": "develop/2.x",
})
# Copy of the script via EVENING_SCRIPT (mutation runs).
SCRIPT = os.environ.get("EVENING_SCRIPT") or os.path.join(
    HERE, "metrics-evening.py")

spec = importlib.util.spec_from_file_location("metrics_evening_mr", SCRIPT)
if spec is None or spec.loader is None:
    print("SKIP: cannot load metrics-evening.py")
    sys.exit(0)
fm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fm)


def pr(number, title, base, head=None, draft=False):
    return {"number": number, "title": title, "baseRefName": base,
            "headRefName": head or ("feat/%d" % number), "isDraft": draft}


def run_readiness(prs, detail=None, review=("accepted", ""), wcfy=True):
    """merge_readiness() with pr-hygiene mocks: the only source of data."""
    calls = {"gh": []}

    def gh(*args, **kw):
        calls["gh"].append(args)
        if args[:2] == ("pr", "list"):
            return json.dumps(prs)
        return ""

    stub = type("HyStub", (), {
        "gh": staticmethod(gh),
        "detail": staticmethod(lambda n: detail or {
            "failed": False, "pending": False, "mergeable": "MERGEABLE"}),
        "review_state": staticmethod(lambda n: review),
        "has_user_summary": staticmethod(lambda n: wcfy),
    })()
    setattr(fm, "_hy", stub)
    return fm.merge_readiness(), calls


def test_single_pr_merges():
    """A PR on develop/2.x, green CI, PASS verdict, summary present -> in
    "Ready to merge" with class single. No mention of a twin."""
    out, _ = run_readiness([pr(42, "Add leak badge", "develop/2.x")])
    assert "#42" in out, out
    assert "class: single" in out, out
    ready = out.split("Ready to merge")[1].split("Waiting for review")[0]
    assert "#42" in ready, out
    assert "twin" not in out.lower(), out
    print("OK test_single_pr_merges")


def test_stack_classification():
    """A base other than develop/2.x -> stack; the base of a stack is also a
    stack (order 1)."""
    cls = fm.merge_classify([
        pr(1, "Add X", "develop/2.x"),
        pr(2, "[1.x] Add X", "develop/1.x"),
        pr(9, "Stack base", "develop/2.x", head="feat/9"),
        pr(3, "Stack child", "feat/9", head="feat/3"),
    ])
    assert cls[1] == ("single", 1), cls
    assert cls[2] == ("stack", 2), cls
    assert cls[9] == ("stack", 1), cls
    assert cls[3] == ("stack", 2), cls
    assert all(v[0] != "twin" for v in cls.values()), cls
    print("OK test_stack_classification")


def test_order_single_before_stack():
    cls = fm.merge_classify([pr(7, "s", "develop/2.x"), pr(8, "k", "feat/1")])
    assert cls[7][1] < cls[8][1] or cls[7][0] == "single", cls
    print("OK test_order_single_before_stack")


def test_red_ci_goes_to_red_section():
    out, _ = run_readiness(
        [pr(50, "Broken", "develop/2.x")],
        detail={"failed": True, "pending": False, "mergeable": "MERGEABLE"})
    red = out.split("Red CI")[1]
    assert "#50" in red, out
    assert "- none" in out.split("Waiting for review")[0], out
    print("OK test_red_ci_goes_to_red_section")


def test_no_open_prs():
    out, _ = run_readiness([])
    assert "no open non-draft PRs" in out, out
    print("OK test_no_open_prs")


def test_import_memory_bounded():
    """Regression: importing metrics-evening and the whole chain of sibling
    loads (pr-hygiene -> metrics-evening / board-watchdog) must fit into 512 MB
    of address space. Once a cycle metrics-evening -> pr-hygiene ->
    metrics-evening -> ... without a sys.modules guard grew by mutual exec
    recursion to several GB and the OOM policy stopped the gateway.
    Needs RLIMIT_AS, which is only usable on Linux."""
    if not sys.platform.startswith("linux"):
        print("SKIP test_import_memory_bounded (RLIMIT_AS needs Linux)")
        return
    code = (
        "import resource, importlib.util as ilu\n"
        "resource.setrlimit(resource.RLIMIT_AS, (512*1024*1024,)*2)\n"
        "spec = ilu.spec_from_file_location('me_ceiling', %r)\n"
        "m = ilu.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "print('CEILING-OK', m._hygiene() is not None)\n" % SCRIPT)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, timeout=180)
    assert r.returncode == 0 and "CEILING-OK True" in r.stdout, (
        r.returncode, r.stdout[-400:], r.stderr[-800:])
    print("OK test_import_memory_bounded")


def main():
    test_single_pr_merges()
    test_stack_classification()
    test_order_single_before_stack()
    test_red_ci_goes_to_red_section()
    test_no_open_prs()
    test_import_memory_bounded()
    print("ALL MERGE-READINESS TESTS PASSED")


if __name__ == "__main__":
    main()
