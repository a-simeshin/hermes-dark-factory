# Teaching the agents

This document describes how the owner corrects the factory when an agent gets something wrong: the "teaching round". The goal is that a mistake happens once per kind, not once per card. The method treats the agents as a colleague who keeps its own notes: you give feedback in the chat, the agent decides where the lesson belongs, and you verify the change in its knowledge layer instead of trusting its report.

## Why not just edit the skills yourself

You could open the skill file and add a line. It works once and then decays: you do not know which layer the agent actually reads at the moment of the mistake, the agent does not know why the rule exists, and the next upgrade or curator pass may undo it. When the agent finds the root cause in its own session transcript, it usually discovers that the rule already existed and was read narrowly, or lived in a skill the acting role never loads. That diagnosis is the valuable part.

Edit agent files by hand only for emergencies (a runaway process, a leaked secret) and tell the agent afterwards what you changed.

## The round

1. **Collect facts.** Card ids, pull request and comment links, file:line, timestamps, the measured effect (tokens burned, mutants that survived, the cards that were archived). No adjectives.
2. **State what you expect**, as outcomes, not as edits: "find why the reviewer did not mutate the fix line, fix the layer it reads, record the miss, show a test that is red on the old rule".
3. **Check your request against recorded decisions.** If it contradicts an earlier owner decision, either change the decision explicitly or drop the request. The agents are taught to stop on such a conflict, but do not rely on it.
4. **Send it to the orchestrator** through its chat, for example `hermes -z "$(cat round.md)"` on the server, or the Telegram chat. Ask for a fixed handover: root cause with quotes from the session, file:line of each change, `git diff --stat` of the knowledge layer, the snapshot hash, and a final line `DONE`.
5. **Verify the artifact.** Read the diff of the snapshot. Re-run the test the agent claims is red on the old code. For board changes, query the board. For a watchdog change, run it and read its output.
6. **Watch the next real case.** The round is closed when the next live review or card shows the rule applied.

Keep rounds short and single-topic. One round per new kind of mistake; group several incidents of the same kind into one round.

## Snapshots

The knowledge layer (personas, skills, project context, scripts, plugins, config) lives in a git repository inside the Hermes home. A snapshot script commits a whitelist of paths with a message describing the round. Never use `git add -A` there: the home directory also holds secrets, databases and caches. Snapshots give you three things: a diff to review, a rollback point, and a history of why each rule exists.

## Patterns worth knowing

- **The rule existed but was not applied.** Most common outcome. Typical causes: the rule is scoped too narrowly ("on rebase reviews" when it should apply to every review), it lives in a skill the acting role does not load, or a broader exemption ("do not re-review untouched code") is read as overriding it. Fix the scope or the place; do not add a second copy.
- **A rule applied on paper but not in practice.** Make it part of the output contract: a table in the verdict (promise / mutant / test / result), a mandatory field in the handover. An agent can skip a paragraph of advice; it cannot skip a required section without the omission being visible.
- **Automation that hides problems.** Archiving "stale" cards, sweeping subscriptions, auto-retrying. Before accepting any automatic cleanup, ask how it is rolled back and what it must never touch (for example cards that wait on a dependency or on the owner).
- **Your own mistake.** If a round you sent was wrong, send a correcting round immediately, name it as your error, and ask the agent to revert only that part.

## Measuring that it worked

Track misses in a journal the reviewer reads (see the adversarial-review skill's misses template) and compare owner verdicts with agent verdicts on the same heads. Fewer owner reversals of agent approvals over the following days is the real signal; a growing skill file is not.
