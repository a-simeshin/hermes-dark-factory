# Config and quota

This document explains the configuration overlay that roughly halved the factory's daily model spend and cut spend per closed card by about 60%. It covers each knob in `pack/config/profile-overlay.example.yaml`: what it does, why the value was chosen and what you give up. It also covers the role split between a cheap and a strong model, reasoning effort per role, and the peak-hour dispatch pause for providers with peak pricing. All keys are native Hermes v0.23.1 keys; only the values differ from the defaults.

## Where the tokens went

Before the overlay, one coder session on a rework card made about 200 model calls with an average context of 211k tokens, about 42 million tokens in one session, most of it re-read from the cache. The whole installation burned 300-500 million tokens a day and exhausted a weekly subscription limit in two and a half days. Three causes dominated:

1. **Context never shrank.** The compression threshold is a fraction of the context window. On a model with a 1.3M window the default-ish 0.5 meant compression at about 655k tokens, so it never fired, and every call carried the whole history.
2. **Tool output was kept whole.** Build logs and test reports landed in context at full size and stayed there.
3. **Everything thought at maximum depth.** Reasoning took 70-88% of the output of worker sessions.

Measure your own installation before tuning: count calls, context per call and reasoning tokens per profile from each profile's `state.db`, deduplicating repeated tool calls (after compression the same `tool_call_id` can appear several times).

## The knobs

### `compression.threshold: 0.18`

Compress when the context passes 18% of the window (about 180k tokens on a 1M window). Lowering the threshold from the default was the single biggest saving: every call after it is cheaper.

There is a floor below which a lower threshold stops helping and starts hurting. After a compression a worker still carries its system prompt, skills, tool schemas and the protected tail of recent messages; on the original installation that floor was 75-115k tokens depending on the profile. A threshold of 0.08 (80k) sat below it, so compression ran in a loop: up to 39 times in one run, each pass costing about a minute and more tokens than it saved, until Hermes blocked further compression as ineffective. Put the threshold above the measured floor with a margin. Measure the floor per profile from the context sizes after compression in `profiles/<profile>/logs/managed-worker.log`, and recompute the threshold when you change models: what matters is the absolute size.

Trade-off: summaries lose detail, so a worker may re-read a file it already saw. Keep `protect_last_n` (recent messages kept verbatim) at about 20 so the current step is intact.

### `auxiliary.compression.reasoning_effort: low`

The compression model summarises at low effort. At the default (maximum) one compression took 100-374 seconds and, across three days, 17% of the coder's wall time. A direct measurement on a 22k-token input: 51-77 seconds at default, 17-18 seconds at low, summary of the same size. No trade-off was found.

### `tool_output.max_bytes: 8000`

Tool output kept in context is capped at 8 KB; Hermes writes the full output to a file the agent can open. The agent must then grep or read the part it needs, which is what it should do with a build log anyway.

Trade-off: an agent that does not know about the file may conclude from a truncated tail. The skills tell workers to look for the full output.

### `agent.disabled_toolsets`

Each enabled toolset sends its tool schemas with every request. The factory never uses computer use, delegation, connections, browser-use, text-to-speech or session search, and workers also lose `clarify` (nobody answers a worker's question in the middle of a run; the right move is a `needs_input` block). Limit large MCP servers with their `tools.include` list for the same reason: one server's full tool set cost about 30 KB of schema per request.

Trade-off: none for tools you do not use. Re-enable delegation only if you have measured that a delegate is cheaper than doing the step in place.

### `agent.api_max_retries: 3`

With a provider that answers 429 when concurrency is exhausted, many retries are right (the factory once ran 12). With a quota plan, a 429 usually means "limit exhausted", and 12 retries with backoff only hang the run for minutes. Three retries fail fast and let the health check stop dispatch.

### `agent.budget_warning_ratio: 0.85`

At 85% of `max_turns` the worker is warned to finish and leave a handoff note. This is the native answer to "a run hit the turn cap and lost its progress"; do not build your own.

### `agent.max_turns`

200 for coder, tester and the default profile, 120 for architect, reviewer and ops. Raising the cap looks like spending more, but a run that dies on the cap wastes everything it did. With 120, 18-20% of coder and tester runs died there, while the p90 of successful runs used 91 calls. Pair a higher cap with the budget warning and with splitting large cards (the rework gate sends cards with three or more blocking items to the architect).

### `memory.memory_char_limit: 2200`

Back to the upstream default from a raised 4000. Memory is in every prompt; project facts belong in the conventions skill, loaded only where needed.

### `updates.pre_update_backup: quick`

A full pre-update backup of a multi-gigabyte home is slow; the nightly native backup covers it. Not a quota setting, but part of the same cleanup.

## Cheap model for doers, strong model for judges

| Profile | Model | Effort | Reason |
|---|---|---|---|
| default | strong | medium | owner chat and triage need judgement; medium is close to low on some models |
| architect | strong | max | a plan mistake multiplies across every child card |
| reviewer | strong | high | missed defects reach the owner; high instead of max keeps reviews fast |
| coder | cheap, strong as fallback | high | code is checked by reviewer and tester; depth still matters for writing it |
| tester | cheap | low | commands with verifiable results |
| ops | cheap | low | mechanical operations |
| auxiliary (compression, titles, vision) | cheap | low | helper calls |

On the original provider the cheap model cost about a third of the strong one per input, cached and output token. Moving coder, tester and ops to it is safe as long as the strong reviewer and the owner's review stay in the loop to catch what the cheaper model misses.

Watch for one trap: `agent.reasoning_overrides` wins over `agent.reasoning_effort`. A shared override `<cheap-model>: low` (meant for auxiliary calls) silently overrode the coder's `high`. Set `<cheap-model>: high` in the coder profile's overrides.

Reasoning effort, measured on one review task (median of three runs): maximum 98 seconds and about 5000 thinking tokens, high 66 seconds and about 3000, low 10 seconds and none, with answers of the same length. Choose low for mechanics, high for writing code, maximum for review, root-cause analysis and design, then check the real effect in `reasoning_tokens`.

## Peak-hour dispatch pause

Some subscription providers charge more during peak hours (the original one billed double on weekday mornings). The factory stops dispatching new cards in that window and lets running cards finish:

- an ops cron job runs every 15 minutes and calls `kanban-dispatch-peak-pause.py tick`;
- inside the window it holds the boards' dispatch locks (the same mechanism as the owner's manual stop); outside it releases only a hold that it owns;
- an owner stop always wins: the pause never overwrites or releases it; a quota stop held by the health check gets the peak label so it is not released in the middle of the window.

Trade-off: cards wait. Only use it if your provider's pricing has peaks and your queue tolerates a few hours of delay. Set the window in `factory.env`: `FACTORY_PEAK_TZ` (an IANA zone), `FACTORY_PEAK_START`, `FACTORY_PEAK_END` and optionally `FACTORY_PEAK_DAYS` (every day when unset). The pack ships no window: until the three keys are set, the tick holds nothing and logs which keys are missing.

## Quota guard and spend report

Both are provider-specific, so both are configuration with no built-in values:

- **Quota guard** (`hermes-health.py`). Set `FACTORY_QUOTA_PROBE_BASE`, `FACTORY_QUOTA_PROBE_KEY_VAR` (the name of the key variable in the Hermes `.env`) and `FACTORY_QUOTA_PROBE_MODEL`, plus the codes and texts your provider writes on a 429 when the quota is gone (`FACTORY_QUOTA_CODES`, `FACTORY_QUOTA_TEXTS`). Without the probe settings the guard is skipped with one line on stderr: a stop that no probe can lift would hold dispatch forever.
- **Spend report** (`metrics-evening.py`, `usage-ledger.py`). `FACTORY_PRICES` holds your plan's coefficients as `model=input/cache_read/output;...`. A model without a price is reported as unknown, never as zero: a zero would hide spend.
- **Peak rates in the estimate.** If your provider charges less outside its peak hours, set `FACTORY_OFFPEAK_MULTIPLIER` (for example `0.5`) together with the peak window above. A session only records its start and end, so the multiplier is applied linearly over that interval, and the report labels its credits as an estimate. Unset, the multiplier is 1.0 and every hour costs the same; with an incomplete window it is 1.0 as well.
- **Checking the estimate.** Set `FACTORY_PLAN_WEEKLY_LIMIT` to your plan's weekly credits, and the week section shows the estimate as a share of it (unset: the line is skipped). Next to it the report counts the real quota errors in the agent logs, matched by `FACTORY_QUOTA_CODES` and `FACTORY_QUOTA_TEXTS`. An estimate far above the limit with no quota errors means the price table or the multiplier is wrong, not that you overspent.
- **Attribution.** `usage-ledger.py` ties each session to its card through the board's `owner_admitted` event, so card runs do not land in "other"; the last 14 days of the ledger are rebuilt on every run, so a fix in classification reaches rows already written.

## Measuring the result

Report two numbers together: spend (per day and per closed card) and an invariant that must not drop, such as cards closed per day and the owner's rework rate. In the original installation spend per day fell by roughly half and spend per closed card by about 60%; the per-card number is the one to trust, because it already accounts for throughput. If spend falls together with throughput, you have not optimised anything: work simply stopped.
