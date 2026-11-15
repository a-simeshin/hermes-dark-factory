#!/bin/bash
# Deferred stock gateway restart: hermes gateway restart WITHOUT -y.
# Meant to live in a separate systemd unit (a clean environment, not a
# descendant of the gateway), so the "from inside the gateway" check lets it
# through, and the refusal while turns are live is handled by a retry, not by
# a flag. The gate is not bypassed: without -y the gateway itself decides when
# the window opens.
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
LOG="$HERMES_HOME/cache/scratch/gw-window.log"
mkdir -p "$(dirname "$LOG")"
echo "$(date '+%F %T') start: waiting for a window for the stock restart" >> "$LOG"
for i in $(seq 1 60); do
  sleep 120
  out=$(env -i HOME="$HOME" PATH="$HOME/.bun/bin:$HOME/.local/bin:$HOME/bin:/usr/local/bin:/usr/bin" \
        HERMES_HOME="$HERMES_HOME" \
        hermes gateway restart 2>&1)
  rc=$?
  echo "$(date '+%F %T') attempt $i rc=$rc: $out" >> "$LOG"
  if [ $rc -eq 0 ]; then
    echo "$(date '+%F %T') restart done the stock way, without -y" >> "$LOG"
    exit 0
  fi
done
echo "$(date '+%F %T') no window within 2 hours, giving up" >> "$LOG"
exit 1
