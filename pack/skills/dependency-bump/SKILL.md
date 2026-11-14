---
name: dependency-bump
description: Bump dependency versions without trusting a stale search index.
version: 1.0.0
author: maintainer, Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [dependencies, versions, ci]
    related_skills: [debugging-discipline, pr-deliverable]
---

# Bumping dependency versions

This skill describes how an agent finds the real latest versions of a
project's dependencies (from the registry's own metadata, not from a search
index), how it builds a "current versus available" table, and how it lands
the bumps in small groups that let CI name the culprit. The examples use a
Maven repository; the same approach applies to any registry (npm, PyPI,
crates.io) through its metadata API.

## The source of truth is the registry metadata, not a search index

Search indexes lag. In one case a search site returned exactly the versions
already pinned in the build file for three libraries, which read as "nothing
to bump", while the real latest versions were several minor releases ahead.

Check against the registry itself. For Maven Central:

```bash
curl -s https://repo1.maven.org/maven2/<group/path>/<artifactId>/maven-metadata.xml \
  | grep -o '<latest>[^<]*'
```

The path is the groupId with dots replaced by slashes, plus the artifactId.
A build plugin's metadata lives separately from the library it wraps, and
their versions differ (for example `4.10.4` versus `4.10.4.0`). For npm use
`npm view <pkg> version`, for PyPI `https://pypi.org/pypi/<pkg>/json`, for
crates `cargo search` or the crates.io API.

## Order

1. List the current versions from the build file on the integration branch
   in one command (for example
   `git show origin/<integration-branch>:pom.xml | grep -oE "<[a-z-]+\\.version>[^<]+"`).
2. For each dependency take the latest version from the metadata and make a
   table "what is pinned, what is available"; rows without a difference do
   not belong in the PR.
3. Bump one group per commit, not everything at once: a red CI then names
   the culprit by itself.
4. Libraries that carry a gate (for example the UI end-to-end test framework
   or the browser driver manager) get their own commit and a full run.
   Falling many minor versions behind on such a library means changed waiting
   and selector behaviour, not cosmetics.

## What not to do

- Do not bump to a version that is not in the metadata: resolution fails and
  it looks like a network problem.
- Do not mix version bumps with product changes: infrastructure changes go
  in a separate PR.
- Do not treat a green `build` job as enough for libraries whose behaviour
  only end-to-end jobs exercise.
- If the project maintains several release lines, follow its conventions on
  which lines receive the bump; do not assume parity.
