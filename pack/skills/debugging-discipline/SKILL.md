---
name: debugging-discipline
description: 'Order of investigating a breakage: experiment, log, screenshot, CI.'
version: 2.1.0
author: maintainer, Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [debug, ci, tests, evidence, logs]
    related_skills: [serena-lsp, worker-run-discipline, pr-deliverable]
---

# Investigating a breakage

This skill gives the order in which a worker investigates a red test, red CI
or "nothing happens": read the failed CI log first, look at the failure
screenshot before the code, log at layer boundaries, and test every hypothesis
with a control experiment before editing. Every rule here cost real hours;
background: references/protocol.md, one-off techniques: references/lessons.md.

## An order, not a bag of tricks

1. red CI: the command first, hypotheses afterwards;
2. there is a failure screenshot: look at it before the code;
3. "nothing happens": log at the boundaries between layers;
4. every hypothesis is checked by a control experiment before editing;
5. an experiment counts only if the change reached the build and the build
   succeeded.

## Rules with the price of breaking them

- a silent config.yaml edit (a value changed with no trace in sessions):
  the write has no audit in the CLI or in the dashboard's orchestration PUT.
  Evidence: the file's mtime plus `backups/config/config.yaml.good.<stamp>`
  (the copy is taken on a fresh load, copy2 keeps the source mtime, new
  copies appear only when the content changes). Then rule out writers one
  by one: state.db messages (`tool_calls LIKE`), `profiles/*/state.db`,
  journalctl for SSH and cron scopes at the second of the write, cron
  scripts. When every automatic writer is ruled out, the guard is the
  `CONFIG:` line gate in `teach-snapshot.sh`: a config change enters
  history only with an owner decision line;
- red CI: `gh run view <run-id> --log-failed` FIRST (run-id from
  `gh pr checks <PR>`). A failure within 20-30 s is build, style or
  configuration: tests did not run and their colour means nothing (one PR lost
  five hours to this). A green neighbouring job is not evidence until you
  checked what it ran;
- a criterion involving CI closes only on a green `gh pr checks <N>` you saw
  yourself;
- a failure screenshot is read with `vision_analyze` BEFORE the code and the
  diff (one screenshot dismissed two hours of hypotheses in a second: the
  request was never sent);
- "nothing happens": one debug line at each layer boundary, one run; the place
  after the last printed line is the break. Remove the lines before
  committing. Explain an empty indicator first: when is it written (a history
  panel that records only completed requests is empty for a hanging one);
- an empty log: first check that the code can log at that level
  (`grep -cE "(LOG|log|logger)\.(debug|trace)" <file>`). Your code is silent:
  go up to a layer that always logs (the framework's request dispatcher at
  DEBUG);
- Hermes log timestamps (for example gateway.log) are the server's local
  time, not UTC: convert before declaring "the log was silent for N hours";
- a failure cause is named only with evidence: list the candidates and for
  each find the log or database record that proves it; an unconfirmed
  candidate is dropped silently. Resetting a card's failure counter without a
  found cause is FORBIDDEN (a reset on a plausible story was followed by
  three new failures; the real cause was a skill id the worker could not
  resolve);
- the same failure signature on one card while its neighbours are healthy
  in the same minutes points at that card's state (skills, claim, workspace,
  bound session), not at the environment: read the card's fields in the
  kanban database first. Kanban worker tracebacks are not in gateway.log but
  in `profiles/<assignee>/logs/managed-worker.log`;
- a `pgrep` check must not find itself: keep the pattern in a variable and
  filter out wrappers;
- "green locally" means the phase CI runs with all its quality gates (static
  analysis, coverage, style, lint) on the touched modules, NOT a build with
  tests skipped. Suppressing an analyser, lowering a coverage threshold,
  widening excludes or marking code as generated is FORBIDDEN.

## Check

- on red CI, `gh run view <run-id> --log-failed` ran first;
- the hypothesis was checked by a control experiment before editing, both
  conditions confirmed;
- a named failure cause cites the log or database record that proves it;
- the CI phase ran on the touched modules before the push.

## References (read on demand)

- references/protocol.md: the full protocol with failure modes and commands
- references/lessons.md: wait for CI with one command, `workflow run` instead
  of closing a PR, no secrets in debug lines, grep the log directory, control
  experiment first, timeout tests, ERROR is not a failure, the INTENT rule and
  more
