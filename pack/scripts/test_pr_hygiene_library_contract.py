#!/usr/bin/env python3
"""Library contract in pr-hygiene (lib_contract_*).

A PR whose tests model the output of an external library must carry a
"Library contract: <artifact>:<version> <Class>#<method>" line in its body,
because a fixture written for the same hypothesis as the implementation is
always green.

Run: python3 test_pr_hygiene_library_contract.py
Mutants (PR_HYGIENE_SCRIPT=<copy of pr-hygiene.py with one edit>) must fail at
least one check:
  M1 the digit requirement in the version is dropped from LIBCONTRACT_RE
     (`Library contract: foo:bar Baz#qux` would pass);
  M2 the trigger is searched only in the PR body, the added test lines are
     not scanned.
"""
import importlib.util
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
# Settings are read at import time; the Hermes home is a temporary directory.
os.environ.update({
    "HERMES_HOME": tempfile.mkdtemp(prefix="ph-libcontract-"),
    "FACTORY_REPO": "acme/widget",
    "FACTORY_REPO_PATH": "/srv/hermes/widget",
    "FACTORY_BOARD": "main",
    "FACTORY_OWNER_LOGIN": "owner-login",
    "FACTORY_OWNER_CHAT": "telegram:000000000",
    "FACTORY_BASE_BRANCHES": "main",
})
SCRIPT = os.environ.get("PR_HYGIENE_SCRIPT") or os.path.join(
    HERE, "pr-hygiene.py")

spec = importlib.util.spec_from_file_location("pr_hygiene_libcontract", SCRIPT)
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


def main():
    print("pr-hygiene library contract")
    v = mod.lib_contract_verdict

    body291 = ("S1 EXACT (normalized equality against the upstream library "
               "two-line `top \\n rootCause` form")
    line_ok = ("Library contract: acme-lib-annotations:2.0.1 "
               "org.acme.lib.tool.AbstractToolCallback"
               "#createErrorResult")

    # 1. no sign = no requirement
    check("body without the sign = allowed",
          v("An ordinary PR body about a timeline", []) == ([], None))

    # 2. sign in the body without a contract line = violation
    bad, sign = v(body291, [])
    check("upstream library two-line sign in the body = violation",
          bad == [sign] and "library" in sign, "%r %r" % (bad, sign))

    # 3. a contract line lifts it
    check("a Library contract line lifts it",
          v(body291 + "\n\n" + line_ok, []) == ([], sign))

    # 4. a line without a digit in the version does not lift it (M1)
    check("a version without a digit does not lift it",
          len(v(body291 + "\nLibrary contract: foo:bar Baz#qux", [])[0]) == 1)

    # 5. a line without Class#method does not lift it
    check("a line without class#method does not lift it",
          len(v(body291 + "\nLibrary contract: lib:1.0", [])[0]) == 1)

    # 6. sign in the added test lines (M2)
    bad6, sign6 = v("clean body",
                    ["// The universal upstream shape (spec F1)"])
    check("sign in the test lines = violation",
          len(bad6) == 1 and "upstream shape" in sign6, "%r" % sign6)

    # 7. the library word comes after the form word
    check("form word first, then library word",
          len(v("the fixture repeats the output format of the library",
                [])[0]) == 1)

    # 8. a library mention without a form word = allowed (no false positive)
    check("upstream mention without a form word = allowed",
          v("updated the inspector to a fresh upstream version", [])
          == ([], None))

    # 9. test files
    tf = mod.is_test_file
    check("Maven src/test = test",
          tf("core/src/test/java/io/acme/widget/core/timeline/"
             "LeakDetectorTests.java"))
    check("Maven *IT.java = test",
          tf("demo-app/src/test/java/io/acme/widget/demo/proxy/"
             "LeakDetectorTimelineIT.java"))
    check("*.test.tsx = test", tf("ui/src/history.test.tsx"))
    check("production *.tsx = not a test", not tf("ui/src/Edit.tsx"))
    check("src/main/java = not a test",
          not tf("core/src/main/java/io/acme/widget/core/Foo.java"))

    # 10. aggregation: scan over PRs (git is replaced)
    prs = [
        {"number": 291, "baseRefName": "develop/2.x", "body": body291,
         "detail": {"files": ["core/src/test/java/"
                              "LeakDetectorTests.java"]}},
        {"number": 295, "baseRefName": "develop/2.x",
         "body": "plain refactoring",
         "detail": {"files": ["core/src/test/java/OtherTests.java"]}},
        {"number": 296, "baseRefName": "develop/2.x",
         "body": body291 + "\n" + line_ok,
         "detail": {"files": ["core/src/test/java/ThirdTests.java"]}},
    ]

    def fake_git(*args, **kwargs):
        if any("refs/prs/291" in a for a in args):
            return 0, ("@@ -1 +1,2 @@\n+ * The upstream error text is always "
                       "the two-line format")
        return 0, "@@ -1 +1,2 @@\n+ an ordinary line"

    setattr(mod, "git", fake_git)
    scan = mod.lib_contract_scan(prs)
    check("scan finds only #291", list(scan) == [291], "%r" % scan)

    print()
    if FAILED:
        print("FAIL: %d" % len(FAILED))
        sys.exit(1)
    print("ok: all library contract tests are green")


if __name__ == "__main__":
    main()
