# worker-run-discipline: full protocol

This reference expands the worker-run-discipline skill with the measurements
and failure modes behind each rule: how context cost grows with the number of
calls, how the iteration ceiling and the completion checkpoint behave, why
waiting and full builds eat the budget, and what a machine-checkable report
looks like. A run has a hard iteration ceiling, and anything not recorded
disappears with it; every rule below comes from a case where work was lost.

## When to use

You start work on a card, plan the order of steps, approach the budget, or see
strange failures.

## Saving context: batches, skills by section, the checkpoint

Every model call carries the whole accumulated context again. In one analysed
session, 140 single-line grep/sed calls in a 200-call session cost about 42
million tokens at an average context of about 200 thousand tokens per call.
Therefore:

- explore in batches: one execute_code (or one terminal call) with several
  greps and only the needed lines printed, instead of ten separate calls;
- read a skill by section (map with `grep -n '^#' SKILL.md`, then the block),
  not whole;
- never dump CI logs, large diffs or test output whole: grep/tail the relevant
  place;
- the iteration budget is finite (`agent.max_turns` per profile). Near the end
  the runtime injects a completion checkpoint into a fresh tool result (kanban
  docs, "Completion checkpoints before the iteration cap"), at the ratio set by
  `agent.budget_warning_ratio`. Failure mode: several coder sessions died on
  the hard ceiling without a terminal call; another card's three runs read the
  rule via skill_view and never executed it (zero terminal calls in more than
  two hundred tool calls). A rule read AFTER entering a long build loop does
  not fire. The checkpoint itself is automatic now, but reacting to it is the
  worker's job;
- the checkpoint is an order, not a hint: do NOT take "one more step".
  Immediately `kanban_comment` with "done / left" (branch, head from `git log
  -1`, `git status --porcelain`), then a terminal call (`kanban_request_review`,
  `kanban_complete` or `kanban_block`). After the last allowed call the
  runtime answers "You've reached the maximum number of tool-calling
  iterations" and cuts the tools: the final report stays as chat text, the card
  never sees it, and the next run does the work again;
- the card does not converge and the context is already heavy: stop at the
  threshold. A fresh session working from the "done / left" note is cheaper
  than topping up an overgrown one.

## Check the environment first, then blame the task

When several neighbouring cards fail at once, the environment is broken, not
the task. See lessons.md for the three-command quick check.

## Hand off right after the push, not after polishing

As soon as a commit is pushed to the PR branch, the NEXT action is the handoff:

- independent verification needed (almost always for code):
  `kanban_request_review` with a summary of what was done and how it is proven,
  and always with the `reviewer` parameter. The card moves to the review lane,
  the reviewer picks it up, and its request_changes returns it to you with the
  history of rounds. Do NOT create a separate review card: this is the built-in
  loop, cheaper and without extra cards;
- no review needed: `kanban_complete` (or `kanban_block` if stuck).

Not after finishing the report, not after a final run for a nicer number, not
after cleaning branches. On one board, successful runs averaged 14 minutes
while runs marked timed_out averaged 32: in those runs the work was usually
done and pushed, only the terminal call did not fit. The card returns to the
queue, spends a second run on what is ready, and the owner sees a red outcome
instead of a green one. Time left after the handoff: keep improving; a repeated
call breaks nothing, a lost one cannot be recovered.

## A pre-commit hook may diff against the branch HEAD, not the merge base

If a pre-commit hook rejects some pattern in new lines (for example a
forbidden character) and compares the index with HEAD, restoring lines from
the base after a rebase or squash looks to the hook like NEW violations,
because the branch HEAD already contains the replacement. The push is not the
problem, the commit is.

Build the branch's final commit from the merge base in one piece:

    git stash push            # uncommitted port
    git reset --hard origin/<base>
    git checkout <old-head> -- .
    git stash pop
    git add -A && git commit  # the diff against the base has no restored lines

This passes the hook without any bypass.

## Rebasing onto a base that renamed a symbol: stale references survive

If the new base renamed a symbol (for example a factory method) and the branch
added calls to it in files that did NOT conflict, auto-merge silently keeps the
old name in the branch's lines: the base renamed its own part and did not touch
the branch's additions. Two mandatory steps after resolving:

- grep the WHOLE repository for the old name (not only the conflicting file)
  and review each hit: same-named members of other types must not be renamed;
- compile the tests of every touched module, not only the one that conflicted.

In the incident behind this rule the only conflict was in one test file, yet
auto-merge left more than twenty stale calls there and a few more in another
module's tests, caught only by compiling those tests.

## Reference report at completion

The report must be machine-checkable, not evaluative:

```
Fixed the error-state reset in ToolCallPanel.retry(): resetError() is called
before the retry. Branch hermes/fix-retry, PR #52.
git log <base>..hermes/fix-retry --oneline: a1b2c3 fix(ui): reset error state
Touched: ToolCallPanel.tsx (+7/-2). Tests: <module test command>: 34 OK.
git status --porcelain: empty.
```

Bad: "Done, everything works". Such a report is sent back.

## On someone else's card, a PR is referenced by number, without a URL

A comment on a card whose assignee is not you (a reviewer's verdict, an
architect's summary, a status note, parking a nearby finding): mention the PR
as "PR #203, CHANGES REQUESTED", without the `https://github.com/.../pull/`
URL. The dispatcher's active-PR guard freezes a ready card with a PR URL in a
fresh comment for 24 hours, regardless of who wrote it; a verdict posted on a
never-run rebase card once held it for a hundred ticks with free slots. The
URL stays in the PR and on your own card.

## Check

- `git status --porcelain` is empty at handoff;
- the report has `git log <base>..HEAD --oneline` with a non-empty list;
- on mass failures of neighbouring cards the analysis started with the
  environment.

## Do not burn the budget on waiting and full builds

- Wait for an event, not for the clock: `sleep N` longer than 30 s is not a
  way to wait. CI: `gh run watch <id> --exit-status` or `gh pr checks <N>
  --watch` (they return as soon as CI finishes); a build or a stand: a polling
  loop on a condition from real output (a success line or exit code) with an
  overall timeout, for example
  `for i in $(seq 1 40); do grep -q '<success line>' log && break; sleep 15; done`.
  Blind sleeps are best caught by a nightly audit of tool calls in the
  profiles' state databases (one such audit counted 154 blind sleeps, 9.5
  hours, in three days).
- `gh pr checks --watch` inside a card is waiting, not work; on short cards it
  burned a quarter of the runtime limit. Check CI with a single `gh pr checks
  <N>` snapshot before the handoff; waiting for green belongs to the next run.
- Background build: one process_manage with a long wait, not polling every
  few minutes. In the incident behind this rule a session hit three
  process_manage timeouts in a row waiting for the final verification and died
  on the last allowed call right between them: the work was done and pushed,
  the budget burned on waiting. Rule: `terminal(background=true)` plus one
  `process_manage` with wait and headroom; meanwhile do useful steps (PR text,
  heartbeat), not polling.
- Targeted builds: build and test only the touched module and test class, not
  a full build on every pass. On a worktree stacked over an open PR, also
  build the in-repo modules it depends on; otherwise the build resolves stale
  artifacts from a local cache and either fails on missing classes or, worse,
  silently runs an old test set green. Sign: grep for the target test class in
  the current command's output is empty while the build reports success. When
  a module filter is combined with a test filter, configure the runner so that
  modules without matching tests do not fail.
- Build on the spot: during the card only the touched module and the needed
  tests; one full build (the phase CI runs) right before the push, CI does the
  rest. A second full build in a row without code changes is forbidden. One
  background build per worker: on a small VPS parallel builds fight each
  other. A shared lock on full builds turned out not to pay off (cross-profile
  overlap was minutes); measure before adding one.
- Before reading a report, make sure the targeted run actually REACHED the
  tests: an old report directory from a previous full run shows green for tests
  that did not run. Trust the test counts in the stdout of the current command
  plus the build result, not a report file without a timestamp check.
- Do not cut background build output with `head -N` in a pipe: head closes the
  pipe, the build gets SIGPIPE and dies mid-suite, and a trailing echo masks
  it. Redirect the whole log to a file and tail/grep the file.
- `<build> | tail -N` returns tail's exit code, not the build's: a green pipe
  does not prove success. Write output to a file (`> log 2>&1; echo
  EXIT=$?; grep '<success line>' log`); an artifact newer than the build is not
  proof that the gates ran.
- A terminal session sitting in a removed worktree fails with exit 126 before
  the command runs: fix it with the terminal's `workdir`, not by repeating
  `cd`. From execute_code the helper's workdir may not reach the command, and
  the build runs in another checkout; put `cd <worktree> &&` in the command
  and check the module paths in the first log lines.
- Work done: commit, push, `kanban_complete` / `kanban_request_review` first,
  cosmetics and final comments afterwards. A card once died on its last
  allowed call while posting a final `gh pr comment`, with the work complete
  but no result recorded.
- A card that hit the iteration ceiling is a defective card: block with
  `kind=needs_input` and a proposed split; do not expect the next run to fit
  in the same budget.
