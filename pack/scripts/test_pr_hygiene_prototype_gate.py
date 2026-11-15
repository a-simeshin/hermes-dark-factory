#!/usr/bin/env python3
"""Tests of prototype_verdict / prototype_check in pr-hygiene.

A PR that implements a feature/ux issue before the owner wrote "prototype
accepted" in the issue is a finding.

Run: python3 test_pr_hygiene_prototype_gate.py
Mutants (PR_HYGIENE_SCRIPT=<copy of pr-hygiene.py with one edit>) must fail at
least one check:
  P1 the feature/ux label filter is dropped (m-labels);
  P2 the Refs/Closes link check is dropped (m-refs);
  P3 the acceptance loop always fires: any set of comments lifts the gate
     (m-accept);
  P4 the comment author is not checked (m-owner);
  P5 the link is searched as any #N mention, without Refs/Closes.
"""
import importlib.util
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
# Settings are read at import time; the Hermes home is a temporary directory.
os.environ.update({
    "HERMES_HOME": tempfile.mkdtemp(prefix="ph-proto-"),
    "FACTORY_REPO": "acme/widget",
    "FACTORY_REPO_PATH": "/srv/hermes/widget",
    "FACTORY_BOARD": "main",
    "FACTORY_OWNER_LOGIN": "owner-login",
    "FACTORY_OWNER_CHAT": "telegram:000000000",
    "FACTORY_BASE_BRANCHES": "main",
    # Issue 235 is exempt from the gate by an owner decision (P7).
    "FACTORY_PROTOTYPE_EXEMPT_ISSUES": "235",
})
SCRIPT = os.environ.get("PR_HYGIENE_SCRIPT") or os.path.join(
    HERE, "pr-hygiene.py")

spec = importlib.util.spec_from_file_location("pr_hygiene_proto", SCRIPT)
if spec is None or spec.loader is None:
    print("SKIP: cannot load pr-hygiene.py")
    sys.exit(0)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

FAILED = []


def check(name, cond, detail=""):
    if cond:
        print("  ok %s" % name)
    else:
        FAILED.append(name)
        print("  FAIL %s %s" % (name, detail))


ISSUE_UX = {"number": 240, "labels": ["ux"], "comments": []}
OWNER_OK = [{"user": "owner-login",
             "body": "Prototype accepted, questions closed."}]
OWNER_LOWER = [{"user": "owner-login", "body": "prototype accepted"}]
OWNER_NO_WORDS = [{"user": "owner-login", "body": "I will look later"}]
STRANGER = [{"user": "some-bot", "body": "prototype accepted"}]


def main():
    print("pr-hygiene prototype gate")
    v = mod.prototype_verdict

    # 1. a PR for a feature/ux issue without acceptance = violation
    bad = v("Refs #240", ISSUE_UX)
    check("PR for a ux issue without comments = violation",
          len(bad) == 1 and "#240" in bad[0], str(bad))

    # 2. the owner's comment lifts it
    check("owner \"Prototype accepted\" = allowed",
          v("Refs #240", dict(ISSUE_UX, comments=OWNER_OK)) == [])
    check("lower-case \"prototype accepted\" = allowed",
          v("Refs #240", dict(ISSUE_UX, comments=OWNER_LOWER)) == [])

    # 3. a stranger or a comment without the words does not lift it (P4, P3)
    check("stranger's comment with the words = violation",
          len(v("Refs #240", dict(ISSUE_UX, comments=STRANGER))) == 1)
    check("owner without the keywords = violation",
          len(v("Refs #240", dict(ISSUE_UX, comments=OWNER_NO_WORDS))) == 1)

    # 4. an issue without feature/ux does not apply (P1)
    check("issue without feature/ux labels = allowed",
          v("Refs #300", {"number": 300, "labels": ["enhancement", "ui"],
                          "comments": []}) == [])

    # 5. a #N mention in prose is not a link (P5, P2)
    check("prose mention of #240 without Refs/Closes = allowed",
          v("saw #240 in the chat", ISSUE_UX) == [])
    check("PR unrelated to the issue = allowed",
          v("Closes #1\nRefs #2", ISSUE_UX) == [])

    # 6. prototype_check: aggregation over open PRs (gh_read is replaced)
    prs = [{"number": 900, "title": "keep-alive pings", "body": "Refs #240"},
           {"number": 901, "title": "other", "body": "Refs #300"}]
    issues = [{"number": 240, "labels": [{"name": "ux"}]},
              {"number": 300, "labels": [{"name": "enhancement"}]}]

    def fake_gh_read(*args, **kwargs):
        if "/issues/240/comments" in " ".join(args):
            return json.dumps([{"user": {"login": "owner-login"},
                                "body": "Prototype accepted"}])
        return "[]"

    setattr(mod, "gh_read", fake_gh_read)
    lines, numbers = mod.prototype_check(prs, issues)
    check("accepted prototype: no findings", lines == [] and numbers == [],
          "%s %s" % (lines, numbers))

    def fake_gh_read2(*args, **kwargs):
        return "[]"

    setattr(mod, "gh_read", fake_gh_read2)
    lines2, numbers2 = mod.prototype_check(prs, issues)
    check("prototype not accepted: one finding for PR #900",
          numbers2 == [900] and "PR #900 -> issue #240" in lines2[0]
          and len(lines2) == 1, "%s %s" % (lines2, numbers2))

    # 7b. a [ux]/[feature] title prefix without labels (P6)
    check("[feature] prefix without labels = violation",
          len(v("Refs #310", {"number": 310, "labels": [],
                              "title": "[feature] new export",
                              "comments": []})) == 1)
    check("title without a prefix and without labels = allowed",
          v("Refs #311", {"number": 311, "labels": [],
                          "title": "An ordinary title",
                          "comments": []}) == [])

    # 7bb. a bug with a [ux] prefix does not need a prototype (P8)
    check("bug with a [ux] prefix = allowed",
          v("Refs #320", {"number": 320, "labels": ["bug"],
                          "title": "[ux] doubled headers",
                          "comments": []}) == [])

    # 7c. an exempt issue: a PR that already implements it is not flagged (P7)
    check("exempt issue: a PR for it is not flagged",
          v("Refs #235", {"number": 235, "labels": ["ux"],
                          "title": "[ux] keep-alive pings",
                          "comments": []}) == [])

    print("TOTAL: %s" % ("OK" if not FAILED
                         else "FAIL %d: %s" % (len(FAILED), FAILED)))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
