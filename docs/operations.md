# Operations

This document collects the operating rules for a host that runs the factory: how to restart the gateway without killing agent turns, how to keep an out-of-memory kill from stopping the gateway, how to run heavy commands in their own memory-bounded scope, two traps that blew memory or silently created empty databases, backups with the native `hermes backup` plus an independent check, upgrades and re-pinning cron jobs, how to test against a sandbox instead of the live home, and basic SSH and host hygiene. Each rule comes from an incident on the original installation.

## Restarting the gateway

Agent turns run inside the gateway process. The worker client of a kanban run lives in its own scope and survives a restart; the turn itself does not.

- Use `hermes gateway restart` **without `-y`**. It drains first (up to `agent.restart_drain_timeout` seconds) and refuses to restart while agent turns are live. That refusal is the feature: wait or stop dispatch first.
- Do not use `systemctl --user restart hermes-gateway` while turns are live. It kills them; the interrupted runs end as `crashed`, are retried, and the work since the last commit is lost.
- To empty the factory before a restart, stop dispatch with the dispatch lock (`kanban-dispatch-hold.py start --owner <you> --reason "restart"`), wait for running cards to finish, restart, release the hold. Editing `max_in_progress` does not help: it is read at gateway start, and `0` means "unset".
- Restart after changing kanban dispatcher settings and after installing or updating plugins. Profile settings (model, compression, effort) apply to new sessions without a restart.

## OOM policy for the gateway unit

Every command an agent runs lives in the gateway's cgroup. With systemd's default `OOMPolicy=stop`, one runaway child killed by the OOM killer stops the whole unit, with all live turns. The core does not set an OOM policy on the unit. Add a drop-in:

```ini
# ~/.config/systemd/user/hermes-gateway.service.d/oom.conf
[Service]
OOMPolicy=continue
OOMScoreAdjust=-800
```

```bash
systemctl --user daemon-reload
hermes gateway restart
systemctl --user show hermes-gateway -p OOMPolicy -p OOMScoreAdjust
```

`OOMPolicy=continue` lets the kernel kill only the offending child; `OOMScoreAdjust=-800` makes the gateway itself the last candidate.

## Heavy runs in their own scope

Run tests, watchdogs under development and anything with an unknown memory appetite outside the gateway's cgroup, with a ceiling:

```bash
systemd-run --user --scope -p MemoryMax=1G --collect <command>
```

A leak then fails that command with a memory error inside its own scope, and the gateway is untouched. This is the same mechanism the native process registry uses for workers. Measure a script's peak memory with `resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss` if `/usr/bin/time` is not installed.

## Two traps that look harmless

### Import cycles between sibling scripts

Scripts with dashes in their names (`pr-hygiene.py`, `metrics-evening.py`) cannot be imported normally, so they load each other with `importlib.util.spec_from_file_location` and `exec_module`. Without a shared `sys.modules` key, every load creates a fresh module, and one back edge (A loads B, B loads A) turns into infinite mutual recursion. On the original host this grew to 4 GB in minutes, the OOM killer fired, and with the default OOM policy the gateway stopped.

Rule: register the module under one canonical key (for example `hermes_sibling:<absolute path>`) in `sys.modules` **before** `exec_module`, and look it up first. A re-entrant load then gets the same object. Better still, move shared code into a module with an importable name.

### Empty databases created by read-write connects

`sqlite3.connect(path)` creates the file if it does not exist. A script with a mistyped board slug, or one that runs before a board exists, leaves an empty `kanban.db` behind, and Hermes then finds a board without schema. Scripts that only read Hermes databases (`kanban.db`, `state.db`) must open them read-only:

```python
sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=10)
```

A read-only connect fails loudly on a missing file and never competes for the writer lock. Write to boards only through the `hermes kanban` CLI or the kanban tools.

## Backups

Use the native backup. `hermes backup -o <file>.zip` excludes the vendored source, caches, `node_modules` and `__pycache__`, and `hermes import <file>` restores it. A self-made tar of the same home was more than twice the size and had no documented restore.

Wrap it with a verdict and a check before rotation (the original setup used a wrapper script plus a restore check):

1. write the backup output to a log, not to `/dev/null`;
2. judge the run by the log: files that vanished during archiving (`[Errno 2]`) are normal when cleanups run, any other error fails the run, and a non-zero exit with no such lines is an unknown failure;
3. test the archive (`unzip -tqq`) before deleting anything: a broken backup is worse than none;
4. rotate (one week of archives was enough);
5. schedule it away from the top of the hour, where hourly cleanups run (the original moved from 04:00 to 04:23).

Periodically restore into a sandbox home to prove the archive is usable. Keep a backup off the host as well.

## Upgrades

```bash
hermes update --backup --yes
```

- Before upgrading, walk `docs/extensions-catalog.md` (or your own deviations list) against the release notes and remove whatever upstream now does natively.
- Diff local plugin overrides (such as `disk-cleanup`) against the new bundled version and re-apply the local change.
- After the update restart the gateway (`hermes gateway restart`) and run `hermes doctor`.
- Run the test of every plugin against the new sources (`test_guard.py` in each plugin directory). Plugins call internal modules of Hermes, and an upstream refactoring can break them without any visible error; see "A guard that fails silently is worse than no guard" in [lessons-learned.md](lessons-learned.md).
- The config is not migrated automatically. If `hermes config show` reports an old config version or something behaves oddly, run `hermes config migrate` and diff the result.
- If `hermes doctor` warns about the SQLite WAL-reset bug, the Python build from `uv` is old: `uv self update` and `uv python install <version> --reinstall --no-cache`.
- **Re-pin agent cron jobs after any provider change.** Each agent job stores its own provider and model. A job pinned to a provider you removed falls through to some other route and fails there (one job ended on a paid fallback with an empty balance). For every agent job: `hermes cron edit <id> --provider <provider> --model <model>`, then check `hermes cron runs <id>` after its next run.
- In v0.23.1 cron scripts must live in `profiles/<profile>/scripts/` (root: `~/.hermes/scripts/`); symlinks out of it are refused. After an upgrade that tightens such rules, run `hermes cron list` and look for paused or failing jobs.

## Test against a sandbox, never the live home

Many Hermes tests and your own script tests read `HERMES_HOME` to find their data. A test run with `HERMES_HOME` pointing at the live `~/.hermes` acts on the live boards: on the original host such a run released the owner's dispatch stop.

- Create a throw-away home (`mktemp -d`), copy only what the test needs, and export `HERMES_HOME` to it.
- Never pass the live path into a test harness, even "read-only".
- Run the test inside a memory-bounded scope (see above).
- For anything that touches GitHub, use a scratch repository.

## SSH and host hygiene

- Key-only SSH: disable root login and password authentication, keep the hardening in a drop-in under `/etc/ssh/sshd_config.d/`.
- Run Hermes as an unprivileged user with lingering enabled, so user services survive logout.
- fail2ban for SSH and automatic security updates without automatic reboot.
- Keep secrets in `.env` files and a `secrets/` directory with mode 600, never in `config.yaml`, SOUL files, memory or card comments. Reference them as `${VAR}` in config.
- Expose only what you need: put the dashboard behind a reverse proxy with TLS and authentication, and if the proxy speaks HTTP/3, open UDP 443 too, otherwise browsers that try QUIC hang without an error.
- The Telegram bot token lives only in the root `.env` when the gateway multiplexes profiles.
- Before declaring a failure "out of memory", find the proof (an OOM kill in `dmesg` with a matching timestamp). Failures that repeat with the same duration are retries or timeouts, not resource exhaustion.
