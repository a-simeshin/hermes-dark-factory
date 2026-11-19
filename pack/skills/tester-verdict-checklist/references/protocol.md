# tester-verdict-checklist: full protocol

This document is the long form of the tester's checklist: every item with the
command that closes it, the hard rules for verdicts after a return, and the
reasoning (retold incidents) behind the rules on regression tests and
mutations. A verdict without the completed checklist is invalid; each item is
closed by command output, not by the word "checked".

## When to use

You are checking the work of a card and preparing an ACCEPTED or RETURN
verdict.

## Items

1. **The branch and diff exist.** `git log <base>..<branch>` is not empty;
   the diff matches the card, no more and no less.
2. **Nothing is left in the tree.** `git status --porcelain` in the worktree
   is empty. `git diff` does not show untracked files; status does.
3. **Build.** The project's compile command for the affected modules (for
   example a Maven `-pl <modules> -am compile`, `npm run build`, `cargo
   build`), as the project conventions define it.
4. **Tests of the affected modules,** with the count attached.
5. **Acceptance criteria** from the card checked one by one, in fact.
6. **Regression.** `find_referencing_symbols` on changed symbols: neighbouring
   uses are not broken.
7. **CI on the PR.** `gh pr checks <N>` with the app token the pack
   provides (in the reference setup `GH_TOKEN=$(~/.hermes/scripts/gh-app-token.sh)`).
   Red CI is RETURN with an excerpt of the failed job's log (`--log-failed`),
   even if local tests are green. A local spot run does not replace CI.
8. **PR base** follows the project conventions, not the default branch.
9. **No infrastructure extras** outside the ticket: new dependencies, CI
   configs, runners. If present and not run by CI: RETURN.
10. **Upstream divergence registry** (only if the project keeps one): the
    diff touches vendored upstream code and the registry has no entry: RETURN.
11. **Issue link.** The PR body contains `Closes #N` or `Refs #N`, and the
    issue has all template sections (for example Current behavior / After the
    change / User benefit / Acceptance criteria). Missing: RETURN.

## Hard rules for a verdict after a return

Each of these was expensive once:

1. **PASS after CHANGES REQUESTED only on a commit younger than that CHANGES
   REQUESTED.** If the last commit is older than the return, the verdict is
   rejected without reading. In one case a PASS was written eighteen hours
   after a return on the same commit, with nothing pushed in between.
2. **A verdict is given only on the PR branch head.** A build with someone
   else's cherry-picks and extra files is not a check of the PR; in one case
   such a private build was "green" and even there the client traffic showed
   zero events.
3. **QA does not PASS while the reviewer's open REQUEST_CHANGES is not closed
   by a commit.** In one case QA wrote PASS twenty minutes after a reviewer's
   proven finding without addressing it.
4. **The QA verdict header is `## QA verdict`.** Board tooling parses
   "Review verdict" headers as the reviewer's verdict. A QA PASS posted as
   `## Review verdict (QA): PASS` was counted as the review, and the PR went
   to the owner with no review at all.

## The owner's decision on a point closes it for the verdict

Before the verdict read the owner's last comment on the PR. A point the
owner explicitly accepted or withdrew does not go into the verdict even if
the fact is true. It can be reopened only by a new fact. Returning PRs three
times on points the owner had accepted the same day only burned rounds; the
owner merged them anyway.

## Automatic RETURN

- an empty diff while work is claimed;
- rerunning CI to get green;
- a weakened assertion or a raised timeout;
- replacing a content check with an existence check;
- changing coverage thresholds, exclusions or suppressions;
- a claimed UI test run without its final screenshot in the PR (separate
  Before/After shots are not required in the PR; the visual delta lives in
  the issue);
- a blocker-closing test that checks a prop or text instead of behaviour;
- a count or claim in the divergence registry, the PR description or a test
  name that does not match the facts on the current head.

## A regression test proves the fix with a red run on broken code

A PASS based on a regression test contains proof of a red run on the base
without the fix, or an explanation, pointing at the render condition or
assertion, of why the test cannot be green without the fix. Without it the
verdict is "not verified", not PASS.

Two typical dummies:

- a "silent drop reconnect" test that really dropped the connection, but its
  wait condition (either the connect button or the error alert) was already
  true at the moment of the click, and its else-branch checked a server name
  the unfixed UI shows forever. Green on the code before the fix;
- a "saved connections" test that saved the form defaults, reloaded into the
  same defaults and clicked the entry: a no-op selection handler passes.

## A test named after a blocker must fail when the fix is reverted

The file name and the test name prove nothing. The test closes the blocker
only if it fails when the fix is removed. The coder shows this in the
handoff: the revert command, the test output before (red) and after (green).
The reviewer repeats the revert, not reads the handoff.

The mutation goes into a PRODUCTION line, not into the test or harness. The
equal alternative is a red run of the new test on the base without the fix.

The mutation must pass the repository's format gate before tests: an inline
comment in the mutated line can fail a formatter check, and the run looks
like a failure without a single test report. Apply mutations as clean lines.

A no-op guard for an await or poll helper must also pass the gates: `false
&& ...` is rejected by static analysis, an early `return;` by the compiler as
unreachable. A working form: append an always-false non-literal comparison
to the loop condition (for example `&& System.nanoTime() < 0`). Revert with
`git checkout -- <file>`, never by a manual reverse edit: hand reconstruction
loses block-opening lines and corrupts the file.

A mutation in two modules in one multi-module run is NOT proven for the
second: the failing first module marks the second as skipped, and "both red"
is read from one red. Run the second module separately and require a finished
exit code for EACH module. Collect test counts from reports fresh by run
time: a shared worktree's report directory accumulates stale files of deleted
classes.

## "Red before the fix" is a mutant inside the test, not live git history

For a test suite that lives next to a frequently committed tree (for example
the agent's own scripts directory under a snapshot job), prove "red on the
code before the fix" with a mutant inside the test itself: a copy of the
source with every old/new pair of the fix reverted at once, or a substituted
function. Do not look for the pre-fix commit in the live history. After the
fix is committed, HEAD equals the current code, so "red against HEAD" proves
nothing; and every later snapshot commit pushes the pre-fix snapshot out of a
walk-back window, so the test turns red on someone else's commit and raises
false alarms in drift monitoring. A self-launching "RED against HEAD" check
that spawns the suite again from inside the suite can also recurse without
bound and exhaust memory: keep the mutant in-process.

How to verify: choose the minimal edit that cancels the fix (remove the
gate, flatten the retry delays, drop the header from the filter), run the
test, show the red output. If no production edit makes the test fail, the
test does not close the blocker: RETURN. Three dummies found this way: a
"no warning logged" test that only checked a bean existed and passed with
four warnings in the log; a retry-ladder test green with all delays equal; a
"forbidden headers" integration test with no application context and no HTTP
request, where `requestCount == 0` was trivially true.

## Check

- the verdict summary contains command output for each item;
- the verdict reads ACCEPTED (what was checked, with what) or RETURN
  (concrete reproduction steps).
