#!/usr/bin/env bash
# Snapshot of the Hermes teaching layers: rules, memory, skills, conventions, config.
#
# Why: a lesson given to an agent through chat settles inside (memories/,
# skills/, shared-skills/, projects-context/, profile SOUL files and configs).
# Without a "before" snapshot it is impossible to prove what changed after the
# lesson: the agent's report about its own learning is its words, not an
# artifact.
#
#   teach-snapshot.sh            show what changed since the last snapshot
#   teach-snapshot.sh "label"    commit a snapshot with a label
set -euo pipefail

# Snapshots were once silently not created: in session workers HERMES_HOME
# points to the PROFILE directory (profiles/<name>), which has no .gitignore,
# and the cd ended with "fatal: pathspec" on the very first git add. A snapshot
# is always taken at the root of the knowledge repository: take the first
# candidate that has a .git.
snap_root=
for cand in "${HERMES_HOME:-}" "$HOME/.hermes"; do
  [ -n "$cand" ] && [ -e "$cand/.git" ] && snap_root=$cand && break
done
if [ -z "$snap_root" ]; then
  echo "REFUSED: no knowledge repository found (looked for .git in HERMES_HOME='${HERMES_HOME:-}' and '$HOME/.hermes')" >&2
  exit 1
fi
cd "$snap_root"

# If the root .gitignore is gone (disk cleanups), a snapshot is still possible
# without it: the index is built from the explicit allowlist below, not from
# git globs.
[ -f .gitignore ] && git add -f .gitignore
git add config.yaml SOUL.md 2>/dev/null || true
# Top-level documents are part of the teaching too: DEVIATIONS.md explains
# deliberate departures from the docs, RETIRED.md what was replaced by what.
# They once sat untracked all along, so their entries were never in a snapshot
# and would have been lost in any cleanup.
# One file at a time: git add with a nonexistent path in the list fails the
# WHOLE call, and 2>/dev/null || true hides that, so nothing gets added.
for f in DEVIATIONS.md RETIRED.md; do
  [ -f "$f" ] && git add "$f"
done
git add memories/MEMORY.md memories/USER.md 2>/dev/null || true

# shared-skills is common to all roles (attached through
# skills.external_directories), projects-context holds project conventions.
# Both layers are part of the teaching.
for d in skills shared-skills projects-context plugins scripts patches githooks docs; do
  [ -d "$d" ] && git add "$d"
done

for p in profiles/*/; do
  p=${p%/}
  git add "$p/config.yaml" "$p/SOUL.md" 2>/dev/null || true
  git add "$p/memories/MEMORY.md" "$p/memories/USER.md" 2>/dev/null || true
  for d in skills plugins holdout scripts; do
    [ -d "$p/$d" ] && git add "$p/$d"
  done
done

# The per-profile scripts/ directory (cron wrappers) is part of the snapshot
# too and is in the list above.

# Keep stray junk out of the snapshot. Skill usage counters (.usage.json) are
# the mechanics of parallel sessions, not knowledge: always dropped from the
# index. Other substantive edits (for example a worker profile's memory) are
# not junk: they stay and are mentioned in the snapshot message.
git reset -q -- ':(glob)**/.usage.json' 2>/dev/null || true

# Safety net: a secret must not reach the history even by accident
if git diff --cached --name-only | grep -qiE '\.env$|\.pem$|\.key$|auth\.json|^secrets/'; then
  echo "REFUSED: a secret got into the index" >&2
  git diff --cached --name-only | grep -iE '\.env$|\.pem$|\.key$|auth\.json|^secrets/' >&2
  exit 1
fi

# Full-suite gate. A snapshot is committed only after a run of the WHOLE
# scripts/test_*.py suite (the runner with the hermeticity audit). The total
# line is printed in full; on red the snapshot is still taken, but the commit
# message gets a RED tag. A teaching round's report must quote the line
# "TEST TOTAL scripts/: N/N green"; "green on a sample" is not accepted (a
# series of rounds once broke 4 tests while 10 tests silently read the live
# cache, and a sample saw none of it).
TEST_SUMMARY=""
TEST_TAG=""
if [ $# -gt 0 ] && [ -f "$snap_root/scripts/run-tests.py" ]; then
  if OUT=$(cd "$snap_root/scripts" && python3 run-tests.py --audit 2>&1); then
    TEST_SUMMARY=$(printf '%s\n' "$OUT" | grep '^TEST TOTAL' || true)
    echo "$TEST_SUMMARY"
  else
    TEST_SUMMARY=$(printf '%s\n' "$OUT" | grep '^TEST TOTAL' || true)
    echo "$TEST_SUMMARY"
    printf '%s\n' "$OUT" | grep '^RED:' || true
    TEST_TAG="RED: ${TEST_SUMMARY:-tests red}; "
  fi
fi

if [ $# -gt 0 ]; then
  # "No changes" is a lie when the commit failed. The commit may have failed on
  # a pre-commit hook (for example one that rejects deleted sources): the hook
  # output goes to stderr above, here is an honest diagnosis and a non-zero code.
  if git commit -q -m "${TEST_TAG}$1"; then
    echo "snapshot committed: ${TEST_TAG}$1"
  else
    rc=$?
    echo "SNAPSHOT NOT TAKEN: git commit failed (code $rc, the reason is in stderr above, usually a pre-commit hook). HEAD did not change." >&2
    exit "$rc"
  fi
else
  if git diff --cached --quiet; then
    echo "nothing has changed inside since the last snapshot"
  else
    echo "SNAPSHOT NOT TAKEN: without a message argument the script only shows the index." >&2
    echo "Run: scripts/teach-snapshot.sh "what was recorded"; take the hash from git log AFTER that." >&2
    git diff --cached --stat
    echo
    git diff --cached
    exit 2
  fi
fi
