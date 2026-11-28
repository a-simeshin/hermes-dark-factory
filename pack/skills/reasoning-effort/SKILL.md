---
name: reasoning-effort
description: Use when picking reasoning_effort or profiling slow sessions
version: 1.1.0
author: maintainer, Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [model, speed, cron, delegation]
---

# reasoning_effort: speed against depth

This skill covers choosing the reasoning level for a profile, cron job or
delegate, where it is configured, diagnosing a slow session, and proving the
level reaches the provider's API.

## When to Use

You set the reasoning_effort of a cron job (`hermes cron edit
--reasoning-effort`) or a delegate, pick a level for a task, profile a slow
session, or check whether the level reaches the API.

A measurement on one thinking model, one review task, median of three runs:

| level | time | reasoning | answer |
|---|---|---|---|
| max | 98 s | ~5000 tokens | same length |
| high | 66 s | ~3000 | same length |
| low | 10 s | 0 | same length |

Session time goes almost entirely into generation (about 60 tokens/s).
Before tuning, 70-88% of worker output was reasoning; moving the default from
max to high cut a typical answer from about 30 minutes to about 5.

## Where the level is set

1. `agent.reasoning_effort` of the profile: the session default.
2. `reasoning_overrides` per model in the profile (for example a cheap model
   pinned to low). An override for a model wins over the profile default, so a
   cheap model that must reason harder needs its own override entry.
3. A cron job's own `reasoning_effort` (`hermes cron edit`) and the
   delegation settings in config.yaml. If `delegation.model` and
   `delegation.provider` are empty, a delegate inherits the parent's model and
   runs with `delegation.reasoning_effort`; a per-model override does NOT
   apply to delegates until a delegate actually runs on that model.
4. Accepted values depend on the provider plugin and the model: some map
   `xhigh` and `ultra` to `max`, and a model may not know every level (on some
   models `medium` behaves close to `low`). Check what the plugin actually
   sends before relying on a level.
5. Auxiliary tasks (context compression, titles, vision) have their own
   `auxiliary.<task>.reasoning_effort`. Compression is mechanical: on `low`
   it is several times faster than on `max` with a summary of the same size,
   and on a thinking model left at `max` it can eat a large share of session
   time.
6. **[RE-01]** Starting layout: default medium, coder high, reviewer and architect max,
   tester and ops low.

## Choosing the level

- low: mechanics without agentic judgement: cron jobs that collect numbers,
  renames, report formatting, a rebase without conflicts. Bounded subtasks
  (one section of a page, one function, text parsing) would fit low, but a
  delegate inherits the parent's model and runs with
  `delegation.reasoning_effort` (see point 3): the delegation config sets the
  level, not this list.
- high: writing code.
- max: review, root-cause analysis, design.

## Diagnosing a long session

A session runs longer than 15 minutes: compare output_tokens and
reasoning_tokens in the session's state database against total tool time. Do
not guess. If reasoning eats most of the tokens and the task is mechanical,
the level is too high.

## Checking that the level actually reaches the API

The config can stay silent. In one case a provider plugin did not pass the
level for a newer model version, and `low` in a profile's config did nothing
for a month.

- read how the provider plugin builds its API kwargs (for example a
  `build_api_kwargs_extras` hook) and whether it passes reasoning_effort for
  your model;
- or call the API directly and check
  `usage.completion_tokens_details.reasoning_tokens`: on low a model that
  supports it should report 0 or close to it.
