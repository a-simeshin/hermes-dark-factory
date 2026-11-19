---
name: adversarial-review
description: 'Review that tries to refute the work: what blocks and what does not.'
version: 2.1.0
author: maintainer, Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [review, verdict, pr, plan, ci, kanban]
    related_skills: [kanban-discipline, decomposition-contract, tester-verdict-checklist]
---

# Adversarial review

This skill is the reviewer's procedure for plans and pull requests: how to
read the change, what counts as a blocker, how the verdict is formatted so
machines can read it, how repeat reviews are scoped, and which proofs (above
all mutation of the production line behind every fix) a PASS must carry. The
goal is to refute the work, not to approve it, but refuting means proving:
a remark without consequences is not a finding. The full protocol with the
reasoning behind each rule is in references/protocol.md; distilled lessons
are in references/lessons.md; the misses journal format is in
references/misses-template.md.

## Protocol

1. Read the card, the issue mirror and their discussion: decisions made by
   people outrank your assumptions.
2. Read the DIFF, not whole files: `gh pr diff N` or
   `git diff <base>...<head>`. Open files only as fragments around the diff
   lines (read_file with offset/limit); a whole file is acceptable only when
   the diff adds it.
3. Prove every claim: `file:line`, command output, a screenshot, a
   reproduction step. A claim without proof is not published.
4. If the work is bad, say so plainly, without softening.
5. Verdict: ACCEPTED (no provable problems) or RETURN (findings ordered by
   severity CRITICAL / IMPORTANT / MINOR, each with its proof and the minimal
   condition that closes it).
6. Post code findings as a review comment on the PR as well, in the
   repository's language.
7. You do NOT edit code, commit or merge.

## What blocks and what does not

A blocker (the only thing that justifies RETURN): the plan is physically
impossible or guarantees rework; a test is not run by the stated runner
(checked in the build file or workflow); a wrong path or endpoint (checked in
code); no test layer for an affected user path; an acceptance criterion of the
issue that you measured as unmet in a live run (that is RETURN with the
measurement, not a "note").

Not a blocker: an incomplete formal contract when the content is clear;
wording, numbering, section order; style; a wish for more detail.

An async test with a hand-written wait loop is reviewed by procedure, not
only by reading: (a) the loop must also wait from an EMPTY initial state
(continue while "no result yet OR not the wanted one", never "a result
exists AND it is not the wanted one", which exits at once on an empty list);
(b) run the test 20-50 times on the head and read its DURATION each time:
milliseconds against a budget of seconds is the signature of "did not wait",
and green repeats hide it; (c) a production mutant does NOT close this point,
because the defect lives in the test itself. A red soak run on an untouched
sibling test is an unmet criterion, not noise.

Any claim about state outside the run (known flake, tracked, fixed,
duplicated, posted, merged) in a report or PR text needs an artifact link (issue,
PR, card, comment URL, sha). No link: a finding. With a link: verify it by a
live call (`gh pr view`, `gh run list`, `gh search issues`) before trusting it.

NEVER block on something only the owner can close (missing owner approval, an
unapproved narrowing of criteria, a "do we do this at all" decision): verdict
ACCEPTED with the note "awaiting the owner's decision on point X". Blocking
on it sends the executor around a loop it cannot exit.

## The verdict: load-bearing marker and safeguards

- The first line of the comment is EXACTLY `## Review verdict` (or
  `## Re-review verdict`). When the same GitHub App opens and reviews PRs,
  GitHub forbids formal reviews of your own PR, so this comment is the only
  machine-readable sign that a PR was reviewed. "This PR has no review" is
  checked by comments, never by the `reviewDecision` field.
- One PR = one card. You review on the card that owns the PR, dispatched
  from the native review status (`kanban_request_review`); there are no
  separate reviewer cards. "Approved" in the card summary does not finish
  the round: the verdict lands as a PR comment on the exact head BEFORE
  `kanban_complete` or `kanban_request_changes`.
- No self-review: the review must run under the reviewer profile named in
  `kanban_request_review`. If the head was produced by your own profile (no
  reviewer was named and the card came back to its author), publish no
  verdict: hand the card to the reviewer and say why.
- In someone else's card (assignee is not you) refer to the PR by number
  only, never by URL: the dispatcher treats a fresh PR URL in a card comment
  as an "active PR" lock and holds that card for many ticks.
- An existing `## Review verdict` in the thread does not prove an independent
  review: all profiles share one app identity, so check authorship by the
  board's task runs (the profile column). A PASS published by the executor's
  own run does not count.
- A `## Calibration` block at the end of the body is mandatory: the review
  layer does not count a verdict without it. One line per applicable class of
  misses; "N/A" only with a reason. A class line is the record of a check you
  actually did, not a formula.
- A stacked PR is not approved while any lower PR in the stack is unreviewed
  or unmerged: the verdict waits. Merging the upper PR would carry the lower
  PR's unreviewed commits into the base. `mergeable_state=blocked` with
  `mergeable=true` over an unmerged base is branch protection, not a
  conflict: only a moved head stops re-issuing a verdict.
- Before reporting or asking to fix a minor (text, count, list of commits),
  re-read the CURRENT PR body and comments: they may have changed after the
  last verdict. Check the actual commit list and publication dates first.

## Repeat review: delta from the last reviewed head

A repeat review covers ONLY the diff since the last reviewed head plus the
closure of earlier blockers. Run the full checklist again only when the delta
touches a new area or rewrites more than half of the PR. Checking closures is
not a review by itself. Surface untouched since the last round is not
re-reviewed: a new finding there becomes a separate deferred card and does
not block. A finding in code changed in this round is a blocker in any round.
Lens by round: round 1 reads the artifact cold, round 2 runs and checks the
claims yourself, round 3+ checks strictly against the original criteria.

Both extremes are expensive: the full checklist every round produces review
loops that never converge; closure-only re-review lets real blockers through.

## PASS requires a mutation for every blocker

"Incremental re-review only, did not re-audit" is a refusal to give a
verdict. A PASS contains, for EVERY blocker it treats as closed, a mutation
line: which mutation you applied, which tests went red, the output. EVERY
means every blocker this PASS treats as closed, INCLUDING blockers closed in
earlier rounds: the delta lens frees you from hunting new findings on old
surface, not from mutating each fix line. A mutation that no test catches
means RETURN, however green the full run is.

- The mutation goes into the EXACT production line that fixes the blocker
  (the state write, the guard, the branch), not into neighbouring logic of the
  same PR: a red test on a different line proves nothing about this fix.
- The mutation must change BEHAVIOUR. A green run under the mutation means
  the mutation is wrong: reinvent it. Prove the mutant is live with a throw
  probe in the mutated branch or by listing the failing test names.
- A throw probe must sit OUTSIDE the try block that covers the catch under
  test: a throw inside that try is swallowed by the same catch and proves
  only that the catch is live, not the path the exception propagates along.
  Put the throw at the method entry or before the try.
- A test that seeds the very state the fix writes (for example it puts the
  value into storage during setup) does not close anything: the mutant of the
  fix line stays green. That is RETURN; the test gets fixed, not the verdict.
- A test of a prop, a text, a class name or a markup string instead of
  behaviour is not verified by mutation: a red run after deleting the checked
  string proves only that the string exists, not the promised behaviour.
- Do not build the artifact for live checks in the same tree while mutation
  runs are going: the bundle can carry the mutant. Verify the key expression
  inside the built artifact itself.
- The equal alternative to a mutation: a red run of the new test on the base
  without the fix.

## Every promise in the PR body and "What changes for you" is pinned by a test

This applies on EVERY review, first and repeat (on a repeat review, to the
delta of promises since the last reviewed head). Review is a review of
promises: list every statement in the PR body, the `## What changes for you`
comment (WCFY) and accompanying comments (which calls are intercepted, which
errors are written, what the user will see) and map EACH to a test that
catches its violation, verified by a mutation on the exact head. A promise
without a pinning test is a blocker (RETURN), not a deferred finding: the
promise is already published, its fulfilment cannot be postponed to a card.
Mutating the fix line and mutating the promises are DIFFERENT axes: the
first does not cover the second.

A PASS carries a promise table, one row per promise:

    | Promise | Mutant | Test | Result |
    |---|---|---|---|
    | cached content renders at once | drop the cache read on open | CacheOpen test #2 | killed RED |
    | error is cached, Refresh retries | do not write the error payload | CacheOpen test #3 | survived -> BLOCKER |

- Result is the actual colour of a mutant run you did on the exact head.
  "covered", "N/A" or an empty cell without a reason are not accepted;
  survived is RETURN.
- The gate looks for ONE header line naming both "promise" and "mutant"
  anywhere in the comment; position does not matter. A PASS without it is
  not counted. For diffs without production lines (workflow YAML, CI config)
  name the column `Mutant-equivalent probe`. RETURN without the table is
  allowed: the error direction is safe.
- Killed by a test that reads the implementation (class name, "method was
  called", a markup string) does NOT close a behaviour promise: the test must
  observe behaviour (size, visibility, response, state). CSS promises ("never
  overflows") are pinned by a browser measurement on the head, or rewritten
  honestly as "defensive no-op".
- A PR that turns unconditional behaviour into conditional pins BOTH branches
  of the new condition. A one-off red under a mutant that does not reproduce
  is a flake, not a pin.
- Duplicate substrings mask deletion: if the promised element and another
  element share text, a text query is satisfied by both. Pin the element
  itself (a test id on the block) and mutate exactly that block away.
- A verdict rejected only for its FORM on an unchanged head is re-issued as
  text: mutants and the battery are not re-run. Check the body against the
  gate before publishing.

## A third return on the same finding is an escalation

Count rounds per finding. On the third return: (a) the finding is not really
blocking: ACCEPTED plus a deferred card; (b) it is blocking but the executor
cannot close it: `kanban_block` with `kind='needs_input'` and the exact
decision needed; (c) the finding splits: split the card. The review lane does
not count rounds; you have to stop.

## Review lane and returns outside it

| verdict | how you finish |
|---|---|
| accepted | `kanban_complete` |
| return | `kanban_comment` with the findings, then `kanban_request_changes` (returns the card to THE SAME executor) |
| escalation | `kanban_block` |

RETURN on a PR goes back through `kanban_request_changes` of the card that
owns the PR: rework, rebase and the re-request happen on that same card.
Create a rework card (key `coder-pr<N>-rework-<head7>`, never your own
spelling) only when the verdict lives only in a GitHub comment and no card
owns the PR (otherwise nobody picks the verdict up). Routing: the code does
the wrong thing: coder; the code is right but the check does not prove it:
tester; the plan is wrong: architect.

A deferred finding: an issue plus a card (`--triage`) plus the line
`DEFERRED: t_xxxxxxxx` or `DEFERRED: none` as the last line of the summary
(the gate parses it).

## CI and repeat verdicts

- Approve only by `gh pr checks <N>` with the result attached: local lint and
  tests are a different set on a different commit.
- PASS only on a head where ALL required checks are green. A red required
  check cancels PASS whatever its cause (a lower PR in the stack, a flake,
  "unrelated to this diff"): write CHANGES REQUESTED or "Verdict: blocked by
  #N", never "PASS, but...".
- Red CI does not mean broken code: (1) could this change physically cause
  the failure; (2) base history, `gh run list --branch <base>`; (3) a local
  reproduction on the same head. A proven flake: rerun the job, no card.
- A deleted test in the diff needs an explanation:
  `git diff --diff-filter=D --name-only <base>...<branch>`. A silent deletion
  is a dropped guarantee.
- Three hard rules for repeat verdicts: PASS after CHANGES REQUESTED only on
  a commit younger than the return; a verdict only on the PR branch head (not
  on a build with someone else's cherry-picks); an open REQUEST_CHANGES blocks
  a QA PASS until a commit closes it.

## Visual, prototype and WCFY gates

- The issue mirror body starts with `## Before / After`; for UI changes the
  reviewer OPENS the images and compares them with the diff (After shows
  exactly what the code changes, Before comes from the base branch, raw links
  return 200). Missing section or not first: [BLOCK]. The PR itself carries no
  Before/After section: visual evidence lives in the issue at the prototype
  stage.
- A PR implementing a feature/ux issue without the owner's comment
  "prototype accepted" in the issue is [BLOCK], however green CI is. A comment
  by anyone else does not count. The implementation branch starts from the
  integration branch, not from the prototype branch.
- WCFY is published AFTER your PASS (do not demand it earlier). A missing
  WCFY after PASS on the final head is an unfinished delivery step, not a
  review finding.
- Every WCFY promise is checked by a live scenario on every kind of entry the
  feature distinguishes (for example a saved item and an ad-hoc one). A live
  run of only one entry kind proves nothing about the others.
- A UI layout change is checked in the error, connected and empty states,
  not only on a clean load: in the error state a fixed block can eat the
  scroll area and hide exactly the field that fixes the error.
- New code that moves or deletes user data is reviewed by ENUMERATING the
  user entry paths into it (which actions bring the user into this state),
  not only by the scenario in the ticket. One unlisted path is enough to move
  data into the wrong bucket irreversibly.
- Before ACCEPTED check that the PR body answers "what changes for the user
  and how it is proven" (a blocking finding under pr-deliverable).
- User-facing text with claims about third-party projects (an issue number,
  a version, library behaviour) is checked against the primary source:
  `gh issue view -R <org>/<repo> N`, release notes. The link to the primary
  source goes into the verdict. "The render matches the diff" only proves the
  text reached the screen, not that it is true. Also look for dead code around
  such text (props or comments pointing to "future" PRs long merged).

## Reviewing a rebase

0. Compare the head's CI start time with base commits merged AFTER it: CI
   never tested that combination. Merge the base tip into a worktree of the
   head locally and run the suite.
1. `git range-diff <old-base>..<old-head> <new-base>..<new-head>`: `!` outside
   the conflict region is an extra. The PR's file sets must match. After a
   rebase done in a worktree, grep for symlinks:
   `git ls-tree -r <head> | grep '^120000'`.
2. Check the conflict resolution in the FINAL file: both sides present, no
   duplicates, intent preserved.
3. A mutation on EACH side of the conflict, with red tests of exactly that
   side.
4. "PASS on a rebase of a previously approved head" is a review of promises:
   the promises section above applies in full. Mutations on conflict sides do
   not replace mutations on promises; they are different axes.
5. A negative test must be able to fail: check that its scenario reaches the
   branch under test instead of returning earlier.

## Reviewing ops changes in ~/.hermes

1. Compare `metadata.changed_files` with `git show <sha> --stat`: the claimed
   commit may not contain the main file (RETURN, not a note).
2. "Live run confirmed" is checked together with the delivery channel: fresh
   files in `cron/output/<job-id>/` and their status.
3. Date the authorship of edits in a dirty working copy by the profile's
   `state.db`: separate the executor's edits from others before the verdict.

## Verdict checklist

- issue body starts with `## Before / After`, images compared with the diff;
- third-party claims opened at the primary source, link in the verdict;
- feature/ux issue: the owner's "prototype accepted" comment exists;
- every claim has proof; the report says "CI: N of M green";
- every blocker treated as closed (this round and earlier) has a mutation of
  its exact fix line that turns a test red;
- every promise in the PR body and WCFY has a row in the promise table,
  killed by a test that observes behaviour;
- a new async wait loop: waits from an empty state, 20-50 repeats with
  durations;
- every claim about external state carries an artifact link you opened;
- minors checked against the current PR body and comments;
- the review runs under the named reviewer, not the head's author;
- a diff that models a third-party library's output: a `Library contract`
  line in the PR body and a fixture produced by running the library;
- repeat review: closures plus the delta since the reviewed head;
- stacked PR: every lower PR reviewed or merged;
- PASS after CHANGES REQUESTED on a younger commit; verdict on the branch
  head; no QA PASS over an open REQUEST_CHANGES;
- `## Calibration` block present, one line per applicable class;
- non-blocking findings: issue plus card plus the DEFERRED line.

## References (read on demand)

- references/protocol.md: the full protocol with the reasoning behind each
  rule
- references/lessons.md: distilled lessons (plan review, mutation battery
  infrastructure, headless browser traps, library contracts, races, domain
  knowledge, mutant probe traps, rebase and test-count reconciliation)
- references/misses-template.md: the misses journal format and how to read it
  before every verdict
