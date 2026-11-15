"""A card was closed but its work is not committed: open a catch-up card.

An agent's report is its words about the work, not the work itself. In the
incident that motivated this plugin, a card closed as completed with the text
"fixed the 6 failing UI test groups", while the working tree still held one
unsaved file: no commit, no push, the PR unchanged, 78 minutes of work hanging
by a thread.

The `kanban_task_completed` hook cannot cancel the completion (it is
best-effort and fires after the transaction commits), but it can turn a silent
loss into a visible card with a ready-made patch.

Which repositories are checked: the card's workspace if it is inside git, plus
the roots from the plugin settings:

    plugins:
      entries:
        kanban-artifact-guard:
          repos:
            - ~/projects/<repo>
"""

import hashlib
import logging
import os
import re
import subprocess

logger = logging.getLogger(__name__)

RESCUE_DIR = os.environ.get("HERMES_RESCUE_DIR") or os.path.join(
    os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes"), "rescue")
GIT_TIMEOUT = 20


def _git(repo, *args):
    out = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True, text=True, timeout=GIT_TIMEOUT,
    )
    return out.stdout.strip() if out.returncode == 0 else ""


def _is_hermes_home(tree):
    """The agent's home (~/.hermes or $HERMES_HOME) is never a work tree."""
    try:
        home = os.path.realpath(
            os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes"))
        return os.path.realpath(tree) == home
    except Exception:
        return False


def _worktrees(repo):
    """The main tree plus every worktree git knows about."""
    trees = [repo]
    for line in _git(repo, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):].strip()
            if path and path not in trees:
                trees.append(path)
    return trees


def _dirty(tree):
    """List of changed paths, empty when the tree is clean."""
    status = _git(tree, "status", "--porcelain")
    return [ln for ln in status.splitlines() if ln.strip()]


def _repos_from_settings(ctx):
    try:
        repos = ctx.get_config("repos", default=None)
    except Exception:
        repos = None
    if isinstance(repos, str):
        repos = [repos]
    return [os.path.expanduser(str(p)) for p in (repos or []) if str(p).strip()]


WHAT_HAPPENED = {
    "kanban_task_completed": "was closed as completed",
    "kanban_task_blocked": "went to blocked",
    "kanban_task_claimed": "was claimed for work, and from the previous run",
}

# Shared repositories are scanned only where the card LEAVES work.
# At claim time another worker may be editing them right now.
EVENTS_SCANNING_SHARED_REPOS = ("kanban_task_completed", "kanban_task_blocked")


def _someone_is_working(conn):
    """Whether any card on the board is in status running.

    While someone works, shared repositories must not be scanned: their
    in-progress edits are indistinguishable from lost work. In one incident
    the guard, triggered by blocking a card that had never run, found a
    worktree where a live worker was editing code at that very moment, and
    opened a priority-95 card "commit and push". That ships someone else's
    unfinished work.
    """
    try:
        row = conn.execute(
            "select count(*) from tasks where status = ?", ("running",)
        ).fetchone()
        return bool(row and row[0])
    except Exception:
        # Could not check: assume someone is working. A missed finding is
        # cheaper than a false push.
        return True


def _has_real_run(task):
    """Whether the card was ever claimed for work.

    The signal is `tasks.started_at`: it is set on the first claim. A card
    blocked straight from the queue could not have left anything behind, and
    its `started_at` is empty.

    Do NOT count by `task_runs.worker_pid`: in the installation this was built
    on that column was empty for every row, including real finished runs, and
    such a check would silently disable the guard entirely (caught by a test).
    """
    if task is None:
        return True
    # hasattr, not getattr(..., None): on a card that was never claimed the
    # field EXISTS and equals None, and that must not be confused with
    # "the field is missing".
    if hasattr(task, "started_at"):
        return bool(task.started_at)
    try:
        return bool(task["started_at"])
    except Exception:
        return True


WORKSPACE_ID = re.compile(r"^t_[0-9a-f]{6,}$")


def _owner_of(tree):
    """The card that owns a work tree, if the tree is that card's workspace.

    The shared-repository scan is triggered by ONE card's event, while the
    dirty tree may belong to ANOTHER. In one incident the guard reported
    uncommitted work from another card's PR worktree as the work of the card
    that was being blocked. The confusion is expensive: a worker could push
    someone else's unfinished change without looking.
    """
    base = os.path.basename(tree.rstrip("/"))
    if WORKSPACE_ID.match(base):
        return base
    parent = os.path.basename(os.path.dirname(tree.rstrip("/")))
    if WORKSPACE_ID.match(parent):
        return parent
    return None


def _body_head(task_id, event, report, owners=()):
    """Message header. The text must match the event: at claim time the card
    was not "closed as completed", it is only starting work.

    ``owners`` are the cards that own the trees found, other than
    ``task_id`` itself. It is passed as a list rather than derived from
    ``report``: the report is already rendered text, and parsing structure out
    of it means writing a branch that silently never fires."""
    owners = [o for o in owners if o and o != task_id]
    note = ""
    if owners:
        note = ("\n\nWARNING: part of this work belongs to other cards (%s). "
                "The scan was triggered by the event of %s, but the tree is "
                "someone else's: sort it out with the owner, do not push "
                "blindly." % (", ".join(owners), task_id))
    return (
        "Card %s %s, but uncommitted work is left in the working tree. "
        "The report was accepted instead of the artifact.%s\n\n%s"
        % (task_id, WHAT_HAPPENED.get(event, "finished"), note,
           "\n".join(report))
    )


def _connect(kb, board=None):
    """Open the board. ``connect`` left ``kanban_db`` for ``kanban_db_connect``
    when the module was split (Hermes v0.21+); older builds still export it."""
    connect = getattr(kb, "connect", None)
    if connect is None:
        from hermes_cli.kanban_db_connect import connect
    return connect(board=board) if board else connect()


def _make_handler(ctx, event="kanban_task_completed"):
    def on_completed(task_id=None, board=None, assignee=None, **_kw):
        if not task_id:
            return
        try:
            from hermes_cli import kanban_db as kb
        except Exception:
            return
        try:
            conn = _connect(kb, board)
        except Exception:
            # Warning, not debug: a guard that cannot reach the board is a
            # dead guard, and that must be visible in the gateway log.
            logger.warning(
                "artifact-guard: board unavailable (board=%s), card %s was "
                "not inspected", board, task_id, exc_info=True)
            return
        try:
            task = kb.get_task(conn, task_id)
            roots = []
            ws = getattr(task, "workspace_path", None) if task else None
            if ws and os.path.isdir(ws):
                top = _git(ws, "rev-parse", "--show-toplevel")
                if top:
                    roots.append(top)
            # At CLAIM time shared repositories are not scanned: another
            # worker may be editing them right now, and its live edits would
            # be declared lost work (this produced two false priority-95
            # cards). Leftovers from this card's previous run are visible in
            # its own tree.
            # Shared repositories are scanned only if the card really ran and
            # nobody on the board is working right now. Otherwise the finding
            # is almost certainly someone else's live work.
            #
            # Deliberate trade-off: the card's OWN tree is always scanned; only
            # the shared-repository scan is skipped. A finding missed there is
            # not lost forever: the tree stays dirty and will be found on the
            # next completion when nobody is working. A false "commit and push"
            # card for live edits ships someone else's unfinished work, and
            # that is irreversible.
            skip_reason = None
            if event in EVENTS_SCANNING_SHARED_REPOS:
                if not _has_real_run(task):
                    # Card straight from the queue: nothing to report, this is
                    # normal and would only be noise.
                    skip_reason = None
                elif _someone_is_working(conn):
                    skip_reason = ("a card on the board is running, its edits "
                                   "are indistinguishable from lost work")
                else:
                    roots.extend(_repos_from_settings(ctx))

            found = []
            seen = set()
            for root in roots:
                if not os.path.isdir(root) or _is_hermes_home(root):
                    continue
                for tree in _worktrees(root):
                    if tree in seen or _is_hermes_home(tree):
                        continue
                    seen.add(tree)
                    lines = _dirty(tree)
                    if lines:
                        found.append((tree, lines))
            if not found:
                if skip_reason:
                    # A silent skip is indistinguishable from "all clean".
                    # Leave a trace.
                    try:
                        kb.add_comment(
                            conn, task_id, "artifact-guard",
                            "Shared repositories were not scanned: %s. The "
                            "card's own tree was checked and is clean." % skip_reason)
                    except Exception:
                        pass
                return

            # Save the patch right away: the next worker may clean the tree.
            os.makedirs(RESCUE_DIR, exist_ok=True)
            saved = []
            for tree, _lines in found:
                name = "%s-%s.patch" % (task_id, os.path.basename(tree))
                path = os.path.join(RESCUE_DIR, name)
                diff = _git(tree, "diff", "HEAD")
                if diff:
                    with open(path, "w", encoding="utf-8") as fh:
                        fh.write(diff + "\n")
                    saved.append(path)

            report = []
            for tree, lines in found:
                owner = _owner_of(tree)
                if owner and owner != task_id:
                    report.append("%s  (belongs to card %s):" % (tree, owner))
                else:
                    report.append("%s:" % tree)
                report.extend("  " + ln for ln in lines[:20])
                if len(lines) > 20:
                    report.append("  ... %d more" % (len(lines) - 20))
            owners = sorted({o for o in (_owner_of(tree) for tree, _ in found) if o})
            body_head = _body_head(task_id, event, report, owners)
            if saved:
                body_head += "\n\nPatches saved: " + ", ".join(saved)

            kb.add_comment(conn, task_id, "artifact-guard", body_head)

            # Idempotency key from the set of dirty paths: a repeated run over
            # the same tree does not multiply cards.
            digest = hashlib.sha1(
                "\n".join(ln for _t, ls in found for ln in ls).encode("utf-8")
            ).hexdigest()[:12]
            kb.create_task(
                conn,
                title="Commit the work of card %s" % task_id,
                body=body_head + (
                    "\n\nDone = commit and push, `git status --porcelain` is "
                    "empty, the report includes the output of "
                    "`git log --oneline -1`. If the edits are unwanted, explain "
                    "that in a comment and clean the tree explicitly."
                ),
                assignee=assignee or None,
                priority=95,
                idempotency_key="artifact-guard-%s" % digest,
                created_by="artifact-guard",
            )
        except Exception:
            logger.debug("artifact-guard: could not file the finding", exc_info=True)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    return on_completed


def register(ctx):

    # Blocking loses work the same way completion does: the card leaves
    # processing, and the uncommitted diff stays in the tree and does not
    # always survive the next run.
    # claimed was added third: a run can die on the iteration limit or on a
    # timeout, and then neither completed nor blocked happens. The claim of
    # the card always happens, so leftovers in the tree are picked up at the
    # start of the next run.
    for event in ("kanban_task_completed", "kanban_task_blocked", "kanban_task_claimed"):
        ctx.register_hook(event, _make_handler(ctx, event))
    logger.info("artifact-guard: subscribed to completed, blocked and claimed")
