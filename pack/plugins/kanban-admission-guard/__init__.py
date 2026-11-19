"""Claim-time guard of the admission gate: an unapproved subtree is not worked.

The problem: the admission gate (part of the board watchdog) runs on a
schedule, about once an hour, and parks cards whose links are not approved
by the owner. Between two runs the platform can promote such a card on its
own (a dependency is released when its parent closes), and the dispatcher
hands it to a worker before the next gate run sees the leak.

The intervention point is a native one, the same as in
kanban-review-loop-guard: the `kanban_task_claimed` hook fires in the
dispatcher process right before a worker is spawned, before the run spends
anything. The guard reads the admission cache
`$HERMES_HOME/kanban/boards/<board>/admission-approved.json` (rewritten by
every admission gate run: approved issues, and open PRs against approved
issues) and checks the `#N` references of the card itself and of all its
ancestors in task_links. If none of them is approved, the card is taken off
the queue with the native `kanban schedule`, using the same
"admission gate:" marker as the gate.

Fail-open in every direction:
- no cache, or a cache older than 3 hours (the gate has not run for a
  while): allow. Parking on a stale snapshot is worse than a miss, and the
  next gate run catches it;
- any read error: allow. The hook is best-effort and must never break the
  dispatcher;
- a card in running/done/archived/blocked/scheduled: untouched;
- a card without any references: untouched, the scheduled gate decides.

Cache format:
    {"updated": "YYYY-MM-DD HH:MM:SS", "issues": {"<N>": "<milestone>"},
     "prs": [<N>, ...]}
"""

import json
import logging
import os
import re
import sqlite3
import subprocess
import time

logger = logging.getLogger(__name__)

BOARDS = os.path.join(
    os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes"),
    "kanban", "boards")
CACHE_NAME = "admission-approved.json"
CACHE_MAX_AGE = 3 * 3600
CMD_TIMEOUT = 90
ADMISSION_MARKER = "admission gate:"
CLAIM_GUARD_TAG = "admission claim-guard"
SKIP_STATUSES = ("done", "archived", "blocked", "scheduled", "running")

# Replaced in tests.
RUN = subprocess.run


def _load_cache(board):
    """The admission cache, or None when it is missing, broken or stale."""
    path = os.path.join(BOARDS, board, CACHE_NAME)
    try:
        with open(path, encoding="utf-8") as fh:
            cache = json.load(fh)
        stamp = time.mktime(time.strptime(cache["updated"],
                                          "%Y-%m-%d %H:%M:%S"))
        if time.time() - stamp > CACHE_MAX_AGE:
            return None
        return cache
    except Exception:
        return None


def _connect_ro(db):
    """Read-only: the hook must not compete for the board's writer lock."""
    return sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)


def _texts_of(con, task_id):
    row = con.execute(
        "select title, body from tasks where id = ?", (task_id,)).fetchone()
    texts = [(row[0] or "") + "\n" + (row[1] or "")] if row else []
    texts.extend(c[0] or "" for c in con.execute(
        "select body from task_comments where task_id = ?", (task_id,)))
    return texts


def _card_refs(db, task_id):
    """`#N` numbers in the title, body and comments of the card and of all
    its ancestors."""
    con = _connect_ro(db)
    try:
        texts = _texts_of(con, task_id)
        parents = {}
        for pid, cid in con.execute(
                "select parent_id, child_id from task_links"):
            parents.setdefault(cid, []).append(pid)
        seen, stack = set(), list(parents.get(task_id, []))
        while stack:
            p = stack.pop()
            if p in seen:
                continue
            seen.add(p)
            texts.extend(_texts_of(con, p))
            stack.extend(parents.get(p, []))
        nums = set()
        for text in texts:
            nums.update(int(n) for n in re.findall(r"#(\d{1,5})\b", text))
        return nums
    finally:
        con.close()


def _status_of(db, task_id):
    con = _connect_ro(db)
    try:
        row = con.execute("select status from tasks where id = ?",
                          (task_id,)).fetchone()
        return row[0] if row else None
    finally:
        con.close()


def _schedule(board, task_id, nums):
    reason = ("%s %s: the card's own references (%s) are not approved by "
              "the owner; the next admission gate run will confirm the "
              "decision" % (ADMISSION_MARKER, CLAIM_GUARD_TAG,
                            ", ".join("#%d" % n for n in sorted(nums))))
    out = RUN(["hermes", "kanban", "--board", board, "schedule", task_id,
               reason], capture_output=True, text=True, timeout=CMD_TIMEOUT)
    return out.returncode == 0


def _on_claimed(task_id=None, board=None, **kw):
    if not task_id or not board:
        return
    try:
        db = os.path.join(BOARDS, board, "kanban.db")
        if not os.path.exists(db):
            return
        cache = _load_cache(board)
        if cache is None:
            return
        if _status_of(db, task_id) in SKIP_STATUSES:
            return
        nums = _card_refs(db, task_id)
        if not nums:
            return
        approved = {int(n) for n in cache.get("issues", {})}
        approved |= {int(n) for n in cache.get("prs", [])}
        if nums & approved:
            return
        if _schedule(board, task_id, nums):
            logger.warning("admission-guard: %s taken off the queue at claim "
                           "(references %s not approved)", task_id,
                           sorted(nums))
    except Exception as exc:  # best-effort: the dispatcher matters more
        logger.debug("admission-guard: %s", exc)


def register(ctx):
    ctx.register_hook("kanban_task_claimed", _on_claimed)
    logger.info("admission-guard: subscribed to kanban_task_claimed")
