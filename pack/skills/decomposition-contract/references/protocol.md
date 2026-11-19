# decomposition-contract: full protocol

This reference expands the decomposition-contract skill: the reference child
card, anti-patterns, the rule against fixed counts in criteria, card size
against the iteration budget, why skills are attached to cards, the
triviality gate, change classes, surgical plan rework, and the machine gate
that stops cards which burn their budget twice. A plan is checked by the same
exam as code, and the exam is known in advance, so run it yourself before
handing the plan in.

## When to use

You split a triage card into children, hand a plan to review, or rework a plan
after a send-back.

## Reference child card

Each body is self-contained: the worker does not see your conversation.

```
[coder] Fix Retry after a timeout in the tool-call panel.
Symptom: <reproduction steps>. Hypothesis: the error state is not reset.
Candidate files: <path to the component> (find with serena).
Acceptance criteria: (a) the repeated call reaches the server; (b) the error
state is reset; (c) existing tests stay green.
Branch: hermes/fix-retry. Stack: <stack keys>.

[tester, depends on the previous] Verify the Retry fix.
Checklist: reproduce the original bug on the base; on the branch run the steps
from card 1; git diff matches the claim; <module test command>; verdict with
steps.
```

Chain structure: coder implements, tester verifies the artifact, reviewer runs
an adversarial review. Dependencies go parent to child; do not create
parallelism without need.

## Anti-patterns

- a child without acceptance criteria;
- a code card without a target branch;
- an "investigate and fix" child without a hypothesis;
- more than five children per card: split through an intermediate parent;
- parallel children writing the same files;
- one "write the tests" card: split by layer, unit / integration / e2e;
- a card without a **Stack** line: a defective card.

An acceptance criterion is always stated with its check: not "cover the class
with tests" but "branches >= 0.80 for module X, measured by command Y".

## Do not write an expected test count into the plan

The criterion "there must be N tests" is a defective criterion: N goes stale
with the first added test, and the checker recounts it every round instead of
checking the work.

Write it as a comparison with the base: the same run on the base branch and on
the PR branch, zero failures and errors, no fewer tests run than on the base,
no more skips than on the base, every base skip named with its reason from the
code.

The rule is wider than tests: any criterion with a fixed quantity that changes
by itself (file count, size, duration) is phrased as a comparison with a
measured baseline, not as a constant. One plan went through five review rounds
over this single finding.

## Card size: the worker's iteration budget

Tasks with an "edit, slow run" loop (builds, browser tests, e2e: one to three
minutes per run) are cut so that the card fits with headroom: no more than two
or three e2e tests or one module of edits.

Failure mode: a card adding six e2e tests at once burned three runs to the
ceiling and blocked the pipeline for half a day. In another week, sixteen
budget give-ups landed on ten coder cards, all retried with unchanged bodies.
A worker that burned the budget twice on a card is not retried: the card is
decomposed.

Keep each layer of a decomposition within one stack or one side (backend
separate from UI). A card that combined a backend endpoint on two stacks with
a UI tab died twice at the iteration ceiling, most of the time spent in the
terminal: a multi-stack diff pushes the coder into full verification of every
stack, which does not fit one run.

## Attach the skill to the card instead of hoping the worker opens it

When creating a code card, pass `skills=["serena-lsp"]` (CLI: `--skill
serena-lsp`). The dispatcher passes the worker one `--skills` flag per skill
and the body loads into the run's prompt at once, not on a `skill_view` call.

Why, if the rule is written down anyway: the choice of tool is made BEFORE the
worker would think of opening the skill. A body loaded up front influences that
choice; a mention in SOUL does not.

**The measurement behind it (one week).** Counted was not every terminal
call but the same action: searching THE SOURCE, excluding greps over logs,
build output, reports and dependency directories.

| role | code search via terminal | serena | serena share |
|---|---|---|---|
| reviewer | 2 | 1028 | 99% |
| coder | 1021 | 552 | 35% |
| architect | 125 | 30 | 19% |
| tester | 111 | 5 | 4% |

All four had the same rule text all week, serena was connected for all,
conditions were equal, behaviour differed 25-fold. Text does not fix it.

Attach `serena-lsp` to every card where the worker will read or edit code:
coder, tester doing impact analysis, reviewer reading the diff. Do not attach
it to cards without code work: an extra skill in the prompt is cost without
benefit. The name check is mandatory (see kanban-discipline): an unknown name
crashes the run at initialisation.

## Triviality gate

A task is trivial only if ALL hold: one file, fewer than ten changed lines, no
new behaviour, the place is known without searching. Do not decompose it and
do not send it to plan review: one coder card with the exact file and
acceptance criterion. Everything else, and everything in doubt, goes through
the full cycle.

## Change class and plan document

MAJOR signs: new feature, public API, two or more modules, five or more
children. CRITICAL signs: release and build infrastructure (CI workflows,
build files, wrappers, configuration), auth and security, data migrations,
external public contracts.

A plan document from the project conventions template is mandatory for both.
Order: plan as a comment on the card and the issue, then plan review; for
CRITICAL, children start only after ACCEPTED and publication of the plan to
the owner.

When decomposing, record the severity as a comment `severity: X (why)` on the
root card and match the severity label on the issue mirror.

## Reworking a plan: surgically, not from scratch

Closing a send-back verdict, change exactly what was pointed out. Do NOT
rebuild the decomposition from scratch: a recreated graph gives the reviewer a
new surface and the cycle repeats. Report in the summary line by line:
"finding N, what was done, where to look". This is also the format the
reviewer uses to check closure incrementally.

## Check

- every child has acceptance criteria, a target branch and a Stack line;
- for a rework verdict: decomposed from the LATEST verdict on the current
  head, one review blocker per child, the push target "branch of PR <url>" in
  every child's body, children chained, not parallel;
- no child repeats work already delivered in a PR: a delivered PR is closed
  with its result and the remainder becomes a new card;
- children are held back until the plan is accepted and linked to the root,
  with a dependency block, not `--initial-status blocked` (the core never
  promotes such a child after the root is done);
- the root card is handed in with `kanban_request_review`, no separate review
  card was created;
- every fact about the repository in the plan is backed by `file:line` or
  config;
- with an issue mirror: the mirror's acceptance criteria are copied verbatim
  into the publishing child, including where the deliverable must be placed.

## Machine gate for repeated budget give-ups

The rule "burned the budget twice: do not retry, decompose" should be enforced
by a watchdog, not by self-discipline.

Trigger: the card's two LATEST consecutive runs have outcome='gave_up' with an
error containing 'Iteration budget', with no outcome='completed' between them.
One burnt budget is a warning, two in a row is a block.

On trigger the watchdog:

- moves the card to status='blocked' with kind='needs_input';
- adds a comment demanding decomposition or a clearer card, naming both burnt
  run ids;
- notifies the architect or owner through its usual channel.

Violation signal: a third consecutive budget give-up on one card. With the gate
in place it should not happen; if it does, it is a watchdog defect, report it.

What the owner of the blocked card (usually the architect) does:

- decomposes the task into smaller cards and closes the original as redundant
  or keeps it as an orchestrating parent;
- or clarifies the card (a sharper hypothesis, facts already checked, smaller
  scope) and unblocks it manually;
- does NOT unblock without changing the card: the next run with the same body
  burns the budget in the same place and the gate fires again.

A completed run between two give-ups resets the series. A manual unblock does
not: two more budget give-ups after it block the card again.

### An honest measurement of the skill-attachment rule

The rule is useful, but its gain is NOT proven. Comparing runs with and without
attached skills in the same time window after the rule went live showed no
difference in budget exhaustion and a difference in success rate that, at
those sample sizes, is noise. Raw all-time numbers looked far more convincing,
and that is a TRAP: skills had only just started being attached, so the
comparison was between two eras, not two approaches. Compare such ratios in
one time window.

What is visible even without statistics: a worker without `serena-lsp`
attached reads source by line numbers with `sed -n` and then reverts its own
edit with `git checkout --`. That, not the numbers, is the argument for
attaching.
