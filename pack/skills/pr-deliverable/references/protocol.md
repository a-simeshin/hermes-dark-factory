# pr-deliverable: full protocol

This document is the long form of the pr-deliverable skill: the git and PR
protocol, the owner summary comment (WCFY) and its lifecycle, the rule that
the merge is the owner's only step, the requirement that every delivery text
is checked by a command on the current head, proofs for closed blockers,
screenshots, the PR self-check, CI expectations, drive-by edits, merge
conditions, and what to do when a PR shows no CI runs. Incidents are retold
generically to explain each rule.

The result lives in the PR, not in chat and not in the card. A Telegram
notification is a copy, not the place of truth.

## When to use

You deliver code, publish a verification or review verdict, or close a card
that has a PR.

## Git and PR protocol

- Work on branch `hermes/<short-task-name>` (the worktree is issued
  automatically).
- Atomic commits with a message about the change itself, WITHOUT
  `Co-Authored-By` footers or generator mentions.
- Push with `git push origin <branch>`. In the reference setup the git
  credential helper is the pack's gh wrapper, which mints a GitHub App token.
  A classic way to break it: an empty `helper =` line at the end of the git
  config clears the helper list; reads keep working on a public repository
  (anonymous fetch) while pushes fail with `could not read Username`. If the
  helper is broken, pass a freshly minted token for one push with
  `TOKEN=$(~/.hermes/scripts/gh-app-token.sh) git -c credential.helper= -c 'credential.helper=!f() { echo username=<bot-account>; echo password=$TOKEN; }; f' push origin <branch>`.
- PR: `gh pr create --title "..." --body "..." --base <branch from the
  project conventions and merge history>`. The base is NOT the default branch
  by default.
- update-branch conflicts: `gh pr update-branch` cannot resolve them. Before
  blocking the card, check whether an identical conflict was already resolved
  by hand in a neighbouring PR that day. Do not duplicate a resolution
  blindly: pull the other fix's setters into your cleanup block and leave no
  duplicates. An automatic merge of tests can keep one side's test pinning the
  other side's old behaviour: re-pin it to the merged semantics. Verify
  (tests, lint, build) before pushing.
- The PR body links the issue mirror explicitly: `Refs #N` for multi-PR
  work, `Closes #N` for a single PR. A mention in prose builds no link.
  `Closes` closes the issue on the merge of the FIRST PR (documented GitHub
  behaviour), so with several PRs it closes the issue before the second is
  merged. One PR: `Closes`; several: `Refs` on each and manual closing.

## The owner summary: a mandatory comment

The owner reads a PR for one question: what changes for them, and how is it
proven. The summary is prepared in the PR body from the first round and
published as a comment (in the repository's language) with exactly this
heading, which machines search for:

```
## What changes for you
```

Three short parts:

1. **What changes for the user.** Behaviour before and after in product
   words, not code words. Not "added ConnectFailedError" but "server
   unreachable: the window used to show Disconnected silently, now it shows
   the cause and a Retry button".
2. **How it is proven.** UI: screenshots before and after (PNG on the assets
   branch, embedded by raw link). Non-UI: an example call and its real output.
   A statement without proof does not count.
3. **What was run.** Commands and numbers: how many tests, which gates, what
   is green.

The hygiene script checks for the heading and does NOT show the PR to the
owner as merge-ready until it exists. The summary does not duplicate the
tester's verdict: the verdict answers "does it work", the summary answers
"what does a person get".

### WCFY is written once, after the reviewer's PASS, on the final head

WCFY is published ONCE, after the reviewer's PASS, on the final head. Before
PASS it is neither published nor updated: rounds of changes are described in
the PR body. An extra WCFY reads to the owner as readiness that does not
exist.

When the watchdog demands a new WCFY (the head moved past the old one), mark
the old comment outdated by PATCH (numeric id), with the marker placed BEFORE
the heading, so the old comment's first line is no longer `## What changes
for you` and exactly one candidate remains. "One per PR" beats any
instruction in a card.

Marking via REST: gh's built-in `--jq` takes no `--arg`, so a variable in the
response format fails with "variable not defined" AFTER the PATCH has already
been applied on the server. Exit code 1 here means "the response formatting
failed", not "the patch failed"; repeating it produces a double marker.
Before a repeated PATCH, reread the body and count existing markers; normalise
to exactly one leading `outdated PATCH-<id>`. Build payloads with a small
script (`json.dump` into a file, then `--input`).

Exception: when the reviewer's verdict or the owner explicitly lists WCFY
among the closing steps for a new head ("update-branch, e2e, green CI on the
new head and WCFY for that head"), publishing it on that head executes their
decision. The agent performs every step of such a recipe except the merge.
The formal PASS then comes on the same head (a comment does not move it);
when the head changes, WCFY is PATCHed by numeric id. All earlier steps are
verified by live commands before publishing.

The other side of the same rule: WCFY must EXIST and match the current head.
A missing WCFY on delivered PRs and a WCFY for an old head are the same
defect: the text fell behind the code. Before delivering each round:

- WCFY for the current head: if the head changed after PASS, PATCH the same
  comment, never add a new one;
- before publishing a WCFY drafted in an earlier run: (a) check that the PR
  has no WCFY already (a second one is forbidden even if a card asks for it);
  (b) refresh EVERY number for the final head: mutation counts and red-test
  counts drift between runs and heads; quote the final head's numbers from the
  public PASS verdict, or name the head of your run explicitly;
- old comments with refuted claims are edited or marked outdated;
- numbers come from the output of THIS run's commands; counts written from
  memory are always slightly wrong.

### The merge is the owner's only step

Only the merge, and decisions with no technical path (milestone, prototype
acceptance, a repository setting), belong to the owner. Everything the app's
rights can do, the agent does before handing the PR over:

- update-branch: `gh api -X PUT repos/<org>/<repo>/pulls/N/update-branch`; on
  422 (a real conflict) merge locally, resolve and push; then reread the PR
  texts against the merged head (counts absorb other people's merged commits);
- waiting for required checks on the new head: poll `gh pr checks` /
  `gh run list`; a red required run is fixed or the card blocks, not "the
  owner will wait";
- `gh run rerun` for an infrastructure flake (empty commits and close/reopen
  are forbidden);
- a fresh WCFY and a repeat review of the new head.

"Owner-only" or "owner steps" in a PR comment, verdict or block condition
is allowed only for the merge or a decision with no technical path. In one
case "update the branch, wait for checks, merge" were all called owner-only
while the app had been doing update-branch on the same board for weeks, and
the PR waited for the owner on agent work.

## Every claim in every text is checked by a command on the current head

Before `kanban_request_review` and after ANY rework, reread ALL delivery
texts against the diff on the current head: PR body, WCFY, divergence
registry, test names, doc comments, commit messages, PR title. Every claim,
count, hash and promise must equal what a command shows. If nothing can check
it, do not write it. Rework almost always makes old text false, and reviewers
do not reread text.

Update-branch makes a body false more quietly than a rebase: counts grow from
OTHER people's merged commits (attribute them with `git diff --name-status
<old-head>..<merge-head> -- <module>`), fresh numbers come from the green CI
run's log, and semantics written by early stack cards start contradicting the
owner's later decisions. The final assembler of a stack rereads the WHOLE
body against the merged head and PATCHes it before the summary comment.

More update-branch tails: (a) an automatic merge combines SIGNATURES (your
parameter lands in someone else's overload) and the base side's tests call
the old arity: compilation fails in test compile; fix by extending the other
side's call sites, not by changing production code; (b) when two contracts
share one stream, the arbiter is the merged tests of BOTH sides: the base's
zone stays with the base, your feature delivers through its own path; (c)
repeat verification with a full build, not a resumed partial build that pulls
stale snapshots from a local cache.

### A decision or blocker is closed by exactly the requested artifact

"Done" and "owner decisions recorded and followed" are written only when the
artifact is exactly what was requested. A surrogate is not fulfilment:

- the decision was "key = active connection id", the code keyed by form
  fields;
- a blocker "needs a port to the other line" was closed by opening an issue
  and reported as done;
- "align the design with PR X" was closed by a paragraph in the body while
  production files had six conflicts and opposite error semantics;
- a condition dropped out of an issue criterion, and QA confirmed the
  rewritten criterion on an irrelevant scenario.

Rules:

- disagree with the owner's decision: ask, change nothing until answered;
- did it differently: write "not done, I propose X", not "done";
- never rewrite an issue criterion: compare with the issue's original text.

"Before" for the user and on screenshots is what is in the integration branch
now, not an earlier head of the same PR. The PR title reflects content
(`test(e2e)` only on a test-only PR). The owner's acceptance is quoted in
their words; "known, accepted difference" without such words from the owner
is fiction.

Typical false texts found by the owner: a body claiming a contract fixed
while CI was red on exactly that test; a WCFY written before the most
visible change landed and silent about it; "no user-facing change" for a PR
that introduced the feature; a WCFY promising a tab the UI never renders;
"Before" described from an older head; "parameters are honoured" while the
browser drops the repeated directive; "visible warning" that is never
visible; a registry entry saying "4 tests" for 5.

Machine check: `gh pr view <N> --json body --jq .body` plus
`git log origin/<base>..HEAD --oneline` plus `gh pr checks <N>`. Fix a body
with a JSON payload: `json.dumps({"body": ...})` into a file and
`gh api repos/<org>/<repo>/pulls/N --method PATCH --input payload.json`,
then compare the read-back byte for byte. (`--field body=@file` sends
multipart and fails with "unexpected end of JSON input".) Capturing
`--jq .body` adds one trailing newline: do not carry it into the upload. An
outdated WCFY is fixed by PATCHing its comment, not with a new comment or an
"outdated" note. The id for the PATCH is the NUMERIC id from
`gh api repos/<org>/<repo>/issues/N/comments --jq '.[].id'`; a GraphQL node id
returns 404.

## A mutation for every closed blocker is part of delivery

For each fixed blocker, attach to the PR one of:

1. a mutation of the production line (the exact edit that cancels the fix)
   and the run output: the new test red on the mutation, green on the fix;
2. a red run of the new test on the base before the fix.

A prop or text test is not proof. The reviewer repeats the mutation on the
exact head.

Run the battery (each mutant times each affected branch) as a background
script via `terminal(background=true)`, logging to a file and reverting with
`git checkout --` after each run: `execute_code` times out and kills the
kernel mid-loop, leaving the mutant applied. After any timeout: `git status`
in every worktree before anything else.

Mutate only a COMMITTED head: `git checkout --` after the run reverts the
whole file to HEAD, including an uncommitted fix. Order: commit, mutate, run,
`git checkout --`.

A body passed through stdin to `gh pr comment --body-file -` may be refused
by a link-guard hook that fails closed: write the body to a real file and pass
its path. Take "expected but was" numbers from the build output of that run;
report files get overwritten by the next run of the battery.

A surviving mutant with no killer is not "covered enough" but a sign of dead
code: the line is unreachable on the current base. When a base change moved a
call inside an inner try, two cleanup lines in catch branches became
unreachable and the battery had zero killers; the right answer was to delete
the dead lines and shrink the matrix, not to keep untestable "defensive"
cleanup or bend a test to kill it.

The second reason a mutant survives: the test does not execute the path
that sets the pinned flag (it used a default controller without the
dependency, so the mutated method never ran and the assertion was vacuous).
Diagnose with a print on entry to the mutated method; mind that test stdout
may land in report files, not in the build log.

## Taking frames: source first, then DOM probe

When a selector fails or a panel appears and disappears, first open the
component's source and see how the id or test id is actually attached
(conditional render, tab state). DOM probes without the source are a lottery.
Order: source, a stable anchor from it, then a probe to confirm.

## A UI verdict carries before and after screenshots

For any task that changes UI, the verdict in the PR comment includes "before"
and "after" screenshots for each checked viewport:

1. push screenshots to the assets branch (`screenshots/<task_id>-<what>.png`);
2. embed them in the PR comment by raw.githubusercontent.com links;
3. take "before" from the defect's issue mirror, or shoot it yourself on the
   base branch before the fix;
4. sending screenshots only to Telegram or only to the card does NOT count.

## PR self-check right after creation

After `gh pr create` check `gh pr view <N> --json baseRefName,files`: base
and file count must match the card. Dozens of unrelated files mean a wrong
base. `gh pr diff` has no `--stat`: the file count is
`gh pr diff N --name-only | wc -l`. To tie CI to a commit use
`gh api repos/<org>/<repo>/commits/<sha>/check-runs`; `gh pr checks` shows
only the current status. A run started by `gh workflow run`
(`workflow_dispatch`) does not appear in `gh pr checks` at all ("no checks
reported") but is fully visible by commit SHA.

## A PR is not delivered while its CI is not green

A CI criterion is closed by a green run, not by reasoning. A red run within
twenty or thirty seconds is a build or style failure where tests never ran:
look at the first failed job, not at hypotheses.

### No drive-by edits outside the card

The PR diff contains only the files the card names. Cosmetic sweeps (dash
replacement, line endings, import order) go in a separate PR and card. A
drive-by edit not declared in the body or the registry is a defect. Typical
cases: a dash-replacement sweep across build files and sources that caused a
conflict with a neighbouring PR; a workflow action bump riding along with a
feature; deletion of upstream tests; a stray local ignore file; hundreds of
unchanged lines touched for punctuation while new lines in the same PR broke
the same style rule.

A change to the core or to a neighbouring module not required by the card
goes in a separate PR with its own issue, even if it is "in the same file".
CI and gate changes always go in a separate PR. Deleting someone else's test
is an automatic RETURN without reading the rest of the diff.

## Merge: only after a PASS younger than the last commit

A PR is merged when:

1. the last review verdict (a comment starting `## Review verdict`) is PASS
   and younger than the last commit; a push after PASS burns it;
2. no CHANGES REQUESTED verdict is younger than the last PASS;
3. CI is green on the last commit.

Typical violations: PRs merged while the last verdict was CHANGES REQUESTED
with no new PASS after the fixes; PRs merged before review, reviewed
"post-merge".

Check before merging:

    gh pr view <N> --json comments --jq '
      [.comments[] | select(.body | startswith("## Review verdict")) |
      {author: .author.login, body: .body[0:200], createdAt: .createdAt}] |
      last'

## A finding is checked against the issue's criteria, not the list of touched files

"Was file X touched" does not close a finding about requirement Y. If the
acceptance criterion says "no secrets persisted in plain text", every file
that writes to storage is checked, not only the one named in the last review.
In one case a reviewer dismissed the secrets question with "the auth module
is untouched" while two other files persisted custom headers (including
Authorization) verbatim in browser storage.

## Checklist

- the issue body starts with `## Before / After` (UI: two real images on the
  assets branch; non-UI: a mermaid diagram of the delta); the PR has no
  Before/After section;
- the PR has a `## What changes for you` comment (one, after the reviewer's
  PASS, on the final head);
- every closed blocker has a production-line mutation or a red run on the
  base attached;
- tests that model a third-party library's output: a `Library contract:
  <artifact>:<version> <Class>#<method>` line in the body, at least one
  fixture produced by running the real library;
- the final screenshot of a UI E2E test is attached when one runs;
- `gh pr view <N> --json baseRefName,files` matches the card;
- CI on the PR is green;
- the last review verdict is a PASS younger than the last commit;
- `Refs #N` (or `Closes #N`) in the body is checked by command:
  `gh pr view <N> --json body --jq '.body' | grep -E '^(Refs|Closes) #[0-9]+'`.
  A self-reference or a missing link is fixed by PATCH. Forbidden: a number
  from memory, `Refs:` with a colon, a board card id instead of an issue.

## No runs on the PR: check mergeability first

`pull_request` workflows run on the merge ref, not on the branch head. If
the PR conflicts with its base, the merge ref is not computed and GitHub
creates NO run: not red, not cancelled, nothing. `gh pr checks` says
`no checks reported`, the commit has zero check suites, and it is easy to
mistake for broken CI or missing permissions.

Order when a push did not start a run:

    gh pr view <N> --json mergeable,mergeStateStatus     # CONFLICTING/DIRTY is the cause
    gh api repos/<org>/<repo>/commits/<sha>/check-suites --jq .total_count   # 0 = no event created
    gh api "repos/<org>/<repo>/actions/runs?per_page=5" --jq ".workflow_runs[] | \"\(.created_at) \(.event) \(.head_branch)\""

The third command answers "is CI stuck for everyone or only for me".
A rerun restarts only an existing run; with no runs at all, the only way back
to checkability is to remove the conflict. The app needs Actions read and
write for rerun and dispatch; a 403 on rerun means degraded permissions, a
signal for the owner, not a reason for a workaround.
