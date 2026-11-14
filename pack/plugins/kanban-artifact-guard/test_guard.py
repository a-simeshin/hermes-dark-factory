#!/usr/bin/env python3
"""Check: uncommitted work after completed produces a comment and a card.

Needs the Hermes sources on sys.path (for hermes_cli.kanban_db). Run with the
Hermes virtualenv and point HERMES_AGENT_SRC at the hermes-agent checkout:

    HERMES_AGENT_SRC=~/.hermes/hermes-agent <hermes venv>/bin/python test_guard.py

The test uses a temporary board database and rescue directory. Never point
HERMES_HOME at your live ~/.hermes while running it.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))

root = Path(tempfile.mkdtemp())
repo = root / "repo"
repo.mkdir()
board_db = root / "kanban.db"
os.environ["HERMES_KANBAN_DB"] = str(board_db)
# Rescued patches go to a temporary directory so the live rescue dir stays clean.
os.environ["HERMES_RESCUE_DIR"] = str(root / "rescue")

sys.path.insert(0, os.path.expanduser(
    os.environ.get("HERMES_AGENT_SRC", "~/.hermes/hermes-agent")))
sys.path.insert(0, HERE)


def git(*a):
    subprocess.run(["git", "-C", str(repo), *a], check=True,
                   capture_output=True, text=True)


git("init", "-q")
git("config", "user.email", "t@t")
git("config", "user.name", "t")
(repo / "a.txt").write_text("one\n")
git("add", "-A")
git("commit", "-qm", "first")

from hermes_cli import kanban_db as kb  # noqa: E402
import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "guard", os.path.join(HERE, "__init__.py"))
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class Ctx:
    def get_config(self, key, default=None):
        return [str(repo)] if key == "repos" else default


handler = guard._make_handler(Ctx())

kb.init_db(board_db)
con = kb.connect(board_db)
tid = kb.create_task(con, title="card", assignee="coder")


def cards():
    return list(con.execute("select id, title from tasks"))


def comments(task_id):
    return [r[0] for r in con.execute(
        "select body from task_comments where task_id=?", (task_id,))]


# 1. Clean tree: the hook stays silent.
handler(task_id=tid, board=None, assignee="coder")
assert len(cards()) == 1, cards()
assert not comments(tid), comments(tid)

# 2. Dirty tree: a comment, a catch-up card and a rescued patch.
# The card must look like it ran: the guard does not scan shared
# repositories for a card that was never claimed.
con.execute("update tasks set started_at = 1788142027 where id = ?", (tid,))
con.commit()
(repo / "a.txt").write_text("one\ntwo\n")
handler(task_id=tid, board=None, assignee="coder")
after = cards()
assert len(after) == 2, after
assert any("Commit the work" in t for _i, t in after), after
body = "\n".join(comments(tid))
assert "a.txt" in body, body
patches = list(Path(os.environ["HERMES_RESCUE_DIR"]).glob(f"{tid}-*.patch"))
assert patches, "patch not saved"

# 3. A repeat with the same set of edits does not multiply cards.
handler(task_id=tid, board=None, assignee="coder")
assert len(cards()) == 2, cards()

for p in patches:
    p.unlink()
print("3/3 OK")


def test_message_matches_event():
    """Regression: the text was hard-coded for completed on every event."""
    claimed = guard._body_head("t_x", "kanban_task_claimed", ["repo:", "  M a.py"])
    assert "closed as completed" not in claimed, claimed
    assert "claimed for work" in claimed, claimed
    done = guard._body_head("t_x", "kanban_task_completed", [])
    assert "closed as completed" in done, done
    blocked = guard._body_head("t_x", "kanban_task_blocked", [])
    assert "went to blocked" in blocked, blocked
    print("OK: message text matches the event")


def test_shared_repos_not_scanned_on_claim():
    """Regression: at claim time shared repositories were scanned while
    another worker was editing them, and its live work was declared lost."""
    assert "kanban_task_claimed" not in guard.EVENTS_SCANNING_SHARED_REPOS
    assert "kanban_task_completed" in guard.EVENTS_SCANNING_SHARED_REPOS
    assert "kanban_task_blocked" in guard.EVENTS_SCANNING_SHARED_REPOS
    print("OK: shared repositories are not scanned on claim")


def test_hermes_home_is_not_a_work_tree():
    """Regression: ~/.hermes became a git repository for knowledge snapshots,
    and the plugin declared its untracked files lost work of a card."""
    home = os.path.realpath(
        os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes"))
    assert guard._is_hermes_home(home), "the Hermes home must be excluded"
    assert guard._is_hermes_home(home + "/"), "a trailing slash must not matter"
    assert not guard._is_hermes_home("/srv/projects/some-repo")
    assert not guard._is_hermes_home(home + "/kanban/boards/x/workspaces/t_1")
    print("OK: the Hermes home is not a work tree")


def test_owner_of_workspace_tree():
    """A card's tree is recognised by its directory name, nested repo too."""
    assert guard._owner_of("/x/workspaces/t_2161519c") == "t_2161519c"
    assert guard._owner_of("/x/workspaces/t_2161519c/repo") == "t_2161519c"
    assert guard._owner_of("/srv/projects/some-repo") is None


def test_foreign_owner_is_named_in_the_message():
    """The scan was triggered by one card, the tree belongs to another.

    The message must name the real owner."""
    body = guard._body_head(
        "t_a158d7b5", "kanban_task_blocked",
        ["/x/workspaces/t_2161519c  (belongs to card t_2161519c):", "  M f.py"],
        owners=["t_2161519c"],
    )
    assert "WARNING" in body, body
    assert "t_2161519c" in body, body
    assert "do not push blindly" in body, body

    # the card's own tree is not foreign: no warning expected
    own = guard._body_head(
        "t_2161519c", "kanban_task_blocked",
        ["/x/workspaces/t_2161519c:", "  M f.py"],
        owners=["t_2161519c"],
    )
    assert "WARNING" not in own, own


def _mem_db():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.execute("create table tasks (id text, status text)")
    conn.execute("create table task_runs (task_id text, worker_pid integer)")
    return conn


def test_shared_repos_skipped_while_someone_works():
    """While the board has a running card, foreign trees must not be touched."""
    conn = _mem_db()
    conn.execute("insert into tasks values ('t_55f5db18', 'running')")
    assert guard._someone_is_working(conn) is True
    conn.execute("delete from tasks")
    conn.execute("insert into tasks values ('t_x', 'done')")
    assert guard._someone_is_working(conn) is False


def test_card_without_a_real_run_left_nothing():
    """A card blocked straight from the queue could not have left anything.

    The signal is tasks.started_at. Counting by task_runs.worker_pid is wrong:
    that column can be empty for ALL rows, including real finished runs, and
    such a check would silently disable the guard entirely."""
    class NeverStarted:
        started_at = None

    class Started:
        started_at = 1788142027

    # the field EXISTS and is None: that means "never ran", not "unknown shape".
    # The first version confused these cases via getattr(..., None).
    assert guard._has_real_run(NeverStarted()) is False
    assert guard._has_real_run(Started()) is True
    assert guard._has_real_run({"started_at": None}) is False
    assert guard._has_real_run({"started_at": 1788142027}) is True
    # an unknown record shape must not silently close the scan
    assert guard._has_real_run(None) is True


def test_broken_check_fails_closed():
    """A broken check must not OPEN the scan: a missed finding is cheaper
    than a false push of someone else's work."""
    class Broken:
        def execute(self, *_a, **_k):
            raise RuntimeError("no such table")
    assert guard._someone_is_working(Broken()) is True

    class NoField:
        def __getitem__(self, _k):
            raise TypeError("not subscriptable")
    assert guard._has_real_run(NoField()) is True
    assert guard._has_real_run(None) is True


if __name__ == "__main__":
    test_message_matches_event()
    test_shared_repos_not_scanned_on_claim()
    test_hermes_home_is_not_a_work_tree()
    test_owner_of_workspace_tree()
    test_foreign_owner_is_named_in_the_message()
    test_shared_repos_skipped_while_someone_works()
    test_card_without_a_real_run_left_nothing()
    test_broken_check_fails_closed()
    print("all checks passed")
