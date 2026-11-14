# debugging-discipline: full protocol

This reference expands the debugging-discipline skill with the commands and
the failure behind each rule: reading the failed CI log first, closing CI
criteria only on a green run, reading screenshots before code, logging at
layer boundaries, explaining an empty log, converting log timestamps, keeping
`pgrep` from matching itself, and running the real CI phase locally. Every
rule here cost concrete hours.

## When to use

A test is red, CI is red, "nothing happens", or behaviour differs from the
expectation.

## An order, not a bag of tricks

1. red CI: the command first, hypotheses afterwards;
2. there is a failure screenshot: look at it before the code;
3. symptom "nothing happens": log at the boundaries between layers;
4. every hypothesis is checked by a control experiment before editing;
5. an experiment counts only if the change reached the build and the build
   succeeded.

## Why CI is red: the command first

```
gh pr checks <PR>   # take the run-id of the red job
gh run view <run-id> --log-failed
```

`--log-failed` prints the steps of the failed jobs with their logs. Read it
FIRST, before any guess.

The main thing it shows: **a failure within 20-30 seconds is build, style or
configuration; tests did not run in such a run at all.** Fixing a test at that
moment is pointless, its colour means nothing.

Failure mode: five hours and seven runs went into the cause of an e2e failure
while the branch build failed in 20 seconds on a style-checker rule and no e2e
suite ever started.

A green neighbouring job is not evidence until you have checked what it ran.

## A CI criterion closes on a green run, not on reasoning

If an acceptance criterion mentions CI, a required job or "make it green", the
card closes ONLY after you have seen a green run yourself: `gh pr checks <N>`.
A local run and "the rest should work" are not proof.

Failure mode: a card closed with "only a selector fix in one test was needed"
while the required job still had six failures; the PR stayed red an extra day
and a second card was needed for the same work.

## The failure screenshot is read first

If a test left a screenshot (browser test frameworks usually store it next to
the report, with the path in the error message), open it with
`vision_analyze` BEFORE reading the code and the diff.

Failure mode: four hours went into hypotheses from the diff. The screenshot
dismissed two of them in a second: the history panel showed that the request
had never been sent at all (everyone was looking for why the answer did not
arrive), and a form stood with a validation error and empty fields.

A DOM dump shows markup. A screenshot shows the state as a human sees it.

## "Nothing happens" is cured by logging at the boundaries

Do not build hypotheses from the diff. Put one debug line at each boundary
between layers (event handler, function, transport, network, server) and run
once. The place after which no lines appear is the break.

Failure mode: five hours of hypotheses from the diff, all discarded. Three
tagged debug lines in the client's request path showed in one run that the
request went out and no answer came back, narrowing the area to the path
between client and server. Remove debug lines before committing; keep the
technique.

Before concluding anything from an empty indicator (history panel, counter,
metric), find out when it is written. A history panel that records COMPLETED
requests does not show a hanging one, so "history is empty" does not mean "there
was no request".

## Explain an empty log before drawing a conclusion

Before concluding "execution never got here", check that this code logs at the
chosen level at all:

```
grep -cE "(LOG|log|logger)\.(debug|trace)" <file>
```

Failure mode: TRACE on a package gave silence, while the class in question
contained no debug or trace statements, only a handful of warnings. The
silence meant nothing.

When your code is silent, go up to a layer that always logs: the web
framework's request dispatcher at DEBUG prints every HTTP request the
application receives. That showed in one run that a proxy received six
messages from the browser and forwarded only four.

## Hermes log timestamps are the server's local time, not UTC

A timestamp like `00:26:56` in gateway.log is not 00:26:56Z if the server runs
in another time zone: the gateway writes the machine's local time. Before
declaring "the log was silent for N hours", convert the timestamps to UTC and
compare with the time zone of the event you were looking for.

Failure mode: "the log has been silent for four hours" was declared while the
entries in question were exactly the review webhooks being searched for, four
hours off by time zone. The whole check rested on a silence that did not
exist.

## A `pgrep` check must not find itself

A command that contains the search pattern in its own command line appears in
its own output. A wait loop "until there are zero processes" with such a
pattern never ends: it always sees at least itself.

```
# Trap: counts itself
until [ "$(pgrep -cf 'worker pattern')" = 0 ]; do sleep 5; done

# Correct: pattern in a variable, wrappers filtered out
PAT="worker pattern"
while :; do
  n=$(pgrep -af "$PAT" | grep -v "bash -c" | wc -l)
  [ "$n" = 0 ] && break
  sleep 5
done
```

**Cost:** a safe-restart script waited 45 minutes for the board to drain,
seeing five workers where there were two; three "workers" were its own wait
loops. The diagnosis "the board is busy" was false.

## "Green locally" means the CI phase, not a build with tests skipped

Quality gates live in the phase CI runs (for example `verify` in Maven, `check`
in Gradle, `lint` plus `test` in a JS project). A build with tests skipped runs
none of the static analysis, coverage or style checks: green locally, CI fails
a minute before the tests. Two PRs in a row once failed this way, one on a
static-analysis finding, one on coverage a hundredth below the minimum.

Before the push, run the phase CI runs on the touched modules (with their
in-repo dependencies).

A gate is closed with code or tests. Suppressing an analyser with an
annotation, lowering a coverage threshold, widening excludes or marking code
as generated is FORBIDDEN.

## Check

- on red CI, `gh run view <run-id> --log-failed` ran first;
- the hypothesis was checked by a control experiment before editing;
- the control experiment confirmed both conditions: the change reached the
  build, and the build succeeded;
- the CI phase ran on the touched modules before the push.
