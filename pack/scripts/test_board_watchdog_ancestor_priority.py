#!/usr/bin/env python3
"""How the board carries a PR from decomposition to merge.

Checks:

  A. I7 ancestor priority: a live child p0 of a live parent p80 is raised
     to 80 with the marker comment "I7: ancestor priority"; a second pass
     is idempotent (no repeated edit or comment). On code without the
     function: AttributeError -> FAIL.
  B. Overlap map (pure functions): overlap_pairs finds an open PR touched
     by a merged one through shared files; merge_order_hint puts the PR
     with the most overlaps first.
  C. Pilot "one PR = one card": an open PR from a pilot branch (setting
     FACTORY_PILOT_EXEMPT_BRANCHES) with no verdict and no cards does NOT
     get an auto-armed reviewer-pr<N>-<head7> card
     (PILOT_EXEMPT_BRANCHES).

The harness is imported from test_board_watchdog_board_lint.py;
BOARD_WATCHDOG_SCRIPT overrides the script path, so red-green runs can
test an older copy of the script (expected FAIL on A and C).

Run: python3 test_board_watchdog_ancestor_priority.py
"""
import importlib.util
import json
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.join(HERE, "test_board_watchdog_board_lint.py")
spec = importlib.util.spec_from_file_location("wd_lint_harness", HARNESS)
if spec is None or spec.loader is None:
    print("SKIP: cannot load harness")
    sys.exit(0)
h = importlib.util.module_from_spec(spec)
sys.modules["wd_lint_harness"] = h
spec.loader.exec_module(h)
mod = h.mod

FAILS = []


def check(name, cond, detail=""):
    print("%s %s%s" % ("ok" if cond else "FAIL", name,
                       (" :: " + detail) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


# --- A. I7 ancestor priority ------------------------------------------------
check("A0 I7 function exists",
      hasattr(mod, "ancestor_priority_lint"),
      "ancestor_priority_lint is missing: code predates the rule")

if hasattr(mod, "ancestor_priority_lint"):
    slug, db, old_boards = h.make_board(
        tasks=[{"id": "t_root", "status": "running", "priority": 80,
                "title": "root PR cycle"},
               {"id": "t_kid", "status": "todo", "priority": 0,
                "title": "kid block 1"}],
        links=[("t_root", "t_kid")])
    calls = []
    real_run = mod.run
    mod.run = h.fake_run(db, calls) if callable(getattr(h, "fake_run", None)) \
        else real_run
    kids = {"t_root": ["t_kid"]}
    findings = mod.ancestor_priority_lint(slug, kids)
    mod.run = real_run
    con = sqlite3.connect(db)
    kid = con.execute("select priority from tasks where id='t_kid'").fetchone()
    marker = con.execute(
        "select count(*) from task_comments where task_id='t_kid' "
        "and body like '%I7: ancestor priority%'").fetchone()[0]
    con.close()
    mod.BOARDS = old_boards
    check("A1 child raised to the parent priority", kid[0] == 80,
          "child priority %r" % (kid,))
    check("A2 the I7 marker comment is present", marker == 1,
          "markers %r" % marker)
    check("A3 finding is reported", len(findings) == 1,
          "findings %r" % findings)
    # idempotency: a second pass over the same database
    con = sqlite3.connect(db)
    con.execute("update tasks set priority=0 where id='t_kid'")
    con.commit(); con.close()
    calls2 = []
    mod.run = h.fake_run(db, calls2)
    again = mod.ancestor_priority_lint(slug, kids)
    mod.run = real_run
    edits = [c for c in calls2 if len(c) > 5 and c[4] == "edit"]
    con = sqlite3.connect(db)
    kid2 = con.execute("select priority from tasks where id='t_kid'").fetchone()[0]
    con.close()
    mod.BOARDS = old_boards
    check("A4 a repeated pass does not edit (marker)", not edits and kid2 == 0,
          "edits=%r prio=%r" % (len(edits), kid2))

# --- B. Overlap map (pure functions) ---------------------------------
if hasattr(mod, "overlap_pairs"):
    opens = [(345, {"a.java", "b.java"}), (357, {"b.java"}),
             (351, {"c.java"})]
    pairs = mod.overlap_pairs(opens, (342, {"a.java", "b.java", "z.java"}))
    got = {(o, m) for o, m, _ in pairs}
    check("B1 touched PRs are found by shared files",
          got == {(345, 342), (357, 342)}, "got %r" % got)
    order = mod.merge_order_hint(opens, pairs)
    check("B2 merge order: the most overlapped first",
          order[0] == 345 and 357 in order[:2], "order %r" % order)
    check("B3 no overlap gives no pairs", 
          mod.overlap_pairs(opens, (999, {"q.java"})) == [])
else:
    check("B0 overlap map functions exist", False,
          "overlap_pairs is missing: code predates the rule")

# --- C. Pilot: auto-arm is not created on a pilot branch ------------------
check("C0 PILOT_EXEMPT_BRANCHES exists",
      hasattr(mod, "PILOT_EXEMPT_BRANCHES")
      and h.PILOT_BRANCH in mod.PILOT_EXEMPT_BRANCHES,
      "attribute is missing: code predates the rule")

if hasattr(mod, "PILOT_EXEMPT_BRANCHES") and hasattr(mod, "review_layer_check"):
    slug, db, old_boards = h.make_board(tasks=[])
    # board_repo -> a fictitious repository
    mod.board_repo = lambda s: "acme/widget"
    head = "ab12cd34ef56"
    listing = json.dumps([{"number": 244, "isDraft": False,
                           "headRefName": h.PILOT_BRANCH}])
    view = json.dumps({
        "headRefOid": head,
        "baseRefName": "develop/2.x",
        "commits": [{"committedDate":
                     time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                   time.gmtime(time.time() - 2 * 3600))}],
        "reviews": [], "comments": []})
    real_run = mod.run

    def _run(cmd, timeout=120):
        if cmd[0] == "gh" and "list" in cmd:
            return listing
        if cmd[0] == "gh" and "view" in cmd:
            return view
        return ""

    calls = []
    mod.run = lambda cmd, timeout=120: (calls.append(cmd), _run(cmd, timeout))[1]
    mod.review_layer_check(slug)
    mod.run = real_run
    mod.BOARDS = old_boards
    creates = [c for c in calls if len(c) > 4 and c[1] == "kanban"
               and "create" in c[:6]]
    check("C1 a pilot PR without cards is NOT armed", not creates,
          "create calls %r" % (creates[:2],))
    check("C2 the gh query for the pilot head was made (not a silent failure)",
          any(c[:2] == ["gh", "pr"] and "list" in c for c in calls))

print("\n%s" % ("ALL OK" if not FAILS else "FAILURES: %s" % ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
