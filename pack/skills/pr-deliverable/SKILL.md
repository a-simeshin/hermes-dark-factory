---
name: pr-deliverable
description: What a PR must contain for the work to count as delivered.
version: 2.0.0
author: maintainer, Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [pr, github, evidence, screenshots, verdict]
    related_skills: [kanban-discipline, worker-run-discipline, issue-mirror]
---

# Delivering work in a PR

This skill defines how an agent delivers work: the git and PR protocol, how
to keep gh output small, the single "What changes for you" comment, the rule
that only the merge is left to the owner, why every delivery text must equal
the facts, the proof required for each closed blocker, screenshots, merge
conditions and what to do when a PR has no CI runs. The result lives in the
PR, not in chat and not in the card. The full protocol with precedents is in
references/protocol.md; distilled lessons in references/lessons.md; the
visual delta (which lives in the issue, not the PR) in
references/visual-delta.md.

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
  Verify before pushing.
- Link to the issue: `Refs #N` when several PRs serve one issue, `Closes #N`
  for a single PR (Closes closes the issue on the first merge). Prose like
  "see #56" builds no link.
- If your gh wrapper cannot parse `-R owner/repo` and fails closed, `cd` into
  the repository checkout and run gh without `-R`/`--repo`.

## Keeping gh output small (budget rules)

A bare `gh pr view N` (body plus all comments) was once the reviewer's
single largest output cost. Rules:

- always use gh with `--json <fields>` plus `--jq` to trim; never a bare
  view;
- `git diff` / `git show`: `--stat` first, then only the needed files; write
  a full diff to a file and read the piece you need;
- a PR or issue number in text comes only from a live `gh pr view N --json
  ...` in this session, with the PR title next to it; never from memory.

## WCFY: one comment, after PASS, on the final head

The `## What changes for you` (WCFY) comment is published ONCE, after the
reviewer's PASS, on the final head; rounds of changes live in the PR body.
Three parts: what changes for the user / how it is proven (screenshots,
output, diff) / what was run (commands and numbers).

- The head moved on: PATCH the old WCFY by its NUMERIC comment id (not the
  node id) and put the marker `outdated PATCH-<id>` before its heading, so
  exactly one WCFY candidate remains for machines.
- "Is there a WCFY yet?" is answered by the FIRST LINE of each comment, not
  by a substring grep (prose mentions give false positives).
- Every number (test counts, hashes) comes from a live command of THIS run on
  the final head, not from an old battery.
- Exception: when the closing recipe explicitly lists WCFY among its steps,
  publishing it is executing that decision; every earlier step of the recipe
  is verified by live commands first.

## Only the merge is left to the owner

Update-branch (422 means a local merge), waiting for required checks on the
new head, `gh run rerun` for an infrastructure flake, a fresh WCFY and a
repeat review of the new head are agent work. "Owner-only" is allowed in
texts only for the merge or for decisions with no technical path (milestone,
prototype acceptance, repository permissions).

## Delivery texts equal the facts

Before `kanban_request_review` and after ANY rework, reread ALL delivery
texts against the current head's diff: PR body, WCFY, divergence registry
entries, test names, commit messages. If no command can check a statement,
do not write it. Edit a PR body with a JSON payload via `--input file.json`
(`--field body=@file` sends multipart and fails); when editing byte for byte,
do not carry the trailing `\n` from `--jq .body`.

- "Done" is written only when the artifact is exactly what was asked;
  otherwise "not done, I propose X".
- An issue criterion is never rewritten: compare with its original wording.
- "Before" for the user means the current integration branch, not an earlier
  head of the PR.
- The owner's acceptance is quoted in the owner's words, not paraphrased.

## A mutation for every closed blocker

Every fixed blocker is closed by one of two proofs: (1) a mutation of the
production line, with the new test red on the mutation and green on the fix;
(2) a red run of the new test on the base before the fix. A prop or text
test does not count; the reviewer repeats the mutation.

- Order: commit, mutate, run, `git checkout --` (on an uncommitted head the
  checkout reverts the fix, not the mutant).
- Run the mutation battery in the background (`terminal(background=true)`)
  with a log file: `execute_code` has a hard timeout and can leave a mutant
  in the tree. After any interruption run `git status` first.
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
- The final screenshot of an E2E test is the test's artifact; no separate
  Before/After shoot in the PR (the visual delta lives in the issue:
  references/visual-delta.md).

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

A conflict with the base means the merge ref is not computed and GitHub
creates NO run at all. Order: `gh pr view --json mergeable,mergeStateStatus`,
then check suites for the SHA (0 means no event), then `actions/runs` (are
other branches running?). A rerun only restarts an existing run; a 403 on
rerun means degraded permissions: tell the owner.

## Checklist before delivery

- `gh pr view <N> --json baseRefName,files` matches the card (dozens of
  unrelated files mean the wrong base);
- the issue body starts with `## Before / After`; the PR has no Before/After
  section;
- WCFY: one, after PASS, on the final head, numbers from live commands;
- every closed blocker has a mutation or a red run on the base attached;
- tests that model a third-party library's output: a `Library contract:
  <artifact>:<version> <Class>#<method>` line in the body and a fixture from
  a real run;
- CI green; the last verdict is a PASS younger than the last commit;
- `Refs/Closes #N` checked by grepping the PR body, the number from a live
  command; no board card ids in place of issues;
- no drive-by edits;
- self-check right after `gh pr create` (base, file count:
  `gh pr diff N --name-only | wc -l`).

## References (read on demand)

- references/protocol.md: the full git and PR protocol, WCFY, merge and CI
  rules with their reasoning
- references/visual-delta.md: the visual delta in the issue (not in the PR)
- references/lessons.md: distilled lessons (prototype gate, noise in PRs,
  same-mechanism fixes, garbage input, library contracts, house-style
  restoration, stack hygiene)
