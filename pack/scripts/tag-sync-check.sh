#!/usr/bin/env bash
# tag-sync-check.sh: compare every local tag with the tags on origin.
#
# A plain `git fetch` never overwrites a tag that already exists locally, so a
# clone can keep a release tag that points at an older commit than the one
# published. Any release fact read from that clone ("what already shipped in
# vX", a changelog audit, a release-prep review) then answers the wrong
# question. Run this before release work and trust its verdict, not
# `git rev-parse` on the clone.
#
# Usage: tag-sync-check.sh [repo-dir] [--force]
#   repo-dir   repository to check (default: the current directory)
#   --force    first force-fetch all tags from origin
#              (git fetch origin '+refs/tags/*:refs/tags/*'), then check.
#              Worktrees of the same repository share its tags, so fixing the
#              main clone is enough. A tag that exists only locally (GONE) is
#              never deleted: push it or delete it by hand.
#
# Output: one line per problem, STALE (local tag differs from origin),
# MISSING (on origin, not local), GONE (local, not on origin).
# Exit: 0 in sync, 1 divergence, 2 not a repository or origin unreachable.
set -uo pipefail

repo=.
force=0
for a in "$@"; do
  case "$a" in
    --force) force=1 ;;
    -h|--help) sed -n '2,21p' "$0"; exit 0 ;;
    *) repo=$a ;;
  esac
done
cd "$repo" 2>/dev/null && git rev-parse --git-dir >/dev/null 2>&1 \
  || { echo "not a git repository: $repo" >&2; exit 2; }

if [ "$force" = 1 ]; then
  git fetch -q origin '+refs/tags/*:refs/tags/*' || exit 2
fi
remote=$(git ls-remote --tags origin) || { echo "cannot list tags of origin" >&2; exit 2; }

remote_hash() {  # exact ref name -> hash, empty when absent
  printf '%s\n' "$remote" | awk -v r="$1" '$2 == r { print $1 }'
}

bad=0
while IFS=$'\t' read -r rhash rref; do
  [ -n "$rref" ] || continue
  case "$rref" in *'^{}') continue ;; esac
  tag=${rref#refs/tags/}
  lhash=$(git rev-parse -q --verify "refs/tags/$tag" 2>/dev/null)
  if [ -z "$lhash" ]; then
    echo "MISSING locally: $tag (origin $rhash)"
    bad=1
  elif [ "$lhash" != "$rhash" ]; then
    # an annotated tag recreated with the same target is still in sync
    lpeel=$(git rev-parse -q --verify "refs/tags/$tag^{commit}" 2>/dev/null)
    rpeel=$(remote_hash "refs/tags/$tag^{}")
    [ -n "$rpeel" ] || rpeel=$rhash
    if [ "$lpeel" != "$rpeel" ]; then
      echo "STALE: $tag local=$lhash origin=$rhash"
      bad=1
    fi
  fi
done <<< "$remote"

while IFS= read -r lref; do
  [ -n "$lref" ] || continue
  if [ -z "$(remote_hash "$lref")" ]; then
    echo "GONE on origin: ${lref#refs/tags/}"
    bad=1
  fi
done < <(git for-each-ref --format='%(refname)' refs/tags/)

if [ "$bad" = 0 ]; then
  echo "tags in sync with origin ($repo)"
  exit 0
fi
echo "TAG DIVERGENCE: fix with: $0 $repo --force" >&2
exit 1
