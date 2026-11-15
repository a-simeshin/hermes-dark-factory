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
| `usage-ledger.py` | cron | Assigns sessions to cards and comments each finished card with its token spend. Credits come from the price table in `metrics-evening.py`. |
| `pr-risk.py`, `pr-risk.yaml`, `pr-risk-labels.sh` | cron | Deterministic `risk:<class>` label on every open PR from path rules. The path globs in `pr-risk.yaml` marked EXAMPLE must be replaced with your layout. |
| `gh-link-guard.py` | guard | Refuses to post a GitHub comment or body whose repository links do not resolve. |
| `prepush-link-guard.py`, `prepush-identity-guard.py` | git hook | Called by `reference/githooks/pre-push`: dead links in pushed files, and commits made under a default agent address. |
| `github-pr-filter.sh` | webhook filter | Passes only the owner's PR comments and reviews to the agent. |
| `gh-app-token.sh` | CLI | Mints a one-hour installation token of a GitHub App. |
| `reviewer-misses.py` | cron | Records every case where the reviewer passed a commit and the owner then requested changes on it. |
| `plan-review-gate.py` | cron | Requires a plan review for a large or critical decomposition before its child cards may start. |
| `teach-snapshot.sh` | CLI | Git snapshot of the agents' knowledge layers, to prove what a teaching round changed. |
| `board-watchdog.py` | cron | Hourly watchdog of the boards: delivers diagnostics, finds stuck graphs and silence, keeps the review layer armed, and runs the gates (rework size, verdict dedup, backlog and admission, prototype first, conflict debt, idempotency keys, board lint). Single gates run through CLI flags; see `--help` in the source header. |
| `pr-hygiene.py` | cron | What can be merged today and what stops it: sibling conflicts, absorbed PRs, merge readiness, rebase cards after a base moves, stale PRs, stack order. |
| `metrics-evening.py` | cron | Daily report: spend by card and role, review latency, rework, reviewer misses, run outcomes, waiting audit, context size, digest of quiet cron events. Holds the price table used by `usage-ledger.py`: replace it with your plan's prices. |
| `visual-delta-capture.py`, `visual-delta-shot.py` | CLI | Before and after screenshots of a UI change from two worktrees. |
| `holdout-nightly.sh`, `holdout-nightly-prep.sh` | cron | Prepares worktrees and builds for the nightly hidden-acceptance run and prints the targets for the agent. Linux only. |
| `rebase-registry-pr.sh`, `resolve-registry.py` | CLI | Brings the base into a PR and resolves the conflict in an append-only numbered registry file that every PR appends to. |
| `telegram-noise-measure.py` | CLI | Counts pushes delivered by script jobs in the last 24 hours. |
| `run_peak_mutants.py` | CLI | Proves `test_peak_pause.py` turns red on each mutant. |

## Verdict words are a contract

`reviewer-misses.py` and `plan-review-gate.py` parse verdict words written by agents (`PASS`, `ACCEPTED`, `CHANGES REQUESTED`, `RETURN`). The defaults match the pack's `adversarial-review` skill. If you change the vocabulary in your skills, change the `FACTORY_VERDICT_*` and `FACTORY_PLAN_*` keys with it, otherwise the gates silently stop seeing verdicts.

## Tests

Every `test_*.py` runs on its own with `python3 test_<name>.py`, uses temporary directories and never touches a real Hermes home. Run them after every `hermes update`: several scripts read Hermes files and board databases directly.

## Known limits of this version

The three large scripts were ported as they were, with behaviour unchanged. Some of what they do is known to be broken, duplicated by Hermes itself, or irreversible (archiving cards, closing issues, rewriting the dispatcher cap). Before enabling their cron jobs, run the dry-run flags (`board-watchdog.py --lint-dry-run`, `--rework-gate-dry-run`, `--mergeflow-dry-run`, `--backlog-dry-run`, `--prototype-dry-run`; `pr-hygiene.py --dry-run`) and read what they would do on your board.
