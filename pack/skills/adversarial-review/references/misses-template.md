# Reviewer misses journal: format and use

This document explains the misses journal: what counts as a reviewer miss,
which classes of misses exist, the fields of an entry, how the journal is
filled, and how the reviewer reads it before every verdict to calibrate. The
pack does not ship a real journal; start an empty `misses.md` next to this
skill (the reference setup keeps it at
`~/.hermes/shared-skills/adversarial-review/misses.md`) and let the watchdog
and the owner's reviews fill it.

## What a miss is

A miss is the reviewer's PASS (or ACCEPTED) on head sha X, after which the
owner requested changes on the same sha or the next one. The pair window is
deliberately narrow: wider pairs cannot be proven to be the reviewer's fault.
If the reviewer itself returned the PR between its PASS and the owner's
CHANGES REQUESTED, there is no miss: the reviewer's last word was a return.

## Who writes it

A watchdog script (in the reference setup `scripts/reviewer-misses.py`)
scans PRs, pairs reviewer verdict comments (first line `## Review verdict` or
`## Re-review verdict`, head sha in the body) with the owner's formal reviews
in state CHANGES_REQUESTED, and appends one entry per pair, deduplicated by
PR plus verdict sha. The class is first guessed by the script and then
calibrated by hand from the owner's review text. The same scan rewrites a
side file (`misses-passes.json`) with all reviewer PASS verdicts, which gives
the denominator for the miss-rate metric: misses / PASS verdicts per week.
The metric's goal is to fall two weeks in a row.

## Classes

Each class names a kind of check that once failed. The reviewer closes every
applicable class in the `## Calibration` block of each verdict.

| class | the check that failed |
|---|---|
| contract | the issue's acceptance criteria or the declared contract were not all verified on the head |
| mutation | a fix or promise had no test that a mutation of its exact production line turns red |
| ui-input | UI was not exercised with real input (hit-testing, overlays, clickability) |
| ui-states | the screen was not checked in error, connected and empty states |
| stack | a lower PR of the stack had no review |
| pr-texts | the PR body, WCFY or other delivery text did not match reality |
| guarantee-transfer | a rework moved a guard or check to another layer, deleted the old one and rewrote its test, and nothing pins the new wiring |
| library-contract | modelled third-party output was not checked against the library's code |

Add classes when the owner's reviews show a new recurring kind of miss; keep
the list short enough that every verdict can address it.

## Entry format

The format is machine-readable: change it only together with the watchdog.

    ### #<PR> <sha7> <verdict date> <class>
    - pr: <N>
    - sha: <sha7>
    - verdict: PASS <date time> (issuecomment-<id>)
    - owner: CHANGES REQUESTED <severity> <date time> (review-<id>)
    - found: <what the owner found, one line>
    - class: <class>
    - missed: <what exactly the reviewer skipped, one line>
    - source: <backfill YYYY-MM-DD | watchdog YYYY-MM-DD>

## How the reviewer uses it

1. Before every verdict, read the last 10 entries.
2. For each class that applies to the current PR, do the check on the
   current head and write one line in `## Calibration` stating what you
   checked and how. "N/A" requires a reason.
3. A class line without the real check behind it will reappear as the next
   entry. The journal is a mirror, not a punishment: its value is that the
   same miss is not made twice.

## Example entries

    ### #42 a1b2c3d 2026-01-12 mutation
    - pr: 42
    - sha: a1b2c3d
    - verdict: PASS 2026-01-11 21:04 (issuecomment-1001)
    - owner: CHANGES REQUESTED normal 2026-01-12 08:30 (review-2001)
    - found: [BLOCK] removing the generation check after the async auth call keeps all tests green
    - class: mutation
    - missed: the fix line of a blocker closed in round 1 was never mutated; only the round-2 fix was
    - source: watchdog 2026-01-12

    ### #57 d4e5f6a 2026-01-20 pr-texts
    - pr: 57
    - sha: d4e5f6a
    - verdict: PASS 2026-01-19 17:40 (issuecomment-1002)
    - owner: CHANGES REQUESTED minor 2026-01-20 09:15 (review-2002)
    - found: WCFY promises a settings tab that the UI never renders
    - class: pr-texts
    - missed: WCFY was compared with the diff, not with the running UI
    - source: watchdog 2026-01-20

    ### #63 b7c8d9e 2026-02-03 ui-states
    - pr: 63
    - sha: b7c8d9e
    - verdict: PASS 2026-02-02 22:10 (issuecomment-1003)
    - owner: CHANGES REQUESTED normal 2026-02-03 08:05 (review-2003)
    - found: [BLOCK] after a failed connect the fixed footer leaves 40px of scroll; the URL field is off screen
    - class: ui-states
    - missed: the layout was checked only on a clean load, not in the error state
    - source: watchdog 2026-02-03

    ### #70 f0a1b2c 2026-02-10 guarantee-transfer
    - pr: 70
    - sha: f0a1b2c
    - verdict: PASS 2026-02-09 19:55 (issuecomment-1004)
    - owner: CHANGES REQUESTED normal 2026-02-10 08:40 (review-2004)
    - found: validation moved to the parent handler, the child's guard and its test were removed; wiring the child to the raw handler keeps the suite green
    - class: guarantee-transfer
    - missed: nobody mutated the new wiring after the guard moved layers
    - source: backfill 2026-02-10
