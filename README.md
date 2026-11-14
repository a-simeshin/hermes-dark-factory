# hermes-dark-factory

An extension pack for [Hermes Agent](https://hermes-agent.nousresearch.com/) (v0.23.1) that turns a set of Hermes profiles into a small autonomous software factory: agents in the roles of orchestrator, architect, coder, reviewer, tester and ops work a kanban board, open pull requests, review and test each other, and hand finished work to a human owner who reviews, merges and teaches them.

The pack contains only what sits on top of Hermes. Hermes itself is installed and upgraded upstream and is never patched.

## What is inside

| Path | Contents |
|---|---|
| `pack/skills/` | Generic skills for the roles: kanban discipline, worker runs, decomposition, adversarial review, tester verdicts, PR deliverables, issue mirroring, debugging, lesson capture and more. Point `skills.external_dirs` at this directory. |
| `pack/souls/` | Role personas (SOUL.md) for each profile. |
| `pack/plugins/` | Hermes plugins: review-loop guard, artifact guard, disk-cleanup override. |
| `pack/config/` | A config overlay with the settings that cut model spend roughly in half. |
| `pack/cron/` | The scheduled jobs the factory runs, as a reference list. |
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

## Status

Extracted from a factory that has been developing a real open-source project for several months. The skills and docs are generic; the watchdog, PR hygiene and metrics scripts of the original setup are described in the catalog but not shipped yet: they still need to be split into a generic engine and a project config.
