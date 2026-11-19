# kanban-admission-guard

This plugin closes the window between two runs of the admission gate. When the dispatcher is about to hand a card to a worker, the plugin checks whether the card (or any of its ancestors) references an issue or PR the owner has approved for work. If none of its references is approved, the card is taken off the queue with the native `hermes kanban schedule`, before the run spends anything.

## Why

The admission gate of the board watchdog decides which cards may be worked: only cards linked to an approved issue (for example, one in the active milestone) or to an open PR against such an issue. The gate runs on a schedule. Between two runs the platform can promote a card on its own (a dependency is released when its parent closes), and the dispatcher can claim it within minutes. The scheduled gate sees the leak only after a worker has already been spawned on unapproved work.

## How it works

- Hook: `kanban_task_claimed`. It fires in the dispatcher process right before a worker is spawned.
- It reads the admission cache `$HERMES_HOME/kanban/boards/<board>/admission-approved.json`, which the admission gate rewrites on every run:

  ```json
  {"updated": "YYYY-MM-DD HH:MM:SS", "issues": {"<N>": "<milestone>"}, "prs": [<N>]}
  ```

- It collects `#N` references from the title, body and comments of the card and of all its ancestors in `task_links` (read-only connection).
- No reference approved: `hermes kanban --board <board> schedule <id> "admission gate: admission claim-guard: ..."`. The marker is the same one the gate uses, so the gate's own return logic handles the card afterwards.
- Fail-open in every direction: no cache, a cache older than 3 hours, a read error, or a card without any references means the guard does nothing and the scheduled gate decides. Cards in `running`, `done`, `archived`, `blocked` or `scheduled` are never touched.

## Install

1. Copy the directory to `~/.hermes/plugins/kanban-admission-guard/`.
2. Make sure your admission gate writes the cache file above. Without it the plugin is a no-op.
3. Restart the gateway with `hermes gateway restart` (the dispatcher runs inside the gateway and loads plugins at start).

## Test

```bash
python3 test_guard.py
```

The test builds a throw-away SQLite board and a fake `hermes` runner and does not touch your live data.
