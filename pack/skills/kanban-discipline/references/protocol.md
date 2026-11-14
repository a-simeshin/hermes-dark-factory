# kanban-discipline: full protocol

This reference expands every rule of the kanban-discipline skill with the
mechanism behind it and the kind of failure that produced it. Read it when a
rule in SKILL.md looks arbitrary, or when the board behaves in a way the short
checklist does not explain. The incidents are retold generically; the
mechanics refer to Hermes Agent v0.23.1.

## When to use

You create a card, block it, set its priority, hand work to review or send
someone else's work back.

## The Stack line belongs in the body, not in `--skill`

`--skill` accepts ONLY the names of skills that are actually installed
(`hermes skills list`). Technology keys from the card's **Stack** line (for
example react, test, e2e, java) are not skills: they are addresses in the
project's standards catalogue and live as a line in the card body.

Passed as `--skill`, they kill the agent during initialisation, before the
first step of work: `Error: Unknown skill(s): ...`, exit code 1. The run counts
as crashed, three in a row trip the circuit breaker, and the card lands in
blocked without ever having started. In the incident behind this rule a
layout fix crashed three times with zero lines of work in the log and the
branch sat idle for ninety minutes.

Before creating a card, every `--skill` name must appear in `hermes skills
list`. Unsure: leave it out. An agent works without skills; it does not start
with a non-existent one.

## `--initial-status blocked` is released only by hand

A card created with `--initial-status blocked` carries an EXPLICIT block. A
parent finishing does not release it: the dependency mechanism and an explicit
block are different things. The parent goes to done, the child stays blocked
forever, and the chain stops silently because formally everything is fine.
Several cards have been found blocked with all parents done while the board
idled on an empty queue.

A card that must wait for another card: create it with a normal status and
link it. The dependency holds it back and releases it by itself.

Keep `--initial-status blocked` for waits on a human or an external event,
where releasing the block should be manual.

Pitfall: a card CREATED blocked and then given `block --kind dependency` is
classified in place. Its status stays blocked, the last event stays blocked,
and the sticky-block check looks exactly at that last event, so ready
recomputation skips the card forever even after the parent closes. The CLI
prints a misleading "Blocked as needs_input (no open parent to wait on)". The
canonical dependency wait is: create the card as ready, then `block --kind
dependency`. It goes to todo with a dependency_wait event (not sticky), and
ready recomputation lifts it when all parents are done or archived. After
setting it up, check: status todo (not blocked), last event dependency_wait,
parent alive in task_links.

## A root waits for its rework chain through a link, not a block

Sending back a root card whose children are already done: do not block the
root and do not make the rework cards its children. The correct layout:

1. the coder rework card is created WITHOUT a parent link to the root
   (otherwise deadlock: the coder needs the root done, the root waits for the
   reviewer's verdict);
2. the reviewer card is linked parent=coder;
3. the root is linked as a child of the reviewer: `link <reviewer> <root>`.
   The root goes to todo and auto-promotes when the reviewer completes; the
   verdict arrives in the summary as a parent handoff.

`kanban_block(kind='dependency')` on the root does NOT hold it: dependency_wait
promotes the card as soon as all current parents are done, and the dispatcher
restarts it on the next tick (seen in practice: promoted again within 21
seconds). `kind='needs_input'` after the run has finished is refused (a block
is accepted only for running/ready cards with a live run).

## Review-package cards are not hung on a blocked umbrella or root

Rework after an owner verdict, blockers, minor items, tester verification and
re-review of an open PR are never made children of an umbrella or root card
sitting in blocked: such a parent holds the whole package while gating nothing
(the umbrella waits for some gate, the root waits for an external event or
even named the rework as its own release condition). Order inside the package
comes from links between its cards: PR body and end-to-end QA after the code
blockers, re-review after its rework card. Priorities: owner review package
80, re-review and tester on an open PR with green CI 100. In the incident
behind this rule, removing the links released three stuck lines of work in one
move.

## Create cards with the CLI and verify the result

An MCP `kanban_create` tool has been observed silently dropping body, parents,
priority, skills, goal_mode and max_runtime: the cards were created empty and
had to be archived and recreated. Create cards with `hermes kanban --board
<board> create` and explicit flags, then verify: body present, priority set,
parents in task_links.

## Mechanical cards on a cheap model

Mechanical work (rebasing a PR onto its base, documentation edits, notice
files, annotations) costs a fraction on a cheaper model; in one measurement
rebases alone were hundreds of calls and tens of millions of tokens over two
weeks. If an automation creates such cards, it should pass the model flags
itself; hand-created mechanical cards get the same flags (see
`hermes kanban create --help` for the model and provider options).

Do NOT assign to the cheap model: reviews and verdicts (the adversarial gate
must catch what a cheap model misses), decomposition and plans, multi-file
refactors, resolution of SEMANTIC rebase conflicts (a mechanical rebase that
hits one must block with a question rather than decide alone).

## The kind of block follows the nature of the wait

- waiting for another card on the board: `link`, not a block (a link holds
  both start and completion: the parent closes strictly before the child);
- waiting for an external event (PR merge, a human action): `kanban_block`
  with `kind='needs_input'`. `kind='dependency'` is useless here:
  dependency_wait auto-promotes the card to ready and the dispatcher reruns
  the check in a loop.

## Priority is set at creation with `--priority`

The dispatcher picks the next card by `priority DESC, created_at ASC`. Without
explicit priorities the queue degrades to "oldest first", and planning, of
which there is always more, gets ahead of code.

One scale, by proximity to merge (higher means picked earlier):

| value | meaning |
|---|---|
| 100 | last step before merge: tester or reviewer on an open PR with green CI |
| 80 | fixes an already open PR: red CI, send-back verdict, missing fix |
| 60 | stacked work on top of an open PR of the same line: the chain cannot merge without it |
| 40 | coding to an accepted plan, no PR yet |
| 30 | new feature to an accepted plan (long chain) |
| 20 | planning and decomposition |
| 0 | triage and ideas |

The point: do not accumulate work in progress. An open PR without a verdict
costs more than a new feature started. Equal priority: the older card wins.

Example: `hermes kanban --board <board> create "<title>" --body "<body>"
--assignee coder --priority 40 --idempotency-key coder-issue12-fix-retry`.

A card without a priority is a defective card.

## Rebase queue: the root of a stack before its child

When many PRs conflict at once, rebase cards can sit in ready for a day while
the dispatcher keeps picking rework cards.

- rebasing the root of a stack unblocks the whole chain: priority 80 ("fixes
  an open PR"). Rebasing a child before its parent is a wasted run: the
  parent's head will move and the child must be rebuilt;
- a "rebase #X onto #Y" card is not taken while #Y is sent back with blockers:
  rebasing onto a head that is about to change is wasted work. Block it
  (`kind='needs_input'`, release condition: PASS on PR #Y or its closure)
  instead of leaving it in ready;
- traversal order: stack roots, then the second layer, then the leaves. The
  current list of roots lives in the body of a rebase umbrella card on the
  board; it changes with every merge and does not belong in a skill.

## GitHub-bridge cards arrive without an assignee

Cards created by the GitHub bridge from an owner review arrive with
`assignee: null`. The run's profile is taken from the assignee, so an empty
assignee is a guaranteed stop: the dispatcher skips the card every tick as
skipped_unassigned and it sits in ready indefinitely. When you see a fresh
bridge card with no assignee, assign the profile by the nature of the work
(coder for code and rework, reviewer for verdicts, tester for QA, architect
for design): `hermes kanban assign <id> <profile>`. Status review does not
mean profile reviewer: a `[coder]` card in review is finished code work
waiting for a verdict.

## Raising the priority of a live card

    hermes kanban --board <board> edit <id> --priority 80

A card carrying an owner verdict once starved for more than a day at the tail
of a FIFO queue (every card at priority 0); after the priority was raised the
dispatcher picked it within a minute. Do not edit the board database to
reorder the queue.

## Every agent-created card carries an idempotency key

Any card an agent (not a human) creates MUST carry `--idempotency-key
<role>-<issue/PR>-<what>`. The key lets a watchdog tell "already filed" from
"new"; without it duplicate detection is blind and two agents file the same
card twice (for example, a reviewer filing the same visual-evidence check
twice in one day). A post-hoc detector catches missing keys, but by then the
duplicate exists.

Exceptions: the `auto-decomposer` (core, sets no keys) and cards a human
created directly (`created_by=user`).

Format: `<role>-<issue|PR>-<verb>`, for example
`tester-pr187-verify-eviction-race`. Picked up a card without a key: check for
duplicates with no-duplicate-work before starting.

## Name the reviewer when requesting review

`kanban_request_review` without a `reviewer` parameter leaves the card on you,
and the dispatcher hands the review to the same assignee: the author reviews
itself. For code and tests the reviewer is the `reviewer` profile.

## One card: one root cause

A card with slow runs (build, e2e: minutes per run) must fit the worker's
iteration budget. Split up front, not when the budget is gone.

A budget estimated for a run from scratch is not valid for a rework card. An
owner review delivers a fresh package of blockers, and the first attempt
spends iterations on setting up the environment (clone, dependency install,
build), leaving no room for the package itself. In the incident behind this
rule, a card carrying four owner blockers plus a set of nits in one body hit
the iteration ceiling on its first attempt; the next three fresh sessions did
not see the terminal state and exited without `kanban_complete`. Four burnt
runs and nearly four hours of machine time; the fifth run closed everything in
about twenty minutes.

Rule: a card that closes an owner review with more than three blockers is not
created as one list. File one card per root cause (a primary blocker and its
regression test are one cause) and tie them together under a shared umbrella.
All cards of the package get priority 80.

## Before closing an issue the board is empty of its cards

Closing an issue is not only a GitHub action. An issue closed on GitHub while
its plan cards still sit in todo/blocked means the board and the issue
disagree.

Before closing an issue, check:

    hermes kanban --board <board> list --json | jq --arg N "ISSUE_NUMBER" '
      .[] | select(.body | test("#" + $N + "\\b")) | .id + " " + .status'

Every card for the issue must be `done` (or archived). If not: close or
archive them with an explanation, or do not close the issue. If cards stay
blocked or deferred while the issue is closed as done, say in the closing
comment which cards remain and why.

## A gate said no: fix the cause or block

A pre-commit hook, CI, a coverage threshold, a reviewer: any gate that says
"no" is a signal to stop. Two correct moves:

1. fix the cause (remove the forbidden character, fix the test, raise coverage
   with code);
2. block with a question to the owner if the cause is unclear or not yours.

A bypass (env override of the hook, `--no-verify`, force-push, lowering a
threshold) is not an option: gates are closed with code, never weakened. In
the incident behind this rule a snapshot hook rejected a commit because of
forbidden characters in someone else's files; instead of fixing or excluding
them the agent overrode the hook and ran `git add -A`, and the knowledge
snapshot swelled by tens of thousands of junk files.

## Assignees only with `on disk = yes`

Before `--assignee <name>`, run `hermes kanban assignees`. The `on disk`
column must be `yes`. An assignee with `no` has no profile or config on disk:
the dispatcher skips the card every tick as skipped_nonspawnable, nothing is
spawned and the card waits silently. Seen in practice: a root-cause card
assigned to a profile that did not exist on disk.

## A block produces an action, not text

When a card goes to blocked, the same turn does one of two things:

(a) creates a rework card with a concrete assignee and acceptance criteria,
    linked to the blocked card: `link <new> <blocked>`;

(b) if the owner is needed, sends a question to `<telegram-chat>` with a
    concrete choice (option A / option B, what each one costs), not "needs a
    human decision".

Either way the block status is written where the owner will see it: a
comment on the PR or issue ("blocked: waiting for X, who does it, by when")
and a chat message. A comment only on the card does not count: the owner does
not read the board.

Failure modes behind this rule: a QA card blocked with "a follow-up card needs
to be created" and nobody created it; a root card registered as a descendant
of its own QA card (a cycle the board never resolves); a plan stopped after
three architect/reviewer rounds with "needs a human decision" while nobody
asked the human; an ops card asking for a gateway restart for more than a day
without saying so anywhere outside the board.

## A card in ready must have an assignee

A card without an assignee in ready is skipped every tick as
skipped_unassigned and gives the watchdog a false stop signal ("ready exists,
nobody can take it"). That noise hides real stops: a beacon card that sat in
ready without an assignee for days produced hundreds of warnings, and a real
stop behind them went unnoticed.

A beacon card without an assignee lives in blocked with kind='needs_input'
and a release-condition line. Todo is no refuge: a todo card without live
parents auto-promotes to ready and becomes the same generator of
skipped_unassigned. Only archived cards never promote.

## UNBLOCK does not remove the cause: remove the cause first

Unblocking a card that was blocked after a series of identical failures,
without removing the cause, guarantees the series repeats. A run that died on
a limit dies the same way with the same body and the same limit. In the
incident behind this rule a card blocked after three runs that each lasted a
few seconds over the runtime limit was unblocked weeks later unchanged, and
burned three more runs of exactly the same length (about an hour and a half of
machine time) before blocking again. A fresh owner verdict justifies the
unblock but does not change the arithmetic of the run.

Before `kanban unblock`, check the last block reason (`hermes kanban show
<id>`, Diagnostics block; `hermes kanban runs <id>`). If it was a timeout on
the limit or an iteration budget, unblocking is allowed only together with
removing the cause: (a) splitting the body into smaller cards, (b) an explicit
`--max-runtime` sized to the work, or (c) recreating it in goal mode. Without
one of the three, it is not an unblock but an order for another burnt run.

## Check

- every `--skill` name is in `hermes skills list`;
- every `--assignee` is in `hermes kanban assignees` with `on disk = yes`;
- the new card has `--priority` and `--idempotency-key`;
- after a send-back there are two open cards linked by a dependency;
- `kanban_request_review` was called with an explicit `reviewer`;
- before closing, the card has no live parents and no self-links in
  task_links (a link cycle never completes).
