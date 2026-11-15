#!/bin/bash
# wt-keep.sh <worktree-dir> [days]: life marker for a worktree outside a card.
#
# Why: worktree-gc.py (cron, hourly) removes worktrees that have no live
# card, no PR and no changes; a build-comparison worktree once vanished in
# the middle of a capture round that way. A .wt-keep marker with a date
# extends the worktree's life; the gc skips it until the date has passed.
# Default: 7 days.
set -euo pipefail
d="${1:?usage: wt-keep.sh <worktree-dir> [days]}"
n="${2:-7}"
printf '%s\n' "$(date -d "+${n} days" +%F)" > "${d%/}/.wt-keep"
echo ".wt-keep until $(cat "${d%/}/.wt-keep") in ${d%/}"
