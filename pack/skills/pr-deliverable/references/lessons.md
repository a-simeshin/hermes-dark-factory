# pr-deliverable: distilled lessons

Delivery lessons reduced from an incident log to one entry each ("Rule. Why.
How to apply."): API limits, the prototype gate, PR noise, same-mechanism
fixes, garbage input, library contracts, frames, stale checks, house-style
restoration and stack hygiene.

1. **`gh pr view --json files` caps at 100 files.** Why: a large PR lost its
   UI files and was classified non-UI. How: at exactly 100, page through
   `gh api repos/<org>/<repo>/pulls/N/files`.
2. **A feature/ux issue starts with a prototype the owner accepts.** How:
   the implementation card is born blocked (`needs_input`, "prototype
   accepted"); its branch starts from the integration branch; a PR opened
   earlier is a finding and a reviewer [BLOCK].
3. **No noise: no empty acknowledgements, CI-trigger commits or
   close/reopen.** Why: noise reads as progress; PR state belongs to the
   owner. How: `gh run rerun <id> [--failed]` after reading the log; a 403
   means block and ask. Ask the owner only at real forks (incompatible
   behaviours, data risk, public contract change).
4. **After a rework, list every place with the same mechanism.** Why: a
   review names one path, the defect lives in the mechanism (second poller,
   second stack or transport, all callers, exception subclasses). How: in the
   reply, list each similar place and its fate.
5. **Parsing server data gets a garbage-input test.** Why: one stray `%`
   crashed a whole UI despite dozens of tests. How: validation or try/catch
   plus a malformed-input test before delivery.
6. **A library contract is calibrated by running the library.** Why: author
   and reviewer verified the same guess about an error format. How: a
   `Library contract: <artifact>:<version> <Class>#<method>` line, source or
   bytecode opened this session, one fixture produced by a real call.
7. **A UI-facing feature is checked end to end once before WCFY.** Why: the
   UI did not listen to the event, or the header name differed. How: read
   every layer this session and run it on a demo stand.
8. **A frame must contain what it proves.** How: open each image before
   pushing; report what is visible, not that a file exists.
9. **Evidence must be fresh and checkable.** How: a check older than the
   last gate or base change is rerun; a claim about another branch or PR
   comes with its command and output; red CI is read before rerun, a base
   flake gets an issue.
10. **Restoring a house-style-forbidden character from the base needs a
    rebuild, not a bypass.** Why: the pre-commit hook blocks even a
    restoration, and bypassing hooks is forbidden. How:
    `git add -A; TREE=$(git write-tree); git reset --hard <merge-base>;
    git read-tree -u --reset $TREE; git commit`, check `HEAD^{tree}` equals
    `$TREE`, full verify, `--force-with-lease`. To keep history, recreate
    only the commits that first rewrote base lines (`cherry-pick -n`,
    restore, `commit -C`) and `rebase --onto` the rest; `--amend` fails
    because the hook compares with HEAD.
11. **Stack hygiene.** How: build a stacked PR on the parent's FINAL head
    (`git log -1 origin/<parent>` before rebasing, re-check before delivery);
    after every rebase grep for conflict markers
    (`git grep -nE '^(<<<<<<<|\|\|\|\|\|\|\||>>>>>>>)'`) and build; never
    rebase a child before the parent merges (a separate card blocked on it).
12. **Infrastructure changes are never glued to a feature.** How: workflow
    and action bumps go in their own PR; together with a feature, RETURN.
13. **Line numbers from someone else's verdict are re-measured** on the
    current head and tied to its hash; explain differences in a comment.
