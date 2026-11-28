# Changelog

The pack follows semantic versioning on its contract with the project layer: the slot headings of `pack/project-template/conventions.md`, the rule ids tagged in pack skills and personas, and the layout of `pack/stacks/`. Renaming or removing a slot or a rule id is a major version bump, because a project's conventions or overrides stop matching. Adding a slot or a rule id is a minor bump; wording changes that keep every slot and id are a patch.

## 1.0.0 (unreleased)

### Contract

- **Slots.** `pack/project-template/conventions.md` is the project conventions skill to copy into the project layer and list in `skills.auto_load`. Its fixed sections: Integration branch and PR base; Build and test commands; Local verification policy; Visual evidence location; Release mechanics; Deprecated surfaces policy; Test stand / environments; Owner decisions (dated); Persona addendum; Pack overrides. Pack skills refer to them as "project conventions § <section>".
- **Rule ids.** Pack defaults a project may override carry a bold tag: `WRK-01` (full build is CI's job), `DBG-01` (CI phase on touched modules before a push), `PR-01` (visual delta lives in the issue), `PR-02` (assets branch), `DEC-01` (at most five children), `DEC-02` (card size), `RE-01` (reasoning effort layout), `DEP-01` (one dependency group per commit), `IM-01` (issue mirror summary length). An override is a line in Pack overrides: `overrides PR-01 because <reason>, <date>`.
- **Stacks.** Language-specific recipes moved out of the core skills into `pack/stacks/<stack>/`; `pack/stacks/java` holds the Java, Maven and Serena recipes. Add a stack directory to `skills.external_dirs` only when the project uses it.
- **Personas** are installed unchanged; project additions go to the Persona addendum section.
- **Lessons** never edit pack files; a generic lesson is marked `pack-candidate:` and proposed upstream.
- `pack/scripts/factory-doctor.py` checks a conventions file against the contract.

### Upgrading

1. Switch the pinned tag of the pack clone (`git -C <pack-clone> checkout <tag>`). The clone is never edited, so there is nothing to merge.
2. Run `python3 <pack-clone>/pack/scripts/factory-doctor.py <conventions-file> <pack-clone>/pack` and fix every reported slot or override id.
3. Read this file for the new release and move any project copy of a rule that the pack now carries out of the project layer.
