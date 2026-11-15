#!/bin/bash
# Cron wrapper (ops profile): peak-hours pause of the kanban dispatcher.
# All the logic lives in the shared script, which sits under the knowledge
# snapshot (git in the Hermes home): make changes there, not here.
#
# The no-agent cron "peak-pause-tick" (every 15 min) calls this wrapper. A
# normal tick is silent (SKIP lines are normal: a foreign owner stop, outside
# the window); noise appears only if the tick fails. Full log:
# $HERMES_HOME/logs/kanban-dispatch-peak-pause.log
export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
# Sibling script in this directory first (the pack layout), else the shared
# scripts directory (the wrapper installed into a profile's scripts/).
HERE="$(cd "$(dirname "$0")" && pwd)"
[ -x "$HERE/kanban-dispatch-peak-pause.py" ] || HERE="$HERMES_HOME/scripts"
out=$("$HERE/kanban-dispatch-peak-pause.py" tick 2>&1)
rc=$?
if [ "$rc" -ne 0 ]; then
  echo "peak-pause tick FAILED (rc=$rc):"
  printf '%s\n' "$out"
  exit "$rc"
fi
exit 0
