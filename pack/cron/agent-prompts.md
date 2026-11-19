# Prompts of the agent cron jobs

These are the prompts of the four agent jobs listed in [jobs.example.md](jobs.example.md), translated from the original installation and stripped of project specifics. They are templates: replace the placeholders, then pass the text to `hermes cron create` (check `hermes cron create --help` for the flags of your version).

The originals grew by appending a paragraph after every incident, and some paragraphs ended up contradicting earlier ones (the UX prompt first told the agent to build and start the demo, and a later paragraph forbade exactly that). The templates below are the consolidated versions: each rule appears once, in the place where the agent needs it.

## Placeholders

| Placeholder | Meaning |
|---|---|
| `<project>` | Name of the product |
| `<repo-path>` | Local checkout of the repository on the host |
| `<gh-repo>` | `owner/name` of the repository on GitHub |
| `<board>` | Slug of the kanban board |
| `<owner-login>` | GitHub login of the owner, used as the assignee of mirrored issues |
| `<conventions-skill>` | Project conventions skill attached to the job (`--skills`) |
| `<conventions-dir>` | Directory with the full text of the conventions |
| `<demo-url>`, `<demo-unit>` | URL of the running demo and the systemd user unit that keeps it up |
| `<milestone>` | The open milestone whose acceptance scenarios are run |
| `<holdout-dir>` | Directory with hidden scenarios, readable only by the tester profile |

## Shared block: issue only, no card

Three of the four prompts end with the same rules for what happens to every finding. Keep them in one place in your conventions skill and reference them from the prompt; they are spelled out here once.

The ideation jobs (ux, ceo, cto) file GitHub issues only, never board cards. A card created by a scheduled agent bypasses the owner's triage: the board's own triage column is auto-decomposed regardless of what the owner decided on GitHub, and unapproved ideas turned into cards and PRs that way. The owner approves an issue (assigns a milestone or removes wait-for-triage), and only then does the admission gate or the issue poller create the executor card.

```text
FOR EVERY FINDING:
- Do NOT create a kanban card. The deliverable is a GitHub issue awaiting the
  owner's triage.
- Write the issue strictly by the template in <conventions-dir>/issue-mirror.md.
  English only. The sections Current behavior / After the change / User
  benefit (so what?) / Acceptance criteria are mandatory. If you cannot state
  the benefit for the user, do not file it at all.
- Deduplicate first: search open and recently closed issues by keywords
  (gh issue list --search) and reuse what you find. A finding that already has
  an issue, or whose work is already delivered, does not produce a new one.
- Labels and assignee at creation: bug or enhancement, area label, the
  factory label, wait-for-triage, a severity label (minor / normal / major /
  critical) by the criteria in the conventions; assignee <owner-login>. No
  milestone: the milestone is the owner's approval.
- Do not repeat proposals from the continuity output of previous runs.
```

## ux-tick

Daily, cheap model, high reasoning effort, conventions skill attached.

```text
UX audit of <project> by dogfooding. Report friction only, not feature ideas.

THE ENVIRONMENT IS READY, do not spend turns on setup. The demo runs as the
systemd unit <demo-unit> at <demo-url>; do not start the build or the
application yourself. If the demo does not answer: systemctl --user restart
<demo-unit> and wait 20 seconds. A headless browser is already attached over
CDP: the browser tools work immediately, do not launch or debug a browser.

BUDGET: at most 45 tool calls for the whole audit. Fewer screens examined
properly is better than all screens skimmed.

1. Walk the UI with the BUILT-IN browser tools (open, click, snapshot by ref).
   Do not write browser automation scripts: it is expensive. Cover: the main
   connection flow with a valid and an INVALID input, every main section of
   the product including error states, a mobile viewport of 375 px, and the
   browser console for JavaScript errors.
2. Turn EVERY friction finding into an issue IMMEDIATELY, with reproduction
   steps and an acceptance criterion in the body. Check whether old findings
   from the continuity output were fixed instead of filing them again.
3. For a UI finding a screenshot is mandatory: save a PNG, commit it to the
   assets branch of the repository as screenshots/<date>-<what>.png, and put
   the raw URL into the issue.

<shared block>
```

## ceo-tick

Weekly, strong model.

```text
Product cycle for <project> (weekly). Product: <one-sentence description>,
repository <gh-repo>.

1. Research with the web search and scraping tools: the state of the product's
   category, what competitors and the reference implementation shipped, changes
   in the platform the product builds on, and user pain (issues, forums).
2. Read the open issues of the repository with gh.
3. Formulate 1 to 3 proposals, product or technical, each as an issue awaiting triage.
   The body must give the rationale: why, for whom, and the market signal
   behind it.

A screenshot is not required, but Current behavior must be understandable to
a non-developer.

<shared block>
```

## cto-tick

Weekly, strong model, conventions skill attached.

```text
Technical cycle for <project> (weekly). Repository <repo-path>, active lines:
<branches>. Conventions: the skill <conventions-skill> is already in the
system prompt; full texts are in <conventions-dir>. gh is already authorised.

Audit six directions:
1. Security: gh api repos/<gh-repo>/dependabot/alerts. Summarise by package
   and severity, and say what a version bump actually fixes.
2. CI health: gh run list for every active line. Share of red runs, flaky
   patterns, job durations.
3. Technical debt: TODO, FIXME and deprecated markers in the project's own
   code (not vendored code), duplication, suppressed warnings.
4. Stack freshness: outdated dependency versions in the build files.
5. Vendored upstream: the vendored version against the latest upstream
   release. Is a re-vendor worth it, given the local patches?
6. Standard conformance: the latest revision of the specification the product
   implements and its recent proposals. Which capabilities appeared or changed,
   which of them the underlying SDK already supports, and which of those the
   product does NOT show or shows incorrectly. A gap of the form "the protocol
   can, the product does not show it" is a candidate for an issue.

Result: at most 1 to 3 issues, the most valuable ones.

Report to the owner's chat: a short summary for each of the six directions
and the list of filed issues.

BUDGET: 45 tool calls.

<shared block>
```

## holdout-nightly

Nightly, tester profile, cheap model. This is an agent job with a prep script: the script (`holdout-nightly.sh`) prepares worktrees and builds, and prints a JSON of targets that is injected above the prompt.

```text
Nightly run of the hidden acceptance scenarios of milestone <milestone>.
Input from the prep script above: a JSON of targets (open PRs and the heads of
the development lines) plus hints. Act strictly on it; do not invent targets.

1. Read your scenario files: <holdout-dir>/<milestone>/*.md (one per milestone
   item) and the helper tools in <holdout-dir>/tools/.
2. For every target with a non-null build artifact, start a stand on its own
   port from the reserved range, ALWAYS through run-bounded.sh with
   --owner cron:holdout-nightly:<target> --ttl-min 90. Start a second stand
   (the upstream or its mock) when the scenario needs one, with the same
   owner. IMMEDIATELY after the scenarios of a target, before moving to the
   next one: holdout-stands-kill.py --owner-prefix
   cron:holdout-nightly:<target>. This is a mechanism, not a guideline:
   keeping stands "until the end of the run" is forbidden, because the
   iteration limit can cut the run before the summary and the stands then live
   until their TTL. Before the final summary kill the leftovers:
   holdout-stands-kill.py without a filter.
3. Drive every scenario with real input in the browser (the one attached over
   CDP in the profile config): a fresh tab, steps and expected results
   strictly by the scenario file. Keep the timings: a 75-second wait in a
   scenario is part of the check, not a delay. Evidence: a screenshot of the
   key step and the status text from the UI; for headers, the log of the mock
   server.
4. Report: <cache>/holdout/report-<date>.md and a copy latest.md (overwrite).
   Table format: | issue | scenario in one line | target | PASS/FAIL | what is
   visible |. PASS only for what you saw yourself (UI, mock log, git show). A
   failure on a development head caused by a PR that is not merged yet is
   expected: mark it "expected", it needs no card.
5. An unexpected FAIL on an acceptance target (a PR head): first classify it
   and WRITE the classification into the report, one of three kinds:
   (a) product defect: there is evidence of observed behaviour (UI, mock log,
       proxy log) and the method does not explain the FAIL;
   (b) scenario error: the method gives a false FAIL (the browser drops a
       forbidden header before sending, a toggle in the UI is off, the wrong
       layer is being checked);
   (c) expected by stack: the head of the target does not contain the fix from
       a neighbouring PR.
   Create a card ONLY for kind (a) and only AFTER the classification is in the
   report: hermes kanban --board <board> create "<title>" --assignee coder
   --idempotency-key holdout-<issue>-<head7>, where head7 is the first seven
   characters of the target's head hash. The card MUST be created with
   --assignee coder in the same command: without an assignee the dispatcher
   skips it on every tick (skipped_unassigned) and the defect waits for a
   manual fix. The card text contains only the observed behaviour and the
   reproduction steps, WITHOUT the scenario text and without the holdout path.
   For kind (b) no card: fix the scenario file and mark "scenario fixed" in the
   report. For kind (c) mark "expected by stack", no card.
6. Answer: a summary of 3 to 6 lines: PASS/FAIL by item, which FAILs were
   unexpected, the classification of each, and what was done about them. Write
   nothing to GitHub except the card from step 5.
7. Do NOT edit or "repair" the profile config (any config.yaml), neither with
   the patch tool nor with sed or echo through the terminal. A refusal from
   the patch tool ("Agent cannot modify security-sensitive configuration") is
   a gate, and going around it through the terminal is forbidden.
   browser.cdp_url is always the stable http://127.0.0.1:<port> address: a
   ws://.../devtools/browser/<id> address belongs to one browser instance and
   is dead after its restart. If the browser tools do not connect, diagnose
   from outside (curl http://127.0.0.1:<port>/json/version) and work with what
   answers; do not touch the config.
```

## What the prompts have in common

- **A tool-call budget in the prompt.** Without it an audit job spends a whole session on setup or on one screen.
- **"The environment is ready."** Agents love to rebuild and restart things. Keep the demo and the browser running as services and say so.
- **An idempotency key on every card.** A job that runs on a schedule will rediscover yesterday's finding; the key and the dedup check keep the board clean.
- **Classify before filing.** The holdout job must write the kind of failure into the report before it may create a card; this is what stops false defects from scenario mistakes.
- **A stated cap on the output** (1 to 3 issues). Owner attention is the scarcest resource of the factory.
