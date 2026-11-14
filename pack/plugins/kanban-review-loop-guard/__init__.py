"""Review loop guard: the third return for rework sends the card to a human.

Hermes ships a loop breaker: a card that gets unblocked and blocked again for
the same reason `BLOCK_RECURRENCE_LIMIT` times (2 by default) goes to triage
with a `block_loop_detected` event instead of another round (docs:
user-guide/features/kanban.md, events table).

The review lane is explicitly excluded from that accounting:
`kanban_request_changes` routes a return "without block-loop accounting"
(same docs, tools table). The lane has no round counter by design, so nothing
ever stops it.

The cost is known. In one incident three chains reached 14, 13 and 10 cycles,
burned a noticeable amount of money on the architect model and spawned 69
cards without moving a single task forward. In another, one card reached the
fifth round over a single finding about the expected number of E2E tests.

The intervention point is a native one. The `kanban_task_claimed` hook fires
in the DISPATCHER process right before a worker is spawned, that is, before
the run spends anything. The hook is observational (its return value is
ignored), but it can block the card the same way the docs show: by calling the
board's own CLI command.

Threshold: two returns are normal work; the third means the finding cannot be
closed by the assignee's own means. Configurable:

    plugins:
      entries:
        kanban-review-loop-guard:
          limit: 3
"""

import logging
import os
import sqlite3
import time
import subprocess

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 3
BOARDS = os.path.join(
    os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes"),
    "kanban", "boards")
CMD_TIMEOUT = 90
# Marker phrase used both in the block reason and in the "already escalated"
# check. Keep them in sync.
ESCALATION_MARKER = "review loop stopped"


def _limit(ctx):
    try:
        value = ctx.get_config("limit", default=DEFAULT_LIMIT)
        value = int(value)
        return value if value >= 2 else DEFAULT_LIMIT
    except Exception:
        return DEFAULT_LIMIT


def _board_db(board):
    """Path to the board database. Without a board we cannot work: there are several boards."""
    if not board:
        return None
    path = os.path.join(BOARDS, board, "kanban.db")
    return path if os.path.exists(path) else None


def _rounds_and_status(db, task_id):
    """How many returns the card has and which status it is in.

    Read-only connection: the hook must not compete for the board's writer
    lock, and a read-write connect to a missing path would create an empty
    database file.
    """
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
    try:
        rounds = con.execute(
            "select count(*) from task_events "
            "where task_id = ? and kind = 'changes_requested'",
            (task_id,),
        ).fetchone()[0]
        row = con.execute(
            "select status from tasks where id = ?", (task_id,)
        ).fetchone()
        return rounds, (row[0] if row else None)
    finally:
        con.close()


def _already_escalated(db, task_id):
    """Do not block twice: one escalation per card."""
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
    try:
        row = con.execute(
            "select count(*) from task_comments "
            "where task_id = ? and lower(body) like ?",
            (task_id, "%" + ESCALATION_MARKER + "%"),
        ).fetchone()
        return bool(row and row[0])
    finally:
        con.close()


def _block(board, task_id, rounds):
    reason = (
        "Review loop stopped: %d returns for rework on one chain. "
        "The built-in review lane does not count rounds, so the "
        "return-rework-review cycle can run forever. A human decision is "
        "needed: close the finding on your own authority, declare it "
        "non-blocking and move it to the backlog, or split the card." % rounds
    )
    out = subprocess.run(
        ["hermes", "kanban", "--board", board, "block", task_id, reason,
         "--kind", "needs_input"],
        capture_output=True, text=True, timeout=CMD_TIMEOUT,
    )
    return out.returncode == 0, (out.stderr or out.stdout).strip()[:200]


SEEN_FILE = os.path.join(
    os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes"),
    "kanban", "review-loop-guard.seen")


def _mark_seen(task_id, board):
    """"Hook was called" marker: proof of life that does not depend on logs."""
    try:
        os.makedirs(os.path.dirname(SEEN_FILE), exist_ok=True)
        with open(SEEN_FILE, "w", encoding="utf-8") as fh:
            fh.write("%s pid=%d task=%s board=%s\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid(), task_id, board))
    except Exception:
        pass


def _on_claimed(task_id=None, board=None, **kw):
    if not task_id:
        return
    _mark_seen(task_id, board)
    try:
        db = _board_db(board)
        if not db:
            return
        rounds, status = _rounds_and_status(db, task_id)
        if rounds < _limit_cache[0]:
            return
        if status in ("done", "archived", "blocked"):
            return
        if _already_escalated(db, task_id):
            return
        ok, detail = _block(board, task_id, rounds)
        if ok:
            logger.warning(
                "review-loop-guard: %s stopped after %d returns", task_id, rounds)
        else:
            logger.warning(
                "review-loop-guard: could not block %s: %s", task_id, detail)
    except Exception as exc:  # best-effort hook: it must never break the dispatcher
        logger.debug("review-loop-guard: %s", exc)


_limit_cache = [DEFAULT_LIMIT]


def register(ctx):
    _limit_cache[0] = _limit(ctx)
    ctx.register_hook("kanban_task_claimed", _on_claimed)
    logger.info("review-loop-guard: subscribed to kanban_task_claimed, limit %d returns",
                _limit_cache[0])
