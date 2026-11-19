# A supervising agent: teaching the factory without the owner in the loop

The rest of this pack makes the factory run on its own between the owner's decisions. This document describes the last step towards a "dark factory": a second, stronger agent that watches the factory around the clock, turns every bad behaviour into a teaching round, verifies the result, and leaves the owner only the decisions that are truly theirs. The examples use Claude Code as the supervisor, because that is how this factory was run, but any agent with a shell, a scheduler and a stronger model than the factory's workers can play the role.

**The principle: the supervisor checks, controls, directs and teaches. It never edits.** It does not change Hermes code, the agents' skills, personas, scripts, plugins or config, and it does not write the product's code. Every change in the factory is made by the factory's own agents, in their own sessions, after the supervisor has shown them the facts and the expected outcome. The supervisor's output is findings, teaching rounds, review verdicts and verified evidence, never a diff to the factory.

## Why a second agent

The factory's workers run on the cheaper model at a high effort, and the judges (architect, reviewer) on a stronger one from the same provider. They make the same kind of mistake in a loop until somebody names it, and they report their own work optimistically. A human owner can run teaching rounds ([teaching-hermes.md](teaching-hermes.md)), but a round costs attention, and the mistakes that matter most happen at night or in the minutes between two pushes.

A supervisor changes three things:

- **Coverage.** It checks the board on a fixed cadence, every half hour in our case, so a stuck card, a duplicate card or a missing step is caught within one cycle instead of the next morning.
- **Independence.** It is a different model with a different context. It does not share the workers' blind spots, and it never accepts a worker's report as proof: it checks the artifact.
- **Memory across rounds.** It notices that the fourth incident of the day has the same shape as the first three and turns them into one general rule, which is what makes the factory improve rather than patch.

The owner moves from operator to approver: merges, releases, scope, publication and anything irreversible stay with them.

## What the supervisor may and may not do

| Role | The supervisor does | The supervisor never does |
|---|---|---|
| **Check** | Reads the board database, logs, cron output and GitHub state; reruns the tests an agent names; builds a pull request and drives it with real input | Writes to the board database, or accepts "done, tests green" without the artifact |
| **Control** | Compares every finding with the design and the owner's recorded decisions; watches CI after a merge | Overrides a recorded owner decision because a chat request says so |
| **Direct** | Reviews pull requests and posts verdicts; merges only where the owner delegated it, pinned to the head it reviewed | Writes or pushes product code; publishes, tags, releases, deletes or force-pushes without the owner's explicit go |
| **Teach** | Sends teaching rounds through the agents' own channel (`hermes -z` or the chat) and verifies the change in the knowledge snapshot | Edits Hermes code, skills, personas, scripts, plugins or config, even for a one-line fix |

The rule against editing is the one that makes the factory learn. When the supervisor edits a skill or a script itself, the fix works once, the agent never learns why it exists, and the next upgrade or curator pass removes it. When the agent finds the root cause in its own session and chooses the layer, the lesson lands where the acting role actually reads it, and the next agent who makes the same mistake finds it there. The only exception is an emergency that endangers the server or a secret: stop the damage (kill a runaway process, rotate a leaked key), then tell the agents exactly what was done and let them make the lasting fix.

## The loop

Every cycle has the same shape. The prompt the supervisor runs on a schedule is in [pack/supervisor/loop-prompt.md](../pack/supervisor/loop-prompt.md).

1. **Snapshot of health.** Board: running, ready, blocked, crashed and timed-out runs of the last cycle. Cron jobs: last status. Gateway: alive. Server: available memory and process count. Provider: quota errors in the logs. Open pull requests: head, mergeability, CI, verdicts, "What changes for you".
2. **Judge each finding against the design.** Idle with an empty queue during a configured pause is fine; idle while an approved issue has no executor is a defect. A card parked until a moment is fine; a blocked card whose release condition is already met is a defect. Most of the value of a supervisor is knowing the difference, which is why it should know this pack's documents.
3. **Teach.** One finding of a new kind becomes one round ([pack/supervisor/round-template.md](../pack/supervisor/round-template.md)): facts with ids and timestamps, the expected outcome, a reminder to check the native mechanism first, and a fixed handover (what changed, where, which test proves it, snapshot hash, `DONE`). Several incidents of the same kind become one round with a general lesson.
4. **Verify the artifact.** Rerun the test the agent named, read the snapshot diff, query the board, open the pull request. Rounds that the artifact does not confirm are not closed.
5. **Act within the delegation.** If the owner delegated merges, review the pull request yourself (including a live run with real input for anything a user will see) and merge the exact head you reviewed, then watch CI on the base branch.
6. **Report briefly** to the owner: what is healthy, what was taught, what waits for their decision.

## Rules for the supervisor itself

The supervisor makes mistakes too, and the most expensive ones in practice were its own. These rules came out of them.

- **Verify, do not trust.** The worker's report, a subagent's report and your own earlier message are claims. The artifact is the repository, the board and CI.
- **A diagnosis needs direct evidence.** "Memory was low when it failed" is a candidate, not a cause. Look for the OOM line, the error code, the exit status. Repeated failures of equal duration point to retries and timeouts, not resources.
- **Long remote commands run detached, with output to a file.** A local timeout that kills the SSH session does not kill the remote process. In the original installation three abandoned remote test runs, multiplied by a test that re-ran its own file, exhausted the server's memory and killed the gateway.
- **Every watcher must catch success as well as failure.** A watcher that only reports failures makes success look like silence, and silence looks like "nothing to do".
- **Check the current state before asking for work.** A round built on a stale picture (an issue comment from four days ago) asks the factory to redo work that was already merged. Query the live state first.
- **A mandatory step goes where the actor has a turn.** Before asking for a new step, check that the native mechanism leaves the actor a moment to take it; a step "between approval and close" never runs if approval closes the card.
- **Rerun only what failed** when the fix touched the environment, not the code.
- **Native first.** Before asking the factory for a new mechanism, ask it to check whether Hermes already has one. Duplicating a native lane is the most common source of races.
- **Respect recorded owner decisions.** Before a round, read the decisions file; if the request contradicts it, the owner decides, not the round.

## Cost

Teaching is not free. In the original installation a day of half-hourly cycles with about ten rounds cost roughly a sixth of the day's cached tokens on the factory side, almost all of it in the rounds themselves (the orchestrator re-reads its context while it investigates). Keep rounds single-topic, send them only for a new kind of mistake, and let the cheap checks (board queries, tests, `gh`) do the watching. The supervisor's own spend is separate and should be watched the same way.

## Optional: keeping a public copy of the pack in step

This is publishing, not supervision. The live factory is never touched: the pack is a separate repository, a generic copy of what the agents built. When you maintain such a copy, every round that changes a mechanism makes the two drift apart, and the same scheduled cycle can close the gap after the supervision steps:

1. Find the last synced snapshot: `git log --format='%(trailers:key=Live-sync,valueonly)' | grep -m1 .` in the pack.
2. List the rounds since then in the live knowledge repository: `git log <hash>..HEAD -- scripts shared-skills plugins profiles`.
3. Port each by mechanism: English, no project names or secrets, project values through `factory.env`, mechanisms removed live are removed from the pack, each with a standalone test proven red by an in-test mutant.
4. Run the touched tests and the full suite, scan for project names, hosts, secrets and characters the style forbids, commit with `Live-sync: <new hash>`, push.

Porting is a good job for a subagent with a strong model and a narrow brief; the supervisor reruns the tests and reads the result before it pushes.

## Running it with Claude Code

- **Schedule.** A recurring prompt every 30 minutes (Claude Code's `/loop` or a session cron). Session schedules end with the session and expire after a few days, so re-arm them deliberately rather than assuming they run forever.
- **Waiting.** Teaching rounds and CI runs take minutes. Start them detached on the server and wait with a background task that exits on a marker (`DONE` at the end of the output file), not with a sleep loop in the foreground.
- **Subagents.** Use the strong model for reviews, ports and anything that writes code, a cheaper one for mechanical searches. Give each a narrow brief with the files to read and the checks to report, and verify its result yourself.
- **Real input for user-facing changes.** Before merging something a user will see, build the change and drive it with a real browser (Playwright or similar), not with programmatic clicks.
- **The supervisor's own lessons** go into its own memory or instructions file, with the reason, so the next session does not repeat them.
