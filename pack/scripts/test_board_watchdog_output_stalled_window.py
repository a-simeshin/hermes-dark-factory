#!/usr/bin/env python3
"""The signal window "the pipeline works but releases nothing" is set by an
exact time, not a truncated date. In a GitHub search, "updated:>2026-11-02"
means "after the whole day" and returns 0 for the entire current UTC day
until the 6 hour window and the start day change. On a day with merges the
watchdog saw zero PRs and shouted "no PR was created or updated" all day.
Live probes with an app token: >date = 0, >=date = 23, >exact time = 22.

The gh stub reproduces this semantics honestly, including the default limit
of 30. A mutant (back to "updated:>" with a truncated date) must turn red on
the scenario "the window is inside one UTC day, a PR was updated an hour
ago" (BOARD_WATCHDOG_SCRIPT=<a copy with the mutation>).
"""
import calendar
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time as real_time

# Project settings are read at import time: set neutral values first.
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="bw-test-home-")
os.environ.update({
    "FACTORY_REPO": "acme/widget",
    "FACTORY_REPO_PATH": "/srv/hermes/widget",
    "FACTORY_BOARD": "main",
    "FACTORY_OWNER_LOGIN": "owner-login",
    "FACTORY_OWNER_CHAT": "telegram:000000000",
    "FACTORY_BASE_BRANCHES": "main",
})

# Mutation runs use a COPY of the script, not the production one.
SCRIPT = os.environ.get("BOARD_WATCHDOG_SCRIPT") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "board-watchdog.py")

spec = importlib.util.spec_from_file_location("board_watchdog", SCRIPT)
if spec is None or spec.loader is None:
    print("SKIP: cannot load board-watchdog.py")
    sys.exit(0)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# A fixed "now": 2026-11-02T12:20:08Z. A 6 hour window means the stamp
# 2026-11-02T06:20:08Z: the window start and "now" are inside one UTC day.
# This is exactly the state in which the old query lost the whole day.
NOW = calendar.timegm((2026, 11, 2, 12, 20, 8))
STAMP = "2026-11-02T06:20:08Z"

SLUG = "stalled-board"
REPO = "acme/widget"

STUB = r'''#!/usr/bin/env python3
"""Stub of gh pr list: an honest GitHub search semantics by time.

updated:>DATE (10 characters): after the WHOLE day, the day is excluded;
updated:>=DATE: the day is included;
updated:>MOMENT / >=MOMENT (a full stamp): an exact time comparison.
Without --limit it returns at most 30, like the real gh.
"""
import json
import os
import sys

args = sys.argv[1:]


def val(flag, default=None):
    if flag in args:
        i = args.index(flag)
        return args[i + 1] if i + 1 < len(args) else default
    return default


search = val("--search", "")
limit = int(val("--limit", "30"))
prs = json.load(open(os.environ["GH_STUB_PRS"]))


def keep(p):
    if not search.startswith("updated:"):
        return True  # sort:updated-desc and the like do not filter by date
    op, bound = (">=", search[10:]) if search[8:10] == ">=" else (">", search[9:])
    upd = p["updatedAt"]
    if len(bound) == 10:  # a date without time: compare at the day level
        upd = upd[:10]
    return upd >= bound if op == ">=" else upd > bound


hits = sorted((p for p in prs if keep(p)),
              key=lambda p: p["updatedAt"], reverse=True)[:limit]
with open(os.environ["GH_STUB_LOG"], "a") as f:
    f.write(json.dumps(args) + "\n")
print(json.dumps(hits))
'''


class _FakeTime:
    """time.time() is frozen, gmtime/strftime are real: the stamp is
    deterministic."""

    def time(self):
        return NOW

    def gmtime(self, t):
        return real_time.gmtime(t)

    def strftime(self, fmt, tup):
        return real_time.strftime(fmt, tup)


def setup_env(prs):
    """A temp dir: the gh stub on PATH, a board with a database, PR fixtures."""
    tmp = tempfile.mkdtemp(prefix="stalled-")
    os.makedirs(os.path.join(tmp, "bin"))
    stub_path = os.path.join(tmp, "bin", "gh")
    with open(stub_path, "w") as f:
        f.write(STUB)
    os.chmod(stub_path, 0o755)
    fixture = os.path.join(tmp, "prs.json")
    with open(fixture, "w") as f:
        json.dump(prs, f)
    log = os.path.join(tmp, "gh.log")
    open(log, "w").close()
    os.environ["PATH"] = os.path.join(tmp, "bin") + os.pathsep + os.environ["PATH"]
    os.environ["GH_STUB_PRS"] = fixture
    os.environ["GH_STUB_LOG"] = log

    board = os.path.join(tmp, SLUG)
    os.makedirs(board)
    import sqlite3
    con = sqlite3.connect(os.path.join(board, "kanban.db"))
    con.executescript(
        "create table task_runs (id integer primary key, started_at real,"
        " outcome text);"
        "create table tasks (id text primary key, title text, status text,"
        " assignee text, goal_mode integer default 0,"
        " block_kind text default '');")
    for k in range(5):  # more runs in the window than the threshold runs >= 4
        con.execute("insert into task_runs (started_at, outcome) values (?, ?)",
                    (NOW - 600 - k, "completed"))
    con.execute("insert into tasks values ('t1','plan A','ready','reviewer',0,'')")
    con.execute("insert into tasks values ('t2','plan B','ready','reviewer',0,'')")
    con.execute("insert into tasks values ('t3','code','ready','coder',0,'')")
    con.commit()
    con.close()

    setattr(mod, "BOARDS", tmp)
    setattr(mod, "time", _FakeTime())
    return log


def stub_query(prs, search, limit=None):
    """A direct question to the stub: how many PRs pass the filter."""
    tmp = tempfile.mkdtemp(prefix="stalled-stub-")
    fixture = os.path.join(tmp, "prs.json")
    with open(fixture, "w") as f:
        json.dump(prs, f)
    log = os.path.join(tmp, "gh.log")
    open(log, "w").close()
    env = dict(os.environ, GH_STUB_PRS=fixture, GH_STUB_LOG=log)
    stub_path = os.path.join(tmp, "gh")
    with open(stub_path, "w") as f:
        f.write(STUB)
    os.chmod(stub_path, 0o755)
    argv = [stub_path, "pr", "list", "--state", "all"]
    if limit is not None:
        argv += ["--limit", str(limit)]
    if search:
        argv += ["--search", search]
    out = subprocess.run(argv, capture_output=True, text=True, env=env,
                         timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


DAY_PRS = [
    {"number": 309, "updatedAt": "2026-11-02T11:20:08Z"},  # an hour ago
    {"number": 310, "updatedAt": "2026-11-02T07:00:00Z"},  # 5h20m ago
    {"number": 292, "updatedAt": "2026-11-02T05:20:08Z"},  # exactly on the stamp boundary
    {"number": 239, "updatedAt": "2026-10-31T23:00:00Z"},  # the day before yesterday
]


def test_stub_search_semantics():
    """The stub is honest: >date drops the whole day, >=date and an exact
    moment include it. The live probes are reproduced deterministically."""
    assert len(stub_query(DAY_PRS, "updated:>2026-11-02")) == 0
    assert len(stub_query(DAY_PRS, "updated:>=2026-11-02")) == 3
    # an exact moment: #292 exactly on the boundary is excluded by strict >
    assert len(stub_query(DAY_PRS, "updated:>2026-11-02T05:20:08Z")) == 2
    assert len(stub_query(DAY_PRS, "updated:>=2026-11-02T05:20:08Z")) == 3


def test_stub_default_limit_30():
    """Without --limit the stub, like the real gh, returns at most 30."""
    many = [{"number": 1000 + i, "updatedAt": "2026-11-02T12:00:%02dZ" % i}
            for i in range(35)]
    assert len(stub_query(many, "updated:>=2026-11-02")) == 30
    assert len(stub_query(many, "updated:>=2026-11-02", limit=200)) == 35


def test_same_day_window_no_alert():
    """The window is inside one UTC day, a PR was updated an hour ago: there
    must be no alert. The old query (>date) lost this day entirely."""
    log = setup_env(DAY_PRS)
    result = mod.output_stalled(SLUG, REPO)
    assert result is None, "false alert:\n%s" % result
    argv = json.loads(open(log).read().strip().splitlines()[-1])
    i = argv.index("--search")
    assert argv[i + 1] == "updated:>=%s" % STAMP, argv[i + 1]
    assert "--limit" in argv and argv[argv.index("--limit") + 1] == "200", argv


def test_stale_window_alerts():
    """There are no updates in the window (the last one the day before
    yesterday): the alert goes out."""
    stale = [
        {"number": 309, "updatedAt": "2026-10-31T23:00:00Z"},
        {"number": 310, "updatedAt": "2026-10-30T10:00:00Z"},
    ]
    setup_env(stale)
    result = mod.output_stalled(SLUG, REPO)
    assert result is not None, "no alert on an empty window"
    assert "releases nothing" in result
    assert "runs completed" in result


def main():
    test_stub_search_semantics()
    test_stub_default_limit_30()
    test_same_day_window_no_alert()
    test_stale_window_alerts()
    print("OK: the signal window is set by an exact time, tests are green")


if __name__ == "__main__":
    main()
