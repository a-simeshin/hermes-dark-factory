# Factory scripts

The mechanical layer of the factory: cron entry points, guards and small tools. They are plain Python 3 and shell with no dependencies beyond the standard library, `git`, `gh` and the `hermes` CLI.

Everything that names one project is read from `$HERMES_HOME/factory.env` through `factory_config.py`; an environment variable with the same name overrides the file. A script that needs a value you have not set stops with a message naming the key.

## Install

1. Copy this directory to `~/.hermes/scripts/`.
2. Copy `factory.env.example` to `~/.hermes/factory.env` and fill in the first block.
3. Create the cron jobs you want from `pack/cron/jobs.example.md`. Hermes runs a script job without arguments and only from the profile's own scripts directory, which is why some scripts here are one-line wrappers.

## What is here

| Script | Kind | What it does |
|---|---|---|
| `factory_config.py` | module | Reads `factory.env`. |
| `cron_quiet.py` | module | Quiet channel for script jobs: routine events go to a daily digest instead of a push to the owner. |
| `hermes-health.py` | cron | Provider and quota health, dispatch stop on quota exhaustion, failed cron runs, orphaned stands. Silent when healthy. |
| `hermes-backup.sh`, `backup-verify.py` | cron | Nightly `hermes backup` with a verdict that tolerates files vanishing under the archiver, archive test and rotation. |
| `kanban-dispatch-hold.py` | CLI | Stops the dispatcher from handing out cards on a live gateway by holding the per-board dispatch lock; running workers finish. |
| `kanban-dispatch-peak-pause.py`, `peak-pause-tick.sh` | cron | Holds dispatch during the provider's peak-pricing window. |
| `gw-window.sh` | systemd unit | Deferred gateway restart that waits for a moment with no live agent turns. |
| `run-bounded.sh` | CLI | Starts a background process with a time-to-live and an owner tag, so stands cannot outlive the run that started them. |
| `holdout-stands-kill.py`, `holdout-stands-kill.sh` | CLI, cron | Stops stands by owner tag from the registry of `run-bounded.sh`. Linux only (reads `/proc`). |
| `worktree-gc.py`, `wt-keep.sh` | cron, CLI | Removes worktrees of finished cards once the branch is pushed and has no open PR; `wt-keep.sh` marks a tree to keep. |
| `usage-ledger.py` | cron | Assigns sessions to cards and comments each finished card with its token spend. Credits need the price table of `metrics-evening.py`, which is not shipped yet; without it tokens are counted and credits are zero. |
| `pr-risk.py`, `pr-risk.yaml`, `pr-risk-labels.sh` | cron | Deterministic `risk:<class>` label on every open PR from path rules. The path globs in `pr-risk.yaml` marked EXAMPLE must be replaced with your layout. |
| `gh-link-guard.py` | guard | Refuses to post a GitHub comment or body whose repository links do not resolve. |
| `prepush-link-guard.py`, `prepush-identity-guard.py` | git hook | Called by `reference/githooks/pre-push`: dead links in pushed files, and commits made under a default agent address. |
| `github-pr-filter.sh` | webhook filter | Passes only the owner's PR comments and reviews to the agent. |
| `gh-app-token.sh` | CLI | Mints a one-hour installation token of a GitHub App. |
| `reviewer-misses.py` | cron | Records every case where the reviewer passed a commit and the owner then requested changes on it. |
| `plan-review-gate.py` | cron | Requires a plan review for a large or critical decomposition before its child cards may start. |
| `teach-snapshot.sh` | CLI | Git snapshot of the agents' knowledge layers, to prove what a teaching round changed. |

## Verdict words are a contract

`reviewer-misses.py` and `plan-review-gate.py` parse verdict words written by agents (`PASS`, `ACCEPTED`, `CHANGES REQUESTED`, `RETURN`). The defaults match the pack's `adversarial-review` skill. If you change the vocabulary in your skills, change the `FACTORY_VERDICT_*` and `FACTORY_PLAN_*` keys with it, otherwise the gates silently stop seeing verdicts.

## Tests

Every `test_*.py` runs on its own with `python3 test_<name>.py`, uses temporary directories and never touches a real Hermes home. Run them after every `hermes update`: several scripts read Hermes files and board databases directly.
