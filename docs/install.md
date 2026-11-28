# Installing the pack

This document describes how to add the pack to an existing Hermes Agent v0.23.1 installation. The pack does not modify Hermes itself: it adds skills, role personas and plugins, and gives you a config overlay to merge. Read [separation-and-adoption.md](separation-and-adoption.md) first if you already run a customised installation. For the full journey from an inventory of your project to verified cron jobs, read [adapting-to-your-project.md](adapting-to-your-project.md); the agents follow the same steps in the `factory-adoption` skill.

## Prerequisites

- Hermes Agent v0.23.1 installed with the gateway running as a user service.
- Profiles for the roles you want. The pack assumes `default` (orchestrator), `architect`, `coder`, `reviewer`, `tester`, `ops`; create missing ones with `hermes profile create <name>` (check `hermes profile --help`).
- A kanban board for the target repository and a GitHub identity for the agents (a GitHub App is recommended; see the `pr-deliverable` skill).
- A repository ruleset on `<org>/<repo>` that lets only `<owner>` merge into protected branches.

## Steps

1. **Clone the pack** somewhere the Hermes user can read, for example `~/hermes-dark-factory`, and check out a release tag. Do not clone it inside the Hermes home, and never edit the clone: everything project-specific lives in the project layer, so an upgrade is a tag switch with no merge.
2. **Skills.** In every profile's `config.yaml` add the pack skills directory to `skills.external_dirs`:
   ```yaml
   skills:
     external_dirs:
       - ~/hermes-dark-factory/pack/skills
       - ~/hermes-project/skills        # your project conventions skill
       # - ~/hermes-dark-factory/pack/stacks/java   # only if the project is on that stack
     auto_load:
       - project-conventions
   ```
   External directories are read-only for the curator, so agents follow the pack skills without rewriting them.
3. **Personas.** Install `pack/souls/<role>.SOUL.md` unchanged as each profile's `SOUL.md` (`orchestrator.SOUL.md` goes to the root profile), for example as a symlink into the clone. Project additions for a role go to the Persona addendum section of the project conventions, which every role has in context.
4. **Plugins.** Install the three kanban guards with the plugin pack in the repository root. It pins each plugin to an exact commit, shows a review screen and asks for consent per plugin:
   ```bash
   hermes plugins pack show https://raw.githubusercontent.com/a-simeshin/hermes-dark-factory/main/hermes-pack.yaml
   hermes plugins pack install https://raw.githubusercontent.com/a-simeshin/hermes-dark-factory/main/hermes-pack.yaml
   hermes plugins enable kanban-admission-guard   # and the other two
   hermes gateway restart
   ```
   `pack install` is interactive only. `disk-cleanup` is an override of the bundled plugin of the same name and is not in the pack file: copy `pack/plugins/disk-cleanup/` into `~/.hermes/plugins/` by hand if you want it. Follow each plugin's README; `kanban-admission-guard` does nothing until the board watchdog's admission gate has written its approval cache.
5. **Config.** Merge `pack/config/profile-overlay.example.yaml` into each profile's config; pick models for your provider. Settings that the dispatcher reads (`max_in_progress` and friends) apply only after a gateway restart: use `hermes gateway restart` without `-y`.
6. **Project layer.** Copy `pack/project-template/conventions.md` to `~/hermes-project/skills/project-conventions/SKILL.md` and fill every section; keep the headings unchanged, because pack skills refer to them by name. Check it with `python3 ~/hermes-dark-factory/pack/scripts/factory-doctor.py ~/hermes-project/skills/project-conventions/SKILL.md ~/hermes-dark-factory/pack`. Put the same essentials in the target repository's `AGENTS.md`.
7. **Cron jobs.** Recreate the jobs you want from `pack/cron/jobs.example.md`. Copy `pack/scripts/` to `~/.hermes/scripts/`, copy `factory.env.example` to `~/.hermes/factory.env` and fill it in. Provider-specific keys (peak window, quota guard, prices, report time zone, build-command regexes) have no defaults: leave one unset and its feature stays off or reports "unknown", as the comment next to the key says. Jobs of other profiles need a small wrapper in `profiles/<profile>/scripts/` (the pack ships `peak-pause-tick.sh` and `holdout-stands-kill.sh` as such wrappers). Run `python3 ~/.hermes/scripts/run-tests.py --audit` and expect its final line to report every test green. Before enabling the board watchdog and PR hygiene jobs, run their dry-run flags and read what they would do on your board.
8. **Snapshot.** Put the knowledge layer of the Hermes home under git with a whitelist and commit the starting point.

## Check that it works

- `hermes -p coder -z "List the first three rules of the project conventions"` should quote your conventions skill.
- Create a small card, watch the same card open a pull request, pass through the review lane and close with "remaining: owner merge", and check that the verdict on the pull request has a Calibration block.
