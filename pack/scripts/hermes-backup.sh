#!/usr/bin/env bash
# Nightly backup with the built-in Hermes tool.
#
# Previously: a hand-made tar over the Hermes home with a manual exclude list.
# `hermes backup` already excludes the vendored code copy, caches,
# node_modules and __pycache__, writes a zip and, most importantly, has a
# documented restore path (`hermes import <file>`) which the tar version never
# had. Measured on the same state: tar 2.67 GB against zip 1.12 GB.
#
# `hermes backup` takes several minutes and exits with rc=1 if even one file
# vanished meanwhile. Hourly cleanups (worktree garbage collection and
# others) start at the top of the hour and remove the cron/output and
# cron/external-workers trees right under the archiver. The archive is intact,
# but under set -e the zip rotation and the removal of legacy tar files were
# skipped. Rules:
#   - the output goes to a log, not to /dev/null;
#   - the verdict on the log is made by backup-verify.py: an ENOENT race is
#     normal, any other error fails the run (including rc!=0 without a single
#     ENOENT: an unknown failure);
#   - schedule the cron entry a few minutes past the hour (for example 04:23),
#     so it does not land where the cleanups run.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p ~/backups/logs
OUT=~/backups/hermes-$(date +%Y%m%d).zip
LOG=~/backups/logs/backup-$(date +%Y%m%d-%H%M%S).log

set +e
~/.local/bin/hermes backup -o "$OUT" >"$LOG" 2>&1
RC=$?
set -e

# Verdict on the log (exit 1 stops the script before rotations and deletions).
python3 "$HERE/backup-verify.py" "$RC" "$LOG"

# Verify the archive before deleting anything: a broken backup is worse than a missing one.
unzip -tqq "$OUT" >/dev/null

# Keep a week of zips, as a week of tars was kept before.
ls -t ~/backups/hermes-*.zip 2>/dev/null | tail -n +8 | xargs -r rm -f

# Legacy from the tar era: rotation no longer touches those files. Do not
# delete them at once: wait until the new path has proven itself with three
# successful nights, and only then keep the single newest tar as insurance.
ZIPS=$(ls ~/backups/hermes-*.zip 2>/dev/null | wc -l)
if [ "$ZIPS" -ge 3 ]; then
  ls -t ~/backups/hermes-*.tar.gz 2>/dev/null | tail -n +2 | xargs -r rm -f
fi

# Keep the verdict logs for two weeks.
find ~/backups/logs -name 'backup-*.log' -mtime +14 -delete 2>/dev/null || true

printf "backup: %s (%s), archive verified, rc=%d, log %s\n" \
  "$OUT" "$(du -h "$OUT" | cut -f1)" "$RC" "$LOG"
