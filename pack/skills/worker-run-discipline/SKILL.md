---
name: worker-run-discipline
description: How not to lose work within a single card run.
version: 2.1.0
author: maintainer, Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [worker, git, commit, delegation, diagnostics, budget]
    related_skills: [kanban-discipline, save-lesson-before-complete, issue-mirror, pr-deliverable, tester-verdict-checklist]
---

# Run discipline

This skill covers how a worker spends one card run without losing its work:
waiting on conditions instead of sleeping, saving context, keeping heavy
processes out of the gateway, committing early and handing off right after
the push. A run has a hard iteration ceiling (`agent.max_turns`), and
everything not committed disappears with it. Background: references/protocol.md,
one-off lessons: references/lessons.md.

## Wait on a condition, not blindly

Blind sleeps are the largest single waste in worker runs. Before every
wait, name the READINESS SIGNAL (a log line, an HTTP code, a file) and wait
for it in a loop "check, pause 5-10 s, check" with a timeout. A bare `sleep N`
is allowed only for N <= 10 s.

## Someone else's result is not waited for inside a run

A run does NOT wait for a reviewer's verdict, a merge, an owner decision, or
CI longer than one cycle of its own build. Hand off the standard way
(`kanban_request_review`, or `kanban_complete`) and exit: the verdict arrives
as an event and the dispatcher slot is free. "Do not close without a verdict"
in a card body means "do not mark it done before the verdict", not "sit in
the run polling the PR". Why: a run polling a PR for hours held a scarce
dispatcher slot; 30 minutes of `gh` queries without edits is a violation.

## Claims about external state carry an artifact link

Any claim about state OUTSIDE your run (known, tracked, duplicated, fixed,
posted, merged, reported) is written only with a link to the artifact: issue
or PR number, card id, comment URL, sha. No link means no claim: create the
artifact or write "not verified". A link is not proof either: if the claim
can be checked with a live call (`gh pr view`, `gh run list`, `gh search
issues`), check it in this session BEFORE writing. Take a comment link
(`issuecomment-N`) from the comment's own `html_url` (`gh api
repos/OWNER/REPO/issues/comments/N --jq .html_url`), never build it by hand:
the gh-link-guard gate refuses an anchor that belongs to another page (a
comment in a PR linked as an issue page).

## Parking and sandboxes

- Parking "until a moment" is only `kanban schedule` with
  `SCHEDULED_UNTIL=<ISO8601 UTC>` in the reason. A block with that label in
  free text is never read by anything and hangs forever.
- Machine fields carry UTC; local time only in human text, with the zone.
- Experiments with board commands go only to a sandbox board in a SEPARATE
  `HERMES_HOME`. A board on the live home is not a sandbox: the gateway
  dispatcher walks every board of the home (spawn and auto-decompose), and
  dispatcher settings are host-wide, there are no per-board ones.

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

- inside a card, CI is checked with one snapshot `gh pr checks <N>`; a
  criterion like "N green CI runs on the head" is collected by HANDOFF:
  push, `kanban_comment` (what was pushed, sha), `kanban schedule` with
  `SCHEDULED_UNTIL` at the CI ETA; the next run takes one snapshot;
- when the card must see CI finish before closing, use a background
  exit-code poller, not a piped `--watch` (piped output is buffered and
  lost; completion notifications are not available in kanban sessions).
  Recipe: references/protocol.md;
- no CI run after a push: first `gh pr view <N> --json
  mergeable,mergeStateStatus`. On CONFLICTING no merge ref is built and no
  pull_request workflow starts; only a merge of the base or a rebase helps.
  "GitHub glitch" is not a diagnosis without this check;
- background build: `terminal(background=true)` plus ONE process_manage wait
  with headroom; meanwhile do useful steps, not polling;
- build and test only the touched module and tests; on a worktree stacked
  over a PR also build its in-repo dependencies, or you test stale
  artifacts. Sign of a stale run: the target test class is absent from
  stdout while the build says success;
- the full build is CI's job when the project conventions say so; locally
  run targeted tests, format and lint. Changes to the build or CI config get
  ONE full build before the push. Never repeat a full build without changes
  in between; one background build per worker;
- write build output to a file: `> log 2>&1; echo EXIT=$?; grep '<success
  line>' log` (a pipe into tail returns tail's code; head in a pipe kills the
  build with SIGPIPE); trust the test counts in stdout of the current command,
  not an old report file;
- in a remote worktree use the terminal's `workdir`, not a repeated `cd`;
  from execute_code put an explicit `cd <worktree> &&` in the command itself
  and check the first log lines for the right paths.

## One PR, one card

The card that owns the PR carries its whole cycle:

- review: `kanban_request_review` on that card with an explicit `reviewer`
  (without it the card stays on you); then exit;
- after a reject the rework happens on the same card: push the new head,
  post a rework response in the PR (per blocking item of the verdict: what
  was done, commit, red-before proof), request review again, exit;
- a rebase (only when the PR is CONFLICTING or the owner asks) is done by the
  live card that owns the PR, not by a separate card;
- woke up for a rework and the PR head already moved past the verdict with a
  response in the PR, and the card never named that sha: the rework was done
  by another run. Comment that and finish. Always name the sha you pushed in
  the card comment, so your own push is told apart from someone else's;
- set `metadata.published_pr` to the full PR URL, not a number;
- if what remains is only the owner's merge, say "remaining: owner merge" in
  the completion summary.

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
    Touched: ToolCallPanel.tsx (+7/-2). Tests: <test runner>: <its final summary line, verbatim>
    git status --porcelain: empty.

- "green" means quoting verbatim the final summary line of YOUR OWN run of
  the test runner; no invented markers, no summary of someone else's run;
- on someone else's card (assignee is not you), and on your own future card
  before its first run, refer to a PR by NUMBER without a URL: a PR URL in a
  fresh comment freezes a ready card under the dispatcher's active-PR guard
  for 24 hours. Pass evidence as PR number plus head sha;
- many neighbouring cards failing: start with the environment, not the task;
  woke up on a card in review/done: exit at once; your run was marked
  timed_out but the process lives: you are a zombie, stop working.

## Check

- `git status --porcelain` is empty at handoff;
- the report has a non-empty `git log <base>..HEAD --oneline`;
- the budget checkpoint was answered with a terminal call, not "one more
  step";
- no wait for a verdict, merge or foreign CI inside the run;
- every external-state claim has an artifact link;
- on mass failures the analysis started with the environment.

## References (read on demand)

- references/protocol.md: the full protocol with numbers and failure modes,
  the background CI poller recipe
- references/lessons.md: pkill -f kills its own shell, worktree hygiene,
  recovery after a run timeout, WIP at the checkpoint, mutations and
  background runs are serialised, two-way content check after a rebase,
  zombie runs
