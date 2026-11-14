# Extensions catalog

This catalog lists every mechanism the factory adds on top of Hermes Agent v0.23.1: config deviations, plugins, scripts, hooks and conventions. For each one it says what it does, which native Hermes feature was checked first and why that was not enough, where it lives in this repository, and how tightly it is coupled to the project it was built for (**portable**: works anywhere after setting paths; **needs config**: generic mechanism, project values to adjust; **reference only**: read for the approach, rewrite for your project). The rule behind the catalog: the Hermes core is never patched, and every deviation from the documented way of doing things must have a recorded reason. Before each Hermes upgrade, walk this list and delete whatever upstream now does natively. Mechanisms that were removed or merged are kept at the end, marked as such, so you do not reinvent them.

## Configuration deviations

### Progressive tool loading off (`tools.tool_search: off`)

- **What:** disables progressive tool loading for architect, coder and tester.
- **Native feature checked:** the docs recommend `tool_search: auto`, which loads tool schemas lazily through a search, describe, call chain.
- **Why not enough:** workers have a turn cap, and their main tool (an LSP code-navigation MCP server, used well over a thousand times a week) went through the three-step chain on every use, eating the budget.
- **Where (original setup):** `pack/config/profile-overlay.example.yaml` (per-profile note).
- **Coupling:** portable. Keep `auto` for profiles with many rarely used tools.

### Raised turn cap (`agent.max_turns: 200`)

- **What:** coder and tester (and the default profile) get 200 turns instead of 120.
- **Native feature checked:** documented defaults of 70 (main) and 120 (profiles).
- **Why not enough:** 18-20% of coder and tester runs died on the cap, while the 90th percentile of successful runs used 91 calls. Runs that die at the cap waste all their work.
- **Where (original setup):** `pack/config/profile-overlay.example.yaml`.
- **Coupling:** portable. Measure your own p90 before choosing.

### Quota savings overlay

- **What:** compression threshold 0.08, `tool_output.max_bytes: 8000`, `api_max_retries: 3`, per-profile `disabled_toolsets`, `memory_char_limit: 2200`, `budget_warning_ratio: 0.85`, cheaper model for coder/tester/ops, reasoning effort per role.
- **Native feature checked:** all are native keys; only the values deviate from defaults.
- **Why:** the subscription quota ran out twice in a week. Together the values cut model spend roughly in half per day and by about 60% per closed card.
- **Where (original setup):** `pack/config/profile-overlay.example.yaml`, explained in `docs/config-and-quota.md`.
- **Coupling:** needs config (model names, thresholds per context window).

### Concurrency cap (`kanban.max_in_progress: 2`)

- **What:** at most two cards run at once.
- **Native feature checked:** the default is derived from memory, `clamp(MemTotal / 512 MB, 2, 8)`, which gave 8 on the host.
- **Why not enough:** one card with an LSP server and a language server for a large codebase weighed about 3.3 GB; the formula does not know what a card costs.
- **Where (original setup):** `pack/config/profile-overlay.example.yaml` (root config); the board watchdog enforces it and rolls back to 1 under memory pressure.
- **Coupling:** needs config. Measure one card's peak RSS and divide.

### Project conventions in the prompt

- **What:** the project's conventions are a skill listed in `skills.auto_load` of every profile; long references are loaded on demand; a symlink to `AGENTS.md` at the `workspaces/` level covers native discovery.
- **Native feature checked:** `AGENTS.md` discovery in the prompt builder.
- **Why not enough:** the prompt builder loads only the first context file found from the git root to the working directory (`agent/prompt_builder.py:1726`). Workspaces with the repository in a subdirectory never saw the repository's `AGENTS.md`. Both fixes use native mechanisms.
- **Where (original setup):** the conventions skill template in `pack/skills/` (see `docs/install.md`).
- **Coupling:** needs config: you write your own conventions skill.

### Shared skills outside telemetry

- **What:** the factory's skills live in one directory added to every profile with `skills.external_dirs`.
- **Native feature checked:** per-profile `skills/` with usage telemetry and the curator.
- **Why not enough:** a skill written for one role must be available to all. The trade-off is that the curator and `.usage.json` only see `~/.hermes/skills/`, so adoption of shared skills is measured from transcripts.
- **Where (original setup):** `pack/skills/`.
- **Coupling:** portable.

## Plugins

### kanban-review-loop-guard

- **What:** the third reviewer return on a chain blocks the card with `needs_input` for the owner (threshold configurable).
- **Native feature checked:** the block loop breaker.
- **Why not enough:** it counts repeated blocks only, `BLOCK_RECURRENCE_LIMIT=2` is hard-coded (`kanban_db.py:115`), and the review lane is explicitly excluded (`kanban_db.py:3408`). There is no review round limit, and chains of 10-14 rounds happened.
- **Where (original setup):** `pack/plugins/kanban-review-loop-guard/` (hook `kanban_task_claimed`).
- **Coupling:** portable.

### kanban-artifact-guard

- **What:** on completed, blocked and claimed, finds uncommitted work in the card's tree and configured repositories, saves a patch, opens a priority-95 "commit the work" card.
- **Native feature checked:** PR completion contracts (`kanban_pr_acceptance.py`).
- **Why not enough:** they look only at GitHub (published PR plus green checks). The working tree and cards without a PR are not covered.
- **Where (original setup):** `pack/plugins/kanban-artifact-guard/`.
- **Coupling:** needs config (`repos` list).

### disk-cleanup override

- **What:** copy of the bundled plugin that never deletes files under `scripts/`.
- **Native feature checked:** the bundled plugin's protected directory list.
- **Why not enough:** it deleted persistent watchdog tests (`test_*.py`) as session scratch. The override uses the native priority of `~/.hermes/plugins/` over bundled plugins.
- **Where (original setup):** `pack/plugins/disk-cleanup/`.
- **Coupling:** portable; re-sync with upstream on every upgrade.

## Watchdogs and gates

### board-watchdog

- **What:** delivers `kanban diag` and aggregates (dispatcher heartbeat, stuck graph with the cost of each blocked root, silence, cards without assignee) plus owner gates: review layer, rework size, verdict dedup, merge unblocker, `needs_input` misuse on agent steps, stacked rework, `max_in_progress`, conflict debt, backlog ownership, prototype first. Alerts only on problems.
- **Native feature checked:** `hermes kanban diag` (`kanban_diagnostics.py`, `_RULES`) and the gateway notifier.
- **Why not enough:** diagnostics are local and have no delivery; detectors do not reach GitHub; the notifier is per card and cannot filter by event kind (`kanban_watchers_notifier.py:47`), so "alert only on problems" cannot be expressed natively. A sweep that subscribed every card was tried and rolled back (127 cards, a ping on every event).
- **Where (original setup):** `board-watchdog.py`, cron hourly; `rework-gate-watchdog.sh` every 5 minutes.
- **Coupling:** reference only. Port the review layer and the stuck-graph check first; the rest encodes one owner's rules.

### pr-hygiene

- **What:** sibling conflicts that `mergeable` does not show, retargeting after a sibling merges, PRs absorbed by another, exact mergeability, verdicts per head delta, abandoned branches; RSS guards after a memory leak.
- **Native feature checked:** none exists: Hermes does not track the state of PRs or a review lane on GitHub.
- **Where (original setup):** `pr-hygiene.py`, cron every 2 hours.
- **Coupling:** reference only.

### pr-risk-labels

- **What:** deterministic risk class (`critical`, `normal`, `trivial`) per PR from diff facts and path rules, one `risk:*` label each.
- **Native feature checked:** none: native labels are not computed.
- **Where (original setup):** `pr-risk.py`, `pr-risk.yaml`, wrapper `pr-risk-labels.sh`, cron every 30 minutes.
- **Coupling:** needs config (path rules in the YAML).

### plan-review-gate

- **What:** a decomposition with more than three children or a critical topic must pass a reviewer's plan review before children run; RETURN triggers architect rework and a repeat review, capped at two cycles.
- **Native feature checked:** `decompose`.
- **Why not enough:** decompose dispatches children immediately; there is no plan review step.
- **Where (original setup):** `plan-review-gate.py`.
- **Coupling:** needs config (critical topic regex, model for the review).

### rework-gate

- **What:** a coder rework card with three or more blocking items is reassigned to the architect for decomposition before it runs.
- **Native feature checked:** none.
- **Why:** on the board's history, rework with up to two blockers burned out 9% of the time, three to four 14%, five or more 50%.
- **Where (original setup):** `board-watchdog.py --rework-gate-only` via `rework-gate-watchdog.sh`, every 5 minutes (the dispatcher picks up fresh cards within minutes, faster than the hourly watchdog).
- **Coupling:** needs config.

### hermes-health and dispatch hold

- **What:** stops dispatch when the provider reports quota exhaustion, resumes when it recovers; checks cron failures, fallback chains, orphaned stands, long owner stops. The hold holds `flock` on each board's dispatch lock.
- **Native feature checked:** dispatcher retries.
- **Why not enough:** there is no native stop on quota: the dispatcher kept retrying runs that died on HTTP 429, and four coder runs ended without completing in one night. `max_in_progress: 0` does not stop dispatch, and config changes need a gateway restart.
- **Where (original setup):** `hermes-health.py` (every 10 minutes), `kanban-dispatch-hold.py`.
- **Coupling:** health needs config (provider error codes); the hold is portable.

### peak-pause

- **What:** holds dispatch during the provider's peak-pricing hours.
- **Native feature checked:** dispatcher scheduling.
- **Why not enough:** the dispatcher has no time-of-day schedule.
- **Where (original setup):** `kanban-dispatch-peak-pause.py`, ops cron every 15 minutes.
- **Coupling:** needs config (window, time zone). Skip if your provider has flat pricing.

## Run hygiene

### run-bounded.sh

- **What:** every background process an agent starts (a stand, a dev server, a browser) goes through this wrapper: owner and deadline in a registry, `timeout` enforced. The health check kills unregistered stands older than two hours.
- **Native feature checked:** the written rule "stop your stands before reporting".
- **Why not enough:** the rule did not hold even in a session dedicated to it: stands outlived their sessions and shared ports with parallel workers.
- **Where (original setup):** `run-bounded.sh`.
- **Coupling:** portable.

### holdout-nightly and stands-kill

- **What:** nightly hidden acceptance scenarios against open PRs, then cleanup of their stands from the registry.
- **Native feature checked:** none: no nightly runs or stand accounting.
- **Where (original setup):** `holdout-nightly.sh`, `holdout-stands-kill.py`, tester cron.
- **Coupling:** reference only (build commands, milestone logic).

### worktree-gc and wt-keep

- **What:** removes worktrees of finished cards once four conditions hold (terminal card, no open PR, fully pushed, clean); dirty trees are rescued as a patch first; `.wt-keep` extends a tree's life.
- **Native feature checked:** "Worktree: preserved on completion"; `hermes kanban gc` prunes only events and logs.
- **Why not enough:** 83 directories held 3.8 GB.
- **Where (original setup):** `worktree-gc.py` (hourly), `wt-keep.sh`.
- **Coupling:** needs config.

### OOM policy drop-in

- **What:** `OOMPolicy=continue` and `OOMScoreAdjust` for the gateway unit, so an OOM kill of a child does not stop the gateway.
- **Native feature checked:** process registry limits.
- **Why not enough:** the core sets no `OOMPolicy` on the unit (`tools/process_registry.py:183`), and the systemd default `stop` took down the whole gateway with every live turn.
- **Where (original setup):** `docs/operations.md`.
- **Coupling:** portable.

## Evidence and PR tooling

### visual-delta-capture and visual-delta-shot

- **What:** one command for before/after screenshots of a UI change, published as a markdown table.
- **Native feature checked:** browser tools.
- **Why not enough:** doing it by hand cost a median of 33 tool calls and 19 minutes per card.
- **Where (original setup):** `visual-delta-capture.py`, `visual-delta-shot.py`.
- **Coupling:** reference only.

### rebase-notice and resolve-notice

- **What:** resolves a recurring merge conflict in a numbered notices file.
- **Native feature checked:** none.
- **Where (original setup):** `rebase-notice-pr.sh`, `resolve-notice.py`.
- **Coupling:** reference only. Better fix: split the file so it cannot conflict.

### reviewer-misses

- **What:** records each reviewer PASS that the owner overturned on the same or next sha; the review skill reads the journal.
- **Native feature checked:** none.
- **Where (original setup):** `reviewer-misses.py`.
- **Coupling:** needs config.

### run_peak_mutants

- **What:** mutation run for the peak-pause tests: every mutant must turn a test red.
- **Native feature checked:** none.
- **Where (original setup):** `run_peak_mutants.py`.
- **Coupling:** needs config. The pattern is the point: prove a gate's tests can fail.

## Metrics

### usage-ledger and metrics-evening

- **What:** per-card spend posted as a card comment; an evening report with spend in provider units, card-to-merge path, rework, escaped defects, outcome shares, waiting discipline, context metrics.
- **Native feature checked:** `hermes insights`.
- **Why not enough:** it does not know subscription pricing in credits (cost shows as unknown) and does not attribute spend to cards.
- **Where (original setup):** `usage-ledger.py`, `metrics-evening.py`.
- **Coupling:** reference only for the report; the per-card attribution is worth porting.

## Knowledge layer and git

### teach-snapshot and git hooks

- **What:** snapshots the knowledge layers of `~/.hermes` with a whitelist commit (never `git add -A`), refuses secrets; hooks reject em dashes and source deletions; commit-msg checks the message.
- **Native feature checked:** none: Hermes has no snapshot of its own layers and ships no hooks.
- **Why:** to prove that a lesson taught in chat actually landed, compare snapshots instead of trusting the agent's report.
- **Where (original setup):** `teach-snapshot.sh`, `reference/githooks/`.
- **Coupling:** portable.

### pre-push guards

- **What:** no push with the stock Hermes identity; every github.com link in pushed content must exist.
- **Native feature checked:** none.
- **Why:** the stock git e-mail belongs to an unrelated GitHub account, which got credited for the agents' commits; a model invented two dozen links to a misspelled repository.
- **Where (original setup):** `reference/githooks/pre-push`, `prepush-identity-guard.py`, `prepush-link-guard.py`, `gh-link-guard.py` (the same check for `gh` comments and bodies).
- **Coupling:** portable (identity), needs config (links).

### gh-app-token and the gh wrapper

- **What:** a one-hour GitHub App installation token minted on demand for `gh`; no static `GITHUB_TOKEN` in `.env`.
- **Native feature checked:** `hermes doctor` asks for `GITHUB_TOKEN`.
- **Why not enough:** a static token is worse than one that expires in an hour; access goes through the App, which is also the bot identity of the PRs.
- **Where (original setup):** `gh-app-token.sh`.
- **Coupling:** portable.

### github-pr-filter

- **What:** webhook filter: only the owner's comments and reviews pass, with a uniform `pr_number` added for the prompt template.
- **Native feature checked:** webhook subscriptions with an external filter script.
- **Why not enough:** the mechanism runs a script but ships no payload logic (who wrote it, which PR).
- **Where (original setup):** `github-pr-filter.sh`.
- **Coupling:** needs config (`FACTORY_OWNER`).

## Operations

### nightly-backup and backup-verify

- **What:** native `hermes backup` plus rotation and an independent restorability check.
- **Native feature checked:** `hermes backup` does the backup itself.
- **Why add anything:** it exits non-zero when files vanish during archiving, which hourly cleanups cause; the verdict distinguishes that from real failure.
- **Where (original setup):** `hermes-backup.sh`, `backup-verify.py`.
- **Coupling:** portable.

### Agent ticks (UX, product, technical reviews)

- **What:** scheduled agent jobs that propose triage cards.
- **Native feature checked:** cron agent jobs are native; only the content is ours.
- **Where (original setup):** `pack/cron/jobs.example.md`.
- **Coupling:** needs config (your prompts).

### Host hygiene (not part of the pack)

The original host also ran fail2ban and automatic security updates, and deliberately left build-time npm advisories in the vendored upstream web UI unfixed (not served). These are host decisions, listed only for completeness.

## Removed or merged mechanisms

- ~~**Core patches**~~ (removing the parent gate in `complete_task`, idempotent decomposition): removed before an upgrade. Never patch the core; the patch could not survive `hermes update`.
- ~~**Own tar backup**~~: replaced by native `hermes backup`, which excludes caches and vendored code and has a documented restore (`hermes import`).
- ~~**Seven metric scripts**~~ (usage report, factory metrics, pipeline quality, wait audit, retro input and others): merged into `metrics-evening.py` with isolated sections.
- ~~**Notify subscription sweep**~~: subscribing every card produced a ping per event; rolled back. The watchdog subscribes only cards created without a chat anchor.
- ~~**Conventions file in `projects-context/`**~~: replaced by an auto-loaded conventions skill.
- ~~**Ops-profile board watchdog cron**~~: paused when v0.23.1 refused cron scripts outside the profile's scripts directory; the job runs in the default profile.
- ~~**Own handoff script on the turn limit**~~: dropped in favour of native `agent.budget_warning_ratio`, which already asks the worker to leave a handoff.
