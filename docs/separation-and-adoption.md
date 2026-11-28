# Separating generic from project-specific, and adopting the pack

This document explains which parts of a factory belong in this pack and which belong to a single project, and how an existing Hermes installation (including the one this pack was extracted from) moves onto the pack without a big-bang switch.

## The line between the two

A piece belongs in the **pack** when it would be true for any repository: how a worker finishes a run, how a reviewer proves a fix, how cards are decomposed, how the board is watched, how spend is cut. It belongs in the **project layer** when it names something only one project has: branch layout, modules, build commands, test stands, release policy, owner decisions about that product.

| Layer | Lives in | Examples |
|---|---|---|
| Hermes core | upstream install, never edited | gateway, kanban, dispatcher, cron, skills loader |
| Pack (this repo) | a clone of this repo, read-only for the agents | generic skills, role personas, plugins, config overlay, docs |
| Project layer | the Hermes home, owned by the agents | a project conventions skill (auto-loaded), project references and decisions, project scripts and their config, the target repo's `AGENTS.md` |
| Runtime state | the Hermes home, never in git | `.env`, databases, sessions, logs, worktrees |

Rules of thumb:

- **A generic skill never names a branch, a path or a tool version.** It says "the integration branch" and the project conventions skill says which branch that is.
- **Project decisions go to the project layer with a date and a reason.** They may override a pack default that carries a rule id (a bold tag such as `**[PR-01]**` in front of the rule); the override is a line in the Pack overrides section of the conventions.
- **Lessons start in the project layer.** A lesson moves into the pack only after it has been retold without project details and still makes sense.
- **Scripts are split into a generic engine and a project config.** The engine reads branch names, labels, board slug, stand URLs and thresholds from a config file (`factory.env`, read through `factory_config.py`); nothing project-specific is hard-coded. A script that needs a value you have not set stops with a message naming the key.

## How the agents see both layers

Hermes loads skills from `~/.hermes/skills/` and from every directory in `skills.external_dirs`. External directories are read-only for the curator, which is what you want for the pack: the agents can read and follow pack skills but propose changes through the owner, not by editing them in place.

- Point `skills.external_dirs` of every profile at `<pack-clone>/pack/skills`.
- Keep the project conventions skill in a project directory that is also in `external_dirs` (or in `~/.hermes/skills/`), and list it in `skills.auto_load` so it is in context on every run. Start it from `pack/project-template/conventions.md`: its fixed sections are the contract the pack skills refer to ("project conventions § Build and test commands"), and `pack/scripts/factory-doctor.py` checks that none is missing or left as a placeholder.
- Stack recipes: add `pack/stacks/<stack>` (for example `pack/stacks/java`) to `external_dirs` only when the project uses that stack. Core skills stay language-neutral.
- Personas: install `pack/souls/<role>.SOUL.md` unchanged into each profile. Project additions go to the Persona addendum section of the auto-loaded conventions, so a pack upgrade never conflicts with them.
- Plugins: install from `pack/plugins/` into `~/.hermes/plugins/`.

When a pack skill and the project skill disagree, the project skill wins, and it says so in the Pack overrides section: `overrides PR-01 because <reason>, <date>`. `factory-doctor.py` rejects an override of an id the pack does not have, which is how a renamed or removed rule shows up after an upgrade.

## Moving an existing installation onto the pack

Do it in stages, one layer per teaching round, so each step can be checked and rolled back with a snapshot.

1. **Freeze and snapshot** the current knowledge layer.
2. **Skills, read side.** Add the pack skills directory to `external_dirs` next to the existing shared skills. Where a pack skill has the same name as an existing one, keep the existing one first and let the agents diff them: anything in the old skill that is not in the pack is either project-specific (move it to the project conventions skill or a project reference) or a generic lesson the pack is missing (propose it upstream to the pack).
3. **Switch skill by skill.** Remove the old copy of one skill, watch a day of real cards that use it, then the next. Start with low-risk skills (debugging, lessons capture) and finish with the review and PR skills.
4. **Personas.** Replace each profile's SOUL with the unchanged pack persona and move its project-specific lines into the Persona addendum section of the conventions.
5. **Config.** Compare each profile's config with `pack/config/profile-overlay.example.yaml`; keep project-tuned values and record them in the project layer.
6. **Scripts.** Split the largest scripts into engine plus project config one at a time; each split ships with its tests green on the old behaviour before the old script is removed.
7. **Review the deviations table** after each stage: anything that is now covered by the pack or by upstream Hermes is deleted, not kept "just in case".

Language: the pack is written in English. Agents follow English skills well while still talking to the owner in the owner's language; the persona says which language to use for chat, PR text and code comments.
