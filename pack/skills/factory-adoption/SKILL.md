---
name: factory-adoption
description: 'Set up, adapt, configure, debug, modify or upgrade the factory pack for a project.'
version: 1.0.0
author: maintainer, Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [setup, adoption, configuration, debugging, upgrade, factory, pack]
    related_skills: [debugging-discipline, kanban-discipline, fix-at-the-root, save-lesson-before-complete, worker-run-discipline]
---

# Adopting, debugging and changing the factory pack

This skill is the procedure for bringing the pack (skills, souls, plugins,
config overlay, scripts, cron jobs) onto a new project, proving it works,
finding the cause when it does not, and changing a mechanism without breaking
the others. Everything here names files, keys and flags that exist in the
pack. Details: references/factory-env.md (every `factory.env` key),
references/debugging.md (where to look, known failure signatures),
references/modifying.md (rules and test pattern for a change). The human
guide is `docs/adapting-to-your-project.md`.

## When to Use

- The owner asks you to set up, adapt or configure the factory for a repository.
- A factory script, cron job, plugin or gate misbehaves and you must find why.
- You are asked to change, extend or remove a mechanism of the pack.
- Before and after `hermes update`.

## Quick Reference

| Need | Command or file |
|---|---|
| All project values | `$HERMES_HOME/factory.env` (from `pack/scripts/factory.env.example`) |
| Run every script test | `python3 ~/.hermes/scripts/run-tests.py --audit` |
| "Green" | the line `TEST TOTAL scripts/: N/M green` with N = M, quoted verbatim |
| Watchdog dry runs | `board-watchdog.py --lint-dry-run`, `--rework-gate-dry-run`, `--mergeflow-dry-run`, `--backlog-dry-run`, `--prototype-dry-run` |
| PR hygiene dry runs | `pr-hygiene.py --dry-run`, `--mergeflow-dry-run` |
| Issue poll dry run | `issue-executor-poll.py --dry-run`; one issue: `--check-issue N` |
| Delivery check | `board-watchdog.py --self-test` |
| Stop / resume dispatch | `kanban-dispatch-hold.py start --owner O --reason R`, `status`, `release` |
| Peak pause state | `kanban-dispatch-peak-pause.py status` |
| Board state | `hermes kanban diag`, `hermes kanban show <id>`, `hermes kanban runs <id>`, `hermes kanban log <id>` |
| Cron state | `hermes cron list`, `hermes cron runs <id>` |
| Snapshot knowledge layer | `teach-snapshot.sh` (diff), `teach-snapshot.sh "label"` (commit) |
| Restart safely | `hermes gateway restart` (never `-y`, never `systemctl --user restart`) |

## Procedure

### 1. Inventory the target project

Collect facts, not guesses; write each with its source (file, command output):

1. Repository `owner/name`, local clone path, default branch, other long-lived
   base branches and release lines (and whether one is end-of-life).
2. CI: workflow files, required checks on protected branches, the ruleset that
   lets only the owner merge.
3. Build and test commands, the fastest static check, the full verification
   command.
4. UI or not: is there a stand to start and a route to screenshot.
5. Vendored or contract code: modules that must change together, an
   append-only registry file every PR touches.
6. Model provider: model ids, prices per input/cache/output, peak-pricing
   window, quota error codes and texts on a 429.
7. Time zone for reports; owner login and chat; bot logins and bot commit email.
8. Milestones in use (the admission gate and priority bands read them).

### 2. Map the inventory to configuration

1. Copy `pack/scripts/factory.env.example` to `~/.hermes/factory.env`. Walk it
   key by key with references/factory-env.md; fill the first block, decide
   each optional block, leave provider keys unset rather than guess.
2. Decide the optional mechanisms explicitly and record the decision:
   release-line twins and EOL check (`FACTORY_EOL_BRANCH`), contract parity
   (`FACTORY_PARITY_PATHS`), registry rebase (`FACTORY_REGISTRY_FILE`), visual
   delta (`FACTORY_STAND_*`), holdout stands (`FACTORY_HOLDOUT_*`), peak pause
   (`FACTORY_PEAK_*`), quota guard (`FACTORY_QUOTA_*`), price table
   (`FACTORY_PRICES`). Off is a valid answer.
3. Replace the `EXAMPLE` path globs in `pack/scripts/pr-risk.yaml`.
4. Write the project conventions skill (integration branch, build and test
   commands, stands, owner decisions with dates) and the same essentials in
   the target repository's `AGENTS.md`.

### 3. Install, in the order of docs/install.md

1. Skills: `skills.external_dirs` in every profile's `config.yaml` points at
   `<pack-clone>/pack/skills`; the conventions skill goes in `skills.auto_load`.
2. Souls: `pack/souls/<role>.SOUL.md` to each profile's `SOUL.md`
   (`orchestrator.SOUL.md` to the root), plus a marked project section.
3. Plugins: `pack/plugins/*` to `~/.hermes/plugins/`; give
   `kanban-artifact-guard` its `repos` list as its README shows.
4. Config: merge `pack/config/profile-overlay.example.yaml` into the root and
   every profile config (each is a complete file); back up first.
5. Scripts: `pack/scripts/` to `~/.hermes/scripts/`; wrappers for other
   profiles in `profiles/<profile>/scripts/`. Git hooks from
   `reference/githooks/` per its README.
6. Restart with `hermes gateway restart`; snapshot with `teach-snapshot.sh "pack installed"`.
7. Cron jobs from `pack/cron/jobs.example.md` only after step 4 of Verification.

### 4. Debug

Follow references/debugging.md: reproduce, read the logs and board events for
the exact time, match a known signature, prove the cause with direct
evidence, fix at the root, add a test. Never name resource exhaustion without
an OOM line or an error code with a matching timestamp.

### 5. Modify

Follow references/modifying.md: native first, extend before adding, values in
`factory.env`, no core patch, a standalone test proven red by an in-test
mutant, full suite green, docs and catalog updated.

### 6. Upgrade

Before `hermes update --backup --yes`: walk `docs/extensions-catalog.md`
against the release notes and remove what became native. After: `hermes
gateway restart`, `hermes doctor`, each plugin's `test_guard.py` with the
Hermes interpreter, `run-tests.py --audit`, `hermes cron list` for paused or
failing jobs, diff `disk-cleanup` against the bundled plugin.

## Pitfalls

- A key you guessed is worse than an unset key: unset provider keys switch a
  feature off and say so; a wrong one acts silently.
- Verdict words (`FACTORY_VERDICT_*`, `FACTORY_PLAN_*`) are a contract with the
  review skills; change both together or the gates stop seeing verdicts.
- `board-watchdog.py`, `pr-hygiene.py` and `metrics-evening.py` archive cards,
  close issues and rewrite the dispatcher cap. Never enable them before their
  dry runs were read with the owner.
- `max_in_progress: 0` does not stop dispatch; use the dispatch hold.
- A dispatch hold also stops native reclaim; keep `worker-api-watchdog.py`
  scheduled whenever holds or the peak pause are in use.
- Cron script jobs get no arguments; a mode needs a wrapper script.
- After a provider change, re-pin every agent cron job (`hermes cron edit <id> --provider P --model M`).
- Never run tests with `HERMES_HOME` pointing at the live home.

## Verification

1. `python3 ~/.hermes/scripts/run-tests.py --audit`: quote the final
   `TEST TOTAL scripts/: N/M green` line of your own run. Anything else is not green.
2. Plugin tests: `python3 test_guard.py` in each plugin directory
   (`kanban-artifact-guard` needs `HERMES_AGENT_SRC` and the Hermes venv, see its README).
3. `board-watchdog.py --self-test` delivers a line to the owner chat.
4. Every dry run in the Quick Reference, output saved and read with the owner
   before the matching cron job is created. Keep the merge flow of
   `pr-hygiene.py` in dry run until the owner accepts it: while
   `$HERMES_HOME/cache/mergeflow-hold` exists, its merge-flow actions only report.
5. `hermes -p coder -z "List the first three rules of the project conventions"` quotes your conventions skill.
6. One end-to-end card on a throwaway issue in the active milestone: the same
   card opens the PR (`metadata.published_pr` is the full URL), posts
   "What changes for you" before `kanban_request_review`, passes the review
   lane, closes with "remaining: owner merge". Check each step on GitHub and
   the board, not in the card's report.

## For the owner

- Ask the agent for: the inventory with sources, the filled `factory.env` with
  a reason per optional block, the dry-run outputs, the `TEST TOTAL` line, the
  snapshot hash.
- Check yourself: the dry-run outputs line by line, the throwaway card on
  GitHub, the snapshot diff after any teaching round.
- Always yours: merges and releases, scope and priorities (milestones),
  archiving or closing work you parked, lifting your own dispatch stop,
  accepting the merge flow (removing `mergeflow-hold`). Teach through the
  agent's chat (`docs/teaching-hermes.md`), not by editing its files.
