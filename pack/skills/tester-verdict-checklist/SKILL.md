---
name: tester-verdict-checklist
description: The checklist without which a verification verdict is invalid.
version: 2.1.0
author: maintainer, Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [tester, qa, verdict, ci, checklist]
    related_skills: [pr-deliverable, debugging-discipline, kanban-discipline]
---

# Verdict checklist

This skill is the tester's checklist for an ACCEPTED or RETURN verdict on a
card's work: what must be verified, by which command, how a regression test
proves a fix (a red run on broken code), which situations are an automatic
RETURN, and the hard rules for verdicts after a return. A verdict without the
completed checklist is invalid: every item is closed by command output, not
by the word "checked". Precedents and expanded rules are in
references/protocol.md, distilled lessons in references/lessons.md.

## Items

1. The branch and diff exist: `git log <base>..<branch>` is not empty, the
   diff equals the ticket (no more and no less).
2. `git status --porcelain` in the worktree is empty (only status shows
   untracked files).
3. Build: the project's compile command for the affected modules (from project
   conventions § Build and test commands).
4. Tests of the affected modules, with the count attached. "Green" is the
   verbatim final summary line of YOUR OWN run of the test runner, quoted;
   no invented markers, no line copied from someone else's handoff.
5. The card's acceptance criteria, one by one, actually exercised.
6. Regression: `find_referencing_symbols` on every changed symbol.
7. CI on the PR: `gh pr checks <N>`; red means RETURN with an excerpt from
   `gh run view <id> --log-failed`, even when local runs are green. A local
   run does not replace CI.
8. The PR base follows project conventions § Integration branch and PR base, not the default branch.
9. No infrastructure extras outside the ticket (dependencies, CI configs,
   runners); anything CI does not run is RETURN.
10. If the project keeps a registry of divergences from an upstream (for
    example a NOTICE directory for a vendored client), a diff touching the
    vendored code without a registry entry is RETURN.
11. `Closes #N` or `Refs #N` in the PR body; the issue contains every section
    of the template.

## Hard rules for a verdict after a return

- PASS after CHANGES REQUESTED only on a commit younger than the return;
- a verdict only on the PR branch head, never on a build with someone else's
  cherry-picks;
- QA does not PASS over an open REQUEST_CHANGES from the reviewer;
- the QA verdict header is exactly `## QA verdict` (for example
  `## QA verdict: PASS`). Never `## Review verdict (QA)` or any other
  "Review verdict" variant: that header is machine-reserved for the reviewer,
  and a QA verdict under it was once counted as the review, sending the PR to
  the owner unreviewed.

The owner's decision on a point closes it for the verdict. Only a new fact
reopens it (a commit after the decision, new evidence).

## Flake: "noise" is proven by mechanism, not by a rerun

A green rerun proves instability, not the CAUSE. Before a "runner noise" or
"flake" verdict all three are required:

- (a) the failing test's duration across several runs compared with known
  numbers (a sequential baseline of N calls x T, timeouts, sleeps, limits):
  a duration matching a known number is the signature of a MECHANISM;
- (b) a comparison with a neighbouring stack or configuration in the same run
  on the same data (one stack fast, the other slow is a property of the
  stack, not of the runner);
- (c) step timestamps from inside the test (server-side log times) that
  rebuild the execution timeline.

Without (a)-(c) write "cause not established", not "flake". A "runner
noise" label once held for two weeks on green reruns while every red sample
sat on the sequential baseline: it was a product serialisation bug. A
one-percent margin with a known mechanism (two clock samples, a 1 ms window)
is a legitimate flake: a known number met a known mechanism.

## Sampling repeated CI runs

Repeated CI runs (budget stability, flake statistics) are collected in short
visits, never by waiting inside one run:

- the sampling contract is written into the card body: the head SHA, the
  number of samples, where samples are written, when the next visit runs;
- one run triggers the reruns and exits with a handoff (what was started,
  where results will appear); the next visit is a scheduled card
  (`kanban schedule` to the expected CI finish) or a CI-completion event;
- samples are appended to the workspace, each visit reports the new rows;
- sample only on the final head after the reviewer's PASS and compare
  `headRefOid` with the head of existing samples: a new head restarts the
  count. A push cancels running reruns in the same concurrency group, and
  the other way round.

Why: a run waiting about 20 minutes per sample holds a dispatcher slot for
hours, and a later push voids every sample it collected.

## Mutations and regression tests

- A PASS based on a regression test contains a red run on the base without
  the fix (or an explanation, pointing at the condition, of why the test
  cannot be green without the fix). Otherwise "not verified".
- A test named after a blocker closes it only by failing when the fix is
  reverted. The coder shows this in the handoff (revert, red, green); the
  reviewer repeats the revert, not reads the handoff.
- The mutation goes into a PRODUCTION line, changes BEHAVIOUR, and is a
  clean line without inline comments (formatter gates reject those before any
  test runs). For an await or poll helper, a no-op guard that passes static
  analysis is an always-false non-literal comparison in the loop condition.
  Revert a mutation with `git checkout -- <file>`, never by hand.
- "Red before the fix" is proven by a mutant INSIDE the test: a copy of the
  source with the fix reverted (all old/new pairs applied at once) or a
  substituted function. Never by walking back the live git history: after
  the fix commit HEAD equals the current code, so a check "red against HEAD"
  is blind, and every later commit pushes the red snapshot out of a
  walk-back window, giving false REDs.
- A two-layer gate (an effect guard plus a callback guard, for example):
  removing ONE layer is an equivalent mutant when the other layer blocks the
  same behaviour. Mutate both layers in one probe and note in the table that
  the single removal survives.
- A mutation in two modules within one multi-module build is NOT proven for
  the second (it is reported as skipped): run each module separately and
  record its exit code.
- Take test counts from reports fresh by timestamp: report directories keep
  stale files of deleted classes.
- A test of a prop or text instead of behaviour does not close a blocker.
- A mutant run is valid only after a GREEN run on the untouched (pristine)
  base: base first, then mutants. After any change to the test harness itself
  (even technical) repeat the whole mutant run: the harness change can void
  earlier kills.
- `pgrep -f` does not prove a background process is alive: the pattern also
  matches the wrapping shell's command line. Filter out the wrapper or trust
  the exit code and the log tail.

## Automatic RETURN

- an empty diff; rerunning CI until green; a weakened assertion or a raised
  timeout; replacing a content check with an existence check; changing
  coverage thresholds, exclusions or suppressions;
- a claimed UI test without its final screenshot in the PR (separate
  Before/After shots in the PR are not required: the visual delta lives in
  the issue);
- a count or claim in the divergence registry, the PR description or a test
  name that does not match the facts on the current head.

## Deferred UI findings

A UI finding deferred from an earlier QA round is re-checked by replaying
the real input (the same clicks and keystrokes through the e2e runner), not
by reading the code that claims to fix it. A programmatic click bypasses
hit-testing and cannot confirm the finding is gone.

## Check

- the verdict summary contains command output for every item, and the green
  claim quotes the final summary line of your own runner run;
- the header is `## QA verdict`;
- the verdict is ACCEPTED (what was checked, with what) or RETURN (exact
  reproduction steps).

## References (read on demand)

- references/protocol.md: the full checklist, precedents, analysis of dummy
  tests and surviving mutants
- references/lessons.md: cheap checks before expensive waits, expected test
  counts are measured on the base, a "flake" is proven, the test pyramid and
  the test-infrastructure contract
