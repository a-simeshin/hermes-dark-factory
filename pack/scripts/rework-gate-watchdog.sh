#!/usr/bin/env bash
# Frequent pass of the rework gates only (the size of a fresh rework card,
# rework of an open PR lifted into the active band, rework cards on a head
# that has moved retired). The dispatcher picks up a fresh card within 2-7
# minutes, faster than the hourly watchdog; this job runs only the gates
# (no other checks, no burnout comments), every 5 minutes. The scheduler
# passes no arguments, hence the wrapper.
exec python3 "$(dirname "$0")/board-watchdog.py" --rework-gate-only
