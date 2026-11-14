# adversarial-review: full protocol

This document is the long form of the adversarial-review skill. It repeats
each rule of SKILL.md with the reasoning and the kind of incident that
produced it, so that a reviewer who doubts a rule can see what it costs to
ignore it. Incidents are retold generically; the numbers are given only where
they explain the scale of the problem.

The goal is to refute the work, not to approve it. Refuting means proving: a
remark without consequences is not a finding.

## When to use

Plan review, PR review, repeat review, checking someone else's finding.

## What you look at, by review type

- **Plan or decomposition:** holes in acceptance criteria, invalid premises,
  missing release lines, orphan tasks, hidden dependencies, the risk of
  parallel edits to the same files.
- **Bug report:** does it reproduce at all? Open a stand and check by hand. A
  false finding is RETURN with proof.
- **Code:** the diff line by line, `find_referencing_symbols` for EVERY
  changed symbol. Look for broken neighbouring calls, races, leaks, invalid
  premises, deviations from conventions, tests CI never runs, extras outside
  the ticket.
- **Tests:** do they test anything? The mutation question: what is the
  smallest break of the code these tests would NOT catch? And does CI run them
  at all?
- **A document for publication (a table, an inventory, arguments for a GitHub
  thread):** redo every grep count and every line number yourself on the live
  head after a fetch. Claims like "the only occurrence of X" or "line Y is not
  in the code" are refuted by one grep. Line numbers in busy files live for
  hours: for publication demand a symbol name plus a permalink to a commit,
  not a bare line number.

## Protocol

1. Read the card, the issue mirror and the discussion: people's decisions
   outrank your assumptions.
2. Prove every claim: `file:line`, command output, a screenshot, a
   reproduction step. A claim without proof is not published.
3. If the work is bad, say it directly.
4. Verdict: ACCEPTED or RETURN (findings by severity CRITICAL / IMPORTANT /
   MINOR, each with proof and a minimal closing condition).
5. Code findings also go into a PR review comment, in the repository's
   language.
6. You do not edit code, commit or merge.

## What may and may not block

**A blocker (the only grounds for RETURN):** the plan cannot be executed or
guarantees rework of the code. A cyclic dependency in the graph; a test not
run by the stated runner (checked in the build file or workflow); a wrong path
or endpoint (checked in code); no test layer for an affected user path. An
acceptance criterion measured as unmet in a live run is a blocker, not a note
attached to an ACCEPTED verdict: in one case the reviewer measured that a
method returned "method not found" while the server advertised the capability
(an explicit acceptance criterion of the issue) and wrote it down as a note.
A note does not close a criterion: RETURN with the measurement.

**Not a blocker (a remark, the verdict is still ACCEPTED):** an incomplete
formal contract when the content is clear; wording, numbering, section order;
card text not matching the issue word for word; a wish for more detail; style.

**Never block on something only the owner can close.** Missing owner
approval, an unapproved narrowing of criteria, a "do we do this at all"
decision are not defects of the plan, and the architect cannot close them.
Verdict ACCEPTED with "awaiting the owner's decision on point X".

**Cost:** one plan-review chain went through 14 RETURN cycles, the last of
which rested on "no owner approval" and "mirrors are not byte-identical".
The architect could close neither; work stood still and the money went on
loops.

## A point the owner accepted does not go into the verdict

Before the verdict, read the owner's LAST comment on the PR. A point the
owner explicitly accepted ("leave as is", "do not count this as coverage") or
explicitly withdrew is not a finding. Only a new fact reopens it: a commit
after the owner's decision, new evidence, a new failure. A true fact plus an
already-made decision about it is a revision of someone else's decision, and
it burns a coder-reviewer round for nothing. In one case the reviewer issued
CHANGES REQUESTED three times on exactly the two points the owner had accepted
an hour earlier; the PRs were merged by the owner and the rework cards were
archived as garbage.

A plan-review verdict STARTS with the word ACCEPTED or RETURN: the
plan-review gate parses it. ACCEPTED unblocks the children; RETURN creates a
rework card and then a repeat review.

## `## Review verdict` is a load-bearing marker, not decoration

When the same GitHub App opens and reviews PRs, a formal GitHub review is
impossible: GitHub forbids reviewing your own PR, and `reviewDecision` stays
empty forever. The verdict comment is therefore the ONLY machine-readable sign
that a PR has been reviewed. The first line is exactly `## Review verdict`.

The error shows from both sides: a maintainer scanned open PRs by
`reviewDecision`, saw "no review" and re-reviewed a PR that already carried a
verdict with a stress experiment and a live measurement. Scanning by the right
signal answers at once:

    gh pr view <N> --json comments \
      --jq 'if any(.comments[]; .body | startswith("## Review verdict")) then "reviewed" else "not reviewed" end'

## The reviewer card does not close before the verdict

A reviewer once closed a reviewer card as done without publishing the
verdict. The idempotency key `reviewer-pr<N>-<head7>` deduplicates against any
non-archived card, including done ones: a repeated create returned the same
closed card, no new card was armed, and the PR waited a day without a verdict.

Rule: the reviewer card closes ONLY after the verdict comment (with the head
sha) is published on the PR. No verdict yet: the card stays open and its
comment says what remains to check. The watchdog safeguard: it sees a done
card for the head key without a verdict, raises "reviewer closed without a
verdict" and arms a new card with key suffix `-r2`, `-r3`. A done without a
verdict saves nothing.

## A verdict in someone else's card goes by PR number, without a URL

The dispatcher's respawn guard checks only whether a fresh comment contains a
PR URL, not who wrote it. A reviewer cross-posted a verdict with a PR comment
URL into a ready rebase card that had never run, and the guard held that card
for 100 ticks while slots were empty. Rule: in your own reviewer card a link
is fine; in a card assigned to someone else, write "PR #N, CHANGES REQUESTED,
verdict of <date>" without a URL.

## The `## Calibration` block

The review layer (the board watchdog) does not count a verdict comment
without a Calibration block and re-arms the card as if there were no verdict.
The owner's formal reviews are exempt. The most recent verdict on the head
decides.

The block comes at the end of the verdict body, one line per applicable class
from the misses journal (see misses-template.md):

    ## Calibration
    - contract: issue #N criteria all present in the head diff
    - mutation: each blocker's fix line mutated, each mutant killed by a named test
    - ui-input: live probe at 375px, the Connect button is clickable by real input
    - pr-texts: WCFY compared with the actual UI behaviour, not with the diff

"N/A" without a reason is not accepted ("stack: N/A, PR is not stacked").
A class line without the real check behind it is a miss and becomes the next
journal entry.

## Repeat review: closures plus the delta since the reviewed head

A repeat review has TWO mandatory parts:

1. closure of the previous verdict's findings, point by point, with proof;
2. a fresh review of the diff since the last reviewed head: changed behaviour
   is checked again with live scenarios, not by comparing with the old
   findings' text. Other orderings of async events, a third-party server with
   its own methods, boundary data sizes, editing a form while connecting.

The scope rule exists for cost: repeat reviews that redo the whole PR each
time become the largest token expense of the factory. A repeat review covers
ONLY the diff since the last reviewed head plus closure of earlier blockers.
A full review only when the delta touches a new area or rewrites more than
half the PR. The watchdog never arms two reviews on one head.

Checking closures is not review. Only the surface untouched since the last
round is skipped: a new finding there gets a separate card and does not block
(anti-loop). A finding in code changed this round blocks in any round.

**Cost of both extremes.** A full checklist every round is a loop: three
chains once reached 14, 13 and 10 cycles with 69 cards and no task moved.
Closure-only review lets blockers through: the reviewer passed several PRs on
"incremental closure" and the owner found new defects on the same heads (a
third-party server's methods being intercepted, history overwritten at large
sizes, a reconnect without await hiding a connection error, a WCFY promising a
tab that did not exist).

The lens changes by round: round 1 reads the artifact cold, before the
executor's story; round 2 runs and checks the claims; round 3+ checks strictly
against the original criteria and the closure of each point.

## PASS requires a mutation check of every blocker

Incremental scope does not cancel checking the implementation. "Incremental
re-review only, did not re-audit the production implementation" is a refusal
of a verdict. A PASS contains, for EACH blocker it treats as closed (from the
owner or from earlier rounds), a mutation line: what you broke in production
code, which tests failed, the run output. A mutation no test catches means
RETURN even with a fully green run; a large green test count proves nothing.

**Every blocker, including earlier rounds.** The delta lens frees you from
searching for new findings on old surface, not from mutating each fix line.
In one case the reviewer mutated only the line fixing the latest round's
blocker; the fix of an earlier blocker (a write of the "last active
connection id") was not mutated, and reverting it kept all its tests green.

**The exact fix line.** The mutation goes into exactly the production line
that fixes the blocker (state write, guard, branch), not into neighbouring
logic of the same PR. A red test caused by a different line does not prove
the closure.

**A test that seeds the fixed state proves nothing.** If the test itself
puts into place the state that the fix is supposed to write (for example it
stores the value in local storage during setup), the mutant of the fix line
stays green. That is RETURN: the test is fixed, not the verdict.

**The mutation must change behaviour.** In one case replacing `continue;`
with a flag assignment inside a catch was neutralised by a later condition on
a stale variable; the "mutated" run was green and the old behaviour did not
come back. Before counting red tests, make sure the mutant is alive: a throw
probe in the mutated branch (every run must explode) or the names of failing
tests. A green run under an under-mutation does not mean weak coverage, it
means the wrong mutation: reinvent it from the old version's semantics. The
equal alternative: a red run of the new test on the base without the fix.

**Prop and text tests.** A test that checks a prop or a text instead of
behaviour is not verified by mutation.

**Do not build the live artifact in parallel with mutation runs** in the same
tree: the bundle can be built from the mutated source and the stand silently
carries the mutant. In one case the bundle contained a multiplied constant and
a value above the limit passed the live guard while the clean-source tests
were green; it was caught only by the mismatch between live behaviour and the
mutation run. Before a live run, check the key expression in the artifact
itself.

**The second symptom of the same disease:** all rounds go into test
scaffolding and nobody checks the semantics of the production path. Before
PASS ask: what does the real code do on the real stack, and does it match
what the test simulates? In one case six rounds polished a HEAD-request probe,
and nobody noticed that the server frameworks answer HEAD like GET and open
exactly the session the probe was meant to avoid. The green tests pinned the
opposite of the PR's goal.

## Promises in the PR body and WCFY are pinned by a mutant-killing test

Applies to EVERY review, first and repeat. On a repeat review it applies to
the delta of promises since the last reviewed head.

Make a list of all statements in the PR body, the WCFY comment and related
comments (which methods are intercepted, which error codes are written, what
the user will see). Map EACH to a test that catches its violation, verified by
a mutation on the exact head. A promise without a pinning test is a blocker:
it is already published, and its fulfilment cannot be deferred to a card.

Typical misses: mutants like "record only responses with a result" or
"record only one method" survived while the body promised recording of all
exchanges, and the gap was filed as a separate issue past the verdict; a body
promised "the same flow as the sidebar" and no live scenario checked it, so a
single click with an empty field showed the promise broken. The second case
happened on a FIRST review because the rule had lived only in the rebase
section.

## A third return on one finding is an escalation, not a fourth round

Count rounds PER FINDING. Two returns are normal work. The third means the
finding cannot be closed by the executor and another round will change
nothing. Choose one:

- the finding is not really blocking: ACCEPTED, and file it as deferred
  (issue plus card, `DEFERRED:` line in the summary);
- it is blocking but the executor cannot close it: `kanban_block` with
  `kind='needs_input'` and the exact decision needed from a human;
- it splits into parts: split the card instead of demanding everything at
  once.

The built-in review lane routes `kanban_request_changes` without loop
accounting: there is no limiter, you are it. The sign it is time to escalate:
your new verdict differs from the last one only in numbers or wording, not in
the substance of the requirement.

## The review lane is a built-in cycle, not cards

The dispatcher started you on a card from the `review` lane; it has its own
transitions.

| verdict | when | how you finish |
|---|---|---|
| accepted | criteria met and checked | `kanban_complete` |
| return | the executor can fix the defect | `kanban_comment` with findings, then `kanban_request_changes` |
| escalation | a human decision or an external event is needed | `kanban_block` |

`kanban_request_changes` returns the card to THE SAME executor and keeps the
round history. Without this, boards grow a parallel machine of review and
rework cards that eats a large share of machine time.

## RETURN on a PR: a rework card only when no card is in the review cycle

First check the board and the PR's events. If the PR already has a live card
in the review cycle, do NOT create a rework card: the verdict has reached the
executor through request_changes. Creating a parallel card makes the same fix
happen twice and get reviewed twice.

A rework card is needed only when the verdict lives only in a GitHub comment
and no card is in the cycle. Without a card, RETURN stops the pipeline: root
cards do not spawn themselves and nobody picks the verdict up. Even a
mechanical fix is a card, not a comment.

| finding | to whom | example |
|---|---|---|
| the code does the wrong thing | coder | a lost header, a global hook with side effects |
| the code is right, the check does not prove it | tester | green test that does not cover the claimed scenario; no live run; no visual proof |
| the plan itself is wrong | architect | a missing layer card, an unreachable goal |

Returning "the check does not prove it" to the architect is a wasted round.

## A deferred finding lives in the backlog, not in the verdict

1. Open an issue by the project's template with severity and awaiting
   triage, referencing the root issue. If the topic already exists, comment
   there instead.
2. Create a board card (`--triage`, linked to the root): the owner sets the
   priority.
3. The last line of the summary is the machine line
   `DEFERRED: t_xxxxxxxx, ...` or `DEFERRED: none`. The gate parses it.

## "Approved" is set by CI state, not by local checks

Before writing approved, run `gh pr checks <N>` and attach the result. Local
lint and tests are NOT the same: CI builds a different set (browser E2E,
coverage, registries) on a different commit. In one case the verdict said
"approved, lint clean, all unit tests green" while the E2E job was red on
exactly the render the PR existed for.

PASS goes only on a head where ALL REQUIRED checks succeed. The required set
is the base branch protection's required contexts:

    gh api repos/<org>/<repo>/branches/<base>/protection \
      --jq '.required_status_checks.contexts'

A red required check cancels PASS whatever the cause. "PASS on this PR's
diff" with a red required job is not a PASS: machines read the verdict as an
accept for the whole head and arm post-PASS work (WCFY, owner-ready signals)
for a head that is about to change. Wording on such a head: CHANGES REQUESTED
with a link to the red job, or for a stack "Verdict: blocked by #N". The word
PASS does not appear at all.

Verdicts and stack status never call update-branch, waiting for CI or a
rerun "owner-only": that is agent work done with the app's rights. Owner-only
means merge, or a decision with no technical path. Write stack status as
"done by the agent: ...; left to the owner: merge".

The report line "CI: N of M green" with the red ones listed is mandatory.

## The reviewer checks the owner summary in the PR body

Before ACCEPTED check that the PR body answers "what will change for the
user and how is it proven": behaviour before and after in product words. A
body without it is a blocking finding under pr-deliverable; approving
without it tells the owner the PR is ready when it is not.

## Before / After lives in the issue, not the PR

The owner wants two pictures, before and after, instead of a wall of text;
for non-UI work, a diagram of the delta. To save cost, this visual delta lives
in the ISSUE, at the prototype stage. The PR has no Before/After section; a
final screenshot from the E2E test is enough when a UI test runs.

The reviewer:

- missing section in the issue, or not first: `[BLOCK]`;
- UI change: OPEN both images and compare them with the diff. After shows
  exactly what the code changes, not an older head or another screen; Before
  is from the base branch; raw links return 200; frames differ by more than a
  timestamp. Images the reviewer did not open were not checked;
- non-UI change: the diagram shows the delta, not the whole system, with the
  changed part highlighted;
- the hygiene script checks the format; the reviewer checks that the
  MEANING of the picture matches the diff.

## A feature/ux implementation PR without "prototype accepted" is [BLOCK]

For an issue labelled feature or ux the first result is the prototype in the
issue's Before / After section. Before the verdict, open the issue the PR
references (`Refs/Closes #N`):

- no comment by `<owner>` saying "prototype accepted": `[BLOCK]`, however
  green CI is. A comment by anyone else does not count: the executor does not
  accept their own prototype;
- the implementation branch starts from the integration branch, not from the
  prototype branch; After in the issue shows what was implemented.

Grep the PR body for the issue number in ANY wording. A body that says
"Issue: #N" instead of `Refs #N` slips past machine gates; apply the gate
yourself and report the wording as a separate text finding.

The `## What changes for you` comment is published AFTER your PASS, once, on
the final head (pr-deliverable). Demanding it before the verdict creates
noise (several premature WCFY on one PR). After PASS, a missing WCFY on the
final head is an unfinished delivery step, not a review finding.

## Every WCFY promise on every entry kind

Each WCFY promise is checked by a live scenario on every kind of entry the
feature distinguishes (saved versus ad-hoc, keyboard versus click, first run
versus returning user). One kind proves nothing about the rest.

## UI layout in error, connected and empty states

A layout change is checked in the error, connected and empty states, not
only on clean load. In one case a fixed call-to-action region with no height
limit, after a failed connection at 1280x600, left 46px for the scroll area,
and the input that fixes the error was off screen.

## Data-moving or data-deleting code: enumerate entry paths

New code that moves or deletes user data is reviewed by listing the user
paths into that state, not only the ticket's scenario. In one case a mismatch
between an ad-hoc key and a saved id was reachable not only by re-saving but
also by clicking a saved item in the sidebar, bypassing the status check;
disconnect then moved the session's history into another bucket and deleted
the original, irreversibly.

## Third-party claims are checked at the primary source

User-facing text that states facts about other projects (an issue number, a
version, library behaviour) is verified at the source: `gh issue view -R
<org>/<repo> N`, release notes, the library's code. The link goes into the
verdict. In one case a product note about an upstream issue was rendered and
matched the PR body perfectly, but the upstream issue had been closed several
releases earlier and did not apply to the version the product shipped.

## Red CI does not mean broken code

Before creating work for a red job, check three things:

1. **can this change physically cause this failure** (a small dependency
   bump does not break a reactive proxy exchange);
2. **base history:** `gh run list --branch <base> --limit 6` and look for the
   same test;
3. **a local reproduction on THE SAME head** in a separate worktree.

A confirmed flake: rerun the job, no card. Not confirmed: the card says
exactly how the change could have caused it. Without that line, "fix the red
test" sends the executor searching blind.

## A deleted test always needs an explanation

    git diff --diff-filter=D --name-only <base>...<branch>

Each deleted test is explained in the PR description: moved, replaced by a
stronger one, or the covered behaviour was removed. A silent deletion looks
like an ordinary minus line. A common source: `git checkout <base> -- <path>`
during an experiment removes files that do not exist on the base. Apply the
same check to yourself: lines starting with `D` in `git status --porcelain`
go into the commit too.

## Repeat verdict: three hard rules

1. PASS after CHANGES REQUESTED only on a commit younger than that return. No
   commit after the return: the verdict is rejected automatically.
2. A verdict only on the PR branch head. A build with someone else's
   cherry-picks is not a check of the PR.
3. An open REQUEST_CHANGES from the reviewer blocks a QA PASS until a commit
   closes it.

## Stacked PRs

Before reviewing a stacked PR, check that every lower PR is reviewed and
approved, or merged. Otherwise the verdict waits: merging the upper PR would
pull unreviewed commits of the lower one. In one case a branch carried two
commits of a lower PR that had never been reviewed while the body said "part
1 landed"; the stack-hygiene rule existed only in pr-deliverable, which the
reviewer did not load.

## Reviewing a rebase: range-diff plus a mutation per conflict side

1. `git range-diff <old-base>..<old-head> <new-base>..<new-head>`: identical
   commits are marked `=`, different ones `!`. Any `!` outside the conflict
   region is an extra. Compare the PR file lists old/new: the sets must match,
   and test files must not disappear. Extras also come as symlinks: a
   `node_modules` symlink in a worktree gets committed because the ignore
   pattern `node_modules/` matches directories only. After any rebase in a
   worktree, grep the head tree for symlinks.
2. Check the conflict resolution in the FINAL file: both sides present, no
   duplicates (absorbing a base line into the branch's cleanup block is fine
   if the side's intent survives).
3. A mutation on EACH conflict side: remove side A's absorbed lines and see
   side A's tests go red; break side B's guard and see side B's tests go red.
   A green run without mutations proves nothing.
4. A PASS on a rebased, previously approved head is a review of promises (see
   above). Conflict-side mutations do not replace promise mutations.
5. A negative test must be able to fail: check that its scenario reaches the
   branch under test. In one case a "never records" test sent a notification
   without an id; the server answered 202 before the interception code ran,
   so the test could never turn red.

## Reviewing ops changes in ~/.hermes: the delivery channel matters more than the function

1. Compare `metadata.changed_files` with `git show <sha> --stat`. The claimed
   commit may not contain the main file (edited in the working copy and never
   committed). A false SHA in a handoff is fixable in minutes and is RETURN:
   machine consumers will follow a pointer to a commit without the change.
2. "Live run confirmed" is checked together with the delivery channel: fresh
   files in `cron/output/<job-id>/` of the production job and their status.
   A function can honestly return a finding while the hourly job crashes on
   another board entirely; `script failed` in the last output is a fact
   against "the report is confirmed".
3. Date authorship in a dirty working copy by the executor profile's
   `state.db`: messages around the first write to the file. Separate the
   executor's edits from others' before the verdict.

## Checklist

- the issue body starts with `## Before / After`, images or diagrams
  compared with the diff (missing section: [BLOCK]);
- feature/ux: the owner's "prototype accepted" comment exists in the issue
  (missing: [BLOCK]);
- every published claim has proof;
- the report has "CI: N of M green";
- a diff that models a third-party library's output: checked against the
  library's code, `Library contract` line and a run-produced fixture;
- every blocker treated as closed has a mutation of its exact fix line;
- every PR-body and WCFY promise has a pinning test proven by mutation;
- repeat review closed earlier findings and rechecked changed behaviour live;
- PASS after CHANGES REQUESTED references a younger commit;
- the verdict is on the branch head; no QA PASS over an open
  REQUEST_CHANGES;
- stacked PR: lower PRs reviewed or merged;
- non-blocking findings filed as issue plus card, DEFERRED line in the
  summary.
