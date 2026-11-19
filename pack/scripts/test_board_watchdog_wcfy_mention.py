#!/usr/bin/env python3
"""A mention of WCFY in a verdict is not a WCFY.

Verdicts "APPROVED on head f9897fe" (PR 243) and "PASS on branch head
819e741" (PR 317) contain the phrase "What changes for you" in prose ("the
`## What changes for you` section is now due on this final head", "the
`## What changes for you` comment is published by the coder after this
PASS"). A substring search in _wcfy_latest made wcfy_at newer than the head
and silenced the arm of branch B: two accepted PRs with green CI stood
without a WCFY and without a card for it.

The rule: a WCFY is a comment that starts with the header line
"## What changes for you". PR 243 has a real WCFY from long ago (it starts
with the header), but it is OLDER than the head: a line from a verdict must
not make it look fresh.

Since the switch to "one PR = one card" the watchdog no longer arms a WCFY
card per head: the review-layer scenarios below check that no card is created
for these heads, and that a PASS head without a live card and without a
formal owner review gives only the "milestone PR without a card" finding.

Fixed dates rot (the arm window is 14 days): the bodies are verbatim-style,
the dates are shifted relative to "now" keeping the live intervals (offsets
in minutes from the head, see OFF_* below).

This file is self-contained: the review-layer scenario helpers it needs
(a mini board, a fake run(), a PR JSON builder) are defined below.
"""
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone

# Project settings are read at import time: set neutral values first. The
# Hermes home is a temp directory: the test never touches ~/.hermes.
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="bw-test-home-")
os.environ.update({
    "FACTORY_REPO": "acme/widget",
    "FACTORY_REPO_PATH": "/srv/hermes/widget",
    "FACTORY_BOARD": "main",
    "FACTORY_OWNER_LOGIN": "owner-login",
    "FACTORY_OWNER_CHAT": "telegram:000000000",
    "FACTORY_BASE_BRANCHES": "develop/1.x,develop/2.x",
})

HERE = os.path.dirname(os.path.abspath(__file__))
# Mutation runs use a COPY of the script, not the production one.
SCRIPT = os.environ.get("BOARD_WATCHDOG_SCRIPT") or os.path.join(
    HERE, "board-watchdog.py")

spec = importlib.util.spec_from_file_location("board_watchdog", SCRIPT)
if spec is None or spec.loader is None:
    print("SKIP: cannot load board-watchdog.py")
    sys.exit(0)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def iso(ts):
    return datetime.fromtimestamp(
        ts, timezone.utc).isoformat().replace("+00:00", "Z")


def wcfy_comment(age_s, fresh=True):
    """A WCFY comment with a dynamic date: fresh=True is newer than the head
    (age_s/2), False is older than the head (age_s + an hour). Fixed dates rot
    with the clock, so only relative ones."""
    ts = time.time() - (age_s / 2 if fresh else age_s + 3600)
    return {"body": "## What changes for you\n- live marker for the head",
            "createdAt": iso(ts)}


def make_board(tasks=(), comments=()):
    """A mini board. tasks: dict(id,title,status,assignee,priority,key,body)."""
    tmp = tempfile.mkdtemp(prefix="wdtest-wcfy-")
    slug = "testboard"
    os.makedirs(os.path.join(tmp, slug), exist_ok=True)
    con = sqlite3.connect(os.path.join(tmp, slug, "kanban.db"))
    con.executescript("""
        create table tasks (id text primary key, title text, body text default '',
            assignee text default 'ops', status text, priority int default 0,
            created_by text default 'agent', created_at int,
            started_at int, completed_at int,
            workspace_kind text, workspace_path text, branch_name text,
            project_id text, claim_lock text, claim_expires int,
            tenant text, result text, idempotency_key text,
            consecutive_failures int default 0, worker_pid int,
            last_failure_error text, max_runtime_seconds int default null,
            last_heartbeat_at int, current_run_id int,
            workflow_template_id text, current_step_key text, skills text,
            model_override text, provider_override text,
            reasoning_effort text, max_retries int,
            goal_mode int default 0, goal_max_turns int,
            session_id text, block_kind text default null,
            block_recurrences int default 0);
        create table task_links (parent_id text, child_id text);
        create table task_runs (id integer primary key, task_id text,
            profile text, status text, started_at int, ended_at int,
            outcome text, summary text, metadata text, error text);
        create table task_events (id integer primary key, task_id text,
            run_id int, kind text, payload text, created_at int);
        create table task_comments (id integer primary key, task_id text,
            body text, created_at int);
    """)
    now = time.time()
    for t in tasks:
        con.execute(
            "insert into tasks (id, title, status, assignee, priority,"
            " created_by, created_at, idempotency_key, body)"
            " values (?,?,?,?,?,?,?,?,?)",
            (t["id"], t.get("title", "t " + t["id"]), t.get("status", "todo"),
             t.get("assignee", "coder"), t.get("priority", 42),
             t.get("created_by", "agent"), int(now - t.get("age", 3600)),
             t.get("key"), t.get("body", "body " + t["id"])))
    for task_id, body in comments:
        con.execute("insert into task_comments (task_id, body, created_at)"
                    " values (?,?,?)", (task_id, body, int(now)))
    con.commit()
    con.close()
    old_boards = mod.BOARDS
    mod.BOARDS = tmp
    return slug, tmp, old_boards


def pr_json(head, age_s, comments=(), reviews=(), base="develop/2.x"):
    committed = datetime.fromtimestamp(
        time.time() - age_s, timezone.utc).isoformat().replace("+00:00", "Z")
    # a string is a comment body dated before CALIBRATION_REQUIRED_SINCE
    # (old verdicts are grandfathered); a dict sets createdAt explicitly
    items = []
    for c in comments:
        if isinstance(c, dict):
            items.append({"body": c["body"],
                          "createdAt": c.get(
                              "createdAt", "2026-10-30T00:00:00Z")})
        else:
            items.append({"body": c, "createdAt": "2026-10-30T00:00:00Z"})
    return json.dumps({
        "headRefOid": head,
        "baseRefName": base,
        "commits": [{"committedDate": committed}],
        "reviews": list(reviews),
        "comments": items,
    })


# The default CI scenario is green; the mandatory set mirrors the required
# contexts of the base branch protection.
REQ_2X = ["build (JDK 17)", "build (JDK 21)", "e2e webmvc", "e2e webflux"]
GREEN_CHECKS = json.dumps([{"name": n, "state": "SUCCESS"} for n in REQ_2X])


def fake_run(prs, calls, new_ids=None, protection=None, checks=None):
    """gh list/view/checks/api by the scenario, hermes create/schedule/unblock
    are recorded.

    protection: {base: a protection JSON string or "" (a read failure)}.
    checks: {PR number: a gh pr checks JSON string or "" (a read failure)}.
    """
    new_ids = new_ids or ["t_new1", "t_new2", "t_new3", "t_new4", "t_new5",
                          "t_new6", "t_new7", "t_new8"]

    def _run(cmd, timeout=120):
        calls.append(cmd)
        if cmd[0] == "gh":
            if len(cmd) > 1 and cmd[1] == "api":
                b = cmd[2].split("/branches/", 1)[1].rsplit(
                    "/protection", 1)[0]
                return (protection or {}).get(
                    b, json.dumps({"required_status_checks":
                                   {"contexts": REQ_2X}}))
            if "checks" in cmd:
                return (checks or {}).get(int(cmd[3]), GREEN_CHECKS)
            if "list" in cmd:
                return json.dumps([{"number": n, "isDraft": False}
                                   for n in sorted(prs)])
            if "view" in cmd:
                return prs.get(int(cmd[3]), "")
            return ""
        if len(cmd) > 4 and cmd[4] == "create":
            return json.dumps({"id": new_ids.pop(0), "status": "ready"})
        if len(cmd) > 4 and cmd[4] == "schedule":
            return "Scheduled %s" % cmd[5]
        if len(cmd) > 4 and cmd[4] == "unblock":
            return "Unblocked %s" % cmd[5]
        return ""
    return _run


def with_scenario(prs, tasks=(), comments=(),
                  protection=None, checks=None, keep_orphans=False):
    """Run review_layer_check on the fixture; gh/hermes calls go to calls."""
    slug, tmp, old = make_board(tasks, comments)
    calls = []
    real_run, real_repo = mod.run, mod.board_repo
    mod.run = fake_run(prs, calls, protection=protection, checks=checks)
    mod.board_repo = lambda s: "acme/repo"
    try:
        found = mod.review_layer_check(slug)
    finally:
        mod.run = real_run
        mod.board_repo = real_repo
        mod.BOARDS = old
    # "A PR of the active milestone without a card" is a legitimate finding
    # in mock scenarios; it has its own tests.
    if not keep_orphans:
        found = [f for f in found if ":r36:ms-orphan:" not in f[0]]
    return found, calls, tmp


def creates(calls):
    return [c for c in calls if len(c) > 4 and c[4] == "create"]


HEAD243 = "f9897fe5998333c1b6ec52c5a9129c81154b511f"
HEAD317 = "819e741fdd19ce94baadce77cf49e3f972caa671"
HEAD_AGE = 2 * 3600  # the head is 2 hours old: inside the auto-arm window

# Offsets (minutes from the head moment), live intervals:
# PR 243: head 17:42, the real WCFY long before (older by 42d 19h), rebase
#         13:49, verdict on the previous head 14:52, handoff 17:56,
#         APPROVED verdict 19:20.
# PR 317: head 19:01, PASS verdict 19:31.
OFF_WCFY_STALE = -61638
OFF_REBASE = -233
OFF_VERDICT_OLD = -170
OFF_HANDOFF = 14
OFF_VERDICT_243 = 98
OFF_VERDICT_317 = 30

WCFY_243_STALE = '''## What changes for you

1. **Timeout errors now show which phase timed out.** Instead of a generic "Connection timed out" banner, you now see whether it was the transport connect or the initialize step that exceeded its budget, with exact numbers like "15.2s of 30.0s".

2. **Actionable hint.** A clickable "Increase Connection timeout in the connection form" link scrolls to and focuses the timeout input so you can adjust it right away.

3. **No regressions.** Errors without structured timeout data (plain text errors, network failures) render exactly as before.

**Evidence:**
- 27 parser tests (up from 17) including timeout parsing and budget formatting
- 16 sidebar alert tests (up from 10) covering the structured timeout banner, budget display, hint interaction, and fallback for unstructured errors
- All 638 UI tests pass, TypeScript compiles, lint clean
- Notice registry: PASS'''

REBASE_243 = '''## Rebase onto develop/2.x completed

Rebased from the old base (11552a1a) onto develop/2.x@69cb7366. New head: be446eb4, same two commits (feat + notice/chore).

Adaptations forced by the new base:
- `errors.ts` -> `connectionErrors.ts`: the module was renamed and its API grown by an earlier merge; the timeout parsing delta now lives in the renamed module.
- The sidebar connect-failure alert moved to the top of the config pane; the structured-timeout branch renders in the new location and keeps the unified banner contract.
- The phase-less timeout test asserts the base text (`POST /mcp request timed out`).

Verification:
- local: targeted suites 53/53, full UI suite 1040/1040, lint clean, build clean, notice-registry script OK;
- CI run on be446eb4: all 5 jobs green (build JDK 17/21, demo E2E webmvc/webflux, notice registry);
- `mergeable: MERGEABLE`, merge state CLEAN.
'''

VERDICT_243_OLD = '''## Review verdict
CHANGES REQUESTED on head be446eb4893627034630c629b82984c0f064f1b0 (first review: no prior verdicts on this PR).

CI: 5 of 5 green (build JDK 17, build JDK 21, demo E2E webflux, demo E2E webmvc, notice registry). Mergeable: CLEAN vs develop/2.x. risk: normal.

Green CI does not exercise the blocker below: the new UI tests feed the parser a wire code that the proxy never sends.

### F1 [BLOCK] Structured timeout banner is unreachable end-to-end: the parser listens for an invented `connection_timeout` code, the proxy never sends it

Evidence chain, each link verified on this head or on develop/2.x:

1. The proxy emits per-phase timeouts as `{"error":{"code":"CONNECT_FAILED","reason":"timeout","phase":"connect","elapsedMs":1234,"budgetMs":1000}}`.
2. `git grep connection_timeout origin/develop/2.x`: the only 2 occurrences in the whole tree are a Java test comment and its assertion. Nothing in the backend produces this code.
3. This PR branches the parser on exactly that invented code.
4. The legacy `CONNECT_FAILED` branch (the only branch the real payload reaches) does not read `phase`/`elapsedMs`/`budgetMs`, so those fields are dropped.

Minimum closure:
- parse `phase`/`elapsedMs`/`budgetMs` in the `CONNECT_FAILED` + `reason === "timeout"` branch (that is the real wire contract);
- rewrite the parser tests to use the real payload shape.

### F2 [BLOCK] PR body does not open with `## Before / After`

The body starts with `## What ...`. Per repo convention every PR must open with `## Before / After`; for a UI diff that means two images. The current body has text-only "Examples" and no images at all.

### F3 [normal] The hint click targets an element that does not exist on this base

The hint scrolls and focuses an element that exists only in a still-open PR. Merged in this order, "Increase Connection timeout" is a silent no-op.

## Calibration
- contract: proxy payload verified byte-level against the producer on develop/2.x plus a live probe on this head: the real payload parses to phase undefined, the structured banner is unreachable (F1)
- mutation: F1 proven by the equivalent red probe (a new test against the real payload fails on this head without any fix)
- ui-input: N/A with cause: the banner is provably unreachable at the parser level (F1 probe), a live UI session would prove nothing until F1 is fixed
- pr-texts: test counts 29/24 re-measured on head vs base (match); Before/After section missing (F2)
- library-contract: N/A with cause: no external library output modeled here
- twin: N/A with cause: no twin lines, single line develop/2.x
'''

HANDOFF_243 = '''Rework pushed in f9897fe5 (CI 5/5 green: build 17/21, both demo E2E jobs, notice registry). Addressing F1 and F3 from the review; F2 (the Before / After section) is tracked separately and follows on the new head.

F1: the parser now reads phase/elapsedMs/budgetMs on the CONNECT_FAILED branch with reason "timeout", which is the contract the proxy actually sends (no separate error code). The connection_timeout branch stays as future-proofing, but no test relies on it anymore.

F3: the "Increase Connection timeout" hint is removed. The target element only exists in another PR, so on this base the click was a silent no-op; the hint comes back together with that field.'''

VERDICT_243 = '''## Review verdict
APPROVED on head f9897fe5998333c1b6ec52c5a9129c81154b511f (second review; the first one was CHANGES REQUESTED on be446eb4893627034630c629b82984c0f064f1b0). The head commit is newer than the first return, so a PASS verdict is valid here.

CI: 5 of 5 green on this head (build JDK 17, build JDK 21, demo E2E webflux, demo E2E webmvc, notice registry; head_sha = f9897fe5998333c1b6ec52c5a9129c81154b511f).

### Round-1 findings closure

**F1 [BLOCK] dead parser/proxy contract - CLOSED, proven live and by mutation.**
- `src/lib/connectionErrors.ts` now reads `phase`/`elapsedMs`/`budgetMs` on the `CONNECT_FAILED` + `reason: "timeout"` branch, which is the contract the proxy actually emits.
- Mutation check on this head: removing the three field reads on the real branch turns 3 tests red, 50 stay green including the alias test. Baseline before mutation: 53/53 in the two touched test files; full suite re-run: 1040/1040, matching the PR body claim.

**F2 [BLOCK] missing Before / After - CLOSED.**
- The body opens with `## Before / After`; all four raw image links answer 200.

**F3 dead hint target - CLOSED.**
- The "Increase Connection timeout" hint is removed.

### Fresh re-review of the rework

Scope: 5 files, all inside the PR surface, no deleted files. Changed behavior verified on a live stand built from this head.

No new findings on the changed surface. Non-blocking note: per the delivery convention the `## What changes for you` section is now due on this final head.

## Calibration
- contract: proxy wire contract verified live against the parser branch on this head
- mutation: dropping the phase/elapsedMs/budgetMs reads on the real branch killed 3 tests; the alias test stayed green by design
- ui-input: live browser session against the jar built from this head, real Connect click against a hanging upstream, structured banner rendered 3 times with live numbers matching the server log
- pr-texts: every numeric claim re-measured: 30+23 = 53 tests in the two files, 1040/1040 full suite, 4 of 4 images at 200
- library-contract: N/A, no external library output is modeled

| Promise | Mutant | Test | Result |
|---|---|---|---|
| structured banner parses real payload | drop phase/elapsedMs/budgetMs reads | parser tests | killed 3 red |
'''

VERDICT_317 = '''## Review verdict

**PASS** on branch head `819e741fdd19ce94baadce77cf49e3f972caa671` (the card was armed on `61bc03ec3e60339e6585274eb1806c96c8cd1907`; the branch advanced by one commit, and `61bc03ec` is an ancestor of the head, so the verdict is issued on the actual head per board rules).

CI: 5 of 5 green on the head (build JDK 17, build JDK 21, demo E2E webflux, demo E2E webmvc, notice registry). PR is MERGEABLE / mergeStateStatus CLEAN.

### What was verified

- Wire shape against the protocol spec page: the new fields are hoisted to the top level of the result object; the retry carries the responses and the state as siblings of `name`/`arguments`; a new JSON-RPC id per retry.
- The integration test really executes in both CI E2E jobs, not skipped: `Tests run: 2, Failures: 0` in each.
- tools/list contract: head asserts 29 tools / 22 read-only vs 28 / 21 on base, consistent with one new read-only tool.
- Issue gate: no feature/ux labels, so the accepted-prototype gate does not apply; the PR correctly uses `Refs`, not `Closes`.
- No deleted tests in the diff. No comments in the PR, so nothing was accepted or withdrawn by the owner.

### Mutation evidence (local, on the head)

- M1, hoisting disabled: the integration test 2/2 red. Response-side hoisting is covered.
- M2b, re-send capture fully disabled: 2/2 red. Request-side capture is covered.
- Baseline: pristine head green 2/2 locally.

### Findings (non-blocking)

- N1 (PR body wording): the Details section describes the direction of the capture backwards. The wire behavior is correct.
- N2 (doc claim stronger than evidence): "Results without the markers serialize byte-for-byte like before" is verified semantically; byte-identity is not asserted anywhere.
- N3 (process): pr-risk.py classifies this PR as critical (643 production lines > 400), so the merge decision stays with the human owner regardless of this PASS.

Next step per convention: the `## What changes for you` comment is published by the coder after this PASS, once, on the final head.

## Calibration
- contract: the issue has no feature/ux labels (prototype gate N/A), the PR uses Refs not Closes; the tools/list contract 28 to 29 verified by git grep on head vs base and by the CI contract test
- mutation: M1 hoist-off 2/2 red, M2b capture-off 2/2 red; baseline green and the pristine artifact restored
- ui-input: N/A, no UI diff in this PR (demo-app server fixture only)
- pr-texts: Before/After first, the diagram delta matches the diff, Refs present, no premature WCFY comment; one wording error filed as N1
- library-contract: wire shape checked against the spec page directly (not against the author's description); the integration fixtures are live E2E runs, not handwritten copies

| Promise | Mutant | Test | Result |
|---|---|---|---|
| requestState hoisted | M1 hoist-off | integration test | 2/2 red |
| re-send captured | M2b capture-off | integration test | 2/2 red |
'''


def at(off_min):
    return iso(time.time() - HEAD_AGE + off_min * 60)


def test_verdict_mention_is_not_wcfy_243():
    data = {"comments": [{"body": VERDICT_243, "createdAt": at(OFF_VERDICT_243)}]}
    assert mod._wcfy_latest(data) is None, mod._wcfy_latest(data)
    assert not mod._wcfy_comment_is(VERDICT_243)


def test_verdict_mention_is_not_wcfy_317():
    data = {"comments": [{"body": VERDICT_317, "createdAt": at(OFF_VERDICT_317)}]}
    assert mod._wcfy_latest(data) is None, mod._wcfy_latest(data)
    assert not mod._wcfy_comment_is(VERDICT_317)


def test_stale_real_wcfy_recognized_but_old():
    """The real old WCFY is recognised as a WCFY but is older than the head:
    in _wcfy_latest it survives, not the line of the verdict."""
    assert mod._wcfy_comment_is(WCFY_243_STALE)
    data = {"comments": [
        {"body": WCFY_243_STALE, "createdAt": at(OFF_WCFY_STALE)},
        {"body": VERDICT_243, "createdAt": at(OFF_VERDICT_243)}]}
    got = mod._wcfy_latest(data)
    assert got is not None
    assert got < time.time() - HEAD_AGE, "wcfy_at must be older than the head"


def comments_243():
    return [
        {"body": WCFY_243_STALE, "createdAt": at(OFF_WCFY_STALE)},
        {"body": REBASE_243, "createdAt": at(OFF_REBASE)},
        {"body": VERDICT_243_OLD, "createdAt": at(OFF_VERDICT_OLD)},
        {"body": HANDOFF_243, "createdAt": at(OFF_HANDOFF)},
        {"body": VERDICT_243, "createdAt": at(OFF_VERDICT_243)},
    ]


def test_243_pass_no_wcfy_arm():
    """Accept on the head, CI green (the scenario default), the real WCFY is
    stale, the mention in the verdict does not count. The WCFY arm is gone
    (one PR = one card): nothing is created."""
    found, calls, tmp = with_scenario(
        {243: pr_json(HEAD243, HEAD_AGE, comments=comments_243())})
    assert creates(calls) == [], calls
    shutil.rmtree(tmp)
    print("OK test_243_pass_no_wcfy_arm")


def test_317_pass_no_wcfy_arm():
    found, calls, tmp = with_scenario(
        {317: pr_json(HEAD317, HEAD_AGE, comments=[
            {"body": VERDICT_317, "createdAt": at(OFF_VERDICT_317)}])})
    assert creates(calls) == [], calls
    shutil.rmtree(tmp)
    print("OK test_317_pass_no_wcfy_arm")


def test_317_real_wcfy_after_head_no_arm():
    """Negative: a real "## What changes for you" AFTER the head. A PASS head
    without a live card and without a formal owner review legitimately gives
    ONLY the finding r36:ms-orphan (no cards created): the PR work fell off
    the board."""
    found, calls, tmp = with_scenario(
        {317: pr_json(HEAD317, HEAD_AGE, comments=[
            {"body": VERDICT_317, "createdAt": at(OFF_VERDICT_317)},
            wcfy_comment(HEAD_AGE)])},
        keep_orphans=True)
    assert creates(calls) == [], calls
    assert [f[0] for f in found] == ["testboard:r36:ms-orphan:317"], found
    shutil.rmtree(tmp)
    print("OK test_317_real_wcfy_after_head_no_arm")


def main():
    test_verdict_mention_is_not_wcfy_243()
    test_verdict_mention_is_not_wcfy_317()
    test_stale_real_wcfy_recognized_but_old()
    test_243_pass_no_wcfy_arm()
    test_317_pass_no_wcfy_arm()
    test_317_real_wcfy_after_head_no_arm()
    print("OK: a WCFY mention in a verdict is not a WCFY, tests are green")


if __name__ == "__main__":
    main()
