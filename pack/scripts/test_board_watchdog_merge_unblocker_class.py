#!/usr/bin/env python3
"""Merge-unblocker: a formality stands before conflict-debt rebases.

A [visual-delta] card (or a "What changes for you" card of the watchdog) is
the last thing left before a merge, but its body carries no verdict text, so a
gate that recognises the formality only by a verdict ("CHANGES REQUESTED" +
minor in the card body) never sees it. Two behaviours are checked here:
  1. the formality class by the card's work (merge_unblocker_card_class):
     [visual-delta], WCFY, update-branch, green CI; the acceptance of the PR
     code is looked up in verdict cards and owner review mirrors (comments
     with author=github), not in the candidate's own body;
  2. MERGE_UNBLOCKER_PRIO is 100: above the conflict-debt rebases p96-p99.

Fixture: a visual-delta card with a done rework card, an owner review mirror
(author=github) and a done reviewer verdict that quotes a FOREIGN APPROVED
(of another PR); a WCFY card of the watchdog with the code acceptance in a done
card of the PR (the second evidence source); two rebases at p96 OLDER than the
visual-delta card (the order is explained only by priority); negatives: a
formality without accepted code, a formality next to a live test blocker, a
rebase of an accepted PR (the class path must not capture pr-hygiene rebases).

After the pass: the visual-delta card first in dispatcher order (priority DESC,
created_at ASC), then the WCFY card, then the p96 rebases, negatives at p80.
Idempotent: a repeated pass is silent.

Mutants (each must turn the test red):
  M_CLASS  the class by title is off -> both positives are not raised;
  M_EVID   code acceptance always "found" -> the formality without a verdict
           is raised;
  M_PRIO   priority back to 90 -> p90 loses to the p96 rebases;
  M_CROSS  the neighbour code check is off -> the formality is raised next to
           a live test blocker of the same PR.
"""
import importlib.util
import os
import shutil
import sqlite3
import sys
import tempfile
import time

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
HOME = tempfile.mkdtemp(prefix="muc-home-")
os.environ["HERMES_HOME"] = HOME
os.environ["FACTORY_REPO"] = "acme/widget"
os.environ["FACTORY_REPO_PATH"] = "/srv/hermes/widget"
os.environ["FACTORY_BOARD"] = "main"
os.environ["FACTORY_OWNER_LOGIN"] = "owner-login"
os.environ["FACTORY_OWNER_CHAT"] = "telegram:000000000"
os.environ["FACTORY_BASE_BRANCHES"] = "develop/1.x,develop/2.x"
BW_PATH = os.environ.get("BW_WATCHDOG_PATH") or os.path.join(
    SCRIPTS, "board-watchdog.py")
SLUG = "mucfix"
NOW = int(time.time())
H = 3600

BODY_278VD = """PR #278 violates the visual-delta rule:

- there is no '## Before / After' section (first, before any text)

Format: the body starts with the `## Before / After` section.
- UI diff: a | Before | After | table, one screenshot per cell;
  one screen in one state, PNG in the assets repository (raw).
- non-UI: a mermaid diagram of the delta (flowchart/sequence/classes).
After the section at most 5 lines, the rest goes into details.
Capture with one call: ~/.hermes/scripts/visual-delta-shot.py

https://github.com/acme/widget/pull/278
"""

# mirror of an owner review on the done rework card of #278 (author=github):
# the only text on the board that carries the code acceptance of #278
BODY_278MIRROR = ("GitHub @owner-login in PR #278 (owner review, "
                  "CHANGES REQUESTED: minor, head dd4cf30): change "
                  "accepted, two merge prerequisites block the merge: "
                  "(1) `## Before / After` section at the top of the PR "
                  "body, a two-row rename table is enough; (2) develop/1.x "
                  "moved 9 commits past the head: update-branch + green CI "
                  "on the updated head. Owner will merge himself once both "
                  "done; no 2.x twin needed. Reaction: single rework card "
                  "for coder, dispatcher spawns worker.")

# done reviewer verdict of #278: quotes the APPROVED of a FOREIGN verdict
# (#208), it must not count as the acceptance context of #278 (APPROVED is
# outside the context regex)
BODY_278REV = """PR #278: verdict on 1.x transport-error test name alignment.

Gist: the only cosmetic leftover from the owner verdict on PR #208
(APPROVED: minor): the transport-error test name is aligned with the
2.x twin. Diff: exactly 2 files. Chain: issue #195 (Refs)."""

BODY_240WCFY = """PR #240 (head 4b1d77c): after PASS there is no fresh dated
"What changes for you" comment. Post WCFY (English, brief): the
user-visible effect of the change. Watchdog auto-arm."""

BODY_240EV = """Owner review verdict on PR #240 (head 4b1d77c): CHANGES
REQUESTED (minor), code accepted, only merge prerequisites remain.

1. [BLOCK] No "What changes for you" comment on the PR.
2. [BLOCK] update-branch: develop/1.x moved 3 commits past this head."""

BODY_999MAJ = """Owner review on PR #999 (head 77a01b2):
CHANGES REQUESTED: major. The error delivery over SSE is not readable by the
client, the error path needs rework and a test for it. The formalities were
never reached."""

BODY_998EV = """Owner review verdict on PR #998 (head 31c04de): CHANGES
REQUESTED (minor), code accepted, only merge prerequisites remain.

1. [BLOCK] No `## Before / After` section in the PR body."""

BODY_998TEST = """Owner review PR #998, BLOCK 1 (head 31c04de).

The test emitError_afterClose_returnsFalse is declared, but the "after close"
part is not verified. Mutant (verified by the owner): a call after terminate
stays green. An assert that catches the mutant is needed."""

BODY_150EV = """Owner review verdict on PR #150 (head e5f2c31): CHANGES
REQUESTED (minor), code accepted, only merge prerequisites remain.
update-branch is done, a rebase onto the new base after the neighbour's merge
remains."""

# (tid, title, body, assignee, status, prio, created_at)
CARDS = [
    # conflict-debt rebases p96, OLDER than all: the order is decided by
    # priority alone
    ("t_rb150", "rebase PR #150 onto develop/1.x@e5f2c31", BODY_150EV,
     "coder", "ready", 96, NOW - 41 * H),
    ("t_rb187", "rebase PR #187 onto develop/1.x@b7c90e2",
     "Rebase #187 onto the new base develop/1.x after the merge of #166.",
     "coder", "ready", 96, NOW - 40 * H),
    # negatives
    ("t_vd999", "[visual-delta] PR #999: Before / After section",
     "Capture Before/After for PR #999 and insert the section into the PR"
     " body.", "coder", "ready", 80, NOW - 5 * H),
    ("t_999maj", "[coder] PR #999 rework: SSE error delivery (owner review)",
     BODY_999MAJ, "coder", "done", 80, NOW - 6 * H),
    ("t_vd998", "[visual-delta] PR #998: Before / After section",
     "Capture Before/After for PR #998 and insert the section into the PR"
     " body.", "coder", "ready", 80, NOW - 4 * H),
    ("t_998ev", "PR #998 rework: Before/After (owner review, head 31c04de)",
     BODY_998EV, "coder", "done", 80, NOW - 4 * H - 60),
    ("t_998test", "[coder] PR #998 BLOCK1: test does not catch the mutant",
     BODY_998TEST, "coder", "ready", 80, NOW - 3.5 * H),
    # positive 1: visual-delta of #278, ready for ~2.5 hours
    ("t_278vd", "[visual-delta] PR #278: Before / After section",
     BODY_278VD, "coder", "ready", 80, NOW - 2.5 * H),
    ("t_278rw", "PR #278 rework: table in body + update-branch"
     " (head dd4cf30)",
     "Rework per owner review #278: update-branch is done (new head "
     "dd4cf30, CI green). The verdict mirror is in the comments.",
     "coder", "done", 80, NOW - 3 * H),
    ("t_278rev", "[reviewer] PR #278: verdict on 1.x transport-error test"
     " name alignment (head dd4cf300)",
     BODY_278REV, "reviewer", "done", 100, NOW - 8 * H),
    ("t_278wcfy", "[coder] PR #278 (head dd4cf30): fresh \"What changes "
     "for you\" (watchdog auto-arm)",
     "WCFY for #278 is published (a dated comment in the PR).",
     "coder", "done", 95, NOW - 30 * H),
    # positive 2: the watchdog's WCFY card, acceptance in a done PR card
    ("t_240wcfy", "[coder] PR #240 (head 4b1d77c): fresh \"What changes "
     "for you\" (watchdog auto-arm)",
     BODY_240WCFY, "coder", "ready", 80, NOW - 1 * H),
    ("t_240ev", "PR #240 rework: What-changes-for-you (owner review,"
     " head 4b1d77c)",
     BODY_240EV, "coder", "done", 80, NOW - 2 * H),
]

COMMENTS = [
    # (task_id, author, body, created_at)
    ("t_278rw", "github", BODY_278MIRROR, NOW - 3 * H + 120),
]

POSITIVE = ["t_278vd", "t_240wcfy"]
NEGATIVE = ["t_vd999", "t_vd998"]
REBASES = ["t_rb150", "t_rb187"]


def make_board(root):
    bdir = os.path.join(root, "boards", SLUG)
    os.makedirs(bdir, exist_ok=True)
    con = sqlite3.connect(os.path.join(bdir, "kanban.db"))
    con.execute("""CREATE TABLE tasks (
        id TEXT PRIMARY KEY, title TEXT, body TEXT, assignee TEXT,
        status TEXT NOT NULL, priority INTEGER DEFAULT 0,
        created_by TEXT, created_at INTEGER NOT NULL,
        started_at INTEGER, completed_at INTEGER)""")
    con.execute("""CREATE TABLE task_comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
        author TEXT NOT NULL, body TEXT NOT NULL,
        created_at INTEGER NOT NULL)""")
    for tid, title, body, assignee, status, prio, created in CARDS:
        con.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,NULL,NULL)",
                    (tid, title, body, assignee, status, prio, "user",
                     int(created)))
    for tid, author, body, created in COMMENTS:
        con.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at)"
            " VALUES (?,?,?,?)", (tid, author, body, int(created)))
    con.commit()
    con.close()
    return bdir


class Recorder(object):
    def __init__(self, mod):
        self.mod = mod
        self.calls = []

    def __call__(self, cmd, timeout=120):
        self.calls.append(cmd)
        return "ok"


def load_bw(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def prepare(root, src_path=BW_PATH, name="bw_muc"):
    bdir = make_board(root)
    mod = load_bw(src_path, name)
    mod.BOARDS = os.path.join(root, "boards")
    rec = Recorder(mod)
    mod.run = rec
    return mod, rec, bdir


def edits(rec):
    out = []
    for cmd in rec.calls:
        if "edit" in cmd and "--priority" in cmd:
            i = cmd.index("edit")
            out.append((cmd[i + 1], int(cmd[cmd.index("--priority") + 1])))
    return out


def run_gate(mod, rec):
    findings = mod.merge_unblocker_gate(SLUG)
    con = sqlite3.connect(os.path.join(mod.BOARDS, SLUG, "kanban.db"))
    for tid, prio in edits(rec):
        con.execute("UPDATE tasks SET priority = ? WHERE id = ?", (prio, tid))
    con.commit()
    prios = dict(con.execute("SELECT id, priority FROM tasks"))
    con.close()
    return findings, prios


def dispatch_order(prios):
    # dispatcher order in the ready+coder queue (positions ready for
    # hand-out): done / reviewer p100 cards are not in the selection
    ready_coder = [c for c in CARDS
                   if c[3] == "coder" and c[4] == "ready"]
    order = sorted(ready_coder, key=lambda c: (-prios[c[0]], c[6], c[0]))
    return [c[0] for c in order]


def check_positive(prios):
    problems = []
    prio = EXPECT_PRIO[0]
    for tid in POSITIVE:
        if prios.get(tid) != prio:
            problems.append("not raised %s (p=%s)" % (tid, prios.get(tid)))
    for tid in NEGATIVE:
        if prios.get(tid) == prio:
            problems.append("negative raised %s" % tid)
    for tid in REBASES:
        if prios.get(tid) != 96:
            problems.append("rebase %s touched (p=%s), the class path has no "
                            "right to raise pr-hygiene rebases"
                            % (tid, prios.get(tid)))
    order = dispatch_order(prios)
    if order[0] != "t_278vd":
        problems.append("the first is not #278: %s" % order[:4])
    if order[1] != "t_240wcfy":
        problems.append("the second is not WCFY #240: %s" % order[:4])
    if set(order[2:4]) != set(REBASES):
        problems.append("p96 rebases do not follow the formalities: %s"
                        % order[:6])
    return problems, order


EXPECT_PRIO = [100]


def main():
    failures = []
    root = tempfile.mkdtemp(prefix="muc-")
    try:
        mod, rec, _ = prepare(os.path.join(root, "main"))
        EXPECT_PRIO[0] = mod.MERGE_UNBLOCKER_PRIO

        # 1. dry-run: exactly two candidates, no CLI calls
        findings = mod.merge_unblocker_gate(SLUG, apply=False)
        dry_tids = sorted(f[0].split(":")[-1] for f in findings)
        if dry_tids != sorted(POSITIVE):
            failures.append("dry-run: %s" % dry_tids)
        if rec.calls:
            failures.append("dry-run: CLI calls were made: %s" % rec.calls)
        bytid = dict((f[0].split(":")[-1], f[2]) for f in findings)
        if "owner review mirror" not in bytid.get("t_278vd", ""):
            failures.append("acceptance source of #278 is not the mirror: %s"
                            % bytid.get("t_278vd", "")[:120])
        if "verdict in the PR card" not in bytid.get("t_240wcfy", ""):
            failures.append("acceptance source of #240 is not the card: %s"
                            % bytid.get("t_240wcfy", "")[:120])

        # 2. apply: two raised, rebases untouched, #278 first
        findings, prios = run_gate(mod, rec)
        problems, order = check_positive(prios)
        if problems:
            failures.append("apply: %s" % problems)
        comments = [c for c in rec.calls if "comment" in c]
        if len(comments) != len(POSITIVE):
            failures.append("marker comments %d, expected %d"
                            % (len(comments), len(POSITIVE)))
        for c in comments:
            if "merge-unblocker" not in " ".join(c):
                failures.append("comment without the marker: %s" % c)
        if EXPECT_PRIO[0] != 100:
            failures.append("MERGE_UNBLOCKER_PRIO is not p100: %d"
                            % EXPECT_PRIO[0])

        # 3. idempotency: a repeated pass is silent
        rec.calls = []
        findings2, _ = run_gate(mod, rec)
        if rec.calls:
            failures.append("repeated pass is not silent: %s" % edits(rec))
        if findings2:
            failures.append("repeated pass gave findings: %s" % findings2)

        # 4. mutants
        mutants = [
            ("M_CLASS",
             "    for name, rx in MERGE_UNBLOCKER_CARD_CLASSES:\n"
             "        if rx.search(title or \"\"):\n"
             "            return name",
             "    for name, rx in MERGE_UNBLOCKER_CARD_CLASSES:\n"
             "        if False and rx.search(title or \"\"):\n"
             "            return name",
             "positives not raised: the class formality is not recognised"),
            ("M_EVID",
             "                ev = merge_unblocker_pr_accepted("
             "pr, rows, comments)",
             "                ev = (merge_unblocker_pr_accepted("
             "pr, rows, comments) or \"mutant\")",
             "#999 is raised without accepted code"),
            ("M_PRIO",
             "MERGE_UNBLOCKER_PRIO = 100",
             "MERGE_UNBLOCKER_PRIO = 90",
             "p90 loses to the p96 rebases, #278 is not first"),
            ("M_CROSS",
             "        if code_left:\n"
             "            continue",
             "        if False:\n"
             "            continue",
             "#998 is raised next to a live test blocker of the same PR"),
        ]
        for mname, old, new, expect in mutants:
            src = open(BW_PATH).read()
            if src.count(old) != 1:
                failures.append("%s: the anchor occurs %d times"
                                % (mname, src.count(old)))
                continue
            mroot = os.path.join(root, mname.lower())
            os.makedirs(mroot, exist_ok=True)
            mpath = os.path.join(mroot, "board-watchdog.py")
            open(mpath, "w").write(src.replace(old, new))
            mmod, mrec, _ = prepare(mroot, src_path=mpath,
                                    name="bw_muc_" + mname.lower())
            _, mprios = run_gate(mmod, mrec)
            mprobs, _ = check_positive(mprios)
            if not mprobs:
                failures.append("%s NOT caught: %s" % (mname, expect))
            else:
                print("  mutant %s caught: %s" % (mname, mprobs[0]))
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(HOME, ignore_errors=True)

    print("=" * 64)
    if failures:
        for f in failures:
            print("FAIL %s" % f)
        sys.exit(1)
    print("PASS merge-unblocker class: formality by the work class "
          "(visual-delta #278 by the owner review mirror, WCFY #240 by the "
          "verdict card) raised to p100 and goes before the p96-p99 "
          "rebases in dispatcher order; negatives (#999 without accepted "
          "code, #998 with a live test blocker, the rebase of an accepted "
          "#150) untouched; idempotent; 4 mutants caught.")


if __name__ == "__main__":
    main()
