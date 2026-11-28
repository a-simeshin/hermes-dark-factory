---
name: decomposition-contract
description: How to split a task into cards and hand the plan to review.
version: 2.1.0
author: maintainer, Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [architect, planning, decomposition, review, kanban]
    related_skills: [kanban-discipline, issue-mirror, tester-verdict-checklist]
---

# Decomposition contract

This skill covers how the architect splits a task into child cards, what a
self-contained child card must contain, how big a card may be, which changes
need a plan document, and how the plan goes through review. A plan is checked
by the same exam as code, so run the exam yourself before handing it in.
Background and measurements: references/protocol.md, one-off rules:
references/lessons.md.

## Reference child card

Each body is self-contained: the worker does not see your conversation.
Template:

    [coder] <what to do>. Symptom: <steps>. Hypothesis: <cause>.
    Candidate files: <path> (find with serena).
    Acceptance criteria: (a)... (b)... (c) existing tests stay green.
    Branch: hermes/<name>. Stack: <stack keys>.

    [tester, depends on the previous] Verify the fix: checklist with commands.

Chain: coder implements, tester verifies, reviewer runs an adversarial review.
Dependencies go parent to child; no parallelism without need. An acceptance
criterion always names its check: not "cover with tests" but "branches >= 0.80
for module X, measured by command Y".

## Completion contract for PR cards

A card whose deliverable is a PR in a repository with required checks is
created with `--completion-contract <org>/<repo>` (kanban_create:
completion_contract=...): the built-in gate will not close it without
metadata.published_pr and green required checks on the PR head (kanban docs,
"PR completion contracts"). Cards without a PR (local run, review, analysis)
get no contract. The worker hands the PR in as usual; the gate reads the
checks itself.

## Anti-patterns

- a child without acceptance criteria; a code card without a branch;
  "investigate and fix" without a hypothesis;
- **[DEC-01]** more than five children: split through an intermediate parent;
- parallel children writing the same files;
- one "write the tests" card: split by layer, unit / integration / e2e;
- a card without a Stack line is a defective card;
- a criterion with a fixed number that changes by itself (test count, file
  count, size): phrase it as a comparison with a measured baseline, not a
  constant.

## Size and budget

- **[DEC-02]** the worker's iteration budget is finite: an "edit, slow run" loop is cut
  with headroom (two or three e2e tests or one module of edits per card).
  Retries without changing the card burn the budget in the same place;
- one decomposition layer stays within ONE stack or one side (backend
  separately from UI, each stack's backend separately when they differ):
  a multi-stack layer pushes the coder into full verification of every
  stack, which does not fit one run and is never verified locally as a whole;
- triviality gate: one file, under 10 lines, no new behaviour, the place is
  known: one coder card without decomposition or plan review. Anything in
  doubt goes through the full cycle.

## What is not decomposed, and from which verdict

- **A delivered PR is not decomposed.** A card that already handed in a PR
  does not go back to triage and is not split. Close it with the result
  "PR <url>"; the remainder becomes a NEW card whose body is reconciled with
  the PR (what is already in it, what is left). Why: a card that dropped into
  triage after repeated blocks was auto-decomposed from its stale creation
  body and the children redid work already sitting in the PR.
- **A PR's rework starts from its LATEST verdict** (all reviewers, not only
  the owner). Read the verdicts in order first: the branch head may have moved
  past an older verdict and closed its blockers already; decomposing the old
  one produces a duplicate chain.
- **One child = one PR or one review blocker.** Rework normally stays on the
  card that owns the PR. If a verdict is too big for one run, split it into a
  chain (`kanban link` in order, never parallel) with one blocker per child;
  every child's body names the push target "branch of PR <url>" and opens no
  new PR.
- **No card for an already delivered issue:** check for a merged PR that
  references it (merged after the last scope change) before creating work.

## Attach the skill to the card, do not just mention it

Pass `--skill serena-lsp` when creating a code card: the dispatcher loads the
skill body into the run's prompt right away. The choice of tool is made
BEFORE the worker would think of opening a skill; with identical rule text,
roles differed 25-fold in how often they used semantic code search, so text
alone does not fix it. Do not attach it to cards without code work. Check the
name against `hermes skills list` (see kanban-discipline).

## Change class and plan document

- MAJOR: new feature, public API, 2+ modules, 5+ children;
- CRITICAL: release and build infrastructure (CI workflows, build files,
  wrappers, configuration), auth, migrations, external contracts.

A plan document is mandatory for both: plan as a comment on the card and the
issue, then plan review; CRITICAL children start only after ACCEPTED and
publication of the plan to the owner. When decomposing, record `severity: X
(why)` on the root and match the severity label on the issue mirror.

## Reworking a plan: surgically

On a send-back, change exactly what was pointed out; rebuilding from scratch
gives the reviewer a new surface and repeats the cycle. Report line by line:
"finding N, what was done, where to look".

## Check

- every child has acceptance criteria, a target branch and a Stack line;
- rework: decomposed from the latest verdict, "branch of PR <url>" as push
  target in every child, one blocker per child, children chained;
- children are held back until the plan is accepted (dependency on the root,
  see lessons.md) and linked to the root; a child created with
  `--initial-status blocked` is never promoted by the core when the root is
  done, so nobody but you (or a watchdog that does it explicitly) releases it;
- if the root closes on plan approval, every delivery-time duty of the root
  (the "fix delivered" comment on the issue mirror after the merge, the
  triage-label check) is written into the body of one child: by delivery
  time the root is already closed;
- the root is handed in with `kanban_request_review`, no separate review card;
- every fact about the repository is backed by `file:line` or config;
- the issue mirror's acceptance criteria are copied verbatim into the
  publishing child.

## References (read on demand)

- references/protocol.md: the full contract with measurements, the budget
  gate for repeated gave_up
- references/lessons.md: children inherit the mirror's acceptance criteria,
  the plan goes to the built-in review lane, repository facts are checked in
  code, third-party APIs by their source, rework from the latest verdict
