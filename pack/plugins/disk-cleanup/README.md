# disk-cleanup (local override)

This is a copy of the upstream `disk-cleanup` plugin that ships with Hermes (v2.0.1), with one change: it never auto-deletes files under `$HERMES_HOME/scripts/`.

## Why

The bundled plugin tracks files that look ephemeral (`test_*`, `tmp_*`, cron output) created during a session, and deletes them on `on_session_end`. That is right for scratch files, but the factory keeps its watchdog regression tests as `scripts/test_*.py`. The bundled plugin deleted those tests as session scratch more than once, silently removing the safety net for the gates.

Hermes loads a user plugin from `~/.hermes/plugins/` with priority over the bundled plugin of the same name, so the override is wired with a native mechanism and no core edit.

## What changed

In `disk_cleanup.py`, the list of protected top-level directories gains `"scripts"` (search for the comment that starts with `scripts/ holds persistent infrastructure`). Everything else is upstream code.

## Hooks

- `post_tool_call`: tracks newly created test/temp paths under `HERMES_HOME`.
- `on_session_end`: runs the quick cleanup when something was tracked in the turn.
- `/disk-cleanup` slash command: `status`, `dry-run`, `quick`, `deep`, `track`, `forget`.

## Install

1. Copy the directory to `~/.hermes/plugins/disk-cleanup/`.
2. Restart the gateway.
3. After every Hermes upgrade, diff this copy against the bundled plugin and re-apply the one-line change to the new upstream version, otherwise you keep an outdated fork.

If you keep persistent tests somewhere else, add that directory to the same set instead of (or in addition to) `scripts`.
