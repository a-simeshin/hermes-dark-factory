#!/usr/bin/env bash
# Webhook filter: pass only issue_comment / pull_request_review events that
# the owner created or submitted. The owner login comes from
# FACTORY_OWNER_LOGIN (an environment variable wins over factory.env).
# Exit 1 = drop the event; no owner configured = drop everything (fail closed).
HH="${HERMES_HOME:-$HOME/.hermes}"
owner="${FACTORY_OWNER_LOGIN:-}"
if [ -z "$owner" ] && [ -f "$HH/factory.env" ]; then
  . "$HH/factory.env"
  owner="${FACTORY_OWNER_LOGIN:-}"
fi
[ -n "$owner" ] || exit 1
payload=$(cat)
action=$(jq -r ".action // empty" <<<"$payload")
login=$(jq -r ".sender.login // empty" <<<"$payload")
[ "$action" = "created" ] || [ "$action" = "submitted" ] || exit 1
[ "$login" = "$owner" ] || exit 1
# Enrich: issue_comment carries the number in .issue.number,
# pull_request_review in .pull_request.number. Expose a single
# .pr_number so the prompt template renders for both event kinds.
echo "$payload" | jq '. + {pr_number: (.pull_request.number // .issue.number // null)}'
