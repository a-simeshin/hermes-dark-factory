# Scheduled supervision prompt

Schedule this prompt in the supervising agent (for Claude Code: `/loop 30m` with the text below, or a session cron). Keep the wording stable so every cycle does the same checks.

```
Check the factory on <server> (board <board>: running, ready, blocked, crashed and
timed-out runs of the last cycle; cron jobs: last status; gateway: alive; server:
available memory and process count; provider: quota errors in the logs; open pull
requests: head, mergeability, CI, verdicts, "What changes for you").

Judge every finding against the pack's documents and the owner's recorded decisions
in <decisions file>. A behaviour that breaks the design becomes one teaching round
through the orchestrator's own channel (hermes -z on the server), sent detached with
its output in a file that ends with DONE. Several incidents of the same kind become
one round with a general lesson.

Never edit Hermes, the agents' skills, personas, scripts, plugins or config, the board
database, or the product's code. Verify every claimed change yourself: rerun the named
test, read the knowledge snapshot diff, query the board, open the pull request.

Merge only where the owner delegated it, only the exact head you reviewed, after a
live run with real input for anything a user will see, then watch CI on the base.

Report briefly: what is healthy, what was taught and verified, what waits for the owner.
```

Optional last step, when you maintain a public copy of the pack:

```
Then the pack at <pack repo>: find the last Live-sync trailer, list the live rounds
since that snapshot, port each by mechanism (generic, English, values through
factory.env, removed mechanisms removed, a test with an in-test mutant), run the
touched tests and the full suite, scan for project names, hosts and secrets, commit
with Live-sync: <new hash>, push. Nothing to port: say so.
```
