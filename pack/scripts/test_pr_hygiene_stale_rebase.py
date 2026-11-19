#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pr-hygiene: a rebase card is quenched on an already MERGEABLE PR.

Origin: a rebase card was created while the PR was CONFLICTING; by the time
a worker claimed it, the head already sat on the target (MERGEABLE, CI green,
a reviewer PASS), and a push from the card would have reset the PASS. The
same class as a stale rework arm, but the check lives in pr-hygiene, which
owns the life cycle of rebase cards, and runs on EVERY pass: a card can go
stale between create and claim.

Shields: a card whose own push made the PR mergeable (it names the live
head in its own text) is kept, and so is a card while the current head has
no accept verdict (the review request of the new head is still owed). In-test
mutants of pr-hygiene.py (copies in a temp dir) must turn those cases red.

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


def make_board(tasks, claims=None):
    """A mini board with live rebase cards. Returns (db, log, fake hermes).

    claims maps a task id to a comment text: the card names a head in its
    own words (the own-push shield reads comments, run summaries and
    parking reasons)."""
    tmp = tempfile.mkdtemp(prefix="ph-stale-")
    db = os.path.join(tmp, "kanban.db")
    con = sqlite3.connect(db)
    con.execute("create table tasks (id text primary key,"
                " idempotency_key text, status text)")
    for tid, key, status in tasks:
        con.execute("insert into tasks values (?,?,?)", (tid, key, status))
    con.execute("create table task_comments"
                " (id integer primary key, task_id text, body text)")
    con.execute("create table task_runs (id integer primary key,"
                " task_id text, summary text)")
    con.execute("create table task_events (id integer primary key,"
                " task_id text, kind text, payload text)")
    for tid, body in (claims or {}).items():
        con.execute("insert into task_comments (task_id, body)"
                    " values (?,?)", (tid, body))
    con.commit()
    con.close()
    # a fake hermes: writes archive calls to a log
    log = os.path.join(tmp, "archive.log")
    fake = os.path.join(tmp, "hermes")
    with open(fake, "w") as fh:
        fh.write("#!/bin/sh\necho \"$@\" >> %s\n" % log)
    os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)
    return db, log, fake


def run_gate(tasks, info, dry_run=False, review="ok", head=None,
             claims=None, m=None):
    """review_state and pr_payload are stubbed: review "ok" (an accept
    verdict on the head) keeps the plain quench cases; gh is never called."""
    mod = m or globals()["mod"]
    db, log, fake = make_board(tasks, claims)
    real_db, real_bin = mod.BOARD_DB, mod.HERMES_BIN
    comments = []
    real_comment = mod.comment_card
    real_review, real_payload = mod.review_state, mod.pr_payload
    mod.BOARD_DB, mod.HERMES_BIN = db, fake
    mod.comment_card = lambda tid, text: comments.append((tid, text)) or True
    mod.review_state = lambda n: (review, "")
    mod.pr_payload = lambda n: {"headRefOid": head} if head else {}
    try:
        findings = mod.stale_rebase_gate(info, dry_run=dry_run)
    finally:
        mod.BOARD_DB, mod.HERMES_BIN = real_db, real_bin
        mod.comment_card = real_comment
        mod.review_state, mod.pr_payload = real_review, real_payload
    archived = []
    if os.path.exists(log):
        with open(log) as fh:
            archived = [line.split() for line in fh.read().splitlines()]
    return findings, comments, archived


def shield_battery(m, want):
    """The own-push and no-verdict shields of the quench."""
    head = "a24ff95b" * 5
    mergeable = {408: {"detail": {"mergeable": "MERGEABLE"}}}

    # 7. own push: the card claims the live head, MERGEABLE was reached by
    #    its push; the delivery cycle is unfinished, the card lives.
    findings, comments, archived = run_gate(
        [("t_reb408", "coder-pr408-rebase-c8c4f24", "scheduled")],
        mergeable, head=head, m=m,
        claims={"t_reb408": "Rebase pushed, head a24ff95, waiting for CI."})
    want("7 own push: the card lives, no actions",
         not comments and not archived
         and any(f[0] == "mergeflow-stale-rebase-shield:408"
                 for f in findings),
         "%s %s %s" % (comments, archived, [f[0] for f in findings]))

    # 8. own push on a running card: no stop directive either.
    findings, comments, archived = run_gate(
        [("t_run", "coder-pr408-rebase-c8c4f24", "running")],
        mergeable, head=head, m=m,
        claims={"t_run": "Pushed the rebased head A24FF95B."})
    want("8 running with its own push: no stop directive",
         not comments and not archived
         and any(f[0] == "mergeflow-stale-rebase-shield:408"
                 for f in findings),
         "%s %s %s" % (comments, archived, [f[0] for f in findings]))

    # 9. a foreign head: the card names another sha, the shield is off and
    #    the plain quench applies (accept verdict on the head).
    findings, comments, archived = run_gate(
        [("t_reb408", "coder-pr408-rebase-c8c4f24", "ready")],
        mergeable, head=head, m=m,
        claims={"t_reb408": "Rebase pushed, head 1234567."})
    want("9 foreign head: quenched as before",
         any("t_reb408" in a for a in archived)
         and any(f[0] == "mergeflow-stale-rebase:408" for f in findings),
         "%s %s" % (archived, [f[0] for f in findings]))

    # 10. no accept verdict on the head: the review request is still owed,
    #     the card lives.
    for state in ("stale", "none"):
        findings, comments, archived = run_gate(
            [("t_reb408", "coder-pr408-rebase-c8c4f24", "ready")],
            mergeable, review=state, head=head, m=m)
        want("10 review_state=%s: the card lives" % state,
             not comments and not archived
             and any(f[0] == "mergeflow-stale-rebase-noverdict:408"
                     for f in findings),
             "%s %s %s" % (comments, archived, [f[0] for f in findings]))

    # 11. the PR left OPEN: the shields do not apply.
    findings, comments, archived = run_gate(
        [("t_reb411", "coder-pr411-rebase-c8c4f24", "todo")],
        {}, review="none", head=head, m=m,
        claims={"t_reb411": "Rebase pushed, head a24ff95."})
    want("11 PR not OPEN: archived despite the shields",
         any("t_reb411" in a for a in archived), str(archived))


# (name, old, new): each mutant of pr-hygiene.py must turn the shield
# battery red. Copies live in a temp dir; this file is never re-run.
MUTANTS = [
    ("own-push shield removed",
     "            if head7 and claims_head \\\n",
     "            if False and claims_head \\\n"),
    ("no-verdict shield removed",
     '            if verdict_state != "ok":\n'
     '                findings.append((\n'
     '                    "mergeflow-stale-rebase-noverdict',
     '            if False:\n'
     '                findings.append((\n'
     '                    "mergeflow-stale-rebase-noverdict'),
]


def run_mutants():
    with open(SCRIPT, encoding="utf-8") as fh:
        src = fh.read()
    tmp = tempfile.mkdtemp(prefix="ph-stale-mut-")
    for i, (name, old, new) in enumerate(MUTANTS):
        if src.count(old) != 1:
            want("mutant anchor present: %s" % name, False)
            continue
        path = os.path.join(tmp, "pr_hygiene_mut_%d.py" % i)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(src.replace(old, new))
        sys.path.insert(0, HERE)        # factory_config for the copy
        mspec = importlib.util.spec_from_file_location(
            "pr_hygiene_stale_mut_%d" % i, path)
        mutant = importlib.util.module_from_spec(mspec)
        mspec.loader.exec_module(mutant)
        # the copy has no sibling board-watchdog: lend it the real one
        mutant.board_watchdog = mod.board_watchdog
        failed = []
        shield_battery(mutant, lambda n, c, d="": c or failed.append(n))
        want("mutant red: %s" % name, bool(failed))


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

    shield_battery(mod, want)
    run_mutants()

    if FAILS:
        print("STALE REBASE (pr-hygiene): FAILURES: %s" % ", ".join(FAILS))
        sys.exit(1)
    print("STALE REBASE (pr-hygiene): tests green")


if __name__ == "__main__":
    main()
