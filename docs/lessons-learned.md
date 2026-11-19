# Lessons learned

This document collects the lessons from running the factory that were expensive to learn and are not specific to any project. Each lesson has the rule, why it exists, and how to apply it. Skill-level lessons for the agents live in each skill's `references/lessons.md`; these are lessons for the person operating the factory.

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

## A guard that fails silently is worse than no guard

**Rule.** A plugin that hooks into Hermes internals must log its own failure at warning level, and its test must be run against the Hermes sources after every upgrade.
**Why.** An upstream refactoring moved the board `connect` function to another module. The artifact guard caught the resulting error and logged it at debug level, so it kept reporting "subscribed" at every gateway start and did nothing for weeks. Nobody noticed: the absence of alerts from a guard looks exactly like healthy workers. It was found only when its test was finally run against the current sources.
**How.** After `hermes update`, run each plugin's `test_guard.py` with the Hermes interpreter. In a plugin, never swallow the "cannot reach the board" branch below warning. For a guard that should fire now and then, treat a long silence as a signal and check the date of its last comment on the board.

## Do not build a parallel lane next to a native one

**Rule.** If Hermes already has a lane for a step (the review lane after `kanban_request_review`, dependency waits, respawn guards), do not add cards or arms that do the same step from outside it. One PR is carried by one card, from opening to the owner's merge.
**Why.** Separate reviewer, summary and rebase cards armed by a watchdog raced the native review lane: the same head was reviewed twice, rebases were done by two cards, and every extra card cost a full worker run.
**How.** Route every card generator through one native-or-live check and put its receipt in the card body; fail the test suite when a generator skips it.

## One way to park, one way to wait

**Rule.** Park a card until a moment only with `kanban schedule` and a machine-readable wake time, and make a wait on an external event a single machine-checkable release line that a watchdog can execute.
**Why.** Transient blocks used as parking dropped cards with run history into triage, where the decomposer rewrote them from stale bodies; free-text conditions in block reasons were read by nobody.

## A stop switch can silence the safety net

**Rule.** When you hold the dispatcher (quota stop, peak-pricing pause, manual stop), check what else that tick did. A hold that silences the whole tick also silences its reclaim of dead runs.
**How.** Keep an independent watchdog that reclaims running cards whose worker process is gone and whose heartbeat is stale, and keep it scheduled whenever holds are in use.

## Compression has a floor

**Rule.** Set the compression threshold above the context a worker keeps after a compression (prompt, skills, tool schemas, protected tail), with a margin.
**Why.** Below that floor every compression immediately triggers the next one: runs compressed dozens of times, each pass costing more than it saved, until Hermes blocked compression as ineffective.
**How.** Measure the floor per profile from the worker logs, and recompute the threshold whenever models or skills change.

## A test must not spawn itself

**Rule.** A test never re-runs its own file without a single-test argument and a timeout, and the test runner puts each test in its own process group and kills the whole group on timeout.
**Why.** A test that re-ran itself to prove it was red on the committed code started an unbounded chain of copies, which grew until the OOM killer took the gateway; after a commit, the committed code equals the current code anyway, so the check proved nothing.

## "Green" is the runner's final line, verbatim

**Rule.** A report that tests pass quotes the final summary line of the reporter's own run of the test runner. A marker that the runner does not print, or a summary of someone else's run, is not evidence.
**Why.** Agents reported success with invented markers that no tool in the repository produces.

## Native contracts expect their own formats

**Rule.** When a native gate reads a field your agents fill in, write it in the exact format the gate parses. For the PR completion contract, `metadata.published_pr` is the full PR URL, not a number.
**Why.** A value the gate cannot parse is not reported as a format error; it surfaces later as a completion that does not behave as the contract promises.

## A new rule goes where the worker always reads

**Rule.** When a lesson changes how workers behave, update the always-loaded layer (the profile's auto-loaded conventions skill) as well as the on-demand skills, and search the always-loaded layers for the old wording.
**Why.** A rule moved to four on-demand skills while the auto-loaded conventions skill still described the old order. The worker followed its system prompt, and only the reviewer's check caught the missing step.

## Evidence is checked against its claim before it is posted

**Rule.** Every screenshot, log or file posted as evidence is checked before publishing: distinct files have distinct hashes, the frame shows what the caption claims, the file name matches the scenario. The short hash goes next to each file so the reviewer can recompute it.
**Why.** Two screenshots captioned as two different stacks were the same file byte for byte, and a third showed the state before the claimed one. A reviewer caught it by hashing; making the hash part of the evidence turns that improvisation into a protocol.

## A mandatory step goes where the actor surely has a turn

**Rule.** Place every mandatory step at a point where the actor who owns it is guaranteed a turn, and have the next actor check it there. A step in a gap that a native mechanism closes in one transition never runs.
**Why.** The owner-facing summary of a PR was required "after approval, before close", but the native review lane closes the card with the reviewer's approval in one transition: the implementer never got that turn, and PRs reached the owner without the summary. The step now belongs to the handoff before the review request, the reviewer checks it, and the watchdog's safety net files a task card instead of only reporting.
