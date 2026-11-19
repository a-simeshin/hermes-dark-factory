# factory.env, key by key

Every script reads project values from `$HERMES_HOME/factory.env` through
`factory_config.py` (`cfg`, `cfg_list`); an environment variable with the same
name wins. A required key that is missing stops the script with a message
naming it. Shell scripts source the file, so values with `;`, `|`, spaces or
backslashes stay in single quotes. The order below follows
`pack/scripts/factory.env.example`.

## Required block

| Key | Inventory fact | What it controls | Unset |
|---|---|---|---|
| `FACTORY_REPO` | `owner/name` of the target repository | every `gh` call of the watchdog, hygiene, poll, metrics, bands | scripts that need it exit naming the key |
| `FACTORY_REPO_PATH` | main local clone | worktree checks, holdout prep, registry rebase | same |
| `FACTORY_BOARD` | kanban board slug | board the gates and polls act on | `main` |
| `FACTORY_OWNER_LOGIN` | owner's GitHub login | whose review is the final verdict; the webhook filter `github-pr-filter.sh` passes only this login | scripts that need it exit |
| `FACTORY_OWNER_CHAT` | `<platform>:<chat id>` | where owner alerts go | scripts that need it exit |
| `FACTORY_BASE_BRANCHES` | default branch plus release lines, comma-separated | PR bases, holdout targets, link guard bases | most scripts fall back to `main` |
| `FACTORY_BOT_EMAIL` | bot commit address | `prepush-identity-guard.py` hint, commit identity of `rebase-registry-pr.sh` | the registry rebase exits; the guard prints a placeholder address |
| `FACTORY_REVIEWER_LOGINS` | bot logins that write `## Review verdict` | reviewer verdict recognition in metrics and misses | no reviewer verdict is recognised |

## Optional, with defaults

| Key | Default | What it controls |
|---|---|---|
| `FACTORY_MIRROR_FOOTER` | `Mirrored from kanban card` | footer of mirror issues; the webhook filter drops issues carrying it |
| `FACTORY_PROTECTED_BRANCHES` | empty | branches whose worktrees `worktree-gc.py` never removes |
| `FACTORY_GUARDED_SCRIPTS` | `board-watchdog.py,pr-hygiene.py` | scripts `hermes-health.py` checks for presence and syntax |
| `FACTORY_VERDICT_PASS_WORDS`, `FACTORY_VERDICT_RETURN_WORDS`, `FACTORY_OWNER_ECHO_PREFIX` | see example | verdict parsing; change only together with `adversarial-review` |
| `FACTORY_PLAN_ACCEPT_WORDS`, `FACTORY_PLAN_RETURN_WORDS` | see example | plan verdict parsing in `plan-review-gate.py` |
| `FACTORY_CRITICAL_PATTERN` | auth, security, migration, `.github`, release | plan topics that always need a plan review |
| `FACTORY_PLAN_REVIEW_MODEL`, `FACTORY_PLAN_REVIEW_PROVIDER` | empty | model of plan-review cards; empty uses the reviewer profile's own |

## Provider-specific, no defaults

Leave unset until you have the fact from the provider's own documentation or
a live response. Unset means off, and the script says so.

| Key | What it controls | Unset |
|---|---|---|
| `FACTORY_PEAK_TZ`, `FACTORY_PEAK_START`, `FACTORY_PEAK_END` | peak-pricing window of `kanban-dispatch-peak-pause.py tick` | pause off; the tick logs which keys are missing |
| `FACTORY_PEAK_DAYS` | weekdays of the window, `0` = Monday | every day |
| `FACTORY_QUOTA_PROBE_BASE`, `FACTORY_QUOTA_PROBE_KEY_VAR`, `FACTORY_QUOTA_PROBE_MODEL` | quota guard of `hermes-health.py`: OpenAI-compatible base URL, name of the key variable in the Hermes `.env`, probe model | guard skipped with one stderr line; probe model falls back to the first fallback model |
| `FACTORY_QUOTA_CODES`, `FACTORY_QUOTA_TEXTS` | error codes and texts on a 429 that mean the quota is gone, paired by position | no log line counts as quota death; the guard never stops anything |
| `FACTORY_PRICES` | `model=input/cache_read/output;...` in plan credits, for `metrics-evening.py` and `usage-ledger.py` | cost shown as unknown, never zero |
| `FACTORY_REPORT_TZ` | day and week boundaries of the spend report | UTC, named in the report header |
| `FACTORY_OFFPEAK_MULTIPLIER` | share of the price charged outside the `FACTORY_PEAK_*` window (`0.5` = half), applied over each session's interval in the credit estimate | `1.0`: no discount; also `1.0` while the window is incomplete |
| `FACTORY_PLAN_WEEKLY_LIMIT` | plan credits per week the weekly estimate is compared with | the comparison line is skipped |

## Board watchdog, PR hygiene, evening metrics

| Key | What it controls | Unset |
|---|---|---|
| `FACTORY_CONVENTIONS_SKILL` | name of the conventions skill, for card texts | listed in the example; no shipped script reads it in this version |
| `FACTORY_BOT_LOGINS` | factory bot logins; their issues are mirrors and get no executor card | falls back to `FACTORY_REVIEWER_LOGINS` |
| `FACTORY_PROTOTYPE_EXEMPT_ISSUES` | issues exempt from the prototype gate, each a dated owner decision | none exempt |
| `FACTORY_PARITY_PATHS`, `FACTORY_PARITY_SCOPE` | path prefixes that must change together, and the sub-path the rule applies to | check off |
| `FACTORY_ISSUE_BRANCH_PREFIX` | branch prefix `<prefix><issue>-<slug>`; `pr-hygiene.py` matches a PR to its owner card by it | `hermes/` |
| `FACTORY_EOL_BRANCH`, `FACTORY_EOL_KEEP_BRANCH` | retired release line and the line that stays; flags PR and issue bodies still promising work on the retired line | check off |
| `FACTORY_VISUAL_DELTA_SUPPRESSED` | issues whose visual-delta card the owner removed | none |
| `FACTORY_CHEAP_MODEL`, `FACTORY_CHEAP_PROVIDER` | model pinned on mechanical cards (rebases) | no pin |
| `FACTORY_MILESTONES` | milestone prefixes when detection fails | none |
| `FACTORY_BUILD_CMD_RE`, `FACTORY_BUILD_FULL_RE`, `FACTORY_BUILD_SCOPE_RE` | recognise build commands in agent terminal calls for the waiting audit | build lines skipped, the report says so |

## Visual delta (UI projects only)

`FACTORY_STAND_BUILD_CMD`, `FACTORY_STAND_ARTIFACT_GLOB`,
`FACTORY_STAND_RUN_CMD` (`{artifact}`, `{port}` substituted),
`FACTORY_STAND_DEPS_REL`, `FACTORY_STAND_ROUTE`, `FACTORY_CHROME_BIN`,
`FACTORY_CDP_PORT` (default `9222`), `FACTORY_ASSETS_BRANCH` (default
`hermes-assets`), `FACTORY_ASSETS_DIR`. Needed by `visual-delta-capture.py`
when a side is a git ref; unset, the capture stops with a FAIL line naming the
key. A project without UI leaves the whole block unset.

## Nightly holdout run

`FACTORY_HOLDOUT_BUILD_CMD`, `FACTORY_HOLDOUT_ARTIFACT_GLOB` (required by
`holdout-nightly-prep.sh`), `FACTORY_HOLDOUT_PRODUCT_EXT` (unset: every changed
file counts), `FACTORY_HOLDOUT_DIFF_BASE`. Linux only. Skip the block and the
`holdout-nightly` and `holdout-stands-kill` jobs if you have no hidden
acceptance scenarios.

## Append-only registry file

`FACTORY_REGISTRY_FILE` (path relative to the repository) and
`FACTORY_BOT_NAME` (default `factory-bot`) for `rebase-registry-pr.sh`. Only
for a numbered file every PR appends to (for example a register of patches
to vendored code). Splitting such a file so it cannot conflict is the better
fix.

## Outside factory.env

- Git hooks (`reference/githooks/`): `FACTORY_STATIC_GATE` (fastest static
  check, receives the diff base ref) and `FACTORY_BASE_PATTERN` (regex over
  `origin/...` refs that count as release lines).
- `pack/scripts/pr-risk.yaml`: replace every glob marked `EXAMPLE`.
- Plugin settings in `config.yaml`: `kanban-artifact-guard` `repos`,
  `kanban-review-loop-guard` `limit` (minimum 2, default 3).
- `kanban-admission-guard` needs no key; it is a no-op until the board
  watchdog's admission gate has written
  `$HERMES_HOME/kanban/boards/<board>/admission-approved.json`.
