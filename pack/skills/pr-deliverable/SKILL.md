---
name: pr-deliverable
description: What a PR must contain for the work to count as delivered.
version: 2.2.0
author: maintainer, Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [pr, github, evidence, screenshots, verdict]
    related_skills: [kanban-discipline, worker-run-discipline, issue-mirror]
---

# Delivering work in a PR

How an agent delivers work. The result lives in the PR, not in chat and not
in the card. One PR is
owned by one card: that card delivers, goes through review, takes the rework
and absorbs any rebase. The full protocol with precedents is in
references/protocol.md; distilled lessons in references/lessons.md; the
visual delta in references/visual-delta.md.

## When to use

You deliver code, publish a verification or review verdict, or close a card
that has a PR.

## Git and PR protocol

- Branch `hermes/<short-name>`, atomic commits, NO Co-Authored-By or
  generator footers.
- Push with a plain `git push origin <branch>`; the pack's credential helper
  mints the GitHub App token (see the setup docs for the fallback when the
  helper is broken).
- PR: `gh pr create ... --base <branch from the project conventions>`. The
  base is NOT the default branch by default.
- `gh pr update-branch` cannot resolve conflicts (422): merge locally and
  resolve. An automatic merge can combine signatures from both sides and keep
  a test that pins the old semantics; re-pin the test to the merged semantics.
  Before pushing run the targeted tests of the changed modules plus format
  and lint; the full verify runs in CI after the push.
- Link to the issue: `Refs #N` when several PRs serve one issue, `Closes #N`
  when the PR closes it (Closes closes the issue on the first merge). Prose
  like "see #56" builds no link. `Refs` does NOT fill
  `closingIssuesReferences` (only a closing keyword does); the machine-visible
  link is the `cross-referenced` event in the issue timeline. An empty field
  under `Refs` is not a missing link; never switch to `Closes` just to fill it.
- `metadata.published_pr` on the card is the full PR URL, not a number.
- If your gh wrapper cannot parse `-R owner/repo` and fails closed, `cd` into
  the repository checkout and run gh without `-R`/`--repo`.

## Keeping gh output small (budget rules)

- always use gh with `--json <fields>` plus `--jq` to trim; never a bare
  `gh pr view N` (body plus all comments was once the reviewer's largest
  output cost);
- `git diff` / `git show`: `--stat` first, then only the needed files; write
  a full diff to a file and read the piece you need;
- a PR or issue number in text comes only from a live `gh pr view N --json
  ...` in this session, with the PR title next to it; never from memory.

## WCFY: part of the handoff, BEFORE kanban_request_review

The `## What changes for you` (WCFY) comment is part of the handoff, next to
the code and the tests: the card that owns the PR publishes it BEFORE
`kanban_request_review`, on the head that goes to review. The reviewer
checks WCFY as part of the deliverable: a missing WCFY means the handoff is
incomplete and no PASS is posted; a WCFY promise that the code and tests do
not keep means CHANGES REQUESTED (adversarial-review). WCFY is not a step
after the approval: in the native review lane the reviewer's approval closes
the card in one transition, and the implementer gets no turn after it.
Three parts: what changes for the user / how it is proven (screenshots,
output, diff) / what was run (commands and numbers).

- Rework after CHANGES REQUESTED: update WCFY before the REPEATED
  `kanban_request_review` (numbers from live commands on the new head).
  PATCH the old WCFY by its NUMERIC comment id (not the node id) and put the
  marker `outdated PATCH-<id>` before its heading, so exactly one WCFY
  candidate remains for machines.
- "Is there a WCFY yet?" is answered by the FIRST LINE of each comment, not
  by a substring grep (prose mentions give false positives).
- Every number (test counts, hashes) comes from a live command of THIS run on
  the head under review, not from an old battery.
- Safety net, not the normal path: the watchdog's WCFY close gate catches a
  closed card whose open PR has an accept verdict and no WCFY, and files a
  task card for the implementer.
- Exception: when the closing recipe explicitly lists WCFY among its steps,
  publishing it is executing that decision; every earlier step of the recipe
  is verified by live commands first.

## Rework: answer in the PR and re-request review

Whoever pushes a new head after CHANGES REQUESTED or RETURN, whichever card
carried the work, must:

- post a rework response in the PR: one answer per BLOCK item of the verdict
  (what was done, the commit, the mutation or red run that proves it);
- update WCFY on the new head (see the WCFY section): the reviewer checks it
  as part of the handoff;
- request review again with `kanban_request_review` naming the reviewer
  explicitly (without a reviewer the card is not reassigned and the author
  reviews itself), then exit; the verdict is not awaited inside the run.

Without the response the reviewer gets a bare new head and reviews blind.

## Only the merge is left to the owner

Update-branch or rebase (422 means a local merge), waiting for required
checks on the new head, `gh run rerun` for an infrastructure flake, a fresh
WCFY and a repeat review of the new head are agent work, done by the card
that owns the PR (no separate rebase card). "Owner-only" is allowed in texts
only for the merge or for decisions with no technical path (milestone,
prototype acceptance, repository permissions). A card closed with "remaining:
owner merge" keeps that signal until the PR is merged.

## Delivery texts equal the facts

Before `kanban_request_review` and after ANY rework, reread ALL delivery
texts against the current head's diff: PR body, WCFY, divergence registry
entries, test names, commit messages. If no command can check a statement,
do not write it.

- "Done" is written only when the artifact is exactly what was asked;
  otherwise "not done, I propose X".
- "Green" means quoting verbatim the final summary line of your own run of
  the test runner; no invented markers.
- A follow-up promise ("tracked as a follow-up") references an existing
  issue or card number on the same line; otherwise create it first.
- An issue criterion is never rewritten: compare with its original wording.
- "Before" for the user means the current integration branch, not an earlier
  head of the PR.
- The owner's acceptance is quoted in the owner's words, not paraphrased.

Edit a body or comment with PATCH and a JSON payload (`--input file.json`):
`--field body=@file` fails as multipart, `-f body=@file` inserts the literal
path. Do not carry the trailing `\n` from `--jq .body`.

## A mutation for every closed blocker

Every fixed blocker is closed by one of two proofs: (1) a mutation of the
production line, with the new test red on the mutation and green on the fix;
(2) a red run of the new test on the base before the fix. A prop or text
test does not count; the reviewer repeats the mutation.

- Behaviour cannot be proven (the measurement does not change)? Remove the
  claim from the body, WCFY and registry instead of pinning a class name or
  markup string: such a test pins a false promise.
- Order: commit, mutate, run, `git checkout --` (on an uncommitted head the
  checkout reverts the fix, not the mutant).
- Run the battery in the background (`terminal(background=true)`) with a
  log: `execute_code` times out and can leave a mutant in the tree; after any
  interruption run `git status` first.
- A surviving mutant with no killer is dead code: delete the line and shrink
  the matrix, or diagnose with a print (test stdout may go to the test report
  files, not the build log).
- `gh pr comment --body-file <real file>`, not stdin.

## Frames and screenshots

- UI verdict: before/after frames for each checked viewport, PNG pushed to
  the assets branch (`hermes-assets` by default), markdown by raw link.
  Telegram or the card alone do not count.
- A selector fails: read the component source first (how the test id is
  really attached), then probe the DOM; the other order is a lottery.
- Evidence is checked against its claim BEFORE it is published. Three
  checks: files with different captions have different hashes (run
  `sha256sum` on every file you post; the same hash under two captions is a
  mistake, not a coincidence); the frame shows what the caption says
  (connected state, badge, value); the file name matches the scenario in
  the caption. Write the short sha256 next to each file in the evidence
  comment, so the reviewer can recompute it from the raw link with one
  command.
- The final screenshot of an E2E test is the test's artifact. Where the
  project keeps the visual delta in the issue, the PR gets no separate
  Before/After shoot (references/visual-delta.md).

## No drive-by edits

The diff contains only the card's files. CI and gate changes always go in a
separate PR. Deleting someone else's test is an automatic RETURN. No
cosmetic sweeps over other people's lines (punctuation, dashes, whitespace);
if a house-style hook forbids a character that the base contains, restore the
base form only by the procedure in references/lessons.md and only in the
ticket's files.

## Merge: only after a PASS younger than the last commit

1. The last `## Review verdict` is PASS and younger than the branch's HEAD
   commit (a push after PASS burns the verdict).
2. No CHANGES REQUESTED is younger than the last PASS.
3. CI is green on the last commit.

Check: `gh pr view <N> --json comments --jq '[.comments[] | select(.body |
startswith("## Review verdict")) | {author: .author.login, createdAt:
.createdAt}] | last'`.

## No runs on the PR

"No CI run after a push" STARTS with `gh pr view <N> --json
mergeable,mergeStateStatus`; "a GitHub glitch" is not a diagnosis without
that check. A conflict with the base means the merge ref is not computed and
GitHub creates NO run at all; the API may report a stale MERGEABLE until a
new head is pushed. Then check suites for the SHA (0 means no event), then
`actions/runs` (are other branches running?). An empty commit or close/reopen
does not help; only a local merge of the base or a rebase does. A rerun only
restarts an existing run; a 403 on rerun means degraded permissions: tell the
owner.

## Checklist before delivery

- `gh pr view <N> --json baseRefName,files` matches the card (dozens of
  unrelated files mean the wrong base);
- the visual delta is where the project keeps it (references/visual-delta.md);
- WCFY: posted before kanban_request_review on the head under review,
  updated before each repeated request after a rework, numbers from live
  commands;
- every closed blocker has a mutation or a red run on the base attached;
- after a rework: a rework response per BLOCK item and review re-requested
  with the reviewer named;
- tests that model a third-party library's output: a `Library contract:
  <artifact>:<version> <Class>#<method>` line in the body and a fixture from
  a real run;
- CI green; the last verdict is a PASS younger than the last commit;
- `Refs/Closes #N` checked by grepping the PR body, the number from a live
  command; no board card ids in place of issues; every follow-up promise
  carries a number;
- `metadata.published_pr` is the full PR URL;
- no drive-by edits;
- self-check right after `gh pr create` (base, file count:
  `gh pr diff N --name-only | wc -l`).

## References (read on demand)

- references/protocol.md: the full git and PR protocol, WCFY, rework
  response, merge and CI rules with their reasoning
- references/visual-delta.md: the visual delta (Before / After for
  UI-facing issues)
- references/lessons.md: distilled lessons (prototype gate, noise in PRs,
  same-mechanism fixes, garbage input, library contracts, house-style
  restoration, stack hygiene, rebase conflict handling)
