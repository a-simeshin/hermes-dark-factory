#!/usr/bin/env python3
"""Tests of the FACTORY section of metrics-evening.py on fixed data (no
network, no board).

What the fixtures pin down:
  - metrics 1/2 are fed ONLY by the reviewer bot's verdict; the owner's
    verdict is measured separately as 1b "reviewer verdict -> owner verdict"
    (fixture pr_f: reviewer verdict after 0.35 h against 21.6 h from the
    owner's review);
  - an owner APPROVED a minute before the merge does not squeeze metric 2
    (fixture pr_g);
  - credits: one card = one PR (branch / key / title; the body does not
    count), the shared card of a twin pair is split in half, the window total
    is reconciled with the ledger;
  - metric 4 (reviewer misses) is single: the numerator is the misses journal
    misses.md, the denominator is the PASS log misses-passes.json.

Mutants that must turn this red:
- "prehistory in the numerator": in journal_misses the comparison with the
  baseline is replaced with `if False` -> test_journal_misses_parse (an entry
  with a CR before 2026-10-20 reaches the metric);
- "denominator without a window": in passes_in_window the window filter is
  replaced with `if True` -> test_miss_share_window (a PASS outside the
  window is counted);
- "numerator without a window": in miss_share the window filter on pass_ts is
  replaced with `if True` -> test_miss_share_window (a 9-day-old miss is
  counted);
- burnouts() rolled back to the old rule "a same-profile comment inside the
  run window + 900 s" -> test_burnouts (a comment 73 minutes after the end
  stops counting as progress).

Run: python3 test_factory_metrics.py
Against a mutated copy: FACTORY_METRICS_SCRIPT=/path/to/copy.py python3 ...
"""
import importlib.util
import os
import sys
import tempfile
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
# Never read or write the real Hermes home; neutral project values.
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="factory-metrics-home-")
os.environ["FACTORY_REPO"] = "acme/widget"
os.environ["FACTORY_REPO_PATH"] = "/srv/hermes/widget"
os.environ["FACTORY_BOARD"] = "main"
os.environ["FACTORY_OWNER_LOGIN"] = "owner-login"
os.environ["FACTORY_OWNER_CHAT"] = "telegram:000000000"
os.environ["FACTORY_REVIEWER_LOGINS"] = "review-bot"
os.environ["FACTORY_MILESTONES"] = "1.2.0,2.2.0"

# The path can be overridden with FACTORY_METRICS_SCRIPT: mutation runs use a
# COPY of the script with a mutation applied, never the shipped file.
FACTORY_SCRIPT = os.environ.get("FACTORY_METRICS_SCRIPT") or os.path.join(
    HERE, "metrics-evening.py")
SPEC = importlib.util.spec_from_file_location("factory_metrics", FACTORY_SCRIPT)
fm = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fm)

T0 = 1793000000                      # fixed anchor
NOW = T0 + 8 * 3600
H = 3600


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


C1 = "1111111" + "a" * 33
C2 = "2222222" + "b" * 33
C3 = "3333333" + "c" * 33
C4 = "4444444" + "d" * 33
C5 = "5555555" + "e" * 33
C6 = "6666666" + "f" * 33


def pr_a():  # merged 1.2.0: push of c2 at T0+1h, reviewer verdict at T0+3h, merge at T0+6h
    return {
        "number": 300, "state": "MERGED", "isDraft": False,
        "milestone": {"title": "1.2.0 (final 1.x release)"},
        "headRefOid": C2, "headRefName": "hermes/metrics-a",
        "mergedAt": iso(T0 + 6 * H), "mergeable": "MERGEABLE",
        "updatedAt": iso(T0 + 6 * H),
        "commits": [
            {"oid": C1, "committedDate": iso(T0)},
            {"oid": C2, "committedDate": iso(T0 + 1 * H)},
        ],
        "comments": [{
            "author": {"login": "review-bot"},
            "createdAt": iso(T0 + 3 * H),
            "body": "## Review verdict: PASS on head %s" % C2,
        }],
        "reviews": [],
    }


def pr_b():  # open 1.2.0: conflicting, no movement for 72 h
    return {
        "number": 301, "state": "OPEN", "isDraft": False,
        "milestone": {"title": "1.2.0 (final 1.x release)"},
        "headRefOid": C3, "headRefName": "hermes/metrics-b",
        "mergeable": "CONFLICTING", "updatedAt": iso(NOW - 72 * H),
        "commits": [{"oid": C3, "committedDate": iso(NOW - 72 * H)}],
        "comments": [], "reviews": [],
    }


def pr_c():  # open 2.2.0: verdict an hour ago, the owner returned it AFTER the verdict
    return {
        "number": 302, "state": "OPEN", "isDraft": False,
        "milestone": {"title": "2.2.0 (parity with 1.2.0)"},
        "headRefOid": C3, "headRefName": "hermes/metrics-c",
        "mergeable": "MERGEABLE", "updatedAt": iso(NOW - 30 * 60),
        "commits": [{"oid": C3, "committedDate": iso(NOW - 3 * H)}],
        "comments": [{
            "author": {"login": "review-bot"},
            "createdAt": iso(NOW - 1 * H),
            "body": "## Review verdict: PASS on head %s" % C3,
        }],
        "reviews": [{
            "author": {"login": "owner-login"}, "state": "CHANGES_REQUESTED",
            "submittedAt": iso(NOW - int(0.5 * H)),
        }],
    }


def pr_d():  # merged 2.2.0 without a verdict on the final head
    return {
        "number": 303, "state": "MERGED", "isDraft": False,
        "milestone": {"title": "2.2.0 (parity with 1.2.0)"},
        "headRefOid": C4, "headRefName": "hermes/metrics-d",
        "mergedAt": iso(NOW - 2 * H), "mergeable": "MERGEABLE",
        "updatedAt": iso(NOW - 2 * H),
        "commits": [{"oid": C4, "committedDate": iso(NOW - 5 * H)}],
        "comments": [], "reviews": [],
    }


def pr_e():  # merged 1.2.0: the ONLY verdict is the owner's review
    return {
        "number": 304, "state": "MERGED", "isDraft": False,
        "milestone": {"title": "1.2.0 (final 1.x release)"},
        "headRefOid": C1, "headRefName": "hermes/metrics-e",
        "mergedAt": iso(NOW - 4 * H), "mergeable": "MERGEABLE",
        "updatedAt": iso(NOW - 4 * H),
        "commits": [{"oid": C1, "committedDate": iso(NOW - 8 * H)}],
        "comments": [],
        "reviews": [{
            "author": {"login": "owner-login"}, "state": "APPROVED",
            "submittedAt": iso(NOW - 6 * H), "commit": {"oid": C1},
            "body": "## Review verdict APPROVED: minor (owner, head %s)" % C1[:7],
        }],
    }


def pr_f():  # open 1.2.0: push 22 h ago, reviewer verdict after 0.35 h, owner
    # verdict (CHANGES REQUESTED) only 1 h ago. Metric 1 must give 0.35 h, not
    # 21.6 h from the owner's review; the owner's delay goes to 1b.
    return {
        "number": 305, "state": "OPEN", "isDraft": False,
        "milestone": {"title": "1.2.0 (final 1.x release)"},
        "headRefOid": C5, "headRefName": "hermes/metrics-f",
        "mergeable": "MERGEABLE", "updatedAt": iso(NOW - 1 * H),
        "commits": [{"oid": C5, "committedDate": iso(NOW - 22 * H)}],
        "comments": [{
            "author": {"login": "review-bot"},
            "createdAt": iso(NOW - 21.65 * H),
            "body": "## Re-review verdict: PASS on head %s" % C5,
        }],
        "reviews": [{
            "author": {"login": "owner-login"}, "state": "CHANGES_REQUESTED",
            "submittedAt": iso(NOW - 1 * H), "commit": {"oid": C5},
            "body": "## Review verdict CHANGES REQUESTED (owner)",
        }],
    }


def pr_g():  # merged 2.2.0: reviewer verdict on the head 10 h ago, owner
    # APPROVED a minute before the merge. Metric 2 must count from the
    # REVIEWER's verdict (8 h), not from the owner's APPROVED (1 min).
    return {
        "number": 306, "state": "MERGED", "isDraft": False,
        "milestone": {"title": "2.2.0 (parity with 1.2.0)"},
        "headRefOid": C6, "headRefName": "hermes/metrics-g",
        "mergedAt": iso(NOW - 2 * H), "mergeable": "MERGEABLE",
        "updatedAt": iso(NOW - 2 * H),
        "commits": [{"oid": C6, "committedDate": iso(NOW - 11 * H)}],
        "comments": [{
            "author": {"login": "review-bot"},
            "createdAt": iso(NOW - 10 * H),
            "body": "## Review verdict: PASS on head %s" % C6,
        }],
        "reviews": [{
            "author": {"login": "owner-login"}, "state": "APPROVED",
            "submittedAt": iso(NOW - 2 * H - 60), "commit": {"oid": C6},
        }],
    }


def board_fixture():
    tasks = [
        {"id": "t_aaa", "title": "coder card PR#300", "status": "done",
         "assignee": "coder", "branch_name": "hermes/metrics-a",
         "idempotency_key": None, "block_kind": None, "created_at": T0},
        {"id": "t_rev300", "title": "[reviewer] PR #300", "status": "done",
         "assignee": "reviewer", "branch_name": None,
         "idempotency_key": "reviewer-pr300-2222222", "block_kind": None,
         "created_at": T0},
        {"id": "t_n1", "title": "waits for the owner", "status": "blocked",
         "assignee": "coder", "branch_name": None, "idempotency_key": None,
         "block_kind": "needs_input", "created_at": NOW - 10 * H},
        {"id": "t_n2", "title": "recently blocked", "status": "blocked",
         "assignee": "coder", "branch_name": None, "idempotency_key": None,
         "block_kind": "needs_input", "created_at": NOW - 1 * H},
        {"id": "t_dep", "title": "waits for the parent", "status": "blocked",
         "assignee": "coder", "branch_name": None, "idempotency_key": None,
         "block_kind": "dependency", "created_at": NOW - 9 * H},
    ]
    runs = [
        {"id": 1, "task_id": "t_aaa", "profile": "coder",
         "started_at": NOW - 4 * H, "ended_at": NOW - 3 * H,
         "error": "Iteration budget exhausted: max iterations"},
        {"id": 2, "task_id": "t_dep", "profile": "coder",
         "started_at": NOW - 30 * H, "ended_at": NOW - 29 * H,
         "error": "Iteration budget exhausted: max iterations"},
        {"id": 3, "task_id": "t_n1", "profile": "coder",
         "started_at": NOW - 2 * H, "ended_at": NOW - 1 * H,
         "error": "Iteration budget exhausted"},
        {"id": 4, "task_id": "t_aaa", "profile": "coder",
         "started_at": NOW - 50 * H, "ended_at": NOW - 49 * H,
         "error": None},
    ]
    comments = [
        # run 1 (t_aaa, ended NOW-3H): the worker reported after 5 minutes
        {"task_id": "t_aaa", "author": "coder",
         "created_at": NOW - 3 * H + 300,
         "body": "Done: skeleton, coverage remains."},
        # run 3 (t_n1, ended NOW-1H): a comment 73 minutes after the end,
        # outside the old +900 s window. The single definition counts it as
        # progress.
        {"task_id": "t_n1", "author": "coder",
         "created_at": NOW - 1 * H + 4380,
         "body": "Burnout closed: did a block, one fix remains."},
        # run 2 (t_dep): a system BLOCKED is not progress, the watchdog
        # marker is progress (the marker starts with "Watchdog:" too, but it
        # is exactly the record of progress)
        {"task_id": "t_dep", "author": "watchdog",
         "created_at": NOW - 29 * H + 600,
         "body": "BLOCKED: waiting for the parent."},
        {"task_id": "t_dep", "author": "watchdog",
         "created_at": NOW - 29 * H + 900,
         "body": "Watchdog: progress after burnout. The worker reported."},
    ]
    blocked = {"t_n1": NOW - 5 * H, "t_n2": NOW - 1 * H}
    return {"tasks": tasks, "runs": runs, "comments": comments,
            "blocked": blocked}


def ledger_fixture():
    return [
        {"ts": NOW - 3600, "kind": "card", "ref": "t_aaa", "model": "glm-5.3",
         "input_tokens": 1_000_000, "cache_read_tokens": 1_000_000,
         "output_tokens": 500_000},
        {"ts": NOW - 3600, "kind": "card", "ref": "t_aaa",
         "model": "glm-5.3-flash",
         "input_tokens": 1_000_000, "cache_read_tokens": 0,
         "output_tokens": 100_000},
        {"ts": NOW - 3600, "kind": "card", "ref": "t_aaa",
         "model": "deepseek-v4-flash",
         "cost_usd_openrouter": 0.5},
        {"ts": NOW - 3600, "kind": "card", "ref": "t_other", "model": "glm-5.3",
         "input_tokens": 1_000_000, "cache_read_tokens": 0,
         "output_tokens": 0},
        {"ts": NOW - 3600, "kind": "session", "ref": "x", "model": "glm-5.3",
         "input_tokens": 1_000_000, "cache_read_tokens": 0,
         "output_tokens": 0},
    ]


def test_push_to_verdict():
    prs = [pr_a(), pr_b(), pr_c(), pr_d(), pr_f(), pr_g()]
    got = fm.push_to_verdict(prs, NOW - 24 * H, NOW)
    by_ref = {ref: h for h, ref in got}
    assert abs(by_ref["PR#300@2222222"] - 2.0) < 1e-6, got   # (T0+3h)-(T0+1h)
    assert abs(by_ref["PR#302@3333333"] - 2.0) < 1e-6, got   # (NOW-1h)-(NOW-3h)
    assert abs(by_ref["PR#305@5555555"] - 0.35) < 0.01, got  # not 21.6
    assert abs(by_ref["PR#306@6666666"] - 1.0) < 1e-6, got   # verdict on the head
    assert "PR#303@4444444" not in by_ref                     # no verdict
    # the owner's only verdict does not feed metric 1 (pr_e)
    assert "PR#304@1111111" not in {ref for _, ref in fm.push_to_verdict(
        [pr_e()], NOW - 24 * H, NOW)}, "owner APPROVED is not a reviewer verdict"
    print("OK test_push_to_verdict")


def test_verdict_to_owner():
    got = fm.verdict_to_owner([pr_c(), pr_e(), pr_f(), pr_g()],
                              NOW - 24 * H, NOW)
    by_ref = {ref: h for h, ref in got}
    assert abs(by_ref["PR#302@3333333"] - 0.5) < 1e-6, got    # (NOW-0.5h)-(NOW-1h)
    # the owner was ~20.65 h late after the reviewer verdict
    assert abs(by_ref["PR#305@5555555"] - 20.65) < 0.01, got
    # the owner's APPROVED came ~8 h after the reviewer verdict
    assert abs(by_ref["PR#306@6666666"] - (8.0 - 60 / 3600)) < 0.01, got
    # pr_e has no reviewer verdict at all: no pair
    assert fm.verdict_to_owner([pr_e()], NOW - 24 * H, NOW) == []
    print("OK test_verdict_to_owner")


def test_verdict_to_merge():
    v = fm.verdict_to_merge(pr_a())
    assert v and abs(v[0] - 3.0) < 1e-6 and v[1] == "PR#300@2222222", v
    assert fm.verdict_to_merge(pr_d()) is None
    # an owner APPROVED without a reviewer verdict is not a reviewer verdict
    assert fm.verdict_to_merge(pr_e()) is None
    # from the REVIEWER's verdict (8 h), not from the owner's APPROVED a minute earlier (pr_g)
    v = fm.verdict_to_merge(pr_g())
    assert v and abs(v[0] - 8.0) < 0.01, v
    print("OK test_verdict_to_merge")


def test_merged_without_verdict():
    assert fm.merged_without_verdict(pr_d()) == "PR#303@4444444"
    assert fm.merged_without_verdict(pr_a()) is None
    # a merge on the owner's APPROVED alone = merged without a reviewer verdict (pr_e)
    assert fm.merged_without_verdict(pr_e()) == "PR#304@1111111"
    print("OK test_merged_without_verdict")


def test_open_summary():
    s = fm.open_summary([pr_a(), pr_b(), pr_c(), pr_d()], NOW)
    assert s["total"] == 2 and s["drafts"] == 0, s
    assert s["conflict"] == 1 and s["conflict_refs"] == ["PR#301"], s
    assert s["stale"] == 1 and s["stale_refs"][0].startswith("PR#301 (72"), s
    print("OK test_open_summary")


def misses_fixture():
    """Live journal format: 6 entries, dates as in a real journal.

    300: PASS in the 24 h window; 301/302: PASS in the 7-day window; 303: PASS
    9 days ago with a late CR (the pair is alive, but the PASS is outside the
    window); 304: prehistory (CR before the 2026-10-20 baseline); 306: the
    heading was edited by hand and differs from the verdict date, the parser
    must take the verdict line, not the heading.
    """
    def entry(pr, sha7, pass_t, cr_date, klass, header_date=None):
        p = datetime.fromtimestamp(pass_t, tz=timezone.utc)
        return (
            "### #%d %s %s %s\n"
            "- pr: %d\n"
            "- sha: %s\n"
            "- verdict: PASS %s (issuecomment-64%05d)\n"
            "- owner: CHANGES REQUESTED normal %s (review-64%05d)\n"
            "- found: the owner's finding after the PASS\n"
            "- class: %s\n"
            "- missed: the reviewer missed this\n"
            "- source: watchdog 2026-10-26\n"
            % (pr, sha7, header_date or cr_date, klass, pr, sha7,
               p.strftime("%Y-%m-%d %H:%M"), pr, cr_date, pr, klass))
    return "\n\n".join([
        entry(300, C1[:7], NOW - 5 * H, "2026-10-26", "mutation"),
        entry(301, C2[:7], NOW - 3 * 86400, "2026-10-23", "contract"),
        entry(302, C3[:7], NOW - 4 * 86400, "2026-10-22", "twin"),
        entry(303, C4[:7], NOW - 9 * 86400, "2026-10-21", "ui-input"),
        entry(304, C5[:7], NOW - 50 * 86400, "2026-09-07", "pr-texts"),
        entry(306, C6[:7], NOW - 2 * H, "2026-10-26", "mutation",
              header_date="2026-10-28"),
    ])


def passes_fixture(generated=None):
    """The watchdog's PASS log: the same verdicts as in the journal plus clean PASSes.

    305 is outside the 7-day window; 307/308 have no misses (the denominator
    is larger than the numerator, as in real life).
    """
    def p(pr, sha7, t, i):
        return {"pr": pr, "sha": sha7, "ts": iso(t), "id": i}
    return {
        "generated": generated or iso(NOW - H),
        "since": "2026-10-20",
        "passes": [
            p(300, C1[:7], NOW - 5 * H, 111),
            p(301, C2[:7], NOW - 3 * 86400, 222),
            p(302, C3[:7], NOW - 4 * 86400, 333),
            p(305, C4[:7], NOW - 9 * 86400, 444),
            p(306, C6[:7], NOW - 2 * H, 555),
            p(307, C2[:7], NOW - 6 * H, 666),
            p(308, C1[:7], NOW - 30 * 60, 777),
        ],
    }


def test_journal_misses_parse():
    entries = fm.journal_misses(misses_fixture())
    # 304 is cut by the baseline (CR before 2026-10-20), the rest are in
    assert [e["pr"] for e in entries] == [300, 301, 302, 303, 306], entries
    e = entries[0]
    assert e["sha7"] == C1[:7] and e["klass"] == "mutation", e
    assert e["cr_date"] == "2026-10-26", e
    # the verdict date comes from the verdict line, not from the heading: the
    # heading of 306 says 2026-10-28 (by hand), the PASS was two days earlier
    e306 = [x for x in entries if x["pr"] == 306][0]
    assert abs(e306["pass_ts"] - (NOW - 2 * H)) < 60, e306
    print("OK test_journal_misses_parse")


def test_miss_share_window():
    sh = fm.miss_share(misses_fixture(), passes_fixture(), NOW - 24 * H, NOW)
    assert sh["n_miss"] == 2 and sh["n_pass"] == 4, sh
    assert sh["refs"] == ["PR#300@%s" % C1[:7], "PR#306@%s" % C6[:7]], sh
    sh7 = fm.miss_share(misses_fixture(), passes_fixture(),
                        NOW - 7 * 86400, NOW)
    assert sh7["n_miss"] == 4 and sh7["n_pass"] == 6, sh7
    # 303: the pair is alive (CR in the baseline) but the PASS is outside the
    # window -> not in the numerator; 305 is outside the window -> not in the
    # denominator
    assert [r.split("@")[0] for r in sh7["refs"]] == [
        "PR#302", "PR#301", "PR#300", "PR#306"], sh7
    print("OK test_miss_share_window")


def test_burnouts():
    board = board_fixture()
    b24 = fm.burnouts(board["runs"], board["comments"], NOW - 24 * H, NOW)
    assert b24["n"] == 2, b24                               # runs 1 and 3
    # both with progress: run 1 the worker after 5 minutes, run 3 the worker
    # 73 minutes after the end (the old rule with a +900 s window called it
    # "WITHOUT a comment")
    assert b24["commented"] == 2, b24
    items = {b["run"]: b["comment"] for b in b24["items"]}
    assert items[3] is True, b24
    b7 = fm.burnouts(board["runs"], board["comments"], NOW - 7 * 86400, NOW)
    # run 2: the system BLOCKED does not count, the watchdog marker does
    assert b7["n"] == 3 and b7["commented"] == 3, b7
    print("OK test_burnouts")


def test_closes_burnout_progress():
    """The single definition (the same for the watchdog and metric 5): the
    watchdog marker or a substantive non-system comment after the end of the
    run."""
    assert fm.closes_burnout_progress("Watchdog: progress after burnout."
                                      " The worker reported.") is True
    assert fm.closes_burnout_progress("Done: skeleton, coverage remains") is True
    assert fm.closes_burnout_progress("BLOCKED: waiting for the parent.") is False
    assert fm.closes_burnout_progress("Watchdog: rework card size gate."
                                      ) is False
    assert fm.closes_burnout_progress("SCHEDULED: stand 15:00") is False
    assert fm.closes_burnout_progress("") is True
    print("OK test_closes_burnout_progress")


def test_credits():
    cr = fm.credits(ledger_fixture(), {"t_aaa"})
    # glm-5.3: (1e6*6.9 + 1e6*1.7 + 5e5*24)/1e4 = 2060.0
    assert abs(cr["per_model"]["glm-5.3"] - 2060.0) < 1e-6, cr
    # flash: (1e6*2.3 + 1e5*8.0)/1e4 = 310.0
    assert abs(cr["per_model"]["glm-5.3-flash"] - 310.0) < 1e-6, cr
    assert abs(cr["zai"] - 2370.0) < 1e-6, cr
    # no dollars; a model without a price is not priced
    assert "usd_other" not in cr, cr
    print("OK test_credits")


def test_needs_input_stale():
    board = board_fixture()
    stale = fm.needs_input_stale(board["tasks"], board["blocked"], NOW)
    assert len(stale) == 1 and stale[0]["id"] == "t_n1", stale
    assert abs(stale[0]["age_h"] - 5.0) < 1e-6, stale        # by the blocked event
    print("OK test_needs_input_stale")


def test_assign_cards():
    board = board_fixture()
    prs = [pr_a(), pr_b(), pr_c(), pr_d(),
           {"number": 154, "milestone": {"title": "1.2.0 (final 1.x release)"},
            "headRefName": "hermes/twin-154"},
           {"number": 178, "milestone": {"title": "2.2.0 (parity with 1.2.0)"},
            "headRefName": "hermes/twin-178"}]
    # the branch is the strongest carrier: t_aaa goes to PR#300 entirely
    w = fm.assign_cards(board, prs)
    assert w["t_aaa"] == {300: 1.0}, w
    assert w["t_rev300"] == {300: 1.0}, w                    # key reviewer-pr300
    # a mention of another PR IN THE BODY does not pull the card
    board["tasks"].append({"id": "t_body", "title": "ops work",
                           "body": "context: see #300 and #89",
                           "branch_name": None, "idempotency_key": None})
    w = fm.assign_cards(board, prs)
    assert "t_body" not in w, w
    # the shared card of a twin pair (#154 1.2.0 + #178 2.2.0) is split in half
    board["tasks"].append({"id": "t_twin", "title": "PR #178/#154 follow-up",
                           "body": "", "branch_name": None,
                           "idempotency_key": None})
    w = fm.assign_cards(board, prs)
    assert w["t_twin"] == {154: 0.5, 178: 0.5}, w
    # the key beats the title: owner-review-#177-... is the card of #177,
    # even if the title names another PR
    board["tasks"].append({"id": "t_key", "title": "PR #300: rework",
                           "body": "", "branch_name": None,
                           "idempotency_key": "owner-review-#177-7c48f92"})
    prs177 = prs + [{"number": 177,
                     "milestone": {"title": "1.2.0 (final 1.x release)"},
                     "headRefName": "hermes/metrics-177"}]
    w = fm.assign_cards(board, prs177)
    assert w["t_key"] == {177: 1.0}, w
    # the key works WITHOUT a PR mention in the title (live format
    # reviewer-pr<N>-<head7>, no hyphen between pr and the number)
    board["tasks"].append({"id": "t_k301", "title": "review without a number",
                           "body": "", "branch_name": None,
                           "idempotency_key": "reviewer-pr301-abc1234"})
    w = fm.assign_cards(board, prs177)
    assert w["t_k301"] == {301: 1.0}, w
    # both PRs on the same line (not a twin pair): one PR by the strongest carrier
    board["tasks"].append({"id": "t_same", "title": "PR #300/#301 shared",
                           "body": "", "branch_name": None,
                           "idempotency_key": None})
    w = fm.assign_cards(board, prs)
    assert w["t_same"] == {300: 1.0}, w
    print("OK test_assign_cards")


def test_credits_weighted_and_totals():
    rows = ledger_fixture()
    weights = {"t_aaa": {300: 0.5, 301: 0.5}}
    per_pr = fm.credits_weighted(rows, weights)
    # t_aaa: glm-5.3 2060 + flash 310 = 2370, half each = 1185
    assert abs(per_pr[300]["zai"] - 1185.0) < 1e-6, per_pr
    assert abs(per_pr[301]["zai"] - 1185.0) < 1e-6, per_pr
    assert "usd_other" not in per_pr[300], per_pr             # no dollars
    tot = fm.ledger_totals(rows)
    # zai_all: t_aaa 2370 + t_other 690 + session 690 = 3750
    assert abs(tot["zai_all"] - 3750.0) < 1e-6, tot
    assert abs(tot["zai_cards"] - 3060.0) < 1e-6, tot          # without session
    assert "usd_all" not in tot, tot                           # no dollars
    # report invariant: the sum over PRs is not above the total ledger spend
    assert sum(v["zai"] for v in per_pr.values()) <= tot["zai_all"] + 1e-6
    print("OK test_credits_weighted_and_totals")


def test_report_end_to_end():
    data = {
        "gh": {"repo": "test/repo",
               "prs": [pr_a(), pr_b(), pr_c(), pr_d(), pr_g()]},
        "board": board_fixture(),
        "ledger": ledger_fixture(),
        "misses": {"journal": misses_fixture(),
                   "passes": passes_fixture()},
    }
    text = fm.report(now=NOW, data=data)
    assert "1. push->reviewer verdict [1.2.0]: median 2.0 h, max 2.0 h (n=1: PR#300@2222222)" in text
    # 1b on a separate line: 2.2.0 has the pairs 302 (0.5 h) and 306 (~8 h)
    assert "1b. reviewer verdict->owner verdict [1.2.0]: no events in the window" in text
    assert "1b. reviewer verdict->owner verdict [2.2.0]: median" in text
    assert "PR#306@6666666" in text and "PR#302@3333333" in text
    assert "2. reviewer verdict->merge [1.2.0]: median 3.0 h" in text
    # metric 2 counts from the reviewer's verdict, not from the owner's APPROVED
    assert "2. reviewer verdict->merge [2.2.0]: median 8.0 h" in text
    assert "merged WITHOUT a reviewer verdict on the final head: PR#303@4444444" in text
    assert "CONFLICTING 1 (PR#301)" in text
    assert "no activity >48 h: 1 (PR#301 (72 h) " in text or "no activity >48 h: 1 (PR#301 (72 h)" in text
    # the single metric 4: journal / PASS log, no milestones
    assert (f"4. reviewer misses by journal: 50% (2 of 4 PASS: "
            f"PR#300@{C1[:7]}, PR#306@{C6[:7]}); watchdog PASS base from "
            f"{iso(NOW - H)}") in text
    assert (f"4. reviewer misses by journal: 67% (4 of 6 PASS: "
            f"PR#302@{C3[:7]}, PR#301@{C2[:7]}, PR#300@{C1[:7]}, "
            f"PR#306@{C6[:7]})") in text
    # 1b in 2.2.0: the median between 0.5 h (302) and ~8 h (306)
    assert "1b. reviewer verdict->owner verdict [2.2.0]: median 4.2 h, max 8.0 h (n=2: PR#306@6666666, PR#302@3333333)" in text
    assert "5. burnouts" in text and "with a progress comment 2" in text
    assert "PR#300 (t_aaa, t_rev300): plan credits 2370.0" in text
    # window total + reconciliation with the ledger (no dollars)
    assert "6. credits for the window in total: plan 3750.0;" in text
    assert "other models" not in text and "DeepSeek" not in text
    assert "merged PRs 2370.0" in text
    assert "reconciliation of the sum with the ledger: OK" in text
    # the heading of section 7 is extended (needs_input + questions without kind)
    assert "7. waiting for the owner more than 2 h (needs_input + questions without kind): 1: t_n1 5.0 h" in text
    # short window: the verdict of PR#300 (NOW-5h) is inside 24 h, PR#303 merged 2 h ago
    assert "6. no merged PRs in the window" not in text.split("--- window: day (24 h)")[1].split("--- window: rolling 7 days")[0]
    print("OK test_report_end_to_end")


def test_miss_report_edges():
    """A missing PASS log and a stale base must be visible in the report."""
    base = {"gh": {"repo": "test/repo", "prs": [pr_a()]},
            "board": board_fixture(), "ledger": ledger_fixture()}
    d1 = dict(base)
    d1["misses"] = {"journal": misses_fixture(), "passes": {}}
    t1 = fm.report(now=NOW, data=d1)
    assert "NO PASS BASE misses-passes.json" in t1, t1
    assert "misses in the journal for the window: 2" in t1, t1
    d2 = dict(base)
    d2["misses"] = {"journal": misses_fixture(),
                    "passes": passes_fixture(generated=iso(NOW - 100 * H))}
    t2 = fm.report(now=NOW, data=d2)
    assert "PASS base is stale, 100 h" in t2, t2
    print("OK test_miss_report_edges")


def test_outcomes_sum_to_total():
    """The line "outcomes (all, as is)" must sum ALL outcomes of task_runs. A
    live report once printed "finished 18, successful 2, failed 0": 16 runs
    (blocked, dependency_wait, changes_requested, superseded, rate_limited)
    fell into no line."""
    now = time.time()
    runs = [
        ("t1", now - 7200, now - 3600, "completed", "coder", ""),
        ("t2", now - 7100, now - 3600, "review_requested", "coder", ""),
        ("t3", now - 7000, now - 3600, "blocked", "coder", ""),
        ("t4", now - 6900, now - 3600, "dependency_wait", "coder", ""),
        ("t5", now - 6800, now - 3600, "changes_requested", "coder", ""),
        ("t6", now - 6700, now - 3600, "superseded", "coder", ""),
        ("t7", now - 6600, now - 3600, "rate_limited", "coder", ""),
        ("t8", now - 6500, now - 3600, "crashed", "coder", "not alive: killed"),
        ("t9", now - 6400, now - 3600, "timed_out", "coder", ""),
    ]
    tasks = {("t%d" % i): {"title": "card %d" % i, "status": "todo",
                           "created": now - 9000, "done": None, "fails": 0}
             for i in range(1, 10)}
    tasks["t1"]["status"] = "done"
    tasks["t1"]["done"] = now - 3500
    saved = fm.q_collect
    fm.q_collect = lambda days: (runs, tasks, [])
    try:
        out = fm.quality_report(1)
    finally:
        fm.q_collect = saved
    line = [l for l in out.splitlines() if "outcomes (all, as is)" in l]
    assert line, out
    # all 9 outcomes in one line, the sum equals the number of runs with an outcome
    assert "blocked=1" in line[0] and "dependency_wait=1" in line[0], line[0]
    assert "changes_requested=1" in line[0] and "superseded=1" in line[0], line[0]
    assert "rate_limited=1" in line[0] and "timed_out=1" in line[0], line[0]
    assert "crashed=1" in line[0] and "(sum 9)" in line[0], line[0]
    # the classic failure of the live report: 2 successful + 1 failed != 9
    assert "successful              2" in out, out
    print("OK test_outcomes_sum_to_total")


def test_scheduled_not_finished():
    """outcome=scheduled is an assigned but not finished run. A live report
    printed "runs finished 27", including 18 scheduled. Right: scheduled is
    outside "finished", outside the outcomes, on a separate line; with zero
    scheduled the line is not printed."""
    now = time.time()
    runs = [
        ("t1", now - 7200, now - 3600, "completed", "coder", ""),
        ("t2", now - 7100, now - 3600, "crashed", "coder", ""),
        ("t3", now - 7000, None, "scheduled", "coder", ""),
        ("t4", now - 6900, None, "scheduled", "coder", ""),
    ]
    tasks = {("t%d" % i): {"title": "card %d" % i, "status": "todo",
                           "created": now - 9000, "done": None, "fails": 0}
             for i in range(1, 5)}
    saved = fm.q_collect
    fm.q_collect = lambda days: (runs, tasks, [])
    try:
        out = fm.quality_report(1)
    finally:
        fm.q_collect = saved
    assert "runs finished           2" in out, out
    assert "scheduled (unfinished)  2" in out, out
    line = [l for l in out.splitlines() if "outcomes (all, as is)" in l]
    assert line and "scheduled" not in line[0] and "(sum 2)" in line[0], line[0]
    # without scheduled there is no separate line
    fm.q_collect = lambda days: (runs[:2], tasks, [])
    try:
        out2 = fm.quality_report(1)
    finally:
        fm.q_collect = saved
    assert "scheduled (unfinished)" not in out2, out2
    print("OK test_scheduled_not_finished")


def test_usage_section_credits_only():
    """The spend section has no dollars and no DeepSeek: the only unit is
    plan credits by the PRICING table."""
    import json as _json
    import tempfile
    from pathlib import Path as _Path
    tmp = _Path(tempfile.mkdtemp(prefix="me-usage-"))
    (tmp / "usage-ledger").mkdir(parents=True)
    rows = [
        {"ts": NOW - 3600, "kind": "card", "ref": "t_aaa", "model": "glm-5.3",
         "input_tokens": 1_000_000, "cache_read_tokens": 1_000_000,
         "output_tokens": 500_000,
         "cost_usd_openrouter": 2.624},          # dollars stay in the ledger,
        {"ts": NOW - 3600, "kind": "card", "ref": "t_ds",              # the report
         "model": "deepseek-v4-flash", "input_tokens": 1_000,          # does not
         "output_tokens": 100, "cache_read_tokens": 0,                 # read them
         "cost_usd_openrouter": 0.5, "deepseek_balance_usd": -0.01},
        {"ts": NOW - 3600, "kind": "session", "ref": "x", "model": "glm-5.3",
         "input_tokens": 1_000_000, "cache_read_tokens": 0,
         "output_tokens": 0, "cost_usd_openrouter": 0.1},
    ]
    (tmp / "usage-ledger" / "ledger.jsonl").write_text(
        "\n".join(_json.dumps(r) for r in rows), encoding="utf-8")
    old_home = fm.HOME
    fm.HOME = tmp
    try:
        out = fm.usage_section(NOW, titles={"t_aaa": ("done", "card"),
                                            "t_ds": ("done", "deepseek card")})
    finally:
        fm.HOME = old_home
    assert "$" not in out and "DeepSeek" not in out, out
    # day: t_aaa 2060.0 + session 690.0 = 2750.0 credits; deepseek is not priced
    assert "credits=2750.0" in out, out
    assert "no price: 1" in out, out
    assert "by model (price table)" in out and "glm-5.3=2750.0" in out, out
    print("OK test_usage_section_credits_only")


def main():
    test_push_to_verdict()
    test_verdict_to_owner()
    test_verdict_to_merge()
    test_merged_without_verdict()
    test_open_summary()
    test_journal_misses_parse()
    test_miss_share_window()
    test_burnouts()
    test_closes_burnout_progress()
    test_credits()
    test_needs_input_stale()
    test_assign_cards()
    test_credits_weighted_and_totals()
    test_miss_report_edges()
    test_outcomes_sum_to_total()
    test_scheduled_not_finished()
    test_usage_section_credits_only()
    test_report_end_to_end()
    print("ALL FACTORY-METRICS TESTS PASSED")


if __name__ == "__main__":
    main()
