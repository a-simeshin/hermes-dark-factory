# Debugging the factory

The general order (reproduce, read the failing log, control experiment, fix
at the root) is in the `debugging-discipline` skill. This file adds where the
factory keeps its evidence and the failure signatures already seen, with
their fixes. Sources: `docs/lessons-learned.md`, `docs/operations.md`,
`docs/extensions-catalog.md`.

## Where to look

| Question | Where |
|---|---|
| What did the gateway, dispatcher and plugins do | `~/.hermes/logs/gateway.log` (plugin failures to reach the board are logged at warning level) |
| Why did a worker run die | `profiles/<assignee>/logs/managed-worker.log` (worker tracebacks are not in `gateway.log`), `hermes kanban runs <id>` (outcomes `completed`, `blocked`, `crashed`, `timed_out`, `gave_up`, `rate_limited`) |
| What happened to a card | `hermes kanban show <id>`, `hermes kanban log <id>`; rows of `task_events` in the board database, opened read-only |
| What the native detectors see | `hermes kanban diag` |
| Did a cron job run, and what did it print | `hermes cron list`, `hermes cron runs <id>`; output under `~/.hermes/cron/` |
| What the watchdog decided | its delivered report; its state `~/.hermes/kanban/watchdog-state.json` (dedup: a finding repeats at most once a day); routine events go to the quiet digest of `cron_quiet.py`, read in the evening report |
| Is dispatch held, by whom | `kanban-dispatch-hold.py status`, `kanban-dispatch-peak-pause.py status`, log `~/.hermes/logs/kanban-dispatch-peak-pause.log` |
| Did the review-loop guard fire at all | timestamp of `~/.hermes/kanban/review-loop-guard.seen` |
| Lost uncommitted work | patches in `$HERMES_RESCUE_DIR` or `~/.hermes/rescue/` from `kanban-artifact-guard` |
| Kernel kills | `dmesg` (OOM kill lines with timestamps) |

Open Hermes databases read-only: `sqlite3.connect("file:%s?mode=ro" % path, uri=True)`.
A read-write connect to a mistyped path creates an empty `kanban.db`.

## Known signatures

| Symptom | Cause | Fix |
|---|---|---|
| A test run grows memory without bound, the gateway is OOM-killed | a test re-runs its own file (for example to prove red on committed code), which re-runs itself | remove the self-run; prove red with an in-test mutant; `run-tests.py` puts each test in its own process group and kills the group on timeout |
| One OOM kill stops the whole gateway with every live turn | systemd default `OOMPolicy=stop` | drop-in with `OOMPolicy=continue` and `OOMScoreAdjust=-800` (`docs/operations.md`); run heavy commands under `systemd-run --user --scope -p MemoryMax=1G --collect` |
| Memory explodes within minutes when two scripts load each other | import cycle through `spec_from_file_location` without a shared `sys.modules` key | register the module under one canonical key before `exec_module` |
| Cards stay `running` with no worker after a restart or kill | a dispatch hold (owner stop, quota stop, peak pause) silences the dispatcher tick including its reclaim | keep `worker-api-watchdog.py` scheduled; it reclaims running cards whose process is gone and heartbeat is stale |
| Runs compress dozens of times, then compression is blocked as ineffective | `compression.threshold` below the context a worker keeps after compression | measure the floor per profile in `managed-worker.log`, set the threshold above it with margin |
| Card with a PR does not complete as the PR contract promises | `metadata.published_pr` holds a number | it must be the full PR URL |
| PRs reach the owner without "What changes for you" | the comment was planned after approval; the review lane closes the card on approval in one transition | the comment belongs before `kanban_request_review`; the reviewer checks it; the owner-merge gate files a task card if it is missing |
| An issue of the active milestone has no executor card | a loose `#N` in another card's title counted as coverage | only anchor references count: `Issue: #N`, `issue #N`, `Refs/Closes/Fixes #N` |
| The same PR head reviewed twice, rebases done by two cards | a generator acting next to the native review lane | one PR, one card; route generators through `board_native_guard.native_or_live` |
| A guard reports "subscribed" at every start and never acts | an upstream refactor moved an internal function; the guard swallowed the error | run each plugin's `test_guard.py` against the current Hermes sources after every update |
| Workers "die of memory" at regular intervals | provider quota errors retried on a fixed schedule | look for the error code on the 429 lines; the quota guard needs `FACTORY_QUOTA_CODES` |
| A test released the owner's dispatch stop | test ran with `HERMES_HOME` at the live home | sandbox home from `mktemp -d`; `run-tests.py --audit` turns such reads red |
| Watchdog tests vanished from `scripts/` | the bundled disk-cleanup plugin deleted `test_*.py` as scratch | install the pack's `disk-cleanup` override |
| A cron script job fails at once, every run | it expects arguments; the scheduler passes none; or the script is outside `profiles/<profile>/scripts/` | a one-line wrapper in the profile's own scripts directory |
| An agent cron job fails after a provider change | the job keeps its own provider and model | `hermes cron edit <id> --provider P --model M` |
| `max_in_progress` change has no effect | dispatcher settings are read at gateway start; `0` means unset | `hermes gateway restart`; to stop, use the dispatch hold |
| Gates silently stop seeing verdicts | verdict words changed in a skill but not in `FACTORY_VERDICT_*` / `FACTORY_PLAN_*` | change both together |

## Telling the cause from a correlation

1. List every candidate cause before naming one.
2. For each, name the record that would prove it, and find it with a matching
   timestamp: an OOM kill line in `dmesg`, `ENOSPC` in a log, a provider
   error code on a 429 line, a traceback in `managed-worker.log`.
3. Compare durations of repeated failures. Equal durations are a retry or
   timeout signature (for example retries x backoff, a runner time limit), not
   resource exhaustion, which is random in time.
4. High load or full swap next to a failure is a candidate, never a verdict.
5. One card failing while its neighbours are healthy in the same minutes
   points at that card's state (skills, claim, workspace), not the host.

## Verify the artifact, not the report

A card's summary, a reviewer's PASS and an agent's "tests green" are claims.
Check `git status --porcelain` and `git log <base>..<branch>` in the worktree,
the PR on GitHub, the board through `hermes kanban show`, the snapshot diff of
`teach-snapshot.sh`, and the `TEST TOTAL` line of your own run.
