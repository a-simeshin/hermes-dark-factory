#!/usr/bin/env python3
"""Tests for the reviewer-misses watchdog.

Pure functions on synthetic data that mirrors live GitHub bodies: headings
"## Review verdict", shas of full length and as prefixes, formal owner
reviews with state CHANGES_REQUESTED. gh is never called, and the real
Hermes home is never read: HERMES_HOME points to a temp directory.

Mutants (must fail):
- M1 a script copy where `if cr_sha in commits:` is replaced with
  `if False:` (the "next sha" window is cut out, only the same sha stays)
  -> test_detect_next_sha_miss fails;
- M2 a script copy where `if key not in have:` is replaced with
  `if True:` (the merge_journal dedupe is cut out)
  -> test_render_and_journal_dedupe fails.
"""
import importlib.util
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.environ.get("REVIEWER_MISSES_SCRIPT") or os.path.join(
    HERE, "reviewer-misses.py")

# The script reads its configuration at import time.
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="rm-home-")
os.environ["FACTORY_REPO"] = "acme/widget"
os.environ["FACTORY_OWNER_LOGIN"] = "owner-login"
for _key in ("FACTORY_VERDICT_PASS_WORDS", "FACTORY_VERDICT_RETURN_WORDS",
             "FACTORY_OWNER_ECHO_PREFIX"):
    os.environ.pop(_key, None)
sys.path.insert(0, HERE)

spec = importlib.util.spec_from_file_location("reviewer_misses", SCRIPT)
if spec is None or spec.loader is None:
    print("SKIP: cannot load reviewer-misses.py")
    sys.exit(0)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

OWNER = "owner-login"
BOT = "factory-app[bot]"

F1729 = "f17297303cfc88d445c8bf5bb31ff6afc9870242"   # a PASS and a CR on one sha
B7390 = "b73906765dece6c070d5fba5c08de0654f5fb5bd"   # a prefix-resolved verdict
COMMITS = ["%040x" % i for i in (1, 2)] + [F1729, B7390]


def comment(body, ts, cid, user=BOT):
    return {"user": {"login": user}, "body": body, "created_at": ts,
            "id": cid}


def review(state, ts, sha, rid, body="", user=OWNER):
    return {"user": {"login": user}, "state": state, "submitted_at": ts,
            "commit_id": sha, "id": rid, "body": body}


# ------------------------------------------------- parsing live shapes
def test_parse_verdict_live_shapes():
    kind, sha = mod.parse_verdict(
        "## Review verdict  PASS on exact head `%s`.\n  Incremental "
        "re-review of the owner blockers." % F1729)
    assert kind == "pass" and sha == F1729, (kind, sha)
    kind, sha = mod.parse_verdict(
        "## Review verdict **PASS** on exact head `%s` (attempt 3, single "
        "open item, previous attempt CHANGES REQUESTED resolved)" % F1729)
    assert kind == "pass", kind
    kind, sha = mod.parse_verdict(
        "## Review verdict  VERDICT: CHANGES REQUESTED  Incremental "
        "re-review of the exact PR head `%s`." % F1729)
    assert kind == "return", kind
    kind, sha = mod.parse_verdict(
        "## Review verdict **CHANGES REQUESTED (normal)** - exact head "
        "2d796fe, re-review after the owner's CHANGES REQUESTED")
    assert kind == "return", kind
    kind, sha = mod.parse_verdict(
        "## Review verdict APPROVED: normal (reviewer, 2026-10-25, "
        "head b7390676)")
    assert kind == "pass" and sha == "b7390676", (kind, sha)
    print("OK test_parse_verdict_live_shapes")


def test_bookkeeping_and_owner_comments_not_reviewer_verdict():
    # a bot bookkeeping comment without a #-heading is not a verdict
    kind, sha = mod.parse_verdict(
        "Review verdict logged (CHANGES REQUESTED: minor, head b739067). "
        "Findings triaged into card t_00000002 (coder)")
    assert kind is None, kind
    # a QA verdict is a different heading
    kind, _ = mod.parse_verdict("## QA verdict  PASS on head `%s`" % F1729)
    assert kind is None, kind
    print("OK test_bookkeeping_and_owner_comments_not_reviewer_verdict")


# ------------------------------------------------------------- pair detection
def test_detect_same_sha_miss():
    # a PASS on f172973, then the owner's major CR on the same sha later
    verdicts = [("2026-09-14T00:19:21Z", F1729, "pass", 5657332218)]
    crs = [("2026-10-24T20:45:43Z", F1729, 5639932667,
            "## Review verdict CHANGES REQUESTED: major (owner, "
            "2026-10-24, head f172973)\n- [BLOCK] persistentHistory.ts")]
    misses, up = mod.detect_misses(218, verdicts, crs, COMMITS)
    assert len(misses) == 1 and misses[0]["pr"] == 218, misses
    assert misses[0]["sha"] == F1729
    assert up == []
    print("OK test_detect_same_sha_miss")


def test_detect_next_sha_miss():
    # M1: the next-sha window is cut out -> this test goes red
    commits = ["a" * 40, "b" * 40, "c" * 40]
    verdicts = [("2026-10-25T10:00:00Z", "b" * 40, "pass", 11)]
    crs = [("2026-10-25T18:00:00Z", "c" * 40, 22,
            "CHANGES REQUESTED: major (owner, 2026-10-25, head ccc)")]
    misses, up = mod.detect_misses(1, verdicts, crs, commits)
    assert len(misses) == 1 and misses[0]["sha"] == "b" * 40, misses
    print("OK test_detect_next_sha_miss")


def test_detect_gap_two_sha_no_miss():
    commits = ["a" * 40, "b" * 40, "c" * 40, "d" * 40]
    verdicts = [("2026-10-25T10:00:00Z", "b" * 40, "pass", 11)]
    crs = [("2026-10-25T18:00:00Z", "d" * 40, 22, "CHANGES REQUESTED")]
    misses, up = mod.detect_misses(1, verdicts, crs, commits)
    assert misses == [], misses
    print("OK test_detect_gap_two_sha_no_miss")


def test_return_between_pass_and_cr_no_miss():
    # PASS, then the reviewer's return on the next sha, then a CR: the last
    # word before the CR belongs to the return, no miss
    commits = ["a" * 40, "b" * 40]
    verdicts = [("2026-10-25T10:00:00Z", "a" * 40, "pass", 11),
                ("2026-10-25T12:00:00Z", "b" * 40, "return", 12)]
    crs = [("2026-10-25T18:00:00Z", "b" * 40, 22, "CHANGES REQUESTED")]
    misses, _ = mod.detect_misses(1, verdicts, crs, commits)
    assert misses == [], misses
    print("OK test_return_between_pass_and_cr_no_miss")


def test_cr_before_pass_no_miss():
    verdicts = [("2026-10-25T18:00:01Z", F1729, "pass", 11)]
    crs = [("2026-10-25T18:00:00Z", F1729, 22, "CHANGES REQUESTED")]
    misses, _ = mod.detect_misses(1, verdicts, crs, COMMITS)
    assert misses == [], misses
    print("OK test_cr_before_pass_no_miss")


def test_cr_without_sha_unprovable():
    # an old review without commit_id and without a sha in the body: no pair
    verdicts = [("2026-10-25T10:00:00Z", F1729, "pass", 11)]
    crs = [("2026-10-25T18:00:00Z", "", 22, "CHANGES REQUESTED")]
    misses, up = mod.detect_misses(1, verdicts, crs, COMMITS)
    assert misses == [] and len(up) == 1, (misses, up)
    # and at the extract_events level: the sha is taken from the body if present
    reviews = [review("CHANGES_REQUESTED", "2026-10-25T18:00:00Z", "", 23,
                      "CHANGES REQUESTED: major (owner, head %s)" % F1729)]
    _, crs2 = mod.extract_events([], reviews, COMMITS)
    assert crs2[0][1] == F1729, crs2
    print("OK test_cr_without_sha_unprovable")


def test_rebased_cr_without_verdict_pair_no_miss():
    # the CR has a sha, but the reviewer said nothing on the window: no miss
    # and nothing unprovable (after a rebase the next-sha window is
    # unavailable, and the same sha has no pair)
    verdicts = [("2026-10-25T10:00:00Z", F1729, "pass", 11)]
    crs = [("2026-10-25T18:00:00Z", "dead" + "f" * 36, 22,
            "CHANGES REQUESTED")]
    misses, up = mod.detect_misses(1, verdicts, crs, COMMITS)
    assert misses == [] and up == [], (misses, up)
    print("OK test_rebased_cr_without_verdict_pair_no_miss")


def test_extract_events_filters_owner_and_state():
    comments = [
        comment("## Review verdict PASS on head `%s`" % F1729,
                "2026-10-25T10:00:00Z", 31),
        comment("## Review verdict PASS on head `%s`" % F1729,
                "2026-10-25T10:05:00Z", 32, user=OWNER),
        comment("unrelated", "2026-10-25T10:06:00Z", 33),
    ]
    reviews = [
        review("CHANGES_REQUESTED", "2026-10-25T18:00:00Z", F1729, 41,
               "CHANGES REQUESTED: major"),
        review("APPROVED", "2026-10-25T19:00:00Z", F1729, 42),
        review("CHANGES_REQUESTED", "2026-10-25T19:30:00Z", F1729, 43,
               body="", user=BOT),
    ]
    verdicts, crs = mod.extract_events(comments, reviews, COMMITS)
    assert verdicts == [("2026-10-25T10:00:00Z", F1729, "pass", 31)], verdicts
    assert len(crs) == 1 and crs[0][3] == "CHANGES REQUESTED: major", crs
    print("OK test_extract_events_filters_owner_and_state")


def test_owner_echo_comment_not_reviewer_event():
    # a bot echo comment "owner comment: ... ## Review verdict APPROVED" is
    # the owner's verdict, not the reviewer's
    comments = [
        comment("owner comment: 2026-10-25T15:43:28Z +00:00\n\n"
                "## Review verdict CHANGES REQUESTED: minor (owner, "
                "2026-10-25, head b739067)",
                "2026-10-25T15:43:30Z", 51),
        comment("## Review verdict APPROVED: normal (reviewer, 2026-10-25, "
                "head b7390676)", "2026-10-25T13:56:08Z", 52),
    ]
    reviews = [review("CHANGES_REQUESTED", "2026-10-25T15:43:28Z",
                      B7390, 53, "CHANGES REQUESTED: minor")]
    verdicts, crs = mod.extract_events(comments, reviews, COMMITS)
    assert len(verdicts) == 1 and verdicts[0][3] == 52, verdicts
    assert verdicts[0][1] == B7390, "the prefix b7390676 did not resolve"
    misses, _ = mod.detect_misses(205, verdicts, crs, COMMITS)
    assert len(misses) == 1, misses
    print("OK test_owner_echo_comment_not_reviewer_event")


def test_rebased_branch_cross_resolution():
    # the branch was rebased, f172973 is not in the commit list, but the
    # PASS verdict and the owner's CR lived on one sha
    comments = [comment(
        "## Review verdict **PASS** on exact head `%s` (attempt 2)" % F1729,
        "2026-09-14T00:19:21Z", 61)]
    reviews = [review("CHANGES_REQUESTED", "2026-10-24T20:45:43Z",
                      F1729, 62, "CHANGES REQUESTED: major (owner, head "
                      "f172973)")]
    commits = ["4" * 40, "5" * 40]        # the rebase removed the old head
    verdicts, crs = mod.extract_events(comments, reviews, commits)
    assert verdicts[0][1] == F1729, verdicts
    misses, up = mod.detect_misses(218, verdicts, crs, commits)
    assert len(misses) == 1 and misses[0]["sha"] == F1729, misses
    assert up == [], up
    print("OK test_rebased_branch_cross_resolution")


# ---------------------------------------------------------- classification
def test_classify():
    assert mod.classify("WCFY promises a nonexistent Skills tab") == \
        "pr-texts"
    assert mod.classify("twin port incomplete, 1.x missing commits") == \
        "twin"
    assert mod.classify("mutation survives, tests stay green") == "mutation"
    assert mod.classify("live probe 375 viewport hid the Connect button") == \
        "ui-input"
    assert mod.classify("acceptance criteria from issue #190 are not met") \
        == "contract"
    # an owner body without class anchors -> an honest "unknown", the class
    # is set by hand
    assert mod.classify("Reconnect without await hides the connection error") == \
        "unclassified"
    print("OK test_classify")


# --------------------------------------------------------------- journal
def _sample_miss():
    return {"pr": 218, "sha": F1729, "pass_ts": "2026-09-14T00:19:21Z",
            "verdict_id": 5657332218, "cr_ts": "2026-10-24T20:45:43Z",
            "cr_id": 5639932667,
            "cr_body": "## Review verdict CHANGES REQUESTED: major (owner, "
                       "2026-10-24, head f172973)\n- [BLOCK] history "
                       "is overwritten at 500 KB"}


def test_render_and_journal_dedupe():
    # M2: the merge_journal dedupe is cut out -> this test goes red
    block = mod.render_entry(_sample_miss(), "ui-input",
                             "two blockers on the same head", "test backfill")
    assert block.startswith("### #218 f172973 2026-09-14 ui-input"), block
    assert "- class: ui-input" in block and "- pr: 218" in block
    assert "\u2014" not in block, "a long dash leaked through"
    text = "journal header\n---\n"
    merged, added = mod.merge_journal(text, [block])
    assert added == 1 and "### #218 f172973" in merged
    again, added2 = mod.merge_journal(merged, [block])
    assert added2 == 0 and again == merged
    keys = mod.journal_keys(again)
    assert (218, "f172973") in keys, keys
    print("OK test_render_and_journal_dedupe")


def test_render_sanitizes_dash_and_takes_findings():
    m = _sample_miss()
    m["cr_body"] = ("## Review verdict CHANGES REQUESTED: major (owner, "
                    "2026-10-24, head f172973) \\u2014 instead of details\n"
                    "- [BLOCK] history overwrite at 500 KB \\u2014 "
                    "checked")
    block = mod.render_entry(m, "ui-input", "skipped", "test")
    assert "\u2014" not in block
    assert "history overwrite at 500 KB" in block, block
    assert "major" in block, block
    print("OK test_render_sanitizes_dash_and_takes_findings")


# ---------------------------------------------------------------- metric
def test_weekly_shares():
    passes = [("2026-10-21T10:00:00Z", None, "pass", 1),
              ("2026-10-22T10:00:00Z", None, "pass", 2),
              ("2026-10-28T10:00:00Z", None, "pass", 3)]
    misses = [{"pass_ts": "2026-10-21T11:00:00Z"},
              {"pass_ts": "2026-10-21T12:00:00Z"},
              {"pass_ts": "2026-10-28T11:00:00Z"}]
    rows = mod.weekly_shares(passes, misses)
    assert rows == [("2026-W43", 2, 2, 100.0), ("2026-W44", 1, 1, 100.0)], rows
    print("OK test_weekly_shares")


def test_write_pass_log():
    """PASS log of the miss-rate metric: sorting, sha7, empty sha."""
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "misses-passes.json")
        n = mod.write_pass_log(
            [("2026-10-27T07:53:12Z", F1729, "pass", 111, 259),
             ("2026-10-26T14:43:01Z", F1729[:7], "pass", 222, 258),
             ("2026-10-26T14:43:01Z", None, "pass", 223, 258)],
            "2026-10-20", path)
        assert n == 3, n
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        assert data["since"] == "2026-10-20", data
        assert data["generated"], data
        # sorted by (ts, pr): the two 258 entries come before 259
        assert [p["pr"] for p in data["passes"]] == [258, 258, 259], data
        assert data["passes"][0]["sha"] == F1729[:7], data    # sha cut
        assert data["passes"][1]["sha"] == "", data           # sha not resolved
        assert data["passes"][2]["id"] == 111, data
        with open(path, encoding="utf-8") as fh:
            assert fh.read().endswith("\n")
    print("OK test_write_pass_log")


def main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {t.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
