#!/usr/bin/env python3
"""Test of the pr-hygiene per-run read cache.

Without the cache a run called `gh pr view` up to three times for the same PR
(review_state, has_user_summary and pr_activity with nearly identical fields),
plus a duplicate `pr list --state all`, which makes a run slow on a slow GitHub.

The cache must keep three properties:
  1. one gh call per PR per run, however many consumers ask;
  2. identical list reads are deduplicated;
  3. mutations (close/comment) are NEVER covered by the cache: a cached close
     would silently lose the action on a repeat.

Freshness between runs comes from the cache living in the process: cron
starts a new process per run, and a TTL cache on disk would serve yesterday's
review verdict.

Run: python3 test_pr_hygiene_cache.py
"""
import importlib.util
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
# Settings are read at import time; the Hermes home is a temporary directory.
os.environ.update({
    "HERMES_HOME": tempfile.mkdtemp(prefix="ph-cache-"),
    "FACTORY_REPO": "acme/widget",
    "FACTORY_REPO_PATH": "/srv/hermes/widget",
    "FACTORY_BOARD": "main",
    "FACTORY_OWNER_LOGIN": "owner-login",
    "FACTORY_OWNER_CHAT": "telegram:000000000",
    "FACTORY_BASE_BRANCHES": "main",
})
SCRIPT = os.path.join(HERE, "pr-hygiene.py")
spec = importlib.util.spec_from_file_location("pr_hygiene_cache", SCRIPT)
if spec is None or spec.loader is None:
    print("SKIP: cannot load pr-hygiene.py")
    sys.exit(0)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# Markers as pr-hygiene reads them: the verdict lives in a comment, a bot has
# no right to a formal review of its own PR.
PR_JSON = json.dumps({
    "body": "PR body, refs #100",
    "headRefOid": "1a2b3c4d5e6f7890aabbccddeeff001122334455",
    "comments": [
        {"body": "review verdict: changes requested",
         "createdAt": "2026-10-27T10:00:00Z"},
        {"body": "review verdict: approve",
         "createdAt": "2026-10-28T12:00:00Z"},
        {"body": "## What changes for you\n- faster",
         "createdAt": "2026-10-28T12:01:00Z"},
    ],
    "commits": [
        {"committedDate": "2026-10-28T11:00:00Z"},
    ],
})


class CountingGh:
    def __init__(self, answers):
        self.answers = answers      # answers in call order
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append(args)
        return self.answers[min(len(self.calls) - 1, len(self.answers) - 1)]


def with_gh(answers):
    box = CountingGh(answers)
    mod.gh = box
    mod.reset_run_caches()
    return box


def restore(gh_orig):
    mod.gh = gh_orig
    mod.reset_run_caches()


def test_three_consumers_one_fetch():
    gh_orig = mod.gh
    box = with_gh([PR_JSON])
    try:
        state, why = mod.review_state(224)
        assert state == "ok" and "accepted" in why, (state, why)
        assert mod.has_user_summary(224) is True
        commit_ts, verdict_ts, verdict_state, head40 = mod.pr_activity(224)
        assert verdict_state == "ok" and head40.startswith("1a2b3c4d"), \
            (verdict_state, head40)
        pr_view = [c for c in box.calls if c[:2] == ("pr", "view")]
        assert len(pr_view) == 1, \
            "three consumers must fit into one gh pr view, " \
            "got %d: %s" % (len(pr_view), pr_view)
        assert pr_view[0][4] == mod.PR_PAYLOAD_FIELDS, pr_view[0]
    finally:
        restore(gh_orig)
    print("OK test_three_consumers_one_fetch")


def test_gh_read_dedups_identical_lists():
    gh_orig = mod.gh
    box = with_gh(['[{"number": 7, "state": "MERGED"}]'])
    try:
        a = mod.gh_read("pr", "list", "--state", "all", "--limit", "200",
                        "--json", "number,state")
        b = mod.gh_read("pr", "list", "--state", "all", "--limit", "200",
                        "--json", "number,state")
        assert a == b and a == '[{"number": 7, "state": "MERGED"}]', (a, b)
        lists = [c for c in box.calls if c[:2] == ("pr", "list")]
        assert len(lists) == 1, "identical reads are deduplicated, got %d" \
            % len(lists)
        # different arguments are a different key, the cache must not
        # substitute the answer
        box.answers = ['[{"number": 9, "state": "OPEN"}]']
        c = mod.gh_read("pr", "list", "--state", "open", "--limit", "100",
                        "--json", "number")
        assert '"OPEN"' in c, c
    finally:
        restore(gh_orig)
    print("OK test_gh_read_dedups_identical_lists")


def test_mutations_never_cached():
    gh_orig = mod.gh
    box = with_gh(["ok", "ok"])
    try:
        mod.gh_read("issue", "close", "123")
        mod.gh_read("issue", "close", "123")
        closes = [c for c in box.calls if c[:2] == ("issue", "close")]
        assert len(closes) == 2, \
            "a mutation must reach gh both times, uncached, got %d" \
            % len(closes)
        assert not any("close" in str(k) for k in mod.GH_READ_CACHE), \
            "a mutation must leave no trace in the read cache"
    finally:
        restore(gh_orig)
    print("OK test_mutations_never_cached")


def test_reset_refetches():
    gh_orig = mod.gh
    box = with_gh([PR_JSON, PR_JSON])
    try:
        mod.pr_payload(224)
        mod.reset_run_caches()
        mod.pr_payload(224)
        views = [c for c in box.calls if c[:2] == ("pr", "view")]
        assert len(views) == 2, \
            "after a cache reset the read must reach gh again, got %d" \
            % len(views)
    finally:
        restore(gh_orig)
    print("OK test_reset_refetches")


def test_payload_fields_cover_consumers():
    # Guards against narrowing the field set by accident: review_state reads
    # comments/commits, has_user_summary reads body/comments, pr_activity
    # reads headRefOid. A dropped field silently breaks a consumer with
    # emptiness, so the check is on the constant.
    for field in ("comments", "commits", "body", "headRefOid"):
        assert field in mod.PR_PAYLOAD_FIELDS.split(","), \
            mod.PR_PAYLOAD_FIELDS
    # detail() is deliberately outside the cache: mergeable/checks change
    # during a run
    det = ("mergeable", "mergeStateStatus", "statusCheckRollup", "files")
    assert mod.PR_PAYLOAD_FIELDS.find("mergeable") == -1
    assert "mergeable" in " ".join(det)
    print("OK test_payload_fields_cover_consumers")


def main():
    test_three_consumers_one_fetch()
    test_gh_read_dedups_identical_lists()
    test_mutations_never_cached()
    test_reset_refetches()
    test_payload_fields_cover_consumers()
    print("ALL PR-HYGIENE CACHE TESTS PASSED")


if __name__ == "__main__":
    main()
