#!/usr/bin/env python3
"""Tests for the GitHub link gate (gh-link-guard.py).

The case behind the gate: a typo in the owner (acmee instead of acme) must
be refused before sending; a correct link passes; a blob link whose path is
missing at the given SHA is refused; a failed check means fail closed.
Non-publishing commands and the owner's own hands (outside a Hermes session)
bypass the gate.

Mutant: GH_LINK_GUARD_SCRIPT=/tmp/mutant-link-guard.py python3 \
  test_gh_link_guard.py
(build the mutant with: sed -E 's/^([[:space:]]*)bad\\.append.*/\\1pass  # mutant/'
gh-link-guard.py; it disables the refusal on a broken link. The tests must
fail on it: a red run proves they check the refusal itself. A mutant of the
gh_api_ok transport is useless: the tests replace it with fake_check.)
Run: python3 test_gh_link_guard.py
"""
import importlib.util
import os
import sys

SCRIPT = os.environ.get("GH_LINK_GUARD_SCRIPT") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "gh-link-guard.py")

spec = importlib.util.spec_from_file_location("gh_link_guard", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print("ok   %s" % name)
    else:
        print("FAIL %s %s" % (name, detail))
        FAILS.append(name)


def fake_check(paths_ok, paths_404=()):
    """check(api): 200 for paths_ok, 404 for paths_404, GuardError otherwise."""
    def _check(api):
        if api in paths_404:
            return False
        if api in paths_ok:
            return True
        raise mod.GuardError("unexpected request: %s" % api)
    return _check


REPO_OK = "repos/acme/widget"
REPO_404 = "repos/acmee/widget"
SHA = "264ec3083ffa97290c44cf5e68bb0713cd51de89"
PATH_OK = "core/src/main/java/com/acme/widget/protocol/Revision.java"
CONTENTS_OK = "repos/acme/widget/contents/%s?ref=%s" % (PATH_OK, SHA)
TYPO_URL = "https://github.com/acmee/widget/blob/%s/%s#L59" % (SHA, PATH_OK)
GOOD_URL = "https://github.com/acme/widget/blob/%s/%s#L59-L80" % (SHA, PATH_OK)


# --- validate_text: the core rule -------------------------------------------
bad = mod.validate_text(TYPO_URL, check=fake_check([], [REPO_404]))
check("typo owner is rejected", bad and "404" in bad[0][1], str(bad))

bad = mod.validate_text(GOOD_URL, check=fake_check([REPO_OK, CONTENTS_OK]))
check("valid permalink passes", bad == [], str(bad))

bad = mod.validate_text(
    "https://github.com/acme/widget/blob/%s/no/such/File.java"
    % SHA,
    check=fake_check([REPO_OK],
                     ["repos/acme/widget/contents/no/such/File.java?ref=%s" % SHA]))
check("missing path at sha is rejected", bad and "404" in bad[0][1], str(bad))

bad = mod.validate_text(
    "https://github.com/acme/widget/commit/%s" % SHA,
    check=fake_check([REPO_OK],
                     ["repos/acme/widget/commits/%s" % SHA]))
check("unknown commit is rejected", bad and "404" in bad[0][1], str(bad))


def boom(api):
    raise mod.GuardError("network went away")


bad = mod.validate_text(GOOD_URL, check=boom)
check("api error fails closed", bad and "unavailable" in bad[0][1], str(bad))

calls = []


def no_calls(api):
    calls.append(api)
    return True


bad = mod.validate_text("text without any links at all", check=no_calls)
check("no links = no checks", bad == [] and calls == [], "calls=%s" % calls)

# markdown: a URL in parentheses must not swallow the closing parenthesis
m = mod.GITHUB_LINK_RE.search("[diff](%s)" % GOOD_URL)
check("markdown link parse", m and m.group(1) == "acme" and m.group(2) == "widget",
      m.groups() if m else None)
bad = mod.validate_text("see https://github.com/acme/widget.",
                        check=fake_check([REPO_OK]))
check("trailing dot stripped from repo", bad == [], str(bad))


# --- a comment anchor belongs to its own page --------------------------------
CMT_ID = "7001"
CMT_API = "repos/acme/widget/issues/comments/%s" % CMT_ID
PR_CMT_URL = "https://github.com/acme/widget/pull/20#issuecomment-%s" % CMT_ID
WRONG_PAGE_URL = "https://github.com/acme/widget/issues/10#issuecomment-%s" % CMT_ID


def fake_fetch(html):
    def _fetch(api):
        if api == CMT_API:
            return html
        raise mod.GuardError("unexpected comment request: %s" % api)
    return _fetch


# the comment lives in pull/20, the link points to issues/10: refused
bad = mod.validate_text("Verdict: %s" % WRONG_PAGE_URL,
                        check=fake_check([REPO_OK]), fetch_comment=fake_fetch(PR_CMT_URL))
check("anchor on wrong page is rejected", bad and "another page" in bad[0][1], str(bad))

# the link taken from the comment's own html_url passes
bad = mod.validate_text("Verdict: %s" % PR_CMT_URL,
                        check=fake_check([REPO_OK]), fetch_comment=fake_fetch(PR_CMT_URL))
check("anchor from html_url passes", bad == [], str(bad))

# GitHub redirects issues <-> pull: owner/repo/number match, page kind is not compared
bad = mod.validate_text("https://github.com/acme/widget/issues/20#issuecomment-%s" % CMT_ID,
                        check=fake_check([REPO_OK]), fetch_comment=fake_fetch(PR_CMT_URL))
check("issues/pull page kind is normalized", bad == [], str(bad))

# the comment does not exist: 404
bad = mod.validate_text(PR_CMT_URL, check=fake_check([REPO_OK]), fetch_comment=fake_fetch(None))
check("missing comment anchor is rejected", bad and "does not exist (404)" in bad[0][1], str(bad))

# the comment lookup fails: fail closed
bad = mod.validate_text(PR_CMT_URL, check=fake_check([REPO_OK]), fetch_comment=boom)
check("comment fetch error fails closed", bad and "unavailable" in bad[0][1], str(bad))

# typo owner in a comment link: 404 before the comment is requested
bad = mod.validate_text("https://github.com/acmee/widget/pull/20#issuecomment-%s" % CMT_ID,
                        check=fake_check([], [REPO_404]))
check("anchor link with typo owner is rejected", bad and "404" in bad[0][1], str(bad))


# --- command classification and text extraction ----------------------------
check("cli issue comment is publish", mod.is_publish_command(
    ["issue", "comment", "305", "--body", "x"]))
check("cli pr edit is publish", mod.is_publish_command(
    ["pr", "edit", "12", "--title", "t"]))
check("pr view is not publish", not mod.is_publish_command(
    ["pr", "view", "12"]))
check("api comments POST is publish", mod.is_publish_command(
    ["api", "repos/acme/widget/issues/305/comments",
     "-X", "POST", "-f", "body=x"]))
check("api comment PATCH is publish", mod.is_publish_command(
    ["api", "repos/acme/widget/issues/comments/6558796908",
     "-X", "PATCH", "--input", "/tmp/b.json"]))
check("api issue PATCH is publish", mod.is_publish_command(
    ["api", "repos/acme/widget/issues/305", "-X", "PATCH"]))
check("api list GET comments is publish-classified (body absent = allow later)",
    mod.is_publish_command(
        ["api", "repos/acme/widget/issues/305/comments"]))
check("api unrelated is not publish", not mod.is_publish_command(
    ["api", "repos/acme/widget/commits/main"]))

import tempfile, json
tf_txt = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
tf_txt.write("known file content")
tf_txt.close()
texts = mod.resolve_texts(mod.extract_texts(
    ["issue", "comment", "305", "--body", "b1", "--body-file", tf_txt.name]))
check("extract --body and --body-file", len(texts) == 2 and texts[0] == "b1" and texts[1] == "known file content",
      str(texts)[:120])
tf = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
json.dump({"body": "body from JSON", "title": "title"}, tf)
tf.close()
texts = mod.resolve_texts(mod.extract_texts(
    ["api", "repos/a/b/issues/1/comments", "-X", "POST", "--input", tf.name]))
check("extract --input JSON body+title",
      texts == ["body from JSON", "title"], str(texts))

texts = mod.resolve_texts(mod.extract_texts(
    ["api", "repos/a/b/issues/1/comments", "-F", "body=field F", "-f", "title=field f"]))
check("extract -F/-f body and title", texts == ["field F", "field f"], str(texts))

try:
    mod.resolve_texts(mod.extract_texts(
        ["api", "repos/a/b/issues/comments/1", "-X", "PATCH", "--input", "/nonexistent.json"]))
    check("unreadable input fails closed", False, "did not raise GuardError")
except mod.GuardError:
    check("unreadable input fails closed", True)
os.unlink(tf_txt.name)
os.unlink(tf.name)


# --- main(): the full path with a patched API -------------------------------
mod.in_hermes_session = lambda env=None: True
mod.gh_api_ok = fake_check([REPO_OK, CONTENTS_OK], [REPO_404])

rc = mod.main(["issue", "comment", "305", "--body", "look at %s" % TYPO_URL])
check("main blocks typo owner comment", rc == 1, rc)

rc = mod.main(["issue", "comment", "305", "--body", "look at %s" % GOOD_URL])
check("main allows valid comment", rc == 0, rc)

rc = mod.main(["api", "repos/acme/widget/issues/comments/6558796908",
               "-X", "PATCH", "-F", "body=%s" % TYPO_URL])
check("main blocks api PATCH with typo", rc == 1, rc)

rc = mod.main(["pr", "view", "301"])
check("main skips non-publish", rc == 0, rc)

mod.in_hermes_session = lambda env=None: False
rc = mod.main(["issue", "comment", "305", "--body", TYPO_URL])
check("outside hermes session guard is off", rc == 0, rc)
mod.in_hermes_session = lambda env=None: True

mod.gh_api_ok = boom
rc = mod.main(["issue", "comment", "305", "--body", GOOD_URL])
check("main fails closed on api error", rc == 1, rc)

# full main() path with a comment anchor on another page
mod.gh_api_ok = fake_check([REPO_OK])
mod.gh_comment_html_url = fake_fetch(PR_CMT_URL)
rc = mod.main(["pr", "comment", "20", "--body", "Verdict: %s" % WRONG_PAGE_URL])
check("main blocks anchor on wrong page", rc == 1, rc)
rc = mod.main(["pr", "comment", "20", "--body", "Verdict: %s" % PR_CMT_URL])
check("main allows anchor from html_url", rc == 0, rc)

print()
if FAILS:
    print("FAILED: %d" % len(FAILS))
    sys.exit(1)
print("All link gate checks are green")
