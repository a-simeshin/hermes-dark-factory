#!/usr/bin/env python3
"""Merge-unblocker priority: a card whose only remaining work is a merge formality.

Fixture: six cards that close the last merge preconditions of a PR whose code
the owner accepted (verdict "CHANGES REQUESTED: minor", only update-branch,
"What changes for you", Before / After, green CI left). An older
[visual-delta] card at p80 and a set of negatives (a code blocker, a test
blocker, merge prerequisites without a mark of accepted code, an umbrella card,
a candidate next to a live test blocker of the same PR, a done card with a code
blocker) must stay untouched. After one pass the six are raised to
MERGE_UNBLOCKER_PRIO, everything else stays at p80, and in dispatcher order
(priority DESC, created_at ASC) the raised cards go before the older
visual-delta card.

Mutants (each must turn the test red):
  M_ACCEPT  accepted-code mark always true -> the synthetic "minor without
            accepted code" card is raised;
  M_FORMAL  formality classes never match -> the six are not raised;
  M_CROSS   cross-check "the PR has no live code blockers" off -> the
            candidate is raised next to a live test blocker of the same PR;
  M_STATUS  active-status filter of neighbours empty -> an active test blocker
            is ignored;
  M_TITLE   code-work marker in the neighbour's title off -> a test blocker
            without a BLOCK line no longer holds the candidate.
"""
import importlib.util
import os
import shutil
import sqlite3
import sys
import tempfile
import time

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
HOME = tempfile.mkdtemp(prefix="mu-home-")
os.environ["HERMES_HOME"] = HOME
os.environ["FACTORY_REPO"] = "acme/widget"
os.environ["FACTORY_REPO_PATH"] = "/srv/hermes/widget"
os.environ["FACTORY_BOARD"] = "main"
os.environ["FACTORY_OWNER_LOGIN"] = "owner-login"
os.environ["FACTORY_OWNER_CHAT"] = "telegram:000000000"
os.environ["FACTORY_BASE_BRANCHES"] = "develop/1.x,develop/2.x"
# An external copy (a hand-made mutant) can be checked directly:
#   BW_WATCHDOG_PATH=/tmp/mutant.py python3 test_board_watchdog_merge_unblocker.py
# Without the variable the script next to this test is checked.
BW_PATH = os.environ.get("BW_WATCHDOG_PATH") or os.path.join(
    SCRIPTS, "board-watchdog.py")
SLUG = "mufix"
NOW = int(time.time())
H = 3600

# --- card bodies ----------------------------------------------------------

BODY_284 = """Rework per owner review verdict on PR #284 (owner-login, head c178d32b08260e9fc8728062fc78fdf10d7423c7), CHANGES REQUESTED: minor. 2 BLOCK, one card.

Context: port parity with #245 is accepted (same emitFromBrowser/emitFromTarget shape, the mutants of #245 apply, including the unit-test gap on emitFromTarget that only an integration test catches). Owner decision: #284 and #245 are merged together (twin rule), do not close the branch.

BLOCK 1: the PR has no "What changes for you" comment.
BLOCK 2: develop/1.x moved 2 commits ahead (fix(ui) sidebar caps, feat(ui) form validation): update-branch (merge develop/1.x into the PR branch), then a green CI on the updated head.

Closed and verified by the reviewer (do not touch): ## Before / After is there, CI is green on c178d32 (4/4 jobs), reviewer APPROVED on c178d32.

Acceptance criteria:
1. A "What changes for you" comment is posted on PR #284: the fix for the frame lost on concurrent POSTs to one proxy session.
2. The PR branch is updated with a merge of develop/1.x; mergeStateStatus CLEAN.
3. CI is green on the new head.
4. A reply in the PR (English) with the new head SHA."""

BODY_166 = """Owner review verdict on PR #166 (fix(ui): reconnect after silent SSE drop resets stale session before connect), head 7e0f9ea: CHANGES REQUESTED: minor. Code is accepted: round-5 setLoggingLevel leak closed, re-review APPROVED, CI green on 7e0f9ea, WCFY present, Refs #157. Only merge prerequisites remain. Owner decision: #166 and #177 merge together.

Item 1 (the Before / After section) is covered by t_vd166: the duplicate was removed by the verdict dedup gate, the work is in that card.

## Blocking item 2: update-branch + green CI
- run update-branch (merge the develop/2.x tip into the PR branch)
- wait for a green CI run on the updated head
- report the new head in a PR comment

## Acceptance criteria
- branch carries the develop/2.x tip (update-branch), CI green on the new head
- no production code changes (round-5 code is accepted)"""

BODY_177 = """Issue: #157

Owner review verdict on PR #177 (head 1d8f11f): CHANGES REQUESTED (minor), code accepted, only merge prerequisites remain.

BLOCK: develop/1.x moved 4 commits past this head (verified live: compare 1d8f11f...develop/1.x ahead_by=4, PR state MERGEABLE/CLEAN).

Steps:
1. gh pr update-branch 177 --repo acme/widget
2. wait for the CI run on the updated head to go fully green
3. post the new head sha in the PR thread

No code changes expected: merge only. If the merge conflicts or CI goes red on the updated base, do not force anything: post findings in this card and block it.

Related: Before/After section blocker is t_vd177. Owner decision: #177 merges together with #166."""

BODY_273 = """Owner review verdict on PR #273 (CHANGES REQUESTED: minor, head 486d248). The fix itself is accepted; only merge prerequisites remain.

Blocking:
1. [BLOCK] No "What changes for you" comment on the PR. Post it (English, brief: user-visible effect of the RFC 6901 decode-order fix).
2. [BLOCK] develop/1.x moved 5 commits past this head. Update the branch (merge develop/1.x into the PR branch), push, get a green CI run on the updated head.

Constraints:
- No agent merges this PR. The owner merges #272 and #273 together himself after both are ready.
- Already verified by owner, no need to redo: decode order per RFC 6901, mutation checks red, CI green on 486d248, Before/After present, Refs #261.

Acceptance criteria:
- "What changes for you" comment posted on PR #273 in English.
- Branch updated onto current develop/1.x, CI green on the new head."""

BODY_272 = """Owner review on PR #272 (head 1ace4f5): CHANGES REQUESTED minor. The fix itself is accepted (reviewer PASS on 1ace4f5, mutants killed, CI 5/5 green). Only merge prerequisites remain.

## Blocking (both from the verdict)

1. [BLOCK] No "What changes for you" comment on PR #272.
   - Post a user-facing WCFY comment on the PR.
2. [BLOCK] develop/2.x moved 7 commits past head 1ace4f5.
   - Run update-branch, then get a green CI run on the updated head (all 5 checks). Report the new head SHA.

## Constraints

- DO NOT MERGE. Owner decision: he merges #272 and #273 together himself after both are ready.
- The fix code is accepted: no changes to schemaUtils.ts / tests / NOTICE.d.

## Acceptance criteria

- WCFY comment visible on PR #272.
- PR branch updated past current develop/2.x; CI green on the new head."""

BODY_277 = """Repo: /srv/hermes/widget. PR #277 (branch base develop/1.x, head e2a540f), issue #254 (reopened), twin of merged PR #205. Owner verdict: CHANGES REQUESTED: minor. The code is accepted; owner re-ran the mutation matrix row himself (5 of 67 red, base 67/67 green).

BLOCK 1: No `## Before / After` section in the PR body. There is no visible UI change, so a mermaid state diagram is the right form: before, a failed connect leaves serverImplementation/serverCapabilities from the previous session next to status=error; after, every failure path and disconnect clears both.
BLOCK 2: Merge prerequisites: (a) post the "What changes for you" comment in the PR; (b) update-branch with a green CI run on the updated head (branch is 4 commits behind develop/1.x).

Closed and verified by owner (do not redo): PATCH markers, NOTICE.d entry, red-on-base tests, Refs to the reopened #254.

AC: `## Before / After` section with a mermaid state diagram added to the PR body; WCFY comment posted; branch updated from develop/1.x with green CI on the updated head. Do NOT merge."""

BODY_154 = """Owner BLOCK 3 of the major verdict on PR #154 (head ff6dcdf).

Finding (owner live run): the error path throws HttpMessageNotWritableException. The failure body is written as JSON into an SSE response.

Task: deliver the error in a form the SSE client can read: an SSE `error` event on the stream, or an HTTP status delivered before the stream starts. Choose one form, implement it, and make the client able to read it.

Test requirement: a test covering the error path that FAILS on the current head ff6dcdf and passes after the fix.

Acceptance:
- Error-path regression test named, verified red on ff6dcdf, green after the fix, CI green (5 checks).
- The chosen error form is documented in the PR body of the rework round."""

BODY_286B1 = """Owner review PR #286, BLOCK 1 (head 46a6b72).

The test emitSessionEvent_afterSinkTerminated_returnsFalse is declared as "rejected without retry", but the "without retry" part is not verified.

Mutant (verified by the owner): the loop condition result == FAIL_NON_SERIALIZED is replaced with !result.isSuccess(), that is, a retry on every failure.

Needed: the test fails on this mutant. Options from the review: assert that the call returns well before the budget, or count the tryEmitNext calls.

Parity: the same test is byte-identical in the twin #285 (2.x). The pair is merged together after a re-review on the final heads.

Acceptance criteria:
- the !result.isSuccess() mutant turns the test red;
- the same test fix is made in #285, line parity is kept."""

BODY_286B3 = """Owner review PR #286, BLOCK 3 (head 46a6b72): merge prerequisites.

1. WCFY: the "What changes for you" comment in the PR (a post-PASS step of the PR deliverable rules).
2. update-branch: the current green run precedes #258 and #181 in the base; update the branch and get a green CI on the updated head before the merge.
3. Owner decision: the pair #286/#285 is merged together, after a re-review of both on the final heads.

Acceptance criteria:
- the WCFY comment is published in the PR;
- the branch is updated, CI is green on the updated head;
- merge only as a pair with #285 and only after a re-review of both."""

BODY_285UMB = """Owner verdict (pull_request_review, CHANGES REQUESTED: minor) on PR #285, head 21c566b. Origin card: t_pr285orig (done, reviewer round-1 approved). This verdict supersedes the merge-readiness of that approval, not the fix itself.

Accepted by owner: the retry is correct, bounded, and cannot duplicate a frame. Owner reproduced the reviewer's mutant and ran one more of his own.

Blocks (one child card each, except block 2 which already has a card):

1. Child card (coder, p80): `emitSessionEvent_afterSinkTerminated_returnsFalse` does not verify the "without retry" part. Owner's mutant: loop condition changed to !result.isSuccess() stays 3 of 3 green. The test must fail on this mutant.
2. Existing card t_vd285 (visual-delta batch, ready): PR body needs a `## Before / After` section.
3. Child card (ops, p80): merge prerequisites. A "What changes for you" comment on the PR, and update-branch with a green CI run."""

BODY_VD = """Before / After section for PR #249 (issue #229): carry it over as text and images.

Tasks:
- capture Before on base (develop/1.x): the current filter behaviour;
- capture After on the PR head: the new behaviour;
- insert the section into the PR body, attach links to the stands.

Criteria: the section is in the PR body, both pictures are available without a login, captions Base/PR head."""

BODY_N5 = """Owner verdict: CHANGES REQUESTED: minor. The code will be re-reviewed after the formalities, the review is not finished.

1. [BLOCK] No "What changes for you" comment on the PR.
2. [BLOCK] develop/2.x moved 3 commits past this head. Update the branch and get a green CI run."""

BODY_N6C = """Owner verdict on PR #290: CHANGES REQUESTED: minor. The fix is accepted; only merge prerequisites remain.

1. [BLOCK] No "What changes for you" comment on the PR.
2. [BLOCK] develop/2.x moved 2 commits past this head. Update the branch."""

BODY_N6T = """Owner review PR #290, BLOCK 1 (head abc1245).

The test emitSessionEvent_afterSinkTerminated_returnsFalse does not catch the retry-on-every-failure mutant: it needs an assert under the budget or a tryEmitNext counter on the wrapping sink.

Acceptance criteria:
- the mutant turns the test red;
- ./mvnw verify is green."""

BODY_DONE284 = """BLOCK 1: rework the error delivery into an SSE event, a test for the error path."""

# (tid, title, body, assignee, status, prio, created_at)
CARDS = [
    # an old visual-delta card: OLDER than all, p80, not a candidate
    ("t_vd249", "[visual-delta] PR #249: Before / After section (issue #229)",
     BODY_VD, "coder", "ready", 80, NOW - 30 * H),
    # the sixth card of the same criterion, older than the five
    ("t_pr277", "PR #277 rework: Before/After mermaid + merge prerequisites"
     " (owner review, head e2a540f)", BODY_277, "coder", "ready", 80,
     NOW - 18.6 * H),
    # the five
    ("t_pr284", "[coder] PR #284 rework per verdict (head c178d32):"
     " What-changes-for-you + update-branch develop/1.x", BODY_284,
     "coder", "ready", 80, NOW - 3.2 * H),
    ("t_pr166", "[coder] PR #166 rework: merge prerequisites, update-branch"
     " (head 7e0f9ea)", BODY_166, "coder", "ready", 80, NOW - 2.9 * H),
    ("t_pr177", "[coder] PR #177 rework: update-branch + green CI"
     " (head 1d8f11f)", BODY_177, "coder", "ready", 80, NOW - 2.8 * H),
    ("t_pr273", "rework(ui): PR #273 merge prerequisites at 486d248:"
     " What-changes-for-you + update-branch", BODY_273, "coder", "ready",
     80, NOW - 1.7 * H - 60),
    ("t_pr272", "[coder] PR #272 rework: What-changes-for-you +"
     " update-branch develop/2.x + green CI (owner review, head 1ace4f5)",
     BODY_272, "coder", "ready", 80, NOW - 1.7 * H),
    # negatives
    ("t_pr154", "[coder] PR #154 block3: SSE-readable error delivery"
     " instead of HttpMessageNotWritableException (head ff6dcdf)", BODY_154,
     "coder", "ready", 80, NOW - 20.4 * H),
    ("t_pr286b1", "PR #286 BLOCK1: test afterSinkTerminated does not catch"
     " the retry-on-every-failure mutant", BODY_286B1, "coder", "ready", 80,
     NOW - 18.6 * H),
    ("t_pr286b3", "PR #286 BLOCK3: merge prerequisites (WCFY, update-branch,"
     " green CI, merge as a pair with #285)", BODY_286B3, "coder", "ready",
     80, NOW - 18.6 * H),
    ("t_pr285", "PR #285 rework umbrella: owner review,"
     " head 21c566b (CHANGES REQUESTED minor, 3 BLOCK)", BODY_285UMB,
     "coder", "ready", 80, NOW - 18.6 * H),
    # a running visual-delta card of the same PR #166: live, but no code items
    ("t_vd166", "[visual-delta] PR #166: Before / After section",
     "Tasks: capture Before/After on base and PR head of PR #166, insert"
     " the section.", "coder", "running", 80, NOW - 10 * H),
    # a partner of #177 from a PAST review round (review): a test in the
    # title, but the work is closed by the later verdict "code accepted",
    # it must not hold the candidate
    ("t_pr177old", "[coder] PR #177 minor: sessionId:undefined test cannot"
     " fail (head 7c48f92)",
     "PR #177 round-5: the sessionId undefined test cannot fail. The later"
     " verdict \"code accepted\" supersedes this round.",
     "coder", "review", 80, NOW - 34.6 * H),
    # a partner of #166 from a past round (review): a test in the title,
    # does not block
    ("t_pr166old", "Port 1.x setLoggingLevel cleanup to kill"
     " order-dependent useConnection test (PR #166 r5)",
     "Port of the setLoggingLevel cleanup to 1.x, the test is"
     " order-dependent (PR #166).",
     "reviewer", "review", 80, NOW - 34.6 * H),
    # synthetic: minor, formalities, code NOT accepted
    ("t_n5", "[coder] PR #291 rework: WCFY + update-branch (owner review)",
     BODY_N5, "coder", "ready", 80, NOW - 2 * H),
    # synthetic: candidate of #290 next to a live test blocker of the same PR
    ("t_n6c", "[coder] PR #290 rework: WCFY + update-branch (owner review)",
     BODY_N6C, "coder", "ready", 80, NOW - 1 * H),
    ("t_n6t", "[coder] PR #290 BLOCK1: test does not catch the mutant",
     BODY_N6T, "coder", "ready", 80, NOW - 1 * H - 60),
    # a done card of #284 with a code blocker: done is not live, no hold
    ("t_done284", "[coder] PR #284: SSE error event (closed)",
     BODY_DONE284, "coder", "done", 80, NOW - 40 * H),
]

POSITIVE = ["t_pr277", "t_pr284", "t_pr166", "t_pr177",
            "t_pr273", "t_pr272"]
FIVE = ["t_pr284", "t_pr166", "t_pr177", "t_pr273", "t_pr272"]
NEGATIVE = ["t_vd249", "t_pr154", "t_pr286b1", "t_pr286b3",
            "t_pr285", "t_vd166", "t_n5", "t_n6c", "t_n6t"]


def make_board(root):
    bdir = os.path.join(root, "boards", SLUG)
    os.makedirs(bdir, exist_ok=True)
    con = sqlite3.connect(os.path.join(bdir, "kanban.db"))
    con.execute("""CREATE TABLE tasks (
        id TEXT PRIMARY KEY, title TEXT, body TEXT, assignee TEXT,
        status TEXT NOT NULL, priority INTEGER DEFAULT 0,
        created_by TEXT, created_at INTEGER NOT NULL,
        started_at INTEGER, completed_at INTEGER)""")
    con.execute("""CREATE TABLE task_comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
        author TEXT NOT NULL, body TEXT NOT NULL,
        created_at INTEGER NOT NULL)""")
    for tid, title, body, assignee, status, prio, created in CARDS:
        con.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,NULL,NULL)",
                    (tid, title, body, assignee, status, prio, "user",
                     int(created)))
    con.commit()
    con.close()
    return bdir


class Recorder(object):
    def __init__(self, mod):
        self.mod = mod
        self.calls = []

    def __call__(self, cmd, timeout=120):
        self.calls.append(cmd)
        return "ok"


def load_bw(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def prepare(root, src_path=BW_PATH, name="bw_mu"):
    bdir = make_board(root)
    mod = load_bw(src_path, name)
    mod.BOARDS = os.path.join(root, "boards")
    rec = Recorder(mod)
    mod.run = rec
    return mod, rec, bdir


def edits(rec):
    """(tid, priority) for every recorded edit --priority command."""
    out = []
    for cmd in rec.calls:
        if "edit" in cmd and "--priority" in cmd:
            i = cmd.index("edit")
            out.append((cmd[i + 1], int(cmd[cmd.index("--priority") + 1])))
    return out


def run_gate(mod, rec):
    """One gate pass plus a simulated apply of the edits to the database
    (as the live CLI does). Returns findings and tid -> priority after it."""
    findings = mod.merge_unblocker_gate(SLUG)
    con = sqlite3.connect(os.path.join(mod.BOARDS, SLUG, "kanban.db"))
    for tid, prio in edits(rec):
        con.execute("UPDATE tasks SET priority = ? WHERE id = ?", (prio, tid))
    con.commit()
    prios = dict(con.execute("SELECT id, priority FROM tasks"))
    con.close()
    return findings, prios


def dispatch_order(prios):
    """Dispatcher order: priority DESC, created_at ASC (the core)."""
    order = sorted(CARDS, key=lambda c: (-prios[c[0]], c[6], c[0]))
    return [c[0] for c in order]


# The merge-unblocker priority is read from the script, not hardcoded.
EXPECT_PRIO = 100


def check_positive(prios):
    problems = []
    for tid in POSITIVE:
        if prios.get(tid) != EXPECT_PRIO:
            problems.append("not raised %s (p=%s)" % (tid, prios.get(tid)))
    for tid in NEGATIVE:
        if prios.get(tid) == EXPECT_PRIO:
            problems.append("negative raised %s" % tid)
    order = dispatch_order(prios)
    first6 = set(order[:6])
    if first6 != set(POSITIVE):
        problems.append("the first six are not the positives: %s" % order[:8])
    if not set(FIVE) <= first6:
        problems.append("the five are not among the first six")
    if "t_vd249" in order[:6]:
        problems.append("the old visual-delta card went before the raised")
    return problems, order


def main():
    global EXPECT_PRIO
    failures = []
    root = tempfile.mkdtemp(prefix="mu-")
    try:
        mod, rec, _ = prepare(os.path.join(root, "main"))
        EXPECT_PRIO = mod.MERGE_UNBLOCKER_PRIO

        # 1. dry-run: findings, no mutations
        findings = mod.merge_unblocker_gate(SLUG, apply=False)
        dry_tids = sorted(f[0].split(":")[-1] for f in findings)
        if dry_tids != sorted(POSITIVE):
            failures.append("dry-run: %s" % dry_tids)
        if rec.calls:
            failures.append("dry-run: CLI calls were made: %s" % rec.calls)

        # 2. apply: the six are raised, the rest is not
        findings, prios = run_gate(mod, rec)
        problems, order = check_positive(prios)
        if problems:
            failures.append("apply: %s" % problems)
        comments = [c for c in rec.calls if "comment" in c]
        if len(comments) != len(POSITIVE):
            failures.append("marker comments %d, expected %d"
                            % (len(comments), len(POSITIVE)))
        for c in comments:
            if "merge-unblocker" not in " ".join(c):
                failures.append("comment without the marker: %s" % c)

        # 3. idempotency: a repeated pass is silent
        rec.calls = []
        findings2, prios2 = run_gate(mod, rec)
        if rec.calls:
            failures.append("repeated pass is not silent: %s" % edits(rec))
        if findings2:
            failures.append("repeated pass gave findings: %s" % findings2)

        # 3b. the same pair of #290, the code card went to done: the
        # candidate is raised. Before the flip the ready test card held
        # t_n6c at p80; after it the "last preconditions" hold.
        con = sqlite3.connect(os.path.join(mod.BOARDS, SLUG, "kanban.db"))
        con.execute("UPDATE tasks SET status='done' WHERE id='t_n6t'")
        con.commit()
        con.close()
        rec.calls = []
        findings3, prios3 = run_gate(mod, rec)
        if prios3.get("t_n6c") != EXPECT_PRIO:
            failures.append("pair with a done code card: t_n6c not raised"
                            " (p=%s)" % prios3.get("t_n6c"))
        if sorted(t for t, _ in edits(rec)) != ["t_n6c"]:
            failures.append("after the flip not only t_n6c was raised: %s"
                            % edits(rec))
        comments3 = [" ".join(c) for c in rec.calls if "comment" in c]
        if not any("merge-unblocker" in c and "t_n6c" in c
                   for c in comments3):
            failures.append("after the flip no marker comment on t_n6c")

        # 4. mutants
        mutants = [
            ("M_ACCEPT",
             'MERGE_UNBLOCKER_ACCEPTED_RE = re.compile(\n'
             '    r"(?i:code is accepted|code accepted|change (?:is )?'
             'accepted"\n'
             '    r"|fix (?:itself )?is accepted"\n'
             '    r"|only merge prerequisites remain)|\\bAPPROVED\\b")',
             'MERGE_UNBLOCKER_ACCEPTED_RE = re.compile(r"[\\s\\S]*")',
             "a minor verdict without accepted code is raised"),
            ("M_FORMAL",
             '        if rx.search(text or ""):\n'
             '            return name',
             '        if False:\n'
             '            return name',
             "the six are not raised"),
            ("M_CROSS",
             "            if not belongs:\n"
             "                continue",
             "            if not belongs or True:\n"
             "                continue",
             "the candidate is raised next to a live test blocker"),
            ("M_STATUS",
             'if live_status[oid] not in ("ready", "running", "todo"):',
             'if live_status[oid] not in ():',
             "the candidate is raised, an active test blocker is ignored"),
            ("M_TITLE",
             "if not code_left and MERGE_UNBLOCKER_CODE_TITLE_RE.search(",
             "if not code_left and False"
             " and MERGE_UNBLOCKER_CODE_TITLE_RE.search(",
             "a test blocker without a BLOCK line no longer holds"),
        ]
        for mname, old, new, expect in mutants:
            src = open(BW_PATH).read()
            if src.count(old) != 1:
                failures.append("%s: the anchor occurs %d times"
                                % (mname, src.count(old)))
                continue
            mroot = os.path.join(root, mname.lower())
            os.makedirs(mroot, exist_ok=True)
            mpath = os.path.join(mroot, "board-watchdog.py")
            open(mpath, "w").write(src.replace(old, new))
            mmod, mrec, _ = prepare(mroot, src_path=mpath,
                                    name="bw_mu_" + mname.lower())
            _, mprios = run_gate(mmod, mrec)
            mprobs, _ = check_positive(mprios)
            if not mprobs:
                failures.append("%s NOT caught: %s" % (mname, expect))
            else:
                print("  mutant %s caught: %s" % (mname, mprobs[0]))
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(HOME, ignore_errors=True)

    print("=" * 64)
    if failures:
        for f in failures:
            print("FAIL %s" % f)
        sys.exit(1)
    print("PASS merge-unblocker: six cards raised to p%d, the old "
          "visual-delta card and the negatives stay at p80, dispatcher order "
          "puts the raised first, idempotent, after the partner's done flip "
          "the candidate is raised, 5 mutants caught." % EXPECT_PRIO)


if __name__ == "__main__":
    main()
