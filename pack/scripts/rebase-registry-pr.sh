#!/usr/bin/env bash
# Catches a PR up with its base branch and resolves a conflict in the
# append-only numbered registry file (FACTORY_REGISTRY_FILE).
#
# While the registry stays one numbered file, every merge into the base breaks
# all other open PRs that append to it too. Resolving it by hand once is not
# enough: the next merge brings the conflict back. The script makes the
# resolution a one-command job. If the registry is the only conflict it is
# resolved by resolve-registry.py, committed and pushed to the PR head; any
# other conflict stops the script.
#
# Usage: rebase-registry-pr.sh <PR number>
# Settings (factory.env): FACTORY_REPO_PATH, FACTORY_REGISTRY_FILE (path of the
# registry relative to the repository), FACTORY_BOT_EMAIL (commit identity),
# FACTORY_BOT_NAME (optional, default factory-bot).
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
[ -f "$HERMES_HOME/factory.env" ] && . "$HERMES_HOME/factory.env"
HERE="$(cd "$(dirname "$0")" && pwd)"
: "${FACTORY_REPO_PATH:?FACTORY_REPO_PATH is not set: add it to $HERMES_HOME/factory.env}"
: "${FACTORY_REGISTRY_FILE:?FACTORY_REGISTRY_FILE is not set: add it to $HERMES_HOME/factory.env}"
: "${FACTORY_BOT_EMAIL:?FACTORY_BOT_EMAIL is not set: add it to $HERMES_HOME/factory.env}"
BOT_NAME="${FACTORY_BOT_NAME:-factory-bot}"

repo="$FACTORY_REPO_PATH"
pr="$1"
work=/tmp/rebase-pr$pr
registry="$FACTORY_REGISTRY_FILE"

cd "$repo"
git fetch -q origin
base=$(gh pr view "$pr" --json baseRefName -q .baseRefName)
head=$(gh pr view "$pr" --json headRefName -q .headRefName)
echo "PR #$pr: $head -> $base"

rm -rf "$work"
git worktree remove --force "$work" 2>/dev/null || true
git worktree add -q "$work" -b "rebase-pr$pr-work" "origin/$head"
cd "$work"

if git merge --no-commit --no-ff "origin/$base" >/dev/null 2>&1; then
  echo "  no conflicts, the merge is clean"
else
  conflicts=$(git status --porcelain | awk '$1=="UU"{print $2}')
  if [ "$conflicts" != "$registry" ]; then
    echo "  THE CONFLICT IS NOT ONLY IN THE REGISTRY, resolve by hand:"
    echo "$conflicts" | sed 's/^/    /'
    exit 1
  fi
  git show "origin/$base:$registry" > /tmp/registry-base.txt
  git show "origin/$head:$registry" > /tmp/registry-branch.txt
  python3 "$HERE/resolve-registry.py" "$registry" /tmp/registry-base.txt /tmp/registry-branch.txt | sed 's/^/  /'
  git add "$registry"
fi

if git diff --cached --quiet && git diff --quiet; then
  echo "  nothing to commit, the branch already contains the base"
  cd "$repo"; git worktree remove --force "$work"; git branch -qD "rebase-pr$pr-work"
  exit 0
fi

git -c user.email="$FACTORY_BOT_EMAIL" -c user.name="$BOT_NAME" \
  commit -q -m "merge: catch up with origin/$base

Only the registry file conflicted: both sides append to the same numbered list,
so a new entry upstream renumbers this branch entry and shifts its indentation.
Resolution keeps the base order and takes this branch version of any entry it
rewrites."
git push -q origin "HEAD:$head"
echo "  pushed to $head"
cd "$repo"; git worktree remove --force "$work"; git branch -qD "rebase-pr$pr-work"
