# save-lesson-before-complete: lessons

Distilled lessons behind the skill, each as rule, cause and application: why
learning is an explicit worker step, how to name a culprit responsibly, and
why every "done" item needs an artifact link.

**Saving knowledge is your explicit tool call, not background magic.** Why: Hermes has a background self-improvement review, but it does not fire in a card run. The memory review starts only after about ten user turns in ONE session, and a card run is one turn; the skill review runs as a fork in a worker thread, and a one-shot process exits right after answering, killing the thread. A measurement found zero background-review entries in every profile's database and zero "Background review complete" lines in all logs. Apply: before `kanban_complete`, write the lesson yourself with `memory` or `skill_manage`.

**Before naming a culprit, test the hypothesis against ALL facts.** Why: missing files were blamed on "the owner's SSH session" while a disk-cleanup plugin deleted them on its session-end hook; the cleanup log showed the deletions at the moment the agent session closed and the owner session had no delete commands at all. Apply: the suspect list includes your own machinery (plugin hooks, cron, session finalisers); check the subsystem log, the suspect sessions' terminal/write/patch calls in their state database and directory mtimes against message times; never accuse a human without evidence. Write "restored" only after `ls` of the file and running it, not after writing it; restore files inside `~/.hermes` by copying from a snapshot or a session record, never with git commands that rewrite the tree, and report to the owner.

**"Done" only with a link to the artifact.** Why: a status was claimed in a report but never set on the issue; the report was written before checking the artifact. Apply: every "done" item carries an identifier obtained after the action: the `url` from the `gh api .../comments` response for a comment, the `id` from `kanban create` output for a card, the `sha` from `git rev-parse HEAD` for a commit. No artifact: the item says "not done", not "claimed".
