---
name: no-duplicate-work
description: Check the work is not already done before starting a card.
version: 1.0.0
author: maintainer, Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [kanban, git, duplication, blocking]
    related_skills: [kanban-discipline, fix-at-the-root]
---

# Do not do the work twice

This skill is the first thing a worker runs after taking a card: three cheap
checks that the work is not already in the branch, not owned by another open
card and not colliding with an open PR, plus how to re-verify review findings
against a moved PR head and deferred cards against merged work. Typical
waste it prevents: a card repeatedly hitting its runtime limit rewriting
findings a commit had closed an hour earlier; a gate filing three rework cards
for one hole; a red-test card waiting in the queue after the test went green
in CI.

## When to use

Always, as the first action after taking a card, before the first file edit.

## The check, three commands

```bash
# 1. Is the work already closed in the card's branch
git log origin/<branch> --oneline -10
git show <suspicious commit> --stat

# 2. Is there another open card with the same goal
hermes kanban --board <board> list --json \
  | python3 -c "import json,sys;[print(t['id'],t['status'],t['title']) for t in json.load(sys.stdin) if t['status'] not in ('done','archived')]" \
  | grep -i "<keyword from your card>"

# 3. Does an open PR touch the same files
gh pr list --json number,files --jq '.[] | "\(.number) \([.files[].path] | join(" "))"' | grep <file>
```

## What to do with the result

**The work is already in the branch.** Do not redo it. Complete the card with
proof: the commit hash, `git show --stat` output, the green CI job. Phrase the
result as: what was claimed, what closed it, what remains and on which card.

**Another open card has the same goal.** Do not start. Wait on it:

```bash
hermes kanban --board <board> link <that card> <yours>
hermes kanban --board <board> block <yours> "Waits for <that>: overlap on <what exactly>" --kind dependency
```

A `dependency` block releases itself; an explicit `blocked` only by hand (see
kanban-discipline for the canonical form).

**An open PR touches the same files.** Build your branch on top of that PR
instead of the base branch, and treat it as a stacked layer (priority 60, see
kanban-discipline).

## A review belongs to the head it was written on

Owner-review findings describe the code of the head they were written on. If
the PR head moved after the review (parallel block cards), check each finding
against the current head, not the card text: `git show <review head>:<file>`
against `git show HEAD:<file>`. In practice, three of five findings were
already closed by earlier rework, and editing blindly by the review text would
have reverted fixed code. Still pin an already fixed item with a test if none
exists: the review demands "fixed with a test", and a test on fixed code is
cheap.

## A deferred card from an issue: the gap may have closed by a merge

A card born from an issue (deferred, follow-up) carries facts as of the
analysis date: heads, surviving mutants, missing tests. By the time it runs,
paired PRs may have merged; with several PRs per issue it does not close by
itself (Refs instead of Closes), and the card describes a hole that no longer
exists.

First step on such a card: check the facts against current heads, not write
the work again. Look for the target pinning tests on the current base heads
(`git show origin/<base>:<file> | grep`) and the state of the PRs from the
issue (`gh pr view N --json state,mergedAt`). Tests in place: run the kill
check on the current head (re-applying the mutation from the issue must turn
red), run the full gates, and close with proof and no new code.

## What the check does not catch

There may be no textual conflict and still a semantic one: one branch
annotates twenty tools, another expects twenty-one. Before merging a pair of
PRs that touch shared files, compare numbers and contracts, not only the
output of `git merge-tree`.
