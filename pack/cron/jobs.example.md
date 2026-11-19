# Cron jobs of the factory

This file lists the cron jobs the original factory ran on Hermes v0.23.1, in a portable form: what each job does, how often, in which profile, and whether it is a script job or an agent job. Use it as a checklist when you set up your own installation; create the jobs with `hermes cron create` (check `hermes cron create --help` for the flags of your version) and inspect them with `hermes cron list` and `hermes cron runs <id>`.

## Two kinds of jobs

- **Script jobs** (`no-agent` mode) run a script and deliver its stdout to the configured target. Empty stdout and exit code 0 is the documented quiet tick: nothing is delivered. Exit code non-zero marks the run as failed, so a watchdog that found problems should still exit 0 and let its printed text be the signal. The scheduler calls the script **without arguments**: a mode such as `--label-sync` must be fixed in a wrapper script, not in the job's prompt field. In v0.23.1 the script is looked up only in `~/.hermes/profiles/<profile>/scripts/` (the default profile uses `~/.hermes/scripts/`), and symlinks pointing outside that directory are refused, so keep a small `exec` wrapper there.
- **Agent jobs** start a session of the profile with a prompt. The job stores its own provider and model. After you change providers in `config.yaml`, re-pin every agent job (`hermes cron edit <id> --provider <provider> --model <model>`), otherwise the job silently falls back to whatever it was pinned to and may fail on a dead key. Agent jobs can also carry their own `--reasoning-effort`.

Schedules below are cron expressions in the server's local time. Shift jobs that do heavy I/O off the top of the hour if hourly cleanups run there (the nightly backup was moved to minute 23 for that reason).

## Script jobs

| Name | Profile | Schedule | Runs | What it does |
|---|---|---|---|---|
| nightly-backup | default | `23 4 * * *` | `hermes-backup.sh` | Native `hermes backup` to a dated zip, log-based verdict, archive test, rotation. |
| hermes-health | default | `*/10 * * * *` | `hermes-health.py` | Provider and quota health, dispatch stop on quota exhaustion, cron failures, orphaned stands. Silent when healthy. |
| board-watchdog | default | `0 * * * *` | `board-watchdog.py` | Board diagnostics delivery, stuck graph, silence, admission gate (writes the cache read by `kanban-admission-guard`), owner gates (a closed PR card waiting for the owner's merge keeps a signal until the merge). Alerts only on problems. |
| rework-gate | default | `*/5 * * * *` | `rework-gate-watchdog.sh` | Fast pass of the rework gates only (size of a fresh rework card, rework of an open PR lifted into the active priority band, rework cards on a head that has moved retired), before the dispatcher grabs the card. |
| worker-api-watchdog | default | every 10 min | `worker-api-watchdog.py` | Live kanban runs: API silence with a live client, compression thrash, a run that only polls GitHub without doing work (repeated finding parks the card with `kanban schedule`), self-review, duplicate review runs, orphaned claims (running without a live process and a stale heartbeat). Also executes `SCHEDULED_UNTIL=<UTC>` parking and wakes cards blocked on a PR merge once the PR is merged. Delivers locally; findings go to the card. |
| issue-executor-poll | default | `*/5 * * * *` | `issue-executor-poll.py` | Every issue of the active milestone gets an executor card within one cycle, with the milestone's priority band; skips issues whose work is already delivered (a merged PR after the last scope change) and anything a native or live card already covers. Silent when nothing was created. |
| pr-hygiene | default | `0 */2 * * *` | `pr-hygiene.py` | Sibling conflicts, retargeting, absorbed PRs, mergeability, abandoned branches. A needed rebase is handed to the live card that owns the PR (a comment), a separate rebase card only when no owner card exists. |
| pr-risk-labels | default | `*/30 * * * *` | `pr-risk-labels.sh` | Deterministic `risk:<class>` label on every open PR. |
| worktree-gc | default | `0 * * * *` | `worktree-gc.py` | Removes worktrees of finished cards once their branch is pushed and has no open PR. |
| usage-ledger | default | `50 23 * * *` | `usage-ledger.py` | Assigns the day's sessions to cards and comments each finished card with its spend. |
| metrics-evening | default | `0 14 * * *` | `metrics-evening.py` | Daily pipeline report: spend, card-to-merge path, rework, escaped defects, outcome shares. |
| peak-pause-tick | ops | every 15 min | `peak-pause-tick.sh` (wraps `kanban-dispatch-peak-pause.py tick`) | Holds dispatch during the provider's peak-pricing window, releases it after. Only needed if your provider prices peak hours higher. |
| holdout-stands-kill | tester | `0 23 * * *` | `holdout-stands-kill.sh` (wraps `holdout-stands-kill.py`) | Stops stands left by the nightly holdout run. |

All of these scripts ship in `pack/scripts/` (see its README), including `worker-api-watchdog.py`, `issue-executor-poll.py` and `rework-gate-watchdog.sh`, the one-line wrapper that runs `board-watchdog.py --rework-gate-only` for the `rework-gate` job, because the scheduler passes no arguments. Jobs of a profile other than default still need such a wrapper in that profile's own `scripts/` directory.

## Agent jobs

| Name | Profile | Schedule | Model tier | Prompt summary |
|---|---|---|---|---|
| holdout-nightly | tester | `0 20 * * *` | cheap | Run the hidden acceptance scenarios of the open milestone against each open PR. The job's script (`holdout-nightly.sh`) prepares worktrees and builds and prints a JSON of targets that is injected into the prompt; the agent starts stands through `run-bounded.sh`, drives the scenarios, and reports pass or fail per PR. The scenarios live in a directory only the tester may read. |
| ux-tick | default | `0 9 * * *` | cheap, high reasoning effort | Daily UX audit by dogfooding: walk the running demo with the built-in browser tools within a budget of 45 tool calls, file every friction finding as a GitHub issue awaiting the owner's triage, with reproduction steps and a screenshot (no board card). Friction only, no feature ideas. |
| ceo-tick | default | `0 7 * * 0` | strong | Weekly product cycle: research the product's category, competitors, the underlying platform and user pain on the web, read the open issues, and file 1 to 3 proposals as issues awaiting triage with the rationale (why, for whom, market signal). |
| cto-tick | default | `0 8 * * 0` | strong | Weekly technical audit in six directions (security alerts, CI health, technical debt, stack freshness, vendored upstream, conformance to the implemented standard) within 45 tool calls; at most 1 to 3 issues and a summary to the owner's chat. |

The full prompts, as templates with placeholders, are in [agent-prompts.md](agent-prompts.md).

Agent jobs produce owner-facing content, so keep them few. Each one costs a full session; a script that prints nothing when there is nothing to say is far cheaper than an agent that is asked to check.

## Retired jobs

Earlier versions ran a separate daily usage report and pipeline retro as agent jobs, and seven separate metric scripts. They were merged into `metrics-evening.py` (one script, isolated sections, one delivery). A board watchdog job in the ops profile was paused when v0.23.1 started refusing cron scripts outside the profile's scripts directory, and its function moved to the default-profile job above. With the move to "one PR = one card", the review layer of the board watchdog no longer creates separate reviewer or "What changes for you" cards: review runs through the native review status of the card that owns the PR.
