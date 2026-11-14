---
name: save-lesson-before-complete
description: Where to record what you learned before closing a card.
version: 1.0.0
author: maintainer, Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [memory, skills, learning, kanban]
    related_skills: [kanban-discipline, worker-run-discipline]
---

# Save the lesson before closing the card

This skill covers the explicit step every worker takes before closing a card:
deciding whether it learned something worth keeping, choosing the right
knowledge layer for it (profile memory, a shared skill, a project skill or the
project conventions), keeping memory within its limit, snapshotting the
knowledge layer when it changed, and reporting who did what from data.

## When to use

Before every `kanban_complete` or `kanban_request_review`.

## The question to ask yourself

Did I learn something in this task that will save time in the next one? If
not, write nothing: empty entries clutter memory and push out useful ones.

## Where it goes, by the nature of the knowledge

| What you learned | Where | How |
|---|---|---|
| an environment fact or trap: a path, a command, a tool's behaviour, a config pitfall | profile memory | `memory` add, ONE short line |
| a universal procedure that works on any project | shared skill in `~/.hermes/shared-skills/` | `skill_manage` |
| a procedure of one project: its build, demo stand, release ritual | project skill in `~/.hermes/shared-skills/<board>/` | `skill_manage` |
| a project fact or rule: branches, runners, paths, agreements | project conventions, NOT memory | a proposal comment on the card |

Check before writing: "will this be true on ANOTHER project?" No: the target
is project-level, not profile memory. A project fact in profile memory becomes
a lie on the next project.

## What never goes into memory

The Hermes docs list it explicitly: card content, statuses, anything already
in SOUL or the project conventions, easily rediscovered facts, large chunks of
code and logs, secrets.

## Memory is limited and does not compact itself

The profile limit is `memory.memory_char_limit` characters. On overflow the
tool returns an error listing the current entries; it does not drop the entry
silently. Then consolidate in the same turn: `replace` merges overlapping
entries, `remove` drops a stale one, and only then repeat `add`.

One entry is one densely packed fact. Good: "Staging server listens for SSH on
2222, key ~/.ssh/staging_ed25519". Bad: a retelling of the task or "the user
has a project".

## Snapshot the knowledge layer if the card touched `~/.hermes`

If `~/.hermes` is kept under git with a snapshot script (for example
`teach-snapshot.sh`), a card that changes tracked files there (scripts,
shared skills, project conventions, profile memory, config.yaml) must record
the layer before `kanban_complete`:

1. `git -C ~/.hermes log -1 --format=%h` (hash "before");
2. `~/.hermes/scripts/teach-snapshot.sh "<card id>: <what changed>"`;
3. the hash again: changed means a snapshot exists, its hash goes into the
   summary. Unchanged: the summary says "no snapshot taken", never the old
   hash.

Otherwise working edits hang uncommitted and the snapshot lies about what is
in effect. A sweep snapshot picks up ALL pending edits of the layer, including
other cards'. If a pre-commit gate rejects a line (for example a forbidden
character in someone else's edit), fix the line by meaning; never bypass the
gate.

## Check

- the lesson question was asked before closing the card;
- if there is a lesson, it was written BEFORE `kanban_complete`;
- the entry passed "is this true on another project";
- a card that changed `~/.hermes` closed with a snapshot and its hash in the
  summary.

## Report who did what from data, not from guesses

Before writing "I did X" or "the owner did X" in a report, check the author
and time of the action:

- GitHub: `gh api repos/<org>/<repo>/issues/<N>/timeline` (closed, committed,
  commented events);
- Git: `git log --format='%an %ae %ad' -1 <sha>`;
- Board: `hermes kanban log <id>`.

Never attribute your own actions to the owner or the other way round. If the
timeline author is `<bot-account>`, a bot closed it through a hook, not a
human.

## References (read on demand)

- references/lessons.md: why this is your explicit step and not background
  magic; check a culprit hypothesis against ALL facts; "done" only with a link
  to the artifact
