# tester-verdict-checklist: distilled lessons

These are the tester's lessons reduced from an incident log to one entry
each, in the form "Rule. Why. How to apply." They cover the order of checks
inside a test, how to state test-count criteria, how to find work that was
claimed but never committed, the shapes of tests that cannot fail, how to
prove a flake, and the test pyramid contract.

1. **Cheap checks come before expensive UI waits.** Why: a 30-second wait
   that fails says only "did not arrive"; the assertions on stand counters
   after it, which name the cause, never run. How: put assertions on stand
   state and counters above long `shouldBe(..., timeout)` waits.
2. **Expected test counts are measured on the base, not fixed in a card.**
   Why: a number written into a plan rots with the first new test and every
   round goes into recounting. How: run the same job on the PR base and on the
   branch, show both columns (run, failures, errors, skipped); criterion:
   zero failures and errors, no fewer tests than the base, skips no wider than
   the base, every base skip named with its reason from the code.
3. **Claimed work is looked for in commits before the tree.** Why: a handoff
   "implemented, N tests green" can describe an uncommitted diff in another
   working tree; QA then burns its budget building a stand for code that does
   not exist. How: `git log --all --grep <marker>`, `git grep <symbol>` over
   every remote branch, `gh pr list`; found only in a working tree: save the
   patch, create a coder card "ship the orphaned work", verdict FAIL for that
   criterion.
4. **A test that is green with the bug reverted and with the UI removed
   tests nothing.** Why: the shapes repeat. How: before PASS run the test on
   the base without the fix AND with the UI part cut out; both must be red.
5. **A feature is not switched off in tests.** Why: a feature disabled in
   all E2E runs "because it kills sessions" hid a real defect that took
   minutes to find. How: disable a feature in tests only after proving a
   defect of the feature; then fix the feature.
6. **A mutation must not be covered by a neighbouring rule.** Why: a check
   for one injected character was also caught by a ban on spaces, so the
   check itself was never tested. How: mutate the bare construct with no
   neighbouring protection.
7. **A hung mutant is not a killed mutant.** Why: a run that hangs was
   recorded as a kill. How: record a mutation result only from a finished run
   with its exit code.
8. **An assertion right after an async dispose is a test defect, not a
   flake.** How: wait for the condition with a timeout.
9. **A negative assertion needs a grace window longer than the hostile
   path's trigger time.** Why: "X did not happen", checked instantly, passes
   before the asynchronous failure happens; a `waitFor(expect(x).toBeUndefined())`
   is true on the initial undefined. How: poll the negative condition over the
   whole period the hostile path could take (plus margin), or assert over the
   full history after async work settles.
10. **A "flake" is proven, not named.** Why: a regression with one failure
    signature across dozens of tests was once called browser drift while
    other branches on the same base were green. How: two facts are required: a
    rerun of the same commit, job and runner that differs, and a green
    neighbouring branch on the same base and environment that day. The
    verdict names the number of failures and the common signature. These two
    facts prove instability only; calling it "noise" also needs the
    mechanism triage (a)-(c) from SKILL.md: durations against known numbers,
    a neighbouring stack in the same run, step timestamps inside the test.
11. **The test pyramid is checked, and the integration layer is mandatory.**
    How: check the split by layer (roughly 80/15/5); an affected endpoint or
    use case without an integration test is RETURN.
12. **The test-infrastructure contract from the plan is checked by
    machine.** How: attach output that the file glob exists, a grep finds the
    infra signature, and the runner command is green with more than zero
    tests. "Checked" without output does not count.
13. **A red-first card keeps its red test first.** How: for a card at the
    red-test stage, the disabled red test is the FIRST commit of the branch,
    re-enabling it lands with the fix, and the test file appears in
    `git diff --name-only`.
14. **"N tests red on the old base" counts only positive pins.** Why: a
    negative guard (asserting something is absent) stays green on old code,
    so counting it makes the red count wrong. How: report negative guards
    separately from the red count.
15. **A mutation probe with test failures ignored reports success anyway.**
    How: read the verdict only from the test report files; build success
    with no report directory means zero tests ran.
