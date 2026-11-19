# issue-mirror: distilled lessons

Lessons on prototypes for feature and ux issues, routing owner verdicts,
deduplicating cards created from verdicts, and closing an issue by its
acceptance criteria rather than by merged PRs. Each entry is "Rule. Why. How
to apply."

1. **For a feature or ux issue, After is the prototype.** Why: the owner
   must see the result before implementation. How: a DOM edit of the
   integration branch's demo stand using the product's own classes, captured
   by the shot script, in the issue's Before / After table, followed by a
   short behaviour description and open questions. Never a separate HTML page:
   it reads as another product.
2. **The implementation card is born blocked.** How: `kind needs_input`,
   condition "prototype accepted"; only the owner's comment in the issue with
   those words unblocks it (the watchdog lifts the block). Its branch starts
   from the integration branch, not from the prototype branch.
3. **Owner verdicts arrive two ways; route both.** Why: a formal review that
   reaches no card is lost. How: accept `issue_comment`/`created` (text
   `comment.body`, number `issue.number`) and
   `pull_request_review`/`submitted` (text `review.body`, state
   `review.state`, head `pull_request.head.sha`, number
   `pull_request.number`). Success is a card on the board.
4. **Deduplicate before creating a card from a verdict.** Why: two routes
   once mirrored one review into two coder cards on one branch. How:

       hermes kanban --board <board> list --json | jq --arg PR "#N" --arg HEAD "<head7>" '
         .[] | select(.status != "done" and .status != "archived") |
         select(.title | contains($PR)) | select(.body | contains($HEAD))'

   Found: comment on it. Not found: create with
   `--idempotency-key owner-review-<pr>-<head7>`.
5. **Close by acceptance criteria, in six steps.** Why: merged PRs are
   necessary, not sufficient. How:
   (1) every criterion met with proof from a stand, not "PR merged";
   (2) no card of the plan is todo, blocked or running;
   (3) do not contradict your own "keeping it open for X";
   (4) mark `[x]` with a proof link on met criteria, leave `[ ]` with a reason
   on unmet ones (`gh issue edit N --body-file`); every PR number comes from
   `gh pr view N --json number,title,baseRefName,state` this session, written
   as `[PR #N](url) "title" (base)`;
   (5) a closing comment: PRs, criteria, proof links, verdict;
   (6) `gh issue close N`.
6. **Only a merge closes an issue.** How: every linked PR is MERGED
   (`gh pr view <N> --json state`) before step 6; "fixed via PR #N" on an open
   PR or "completed" with unmerged work is forbidden.
7. **`Refs` for several PRs, `Closes` only for one.** Why: GitHub closes the
   issue on the first merged PR carrying `Closes`. How: change `Closes` to
   `Refs` before pushing if the plan grows to several PRs; close by hand.
   `Refs #N` does not fill the PR's `closingIssuesReferences`: when you look
   for the PRs of an issue, read the `cross-referenced` events on the issue
   timeline, not that field.
8. **An issue wider than what is merged gets `keep-open`** plus a comment
   explaining why.
