# debugging-discipline: lessons

Distilled debugging lessons from worker runs, each as rule, cause and
application. They cover waiting for CI, retriggering runs, secrets in debug
output, finding the right log, control experiments, timing tests, reading
resource numbers and ERROR lines, and the INTENT check before changing
behaviour.

**Wait for CI with one blocking command, not a polling loop.** Why: a card spent its budget on nine identical `gh run list` calls and several sleeps after the real work was pushed. Apply: `gh run watch <id> --exit-status`; for the card author, "wait for green CI" is an expensive criterion: close with the push report and leave the run check to a cheap next step.

**Retrigger a run with `workflow run`, never by closing and reopening a PR.** Why: after an amend and force-push no run appeared and a worker closed and reopened the PR, an outward action that notifies people and can confuse review state. Apply: check `gh api repos/<org>/<repo>/commits/<sha>/check-runs --jq .total_count`; then `gh workflow run <workflow> --ref <branch>` (if it has workflow_dispatch) or `gh run rerun <id> --failed`. A force-push after amend does not always emit a pull_request event.

**A debug line never prints a secret.** Why: a debug patch printed a whole access token at WARN; diagnostic lines reach PRs more often than expected. Apply: use a non-sensitive signal (length, hash, `identityHashCode` of the holder object); if the project logs no such values anywhere, adding the first is a separate decision.

**Hermes logs are split by component: grep the directory, not one file.** Why: an hour went into "the plugin does not load" while its lines were in agent.log, not gateway.log; a gateway restart was requested for nothing. Apply: `grep -rn "<string>" ~/.hermes/logs/` before concluding "no such entry".

**The first step on any breakage is a control experiment.** Why: ninety minutes went into fixing a helper because the card title blamed a new flow; reverting to the old flow took 25 seconds and failed on the same line. Apply: remove the suspected factor and see whether the symptom changes. Edits that leave the error message unchanged line for line mean you are fixing the wrong thing. The diagnosis in a card is a HYPOTHESIS even if a maintainer wrote it. If a green test requires removing the tested behaviour, the hypothesis is wrong, not the behaviour.

**A dead hypothesis sends you back to the log, not to the next candidate.** Why: three operators in a row were blamed by their semantics while the real exception was printed forty lines above the assertion in the same CI output; a full run and a day of red PR were lost. Apply: until the explanation contains a log line with an exception name, there is no explanation.

**The experiment tests what was built, not what you wrote.** Why: a component was removed, the run stayed red and the hypothesis was dropped, but the build was never checked and the run may have used an old artifact. Apply: rebuild the changed module and its in-repo dependencies, look for the success line explicitly (`-q ... | tail` hides compile errors); both conditions or the negative result proves nothing.

**Never push a branch that does not build.** Why: code restored from an abandoned worktree was pushed unbuilt, collided with an existing method, every CI job failed in under a minute and a neighbouring card on the branch blocked after three failures. Apply: compile, and compile tests if you touched tests, before `git push`; moved code is new code.

**A timing test does not wait for the real timeout.** Why: a correct test waited a real one-minute timeout, adding minutes of CI on every push. Apply: use the framework's virtual time or an injected clock; real waits belong in e2e; watch per-class elapsed time in test reports like you watch test counts.

**Swap usage is not memory pressure.** Why: an alert on "swap above 3 GB" fired while paging was near zero, I/O wait was zero and gigabytes were available; swap held cold pages of idle tools. Apply: `vmstat 2 5` (si/so, wa) and `free -m` (available); alert on low available memory, high paging or high I/O wait, not on occupancy.

**An ERROR line is not a failure until the operation's outcome is checked.** Why: a dozen quota errors were analysed through six disproven hypotheses while the status command showed the operation succeeded minutes later after retries. Apply: find the outcome record first (`hermes kanban runs <id>`, the subsystem's status command, `gh pr checks`, an exit code); a provider's quota error text may mean per-request throttling, not account state.

**A silent `if (el.exists())` in a test is a future false lead.** Why: a click was silently skipped and the next step failed with a message pointing at the wrong element. Apply: assert required elements unconditionally; when reading someone's failure at step N, verify steps before N really ran.

**No half hour of reading between two runs.** Why: one card spent five minutes in ten test runs and almost an hour re-reading one huge file piece by piece. Apply: at most two or three file reads between an edit and the next run; search code with `serena-lsp`; do not start by hand what the test runner starts itself; edit and run in the same tree.

**The laziness ladder: take the first rung that holds.** Apply: does it need to exist, is it already in the repo, does the standard library do it, does the platform or database cover it, does an installed dependency solve it, one line, minimal code. The ladder shortens the solution, never the reading; never simplify away trust-boundary validation, data-loss error handling, security, accessibility or anything the owner asked for explicitly.

**INTENT: name three sources before changing behaviour.** Apply: write `INTENT: the code does <X>; the card or failing test expects <Y>; the spec says <Z>`, with Z filled only after opening the document. If they disagree, do not edit: the disagreement is the finding, report it on the card. Authority order: explicit owner instruction, spec, tests, current code; "make the tests pass" is not permission to edit the test instead of the code.

**A test stub server must not serialise exchanges it is meant to run concurrently.** Why: an embedded HTTP server with a single dispatcher thread let a parked streaming request starve every following request, and a test passed vacuously because the checked path never ran. Apply: give test stubs a thread pool, and make a refutation probe really create the concurrency (check the stub's own logs that both exchanges reached their handlers).
