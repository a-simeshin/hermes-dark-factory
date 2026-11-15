#!/bin/bash
# Thin cron wrapper (tester profile): all the logic lives in the shared
# script, which sits under the knowledge snapshot (git in the Hermes home):
# make changes there, not here.
# A safety net: kill the holdout stands a few hours after the run starts,
# no-agent, silent when the registry is clean.
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HERE="$(cd "$(dirname "$0")" && pwd)"
[ -x "$HERE/holdout-stands-kill.py" ] || HERE="$HERMES_HOME/scripts"
exec "$HERE/holdout-stands-kill.py" "$@"
