# kanban-artifact-guard

This plugin catches cards that leave work without committing it. When a card completes, blocks, or is claimed again, the plugin checks the relevant git trees for uncommitted changes. If it finds any, it saves the diff as a patch, comments on the card, and creates a high-priority catch-up card "Commit the work of card t_...".

## Why

An agent's report is its words about the work, not the work. Workers regularly close a card with a confident summary ("fixed the failing tests") while the change is still sitting unsaved in the working tree: no commit, no push, the PR unchanged. Hermes v0.23.1 has PR completion contracts (`kanban_pr_acceptance.py`: a card with a published PR cannot complete without green checks), but those look only at GitHub. They do not see the working tree, and they do not cover cards that never had a PR.

The completion hook cannot undo the completion (it fires after the transaction commits), but it can turn a silent loss into a visible card with a ready patch.

## How it works

- Hooks: `kanban_task_completed`, `kanban_task_blocked`, `kanban_task_claimed`. `claimed` is there because a run can die on the turn limit or a timeout, and then neither completed nor blocked happens; the next claim picks up the leftovers.
- What is scanned:
  - always: the card's own workspace, if it is inside a git repository, plus all its worktrees;
  - on completed and blocked only: the shared repositories listed in the plugin settings, and only if the card really ran (`tasks.started_at` is set) and no other card on the board is `running`. Otherwise live edits of another worker would be reported as lost work. When the shared scan is skipped for that reason, the plugin leaves a comment saying so.
- The Hermes home (`$HERMES_HOME` or `~/.hermes`) is never treated as a work tree, so a git-tracked knowledge layer does not trigger it.
- If a dirty tree belongs to another card (its directory name is a card id such as `t_2161519c`), the message names that owner and warns not to push blindly.
- Patches go to `$HERMES_RESCUE_DIR` or `$HERMES_HOME/rescue/<card>-<tree>.patch`.
- The catch-up card has priority 95, the same assignee, and an idempotency key derived from the set of dirty paths, so repeated events over the same tree do not create duplicates.
- All failures are swallowed: the hook must never break the kanban.

## Install

1. Copy the directory to `~/.hermes/plugins/kanban-artifact-guard/`.
2. Configure the shared repositories (the main clones your workers use):

   ```yaml
   plugins:
     entries:
       kanban-artifact-guard:
         repos:
           - ~/projects/<repo>
   ```

3. Restart the gateway with `hermes gateway restart`.

## Test

The test needs the Hermes sources for `hermes_cli.kanban_db`:

```bash
HERMES_AGENT_SRC=~/.hermes/hermes-agent ~/.hermes/hermes-agent/venv/bin/python test_guard.py
```

Adjust the venv path to your installation. The test uses a temporary board and rescue directory; never run it with `HERMES_HOME` pointing at your live directory.
