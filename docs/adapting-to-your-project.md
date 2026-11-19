# Adapting the factory to your project

This guide is for the person who wants to run the pack on their own repository. It walks through the whole journey: learning the facts about your project, turning them into configuration, installing the pack, proving that it works before anything acts on its own, finding the cause when something misbehaves, and changing a mechanism safely. The agents follow the same journey as a procedure in the `factory-adoption` skill (`pack/skills/factory-adoption/`); this document explains what they are doing and where your judgement is needed.

## Start with facts about your project

Everything the factory needs to know about a project lives in one file, `factory.env`, and a project conventions skill. Before writing either, collect the facts. You can ask the orchestrator to do the inventory with the `factory-adoption` skill, but ask for the source of every fact (a file in the repository, a command output, a page of the provider's documentation), because a guessed value does more harm than an empty one.

The facts that matter are these. Which repository, which local clone, and which long-lived branches pull requests target, including release lines and any line you have declared end-of-life. Which CI checks are required, and whether a ruleset already lets only you merge. How the project is built and tested, and which static check is fastest. Whether the product has a user interface that can be started on a stand and screenshotted. Whether some modules must always change together, or whether every pull request appends to one numbered registry file. Which model provider you use, what it charges for input, cached and output tokens, whether it has peak-hour pricing, and which error codes it returns when the quota runs out. Finally, your time zone, your GitHub login and chat, and the logins and commit address of the bot accounts.

## Turn the facts into configuration

Copy `pack/scripts/factory.env.example` to `~/.hermes/factory.env` and fill in the first block: repository, clone path, board, owner login and chat, base branches, bot email and reviewer logins. The skill's reference `references/factory-env.md` explains every key, what it controls, and what happens while it is unset.

The rest of the file is a set of decisions rather than blanks to fill. Each optional mechanism can stay off, and off is often the right answer for a new installation:

- **Release-line checks.** Set `FACTORY_EOL_BRANCH` and `FACTORY_EOL_KEEP_BRANCH` only if you retired a release line and want PR and issue bodies that still promise work on it flagged.
- **Contract parity.** `FACTORY_PARITY_PATHS` and `FACTORY_PARITY_SCOPE` are for modules that implement one contract and must change together.
- **Registry rebase.** `FACTORY_REGISTRY_FILE` is for an append-only numbered file that every pull request touches. Splitting the file is the better fix if you can.
- **Visual delta.** The `FACTORY_STAND_*` keys are only for projects with a user interface.
- **Nightly holdout runs.** The `FACTORY_HOLDOUT_*` keys are only for projects with hidden acceptance scenarios.
- **Peak pause, quota guard and prices.** The `FACTORY_PEAK_*`, `FACTORY_QUOTA_*` and `FACTORY_PRICES` keys depend entirely on your provider. The pack ships no values for them on purpose. Leave them unset until you have the numbers from the provider; while they are unset the feature reports itself as off, and the spend report shows cost as unknown rather than zero.

Two more files carry project values. Replace every path glob marked `EXAMPLE` in `pack/scripts/pr-risk.yaml` with your own layout. Write a project conventions skill with the integration branch, the build and test commands, the stand addresses and your decisions with dates, and put the same essentials into the target repository's `AGENTS.md`.

## Install

Installation follows [install.md](install.md): skills through `skills.external_dirs`, personas from `pack/souls/`, plugins from `pack/plugins/`, the config overlay merged into the root and every profile config, scripts copied to `~/.hermes/scripts/`, and git hooks from `reference/githooks/`. Restart the gateway with `hermes gateway restart`, without `-y`, so it refuses to interrupt live agent turns. Take a snapshot of the knowledge layer with `teach-snapshot.sh` so that every later change has a baseline to diff against.

Do not create the cron jobs yet.

## Prove it works before it acts

The test suite comes first. Run `python3 ~/.hermes/scripts/run-tests.py --audit` and read its last line. The only acceptable evidence of a green suite is the line `TEST TOTAL scripts/: N/M green` with both numbers equal, from a run you or the agent just made. Run each plugin's `test_guard.py` as its README describes.

Next come the dry runs. The three large scripts encode one owner's rules and can archive cards, close issues and rewrite the dispatcher cap, so you read what they would do before they do it. Run `board-watchdog.py` with `--lint-dry-run`, `--rework-gate-dry-run`, `--mergeflow-dry-run`, `--backlog-dry-run` and `--prototype-dry-run`, `pr-hygiene.py --dry-run`, and `issue-executor-poll.py --dry-run`. Read the output line by line with the board open. If a finding is wrong for your project, that is a configuration or a rule to adjust now, not after the job has acted on it. The merge flow of `pr-hygiene.py` stays in report-only mode while the file `$HERMES_HOME/cache/mergeflow-hold` exists; keep it there until you accept the flow. `board-watchdog.py --self-test` proves that reports actually reach your chat.

Only then create the cron jobs from `pack/cron/jobs.example.md`, starting with the read-only and reporting ones. Finally, push one card through the whole flow on a throwaway issue in the active milestone: the same card should open the pull request, post the "What changes for you" comment before it asks for review, pass the review lane, and close with "remaining: owner merge". Check each step on GitHub and on the board rather than in the card's own summary.

## When something goes wrong

The skill's reference `references/debugging.md` lists where the evidence lives (gateway log, the per-profile `managed-worker.log`, `hermes kanban runs` and `hermes kanban log` for a card, `hermes cron runs` for a job, the watchdog's state and digest, `dmesg` for kernel kills) and the failure signatures already seen on the original installation, each with its fix. Among them: a test that re-runs itself and grows until the OOM killer takes the gateway; a dispatch hold that also silences the reclaim of dead runs; a compression threshold below the context a worker keeps after compression, which makes compression loop; `metadata.published_pr` holding a number instead of the full URL; the "What changes for you" comment placed after approval, where the implementer never gets a turn; and a loose `#N` mention counted as an issue's executor.

Two habits matter more than any single signature. First, do not accept a diagnosis from resource correlation alone. High load next to a crash is a candidate; the cause is named only with direct evidence such as an OOM line, an error code or a traceback with a matching timestamp, and failures that repeat with the same duration usually point at retries or timeouts rather than at memory. Second, verify the artifact, not the report: the working tree, the pull request, the board and the snapshot diff, never the agent's description of them.

## Changing a mechanism

When you or an agent change the pack, the rules in `references/modifying.md` apply. Check whether Hermes already does it natively before building anything, and extend the mechanism that already owns the area instead of adding a parallel one. Keep project values in `factory.env`, and never patch the Hermes core. Every change comes with a standalone test that runs in a temporary Hermes home and proves it can fail through a mutant applied inside the test, never through git history and never by re-running its own file. The full suite must end with an equal `TEST TOTAL` line. Update `pack/scripts/README.md` and [extensions-catalog.md](extensions-catalog.md) so the next person knows what exists and why.

When the change is to the agents' behaviour rather than to a script, teach them through their own chat as described in [teaching-hermes.md](teaching-hermes.md), and verify the snapshot diff instead of editing their files yourself.

## What stays with you

Some decisions are always the owner's, whatever the agents propose: merging and releasing, the scope and priority of work (which is what milestones express), archiving or closing work you parked, lifting a dispatch stop you put up, and accepting the merge flow by removing the hold file. Ask the agent for evidence you can check (the inventory with sources, the dry-run outputs, the `TEST TOTAL` line, the snapshot hash) and check it yourself. [owner-journey.md](owner-journey.md) describes the day-to-day side of the same role.
