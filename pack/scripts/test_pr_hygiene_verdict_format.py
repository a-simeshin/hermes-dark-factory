#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Verdicts in the "## Review verdict" + deciding line format.

The accepted-head guard in pr-hygiene.py opened a coder card on an already
accepted PR more than once: ACCEPT_MARKERS match legacy substrings
("verdict: pass"), while the real thread format is a heading without a
colon and the deciding line in the next paragraph:
    ## Review verdict
    **PASS** on head `8ff7e029` ...
review_state() returned none and the guard treated the head as not
accepted. Fix: verdict_kind() recognises the heading format, the legacy
substrings stay for older comments.

Older-format fixtures are taken from test_board_watchdog_qa_verdict_split.py
through ast: a CR with a non-bold deciding line and APPROVED on the heading
line.

Run: python3 test_pr_hygiene_verdict_format.py
"""
import ast
import importlib.util
import os
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
# Settings are read at import time; the Hermes home is a temporary directory.
os.environ.update({
    "HERMES_HOME": tempfile.mkdtemp(prefix="ph-verdict-"),
    "FACTORY_REPO": "acme/widget",
    "FACTORY_REPO_PATH": "/srv/hermes/widget",
    "FACTORY_BOARD": "main",
    "FACTORY_OWNER_LOGIN": "owner-login",
    "FACTORY_OWNER_CHAT": "telegram:000000000",
    "FACTORY_BASE_BRANCHES": "main",
})

spec = importlib.util.spec_from_file_location(
    "phvf", os.path.join(HERE, "pr-hygiene.py"))
ph = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ph)

FAILED = []


def check(name, got, want):
    if got == want:
        print("PASS " + name + " -> " + str(got))
    else:
        FAILED.append(name)
        print("FAIL " + name + ": got " + repr(got) + ", want " + repr(want))


def kind(body):
    if ph.qa_verdict_comment(body):
        return "skip"
    return ph.verdict_kind(body)


# The real published format, verbatim shape.
BOLD_PASS = ("## Review verdict\n\n**PASS** on head `8ff7e029` "
             "(6/6 green).")
check("bold PASS", kind(BOLD_PASS), "ok")

BOLD_CR = "## Review verdict\n\n**CHANGES REQUESTED** on head `abc1234`."
check("bold CR", kind(BOLD_CR), "rejected")

PLAIN_PASS = "## Review verdict\n\nPASS on head `abc1234`."
check("plain PASS", kind(PLAIN_PASS), "ok")

PLAIN_CR = "## Review verdict\n\nCHANGES REQUESTED"
check("plain CR", kind(PLAIN_CR), "rejected")

COLON_PASS = "## Review verdict: PASS\n\nbody"
check("colon PASS on heading line", kind(COLON_PASS), "ok")

COLON_CR = "## Review verdict: changes requested"
check("colon CR on heading line", kind(COLON_CR), "rejected")

check("QA colon form skipped", kind("## Review verdict (QA): PASS"), "skip")
check("QA new form skipped", kind("## QA verdict: PASS"), "skip")

LEGACY_PASS = "verdict: pass on head abc"
check("legacy accept substring", kind(LEGACY_PASS), "ok")
LEGACY_CR = "review verdict: changes requested"
check("legacy reject substring", kind(LEGACY_CR), "rejected")

NOISE = "Random comment mentioning PASS and verdicts in prose."
check("prose without heading", kind(NOISE), "")
WCFY = "## What changes for you\n\n- something changed"
check("WCFY is not a verdict", kind(WCFY), "")

# Older formats from the QA-split fixtures must stay recognised.
with open(os.path.join(HERE, "test_board_watchdog_qa_verdict_split.py")) as f:
    tree = ast.parse(f.read())
fixtures = {}
for node in ast.walk(tree):
    if isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id in (
                    "REVIEWER_CR_241_R3", "REVIEWER_PASS_335"):
                fixtures[t.id] = ast.literal_eval(node.value)

check("older CR fixture (plain CR)",
      kind(fixtures["REVIEWER_CR_241_R3"]), "rejected")
check("older APPROVED fixture (colon heading)",
      kind(fixtures["REVIEWER_PASS_335"]), "ok")

print()
if FAILED:
    print("FAILED: %d" % len(FAILED))
    raise SystemExit(1)
print("ALL VERDICT-FORMAT TESTS PASSED")
