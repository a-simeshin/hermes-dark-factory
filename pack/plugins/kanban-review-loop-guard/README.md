# kanban-review-loop-guard

This plugin stops an endless review loop. When a card is about to be claimed for the third time after the reviewer returned it for rework (`changes_requested`), the plugin blocks the card with `--kind needs_input` and a reason that asks a human to decide.

## Why

Hermes has a native loop breaker: a card that is blocked again for the same reason `BLOCK_RECURRENCE_LIMIT` times (hard-coded to 2 in v0.23.1) moves to triage with a `block_loop_detected` event. The review lane is explicitly excluded from that accounting: `kanban_request_changes` routes a return without block-loop accounting, and the lane has no round limit of its own. A reviewer and a coder that disagree on one finding can therefore cycle forever. In practice we saw chains of 10 to 14 rounds that produced dozens of cards and moved nothing.

Two returns are normal work. The third usually means the finding cannot be closed by the assignee: it needs an owner decision (close it, defer it, or split the card).

## How it works

- Hook: `kanban_task_claimed`. It fires in the dispatcher process right before a worker is spawned, so the guard acts before the run spends any tokens.
- It reads the board database read-only (`mode=ro`) and counts `task_events` rows with `kind = 'changes_requested'` for the card.
- At or above the limit, if the card is not already `done`, `archived` or `blocked`, and it was not escalated before, it calls `hermes kanban --board <board> block <id> "<reason>" --kind needs_input`.
- One escalation per card: it looks for the marker phrase `review loop stopped` in the card comments.
- It writes a one-line proof-of-life file, `$HERMES_HOME/kanban/review-loop-guard.seen`, on every call, so you can confirm the hook is wired without reading logs.
- The hook is best-effort: every exception is swallowed so it can never break the dispatcher.

## Install

1. Copy the directory to `~/.hermes/plugins/kanban-review-loop-guard/` (user plugins are discovered from that directory).
2. Optionally set the threshold (minimum 2, default 3) in `config.yaml`:

   ```yaml
   plugins:
     entries:
       kanban-review-loop-guard:
         limit: 3
   ```

3. Restart the gateway with `hermes gateway restart` (the dispatcher runs inside the gateway and loads plugins at start). If your version requires plugins to be enabled explicitly, check `hermes --help` for the plugin commands.
4. Verify: after the next dispatcher claim, the `.seen` file has a fresh timestamp.

## Test

```bash
python3 test_guard.py
```

The test builds throw-away SQLite boards and does not touch your live data.
