---
name: issue-mirror
description: "A card's issue mirror: the gate, the content, the closing."
version: 1.0.0
author: maintainer, Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [github, issue, kanban, owner, evidence]
    related_skills: [decomposition-contract, pr-deliverable]
---

# A card's issue mirror

Every product card on the board is mirrored by a GitHub issue, and a card
without a mirror does not go into work. This skill covers the gate before
decomposition, what the issue must show the owner (above all the Before /
After section that opens its body), how the hygiene script interferes with
closing, and the checks before an issue is closed. Lessons on prototypes,
owner verdicts, deduplication and closing by acceptance criteria are in
references/lessons.md.

## When to use

Before decompose, when creating a product card, when closing a chain.

## Gate before decompose

1. Check that the parent card has an issue mirror.
2. If not, first SEARCH for an existing one (by `t_<id> in:body` and by
   keywords, including issues opened by people) and reuse it.
3. If none is found, create one by the project's template, in the
   repository's language, and put its URL into the card body.
4. At decompose time remove the awaiting-triage label from the issue
   (`gh issue edit N --remove-label <triage-label>`): human approval has been
   given.
5. Put the issue number (`#N`) into the body of EVERY child card: the coder
   must write `Refs #N` (several PRs) or `Closes #N` (a single PR) in the PR
   body.
6. Subscribe the owner to notifications for every child card you create.

## The issue is a showcase, not a ticket

The issue answers the owner the same question as the PR, but earlier: what
they will get and what it will look like. Besides the template sections (for
example Current behavior / After the change / User benefit / Acceptance
criteria) the issue shows proof that the idea works BEFORE the owner reads
code:

- UI: a picture of the prototype or the built change (PNG on the assets
  branch, embedded as an image), next to a picture of current behaviour;
- non-UI: a short working example, the call and its real output, not an
  invention;
- one line "what changes for the user" in product words.

An empty promise ("will be fixed") is a defect: either the proof is there or
the issue says why it cannot be obtained before implementation.

### The Before / After section comes first

The body of every issue starts with `## Before / After`, first, before any
text. The visual delta lives ONLY here, at the prototype stage; the PR has no
such section (see pr-deliverable). At the task level, not the code level:

- UI task (a real visible change on screen): Before is the current screen,
  After is the prototype of the desired one (a DOM edit of the live UI, or a
  render of a mock-up, captured as a screenshot); a two-column table, PNG on
  the assets branch, raw links;
- non-UI task (a small bug, documentation, refactoring): a simple mermaid
  diagram of 5 to 10 nodes of what changes; for a bug, where it breaks and
  what the fix changes. The delta, not the whole system;
- the AUTHOR of the issue writes it at creation, in the body, without waiting
  for review;
- after the section at most 5 lines in the user's words, everything else in
  `<details><summary>Details</summary>`.

Two hard rules:

- RENDERING TEXT INTO A PNG TO PASS A CHECK IS FORBIDDEN. No real visible
  change on screen means a diagram, not pictures.
- FILE PATHS IN THE DIFF DO NOT MAKE AN ISSUE A UI ISSUE: component or style
  edits without a visible change are non-UI and get a diagram.

The hygiene script opens a visual-delta card only when the section is
missing entirely; a malformed section stays a report finding without a card.
Capture details and traps: pr-deliverable, references/visual-delta.md.

## Conflict with the hygiene script

The hygiene script (in the reference setup `scripts/pr-hygiene.py`, run by
cron) closes an issue by the heuristic "all PRs merged, none open". It does
NOT check acceptance-criteria checkboxes, the board, or the closing order
below, so it can close an issue too early. Live with it like this:

- add the `keep-open` label ONLY when the issue is deliberately wider than
  what is being merged (for example a sibling PR of the same work is not yet
  merged);
- close all other issues by hand, by the closing steps in
  references/lessons.md, BEFORE the script fires; check open issues with
  merged PRs daily;
- if the script closed an issue too early while criteria are unmet: reopen
  it, uncheck the unmet criteria, and comment why it was reopened.

## Check

- the issue body starts with `## Before / After` (UI: a table of two
  screenshots; non-UI: a mermaid delta);
- the root card body contains the issue URL;
- every child card body contains the issue number;
- the issue contains proof, not a promise;
- the awaiting-triage label was removed at decompose;
- before closing: every acceptance criterion is met and proven, the board
  has no live cards for it, the last comment does not contradict closing, and
  the closing comment lists the PRs and links to the proof.

## References (read on demand)

- references/lessons.md: feature/ux prototypes and the blocked
  implementation card; owner verdicts as comments and formal reviews;
  deduplication before creating a card from a verdict; closing by acceptance
  criteria, not by PRs
