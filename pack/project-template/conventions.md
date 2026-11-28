---
name: project-conventions
description: 'Project contract: branches, build commands, policies and owner decisions every role must follow.'
version: 1.0.0
license: MIT
metadata:
  hermes:
    tags: [project, conventions, contract]
---

# Project conventions

<!-- Copy this file to <project-skills>/project-conventions/SKILL.md, list
project-conventions in skills.auto_load of every profile, and replace every
TODO line. Keep the ## headings exactly as they are: pack skills refer to
them as "project conventions § <heading>", and factory-doctor.py checks them.
A slot that does not apply says so in one line ("None: no UI"). -->

## Integration branch and PR base

<!-- The branch pull requests target, release lines, end-of-life lines. -->
TODO: e.g. PRs target `develop`; `release/2.x` takes fixes only; `1.x` is end-of-life.

## Build and test commands

<!-- Exact commands: fastest static check, compile, unit tests, full verification. -->
TODO: e.g. lint `make lint`, compile `make build`, tests `make test`, full `make verify`.

## Local verification policy

<!-- What a worker runs locally before a push and what is left to CI only. -->
TODO: e.g. locally: lint and the tests of touched modules; CI only: e2e and the full matrix.

## Visual evidence location

<!-- Where before/after screenshots and diagrams go: issue, PR, assets branch. -->
TODO: e.g. Before/After in the issue body; screenshots on the `hermes-assets` branch.

## Release mechanics

<!-- How a release is cut, who tags, which files carry the version. -->
TODO: e.g. the owner tags `vX.Y.Z` on `main`; agents only prepare the changelog PR.

## Deprecated surfaces policy

<!-- What happens to deprecated APIs, flags or modules: keep, warn, remove after N releases. -->
TODO: e.g. deprecated APIs stay one minor release with a warning, then are removed.

## Test stand / environments

<!-- Stand URLs or start commands, test accounts by role (no secrets here). -->
TODO: e.g. `make stand-up` serves http://localhost:8080; no shared staging.

## Owner decisions (dated)

<!-- One line per decision: date, decision, reason. Newest first. -->
TODO: e.g. 2026-01-15: no new runtime dependencies without an issue, because of image size.

## Persona addendum

<!-- Project additions to the pack personas, grouped by role. Never edit the pack SOUL files. -->
TODO: e.g. coder: run the code generator after editing any schema file.

## Pack overrides

<!-- One line per override of a pack rule id: "overrides <ID> because <reason>, <date>". -->
TODO: e.g. overrides PR-01 because the owner reviews screenshots in the PR, 2026-01-20.
