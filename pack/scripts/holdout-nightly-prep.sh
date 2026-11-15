#!/bin/bash
# holdout-nightly-prep.sh: preparation of the targets of the nightly run of
# hidden acceptance scenarios against the open milestone (the milestone is
# detected automatically; a hardcoded version once went stale). A script for the
# tester profile's cron: its stdout goes into the agent's prompt; it can be
# called by hand. The cron wrapper is holdout-nightly.sh.
#
# What it does:
#   1) resolve: the heads of the maintained base branches (FACTORY_BASE_BRANCHES)
#      plus the open PRs of the open milestones (their heads);
#   2) a worktree holdout-pr<N> for every open PR (fetch refs/pull/N/head), a
#      .wt-keep marker for 3 days against worktree-gc;
#   3) build of the artifact for a PR with product changes (files with the
#      extensions of FACTORY_HOLDOUT_PRODUCT_EXT); the build runs inside
#      run-bounded.sh (TTL 50 min); a fresh ready artifact is not rebuilt
#      (idempotent);
#   4) one JSON on stdout: the date, the shas of the base heads, the list of
#      targets with worktree and artifact paths (or null when no build is
#      needed or it failed).
#
# The stands themselves and the browser input are the cron agent's job: the
# life of a stand depends on the scenario, and the agent must start everything
# in the background through run-bounded.sh.
#
# Settings (factory.env):
#   FACTORY_REPO                   owner/name of the GitHub repository
#   FACTORY_REPO_PATH              local clone
#   FACTORY_BASE_BRANCHES          maintained base branches, comma-separated
#   FACTORY_HOLDOUT_BUILD_CMD      build command, run in the PR worktree (split on
#                                  whitespace, no shell quoting: point it at a
#                                  script for anything complex)
#   FACTORY_HOLDOUT_ARTIFACT_GLOB  built artifact, relative to the worktree
#   FACTORY_HOLDOUT_PRODUCT_EXT    (optional) extensions that need a build,
#                                  default java,xml,ts,tsx
#   FACTORY_HOLDOUT_DIFF_BASE      (optional) branch the changed files are
#                                  counted against, default: the last entry of
#                                  FACTORY_BASE_BRANCHES
# A value with spaces must be quoted in factory.env.
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
[ -f "$HERMES_HOME/factory.env" ] && . "$HERMES_HOME/factory.env"
HERE="$(cd "$(dirname "$0")" && pwd)"
: "${FACTORY_REPO:?FACTORY_REPO is not set: add it to $HERMES_HOME/factory.env}"
: "${FACTORY_REPO_PATH:?FACTORY_REPO_PATH is not set: add it to $HERMES_HOME/factory.env}"
: "${FACTORY_BASE_BRANCHES:?FACTORY_BASE_BRANCHES is not set: add it to $HERMES_HOME/factory.env}"
: "${FACTORY_HOLDOUT_BUILD_CMD:?FACTORY_HOLDOUT_BUILD_CMD is not set: add it to $HERMES_HOME/factory.env}"
: "${FACTORY_HOLDOUT_ARTIFACT_GLOB:?FACTORY_HOLDOUT_ARTIFACT_GLOB is not set: add it to $HERMES_HOME/factory.env}"
PRODUCT_EXT="${FACTORY_HOLDOUT_PRODUCT_EXT:-java,xml,ts,tsx}"

REPO="$FACTORY_REPO_PATH"
WTROOT="$REPO/.worktrees"
CACHE="$HERMES_HOME/cache/holdout"
SCRIPTS="$HERE"
BOUND_TTL_MIN=50
mkdir -p "$CACHE"
cd "$REPO"

IFS=',' read -r -a BRANCHES <<< "${FACTORY_BASE_BRANCHES// /}"
DIFF_BASE="${FACTORY_HOLDOUT_DIFF_BASE:-${BRANCHES[${#BRANCHES[@]}-1]}}"
read -r -a BUILD_ARGV <<< "$FACTORY_HOLDOUT_BUILD_CMD"
EXT_RE=$(printf '%s' "$PRODUCT_EXT" | sed 's/[ ]//g; s/,/|/g')

# Leftovers of the previous run are stopped BEFORE the start so that dead
# stands do not share ports with the new ones. Only entries of the owner
# cron:holdout-nightly; foreign ones are not touched.
"$SCRIPTS/holdout-stands-kill.py" || true

git fetch origin "${BRANCHES[@]}" --quiet
HEADS=""
for b in "${BRANCHES[@]}"; do
  HEADS="$HEADS\"$b\":\"$(git rev-parse --short "origin/$b")\","
done
HEADS="${HEADS%,}"

# The open PRs of all open milestones (auto-detected): take the titles of the
# open milestones (for example 2.3.0) and the open PRs whose milestone starts
# with one of them; an empty list is normal when a milestone is closed and a
# new one is not open yet.
REPO_NAME="$FACTORY_REPO"
OPEN_MS=$(gh api "repos/$REPO_NAME/milestones?state=open&per_page=20" \
      --jq '[.[].title | split(" ")[0]] | join(" ")')
export OPEN_MS
PRS=$(gh pr list --repo "$REPO_NAME" --state open --limit 100 \
      --json number,milestone \
      --jq '.[] | select(.milestone.title != null)
            | .milestone.title as $t
            | select($ENV.OPEN_MS | split(" ") | map($t | startswith(.)) | any)
            | .number' | sort -n | tr '\n' ' ')

build_jar() {  # $1 = worktree directory; prints the artifact path or nothing
  local d=$1
  (cd "$d" && "$SCRIPTS/run-bounded.sh" --owner cron:holdout-nightly \
     --ttl-min "$BOUND_TTL_MIN" -- \
     "${BUILD_ARGV[@]}" >/dev/null 2>&1) || true
  # ls expands the glob; the .original filter skips repackaged-archive leftovers
  ls "$d"/$FACTORY_HOLDOUT_ARTIFACT_GLOB 2>/dev/null \
    | grep -v '\.original$' | head -1 || true
}

echo_n() { printf '%s' "$1"; }
OUT="{\"date\":\"$(date +%F)\",\"dev\":{$HEADS},\"targets\":["
echo_n "$OUT"
FIRST=1
for n in $PRS; do
  git fetch origin "+refs/pull/$n/head:refs/holds/pr$n" --quiet 2>/dev/null || true
  SHA=$(git rev-parse --short "refs/holds/pr$n" 2>/dev/null || echo "?")
  WT="$WTROOT/holdout-pr$n"
  if [ ! -d "$WT" ]; then
    git worktree add --detach "$WT" "refs/holds/pr$n" >/dev/null 2>&1 || true
  fi
  if [ -d "$WT" ]; then
    git -C "$WT" checkout --detach --quiet "refs/holds/pr$n" 2>/dev/null || true
    SHA=$(git -C "$WT" rev-parse --short HEAD)
    "$SCRIPTS/wt-keep.sh" "$WT" 3 >/dev/null
    CHANGED=$(git -C "$WT" diff --name-only "origin/$DIFF_BASE...HEAD" 2>/dev/null \
      | grep -Ec "\.($EXT_RE)\$" || true)
    JAR=""
    if [ "${CHANGED:-0}" -gt 0 ]; then
      HAVE=$(ls "$WT"/$FACTORY_HOLDOUT_ARTIFACT_GLOB 2>/dev/null \
        | grep -v '\.original$' | head -1 || true)
      HEAD_TS=$(git -C "$WT" log -1 --format=%ct)
      JAR_TS=$(stat -c %Y "$HAVE" 2>/dev/null || echo 0)
      if [ -n "$HAVE" ] && [ "$JAR_TS" -ge "$HEAD_TS" ] 2>/dev/null; then
        JAR=$HAVE
      else
        JAR=$(build_jar "$WT")
      fi
    fi
    [ -z "$JAR" ] && JSTR=null || JSTR="\"$JAR\""
  else
    WT=null; SHA="?"; JSTR=null
  fi
  [ $FIRST -eq 1 ] && FIRST=0 || echo_n ","
  echo_n "{\"label\":\"pr$n\",\"issue\":$n,\"sha\":\"$SHA\",\"dir\":\"$WT\",\"jar\":$JSTR}"
done
echo_n "]}"
echo
echo "# targets: the open PRs of the current milestone from the JSON above; the scenarios"
echo "# come from the tester's instructions; base heads are read with git show (no stand)"
