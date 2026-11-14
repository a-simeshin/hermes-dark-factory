# Reference git hooks

These are the three git hooks the factory used, translated to English and stripped of the original project's stack. They serve two places at once: the git repository that snapshots the knowledge layers of `~/.hermes`, and every checkout and worktree of the target project, through one `core.hooksPath`.

## Hooks

| Hook | What it enforces | Bypass |
|---|---|---|
| `commit-msg` | No em dash (U+2014) in the commit message. The house style bans it, and most violations came in through commit subjects, past `pre-commit`. | `ALLOW_EMDASH=1` |
| `pre-commit` | (1) No silent deletion of source files while "fixing a test" (pattern in `SRC_DELETE_PATTERN`). (2) No em dash added to text the team authors; the path filter is set for the `~/.hermes` layer and deliberately skips upstream skills, so `hermes update` can keep updating them. | `ALLOW_SRC_DELETE=1`, `ALLOW_EMDASH=1` |
| `pre-push` | (1) Every github.com link in added markdown or code comments must exist (`prepush-link-guard.py`, only inside Hermes sessions). (2) No commit may carry the stock Hermes git identity (`prepush-identity-guard.py`). (3) The project's static gate (`FACTORY_STATIC_GATE`), run against a base derived from the branch's upstream or the nearest release line. | `SKIP_LINK_GUARD=1`, `SKIP_STATIC_GATE=1`; no bypass for identity |

The bypass variables exist for the human owner. Agents are told in their SOUL that a gate saying "no" means fix the cause or block the card with a question; using a bypass, `--no-verify` or a force-push to get past a gate is forbidden.

## Install

```bash
mkdir -p ~/.hermes/githooks
cp commit-msg pre-commit pre-push ~/.hermes/githooks/
chmod +x ~/.hermes/githooks/*

# the knowledge-layer repository
git -C ~/.hermes config core.hooksPath ~/.hermes/githooks
# the target project's main clone (worktrees inherit it)
git -C ~/projects/<repo> config core.hooksPath ~/.hermes/githooks
```

`pre-push` calls `prepush-link-guard.py` and `prepush-identity-guard.py` from `$HERMES_SCRIPTS` (default `~/.hermes/scripts`); these guard scripts are not shipped yet, so remove those two calls or provide your own guards, and set `FACTORY_BOT_EMAIL` for the identity guard. Set `FACTORY_STATIC_GATE` to your fastest static check, for example `npm run lint` or a formatter in check mode; the command receives the diff base ref as its first argument. `FACTORY_BASE_PATTERN` (an extended regex over `origin/...` refs) tells the hook which branches count as release lines.

## Adapting

- If you keep the em-dash rule, adjust the `pre-commit` path filter to the directories your team writes. Do not normalise punctuation in upstream skill files.
- The source-deletion ban is cheap insurance against a worker that "fixes" a failing test by deleting the code under test. Widen the pattern to your languages.
- Keep hooks fast. A hook that takes minutes trains everyone, agents included, to look for the bypass.
