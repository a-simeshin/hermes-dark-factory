# kanban-discipline: lessons

Distilled lessons from running a multi-profile board, each as rule, cause and
application. They complement the protocol with less frequent situations:
completion order, umbrella cards, CLI quirks, recreated cards and verdict
routing.

**A link binds completion order, not only start.** The core refuses `kanban_complete` while a card has unfinished parents, so `link <parent> <child>` means the parent is done strictly before the child. Why: a link added only to delay a start became a deadlock; a card that was its own parent sat in todo for over a month and held three children. Apply: link only when the child truly waits for the parent; remove a stale link deliberately with `hermes kanban unlink <parent> <child>`; self-links and A-B cycles are permanent deadlocks.

**An umbrella releases its children on completion; a chain gives the order.** Why: children created with parents=[umbrella] wait for the umbrella, whose body waits for "all children done"; workers may not unlink from a worker context. Apply: before completing the umbrella, chain the children in order (`link block1 block2`, they push to one branch); move the umbrella's final duties (all children done, CI green, summary comment) into a tail card with its own key and parents=[last block], and make any high-priority guard cards children of the tail; then complete the umbrella.

**CLI quirks.** `hermes kanban block <id> "<reason>" --kind needs_input`: the reason goes BEFORE the options. Re-blocking after an unblock sends the card to triage (unblock-loop detector), so prepare the exact reason first. `needs_input` waits ONLY for an owner event (merge, a decision with no technical path); "the owner will update the branch, wait for CI, rerun a flaky job" is a false block, the agent does those itself with its app rights. `promote` works only for todo/blocked. With `max_in_progress: 1` the board is strictly serial: plan with priorities and blocks, not with parallelism.

**The idempotency key is given in the create command; "later" never comes.** `hermes kanban edit` has no key flag. Why: a watchdog recreating keyless cards carries over body, assignee, priority and PARENTS but not CHILDREN, so children hang on the archived original forever; copied parents that are already archived let the copy promote with nothing to work on. Apply: when you take a recreated card, check its expected children and parents with `kanban_show`, repair links to the live line of work before your own `kanban_complete`.

**A send-back creates TWO cards.** Why: a rework closed with nobody reviewing the fix, the last word on the PR stayed "changes requested" and hygiene reported it ready to merge. Apply: after a send-back verdict there are two open cards, coder and reviewer, the second depending on the first.

**An output meant for an issue or PR is not done until published.** Why: a root-cause card completed with its conclusion while the issue got no comment. Apply: post the comment, put its link in the `kanban_complete` summary. A coder card that opened a PR completes only after a reviewer card (priority 100) for that PR exists; if it already closed, file the reviewer card now.

**One owner verdict = one coder card on THAT PR; workers never merge.** Why: a verdict left as a comment on a neighbouring PR's card was never delivered; a "merge gate" card with a merge step is defective. Apply: the PR number in the verdict and in the card body must match; a post-approval card holds only the "what changes for you" note and the report, the owner merges; reference only card ids you have seen in `hermes kanban list` / `kanban_show` in this run.

**A verdict item already covered by a live card of the same PR is not filed again.** Why: two coders produced the same screenshots for one PR. Apply: before `kanban create` for a verdict, compare with the PR's live cards and subtract covered items from the body and its acceptance criteria, noting which card covers them.

**Rework for a verdict lives in the verdict PR's branch.** Why: parallel blocks of one verdict each opened a stacked PR, turning one review into a chain of three or four PRs and colliding on shared files. Apply: each child names its push target ("branch of PR #N: <branch>"); children are chained, not parallel; a new stacked PR only with an explicit owner marker in the PR body; an already-opened stacked PR is folded back by the architect, never force-pushed by the worker.

**Test fixtures must mimic the live tool's shape.** Why: a fixture returned a list where `gh ... --json` returns an object, an `except` swallowed the error and idempotency silently died. Apply: copy the container type from real output.

**`kanban_attach`: long base64 copied from terminal output arrives corrupted.** Why: payloads over about 2 KB were refused as invalid base64 three times in a row, each retry repeating the same damage. Apply: on the first refusal do not resend; regenerate with a sha256 next to it and keep attachments under about 1 KB, or deliver the content via `kanban_comment` plus an absolute path in metadata.
