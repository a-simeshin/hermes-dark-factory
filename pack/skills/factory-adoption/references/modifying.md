# Changing a mechanism of the pack

A change to a script, plugin, gate, cron job or skill follows these rules in
order. Each one exists because its absence cost real work; the reasons are in
`docs/lessons-learned.md` and `docs/extensions-catalog.md`.

## 1. Native first, or a live card

Before building anything, check whether Hermes already does it in the
installed version and in the latest upstream: docs, `--help`, code, release
notes. Write down which native feature you checked and why it is not enough.
For a card generator the check is mechanical: call
`board_native_guard.native_or_live` and put its receipt in the card body;
`test_board_native_guard.py` fails the suite when a generator skips it.

## 2. Extend, do not add a parallel mechanism

If a script already owns the area (rework gates in `board-watchdog.py`, PR
state in `pr-hygiene.py`, live runs in `worker-api-watchdog.py`, parking with
`kanban schedule` and `SCHEDULED_UNTIL=<UTC>`), extend it. A second lane next
to a native one (review, respawn, dependency waits) races it: duplicate
reviews, duplicate rebases, extra worker runs.

## 3. Project values in factory.env

No repository, branch, login, path, model, price or time zone in code. Read
it with `cfg("FACTORY_...")` (required) or `cfg("FACTORY_...", default)`;
provider-specific values get no default, and the feature reports itself off
while the key is unset. Add the key with a comment to
`pack/scripts/factory.env.example`.

## 4. Never patch Hermes core

Plugins (hooks such as `kanban_task_claimed`), user plugin overrides in
`~/.hermes/plugins/`, config keys, cron jobs and skills are the extension
points. A core patch does not survive `hermes update`. A plugin must never
raise into the dispatcher, and must log its own failure at warning level.

## 5. A standalone test, red by an in-test mutant

- File `pack/scripts/test_<area>.py` (or `test_guard.py` in a plugin), runnable
  alone with `python3 test_<area>.py`.
- Temporary directories only, with `HERMES_HOME` pointing at a temp home; fake
  `hermes` and `gh` runners instead of real ones; no network.
- Prove the test can fail inside the test itself: apply a mutant (revert the
  fix line, restore the old regex) to the loaded module, assert the scenario
  turns red, restore. `test_board_watchdog_executor_anchor_cover.py` and
  `run_peak_mutants.py` show the pattern.
- Never prove red through git history (after the commit the committed code
  equals the current code) and never by re-running the test's own file.
- A waiting case for every cleanup rule: what it must never touch (cards with
  open links, owner-parked cards), and how it is rolled back.

## 6. Full suite, quoted

`python3 pack/scripts/run-tests.py --audit` (or the installed copy). Report the
final `TEST TOTAL scripts/: N/M green` line of your own run, verbatim. Run each
plugin's `test_guard.py` when the change touches a plugin.

## 7. Record it

- `pack/scripts/README.md`: the table row of the script.
- `docs/extensions-catalog.md`: What, Native feature checked, Why not enough,
  Where, Coupling. A removed mechanism moves to "Removed or merged" with the
  reason.
- A dry-run flag for any new action that archives, closes, comments or
  rewrites config.

## 8. Teaching instead of editing

When the owner corrects agent behaviour, the change goes through the agent's
own channel (`docs/teaching-hermes.md`): facts, expected outcome, the agent
chooses the layer, the owner verifies the snapshot diff of `teach-snapshot.sh`.
Do not edit another agent's skills or SOUL directly except in an emergency,
and say afterwards what you changed. Check a requested rule against recorded
owner decisions first; on conflict, stop and ask.
