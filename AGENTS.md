# Rules for agents changing this repository

This repository is a public, project-agnostic extension pack for Hermes Agent. These rules apply to any coding agent that edits it.

## Content

- Write everything in English: docs, skills, comments, commit messages.
- Keep it project-agnostic. No project, owner, host or person names from any installation. Project values are read from `factory.env` through `factory_config.py`; placeholders such as `<org>/<repo>` stand in for them in docs.
- No secrets of any kind: tokens, passwords, keys, private hostnames or addresses.
- Never use the em dash character (U+2014). Use a colon, commas, parentheses or a new sentence instead. The `commit-msg` and `pre-commit` hooks in `reference/githooks/` reject it once installed.
- Docs are full readable sentences; skills are concise procedures in the layout of the other `pack/skills/*/SKILL.md` files.

## Code and tests

- Do not patch Hermes core; extend through plugins, config, cron and skills. Check for a native Hermes feature before adding a mechanism.
- Every change to a script or plugin comes with a standalone test (`pack/scripts/test_<area>.py` or a plugin's `test_guard.py`) that runs alone, uses a temporary `HERMES_HOME`, fakes `hermes` and `gh`, and never touches the network or a live board.
- Prove a test can fail with a mutant applied inside the test. Never prove it through git history, and never let a test re-run its own file.
- Run `python3 pack/scripts/run-tests.py --audit` and quote its final `TEST TOTAL scripts/: N/M green` line verbatim in your report. Nothing else counts as green.

## Documentation

- When a mechanism changes, update `pack/scripts/README.md` and `docs/extensions-catalog.md` in the same commit. A new `factory.env` key goes into `pack/scripts/factory.env.example` with a comment that says what happens while it is unset.

## Commits

- Commit messages describe the change only. No AI attribution trailers (`Co-Authored-By` for an AI, generated-with lines, session links).
