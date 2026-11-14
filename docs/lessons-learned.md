# Lessons learned

This document collects the lessons from running the factory that were expensive to learn and are not specific to any project. Each lesson has the rule, the incident that taught it (told generically), and how to apply it. Skill-level lessons for the agents live in each skill's `references/lessons.md`; these are lessons for the person operating the factory.

## Look for the native feature first

**Rule.** Before building a mechanism on top of Hermes (script, plugin, cron, rule), check the current version and the latest upstream for an equivalent: docs, `--help`, code, release notes. Build your own only for what is missing, and write down which native feature you checked and why it was not enough.
**Why.** An audit of a mature installation found more than a dozen home-grown mechanisms that duplicated native features or were dead.
**How.** Keep a deviations table (see [extensions-catalog.md](extensions-catalog.md)) and review it before every upgrade; delete what became native.

## An agent's report is not the artifact

**Rule.** Verify the state of the repository, the board or the server, not the agent's description of it.
**Why.** Agents report "implemented, tests green" with nothing committed, approve pull requests whose tests cannot fail, and claim live checks they never ran.
**How.** Ask for machine-checkable handovers (`git status --porcelain`, `git log base..branch`, the snapshot hash) and re-run the key check yourself.

## Measure the metric together with its invariant

**Rule.** Every optimisation has a metric (cost, speed) and an invariant (cards closed, tests run, review depth). Record both before and after.
**Why.** Cheaper runs can simply be runs that stopped doing part of the work.
**How.** Report cost per closed card next to cost per day; watch the owner-reversal rate after making reviews cheaper.

## Waiting is not staleness

**Rule.** A card that waits on a dependency or on the owner is not stale. Automatic archiving may only touch cards with no open links and no owner hold.
**Why.** An "archive cards idle for 14 days" rule archived review and QA children of live work; the implementation would have shipped unreviewed.
**How.** For every cleanup rule, ask what it must never touch and how it is rolled back, and add a regression test for the waiting case.

## Restart the gateway the safe way

**Rule.** Restart with `hermes gateway restart` (without `-y`), which refuses while agent turns are live. A plain `systemctl --user restart` kills running turns.
**Why.** Worker clients survive in their own scope, but the agent turn runs inside the gateway; a hard restart crashed every running card and they retried from scratch.

## Memory blow-ups come from code, not from load

**Rule.** Do not diagnose a crash by resource correlation alone. Find the direct evidence (OOM kill in the kernel log, ENOSPC) with matching timestamps, and check whether repeated failures have a fixed duration (a retry or timeout signature).
**Why.** Workers that "died of memory" were in fact hitting a provider quota and retrying on a fixed schedule; another time a real 4 GB blow-up came from two scripts loading each other in an endless import cycle.
**How.** Run heavy jobs in a memory-capped scope, set the gateway's OOM policy so one killed child does not stop the service, and guard dynamic module loading against cycles.

## Check your request against your own decisions

**Rule.** Before telling the agents to adopt a rule, check it against the decisions you recorded earlier.
**Why.** The owner asked for before/after screenshots in every pull request a day after deciding to move them to issues to save quota; the agents complied and created a batch of the most expensive kind of card.
**How.** Keep decisions in one place with dates; the personas tell the agents to stop and ask when a chat request contradicts a recorded decision.

## Cheaper models are fine for doing, not for judging

**Rule.** Put the cheap model on implementation, QA and operations; keep the stronger model and higher reasoning effort on review, architecture and decomposition.
**Why.** Moving the coder to the cheaper model cut its cost per call about 3.5 times with no visible loss; reviews are where a miss is expensive.

## Notifications must cover success too

**Rule.** A watcher that only reports failures makes success look like silence, and silence looks like "nothing to do".
**How.** Subscribe to completed, review_requested and changes_requested as well as crashed, timed_out, gave_up and blocked, and make watchers reconnect: a dropped connection also looks like silence.

## Re-pin scheduled agent jobs after a provider change

**Rule.** Agent cron jobs store their own provider and model. After changing providers, edit every agent job, or it silently falls back to whatever is left in the chain.
