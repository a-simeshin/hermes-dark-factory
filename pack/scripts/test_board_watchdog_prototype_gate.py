#!/usr/bin/env python3
"""Tests of the prototype-first gate in board-watchdog.py.

Run: python3 test_board_watchdog_prototype_gate.py
Mutants: a copy of board-watchdog.py with one edit, run through
BOARD_WATCHDOG_SCRIPT=/tmp/mutant.py python3 test_board_watchdog_prototype_gate.py
A mutant must fail at least one test:
  M1 the comment author is not checked (any "prototype accepted" counts);
  M2 the feature/ux label filter is dropped (fetch pulls every issue);
  M3 the unblock of the card after acceptance is dropped;
  M4 the words "prototype accepted" are not checked (any owner comment);
  M5 the hold is ignored (the gate acts without "accepted");
  M6 the todo/ready status check is dropped (blocks the correctly blocked);
  M7 the [ux]/[feature] title prefix is not recognised;
  M8 the exempt issue list is ignored;
  M9 a bug with a [ux] prefix is taken as a target.

The gate's actions (block/unblock/comment) are recorded by a monkeypatched
run(); the issue cache is replaced by a temporary file: neither the board
nor GitHub is touched.
"""
import atexit
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
_HOME = tempfile.mkdtemp(prefix="wd-home-")
atexit.register(shutil.rmtree, _HOME, True)
os.environ.update({
    "HERMES_HOME": _HOME,
    "FACTORY_REPO": "acme/widget",
    "FACTORY_REPO_PATH": "/srv/hermes/widget",
    "FACTORY_BOARD": "main",
    "FACTORY_OWNER_LOGIN": "owner-login",
    "FACTORY_OWNER_CHAT": "telegram:000000000",
    "FACTORY_BASE_BRANCHES": "develop/1.x,develop/2.x",
    # an issue whose implementation PR is already open (a prototype after the
    # implementation checks nothing)
    "FACTORY_PROTOTYPE_EXEMPT_ISSUES": "235",
})
SCRIPT = os.environ.get("BOARD_WATCHDOG_SCRIPT",
                        os.path.join(HERE, "board-watchdog.py"))
FAILED = []


def check(name, cond, detail=""):
    if cond:
        print("  ok %s" % name)
    else:
        FAILED.append(name)
        print("  FAIL %s %s" % (name, detail))


def load_mod():
    spec = importlib.util.spec_from_file_location("wd11_test", SCRIPT)
    assert spec is not None and spec.loader is not None, SCRIPT
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_run(recorder, gh_issues=None, gh_comments=None):
    """run(): gate actions go to the recorder, gh reads get stub answers."""
    gh_issues = gh_issues or []
    gh_comments = gh_comments or {}

    def fake_run(cmd, timeout=120):
        recorder.append(cmd)
        if cmd[:3] == ["gh", "issue", "list"]:
            return json.dumps(gh_issues)
        if "api" in cmd:
            for number, body in gh_comments.items():
                if "/issues/%d/" % number in " ".join(cmd):
                    return json.dumps(body)
            return "[]"
        return ""
    return fake_run


def kanban_actions(rec):
    return [c for c in rec if "kanban" in c]


ISSUE_240 = {"number": 240, "title": "[ux] keep-alive pings",
             "labels": [{"name": "ux"}], "body": "## Before / After"}
OWNER_ACCEPT = [{"user": {"login": "owner-login"},
                 "body": "Prototype accepted, questions closed."}]
STRANGER_ACCEPT = [{"user": {"login": "coder-bot"},
                    "body": "prototype accepted"}]
OWNER_OTHER = [{"user": {"login": "owner-login"},
                "body": "will look at it on the weekend"}]

CARD_TODO = {"id": "t_impl240", "status": "todo",
             "title": "Implementation #240",
             "body": "Issue: #240", "comments": []}
CARD_BLOCKED = {"id": "t_impl240b", "status": "blocked",
                "title": "Implementation #240",
                "body": "Issue: #240",
                "comments": ["Release condition: prototype accepted."]}


def main():
    print("prototype_gate (a)(b)")
    mod = load_mod()
    tmp = tempfile.mkdtemp(prefix="wd11-")
    hold = os.path.join(tmp, "hold")
    n_cache = [0]

    def wire(recorder, gh_issues=None, gh_comments=None):
        setattr(mod, "run", make_run(recorder, gh_issues, gh_comments))
        n_cache[0] += 1
        setattr(mod, "PROTOGATE_CACHE",
                os.path.join(tmp, "cache-%d.json" % n_cache[0]))

    # 1. Extraction of issue references
    refs = mod.protogate_issue_refs("Pair of #12", "Issue: #34\nsee #56")
    check("references from the title and the body", refs == {12, 34, 56},
          str(refs))

    # 2. Acceptance of the prototype: only the owner and only the right words
    check("owner + \"Prototype accepted\" = yes",
          mod.protogate_owner_accepted(OWNER_ACCEPT) is True)
    check("owner without the words = no (M4)",
          mod.protogate_owner_accepted(OWNER_OTHER) is False)
    check("a stranger's comment with the words = no (M1)",
          mod.protogate_owner_accepted(STRANGER_ACCEPT) is False)
    check("lowercase \"prototype accepted\" = yes",
          mod.protogate_owner_accepted(
              [{"user": {"login": "owner-login"},
                "body": "prototype accepted, go"}]) is True)
    check("no comments = no",
          mod.protogate_owner_accepted([]) is False)

    # 3. A prototype in the issue body: a Before / After section with two images
    body2 = ("## Before / After\n\n| Before | After |\n|---|---|\n"
             "| ![b](u1.png) | ![a](u2.png) |\n\ntext\n\n## Details\nx")
    check("a section with two images = prototype present",
          mod.protogate_proto_present(body2) is True)
    check("one image = no prototype",
          mod.protogate_proto_present("## Before / After\n![b](u1.png)")
          is False)
    check("mermaid = prototype present",
          mod.protogate_proto_present(
              "## Before / After\n```mermaid\nflowchart LR\n  A-->B\n```")
          is True)
    check("no section = no",
          mod.protogate_proto_present("## Current\nold body") is False)

    # 4. (a) a todo card on a not accepted prototype is blocked
    rec = []
    wire(rec)
    issues = {"240": {"number": 240, "labels": ["ux"], "accepted": False,
                      "title": "t", "body": ""}}
    out = mod.prototype_gate("main", apply=True, issues=issues,
                             cards=[CARD_TODO])
    blocks = [c for c in kanban_actions(rec) if "block" in c]
    check("(a) block with kind needs_input",
          any("--kind" in c and "needs_input" in c for c in blocks), str(rec))
    check("(a) the reason carries the condition \"prototype accepted\"",
          any("prototype accepted" in " ".join(c) for c in blocks), str(rec))

    # 5. (a) an issue without feature/ux labels does not pull a card into a
    #    block: the label filter lives in fetch and is repeated in the gate
    rec2 = []
    wire(rec2, gh_issues=[
        {"number": 300, "title": "chore", "labels": [{"name": "enhancement"}],
         "body": "old body"}])
    fetched = mod.protogate_fetch_issues(now=time.time())
    check("fetch: an issue without feature/ux is filtered out (M2)",
          fetched == {}, str(fetched))
    out2 = mod.prototype_gate("main", apply=True,
                              issues={"300": {"number": 300,
                                              "labels": ["enhancement"],
                                              "accepted": False,
                                              "title": "t", "body": ""}},
                              cards=[{"id": "t_300", "status": "todo",
                                      "title": "x #300", "body": "",
                                      "comments": []}])
    check("a card on an enhancement issue is not blocked",
          out2 == [] and not kanban_actions(rec2), "%s %s" % (out2, rec2))

    # 6. (b) an accepted prototype lifts the card
    rec3 = []
    wire(rec3)
    issues_acc = {"240": {"number": 240, "labels": ["ux"], "accepted": True,
                          "title": "t", "body": ""}}
    mod.prototype_gate("main", apply=True,
                       issues=issues_acc, cards=[CARD_BLOCKED])
    check("(b) unblock after acceptance (M3)",
          any("unblock" in c for c in kanban_actions(rec3)), str(rec3))
    check("(b) the comment names the develop branch",
          any("develop" in " ".join(c) for c in kanban_actions(rec3)),
          str(rec3))

    # 7. (b) a stranger's "prototype accepted" does not lift the card: the
    #    check goes through the whole fetch (a gh stub with a foreign comment)
    rec4 = []
    wire(rec4, gh_issues=[ISSUE_240], gh_comments={240: STRANGER_ACCEPT})
    fetched4 = mod.protogate_fetch_issues(now=time.time())
    check("fetch: a foreign comment does not make accepted (M1)",
          fetched4.get("240", {}).get("accepted") is False, str(fetched4))
    out4 = mod.prototype_gate("main", apply=True,
                              issues=fetched4, cards=[CARD_BLOCKED])
    check("a foreign comment: the card stays in blocked",
          not any("unblock" in c for c in kanban_actions(rec4)), str(rec4))
    check("a foreign comment: a \"held correctly\" finding",
          any(k.startswith("protogate-held") for k, _, _ in out4), str(out4))

    # 8. Idempotency: a blocked card with the condition and no acceptance is
    #    left alone (M6)
    rec5 = []
    wire(rec5)
    out5 = mod.prototype_gate("main", apply=True, issues=issues,
                              cards=[CARD_BLOCKED])
    check("blocked without acceptance: no actions",
          kanban_actions(rec5) == [] and
          any(k.startswith("protogate-held") for k, _, _ in out5),
          "%s %s" % (rec5, out5))

    # 9. The hold: without "accepted" the gate only reports (M5)
    open(hold, "w").close()
    rec6 = []
    wire(rec6)
    out6 = mod.prototype_gate("main", issues=issues,
                              cards=[CARD_TODO], hold_path=hold)
    check("hold: no actions", kanban_actions(rec6) == [], str(rec6))
    check("hold: a finding about suppressed actions",
          any(k == "protogate-hold:1" for k, _, _ in out6), str(out6))

    # 10b. a [ux]/[feature] prefix without labels is also a feature (M7)
    rec7 = []
    wire(rec7, gh_issues=[
        {"number": 310, "title": "[UX] new timeline",
         "labels": [], "body": "old body"}])
    fetched7 = mod.protogate_fetch_issues(now=time.time())
    check("fetch: a [ux] prefix without labels is recognised (M7)",
          "310" in fetched7, str(fetched7))
    out7 = mod.prototype_gate("main", apply=True,
                              issues=fetched7,
                              cards=[{"id": "t_310", "status": "todo",
                                      "title": "Implementation #310",
                                      "body": "", "comments": []}])
    check("a card on a [ux] issue without labels is blocked",
          any(k.startswith("protogate-block") for k, _, _ in out7)
          and any("block" in c for c in kanban_actions(rec7)),
          "%s %s" % (out7, rec7))

    # 10bb. a bug with a [ux] prefix is not a target (M9)
    rec7b = []
    wire(rec7b, gh_issues=[
        {"number": 320, "title": "[ux] doubled headers",
         "labels": [{"name": "bug"}], "body": "old body"}])
    fetched7b = mod.protogate_fetch_issues(now=time.time())
    check("fetch: a bug with a [ux] prefix is not a target (M9)",
          fetched7b == {}, str(fetched7b))

    # 10c. #235 is exempt, its implementation PR is already open (M8)
    rec8 = []
    wire(rec8, gh_issues=[
        {"number": 235, "title": "[ux] keep-alive pings",
         "labels": [{"name": "ux"}], "body": "## Before / After"}])
    fetched8 = mod.protogate_fetch_issues(now=time.time())
    check("fetch: #235 is skipped (a PR already implements it)",
          fetched8 == {}, str(fetched8))
    out8 = mod.prototype_gate("main", apply=True,
                              issues={"235": {"number": 235, "labels": ["ux"],
                                              "title": "[ux] keep-alive",
                                              "accepted": False,
                                              "body": ""}},
                              cards=[{"id": "t_235", "status": "todo",
                                      "title": "Implementation #235",
                                      "body": "", "comments": []}])
    check("gate: #235 is exempt, the card is not blocked",
          out8 == [] and not kanban_actions(rec8), "%s %s" % (out8, rec8))

    # 10. The gate works only on the configured board
    check("another board is ignored",
          mod.prototype_gate("product", apply=True, issues=issues,
                             cards=[CARD_TODO]) == [])

    print("TOTAL: %s" % ("OK" if not FAILED
                         else "FAIL %d: %s" % (len(FAILED), FAILED)))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
