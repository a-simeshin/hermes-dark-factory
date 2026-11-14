---
name: kanban-discipline
description: Creating, blocking, prioritising and handing off board cards.
version: 2.0.0
author: maintainer, Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [kanban, board, priority, blocking, review]
    related_skills: [worker-run-discipline, save-lesson-before-complete, issue-mirror]
---

# Board discipline

This skill covers how an agent creates a card, picks its priority and
assignee, blocks it correctly, links it to other cards and hands work off to
review or completion. Every rule was written after a concrete failure; the
reasoning behind each one is in references/protocol.md, one-off lessons are
in references/lessons.md.

## When to use

You create a card, block it, set its priority, hand work to review, or send
someone else's work back.

## Creating a card: checklist

- `--skill`: ONLY names from `hermes skills list`. Technology keys from the
  card's Stack line (react, java, ...) are not skills: an unknown name crashes
  the worker at start-up, three crashes in a row trip the circuit breaker;
- `--assignee`: only profiles shown by `hermes kanban assignees` with
  `on disk = yes`. `no` means skipped_nonspawnable (the card silently waits
  forever); no assignee at all means skipped_unassigned and a false stop
  signal for any watchdog;
- `--priority` is mandatory: the queue is `priority DESC, created_at ASC`.
  Scale: 100 tester/reviewer on an open PR with green CI; 80 fixes an open PR
  (owner review package, rebasing the root of a stack); 60 stacked layer;
  40 coding to an accepted plan; 30 new feature; 20 planning; 0 triage.
  Live card: `hermes kanban edit <id> --priority 80` (agents never edit the
  board database directly);
- `--idempotency-key <role>-<issue|PR>-<verb>` is mandatory on every card an
  agent creates (exceptions: the auto-decomposer and cards created by a
  human). Picked up a card without a key: check for duplicates with
  no-duplicate-work before starting;
- create through the CLI, `hermes kanban --board <board> create ...`, and
  verify the result: body, priority and parents present in task_links (an
  MCP create tool has been seen silently dropping body/parents/priority);
- a card whose deliverable is a PR in a repository with required checks is
  created with `--completion-contract <org>/<repo>`: the built-in gate will
  not let it close without a green PR head;
- cards created by the GitHub bridge arrive with `assignee: null`: assign a
  role at once (`hermes kanban assign`); status review does not mean profile
  reviewer;
- a beacon card or a wait on a human: `--initial-status blocked` (or block
  needs_input) with a release-condition line, NOT ready/todo: todo
  auto-promotes.

## Mechanical cards on a cheap model

Mechanical rebases, documentation edits, notice files and annotations can be
pinned to a cheaper model (`<cheap-model>` in your config). Never on the
cheap model: reviews and verdicts, decomposition and plans, multi-file
refactors, SEMANTIC rebase conflicts (a mechanical rebase that meets one must
block with a question, not decide).

## Links and blocks: pick the kind by what you wait for

- waiting for another card: `link`, not a block. The dependency holds both
  start and completion;
- waiting for an external event (PR merge, a human): `block kind='needs_input'`
  plus a line `Release condition: ...`;
- `--initial-status blocked` is released ONLY by hand, never by a parent
  finishing;
- canonical dependency chain: create in ready, then `block --kind dependency`
  (status todo, event dependency_wait, promotes itself). A card created
  blocked and then dependency-blocked is classified in place and waits
  forever;
- a `dependency` block does NOT hold a card whose parents are done: it
  auto-promotes and loops.

Layouts:

- sending back a root whose children are done: coder rework card WITHOUT a
  parent link to the root, reviewer card linked parent=coder, root linked as
  child of the reviewer;
- review-package cards are never hung on a blocked umbrella or root: order
  them with links inside the package, priority 80 (re-review and tester on an
  open PR: 100);
- rebase queue: root of a stack before its child; a "rebase #X onto #Y" card
  is blocked needs_input while #Y is sent back with blockers.

## A block produces an action, not text

- a verdict on a PR is not a card on the board: before closing or reviving a
  stale PR, look for open cards by PR number and by blocker sha;
- in the same turn either (a) create a rework card with an assignee and
  acceptance criteria, `link <new> <blocked>`, or (b) ask the owner in
  `<telegram-chat>` with concrete options. Status goes where the owner sees
  it: a PR/issue comment plus the chat; a card comment alone does not count;
- UNBLOCK does not remove the cause: identical durations of repeated failures
  are the signature of a limit. Unblock after a timeout or iteration budget
  only together with decomposition, a higher `--max-runtime`, or goal mode.

## Handing off and closing

- `kanban_request_review` with an explicit `reviewer` (otherwise the author
  reviews itself);
- a coder card does not complete until a reviewer card exists for its PR;
- before closing an issue the board holds no open cards for it:
  `hermes kanban --board <board> list --json | jq ... select(.body | test("#N\\b"))`;
  cards left open are explained in the closing comment;
- before closing a card: no live parents, no self-links in task_links;
- one card = one root cause: an owner review with more than three blockers is
  split per cause under a shared umbrella; split slow cards up front to fit the
  worker's iteration budget;
- a gate said no (pre-commit, CI, coverage, reviewer): fix the cause or block
  with a question. Bypassing it (`--no-verify`, env overrides, force-push,
  lowering a threshold) is not an option: gates are closed with code, never
  weakened;
- `kanban_complete` returned classification infra or stale: retry in the same
  run, up to 3 attempts 60-120 s apart (check live status before each retry,
  no blind sleep); block kind=transient only after the third refusal, quoting
  receipt.detail verbatim. auth, failure, pending are handled as before.

## Check

- every `--skill` name is in `hermes skills list`;
- every `--assignee` is in `hermes kanban assignees` with `on disk = yes`;
- the new card has `--priority` and `--idempotency-key`;
- after a send-back there are two open linked cards on the board;
- `kanban_request_review` names the `reviewer`;
- before closing: no live parents, no cycles in task_links.

## References (read on demand)

- references/protocol.md: the full protocol with the failure behind each rule
- references/lessons.md: link and completion order, umbrella coordinator, CLI
  mechanics, key at create time, send-back creates two cards, verdict routing
  and no merges by workers, rework in the verdict PR's branch
