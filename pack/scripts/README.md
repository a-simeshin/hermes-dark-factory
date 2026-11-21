# Factory scripts

The mechanical layer of the factory: cron entry points, guards and small tools. They are plain Python 3 and shell with no dependencies beyond the standard library, `git`, `gh` and the `hermes` CLI.

Everything that names one project is read from `$HERMES_HOME/factory.env` through `factory_config.py`; an environment variable with the same name overrides the file. A script that needs a value you have not set stops with a message naming the key. Provider, price, time-zone and build-tool settings have no built-in values from any one installation: while such a key is unset, the feature that needs it is off or reports "unknown" and says so (`factory.env.example` lists what each key does when unset).

## Install

1. Copy this directory to `~/.hermes/scripts/`.
2. Copy `factory.env.example` to `~/.hermes/factory.env` and fill in the first block.
3. Create the cron jobs you want from `pack/cron/jobs.example.md`. Hermes runs a script job without arguments and only from the profile's own scripts directory, which is why some scripts here are one-line wrappers.

## What is here

| Script | Kind | What it does |
|---|---|---|
| `factory_config.py` | module | Reads `factory.env`. |
| `cron_quiet.py` | module | Quiet channel for script jobs: routine events go to a daily digest instead of a push to the owner. |
| `board_native_guard.py` | module, CLI | The native-or-live rule: every card generator asks `native_or_live` whether a native Hermes mechanism or a live card already covers the work, and puts the receipt into the card body; `rework_key` is the one key of a rework card. The static scan in its test fails the suite on a new generator that skips the helper. The CLI gives webhook prompts the same receipt. |
| `milestone_bands.py` | module, CLI | Priority bands computed from the open GitHub milestones on every run (active 80, next 40, others 20), with a disk cache for gh failures. The CLI prints the band of an issue or PR for prompts that create cards. |
| `scripts_digest.py` | module | Digest of the scripts directory, shared by the test runner's green stamp and the drift check. |
| `sitecustomize.py` | module | Audit hook loaded by `run-tests.py --audit`: a test that reads a real Hermes home, a live board, a real `gh` or the network turns red. |
| `run-tests.py` | CLI | Runs every `test_*.py`, each in its own process group (a timeout kills the whole group), prints `TEST TOTAL scripts/: N/M green` and writes the green stamp after a full green audited run. |
| `teach-snapshot.sh` | CLI | Git snapshot of the agents' knowledge layers, gated on the full test suite: a red suite marks the commit `RED:`. Proves what a teaching round changed. |
| `hermes-health.py` | cron | Provider and quota health, dispatch stop on quota exhaustion, failed cron runs, orphaned stands, doctor alerts for providers in use, and drift of the scripts directory past the last green test run. Silent when healthy. The quota guard is opt-in (`FACTORY_QUOTA_PROBE_*`, `FACTORY_QUOTA_CODES`); unset, it is skipped with one line on stderr. |
| `worker-api-watchdog.py` | cron | Live kanban runs: API silence with a live client, compression thrash, a run that only polls GitHub (a repeat parks the card with `kanban schedule`), self-review, duplicate review runs, orphaned claims (running without a live process and with a stale heartbeat). Also executes `SCHEDULED_UNTIL=<UTC>` parking and wakes cards blocked on a PR merge once the PR is merged. |
| `issue-executor-poll.py` | cron, CLI | Every issue of the active milestone gets an executor card within one cycle, in the milestone's band; skips delivered issues and anything already covered. `--check-issue N` answers a webhook prompt whether a card is appropriate. Logic lives in `board-watchdog.py`. |
| `board-watchdog.py` | cron | Hourly watchdog of the boards: delivers diagnostics, finds stuck graphs and silence, and runs the gates (admission and milestone bands, rework size and priority, verdict dedup, backlog, prototype first, conflict debt, idempotency keys, closed-issue tails, a closed PR card waiting for the owner's merge, board lint). One PR is worked by one card: the watchdog no longer arms reviewer cards of its own, with one safety net: the complete-review gate files a reviewer card when a card was closed with `kanban_complete` while its open PR has no verdict on the head and no live review card; it stays silent for a draft PR, for a PR with a live structural owner card (`pr-hygiene.live_cards_by_pr`, any column except done/archived), and when the closed card is a reviewer card. A second safety net, the review-lane repair gate (hourly and in `--rework-gate-only`), catches a live card in review, scheduled, ready or blocked whose last `review_requested` event has no reviewer (the native `kanban_request_review` without `reviewer=` leaves the card with the executor) and no later `changes_requested`: it runs a native `kanban reassign` to the `reviewer` profile and posts one marker comment; it leaves running cards alone, stays silent when a live reviewer card keyed `reviewer-pr<N>-*` owns the PR, and reports a failed reassign as a severity-1 finding. Single gates run through CLI flags; see `--help` in the source header. |
| `rework-gate-watchdog.sh` | cron | Runs only the rework gates of `board-watchdog.py` every 5 minutes, before the dispatcher grabs a fresh card. |
| `pr-hygiene.py` | cron | What can be merged today and what stops it: sibling conflicts, absorbed PRs, merge readiness, rebase cards after a base moves (absorbed by a live card that owns the PR, retired once the PR is mergeable unless its own push got it there or the new head still has no accept verdict), an owner card for a mergeable PR left without one, a rebase card for a conflicting PR left without one (checked on every run, not only when the base moves), a base head change kept unconsumed while any PR on that base has an undecided mergeable, stale PRs, stack order, rework priority, untracked follow-up promises, claims about external state without an artifact link, visual delta for UI issues only. |
| `plan-review-gate.py` | cron | Requires a plan review for a large or critical decomposition before its child cards may start. Runs in drain mode: plan review now goes through the native review lane, the gate only services open cycles. |
| `metrics-evening.py` | cron | Daily report: spend by card, profile (role) and, outside cards, by session source; review latency, rework, reviewer misses, run outcomes, waiting audit, context size, digest of quiet cron events. Prices come from `FACTORY_PRICES` (shared with `usage-ledger.py`); a model without a price shows its cost as unknown. Credits are an estimate: `FACTORY_OFFPEAK_MULTIPLIER` outside the `FACTORY_PEAK_*` window, applied over each session's interval (1.0 when unset). The week is compared with `FACTORY_PLAN_WEEKLY_LIMIT` (line skipped when unset) and with the quota errors counted in the agent logs by `FACTORY_QUOTA_CODES`/`FACTORY_QUOTA_TEXTS`. Day boundaries follow `FACTORY_REPORT_TZ` (UTC when unset); the local-build lines need `FACTORY_BUILD_CMD_RE`. |
| `usage-ledger.py` | cron | Assigns sessions to cards through the board's `owner_admitted` event (the title id is only a fallback), writes ledger rows with `source`, `st` and `en`, rebuilds the last 14 days on every run and comments each finished card with its token spend once: the done cards of the first run are a baseline, and the state is saved after each comment, so an interrupted run never repeats them. |
| `reviewer-misses.py` | cron | Records every case where the reviewer passed a commit and the owner then requested changes on it. |
| `hermes-backup.sh`, `backup-verify.py` | cron | Nightly `hermes backup` with a verdict that tolerates files vanishing under the archiver, archive test and rotation. |
| `worktree-gc.py`, `wt-keep.sh` | cron, CLI | Removes worktrees of finished cards once the branch is pushed and has no open PR; `wt-keep.sh` marks a tree to keep. |
| `pr-risk.py`, `pr-risk.yaml`, `pr-risk-labels.sh` | cron | Deterministic `risk:<class>` label on every open PR from path rules. The path globs in `pr-risk.yaml` marked EXAMPLE must be replaced with your layout. |
| `kanban-dispatch-hold.py` | CLI | Stops the dispatcher from handing out cards on a live gateway by holding the per-board dispatch lock; running workers finish. |
| `kanban-dispatch-peak-pause.py`, `peak-pause-tick.sh` | cron | Holds dispatch during the provider's peak-pricing window (`FACTORY_PEAK_TZ`, `FACTORY_PEAK_START`, `FACTORY_PEAK_END`); off until the window is set. |
| `gw-window.sh` | systemd unit | Deferred gateway restart that waits for a moment with no live agent turns. |
| `run-bounded.sh` | CLI | Starts a background process with a time-to-live and an owner tag, so stands cannot outlive the run that started them. |
| `holdout-stands-kill.py`, `holdout-stands-kill.sh` | CLI, cron | Stops stands by owner tag from the registry of `run-bounded.sh`. Linux only (reads `/proc`). |
| `holdout-nightly.sh`, `holdout-nightly-prep.sh` | cron | Prepares worktrees and builds for the nightly hidden-acceptance run and prints the targets for the agent; a base branch missing on origin is skipped, not fatal. Linux only. |
| `gh-link-guard.py` | guard | Refuses to post a GitHub comment or body whose repository links do not resolve. |
| `prepush-link-guard.py`, `prepush-identity-guard.py` | git hook | Called by `reference/githooks/pre-push`: dead links in pushed files, and commits made under a default agent address. |
| `github-pr-filter.sh` | webhook filter | Passes only the owner's PR comments, reviews and issue events (opened, milestoned, labeled) to the agent; drops the bot's own mirror issues. |
| `tag-sync-check.sh` | CLI | Compares every local tag with `git ls-remote --tags origin` and exits 1 on a STALE, MISSING or GONE tag; `--force` force-fetches the tags. Run before release work: a plain fetch never overwrites an existing tag. |
| `gh-app-token.sh` | CLI | Mints a one-hour installation token of a GitHub App. |
| `visual-delta-capture.py`, `visual-delta-shot.py` | CLI | Before and after screenshots of a UI change from two worktrees; one browser per run, always stopped. |
| `rebase-registry-pr.sh`, `resolve-registry.py` | CLI | Brings the base into a PR and resolves the conflict in an append-only numbered registry file that every PR appends to. |
| `telegram-noise-measure.py` | CLI | Counts pushes delivered by script jobs in the last 24 hours. |
| `run_peak_mutants.py` | CLI | Proves `test_peak_pause.py` turns red on each mutant. |

## Verdict words are a contract

`reviewer-misses.py` and `plan-review-gate.py` parse verdict words written by agents (`PASS`, `ACCEPTED`, `CHANGES REQUESTED`, `RETURN`). The defaults match the pack's `adversarial-review` skill. If you change the vocabulary in your skills, change the `FACTORY_VERDICT_*` and `FACTORY_PLAN_*` keys with it, otherwise the gates silently stop seeing verdicts.

## Tests

Every `test_*.py` runs on its own with `python3 test_<name>.py`, uses temporary directories and never touches a real Hermes home. `python3 run-tests.py --audit` runs them all with the hermeticity audit and prints one total line. Run it after every `hermes update` and before every teaching snapshot: several scripts read Hermes files and board databases directly. Tests that need Linux (`/proc`) or a Chromium binary say SKIP with the reason instead of passing silently.

## Known limits of this version

The three large scripts were ported as they were, with behaviour unchanged. Some of what they do is known to be broken, duplicated by Hermes itself, or irreversible (archiving cards, closing issues, rewriting the dispatcher cap). Before enabling their cron jobs, run the dry-run flags (`board-watchdog.py --lint-dry-run`, `--rework-gate-dry-run`, `--mergeflow-dry-run`, `--backlog-dry-run`, `--prototype-dry-run`; `pr-hygiene.py --dry-run`) and read what they would do on your board.
