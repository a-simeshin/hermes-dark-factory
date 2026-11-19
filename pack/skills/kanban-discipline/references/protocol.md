# kanban-discipline: full protocol

This reference expands every rule of the kanban-discipline skill with the
mechanism behind it and the kind of failure that produced it. Read it when a
rule in SKILL.md looks arbitrary, or when the board behaves in a way the short
checklist does not explain. The incidents are retold generically; the
mechanics refer to Hermes Agent v0.23.1.

## When to use

You create a card, block it, set its priority, hand work to review or send
someone else's work back.

## Native or live already exists: check before you act

Before creating, reassigning, restoring or removing anything on the board,
check whether a native Hermes mechanism or a live card already does it: the
review lane after `kanban_request_review`, native respawn and its guards
(active_pr, recent_success), task_links and dependency_wait, a verdict on this
head, a live card with the same key (issue or PR + role + head). If one does,
create nothing; extend it or wait for it. A local mechanism may act only where
the native one will not, and the code next to it says which native path was
checked and why it is not enough. The same holds for manual actions: restoring
something by hand past the gates is forbidden, and restoring what the owner
closed or archived is a question to the owner.

The rule was distilled from four failures of one shape: three coder cards for
one patch, a reclaim/respawn loop triggered by a card's own handoff comment,
a head sampled while it was still in rework, and a review card duplicating the
native review lane. Your own layer counts as "native" too: two local gates
with the same job once parked and returned each other's cards. Before
building a gate, watchdog or generator, walk the list of local mechanisms
(the deviations file, scripts, plugins) and extend the one that exists.

**No card for delivered work.** "Nobody is working on it" is not judged by
live cards alone. Do not file an executor card for an open issue if (a) a
merged PR references it and was merged after the last scope change (creation,
label or title change; milestone changes and comments are not scope), or (b) a
done executor card links a merged PR (done by itself is not coverage). A
release chore left open until the tag is intentional: stay silent. Delivered
work that is not a chore gets a question to the owner, "what is left on #N",
not a card. In the incident behind this rule a poller filed three duplicates
in twenty minutes for an issue whose PR had already been merged.

**Erroneous cards are archived by their creator.** A card created by mistake
(wrong verb, a probe, a duplicate) is archived by whoever created it, at once,
with a comment giving the reason. Left in ready "for a worker to clean up", it
costs a dispatcher slot and a run. There are no per-board dispatcher settings
in Hermes: the configuration is host-wide and the dispatcher walks every
board, so a "sandbox" board in the live home is not a sandbox (a probe card
there was decomposed and spawned real workers). Experiments go into a separate
`HERMES_HOME`, or use read-only commands.

**Claims about mechanisms need evidence.** A report that says a mechanism
"returns by itself", "does not leak" or "will let it through" is backed by a
test on this code or a check on the live board made in the same session.
Without evidence, write "not verified". In the incident behind this rule a
report announced an automatic return that no code implemented, and the cards
stayed parked. Only a red test before the fix and a green one after prove
"works now".

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

## One PR = one card: review and rework live on the owner card

The card that owns a PR carries the whole cycle until the owner merges:
review, rework, rebase and the owner-facing summary. Earlier layouts with a
separate reviewer card per head, a rework card per verdict, a serial chain of
rework cards and a rebase card per PR were removed: every extra card was
another place for duplicates, races of two coders pushing into one branch,
and reviews of a head that was about to change.

- Review: `kanban_request_review` on the owner card, with an explicit
  `reviewer`. The card moves to the native review lane and the dispatcher
  starts the review run itself. A reviewer card on the same head is a second
  full review of the same sha. The owner-facing summary ("## What changes
  for you") is posted BEFORE the request, as part of the handoff: an accept
  verdict closes the card in the same transition, so the owner card gets no
  turn after approval.
- Reject: the reviewer, running on the card claimed from review, calls
  `kanban_request_changes`; the card returns to its owner. The owner fixes in
  the PR's branch, answers each item in the PR (rework response) and requests
  review again. `kanban_request_changes` is refused on a card that was not
  claimed from review.
- Rebase: only on CONFLICTING (or an owner request), on the owner card, once,
  last before the final review. An automation that finds a conflict comments
  on the live owner card instead of creating a rebase card.
- After close: a card closed with "remaining: owner merge" keeps a signal and
  a release condition until the PR is merged, so a PR waiting on the owner
  never disappears from view. A new owner CHANGES REQUESTED after the card
  closed opens one new card for the new cycle.

`kanban_block(kind='dependency')` on a card whose parents are all done does
NOT hold it: dependency_wait promotes the card at once, and the dispatcher
restarts it on the next tick (seen in practice: promoted again within 21
seconds). `kind='needs_input'` after the run has finished is refused (a block
is accepted only for running/ready cards with a live run).

## Decomposing an oversized verdict

If a verdict does not fit one run's iteration budget, split it: one child =
one blocker, idempotency key `<pr>:<head7>:<block>`, every child pushes into
the PR's branch, and the children are chained with links (strictly one after
another; only cards of different PRs run in parallel). Children inherit the
root's priority; the core gives decomposed children 0. Decompose from the
LATEST verdict on the PR from any reviewer: the branch head may already have
moved past the verdict you remember and closed its blockers. In the incident
behind this rule five blockers were split into cards that a later review
round had already closed.

A gate is not a parent of work: the result of a plan check is written into
the plan card, and the work refers to it in the body.

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
- waiting for a human: the question goes in the reason and the kind is
  `needs_input`. A block without a kind is invisible to reports that collect
  owner questions (one such block went unnoticed for two weeks). A plain
  comment after the block can reset a stuck-in-blocked timer, so an owner
  question older than a day must be reported explicitly; if it is missing
  from the attention report, that is a watchdog defect;
- card ids of this same board in a needs_input reason mean the wrong kind
  was chosen: waiting for a card is a link;
- waiting for a PR merge: write the release condition as ONE line with the
  machine check inside it, `Release condition: PR #N merged (gh pr view N
  --json state == MERGED)`. Native dependency_wait and SCHEDULED_UNTIL do not
  cover a PR merge, so a watchdog can execute exactly this form and return
  the card to ready; composite conditions (milestone, owner decision,
  re-review) stay with a human;
- `needs_input` waits only for an owner event. "The owner will update the
  branch, wait for CI, rerun a flaky job, re-review the new head" is a false
  block: the agent does those itself with its app rights.

**Parking until a moment is `kanban schedule`, nothing else.** Use `hermes
kanban schedule` with SCHEDULED_UNTIL in UTC. Never park a card with run
history through a transient block: two transient blocks in a row drop it into
triage (block_loop_detected), and the auto-decomposer then rewrites title and
body from the stale body written at creation, because it reads neither
comments nor run history. The recurrence counter is not reset by specify or
by schedule, so one more transient block repeats it. Cards whose body ages
get `hermes kanban edit --body` at every handoff.

**A delivered PR is not decomposed.** The same triage path once split a card
that had already delivered its PR: four children redid work that was in the
PR. After the PR is delivered, close the card with "PR #N" and file the tail
as a NEW card whose body was checked against the PR (what is in, what is
left). Do not transient-block a card that has delivered a PR. A review cycle
(changes_requested, then decomposition) is legitimate.

## Priority is set at creation with `--priority`

The dispatcher picks the next card by `priority DESC, created_at ASC`. Without
explicit priorities the queue degrades to "oldest first", and planning, of
which there is always more, gets ahead of code.

Priority follows milestone bands:

| value | meaning |
|---|---|
| 100 | bring an open PR to merge: owner verdict, last fixes |
| 80 | the active milestone |
| 40 | the next milestone |
| 0 | everything else: triage and ideas |

The band is derived from the card's own anchor: the issue number in the
title, the `Issue:` line, an issue-link marker, plus the same anchors of its
ancestors. A PR or issue of another milestone mentioned in passing in the body
does not change it; in the incident behind this rule such a mention gave a
next-milestone chain the active band and a release slot. An owner comment in
the issue without an explicit "do it now" or a milestone change does not raise
the band either. If your setup has a helper that computes the band, every card
generator and every hand-created card uses it rather than a constant.

The point: do not accumulate work in progress. An open PR without a verdict
costs more than a new feature started. Next-milestone ideas do not go to ready
while the active milestone has work that can be unblocked.

Example: `hermes kanban --board <board> create "<title>" --body "<body>"
--assignee coder --priority 40 --idempotency-key coder-issue12-fix-retry`.

A card without a priority is a defective card.

**Lifting a priority walks up task_links.** Raising a card whose ancestor is
blocked changes nothing: the dispatcher never reaches it. Walk up to the first
open ancestor; if it is blocked, unblock it or ask the owner. In the incident
behind this rule three cards were raised close to 100 while they sat behind a
gate that had been blocked for two weeks.

## Rebase: owned by the PR's card, planned by overlap

- No rebase without CONFLICTING when branch protection does not require an
  up-to-date branch: freshness alone is wasted runs and new review rounds.
- After each merge, map open PRs that touch the same files. Each affected PR
  gets one decision "needs rebase because of #N", and the owner gets a
  suggested merge order.
- In a stack, the root is rebased before its child; rebasing a child onto a
  head that is about to change is a wasted run. Do not rebase onto a PR that
  is currently sent back with blockers.
- **Budget burnout changes the strategy, not the queue.** A card that died on
  the iteration budget during a rebase does not go back to ready with the same
  wording: the same plan burns again (in the incident behind this rule a third
  run spent its whole budget on repeated `git rebase --abort` and replays).
  Read the run transcript first (restarts, repeated resolutions, builds), then
  change the strategy and write it into the body: continue from the existing
  workspace without replaying, merge the integration branch into the PR branch
  once (each conflict resolved once) instead of rebasing N commits, or split
  by conflicting files.

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

## A mandatory step goes where the actor surely has a turn

A step placed in a gap that a native mechanism closes in one transition
never runs. The native review lane closes the card with the reviewer's
approval, so a step "between approval and close" (for example WCFY) was
never executed: the implementer is not woken after done, and reopen-review
works only from status review. Put such a step into the handoff before the
transition, have the next actor check it, and make a safety-net gate create
a task card for the step, not only a report.

## Name the reviewer when requesting review

`kanban_request_review` without a `reviewer` parameter does not reassign the
card, and the review dispatch spawns the current assignee: the author reviews
itself. For code and tests the reviewer is the `reviewer` profile. After the
request the run exits: someone else's verdict is never awaited inside a run.

## Ready for the owner: a reviewer verdict on the current head

A PR is reported ready for the owner only when a REVIEWER verdict (a
`## Review verdict` header or a formal review) exists on the exact current
head. A QA verdict (`## QA verdict`) is a different header and does not count.
In the incident behind this rule a PR went to the owner on a QA verdict alone
while no reviewer had looked at that head.

## One card: one root cause

A card with slow runs (build, e2e: minutes per run) must fit the worker's
iteration budget. Split up front, not when the budget is gone.

A budget estimated for a run from scratch is not valid for a rework run. An
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
The pieces are chained in the PR's branch as described under "Decomposing an
oversized verdict".

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

(a) puts the work on a card with a concrete assignee and acceptance criteria
    (for a PR: its owner card), linked to the blocked card if one waits for
    the other;

(b) if the owner is needed, sends a question to `<owner-chat>` with a
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
- the new card has `--priority` from its band and `--idempotency-key`;
- no native mechanism or live card already covers it;
- `kanban_request_review` was called with an explicit `reviewer`, and no
  separate reviewer card exists for the PR;
- before closing, the card has no live parents and no self-links in
  task_links (a link cycle never completes).
