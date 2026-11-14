---
name: tester-verdict-checklist
description: The checklist without which a verification verdict is invalid.
version: 2.0.0
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
3. Build: the project's compile command for the affected modules (from the
   project conventions).
4. Tests of the affected modules, with the count attached.
5. The card's acceptance criteria, one by one, actually exercised.
6. Regression: `find_referencing_symbols` on every changed symbol.
7. CI on the PR: `gh pr checks <N>`; red means RETURN with an excerpt from
   `gh run view <id> --log-failed`, even when local runs are green. A local
   run does not replace CI.
8. The PR base follows the project conventions, not the default branch.
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
- QA does not PASS over an open REQUEST_CHANGES from the reviewer.

The owner's decision on a point closes it for the verdict. Only a new fact
reopens it (a commit after the decision, new evidence).

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
- a count or claim in the divergence registry, WCFY or a test name that does
  not match the facts on the current head.

## Check

- the verdict summary contains command output for every item;
- the verdict is ACCEPTED (what was checked, with what) or RETURN (exact
  reproduction steps).

## References (read on demand)

- references/protocol.md: the full checklist, precedents, analysis of dummy
  tests and surviving mutants
- references/lessons.md: cheap checks before expensive waits, expected test
  counts are measured on the base, a "flake" is proven, the test pyramid and
  the test-infrastructure contract
