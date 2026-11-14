---
name: fix-at-the-root
description: Fix the root of a defect and find every sibling place.
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [bug, refactor, serena, scope]
    related_skills: [no-duplicate-work]
---

# The root and its siblings

This skill is the mandatory procedure for any defect fix: find where all
callers converge and fix it once there, name the faulty construct as a
pattern, search the whole repository for it, and report the result in a fixed
SIBLINGS line. A card names a symptom, not a place.

1. Before editing, find every consumer of the function you are about to
   touch: `serena__find_referencing_symbols`. Fix once, where all callers
   converge. One guard in the shared function is a smaller diff than a guard
   in every caller, and patching only the path the card names leaves the
   neighbouring calls broken.
2. After the fix, name the exact faulty construct as a pattern, not in words:
   a missing null guard before `.get()`, the same wrong date format, the same
   call without a timeout.
3. Search the whole repository with that pattern:
   `serena__search_for_pattern` over the source tree, including tests and
   vendored modules.
4. Put exactly this line into the card summary and the PR body:

   `SIBLINGS: searched <pattern>, found <N> other places: <files or "none">`

   Fix what you found in the same PR if it is the same root and within the
   card's scope; otherwise file a separate card and reference it in the same
   line. Staying silent about findings is not allowed.

A claim "fixed everywhere" without step 3 is void.

The same defect has a review-level form: the path named in the blocker is
fixed, while a neighbour with the same mechanism is left (a second poller, a
second stack, a second transport, a subclass of the caught exception). When
answering a review, list ALL places with the same mechanism and what happened
to each. Full candidate list: pr-deliverable, references/lessons.md, the lesson on
finding every place with the same mechanism after rework.

**Cost of the mistake:** the same defect returns as a neighbouring card a week
later, and the full worker, review, merge cycle is paid a second time.
