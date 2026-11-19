#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pr-hygiene: a rebase card is quenched on an already MERGEABLE PR.

Origin: a rebase card was created while the PR was CONFLICTING; by the time
a worker claimed it, the head already sat on the target (MERGEABLE, CI green,
a reviewer PASS), and a push from the card would have reset the PASS. The
same class as a stale rework arm, but the check lives in pr-hygiene, which
owns the life cycle of rebase cards, and runs on EVERY pass: a card can go
stale between create and claim.

Run: python3 test_pr_hygiene_stale_rebase.py
"""
import importlib.util
import os
import sqlite3
import stat
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.update({
    "HERMES_HOME": tempfile.mkdtemp(prefix="ph-stale-rebase-"),
    "FACTORY_REPO": "acme/widget",
    "FACTORY_REPO_PATH": "/srv/hermes/widget",
    "FACTORY_BOARD": "main",
    "FACTORY_OWNER_LOGIN": "owner-login",
    "FACTORY_OWNER_CHAT": "telegram:000000000",
    "FACTORY_BASE_BRANCHES": "main",
})
SCRIPT = os.environ.get("PR_HYGIENE_SCRIPT") or os.path.join(
    HERE, "pr-hygiene.py")
spec = importlib.util.spec_from_file_location("pr_hygiene_stale", SCRIPT)
if spec is None or spec.loader is None:
    print("SKIP: cannot load pr-hygiene.py")
    sys.exit(0)
mod = importlib.util.module_from_spec(spec)
sys.modules["pr_hygiene_stale"] = mod
spec.loader.exec_module(mod)

FAILS = []


def want(name, cond, detail=""):
    print("%s %s%s" % ("ok  " if cond else "FAIL", name,
                       (" :: %s" % (detail,)) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def make_board(tasks):
    """A mini board with live rebase cards. Returns (db, log, fake hermes)."""
    tmp = tempfile.mkdtemp(prefix="ph-stale-")
    db = os.path.join(tmp, "kanban.db")
    con = sqlite3.connect(db)
    con.execute("create table tasks (id text primary key,"
                " idempotency_key text, status text)")
    for tid, key, status in tasks:
        con.execute("insert into tasks values (?,?,?)", (tid, key, status))
    con.commit()
    con.close()
    # a fake hermes: writes archive calls to a log
    log = os.path.join(tmp, "archive.log")
    fake = os.path.join(tmp, "hermes")
    with open(fake, "w") as fh:
        fh.write("#!/bin/sh\necho \"$@\" >> %s\n" % log)
    os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)
    return db, log, fake


def run_gate(tasks, info, dry_run=False):
    db, log, fake = make_board(tasks)
    real_db, real_bin = mod.BOARD_DB, mod.HERMES_BIN
    comments = []
    real_comment = mod.comment_card
    mod.BOARD_DB, mod.HERMES_BIN = db, fake
    mod.comment_card = lambda tid, text: comments.append((tid, text)) or True
    try:
        findings = mod.stale_rebase_gate(info, dry_run=dry_run)
    finally:
        mod.BOARD_DB, mod.HERMES_BIN = real_db, real_bin
        mod.comment_card = real_comment
    archived = []
    if os.path.exists(log):
        with open(log) as fh:
            archived = [line.split() for line in fh.read().splitlines()]
    return findings, comments, archived


def main():
    # 1. a ready card on a MERGEABLE PR: marker comment + archive
    findings, comments, archived = run_gate(
        [("t_reb408", "coder-pr408-rebase-c8c4f24", "ready")],
        {408: {"detail": {"mergeable": "MERGEABLE"}}})
    want("1 MERGEABLE PR: the card is archived",
         archived == [["kanban", "--board", mod.BOARD_SLUG,
                       "archive", "t_reb408"]],
         str(archived))
    want("1 marker comment",
         any(tid == "t_reb408" and text.startswith(mod.REBASE_STALE_MARKER)
             for tid, text in comments), str(comments))
    want("1 finding mergeflow-stale-rebase:408",
         any(f[0] == "mergeflow-stale-rebase:408" for f in findings),
         str([f[0] for f in findings]))

    # 2. a CONFLICTING PR: the card is needed, nothing happens
    findings, comments, archived = run_gate(
        [("t_reb413", "coder-pr413-rebase-c8c4f24", "blocked")],
        {413: {"detail": {"mergeable": "CONFLICTING"}}})
    want("2 CONFLICTING PR: the card lives, no actions",
         not comments and not archived and not findings,
         "%s %s %s" % (comments, archived, findings))

    # 3. mergeable UNKNOWN (GitHub is computing): conservatively kept
    findings, comments, archived = run_gate(
        [("t_reb408", "coder-pr408-rebase-c8c4f24", "ready")],
        {408: {"detail": {"mergeable": "UNKNOWN"}}})
    want("3 UNKNOWN mergeable: the card lives",
         not comments and not archived and not findings,
         "%s %s %s" % (comments, archived, findings))

    # 4. running on a MERGEABLE PR: a stop directive, NO archive
    findings, comments, archived = run_gate(
        [("t_run", "coder-pr408-rebase-c8c4f24", "running")],
        {408: {"detail": {"mergeable": "MERGEABLE"}}})
    want("4 running: a stop directive without archive",
         archived == []
         and any(tid == "t_run" and "Do NOT push" in text
                 for tid, text in comments),
         "%s %s" % (comments, archived))
    want("4 finding mergeflow-stale-rebase-running:408",
         any(f[0] == "mergeflow-stale-rebase-running:408" for f in findings),
         str([f[0] for f in findings]))

    # 5. the PR left OPEN: the card is not needed
    findings, comments, archived = run_gate(
        [("t_reb411", "coder-pr411-rebase-c8c4f24", "todo")],
        {})  # 411 is not among the open ones
    want("5 PR not OPEN: the card is archived",
         any("t_reb411" in a for a in archived) and archived,
         str(archived))
    want("5 the reason says \"no longer OPEN\"",
         any("no longer OPEN" in text for _, text in comments),
         str(comments))

    # 6. dry-run: only a finding, the world is untouched
    findings, comments, archived = run_gate(
        [("t_reb408", "coder-pr408-rebase-c8c4f24", "ready")],
        {408: {"detail": {"mergeable": "MERGEABLE"}}}, dry_run=True)
    want("6 dry-run: no actions, a would-finding",
         not comments and not archived
         and any(f[0] == "mergeflow-stale-rebase-would:408"
                 for f in findings),
         "%s %s %s" % (comments, archived, [f[0] for f in findings]))

    if FAILS:
        print("STALE REBASE (pr-hygiene): FAILURES: %s" % ", ".join(FAILS))
        sys.exit(1)
    print("STALE REBASE (pr-hygiene): tests green")


if __name__ == "__main__":
    main()
