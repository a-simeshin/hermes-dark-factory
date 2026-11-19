#!/usr/bin/env python3
"""A QA verdict does not count as a reviewer verdict.

Failure this guards: a tester titled a verdict "## Review verdict (QA): PASS".
The reviewer verdict header regex without a lookahead matched it, the comment
passed the Calibration and promises-table gates (the table is in the fixture
in full, as in the real comment), and _pr_review_state returned verdict=True
with head_kind=accept: the PR looked reviewed and went to the owner without a
review. PRE_FIX_RE below is the regex before the fix; it must match the QA
header of the fixture (the RED proof), the current one must not.

Also checked: a "## QA verdict" header is not a reviewer verdict, real
reviewer verdicts (CHANGES REQUESTED, APPROVED) still count, and pr-hygiene
skips both QA header forms.

Run: python3 test_board_watchdog_qa_verdict_split.py
"""
import importlib.util
import os
import re
import sys
import tempfile

# Settings and Hermes home are fixed before the scripts are loaded.
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="wd-home-")
os.environ.update({
    "FACTORY_REPO": "acme/widget",
    "FACTORY_REPO_PATH": "/srv/hermes/widget",
    "FACTORY_BOARD": "main",
    "FACTORY_OWNER_LOGIN": "owner-login",
    "FACTORY_OWNER_CHAT": "telegram:000000000",
    "FACTORY_BASE_BRANCHES": "main",
})

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.environ.get("BOARD_WATCHDOG_SCRIPT",
                        os.path.join(HERE, "board-watchdog.py"))

spec = importlib.util.spec_from_file_location("bw_qa_split", SCRIPT)
if spec is None or spec.loader is None:
    print("SKIP: cannot load board-watchdog.py")
    sys.exit(0)
bw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bw)

spec_ph = importlib.util.spec_from_file_location(
    "ph_qa_split", os.path.join(HERE, "pr-hygiene.py"))
ph = importlib.util.module_from_spec(spec_ph)
spec_ph.loader.exec_module(ph)

FAILED = []


def check(name, cond, detail=""):
    if cond:
        print("PASS " + name)
    else:
        FAILED.append(name)
        print("FAIL " + name + " " + str(detail))


HEAD241 = "bd16143677b9db98f50154e4f59da48ee5803eca"

# --- fixtures: verbatim bodies of real PR comments (project names replaced) ---
QA_VERDICT_241 = '## Review verdict (QA): PASS\n\nQA verification of the two follow-up timeout fixes (proxy-scoped guard, decimal-only validation) on a clean-built stand from the merged head `bd161436` (demo-webmvc exec.jar, auth off, real Chromium). Scenarios A-E from the card, each exercised live.\n\n**A (proxy, 700 + blur): PASS.** Field error "Must be a whole number of seconds between 1 and 600" visible, Connect disabled. [Screenshot](https://raw.githubusercontent.com/acme/widget/hermes-assets/screenshots/qa/pr241-qa-A-proxy-700.png)\n\n**B (switch to Direct): PASS.** Timeout field and error removed from the DOM, Connect enabled, clicking fired real requests (4x fetch to `http://127.0.0.1:18080/mcp`, initialize round-trip), status Connected.\n\n**C (reload with persisted lastConnectionType=direct, lastConnectionTimeout=700): PASS.** After reload the field is absent, Connect enabled, click fired 4 real fetches to `/mcp`, status Connected, zero `window.alert` calls, no "Failed to connect" banner.\n\n**D (non-decimal values): PASS with one environment note.** `1e1` and `1.5`: field-level timeout error shown, Connect disabled, 0 network requests, no generic banner ([screenshot](https://raw.githubusercontent.com/acme/widget/hermes-assets/screenshots/qa/pr241-qa-D-1e1-error.png)). `0x10` and `+30`: in a real Chrome `input[type=number]` these cannot be entered at all, the browser sanitizes the DOM value to `""`, so React state never holds them and no field error is possible; nothing is forwarded and no banner appears, which satisfies the safety intent. Both literals are pinned at component level in `Sidebar.test.tsx` (jsdom accepts any raw value), so coverage is intact. This is a browser-sanitization fact, not a defect of the guard.\n\n**E (120 accepted): PASS.** Connect forwarded `connectionTimeout=120` on the proxy URL (`/widget-api/mcp?url=%2Fmcp&transportType=streamable-http&connectionTimeout=120`), SSE stream opened, status Connected, no alerts, no banner ([screenshot](https://raw.githubusercontent.com/acme/widget/hermes-assets/screenshots/qa/pr241-qa-E-120-connected.png)). Optional proxy-side curl for 400 on 1e1-class values was not reproducible: the proxy API answers 401 to raw curl/in-page replay (token handshake), which is out of scope for this card; UI-side evidence shows invalid values never leave the client.\n\nGates: CI 5/5 green on `bd161436` (build JDK 17/21, demo E2E webmvc, demo E2E webflux, notice registry); local jest 68 suites, 1269/1269 green; `npm run lint` clean; worktree `git status --porcelain` empty; diff touches exactly the 4 claimed files (Sidebar.tsx, Sidebar.test.tsx, App.connectionTimeoutPersistence.test.tsx, NOTICE.d/connection-timeout.txt).\n\n## Calibration\n\n- contract: both fix promises from the parent handoff enumerated and mapped in the table below; each row verified by a live repro on the head, not by report text.\n- mutation: N/A for this card (QA verification, no new test authored); the guards\' mutation evidence is carried by the prior verdict rounds (regex-removal mutant kills exactly the 3 new tests, per round 3 closure on `bb695e69`) and the regression tests are green on this head.\n- ui-input: verified with real Chromium events (focus + focusout for React onBlur, native value setter for input), including the number-input sanitization caveat in scenario D.\n- pr-texts: PR body matches behavior verified live (proxy-only field, whole seconds 1..600, empty = default); NOTICE.d entry present in the diff.\n\n| Promise | Mutant / probe | Test | Result |\n|---|---|---|---|\n| Invalid timeout (proxy) blocks Connect with a field error | live repro: 700 + blur on head bd161436 | DOM probe: error paragraph present, Connect disabled | PASS |\n| Direct mode never locked by a stale invalid persisted timeout | live repro: switch to Direct with timeout=700, then reload with persisted direct/700 | DOM probe + fetch tap: field absent, Connect enabled, real `/mcp` requests | PASS |\n| Non-decimal input rejected client-side, never forwarded | live repro: 1e1, 1.5 typed and blurred; network tap | 0 requests, field error, disabled Connect; 0x10/+30 sanitized by the browser, jest covers them | PASS |\n| Valid timeout 120 accepted and forwarded to the proxy | live repro: 120 + Connect | proxy URL carries connectionTimeout=120, SSE connected | PASS |\n'

REVIEWER_CR_241_R3 = '## Review verdict\n\nCHANGES REQUESTED on head `afa4447fd9c4294df6b4cb162dcd837b976c0547` (round 3; the two commits pushed after the round-2 approval were re-reviewed live: `a372813` decimal-digits guard, `afa4447` proxy scoping).\n\nCI: 5 of 5 green on this head (build JDK 17, build JDK 21, demo E2E webflux, demo E2E webmvc, notice registry); all 4 required contexts of the base branch pass. Mergeable, state CLEAN.\n\n### Round-2 follow-up findings: both closed live\n\nVerified on a stand built from this exact head (see "Live verification" below):\n\n1. Direct-mode silent lockout (IMPORTANT, round 2): closed in both flows.\n   - Same session: with a live `700` error in proxy mode, switching Connection Type to Direct hides the field and the error, Connect is enabled, clicking connects (status Connected).\n   - After reload with `lastConnectionTimeout=700` and Direct persisted: Connect is enabled, clicking connects.\n2. Numeric literals reaching the proxy (MINOR, round 2): closed for the reachable class. `1e1` now shows the inline error "Must be a whole number of seconds between 1 and 600" and a Connect click fires zero network requests (round 2: silently forwarded, then a generic 400 banner). `+30` and `0x10` never reach the validator in a real browser at all: the number input sanitizes them to `30` and `` before the change event, so the regex\'s observable catch is the e-notation class.\n\nRegression checks pass: `120` connects with the wire request `/widget-api/sse?url=%2Fsse&proxyFullAddress=...&transportType=sse&connectionTimeout=120`; an empty field connects with no `connectionTimeout` parameter.\n\n### BLOCK: the decimal-rejection behavior of `a372813` has zero test coverage\n\nMutation on the production line: I reverted `isValidConnectionTimeout` to its pre-`a372813` form (regex block removed, `Number.isInteger(n) && n >= 1 && n <= CONNECTION_TIMEOUT_MAX_SECONDS` restored). Result: 59/59 green in `Sidebar.test.tsx`. The mutant is alive.\n\nProof the gap is real:\n- No test anywhere in the tree feeds `1e1`, `0x10` or `+30` to the field; the only mention of these literals is the source comment itself (grep over `src/**/__tests__`).\n- Every invalid input the suite does use was already rejected by the OLD guard too: `abc` gives NaN, `0` and `601` fail the range check, `1.5` fails `Number.isInteger`, `1e3` (= 1000) fails the range check. So the suite pins the range, not the new decimal-digits rule.\n- The behavior itself works (live: `1e1` errors inline and blocks Connect with zero requests), so this is a coverage hole, not a code bug. But the commit exists solely to change this behavior, the PR body and NOTICE.d both claim it, and nothing locks it: any refactor (or the next re-vendor of the upstream client) can silently drop it with a fully green suite.\n\nClosure (small, one push):\n- Add a test feeding `"1e2"` to the blur and click guards, expecting the field-level error and `onConnect` not called. `1e2` is the discriminating input: `Number("1e2")` is 100, an integer inside 1..600, so ONLY the regex rejects it; that exact test goes red under my surviving mutant. Optionally add `"1e1"`/`"0x10"` alongside (jsdom `fireEvent.change` bypasses the browser number-input sanitization, so raw strings reach the validator there).\n- Re-run the suite; then this verdict\'s blocker is closed.\n\n### MINOR (fold into the same push)\n\n1. Body numbers are stale after the two new commits: "16 new tests in Sidebar.test.tsx ... All 1010 UI tests pass" vs the actual head: 18 connection-timeout tests in `Sidebar.test.tsx`, full local suite 54 suites / 1012 passed (my clean-head run).\n2. The new comment in `Sidebar.tsx` (mirrored in NOTICE.d) justifies rejecting `+30` by "instead of surfacing as a generic banner after a proxy 400". Factually the proxy does NOT 400 on `+30`: `Long.parseLong("+30")` returns 30 (verified by running a JDK one-liner). The UI is intentionally stricter than the proxy here, which is fine; the comment\'s stated reason is wrong for this literal. One-line rewording, e.g. "stricter than the proxy on purpose".\n\n### Mutation log (production lines, each reverted, worktree clean at afa4447)\n\n- Unscoped disabled term (back to `|| !!timeoutError`): 1 red, "keeps Connect enabled and clickable in direct mode after a proxy-mode timeout error". Killed.\n- Unscoped click guard (dropped `connectionType === "proxy" &&`): 2 red, both direct-mode tests. Killed.\n- Regex removal: 0 red. Survived, promoted to the BLOCK above.\n\n### Live verification (clean-built stand, artifact checked first)\n\n- Stand built from this head after all mutation runs were reverted (`git status` clean); `index.html` references bundle `index-CmE4aGU3.js`, and the bundle itself contains `/^[0-9]+$/`, the proxy-scoped click guard and the proxy-scoped disabled term, so the artifact matches the reviewed source.\n- Scenarios driven in a real browser against the running jar: all outcomes listed in the round-2 closure section above, including the wire URLs captured via fetch/EventSource wrappers armed before each click.\n- Full local jest on the clean head: 54 suites, 1012/1012 passed.\n\n## Calibration\n- mutation: 3 mutants on the two new production lines; scoping mutants killed (1 and 2 reds), regex-removal mutant survived 59/59 green and is the blocker of this verdict\n- contract: every claim of both new commits verified live against the running proxy (scoping both flows, 1e1 blocked with zero requests, 120 forwarded on the wire, empty sends no parameter); NOTICE.d claims match shipped behavior\n- ui-input: real browser input on a clean-built stand (bundle expression checked inside the artifact before driving); proxy and direct flows, reload flow, wire capture on every connect\n- pr-texts: body test counts re-measured on this head (18 new tests, 1012 total) vs stale body numbers (16 / 1010), listed as MINOR 1\n- library-contract: N/A, the diff models no external library output; the only external contract (proxy `Long.parseLong`) was verified directly by compiling and running a JDK probe, not from memory\n- twin: N/A, single release line the base branch since 1.2.0 EOL (owner decision)\n'

QA_NEW_FORMAT = (
    "## QA verdict\n\nPASS on head `" + HEAD241[:7] +
    "` (QA header format).")

REVIEWER_PASS_335 = '## Review verdict: APPROVED on 886f24e6249ca35ba0754cc2ffd5dac3e9d0723d\n\nReviewed as delta from the last reviewed head 622222a6 (round-3 CHANGES\nREQUESTED). The delta is exactly one commit, `886f24e6 test(ui): pin the\nsidebar Connect CTA to the shared validating handler`, and it is test-only:\none new spec, `src/__tests__/App.sidebarConnectValidation.test.tsx` (+147),\nzero production changes, zero deletions across the whole PR range.\n\nCI on the exact head (run 43714404691, headSha verified): 5/5 green\n(build JDK 17/21, demo E2E webmvc/webflux, notice registry). `mergeable:\nMERGEABLE`; the BLOCKED merge state is only this round-3 review itself,\nwhich this verdict clears. Not stacked: all four branch commits are this\nPR\'s own work, base is the base branch.\n\n### Closure of the round-3 blocker (the one finding)\n\nThe blocker: the sidebar half of "both CTAs share one validating handler"\nwas pinned by nothing; the mutant `onConnect={handleConnectRequest}` ->\n`onConnect={handleConnect}` survived 1235/1235 on 622222a.\n\nThe new spec meets the closure criterion exactly: forwarding mock of the\nSidebar (the allowed alternative to rendering the real one) that invokes\nthe `onConnect` prop App actually passes and surfaces `urlError` as\n`role="alert"`; `transportType=streamable-http`; empty URL and invalid URL\n(`htp://not-a-valid-url`); explicitly asserts no alert exists before the\nclick (no prior blur, fresh page); then asserts (a) the validation alert\ntext appears inside the sidebar container and (b) `useConnection.connect`\nis never called. The spec does not seed the state the handler writes: the\nerror must come out of `handleConnectRequest` itself.\n\nMutant re-run on this exact head, full suite: `App.tsx:2057`\n`onConnect={handleConnectRequest}` -> `onConnect={handleConnect}` gives\n2 RED, both in `App.sidebarConnectValidation.test.tsx` (failure at the\n`findByRole("alert")` wait, i.e. the validation-loss axis), 1235/1237 green.\nThe spec is the catcher and the mutant is alive (it compiled, ran, and\nchanged behavior: the sidebar click goes straight to connect). Blocker\nclosed.\n\n### Promise / mutant table (every run on the exact head 886f24e)\n\n| Promise | Mutant | Test | Result |\n|---|---|---|---|\n| Empty/invalid URL blocks the connect at click time (guard in `handleConnectRequest`) | `App.tsx:1039` `if (false && !result.isValid)` | `App.emptyStateConnect.test.tsx` + `App.sidebarConnectValidation.test.tsx` | killed RED (4) |\n| Canvas CTA uses the shared validated handler, never raw connect | `App.tsx:2531` `onClick={() => { void connectMcpServer(); }}` | `App.emptyStateConnect.test.tsx` | killed RED (2) |\n| Both CTAs keep the #218 history-bucket freeze (delegation to `handleConnect`) | `App.tsx:1043` `void handleConnect();` -> `void connectMcpServer();` | `App.transportScoping.test.tsx` | killed RED (5) |\n| Sidebar wiring keeps the shared handler (no raw connect) | `App.tsx:2057` `onConnect={connectMcpServer}` | `App.transportScoping.test.tsx` | killed RED (5) |\n| Sidebar CTA click-time validation, same handler as canvas (round-3 blocker) | `App.tsx:2057` `onConnect={handleConnect}` | `App.sidebarConnectValidation.test.tsx`, full suite | killed RED (2 of 1237) |\n| "Open Auth Settings" stays as the secondary action | `App.tsx:2557` button label removed | `App.emptyStateConnect.test.tsx` | killed RED (1) |\n\nBaseline before mutations, clean head: 69/69 suites, 1237/1237 tests green.\nThe battery re-runs every mutant from the round-2/round-3 tables on this\nhead, so this APPROVED closes each previously found blocker with a fresh\nmutation, not by reference.\n\n### Notes (non-blocking)\n\n- PR body "Verification" still cites the round-1 counts ("62 suites, 1128\n  tests green on the new head"); current head is 69 suites / 1237 tests.\n  No behavioral claim in the body is false; stale counts only.\n- Production code is byte-identical to 622222a, where round 3 ran the live\n  input-driven scenarios (fresh-load geometry, empty-URL clicks on both\n  CTAs with zero network attempts, valid-URL connect, bucket-freeze\n  reconnect). A test-only delta has no new live surface; the CI demo E2E\n  jobs ran green against this exact head.\n\n## Calibration\n\n- contract: issue #269 rechecked in round 3, unchanged since; labels bug/ui\n  (prototype-acceptance gate not applicable, owner shaped the rework in\n  round-1 review); issue heading "Current behavior / After the change"\n  semantically equals Before/After, carried as a round-3 note;\n  `wait-for-triage` on the issue remains owner-side housekeeping.\n- mutation: 6 mutants on the exact head 886f24e (guard, canvas wiring,\n  delegation, sidebar raw wiring, sidebar validation loss, secondary\n  button), all killed with named failing suites; the blocker mutant run on\n  the full suite, 2 RED / 1235 green; baseline clean run 1237/1237.\n- ui-input: delta is test-only, production byte-identical to the head where\n  round 3 did the live runs (both CTA clicks with empty URL, zero fetch,\n  reconnect bucket freeze); CI demo E2E webmvc/webflux green on this head.\n- pr-texts: PR body and the delta commit enumerated; every behavioral\n  promise mapped in the table above; the only staleness is the test-count\n  line noted in Notes, which carries no behavioral claim; no third-party\n  claims in the delta.\n\n'

# The regex BEFORE the fix (the RED proof on the QA header).
PRE_FIX_RE = re.compile(
    r"(?im)^#{1,4}\s*(?:re[- ]?)?review\s+verdict\b")


def main():
    print("QA verdict != reviewer verdict")

    check("RED proof: the old regex matches the real QA header",
          bool(PRE_FIX_RE.search(QA_VERDICT_241)))
    check("fix: _VERDICT_HEADER_RE does not match the QA header",
          not bw._VERDICT_HEADER_RE.search(QA_VERDICT_241))
    check("fix: _QA_VERDICT_HEADER_RE recognizes the QA header",
          bool(bw._QA_VERDICT_HEADER_RE.search(QA_VERDICT_241)))

    # Snapshot of the PR: a round-3 CHANGES REQUESTED on an older head
    # (afa4447, the body does not name bd16143); the only "verdict" on the
    # head is the QA comment. Before the fix _latest_verdict
    # (owner_any_head=False) returned accept.
    data = {"reviews": [], "comments": [
        {"createdAt": "2026-11-03T03:04:08Z", "body": REVIEWER_CR_241_R3},
        {"createdAt": "2026-11-15T15:31:35Z", "body": QA_VERDICT_241},
    ]}
    check("no reviewer verdict on head bd161436 (None)",
          bw._latest_verdict(data, HEAD241, owner_any_head=False) is None)
    check("no meaningful verdict on the PR either (QA does not count)",
          bw._latest_verdict(data, HEAD241) is None)

    # The new QA verdict form must not leak in the future either.
    data_new = {"reviews": [], "comments": [
        {"createdAt": "2026-11-16T00:00:00Z", "body": QA_NEW_FORMAT}]}
    check("'## QA verdict: PASS' is not a reviewer verdict",
          bw._latest_verdict(data_new, HEAD241) is None)

    # The other side: real reviewer verdicts count as before.
    data_rev = {"reviews": [], "comments": [
        {"createdAt": "2026-11-03T03:04:08Z", "body": REVIEWER_CR_241_R3}]}
    cr = bw._latest_verdict(
        data_rev, "afa4447fd9c4294df6b4cb162dcd837b976c0547",
        owner_any_head=False)
    check("the round-3 CR stays reject", bool(cr) and cr["kind"] == "reject")
    data_335 = {"reviews": [], "comments": [
        {"createdAt": "2026-11-15T14:31:29Z", "body": REVIEWER_PASS_335}]}
    ap = bw._latest_verdict(
        data_335, "886f24e6249ca35ba0754cc2ffd5dac3e9d0723d",
        owner_any_head=False)
    check("APPROVED stays accept", bool(ap) and ap["kind"] == "accept")

    # pr-hygiene: both QA header forms are skipped by review_state.
    check("pr-hygiene: the real QA comment is recognized as QA",
          ph.qa_verdict_comment(QA_VERDICT_241))
    check("pr-hygiene: the new '## QA verdict' form is recognized",
          ph.qa_verdict_comment(QA_NEW_FORMAT))
    check("pr-hygiene: a real CR is not recognized as QA",
          not ph.qa_verdict_comment(REVIEWER_CR_241_R3))
    verdicts = []
    for body in (QA_VERDICT_241, QA_NEW_FORMAT):
        if ph.qa_verdict_comment(body):
            continue
        low = body.lower()
        if ph.REJECT_MARKER in low:
            verdicts.append("rejected")
        elif any(m in low for m in ph.ACCEPT_MARKERS):
            verdicts.append("ok")
    check("pr-hygiene: neither QA form gives ok or rejected",
          verdicts == [])

    print()
    if FAILED:
        print("FAILED: %d (%s)" % (len(FAILED), ", ".join(FAILED)))
        return 1
    print("all green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
