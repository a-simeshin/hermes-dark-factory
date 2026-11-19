# Verifying the artifact, read-only

The supervisor proves every claim with the repository, the board or CI. All commands below read; none of them changes the factory. Run long commands detached on the server with output to a file, and kill whole process groups on timeout.

| Claim | Check |
|---|---|
| "Snapshot taken" | `git -C ~/.hermes log --oneline -1` and `git -C ~/.hermes show --stat HEAD` |
| "Test is green" | rerun that test yourself: `HERMES_HOME=$(mktemp -d) timeout 300 python3 ~/.hermes/scripts/<test>.py` |
| "Full suite green" | the verbatim last line of `run-tests.py`, `TEST TOTAL scripts/: N/N green` |
| "Card fixed or closed" | query the board database read-only (`sqlite3 'file:<board db>?mode=ro'` or Python with `mode=ro`): status and the last events |
| "Comment posted" | `gh api repos/<org>/<repo>/issues/comments/<id> --jq .html_url` |
| "PR ready" | `gh pr view <N> --json headRefOid,mergeable,statusCheckRollup,comments` |
| "CI green after merge" | `gh run list --branch <base> --limit 1` then `gh run watch <id> --exit-status` |
| "Quota fine" | grep the gateway log for the provider's quota codes |
| "Release facts checked" (tag, version, what shipped) | `~/.hermes/scripts/tag-sync-check.sh <repo>` exits 0; the release point from `git ls-remote --tags origin`, not the local tag |
| "No runaway processes" | `free -m` and `pgrep -c python3` on the server |
