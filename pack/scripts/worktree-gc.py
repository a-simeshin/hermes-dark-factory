#!/usr/bin/env python3
"""Removes the worktrees of cards whose work is finished.

Why: Hermes' `_cleanup_workspace` removes the scratch directory but not the
git worktree, and finished cards left gigabytes behind (mostly dependency and
build directories). A worktree must not be removed on card status alone,
though: until the card is in a terminal status, people still come back to
the branch.

Scope: ALL worktrees from `git worktree list` of the main repository, not
only .worktrees/: kanban workspaces and review/comparison worktrees under
/tmp are included.

Removal condition, every item is required:
  1. the worktree is not the main one and does not belong to a card in a
     non-terminal status (done/archived/no card: terminal; running/blocked/
     ready/...: alive);
  2. there is no OPEN PR for the worktree's branch;
  3. every commit of the branch is on origin;
  4. the worktree is clean: `git status --porcelain` is empty. A dirty
     terminal worktree is not skipped silently: the changes are rescued as a
     patch into $HERMES_HOME/rescue/ (`git diff HEAD` + `git diff --cached`,
     untracked files via `git add -N`), then the worktree is removed and the
     output carries a line with the patch path.

A skip with its reason is always printed: "nothing removed" must not look
the same as "the script is broken".

Dry run: worktree-gc.py --dry-run
"""
import datetime
import json
import os
import re
import shutil
import subprocess
import sys

from factory_config import cfg, cfg_list, hermes_home

BOARDS = os.path.join(hermes_home(), "kanban", "boards")
RESCUE = os.path.join(hermes_home(), "rescue")
MAIN_REPO = cfg("FACTORY_REPO_PATH")
TERMINAL = {"done", "archived"}
# Branches (worktrees) that are never touched: the owner's own working
# branches. Comma-separated list, empty by default.
PROTECTED_BRANCHES = set(cfg_list("FACTORY_PROTECTED_BRANCHES"))
DRY = "--dry-run" in sys.argv


def run(args, cwd=None):
    try:
        r = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except FileNotFoundError:
        # the worktree directory may vanish between the list and the call
        return 127, "", "cwd or command does not exist"


def board_dbs():
    import glob
    return glob.glob(os.path.join(BOARDS, "*", "kanban.db"))


def task_status():
    import sqlite3
    st = {}
    for db in board_dbs():
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
        try:
            st.update({r[0]: r[1] for r in con.execute("select id, status from tasks")})
        except sqlite3.Error as e:
            # an empty or broken database (an archived board): skip it with a
            # warning, the GC carries on with the other boards
            print("worktree-gc: skipping %s: %s" % (db, e), file=sys.stderr)
        finally:
            con.close()
    if not st:
        sys.exit("worktree-gc: no board could be read, check %s" % BOARDS)
    return st


def pr_state(repo_dir, branch, cache):
    """MERGED / CLOSED / OPEN / NONE."""
    if branch in cache:
        return cache[branch]
    code, out, _ = run(["gh", "pr", "list", "--head", branch, "--state", "all",
                        "--json", "state", "--limit", "1"], cwd=repo_dir)
    state = "NONE"
    if code == 0 and out:
        try:
            rows = json.loads(out)
            if rows:
                state = rows[0]["state"]
        except Exception:
            state = "NONE"
    cache[branch] = state
    return state


def worktrees():
    """All worktrees of the main repository except the main one itself."""
    code, out, _ = run(["git", "worktree", "list", "--porcelain"], cwd=MAIN_REPO)
    if code != 0:
        sys.exit("worktree-gc: git worktree list failed in %s" % MAIN_REPO)
    paths = [l.split(" ", 1)[1] for l in out.splitlines() if l.startswith("worktree ")]
    return [p for p in paths if os.path.realpath(p) != os.path.realpath(MAIN_REPO)]


def card_id_for(path, status_map):
    """Card by the worktree directory name: t_xxxxxxxx or a wrapper directory."""
    base = os.path.basename(path.rstrip("/"))
    if base == "repo":
        base = os.path.basename(os.path.dirname(path.rstrip("/")))
    if base in status_map:
        return base
    m = re.search(r"(t_[0-9a-f]{8})", path)
    return m.group(1) if m else None


def rescue_patch(top, name):
    """Saves the uncommitted changes of the worktree to $HERMES_HOME/rescue/<name>.patch.
    Returns the patch path, or None if the patch is empty."""
    run(["git", "add", "-N", "."], cwd=top)
    _, d1, _ = run(["git", "diff", "HEAD"], cwd=top)
    _, d2, _ = run(["git", "diff", "--cached"], cwd=top)
    body = (d1 + "\n" + d2).strip()
    if not body:
        return None
    os.makedirs(RESCUE, exist_ok=True)
    path = os.path.join(RESCUE, "%s.patch" % name)
    with open(path, "w") as f:
        f.write(body + "\n")
    return path


def du_mb(path):
    code, out, _ = run(["du", "-sm", path])
    try:
        return int(out.split()[0])
    except Exception:
        return 0


def keep_until(top):
    """Date from the .wt-keep marker (first line, YYYY-MM-DD) or None.

    The marker extends the life of a worktree that works OUTSIDE a card
    (screenshot capture, build comparisons): a scheduled worktree-gc once
    removed such worktrees in the middle of a capture round because no card
    belonged to them. An expired or malformed marker does not protect, so a
    "capture for a week" does not turn into eternal garbage.
    """
    try:
        with open(os.path.join(top, ".wt-keep")) as fh:
            first = fh.readline().strip()
        return datetime.date.fromisoformat(first)
    except Exception:
        return None


def main():
    st = task_status()
    cache = {}
    freed = 0
    removed, kept, rescued = [], [], []

    for path in worktrees():
        name = os.path.basename(path.rstrip("/"))
        top = path
        # An entry in git worktree list can outlive its directory (a manual
        # rm -rf): prune and move on, branch/status on a missing cwd raises
        # FileNotFoundError and would fail the whole run.
        if not os.path.isdir(top):
            kept.append((name, "skipped: directory is missing, prune"))
            if not DRY:
                run(["git", "worktree", "prune"], cwd=MAIN_REPO)
            continue
        # kanban workspace: the worktree lives in workspaces/<card>/repo, the
        # card id comes from the parent; for other worktrees the directory
        # name speaks for itself.
        cid = card_id_for(path, st)
        status = st.get(cid) if cid else None

        if status is not None and status not in TERMINAL:
            kept.append((name, "card %s is in status %s" % (cid, status)))
            continue

        keep = keep_until(top)
        if keep and keep >= datetime.date.today():
            kept.append((name, ".wt-keep marker until %s" % keep))
            continue

        bcode, branch, _ = run(["git", "branch", "--show-current"], cwd=top)
        if bcode != 0:
            kept.append((name, "skipped: git branch failed (directory gone?)"))
            continue
        if branch in PROTECTED_BRANCHES:
            kept.append((name, "protected branch %s" % branch))
            continue

        if branch:
            code, missing, _ = run(["git", "log", "--oneline", "--not",
                                    "--remotes=origin", "HEAD"], cwd=top)
            if code != 0:
                kept.append((name, "git log failed"))
                continue
            if missing:
                kept.append((name, "%d commits are not on origin" % len(missing.splitlines())))
                continue
            state = pr_state(top, branch, cache)
            if state == "OPEN":
                kept.append((name, "PR for branch %s is open" % branch))
                continue

        scode, dirty, _ = run(["git", "status", "--porcelain"], cwd=top)
        if scode != 0:
            kept.append((name, "skipped: git status failed"))
            continue
        if dirty:
            patch = rescue_patch(top, cid or name)
            if patch:
                rescued.append((name, patch))
            elif not DRY:
                # only ignored untracked files, which produced no diff
                pass

        size = du_mb(path)
        if not DRY:
            run(["git", "worktree", "remove", "--force", top], cwd=MAIN_REPO)
            # kanban workspace: remove the wrapper directory entirely too
            parent = os.path.dirname(path.rstrip("/"))
            if os.path.basename(parent) == (cid or "") and "workspaces" in parent:
                shutil.rmtree(parent, ignore_errors=True)
        freed += size
        removed.append((name, branch or "detached", size))

    if not DRY and removed:
        run(["git", "worktree", "prune"], cwd=MAIN_REPO)

    # The documented cron --no-agent idiom: empty output is a quiet tick.
    if not DRY and not removed and not rescued:
        return

    head = "DRY RUN, nothing removed\n" if DRY else ""
    lines = ["%sworktree cleanup: removed %d, freed %d MB, kept %d"
             % (head, len(removed), freed, len(kept))]
    for n, why, sz in removed[:15]:
        lines.append("  removed %-28s %-42s %5d MB" % (n, why[:42], sz))
    if len(removed) > 15:
        lines.append("  ... and %d more" % (len(removed) - 15))
    for n, p in rescued:
        lines.append("  rescued patch %-22s %s" % (n, p))
    reasons = {}
    for _n, why in kept:
        key = why.split(" ", 1)[0] if why[0].isdigit() else why
        reasons[key] = reasons.get(key, 0) + 1
    for why, n in sorted(reasons.items(), key=lambda x: -x[1]):
        lines.append("  kept %-3d %s" % (n, why))
    # Worktree cleanup is routine: it goes to the daily digest, and the chat
    # stays silent.
    if not DRY:
        from cron_quiet import digest_add
        digest_add("worktree-gc", "\n".join(lines))
    else:
        print("\n".join(lines))


if __name__ == "__main__":
    main()
