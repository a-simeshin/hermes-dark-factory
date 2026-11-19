#!/usr/bin/env bash
# Webhook filter: pass only the owner's events to the agent run. Branches:
#   issue_comment        -> only "created" by the owner;
#   pull_request_review  -> only "submitted" by the owner;
#   issues               -> "opened", "milestoned" or "labeled" by the owner.
# Issue events reach the run so that a release blocker the owner files gets
# an executor card at once, not after the owner nudges with a comment.
# Mirror issues written by the bot (the card already exists; their body
# carries FACTORY_MIRROR_FOOTER) and events by anyone else are dropped: a run
# on them would create a duplicate card (the native-or-live rule).
# The owner login comes from FACTORY_OWNER_LOGIN (an environment variable
# wins over factory.env).
# Exit 1 = drop the event; no owner configured = drop everything (fail closed).
HH="${HERMES_HOME:-$HOME/.hermes}"
owner="${FACTORY_OWNER_LOGIN:-}"
footer="${FACTORY_MIRROR_FOOTER:-}"
if [ -f "$HH/factory.env" ]; then
  . "$HH/factory.env"
  owner="${owner:-${FACTORY_OWNER_LOGIN:-}}"
  footer="${footer:-${FACTORY_MIRROR_FOOTER:-}}"
fi
footer="${footer:-Mirrored from kanban card}"
[ -n "$owner" ] || exit 1
payload=$(cat)
action=$(jq -r ".action // empty" <<<"$payload")
login=$(jq -r ".sender.login // empty" <<<"$payload")
if jq -e '.comment' >/dev/null 2>&1 <<<"$payload"; then
  [ "$action" = "created" ] || exit 1
elif jq -e '.pull_request' >/dev/null 2>&1 <<<"$payload"; then
  [ "$action" = "submitted" ] || exit 1
else
  [ "$action" = "opened" ] || [ "$action" = "milestoned" ] \
    || [ "$action" = "labeled" ] || exit 1
  jq -r '.issue.body // ""' <<<"$payload" \
    | grep -qF "$footer" && exit 1
fi
[ "$login" = "$owner" ] || exit 1
# Enrich: issue_comment carries the number in .issue.number,
# pull_request_review in .pull_request.number, issues in .issue.number.
# Expose a single .pr_number so the prompt template renders for all three
# event kinds.
echo "$payload" | jq '. + {pr_number: (.pull_request.number // .issue.number // null)}'
