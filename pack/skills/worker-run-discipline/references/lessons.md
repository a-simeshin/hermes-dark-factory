# worker-run-discipline: lessons

Distilled lessons from worker runs on a multi-profile board, each as rule,
cause and application. They cover process hygiene, worktrees, recovery after
a lost run, verifying content after a rebase, delegation and duplicate or
zombie runs.

**`pkill -f` kills its own shell.** Why: the pattern appears in the argv of the bash running the command, so the shell died before a `git commit` in the same command ran. Apply: write `pkill -f "server[.]port=6390"`; the bracket form does not match its own command line.

**"already used by worktree" is garbage, not a blocker.** Why: a card sat blocked for two days because a worktree of a long-finished card still held the branch. Apply: `git worktree list`, check the old card with `hermes kanban show`; if done/archived, `git worktree remove --force <path>` (or `git worktree prune`) and retry. Block only if the old card is still live.

**Mass failures mean a broken environment.** Why: four "protocol violation" crashes were really HTTP 401 on every call after a spend guard swapped the provider but kept the old base URL and key; a profile on another provider kept working. Apply: `hermes -z ping`, `hermes config show | grep -A4 Model`, `hermes kanban diag` before analysing the task.

**A run timeout does not destroy committed work.** Why: a second run found the previous run's worktree from its log and closed the card without redoing edits. Apply: take the commit hash and branch from heartbeat notes and `worker_context`, find the worktree (the card's scratch workspace is empty), read the previous run's log under the board's logs directory, compare `git log`, do only the remainder.

**Edit in your own worktree, never in the shared checkout.** Why: uncommitted files left on the base branch of the shared checkout look like lost work to every other worker and automation. Apply: `git worktree add .worktrees/<branch> -b hermes/<branch> origin/<base>`; before the first edit check that `git rev-parse --show-toplevel` contains `.worktrees/`.

**Commit as you go and rerun local gates after every commit.** Why: a run reached green tests and ran out of budget before committing; another passed a local gate, a second commit added a file the gate rejects and CI failed a full round later. Apply: commit each green, coherent piece with a real message; push every few commits; `git status --porcelain` empty at exit (untracked files do not show in `git diff`); put `git log <base>..HEAD --oneline` in the handoff.

**Record the work before the checkpoint, never vanish silently.** Why: several cards exhausted the budget with a dozen uncommitted files and the retry started elsewhere. Apply: at the budget checkpoint commit normally (no `--no-verify`); a `worktree` or `dir:` workspace survives the run's death (only `scratch` is ephemeral), so the next run continues in the same tree.

**A "commit this work" card: first find out whose work it is.** Why: automation once flagged a worktree that a live worker of another card was editing at that moment. Apply: check `git branch --show-current`, `hermes kanban list --status running` and the substance of `git diff`; if any of the three does not match, do not commit, write down whose tree it is and block. Committing someone's unfinished work is irreversible.

**A burnt budget repeats in the same place.** Why: a retry with the same body burns the budget at the same step. Apply: after the first gave_up on "Iteration budget", record what did not fit and block needs_input yourself; a watchdog should block after two in a row, a completed run in between resets the series, a manual unblock does not.

**Mutations and background runs are serialised per worktree.** Why: a mutation applied during background test runs leaked into those runs through shared build output directories, and colliding output directories produced an empty unit-test log. Apply: check `process_manage list` before mutating; measure flakiness and mutations in separate windows; use a mutation form that passes the build's formatters.

**Do not edit module files while a background build of the same project runs.** Why: the build can pick up a half-written file and report a result for code that never existed. Apply: wait for the build to finish before editing, and after the edits a final run is mandatory.

**A failed rebase of a long stack is not replayed from scratch.** Why: replaying more than about ten commits again repeats the same conflicts and burns the budget. Apply: continue from the failure point; if the PR is squash-merged anyway, merge the base into the PR branch once with a single merge commit instead.

**When rebasing, the new base's tests are a reviewed contract too.** Why: resolving a conflict as "branch side entirely" turned tests added by the base red. Apply: a resolution is correct only when both batteries (branch and base) are green live; reconcile with a predicate derived from both sets of assertions, not from taste.

**Verify content both ways after a rebase.** Why: "auto-merge without conflicts" does not mean the branch's content survived. Apply: for each file the branch touched, diff(old head to new head) must equal diff(merge base to new base); for files both sides changed, compare the multiset of lines the branch added with the new head (zero lost lines); `git log -S` on a conflicting line finds which base change really caused it.

**Compile the touched modules before every push to a PR branch.** Why: a rework went to review without compiling tests, the reviewer returned it for a compilation error, and a minute of local compiling cost two extra rounds. Apply: compile code for code changes, compile tests for test changes; a CI failure that starts with a compilation error is almost always a skipped local compile.

**Write what you learned on the way into the card.** Why: an edit can be redone in twenty minutes, an hour of investigation dies with the run. Apply: one or two lines, what you checked, what you got, the conclusion, before continuing.

**Delegate independent pieces, keep the responsibility.** Why: `delegate_task` runs up to three children at once with a time limit each. Apply: delegate when there are three or more pieces that do not touch the same files; not for one file, sequential work or fewer than three pieces; brief a child like a card (what, where, how to prove it); final verification, build and `kanban_request_review` stay with you.

**A test-only rework proves itself red on the base.** Why: there is no production line to mutate. Apply: in a detached worktree of the base inside the card's workspace (not a cache directory a cleaner may wipe), copy only the test file, run it and see it fail on the first expectation of the feature; filtered runs can be "green with 0 tests", so check the count; reinstall the branch's artifacts afterwards if the base run overwrote a shared local cache.

**Background processes get a lifetime at start.** Why: the textual rule "stop your stands before the summary" was broken by the very session devoted to it, and a forgotten server lived for 18 hours. Apply: start stands, builds and test servers only under a TTL wrapper that records owner and deadline (for example `run-bounded.sh --owner card:<id> --ttl-min N -- <command>`), and let a health cron kill unregistered long-lived processes.

**Woke up on a card in review/done: exit at once.** Why: a duplicate run spawned a minute after handoff recognised itself as a duplicate yet spent its whole budget. Apply: first step, `hermes kanban show <id>`; if review/done/archived and you are not the reviewer called for a verdict, comment "duplicate run, card in status X" and finish.

**Your run is timed_out but the process lives: you are a zombie.** Why: a replacement run was spawned and two reviewers fought over the same card's ports, worktrees and verdict for an hour. Apply: signs are "unknown id or not running" from `kanban_show`/`kanban_heartbeat`, outcome timed_out for your run id, or a newer running run; then publish no verdict, record finished checks with artifact paths, clean up only your own processes (check cwd and ppid) and exit.
