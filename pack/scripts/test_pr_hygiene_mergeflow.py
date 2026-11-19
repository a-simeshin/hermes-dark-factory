#!/usr/bin/env python3
"""Tests of the merge watchdog and rebase ordering in pr-hygiene.py.

The post-merge watchdog is covered with gh/git/sqlite mocks: base shift
trigger, bootstrap, duplicate protection by key and retarget of a live card.
Only the pure functions (rebase_priority, rebase_key) run live.

Mutants (PR_HYGIENE_SCRIPT=<copy with one edit>) must fail at least one test:
- drop the addressee filter (existing = list(by_pr[n])) -> fails
  test_wrong_addressee_gets_rebase_card_not_comment: the existing-plan goes
  to a card without an assignee;
- drop the rebase kind from the absorbing set -> fails
  test_existing_card_gets_comment_not_new_card: a coder rebase card stops
  absorbing and a duplicate is created;
- drop the assignee == coder check -> fails
  test_wrong_addressee_gets_rebase_card_not_comment: a card without an
  executor receives the comment.
"""
import importlib.util
import json
import os
import re
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# Settings are read at import time; the Hermes home is a temporary directory.
os.environ.update({
    "HERMES_HOME": tempfile.mkdtemp(prefix="ph-mergeflow-"),
    "FACTORY_REPO": "acme/widget",
    "FACTORY_REPO_PATH": "/srv/hermes/widget",
    "FACTORY_BOARD": "main",
    "FACTORY_OWNER_LOGIN": "owner-login",
    "FACTORY_OWNER_CHAT": "telegram:000000000",
    # Two long-lived release lines: the scenarios depend on both.
    "FACTORY_BASE_BRANCHES": "develop/1.x,develop/2.x",
})
# A copy of the script is substituted via PR_HYGIENE_SCRIPT (mutation runs).
SCRIPT = os.environ.get("PR_HYGIENE_SCRIPT") or os.path.join(
    HERE, "pr-hygiene.py")

spec = importlib.util.spec_from_file_location("pr_hygiene", SCRIPT)
if spec is None or spec.loader is None:
    print("SKIP: cannot load pr-hygiene.py")
    sys.exit(0)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

HEADS = {"develop/1.x": "1111111111111111111111111111111111111111",
         "develop/2.x": "2222222222222222222222222222222222222222"}


def make_info(conflicting=(), base="develop/2.x"):
    info = {}
    for n in conflicting:
        info[n] = {"baseRefName": base, "detail": {"mergeable": "CONFLICTING"},
                   "url": "http://x/%d" % n, "headRefName": "b%d" % n}
    for n in (900,):
        info[n] = {"baseRefName": base, "detail": {"mergeable": "MERGEABLE"},
                   "url": "http://x/900", "headRefName": "b900"}
    return info


def isolate(tmpdir, live=None):
    """Mock the world: line heads, review, board, state file paths."""
    mod.MERGE_WATCH = os.path.join(tmpdir, "merge-watch.json")
    mod.DEBT_CACHE = os.path.join(tmpdir, "debt.json")
    mod.line_heads = lambda: dict(HEADS)
    mod.live_rebase_cards = lambda: dict(live or {})
    created = []
    mod.create_rebase_card = lambda *a, **k: (created.append(a) or (True, "ok"))
    mod.comment_card = lambda tid, text: True
    # The admission gate before create (board-watchdog gen_admission) reads
    # the boards: keep it inside the sandbox.
    bw = getattr(mod, "board_watchdog", None)
    if bw is not None and hasattr(bw, "BOARDS"):
        bw.BOARDS = os.path.join(tmpdir, "boards")
        os.makedirs(bw.BOARDS, exist_ok=True)
    return created


def test_rebase_priority_order():
    """The reviewer's verdict (99) outranks a stack root (97) which outranks
    the rest (96)."""
    assert mod.rebase_priority(True, False) == 99
    assert mod.rebase_priority(False, True) == 97
    assert mod.rebase_priority(False, False) == 96
    assert mod.rebase_priority(True, True) == 99, "verdict outranks the root"
    print("OK test_rebase_priority_order")


def test_rebase_key_format():
    assert mod.rebase_key(210, "abc1234") == "coder-pr210-rebase-abc1234"
    m = mod.REBASE_KEY_RE.match("coder-pr210-rebase-abc1234")
    assert m and m.group(1) == "210" and m.group(2) == "abc1234"
    assert mod.REBASE_KEY_RE.match("coder-pr210-rebase-XYZ") is None
    print("OK test_rebase_key_format")


def test_bootstrap_plan():
    """No observation yet: cards for the whole current conflict debt; a
    dry-run creates nothing and writes no state (the event is not consumed)."""
    with tempfile.TemporaryDirectory() as tmp:
        created = isolate(tmp)
        mod.review_state = lambda n: ("ok" if n == 244 else "none", "")
        findings, total = mod.post_merge_watch(
            make_info((244, 247, 113)), {250: 247}, dry_run=True)
        assert total == 3
        would = [k for k, _ in findings if k.startswith("mergeflow-rebase-would")]
        assert len(would) == 3, findings
        assert any("p99" in t for _, t in findings), "244 with a verdict first"
        assert any("p97" in t for _, t in findings), "247 is a stack root"
        assert not created, "dry-run creates no cards"
        assert not os.path.exists(mod.MERGE_WATCH), "dry-run writes no state"
        assert not os.path.exists(mod.DEBT_CACHE)
    print("OK test_bootstrap_plan")


def test_no_merge_no_cards():
    """The base SHA did not change: the watchdog is silent even if PRs
    conflict (a card with a live target exists or the conflict is not fresh)."""
    with tempfile.TemporaryDirectory() as tmp:
        isolate(tmp)
        mod.review_state = lambda n: ("none", "")
        json.dump({"develop/1.x": {"sha": HEADS["develop/1.x"]},
                   "develop/2.x": {"sha": HEADS["develop/2.x"]}},
                  open(mod.MERGE_WATCH, "w"))
        findings, total = mod.post_merge_watch(
            make_info((113, 244)), {}, dry_run=False)
        assert total == 2
        assert findings == [], findings
    print("OK test_no_merge_no_cards")


def test_merge_creates_cards_and_writes_state():
    """The SHA moved: a live run creates cards and writes the observation."""
    with tempfile.TemporaryDirectory() as tmp:
        created = isolate(tmp)
        mod.review_state = lambda n: ("none", "")
        json.dump({"develop/2.x": {"sha": "0" * 40}},
                  open(mod.MERGE_WATCH, "w"))
        findings, total = mod.post_merge_watch(
            make_info((113,)), {}, dry_run=False)
        assert total == 1 and len(created) == 1, (findings, created)
        args = created[0]
        assert args[0] == 113 and args[1] == "develop/2.x" and args[2] == "2222222"
        watch = json.load(open(mod.MERGE_WATCH))
        assert watch["develop/2.x"]["sha"] == HEADS["develop/2.x"]
        debt = json.load(open(mod.DEBT_CACHE))
        assert debt["count"] == 1 and debt["lines"]["develop/2.x"] == "2222222"
    print("OK test_merge_creates_cards_and_writes_state")


def test_same_key_skip_and_retarget():
    """A live card with the same target is silent; the target moved ->
    a retarget comment, no second card for the same PR."""
    with tempfile.TemporaryDirectory() as tmp:
        created = isolate(tmp, live={
            113: ("t_x", "coder-pr113-rebase-2222222"),
            247: ("t_y", "coder-pr247-rebase-oldshaa"),
        })
        mod.review_state = lambda n: ("none", "")
        findings, _ = mod.post_merge_watch(
            make_info((113, 247)), {}, dry_run=True)
        keys = [k for k, _ in findings]
        assert not any("rebase-would:coder-pr113" in k for k in keys), keys
        retarget = [k for k in keys if k.startswith("mergeflow-retarget")]
        assert len(retarget) == 1 and "coder-pr247" in retarget[0], keys
        assert not created
    print("OK test_same_key_skip_and_retarget")


def test_debt_finding_threshold():
    """Debt above the limit gives a finding, within the limit it is silent."""
    with tempfile.TemporaryDirectory() as tmp:
        isolate(tmp)
        mod.review_state = lambda n: ("none", "")
        findings, _ = mod.post_merge_watch(make_info((1, 2, 3, 4)), {})
        assert any(k.startswith("mergeflow-debt") for k, _ in findings)
        findings, _ = mod.post_merge_watch(make_info((1, 2, 3)), {})
        assert not any(k.startswith("mergeflow-debt") for k, _ in findings)
    print("OK test_debt_finding_threshold")


def test_classify_stale():
    """A commit or a verdict inside the window clears staleness; None means
    "unknown"."""
    now = 1_000_000
    h = lambda n: now - n * 3600
    assert mod.classify_stale(h(50), None, now)          # old, no verdicts
    assert not mod.classify_stale(h(10), None, now)      # fresh commit
    assert not mod.classify_stale(h(50), h(10), now)     # fresh verdict
    assert mod.classify_stale(h(50), h(50), now)         # everything stale
    assert not mod.classify_stale(None, None, now)       # no data
    assert mod._iso_to_epoch("1970-01-01T00:00:00Z") == 0
    assert mod._iso_to_epoch("") is None
    assert mod._iso_to_epoch("junk") is None
    print("OK test_classify_stale")


def test_stale_scan_idempotent():
    """One triage card per PR: a live one by key suppresses a repeat, a
    dry-run creates no cards."""
    with tempfile.TemporaryDirectory() as tmp:
        created = []
        mod.create_triage_card = (
            lambda n, url, c, v: created.append(n) or (True, "ok"))
        prs = [{"number": 111, "updatedAt": "1970-01-01T00:00:00Z",
                "url": "http://x/111"},
               {"number": 222, "updatedAt": "1970-01-01T00:00:00Z",
                "url": "http://x/222"},
               {"number": 333, "updatedAt": "2100-01-01T00:00:00Z",
                "url": "http://x/333"}]
        mod.pr_activity = lambda n: (1.0, None, "", "")  # everything is stale
        mod.BOARD_DB = os.path.join(tmp, "kanban.db")
        con = __import__("sqlite3").connect(mod.BOARD_DB)
        con.execute("create table tasks (id text, status text,"
                    " idempotency_key text)")
        con.execute("insert into tasks values ('t1','ready',"
                    "'architect-pr111-triage-48h')")
        con.commit()
        con.close()
        findings, count = mod.stale_scan(prs, dry_run=True)
        assert count == 1, (findings, count)
        assert findings and "222" in findings[0][1] and "111" not in \
            findings[0][1], findings
        assert not created, "dry-run creates no cards"
        findings, count = mod.stale_scan(prs, dry_run=False)
        assert count == 1 and created == [222], (created, findings)
    print("OK test_stale_scan_idempotent")


def test_stack_lint():
    """A foreign base and a missing stack order give findings; a correct body
    is silent; the advice comment is posted once."""
    with tempfile.TemporaryDirectory() as tmp:
        posted = []
        mod.post_stack_comment = lambda c, p: posted.append((c, p))
        mod.stack_comment_present = lambda c, p: False
        prs = [
            {"number": 101, "baseRefName": "develop/2.x"},
            {"number": 189, "baseRefName": "feature/big-jump"},  # foreign base
            {"number": 205, "baseRefName": "develop/1.x"},
            {"number": 206, "baseRefName": "develop/1.x"},
        ]
        info = {
            101: {"body": "ordinary body", "baseRefName": "develop/2.x"},
            189: {"body": "stack: #87 -> #189", "baseRefName":
                  "feature/big-jump"},
            205: {"body": "stack: base develop/1.x", "baseRefName":
                  "develop/1.x"},
            206: {"body": "stack: base #205 -> #206, then #207",
                  "baseRefName": "develop/1.x"},
        }
        ancestors = {206: 205}              # 206 sits on 205
        findings = mod.stack_lint(prs, info, ancestors, dry_run=True)
        keys = [k for k, _ in findings]
        assert "mergeflow-stackbase:189" in keys, keys
        assert not any(k.startswith("mergeflow-stackbase:10") for k in keys)
        assert "mergeflow-stackorder:206-205" not in keys, keys  # body names it
        assert not posted, "dry-run posts nothing"
        # 101 violates nothing, but let it be a child that does not name its
        # parent:
        findings = mod.stack_lint(
            prs, info, {101: 100}, dry_run=True)
        assert "mergeflow-stackorder:101-100" in [k for k, _ in findings]
        # live: the advice is posted, a duplicate is suppressed by
        # stack_comment_present
        mod.stack_lint(prs, info, {101: 100}, dry_run=False)
        assert posted == [(101, 100)], posted
        posted.clear()
        mod.stack_comment_present = lambda c, p: True
        mod.stack_lint(prs, info, {101: 100}, dry_run=False)
        assert posted == [], "the duplicate comment is not posted"
    print("OK test_stack_lint")


def make_board_db(tmp, tasks=()):
    """A mini board for live_cards_by_pr:
    (id, status, title, key, branch, assignee)."""
    db = os.path.join(tmp, "kanban.db")
    con = __import__("sqlite3").connect(db)
    con.execute("create table tasks (id text primary key, title text,"
                " idempotency_key text, branch_name text, status text,"
                " assignee text)")
    for tid, status, title, key, branch, assignee in tasks:
        con.execute("insert into tasks values (?,?,?,?,?,?)",
                    (tid, title, key, branch, status, assignee))
    con.commit()
    con.close()
    return db


def test_live_cards_by_pr_all_carriers():
    """Branch > key > title; the body does not count; done/archived are dead;
    key conventions rebase-<N>-, owner-review-<pr>-, decomp by title;
    the assignee reaches the absorption addressee filter."""
    with tempfile.TemporaryDirectory() as tmp:
        old_db = mod.BOARD_DB
        mod.BOARD_DB = make_board_db(tmp, [
            ("t_rb198", "blocked", "[coder] Rebase PR #198 onto"
             " develop/2.x (head aecfaa7", "rebase-198-aecfaa7", None,
             "coder"),
            ("t_rw141", "blocked", "[coder] PR #141 rework 3",
             "owner-review-141-3c0824c", None, "coder"),
            ("t_rb224a", "todo", "Rebase PR #224 onto rebased #223 head",
             "rebase-224-538d6dc", None, "architect"),
            ("t_rb224b", "scheduled", "Rebase #224 onto rebased #223 head",
             "decomp-t_rb224a-rebase-224", None, None),
            ("t_dbl", "ready", "Rebase #244 onto rebased #223 head",
             None, None, "coder"),
            ("t_dead", "done", "Rebase PR #198 old", "rebase-198-old", None,
             "coder"),
            ("t_branch", "ready", "work on a branch without a number",
             None, "feat/242-ui", "tester"),
            ("t_bodyonly", "ready", "no number in the title",
             None, None, None),
        ])
        try:
            by = mod.live_cards_by_pr(
                {198: "b/198", 141: "b/141", 224: "b/224", 242: "feat/242-ui",
                 223: "b/223", 244: "b/244"})
            assert [c[0] for c in by.get(198, [])] == ["t_rb198"], by
            assert [c[0] for c in by.get(141, [])] == ["t_rw141"], by
            assert {c[0] for c in by.get(224, [])} == {
                "t_rb224a", "t_rb224b"}, by
            # the subject of a card: the FIRST number of the title, not the
            # smaller one
            assert [c[0] for c in by.get(244, [])] == ["t_dbl"], by
            assert "t_dbl" not in [c[0] for c in by.get(223, [])], by
            assert [c[0] for c in by.get(242, [])] == ["t_branch"], by
            assert 198 not in [n for n in by] or "t_dead" not in \
                [c[0] for c in by[198]], by
            # ranks: key (1) beats title (2), branch (0) beats all
            ranks = {c[0]: c[2] for c in by[224]}
            assert ranks["t_rb224a"] == 1, ranks
            assert ranks["t_rb224b"] == 2, ranks
            # the assignee (element 4) feeds the rebase addressee filter
            who = {c[0]: c[4] for c in by[224]}
            assert who["t_rb224a"] == "architect", who
            assert who["t_rb224b"] in (None, ""), who
            assert by[198][0][4] == "coder", by[198]
        finally:
            mod.BOARD_DB = old_db
    print("OK test_live_cards_by_pr_all_carriers")


def test_existing_card_gets_comment_not_new_card():
    """A live CODER card of kind rework/rebase: a comment into it, no new
    card is created (absorption of the work)."""
    with tempfile.TemporaryDirectory() as tmp:
        created, commented = [], []
        mod.MERGE_WATCH = os.path.join(tmp, "mw.json")
        mod.DEBT_CACHE = os.path.join(tmp, "debt.json")
        mod.line_heads = lambda: dict(HEADS)
        mod.live_rebase_cards = lambda: {}
        mod.create_rebase_card = lambda *a, **k: (
            created.append(a) or (True, "ok"))
        mod.comment_card = lambda tid, text: commented.append((tid, text)) \
            or True
        mod.review_state = lambda n: ("none", "")
        info = make_info((224,))
        by_pr = {224: [("t_rb224a", "Rebase PR #224 ...", 1,
                        "rebase-224-538d6dc", "coder")]}
        findings, total = mod.post_merge_watch(
            info, {}, by_pr=by_pr, dry_run=True)
        assert total == 1 and not created and not commented
        assert any(k.startswith("mergeflow-existing-would:224:t_rb224a")
                   for k, _ in findings), findings
        findings, _ = mod.post_merge_watch(info, {}, by_pr=by_pr,
                                           dry_run=False)
        assert not created, "no new card is created"
        assert commented and commented[0][0] == "t_rb224a", commented
        assert "2222222" in commented[0][1], commented
        assert "absorbs the rebase" in commented[0][1], commented
    print("OK test_existing_card_gets_comment_not_new_card")


def test_wrong_addressee_gets_rebase_card_not_comment():
    """A rebase is absorbed only by a live coder card of kind rework/rebase.
    Visual-delta, reviewer, decomposition-tester, architect-rebase and
    assignee-less cards do NOT get the comment: a rebase card (key
    coder-pr<N>-rebase-<sha7>) goes out instead, "one card = one PR" does not
    apply here."""
    with tempfile.TemporaryDirectory() as tmp:
        created, commented = [], []
        mod.MERGE_WATCH = os.path.join(tmp, "mw.json")
        mod.DEBT_CACHE = os.path.join(tmp, "debt.json")
        mod.line_heads = lambda: dict(HEADS)
        mod.live_rebase_cards = lambda: {}
        mod.create_rebase_card = lambda *a, **k: (
            created.append(a) or (True, "ok"))
        mod.comment_card = lambda tid, text: commented.append((tid, text)) \
            or True
        mod.review_state = lambda n: ("none", "")
        info = make_info((218, 203, 150, 187, 224, 206))
        by_pr = {
            # coder, but kind visual-delta: does not absorb
            218: [("t_vd218",
                   "[visual-delta] PR #218: Before / After section", 2,
                   "visual-delta-218", "coder")],
            # assignee reviewer, kind re-review: does not absorb
            203: [("t_rev203", "[reviewer] PR #203: verdict review", 1,
                   "reviewer-pr203-252825d", "reviewer")],
            # kind rework, but the assignee is empty: does not absorb
            150: [("t_rw150", "PR #150 rework after review", 1,
                   "owner-review-150-a1b2c3d", "")],
            # kind rework, but the assignee is architect: does not absorb
            187: [("t_rw187", "PR #187 rework after review", 1,
                   "owner-review-187-d4e5f6a", "architect")],
            # kind rebase, but the assignee is architect: does not absorb
            224: [("t_rb224a", "Rebase PR #224 onto rebased #223 head", 1,
                   "rebase-224-538d6dc", "architect")],
            # a tester decomposition card: does not absorb
            206: [("t_dec206",
                   "Analyze the lockfile conflict in PR #206", 2,
                   "decomp-t_parent-lockfile-pr-206-5-npm", "tester")],
        }
        findings, total = mod.post_merge_watch(
            info, {}, by_pr=by_pr, dry_run=True)
        assert total == 6, total
        assert not commented, commented
        would = [k for k, _ in findings
                 if k.startswith("mergeflow-rebase-would:")]
        assert len(would) == 6, findings
        for n in (218, 203, 150, 187, 224, 206):
            assert "mergeflow-rebase-would:coder-pr%d-rebase-2222222" % n \
                in would, (n, would)
        # no false addressee gets an existing-plan
        assert not [k for k, _ in findings
                    if k.startswith("mergeflow-existing-would")], findings
        findings, _ = mod.post_merge_watch(info, {}, by_pr=by_pr,
                                           dry_run=False)
        assert not commented, "no comment goes into a foreign kind"
        assert len(created) == 6, created
        made_keys = sorted(mod.rebase_key(a[0], a[2]) for a in created)
        assert made_keys == sorted(
            "coder-pr%d-rebase-2222222" % n
            for n in (218, 203, 150, 187, 224, 206)), made_keys
        assert any("t_vd218" in t for _, t in findings) is False
    print("OK test_wrong_addressee_gets_rebase_card_not_comment")


def test_duplicate_rebase_shown():
    """Two live rebase cards -> a finding; verify/force-push next to them do
    not count as a rebase."""
    by_pr = {
        224: [
            ("t_rb224a", "Rebase PR #224 onto rebased #223 head", 1,
             "rebase-224-538d6dc", "architect"),
            ("t_rb224b", "Rebase #224 onto rebased #223 head", 2,
             "decomp-...", None),
            ("t_ver224", "Adversarially verify rebased #224 branch", 2,
             "decomp-...", "tester"),
            ("t_fp224", "Force-push rebased #224 and update PR", 2,
             "decomp-...", "coder"),
        ],
        141: [("t_rw141", "[coder] PR #141 rework 3", 1,
               "owner-review-141-3c0824c", "coder")],
    }
    findings = mod.duplicate_rebase_findings(by_pr)
    keys = [k for k, _ in findings]
    assert keys == ["mergeflow-duplicate-rebase:224"], findings
    text = findings[0][1]
    assert "t_rb224a" in text and "t_rb224b" in text, text
    assert "t_ver224" not in text and "t_fp224" not in text, text
    print("OK test_duplicate_rebase_shown")


def test_effective_age_stop_window():
    now = 1_000_000.0
    stop = now - 8 * 3600
    # activity before the stop: ages only until the stop began
    assert mod.effective_age_s(now - 60 * 3600, now, stop) == 52 * 3600
    # activity during the stop: does not age at all
    assert mod.effective_age_s(now - 2 * 3600, now, stop) == 0.0
    # no stop: the full age
    assert mod.effective_age_s(now - 60 * 3600, now, None) == 60 * 3600
    print("OK test_effective_age_stop_window")


def test_stale_scan_rework_vs_triage_and_stop():
    """Rework for an unanswered verdict, triage without a verdict, a live
    card silences, the stop window is subtracted from 48 h."""
    import json as _json
    with tempfile.TemporaryDirectory() as tmp:
        made = []
        mod.STOP_META = os.path.join(tmp, "stop.meta.json")
        _json.dump({"since": time.time() - 8 * 3600},
                   open(mod.STOP_META, "w"))
        mod.BOARD_DB = make_board_db(tmp, [])
        mod.create_rework_card = lambda n, u, h, v: made.append(
            ("rework", n, h[:7])) or (True, "ok")
        mod.create_triage_card = lambda n, u, c, v: made.append(
            ("triage", n, None)) or (True, "ok")
        now = time.time()
        old = lambda hours: now - hours * 3600
        # 111: verdict rejected 60 h ago (newer than the commits), stop 8 h
        # -> effective 52 h >= 48: rework. 222: no verdict, commits 60 h:
        # triage. 333: commits 60 h, a live card -> silenced. 444: commits
        # 40 h -> not stale. 555: commits 5 h -> not stale.
        prs = [{"number": n, "updatedAt": "2000-01-01T00:00:00Z",
                "url": "http://x/%d" % n} for n in (111, 222, 333, 555)]
        prs.append({"number": 444,
                    "updatedAt": "2000-01-01T00:00:00Z",
                    "url": "http://x/444"})
        acts = {
            111: (old(60), old(58), "rejected", "abcd" * 10),
            222: (old(60), None, "", "bbbb" * 10),
            333: (old(60), None, "", "cccc" * 10),
            444: (old(40), None, "", "dddd" * 10),
            555: (old(5), None, "", "eeee" * 10),
        }
        mod.pr_activity = lambda n: acts[n]
        by_pr = {333: [("t_x", "PR #333 work", 2, "")]}
        findings, stale = mod.stale_scan(prs, by_pr=by_pr, dry_run=False)
        assert stale == 2, (stale, findings)
        kinds = {(kind, n) for kind, n, _ in made}
        assert kinds == {("rework", 111), ("triage", 222)}, made
        assert made[0][2] == ("abcd" * 10)[:7], made  # key from head40
        # 555: updatedAt is old, but the commit is newer than the stop -> not
        # stale; 444: commits younger than 48 h -> not stale; 333: a live
        # card -> silence.
        # Now a longer stop: the same 60 h of commits give effective 40 h < 48.
        _json.dump({"since": time.time() - 20 * 3600},
                   open(mod.STOP_META, "w"))
        made.clear()
        findings, stale = mod.stale_scan(
            [{"number": 222, "updatedAt": "2000-01-01T00:00:00Z",
              "url": ""}], by_pr={}, dry_run=False)
        assert stale == 0 and not made, (stale, made, findings)
    print("OK test_stale_scan_rework_vs_triage_and_stop")


def test_rework_key_format():
    key = "coder-pr111-rework-abcdef1"
    assert re.match(r"^coder-pr(\d+)-rework-([0-9a-f]{7})$", key)
    # the carrier coder-pr<N>- is visible to the factory-metrics rule
    assert mod.factory_metrics.KEY_PR_RE.match(key)
    print("OK test_rework_key_format")


if __name__ == "__main__":
    test_rebase_priority_order()
    test_rebase_key_format()
    test_bootstrap_plan()
    test_no_merge_no_cards()
    test_merge_creates_cards_and_writes_state()
    test_same_key_skip_and_retarget()
    test_debt_finding_threshold()
    test_classify_stale()
    test_stale_scan_idempotent()
    test_stack_lint()
    test_live_cards_by_pr_all_carriers()
    test_existing_card_gets_comment_not_new_card()
    test_wrong_addressee_gets_rebase_card_not_comment()
    test_duplicate_rebase_shown()
    test_effective_age_stop_window()
    test_stale_scan_rework_vs_triage_and_stop()
    test_rework_key_format()
    print("ALL PASS")
