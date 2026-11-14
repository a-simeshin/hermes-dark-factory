# How Hermes works (the parts a factory depends on)

This document explains the internals of Hermes Agent v0.23.1 that matter when you run an autonomous team on it: profiles and the multiplexed gateway, the kanban board and its dispatcher, worker runs, cron jobs, webhooks, Telegram delivery and notification subscriptions, the knowledge layers (SOUL, skills, memory, project context files), context compression, reasoning effort and the skill curator. It is written from the operator's point of view: what each piece does, where its state lives, which settings are read when, and the non-obvious behaviours that cost us time. Statements about Hermes are taken from the running installation and its source; file references such as `kanban_db.py:115` point at v0.23.1. When in doubt about a flag, run `hermes <command> --help` on your version.

## Home directory and profiles

Everything Hermes owns lives under `~/.hermes` (or `$HERMES_HOME`):

| Path | Contents |
|---|---|
| `config.yaml`, `.env`, `SOUL.md` | root (default profile) configuration, secrets, persona |
| `profiles/<name>/` | one directory per additional profile: its own `config.yaml`, `.env`, `SOUL.md`, `state.db`, `memories/`, `skills/`, `scripts/` |
| `skills/` | built-in, hub-installed and agent-created skills |
| `kanban/boards/<slug>/kanban.db` | one SQLite database per board, plus `workspaces/t_<id>/` for card workspaces |
| `cron/`, `sessions/`, `logs/` | cron jobs and their output, session transcripts, logs (`logs/gateway.log` is the main one) |
| `plugins/` | user plugins; a user plugin overrides a bundled one with the same name |
| `hermes-agent/` | the vendored Hermes source and its virtualenv |

A **profile** is a full agent identity: model and provider, reasoning effort, toolsets, SOUL, memory, skills and secrets. The factory uses six: `default` (the owner's chat front desk and the home of most cron jobs), `architect`, `coder`, `reviewer`, `tester` and `ops`. You talk to a specific profile with `hermes -p <profile> ...` (for example `hermes -p reviewer -z "question"` for a one-shot), and list them with `hermes profile list`.

Each profile's `config.yaml` is a complete file, not a diff over the root: a setting you want everywhere must be written into every profile. This is why the pack ships an overlay (`pack/config/profile-overlay.example.yaml`) that you merge into each profile.

## The gateway

The gateway is the long-running process that connects Hermes to messaging platforms (Telegram in this setup), runs the kanban dispatcher and executes agent turns. It runs as a systemd user service (`hermes-gateway.service`) with lingering enabled, so it survives logout and reboot.

### One gateway for all profiles

Since v0.21 a single gateway can serve every profile on the host (`gateway.multiplex_profiles: true`, set up with `hermes gateway migrate --multiplex`). Kanban workers and `hermes -p <profile> -z` calls are routed through it, and their sessions are named `local-...`.

Pitfall: the Telegram bot token must exist only in the root `.env`. If a profile's `.env` also has `TELEGRAM_BOT_TOKEN`, the gateway "parks" that profile (`profile_parked` in the log) and every worker of that profile exits with rc=1. Comment the token out in all profile `.env` files.

### When configuration is read

- Profile `config.yaml` (model, compression, reasoning effort, toolsets) is read when a session is created and frozen inside that session. A change applies to the next session without a restart, and never to a session already running.
- Kanban dispatcher settings (`kanban.dispatch_in_gateway`, `max_spawn`, `max_in_progress`) are resolved when the gateway starts (`gateway/kanban_watchers.py`, outside the tick loop). Treat a gateway restart as required after changing them. Some code paths re-read the concurrency caps per tick (`_read_concurrency_caps`); do not rely on that without checking your build.
- `max_in_progress: 0` does not stop the dispatcher: zero counts as "not set" (`kanban_watchers_dispatcher.py:64`) and is replaced by a default derived from memory, `clamp(MemTotal / 512 MB, 2, 8)`. To stop dispatch, use the dispatch lock (see below), not the config.

### Restarting

`hermes gateway restart` drains first: it waits up to `agent.restart_drain_timeout` seconds for live turns and, without `-y`, refuses to restart while agent turns are running. Agent turns execute inside the gateway, so a hard restart (`systemctl --user restart hermes-gateway`) kills them, and the interrupted kanban runs end as `crashed` and are retried. The worker client process lives in its own scope and survives the restart; the turn does not. See `docs/operations.md` for the restart procedure.

## The kanban board

### Data model

Each board is a SQLite database with, among others, these tables: `tasks` (one row per card, with `status`, `assignee`, `priority`, `title`, `body`, `started_at`, `workspace_path`, `branch_name`), `task_events` (state changes and their kind), `task_comments`, `task_runs` (one row per worker run) and `task_links` (dependencies). Boards are listed with `hermes kanban boards list`; a project (`hermes project list`, `hermes project show <slug>`) ties folders to a board.

Read board databases from your own scripts **read-only**: `sqlite3.connect("file:<path>?mode=ro", uri=True)`. A plain read-write connect to a mistyped or not-yet-created path silently creates an empty `kanban.db`, and Hermes then sees a board without schema.

### Statuses

| Status | Meaning |
|---|---|
| `triage` | new idea, waiting for the owner's go/no-go (`create --triage`) |
| `todo` | accepted but waiting: unfinished parents, or a dependency block |
| `ready` | can be dispatched now |
| `running` | a worker run holds it |
| `review` | the author asked for review (`kanban_request_review`) and waits for the verdict |
| `blocked` | stopped with a reason (`--kind dependency`, `needs_input`, `transient`) |
| `scheduled` | parked until a time or until someone unblocks it (`hermes kanban schedule <id> <reason>`) |
| `done` | completed |
| `archived` | removed from the active board |

### Links, parents and children

`hermes kanban link <parent> <child>` makes the child depend on the parent: the child cannot start (and cannot complete) while the parent is open, and the platform promotes it by itself when the parent closes. In the data, `task.parents` are the tasks a card depends on (upstream). Decomposition inverts the usual intuition: a decomposed root depends on all of its children, so the root closes only after them.

Two blocking kinds are easy to confuse:

- waiting for **another card** of the board: use a link, not a block;
- waiting for an **external event** (a PR merge, a human answer, a release elsewhere): `block --kind needs_input` with a line "Condition to unblock: ..." that anyone can check.

A `dependency` block on a card whose parents are all finished does not hold it: the dispatcher promotes it within seconds, the worker starts, sees nothing changed, blocks again, and the loop burns runs. In recent versions a repeated `dependency` block with no open parent is converted to `needs_input` automatically, but choose the right kind yourself. A card created with `--initial-status blocked` is only released by hand.

### Decompose

`hermes kanban decompose <id>` hands a triage card to the orchestrator profile (`kanban.orchestrator_profile`, `architect` here), which writes self-contained child cards and links them. With `kanban.auto_decompose: true` the dispatcher decomposes accepted roots by itself, at most `auto_decompose_per_tick` per tick. Hermes has no review step between decomposition and dispatch; the factory adds one (`plan-review-gate.py`, see the extensions catalog).

### The dispatcher

The dispatcher runs inside the gateway (`kanban.dispatch_in_gateway: true`) and ticks every `dispatch_interval_seconds`. On each tick, per board, it:

1. takes the board's dispatch lock, an exclusive `flock` on `<db>.dispatch.lock`; if another process holds it, the tick is recorded as `skipped_locked` and nothing is spawned;
2. picks `ready` cards by priority, up to `kanban.max_in_progress` running cards in total and `max_in_progress_per_profile` per profile;
3. skips cards with no assignee (`skipped_unassigned`) or with an assignee that is not a spawnable profile (`skipped_nonspawnable`); such cards sit in `ready` forever unless something notices;
4. claims the card (status `running`, event, `kanban_task_claimed` plugin hook) and spawns a worker run of the assignee's profile in the card's workspace.

`kanban.failure_limit` caps how many failed runs a card gets before it stops being retried. Dead workers are reaped (`reaped N zombie worker(s)` in the log).

The dispatch lock is the cleanest way to pause the factory without stopping the gateway: hold the lock and running workers finish, the chat keeps working, nothing new starts. When the holder dies, the kernel releases the lock and dispatch resumes on the next tick. The original setup wraps this in a small hold script.

### Worker runs

A worker run is one agent session, usually a single long turn, of the assignee profile. It sees the card through kanban tools:

- `kanban_show`, `kanban_comment`, `kanban_heartbeat` (progress notes; `kanban.diagnostics.stranded_threshold_seconds` flags runs that stop reporting);
- `kanban_create`, `kanban_link` to create follow-up work;
- `kanban_block` with a kind;
- `kanban_request_review` (name the reviewer explicitly, otherwise the author reviews itself) and `kanban_request_changes` (the reviewer's return for rework);
- `kanban_complete`, which returns a receipt with a classification such as `infra`, `stale`, `auth`, `failure` or `pending`.

Run outcomes recorded in `task_runs` include `completed`, `blocked`, `crashed`, `timed_out`, `gave_up` and `rate_limited`. `hermes kanban runs <id>` and `hermes kanban log <id>` show them; `hermes kanban reclaim` takes a card back from a run (and kills that run: there is no pause).

Native safety nets you should know about:

- **Block loop breaker**: a card blocked again for the same reason `BLOCK_RECURRENCE_LIMIT` times (hard-coded 2, `kanban_db.py:115`) moves to triage with `block_loop_detected`. The review lane is explicitly excluded (`kanban_db.py:3408`): returns via `kanban_request_changes` are not counted, and there is no round limit for review.
- **PR completion contract** (`kanban_pr_acceptance.py`): a card whose metadata has a published PR cannot complete without green checks on GitHub. It does not look at the local working tree.
- **Workspaces are preserved on completion.** The scratch directory is cleaned, git worktrees are not; `hermes kanban gc` only prunes events and logs.
- **Background memory review does not run for workers.** The memory nudge fires after `memory.nudge_interval` turns in one session (10), and a worker run is one turn. A worker learns only if it saves the lesson explicitly before `kanban_complete`.

### Diagnostics

`hermes kanban diag` runs built-in detectors (for example `stuck_in_blocked`, `repeated_failures`) per board. They are local: nothing is delivered anywhere unless you run the command. The factory's board watchdog delivers them.

### CLI verbs used by the factory

All take `--board <slug>`. `boards list`, `list [--json]`, `show <id>`, `create` (with `--assignee`, `--priority`, `--idempotency-key`, `--triage`, `--skill`), `edit`, `assign`, `reassign`, `assignees` (shows which profiles are spawnable, "on disk"), `link` / `unlink`, `block <id> <reason> --kind <kind>`, `unblock`, `schedule <id> <reason>`, `comment <id> <text> --author <name>`, `complete`, `archive`, `decompose`, `diag`, `runs`, `log`, `reclaim`, `gc`, `notify-subscribe`. Always pass `--idempotency-key` when a script or webhook creates cards: it is what makes a retried tick harmless.

## Cron jobs

`hermes cron` runs scheduled jobs per profile (`hermes cron list`, `create`, `edit`, `runs <id>`, `doctor` for a delivery preflight). There are two kinds:

- **Script jobs** (`no-agent` mode) execute a script and deliver its stdout. Empty output with exit 0 is a quiet tick. Exit non-zero marks the run failed, so watchdogs should exit 0 even when they report problems. The scheduler calls the script with no arguments (`cron/scheduler_script.py`); the job's prompt field never reaches it, so put modes into a wrapper. In v0.23.1 scripts are resolved only inside `profiles/<profile>/scripts/` (root: `~/.hermes/scripts/`), symlinks out of it are refused, and a job whose script restarts the gateway cannot be created.
- **Agent jobs** run a session with a prompt; a job may also have a script whose stdout is injected into the prompt. Each job stores its own provider, model and optionally `--reasoning-effort`. When you move to another provider, re-pin every agent job with `hermes cron edit <id> --provider <p> --model <m>`; a job pinned to a provider whose key or address is gone falls through to some other route and fails there.

## Webhooks

The gateway accepts webhooks and turns them into agent runs. A subscription (stored in `webhook_subscriptions.json` under the Hermes home) has:

- `events`: the event types to accept (for GitHub, for example `issue_comment` and `pull_request_review`);
- `secret`: the shared secret used to verify the signature;
- `script`: an optional filter. It receives the JSON payload on stdin; a non-zero exit drops the event, and whatever it prints becomes the payload. Hermes does not ship payload logic (who the author is, which PR number), so the filter is where you put it;
- `prompt`: a template with `{dotted.path}` placeholders filled from the payload;
- `skills`, `toolsets`: what the run may use;
- `deliver` and `deliver_extra`: where the run's final text goes (Telegram here).

The factory uses one subscription: the owner's PR reviews and comments wake the default profile, which finds the linked card and turns the verdict into rework cards (see `docs/architecture.md`).

## Telegram delivery and notify subscriptions

Telegram is a gateway platform: the owner chats with the default profile, and cron output and webhook results are delivered to configured targets. Card events reach the owner through **notify subscriptions**:

```bash
hermes kanban --board <board> notify-subscribe <id> \
  --platform telegram --chat-id <telegram-chat> --chat-type dm --delivery-mode notify
```

The gateway notifier (`kanban_watchers_notifier.py`) then delivers that card's events (`completed`, `blocked`, `review`, `crashed`, `timed_out`, `gave_up`). Subscriptions are per card and have no filter by event kind. Cards created from a chat session are anchored to that chat; cards created by cron jobs, scripts and gates are not, and need an explicit subscription if the owner should hear about them. A blanket sweep that subscribes every card turns the chat into noise: the factory tried it on 127 cards and rolled it back.

## Knowledge layers

Hermes gives an agent several places to keep what it knows. They load differently, and putting knowledge in the wrong layer either wastes prompt tokens on every call or makes it invisible.

| Layer | Loaded | Use it for |
|---|---|---|
| `SOUL.md` of the profile | always, in the system prompt | identity and behaviour: who you are, what you read first, which skill to open when, a short list of hard prohibitions |
| skills (`SKILL.md` plus `references/`) | on demand via the skill tool; listed skills via `skills.auto_load` are put into the system prompt | procedures: how to review, how to deliver a PR, how to debug |
| memory | always, small | short durable facts about the user and environment |
| project context file (`AGENTS.md` and friends) | always, if discovered | project conventions |

Details:

- **Skills.** Built-in and hub skills live in `~/.hermes/skills/`. `skills.external_dirs` adds directories shared by all profiles; the factory keeps its own skills in `~/.hermes/shared-skills/` this way, so a skill written for one role is available to all. `skills.disabled` hides skills you do not want offered, and `skills.auto_load` injects named skills into every session's system prompt (use it sparingly: it costs tokens on every call). `hermes skills list` shows what a profile sees.
- **Memory.** `memory.memory_char_limit` (2200 characters upstream) and `memory.user_char_limit` (1375) cap the two memory stores. Keep project facts out of memory; they belong in project context or a skill.
- **Project context files.** The prompt builder walks from the git root to the working directory and loads **only the first context file it finds** (`agent/prompt_builder.py:1726`). A kanban workspace whose repository sits in a subdirectory (for example `workspaces/t_x/repo/`) therefore never sees the repository's `AGENTS.md`. Two native fixes: a symlink to the conventions file at the `workspaces/` level, or a conventions skill listed in `skills.auto_load`. `hermes prompt-size` (if available in your version) shows the `context:` bytes actually loaded; `0 B` means nothing was found.

## Context compression

When a session's context passes `compression.threshold` (a fraction of the model's context window), Hermes summarises older turns with the auxiliary compression model (`auxiliary.compression.provider`, `model`, `reasoning_effort`). Other knobs: `target_ratio` (how small the summary should be), `protect_last_n` (recent messages kept verbatim), `proactive_prune_tokens`.

The threshold is relative, so the same value means very different absolute sizes on different models. With a 1.3M-token window, a threshold of 0.5 never fired, and sessions dragged 200k tokens of context through every call. The factory runs at 0.08; see `docs/config-and-quota.md`. Run the compression model at low reasoning effort: at the default it spent minutes per compression for a summary of the same size.

Caveat for accounting: after compression `state.db` can store the same tool call several times under one `tool_call_id`. Deduplicate before you sum tool time or count calls.

## Reasoning effort

Reasoning models spend most of their time thinking, and the depth is a setting:

- `agent.reasoning_effort` in the profile sets the session default (`low`, `medium`, `high`, `max`; providers map unknown levels such as `xhigh` to their maximum);
- `agent.reasoning_overrides` sets a level per model and wins over `reasoning_effort` when that model is used. A shared override such as `cheap-model: low` silently overrides a profile that wanted `high` on that model, so set the override in that profile too;
- `delegation.reasoning_effort` applies to delegated subagents, and `hermes cron edit --reasoning-effort` to a cron job;
- `auxiliary.<task>.reasoning_effort` applies to helper calls such as compression.

`hermes config set` may warn that a key is "not a recognized config key" for these; the key is still read. Verify the level reaches the API by comparing `reasoning_tokens` in `state.db` across levels.

## The skill curator

The curator is a periodic background job that reviews skills: it marks unused ones stale, archives them, and can consolidate overlapping skills. Settings: `curator.enabled`, `interval_hours` (168), `min_idle_hours`, `stale_after_days`, `archive_after_days`, `consolidate`, `backup.enabled` and `backup.keep`. `hermes curator status` shows the last run and its result.

The curator and the skill usage telemetry (`.usage.json`) only see `~/.hermes/skills/`. Skills loaded through `skills.external_dirs` are invisible to both: they are never archived, and their adoption has to be measured from session transcripts.

## Tool loading

`tools.tool_search` enables progressive tool loading: the model first searches and describes tools before calling them. That saves prompt space for profiles with many rarely used tools, but costs turns. The factory turns it off (`enabled: 'off'`) for architect, coder and tester, whose core tool (an LSP code-navigation MCP server) is called constantly. Limit large MCP servers with the per-server `tools.include` list under `mcp_servers` instead, so their schemas do not ride along with every request.
