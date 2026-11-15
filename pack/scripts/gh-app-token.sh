#!/usr/bin/env bash
# Mint a GitHub App installation token (TTL 1h). Usage: GH_TOKEN=$(gh-app-token.sh)
# Needs: $HERMES_HOME/secrets/gh-app.env (APP_ID, INSTALLATION_ID) and gh-app.pem
set -euo pipefail
HH="${HERMES_HOME:-$HOME/.hermes}"
source "$HH/secrets/gh-app.env"
PEM="$HH/secrets/gh-app.pem"
now=$(date +%s)
b64() { openssl base64 -A | tr "+/" "-_" | tr -d "="; }
header=$(printf "{\"alg\":\"RS256\",\"typ\":\"JWT\"}" | b64)
payload=$(printf "{\"iat\":%d,\"exp\":%d,\"iss\":\"%s\"}" $((now-60)) $((now+540)) "$APP_ID" | b64)
sig=$(printf "%s.%s" "$header" "$payload" | openssl dgst -sha256 -sign "$PEM" | b64)
jwt="$header.$payload.$sig"
curl -sf -X POST -H "Authorization: Bearer $jwt" -H "Accept: application/vnd.github+json" \
  "https://api.github.com/app/installations/$INSTALLATION_ID/access_tokens" | python3 -c "import json,sys; print(json.load(sys.stdin)[\"token\"])"
