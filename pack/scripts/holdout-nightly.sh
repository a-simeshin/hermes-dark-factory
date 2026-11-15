#!/bin/bash
# Thin cron wrapper (tester profile) of the nightly hidden-acceptance run: all
# the logic lives in holdout-nightly-prep.sh, which sits under the knowledge
# snapshot (git in the Hermes home): make changes there, not here. The stdout
# of the prep script goes into the agent's prompt.
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HERE="$(cd "$(dirname "$0")" && pwd)"
[ -x "$HERE/holdout-nightly-prep.sh" ] || HERE="$HERMES_HOME/scripts"
exec "$HERE/holdout-nightly-prep.sh" "$@"
