---
name: kanban-discipline
description: Creating, blocking, prioritising and handing off board cards.
version: 2.1.0
author: maintainer, Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [kanban, board, priority, blocking, review]
    related_skills: [worker-run-discipline, save-lesson-before-complete, issue-mirror]
---

# Board discipline

Every rule here was written after a concrete failure; the reasoning is in
references/protocol.md, one-off lessons in references/lessons.md.

## When to use

You create a card, block it, set its priority, hand work to review, or send
someone else's work back.

## First: native or live already exists

Before you create, reassign, restore or remove anything on the board, check
whether a native Hermes mechanism or a live card already does it: the review
lane after `kanban_request_review`, native respawn and its guards (active_pr,
recent_success), task_links and dependency_wait, a verdict on this head, a
live card with the same key (issue or PR + role + head). If one does, create
nothing: extend it or wait for it. Your own layer counts too: before building
a new gate or watchdog, walk the list of local mechanisms (deviations file,
scripts, plugins) and extend the existing one. Restoring something the owner
closed or archived is a question to the owner, never a new card or a manual
unarchive past the gates.

No card for already delivered work: an open issue is covered if a merged PR
references it and was merged after the last scope change (creation, label or
title change; milestone changes and comments are not scope), or a done
executor card links a merged PR. Delivered work that is not a release chore
gets a question to the owner ("what is left on #N"), not a card.

## Creating a card: checklist

- `--skill`: ONLY names from `hermes skills list`. Technology keys from the
  card's Stack line are not skills: an unknown name crashes the worker at
  start-up, three crashes in a row trip the circuit breaker;
- `--assignee`: only profiles shown by `hermes kanban assignees` with
  `on disk = yes`. `no` means skipped_nonspawnable; no assignee at all means
  skipped_unassigned and a false stop signal for any watchdog;
- `--priority` is mandatory (queue order is `priority DESC, created_at ASC`).
  Take it from the milestone band of the card's OWN issue (see Priority
  bands), not from memory. Live card: `hermes kanban edit <id> --priority N`;
  agents never edit the board database;
- `--idempotency-key <role>-<issue|PR>-<verb>` on every card an agent
  creates (exceptions: the auto-decomposer, cards created by a human). It is
  given in the create command; `edit` cannot add it later;
- create through the CLI, `hermes kanban --board <board> create ...`, and
  verify body, priority and parents in task_links;
- a card whose deliverable is a PR in a repository with required checks:
  `--completion-contract <org>/<repo>`, so it cannot close without a green
  PR head;
- cards created by the GitHub bridge arrive with `assignee: null`: assign a
  role at once; status review does not mean profile reviewer;
- a beacon card or a wait on a human: `--initial-status blocked` (or block
  needs_input) with a release-condition line, NOT ready/todo;
- created a card by mistake (wrong verb, probe, duplicate): the creator
  archives it itself, at once, with a comment giving the reason. No probes on
  the live board: Hermes has no per-board dispatcher settings, so a "sandbox"
  board inside the live home is dispatched like any other. Use a separate
  `HERMES_HOME` or read-only commands;
- a comment on a card that has yet to work on a PR mentions the PR by number
  and head sha, not by URL: a PR comment link in a card comment trips the
  native active_pr guard and holds the card for a day. The body may carry
  URLs.

## Mechanical cards on a cheap model

Mechanical rebases, documentation edits, notice files and annotations can be
pinned to `<cheap-model>`. Never on it: reviews and verdicts, decomposition
and plans, multi-file refactors, SEMANTIC rebase conflicts (a mechanical
rebase that meets one blocks with a question).

## Priority bands

- 100: bring an open PR to merge (owner verdict, last fixes);
- active milestone: 80; next milestone: 40; any other milestone: 20 (the
  bands come from `milestone_bands.py`; no milestone means no band);
- the band comes from the card's own anchor (issue number in the title, the
  `Issue:` line, or the anchors of its ancestors). A PR or issue of another
  milestone mentioned in passing in the body does not raise it, and neither
  does an owner comment without an explicit "do it now" or milestone change;
- next-milestone ideas stay out of ready while the active one has
  unblockable work;
- lifting a priority: walk up task_links to the first open ancestor. If it is
  blocked, unblock it or ask the owner; a child's priority behind a blocked
  ancestor buys nothing;
- children of a decomposition inherit the root's priority (the core sets 0).

## Links and blocks: pick the kind by what you wait for

- waiting for another card: `link`, not a block. The dependency holds both
  start and completion. Card ids of this board inside a needs_input reason
  mean the wrong kind was chosen;
- waiting for a human: `block --kind needs_input` with the question in the
  reason. A block without a kind is invisible to owner-question reports;
- waiting for a PR merge: needs_input with ONE line that carries the machine
  check: `Release condition: PR #N merged (gh pr view N --json state ==
  MERGED)`. A watchdog can execute such a line; composite conditions
  (milestone, owner decision, re-review) stay with a human;
- parking until a moment: ONLY `hermes kanban schedule` with SCHEDULED_UNTIL
  (UTC). Never a transient block: two transient blocks in a row drop a card
  with run history into triage, where the decomposer rewrites it from its
  stale body. Update the body (`kanban edit --body`) at every handoff;
- `--initial-status blocked` is released ONLY by hand;
- canonical dependency chain: create in ready, then `block --kind
  dependency`. A card created blocked and then dependency-blocked waits
  forever;
- a `dependency` block does NOT hold a card whose parents are done: it
  auto-promotes and loops.

## One PR = one card

The card that owns a PR carries its whole cycle up to the owner's merge.
There are no separate reviewer, rework, rebase or summary cards for a PR.

- review: `kanban_request_review` on the owner card with an explicit
  `reviewer` (without it the author reviews itself), then exit. Someone
  else's verdict is never awaited inside a run;
- the native review lane IS the review: never create a reviewer card for a
  head that is in review or already has a verdict;
- reject: the reviewer calls `kanban_request_changes`; rework happens on the
  same owner card, in the PR's branch, with a rework response in the PR and a
  new review request;
- rebase: only when the PR is CONFLICTING (or the owner asks), done by the
  owner card, once, last before the final review. After each merge, check
  which open PRs overlap it by files: an affected PR gets one "needs rebase
  because of #N" decision, the owner gets a suggested merge order;
- rebase burns the iteration budget: change the strategy, not the queue.
  Read the run transcript first, then continue from the workspace, merge the
  integration branch in once instead of rebasing N commits, or split by
  conflicting files, and write the new strategy into the body;
- a delivered PR is not decomposed and not sent back to triage: close the
  card with "PR #N" and file the remainder as a NEW card whose body says
  what is already in the PR;
- a card closed with "remaining: owner merge" keeps a signal and a release
  condition until the PR is merged;
- WCFY is a step of the owner card: between the reviewer's accept verdict on
  the exact head and `kanban_complete`, the same card posts a fresh
  "## What changes for you" comment (pr-deliverable). Closing without WCFY on
  the accepted head does not finish the card: the native completion contract
  checks only CI gates of the head, so the watchdog's owner-merge hold flags a
  closed card whose open PR has an accept verdict and no WCFY newer than the
  head (marker `WCFY_HOLD_MARKER`, owner class; the PR number is also read
  from the summary of the finishing run);
- an owner CHANGES REQUESTED after the card closed opens one new card for
  the cycle; decompose from the LATEST verdict of the PR (all reviewers):
  the head may already have closed older blockers;
- ready for the owner means a REVIEWER verdict on the exact current head. A
  QA verdict does not count;
- `metadata.published_pr` is the full PR URL.

## A block produces an action, not text

- a verdict on a PR is not a card on the board: before closing or reviving a
  stale PR, look for open cards by PR number and by blocker sha;
- in the same turn either (a) put the work on a card with an assignee and
  acceptance criteria, or (b) ask the owner in `<owner-chat>` with concrete
  options. Status goes where the owner sees it (PR/issue comment plus chat);
  a card comment alone does not count. An owner question older than a day
  must appear in the attention report;
- UNBLOCK does not remove the cause: identical durations of repeated failures
  are the signature of a limit. Unblock after a timeout or iteration budget
  only together with decomposition, a higher `--max-runtime`, or goal mode.

## Closing

- before closing an issue the board holds no open cards for it:
  `hermes kanban --board <board> list --json | jq ... select(.body | test("#N\\b"))`;
- before closing a card: no live parents, no self-links in task_links;
- one card = one root cause, sized to the worker's iteration budget;
- a gate said no (pre-commit, CI, coverage, reviewer): fix the cause or block
  with a question. Bypassing it is not an option: gates are closed with code,
  never weakened;
- `kanban_complete` returned infra or stale: retry in the same run, up to 3
  attempts 60-120 s apart (check live status before each); block transient
  only after the third refusal, quoting receipt.detail verbatim;
- a claim about how a mechanism behaves ("returns by itself", "the gate lets
  it through") needs evidence from this session: a test on this code or a
  check on the live board. Otherwise write "not verified". Only a red test
  before the fix and a green one after prove "works now".

## Check

- every `--skill` is in `hermes skills list`, every `--assignee` is on disk;
- the new card has `--priority` from its band and `--idempotency-key`;
- nothing native or live already covers it;
- `kanban_request_review` names the `reviewer`; no reviewer card exists;
- before closing: no live parents, no cycles in task_links.

## References (read on demand)

- references/protocol.md: the full protocol with the failure behind each rule
- references/lessons.md: link and completion order, umbrella coordinator, CLI
  mechanics, key at create time, verdict routing and no merges by workers,
  rework in the PR's branch
