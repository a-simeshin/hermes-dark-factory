# adversarial-review: distilled lessons

These are the lessons a reviewer learned the hard way, reduced from a long
incident log to one entry each in the form "Rule. Why. How to apply." Read
them when a review touches the area; the misses journal (misses-template.md)
is the companion that tells you which of them you actually failed recently.

1. **Pin the content source of a plan.** Why: "final content = donor branch
   HEAD" silently pulls in features merged after the reference point. How:
   demand a pinned merge SHA or a per-file decision before the verdict.
2. **Measure the blast radius of a semantic flip.** Why: changing behaviour
   breaks base-branch tests outside the plan's inventory, so "inventory only"
   and "CI green" contradict each other. How: grep the base tests for the old
   semantics; a contradiction in the criteria is CRITICAL.
3. **Re-measure your own numbers from round 1.** Why: a revision copies your
   anchors as verified and your error becomes the plan's error. How: in round
   2 measure both the author's claims and yours; a mismatch with your own past
   text is an erratum inside ACCEPTED, not a reason for round 3.
4. **Mutation batteries run in a project worktree, one runner at a time.**
   Why: scratch directories get cleaned under you; concurrent builds share
   caches and fail spuriously. How: worktree under `<repo>/.worktrees/` with a
   task-prefixed name, check it exists before each run, logs outside it, one
   build at a time, kill stale background children before a restart.
5. **Read the last 10 misses before every verdict.** Why: the same misses
   repeat when they are not written down. How: close each applicable class in
   the `## Calibration` block (see misses-template.md).
6. **Compare with the live reference, not a snapshot.** Why: a board
   attachment can be stale while the live comment was corrected; "byte-equal
   to the reference" then ships dead links. How: check the snapshot against
   the live source first, then the branch file against the live source.
7. **Look for the issue number in any wording.** Why: gates link PRs to
   issues only by `Refs/Closes #N`; "Issue: #N" slips through. How: apply the
   gate yourself and report the wording as a text finding.
8. **Headless browsers lie in three known ways.** Why: resource timing is
   cleared and a `fetch` captured at module load bypasses late wrappers;
   `el.blur()` moves focus without firing the framework's onBlur; a number
   input turns `+30` into `30` before any validator. How: wrap
   `fetch`/`EventSource` before the click or take wire proof from the server
   log; confirm a handler ran before blaming the code; unit-test validators on
   a discriminating input (for example `1e2`) so their mutant dies.
9. **Library output is checked against the library's code.** Why:
    hand-written fixtures encode the same guess as the implementation, so
    green tests prove self-consistency only. How: open the library source or
    bytecode yourself, compare the modelled shape (including cause-chain
    depth), require a `Library contract: <artifact>:<version> <Class>#<method>`
    line and a fixture produced by running the library; add a Calibration line.
10. **The verdict covers the whole head with its CI.** Why: "PASS on this
    diff" with a red required job arms post-PASS work on a doomed head. How:
    PASS only when every required check is green; otherwise CHANGES REQUESTED
    or "blocked by #N".
11. **A lock moves the cost of a race, it does not remove it.** Why: wrapping
    a hot path in `synchronized` can serialise full-collection scans on every
    request. How: ask how often the method runs, what is inside the critical
    section, and whether a standard structure (for example an LRU map with
    eviction built in) gives the property for free.
12. **Domain knowledge in code is checked against the outside source.** Why:
    expectations copied from the code's behaviour test the code against
    itself, and data can be right while the conclusion drawn from it is
    inverted. How: open the spec or changelog, compare tables line by line
    (including completeness), and separately check the DIRECTION of every
    inference. Tell-tale sign: the test's expectation uses the same words as
    the implementation.
