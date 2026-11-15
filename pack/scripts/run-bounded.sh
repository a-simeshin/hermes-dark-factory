#!/bin/bash
# run-bounded.sh: a background process with a lifetime set at start.
#
# Why: a session that hit its iteration limit once kept working (a build
# notification opened a new turn), and its test stands lived on until the
# orphan reaper, sharing ports with a parallel worker. A rule in prose
# ("shut down before you report") does not hold even in a session devoted to
# that rule. A mechanism instead of a rule: every stand an agent starts must go
# through this wrapper.
#
# Usage: run-bounded.sh --owner <owner> [--ttl-min 45] -- <command...>
#   --owner   who started it: session:local-xxx | card:t_XXXX | any string
#   --ttl-min lifetime in minutes (default 45); timeout kills it when it expires
#
# Writes a line to the registry $HERMES_HOME/cache/bounded-procs.jsonl:
#   {"pid":N,"owner":"...","started":epoch,"deadline":epoch,"cmd":"..."}
# The watchdog (check_orphan_stands in hermes-health.py) exempts live registry
# entries and kills java/node/chromium processes older than 2 hours that have
# no entry. The watchdog cleans the registry: dead pids and expired entries
# are removed.

set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
OWNER="" ; TTL_MIN=45 ; REGISTRY="$HERMES_HOME/cache/bounded-procs.jsonl"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --owner) OWNER="$2"; shift 2 ;;
    --ttl-min) TTL_MIN="$2"; shift 2 ;;
    --registry) REGISTRY="$2"; shift 2 ;;
    --) shift; break ;;
    *) echo "run-bounded: unknown argument: $1" >&2; exit 2 ;;
  esac
done
if [[ -z "$OWNER" || $# -eq 0 ]]; then
  echo "Usage: run-bounded.sh --owner <owner> [--ttl-min 45] [--registry PATH] -- <command...>" >&2
  exit 2
fi

mkdir -p "$(dirname "$REGISTRY")"
STARTED=$(date +%s)
DEADLINE=$(( STARTED + TTL_MIN * 60 ))
PID=$$
CMD_JSON=$(printf '%s' "$*" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')
printf '{"pid":%d,"owner":%s,"started":%d,"deadline":%d,"cmd":%s}\n' \
  "$PID" "$(printf '%s' "$OWNER" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')" \
  "$STARTED" "$DEADLINE" "$CMD_JSON" >> "$REGISTRY"

# exec: the wrapper's pid becomes the pid of timeout; TERM on timeout reaches the child.
exec timeout --kill-after=30s "${TTL_MIN}m" "$@"
