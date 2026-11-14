# Installing the pack

This document describes how to add the pack to an existing Hermes Agent v0.23.1 installation. The pack does not modify Hermes itself: it adds skills, role personas and plugins, and gives you a config overlay to merge. Read [separation-and-adoption.md](separation-and-adoption.md) first if you already run a customised installation.

## Prerequisites

- Hermes Agent v0.23.1 installed with the gateway running as a user service.
- Profiles for the roles you want. The pack assumes `default` (orchestrator), `architect`, `coder`, `reviewer`, `tester`, `ops`; create missing ones with `hermes profile create <name>` (check `hermes profile --help`).
- A kanban board for the target repository and a GitHub identity for the agents (a GitHub App is recommended; see the `pr-deliverable` skill).
- A repository ruleset on `<org>/<repo>` that lets only `<owner>` merge into protected branches.

## Steps

1. **Clone the pack** somewhere the Hermes user can read, for example `~/hermes-dark-factory`. Do not clone it inside the Hermes home.
2. **Skills.** In every profile's `config.yaml` add the pack skills directory to `skills.external_dirs`:
   ```yaml
   skills:
     external_dirs:
       - ~/hermes-dark-factory/pack/skills
       - ~/hermes-project/skills        # your project conventions skill
     auto_load:
       - <project>-conventions
   ```
   External directories are read-only for the curator, so agents follow the pack skills without rewriting them.
3. **Personas.** Copy `pack/souls/<role>.SOUL.md` to each profile's `SOUL.md` (`orchestrator.SOUL.md` goes to the root profile), then append a short project section.
4. **Plugins.** Copy the directories under `pack/plugins/` into `~/.hermes/plugins/` and follow each plugin's README.
5. **Config.** Merge `pack/config/profile-overlay.example.yaml` into each profile's config; pick models for your provider. Settings that the dispatcher reads (`max_in_progress` and friends) apply only after a gateway restart: use `hermes gateway restart` without `-y`.
6. **Project layer.** Write a `<project>-conventions` skill: integration branch, build and test commands, stand URLs, owner decisions. Put the same essentials in the target repository's `AGENTS.md`.
7. **Cron jobs.** Recreate the jobs you want from `pack/cron/jobs.example.md`. The scripts behind the script jobs are not shipped yet (see the catalog); write your own or wait for the engine split.
8. **Snapshot.** Put the knowledge layer of the Hermes home under git with a whitelist and commit the starting point.

## Check that it works

- `hermes -p coder -z "List the first three rules of the project conventions"` should quote your conventions skill.
- Create a small card, watch it go through coder, reviewer and tester, and check that the verdict on the pull request has a Calibration block.
