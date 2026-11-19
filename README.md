# hermes-dark-factory

An extension pack for [Hermes Agent](https://hermes-agent.nousresearch.com/) (v0.23.1) that turns a set of Hermes profiles into a small autonomous software factory: agents in the roles of orchestrator, architect, coder, reviewer, tester and ops work a kanban board, open pull requests, review and test each other, and hand finished work to a human owner who reviews, merges and teaches them.

The pack contains only what sits on top of Hermes. Hermes itself is installed and upgraded upstream and is never patched.

## How it works

![From an idea to a merged pull request: the owner, the Telegram gateway, GitHub, the kanban board and the agent profiles across six stages](docs/assets/factory-flow.svg)

| Stage | Who is responsible | What happens |
|---|---|---|
| 1. Intake | Owner, Telegram gateway (`default` profile), GitHub | The owner writes an idea or a bug to the bot in Telegram. The front desk turns the message into a triage card and mirrors it to a GitHub issue (`issue-mirror` skill). The owner can also file the issue directly, and scheduled agent jobs file issues that wait for the owner's triage. |
| 2. Admission | GitHub milestone, board scripts and plugins | Putting an issue into the active milestone is the owner's approval. The issue executor poll gives an approved issue exactly one executor card, the admission gate parks work nobody approved, and the admission plugin stops the dispatcher from claiming it in between. Priority comes from the milestone band; the dispatcher claims ready cards in priority order and pauses during the provider's peak window if configured. |
| 3. Plan | `architect` | Large work is decomposed into self-contained child cards, each owning one pull request. A large or critical plan goes to the `reviewer` through the native review lane before any child runs. |
| 4. Build | `coder` | The card works in its own worktree, pushes a branch as the bot account and opens the pull request. Before asking for review it posts a "What changes for you" comment: what the user of the product will notice. One PR = one card: rework and rebases happen on this same card. |
| 5. Review | `reviewer`, `tester`, GitHub | The reviewer tries to refute the work and posts `## Review verdict` on the exact head; changes requested send the work back to the same card. Where the plan has a QA step, the tester verifies the real diff. CI must be green on that head. |
| 6. Merge | Owner, watchdogs, Telegram gateway | The owner reviews and squash-merges; a formal review with changes reaches the card through the GitHub webhook. Watchdogs keep a signal on closed cards waiting for the merge, return runs whose worker died, and report through the gateway: hourly findings, quota alarms and an evening spend report. |

The diagrams below open each part of that picture in detail. Every fact in them comes from the files of this pack; the text sources are in `docs/`.

### Where work comes from

The owner's chat, the owner's own issues, the scheduled UX, product (CEO) and technical (CTO) audits, generated cards and the owner's PR reviews: what waits for the owner's triage, the three triage decisions, and the cards that skip triage.

![Every source of work, the triage step with the owner's go, no and investigate decisions, and generated cards with their receipts](docs/assets/work-intake.svg)

### Admission and priorities

How approved work becomes a running worker: milestone priority bands, the issue executor poll, the admission gate and its plugin, the dispatcher with its holds (quota stop, peak pause, owner stop), and how parked cards come back.

![From an approved issue to a claimed run: priority bands, coverage rule, admission gate, admission guard, dispatcher, dispatch lock holds and parking](docs/assets/admission-and-priority.svg)

### The life of a card

Every status a card passes through, what triggers each change, and who or what makes it.

![Card states triage, todo, ready, scheduled, running, blocked, review, done and archived with every transition and its cause](docs/assets/card-lifecycle.svg)

### The pull request path

From a worktree to the owner's squash merge, with the check attached to each step: "What changes for you" before the review request, the adversarial verdict on the exact head, the tester's QA verdict, CI, the completion contract and the owner-merge hold.

![Thirteen steps of the pull request path with the check attached to each one](docs/assets/pr-path.svg)

### Gates and guards

Every gate, plugin, hook and skill rule that checks the work, grouped by when it acts. Part 1 covers a card from creation to close and the board-wide sweeps; part 2 covers commit, push, publish and the pull request.

![Gates around a card: on creation, while a run works, on close and in board-wide sweeps](docs/assets/gates-map-cards.svg)

![Gates around a pull request: commit and push hooks, link guards, verdict rules, CI, completion contract, risk labels and PR hygiene](docs/assets/gates-map-pr.svg)

### What runs on a schedule

Every cron job grouped by how often it runs, what it touches and where its output goes. Silent when healthy is the default.

![Cron jobs from every five minutes to weekly, with profile, schedule, what each touches and where it reports](docs/assets/cron-schedule.svg)

### What reaches the owner

Four sources reach the owner's phone through the Telegram gateway, and each comes with an action; routine events wait in the quiet digest that the evening report prints.

![Card events, board watchdog, health and evening report delivered through the Telegram gateway, with what the owner does with each](docs/assets/owner-notifications.svg)

### Roles and models

Six profiles: the judges (architect, reviewer) run the strong model, the doers run the cheap one because later stages check their work.

![The six profiles with role, model tier, reasoning effort, turn cap, what each reads before work and its main skills](docs/assets/roles-and-models.svg)

### Knowledge and teaching

Where the agents keep what they learn, and how the owner teaches them through their own channel and checks the snapshot diff rather than the agent's report.

![Knowledge layers from persona to lessons, save-lesson-before-complete, teach-snapshot and the owner's teaching round](docs/assets/knowledge-and-teaching.svg)

### Lights out: a supervising agent

The last step towards a dark factory: a second, stronger agent (Claude Code in our case) watches the factory every half hour, turns each bad behaviour into a teaching round through the agents' own channel, verifies the result in the repository, and leaves the owner only merges, releases and scope. It checks, controls, directs and teaches; it never edits Hermes, the agents' files or the product's code. The factory makes every change itself, so every lesson lands where the acting agent will read it next time. See [docs/supervisor-agent.md](docs/supervisor-agent.md) and the templates in [pack/supervisor/](pack/supervisor/).

![The supervisor's six-step loop (check, control, teach, verify, direct, report) on one side of a no-edit boundary, the factory's board, orchestrator chat, agents, knowledge snapshot and pull requests on the other](docs/assets/supervisor-loop.svg)

## What is inside

| Path | Contents |
|---|---|
| `pack/skills/` | Generic skills for the roles: kanban discipline, worker runs, decomposition, adversarial review, tester verdicts, PR deliverables, issue mirroring, debugging, lesson capture and more. Point `skills.external_dirs` at this directory. |
| `pack/souls/` | Role personas (SOUL.md) for each profile. |
| `pack/plugins/` | Hermes plugins: review-loop guard, artifact guard, admission guard (keeps unapproved cards from being claimed between two admission runs), disk-cleanup override. |
| `pack/config/` | A config overlay with the settings that cut model spend roughly in half. |
| `pack/scripts/` | The mechanical layer: watchdogs, gates, guards and metrics, configured through one `factory.env` file, with their tests and a hermetic test runner. |
| `pack/cron/` | The scheduled jobs the factory runs, as a reference list, and the prompts of the agent jobs. |
| `pack/supervisor/` | Templates for a stronger supervising agent: the scheduled loop prompt, the teaching round and read-only verification commands. |
| `reference/githooks/` | Git hooks the agents' commits pass through (identity, link and style guards). |
| `docs/` | How it all works. |

## Read in this order

1. [docs/owner-journey.md](docs/owner-journey.md): what the owner of the factory does day to day.
2. [docs/how-hermes-works.md](docs/how-hermes-works.md): the Hermes internals that matter for a factory.
3. [docs/architecture.md](docs/architecture.md): how the factory is built on top of them.
4. [docs/extensions-catalog.md](docs/extensions-catalog.md): every mechanism we added, and why the native feature was not enough.
5. [docs/config-and-quota.md](docs/config-and-quota.md): spend and the knobs that control it.
6. [docs/operations.md](docs/operations.md): running it on a small server.
7. [docs/teaching-hermes.md](docs/teaching-hermes.md): correcting the agents through their own channel.
8. [docs/separation-and-adoption.md](docs/separation-and-adoption.md): generic versus project-specific, and moving an existing setup onto the pack.
9. [docs/lessons-learned.md](docs/lessons-learned.md) and [docs/install.md](docs/install.md).
10. [docs/adapting-to-your-project.md](docs/adapting-to-your-project.md): adapting, verifying, debugging and changing the pack for your own project (the agents' procedure is the `factory-adoption` skill).
11. [docs/supervisor-agent.md](docs/supervisor-agent.md): lights-out operation with a stronger agent that checks, controls, directs and teaches, and never edits.

## Status

Extracted from a factory that has been developing a real open-source project for several months, and kept in sync with that installation. The skills and docs are generic. `pack/scripts/` holds every script of the original setup, including the board watchdog, the worker-run watchdog, the issue executor poll, PR hygiene and the evening metrics, with project values moved into one `factory.env` file and more than a hundred test files that `run-tests.py --audit` runs in isolation.

The pipeline follows the rule "one PR = one card": the card that owns a pull request carries it through the native review lane up to the owner's merge, and no separate reviewer, rebase or summary cards are created for it. Every card that a script generates first asks whether a native mechanism or a live card already covers the work, and carries that receipt in its body.

The three large scripts (`board-watchdog.py`, `pr-hygiene.py`, `metrics-evening.py`) still encode one owner's rules and include irreversible actions (archiving cards, closing issues, rewriting the dispatcher cap). Start them with their dry-run flags and read what they would do on your board before you enable their cron jobs.
