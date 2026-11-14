---
name: worker-run-discipline
description: How not to lose work within a single card run.
version: 2.0.0
author: maintainer, Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [worker, git, commit, delegation, diagnostics, budget]
    related_skills: [kanban-discipline, save-lesson-before-complete, issue-mirror]
---

# Run discipline

This skill covers how a worker spends one card run without losing its work:
waiting on conditions instead of sleeping, saving context, keeping heavy
processes out of the gateway, committing early and handing off right after
the push. A run has a hard iteration ceiling (`agent.max_turns`), and
everything not committed disappears with it. Background: references/protocol.md,
one-off lessons: references/lessons.md.

## Wait on a condition, not blindly

Blind sleeps are the largest single waste in worker runs (in one two-week
measurement, almost half of a coder's wall time was waiting). Before every
wait, name the READINESS SIGNAL (a log line, an HTTP code, a file) and wait
for it in a loop "check, pause 5-10 s, check" with a timeout. A bare `sleep N`
is allowed only for N <= 10 s.

## Save context

Every model call carries the whole context again, so cost is the NUMBER OF
CALLS times the context:

- any multi-step reconnaissance (several files or fragments, a grep across
  the tree, PR status plus CI) goes into ONE execute_code: the script calls
  the tools itself (up to 50 per call) and prints only the digest needed for
  the next decision:

        from hermes_tools import terminal, read_file
        pr = terminal("gh pr view 236 --json title,mergeable --jq '.title, .mergeable'")
        checks = terminal("gh pr checks 236 | tail -5")
        frag = read_file("src/handler.py", offset=120, limit=40)
        print(pr, checks, frag["content"][:500])  # only the digest enters context

- read a skill by section (map with `grep -n '^#' SKILL.md`, then the block
  you need), not whole;
- CI logs, diffs, test output: grep/tail the relevant part, never whole;
- the budget is finite; near its end the runtime injects a completion
  checkpoint into a fresh tool result (kanban docs, "Completion checkpoints
  before the iteration cap"; threshold `agent.budget_warning_ratio`);
- the checkpoint is an order, NOT "one more step": immediately
  `kanban_comment` "done / left" (branch, `git log -1`, `git status
  --porcelain`), then a terminal call (request_review / complete / block).
  After the ceiling the runtime cuts the tools and the report stays as chat
  text the card never sees;
- the card does not converge and context is heavy: stop; a fresh session from
  the written notes is cheaper than topping up this one.

## Heavy runs outside the gateway's cgroup

Tests and heavy checks with unknown memory appetite run in their own scope
with a memory ceiling, not inside the gateway service's cgroup:

    systemd-run --user --scope -p MemoryMax=1G --collect <command>

Why: agent commands live in the gateway's cgroup; a leak there grew to
several GB, the OOM killer fired and the unit's OOM policy stopped the whole
gateway with every live turn. With MemoryMax the run dies with a MemoryError
in its own scope. Measure peak RSS with
`resource.getrusage(RUSAGE_CHILDREN).ru_maxrss` if `/usr/bin/time` is missing.

## Do not burn the budget on waiting and full builds

- CI: `gh run watch <id> --exit-status`; build: a loop on a condition taken
  from real output, with a timeout;
- inside a card, CI is checked with one snapshot `gh pr checks <N>`; waiting
  for green is the next run's job;
- background build: `terminal(background=true)` plus ONE process_manage wait
  with headroom; meanwhile do useful steps, not polling;
- build and test only the touched module and tests; on a worktree stacked
  over a PR also build its in-repo dependencies, or you test stale
  artifacts. Sign of a stale run: the target test class is absent from
  stdout while the build says success;
- one full build (the phase CI runs) right before the push; never repeat a
  full build without changes in between; one background build per worker;
- write build output to a file: `> log 2>&1; echo EXIT=$?; grep '<success
  line>' log` (a pipe into tail returns tail's code; head in a pipe kills the
  build with SIGPIPE); trust the test counts in stdout of the current command,
  not an old report file;
- in a remote worktree use the terminal's `workdir`, not a repeated `cd`;
  from execute_code put an explicit `cd <worktree> &&` in the command itself
  and check the first log lines for the right paths.

## Git mechanics of a run

- commit as you go, not at the end; at the budget checkpoint the work is
  already committed;
- as soon as a commit is pushed to the PR branch, the NEXT action is the
  handoff: `kanban_complete` / `kanban_request_review`; cosmetics later;
- a pre-commit hook that compares the index with the branch HEAD can see
  lines restored from the base after a rebase as NEW violations: build the
  final commit from the merge base (stash, reset --hard origin/<base>,
  checkout old-head -- ., stash pop, commit) instead of bypassing the hook;
- rebasing onto a base that renamed a symbol: auto-merge leaves stale
  references in the branch's own lines of NON-conflicting files: grep the
  whole repo for the old name (leave same-named members of other types alone)
  and compile the tests of every touched module;
- edit in your own worktree, never in the shared checkout; a card that hit
  the iteration ceiling is a defective card: block needs_input with a
  proposed split;
- before a knowledge snapshot of `~/.hermes` that does `add -A`, look at
  `git status --porcelain`: another session's WIP in the tree means leave
  your files uncommitted and let the round's owner session snapshot.

## Report and communication

Reference report (machine-checkable, not "everything works"):

    Done: resetError() before the retry call. Branch hermes/fix-retry, PR #52.
    git log <base>..hermes/fix-retry --oneline: a1b2c3 fix(ui): reset error state
    Touched: ToolCallPanel.tsx (+7/-2). Tests: <module test command>: 34 OK.
    git status --porcelain: empty.

- on someone else's card (assignee is not you) refer to a PR by NUMBER
  without a URL: a PR URL in a fresh comment freezes a ready card under the
  dispatcher's active-PR guard for 24 hours;
- many neighbouring cards failing: start with the environment, not the task;
  woke up on a card in review/done: exit at once; your run was marked
  timed_out but the process lives: you are a zombie, stop working.

## Check

- `git status --porcelain` is empty at handoff;
- the report has a non-empty `git log <base>..HEAD --oneline`;
- the budget checkpoint was answered with a terminal call, not "one more
  step";
- on mass failures the analysis started with the environment.

## References (read on demand)

- references/protocol.md: the full protocol with numbers and failure modes
- references/lessons.md: pkill -f kills its own shell, worktree hygiene,
  recovery after a run timeout, WIP at the checkpoint, mutations and
  background runs are serialised, two-way content check after a rebase,
  zombie runs
