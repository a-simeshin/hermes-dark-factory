#!/usr/bin/env python3
"""Test of the max_in_progress gate in board-watchdog.py.

Ceiling 2, rollback to 1 on measurable conditions:
  cond 1: swap above 3584 MB; cond 2: load5 above 12 with >= 2 running;
  cond 3: a double overlapping crashed run in a 30 min window plus a trace of
  an unclean gateway stop.

The blocker tests must fail if the gate is rolled back to a rigid "no more
than 1" (the owner set 2 and the watchdog put it back to 1).
"""
import atexit
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# Isolated Hermes home and neutral project settings, set before the script is loaded.
_HOME_TMP = tempfile.mkdtemp(prefix="wd-home-")
atexit.register(shutil.rmtree, _HOME_TMP, ignore_errors=True)
os.environ.update({
    "HERMES_HOME": _HOME_TMP,
    "FACTORY_REPO": "acme/widget",
    "FACTORY_REPO_PATH": "/srv/hermes/widget",
    "FACTORY_BOARD": "main",
    "FACTORY_OWNER_LOGIN": "owner-login",
    "FACTORY_OWNER_CHAT": "telegram:000000000",
    "FACTORY_BASE_BRANCHES": "main",
})
# BOARD_WATCHDOG_SCRIPT lets a mutation run test a modified copy of the script.
SCRIPT = os.environ.get("BOARD_WATCHDOG_SCRIPT") or os.path.join(
    HERE, "board-watchdog.py")

spec = importlib.util.spec_from_file_location("board_watchdog", SCRIPT)
if spec is None or spec.loader is None:
    print("SKIP: cannot load board-watchdog.py")
    sys.exit(0)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)



class GateEnv:
    """Replaces the outside world for max_in_progress_gate without touching
    the config.

    read_max_in_progress and run are linked as in reality: `config set`
    changes the value that is read back later.
    """

    def __init__(self, value=2, swap=(2000, 4095, 3500), l5=0.7,
                 running=1, crash=None, unclean=None):
        self.box = [value]
        self.calls = []
        self.saved = {}
        self.swap = swap
        self.l5 = l5
        self.running = running
        self.crash = crash
        self.unclean = unclean

    def __enter__(self):
        mocks = {
            "read_max_in_progress": lambda: self.box[0],
            "system_memory_sample": lambda: self.swap,
            "load5": lambda: self.l5,
            "running_workers_all_boards": lambda: self.running,
            "parallel_crash_pair_recent": lambda w: self.crash,
            "gateway_unclean_exit_recent": lambda w: self.unclean,
        }
        for name, mock in mocks.items():
            self.saved[name] = getattr(mod, name)
            setattr(mod, name, mock)
        self.saved["run"] = mod.run
        mod.run = self._fake_run
        return self

    def __exit__(self, *exc):
        for name, fn in self.saved.items():
            setattr(mod, name, fn)
        return False

    def _fake_run(self, cmd, timeout=120):
        self.calls.append(cmd)
        cmd = list(cmd)
        if ("config" in cmd and "set" in cmd
                and "kanban.max_in_progress" in cmd):
            i = cmd.index("kanban.max_in_progress")
            if i + 1 < len(cmd):
                try:
                    self.box[0] = int(cmd[i + 1])
                except ValueError:
                    pass
        return ""

    def set_config_calls(self):
        return [c for c in self.calls
                if "config" in c and "set" in c
                and "kanban.max_in_progress" in c]


def test_ceiling_is_two():
    assert mod.MAX_IN_PROGRESS_ALLOWED == 2, (
        "the ceiling must be 2 (owner decision), now %s"
        % mod.MAX_IN_PROGRESS_ALLOWED)
    print("OK test_ceiling_is_two")


def test_two_is_norm_no_action():
    """value=2, healthy numbers: the gate is silent and writes nothing to the config."""
    with GateEnv(value=2) as env:
        out = mod.max_in_progress_gate({}, time.time())
        assert out is None, "the norm must not alert, got %s" % (out,)
        assert env.set_config_calls() == [], (
            "the config must not be touched in the norm: %s" % env.set_config_calls())
    print("OK test_two_is_norm_no_action")


def test_rollback_on_swap():
    """cond 1: swap 3900 of 4095 at value=2 -> lowered to 1."""
    with GateEnv(value=2, swap=(3900, 4095, 300)) as env:
        out = mod.max_in_progress_gate({}, time.time())
        assert out is not None, "the swap rollback must fire"
        key, text = out
        assert key == "maxinprogress-rollback"
        assert "ROLLBACK ON CONDITION" in text, text
        assert "cond 1" in text and "3900" in text, text
        calls = env.set_config_calls()
        assert any(c[-1] == "1" for c in calls), \
            "expected a call set kanban.max_in_progress 1: %s" % calls
    print("OK test_rollback_on_swap")


def test_rollback_on_load5_with_two_running():
    """cond 2: load5=14 with 2 running -> lowered to 1."""
    with GateEnv(value=2, l5=14.0, running=2) as env:
        out = mod.max_in_progress_gate({}, time.time())
        assert out and "cond 2" in out[1], out
        assert any(c[-1] == "1" for c in env.set_config_calls()), env.calls
    print("OK test_rollback_on_load5_with_two_running")


def test_no_rollback_high_load_single_worker():
    """cond 2 without a pair of running: load5=14 with 1 worker is not a rollback."""
    with GateEnv(value=2, l5=14.0, running=1) as env:
        out = mod.max_in_progress_gate({}, time.time())
        assert out is None, "one worker: no rollback needed, got %s" % (out,)
        assert env.set_config_calls() == []
    print("OK test_no_rollback_high_load_single_worker")


def test_rollback_on_crash_pair_with_unclean_gateway():
    """cond 3: an overlapping double crash + an unclean gateway death -> lowered."""
    crash = ("board test: t_a and t_b crashed, overlap 600 s, "
             "crashes 30 s apart")
    unclean = "gateway.previous_unclean_exit @ 2026-10-27T01:00:00+00:00"
    with GateEnv(value=2, crash=crash, unclean=unclean) as env:
        out = mod.max_in_progress_gate({}, time.time())
        assert out and "cond 3" in out[1] and "gateway.previous_unclean_exit" in out[1], out
        assert any(c[-1] == "1" for c in env.set_config_calls()), env.calls
    print("OK test_rollback_on_crash_pair_with_unclean_gateway")


def test_no_rollback_crash_pair_clean_gateway():
    """cond 3 without a trace of an unclean gateway stop: a double crash is not a trigger."""
    crash = "board test: t_a and t_b crashed, overlap 600 s"
    with GateEnv(value=2, crash=crash, unclean=None) as env:
        out = mod.max_in_progress_gate({}, time.time())
        assert out is None, "clean gateway: no rollback needed, got %s" % (out,)
        assert env.set_config_calls() == []
    print("OK test_no_rollback_crash_pair_clean_gateway")


def test_no_second_rollback_when_already_one():
    """The condition holds but the limit is already 1: do nothing and stay silent."""
    with GateEnv(value=1, swap=(3900, 4095, 300)) as env:
        out = mod.max_in_progress_gate({}, time.time())
        assert out is None, "at value=1 no rollback is needed, got %s" % (out,)
        assert env.set_config_calls() == [], env.calls
    print("OK test_no_second_rollback_when_already_one")


def test_value_three_capped_to_two():
    """value=3: hard ceiling, lowered to 2 (not to 1)."""
    with GateEnv(value=3) as env:
        out = mod.max_in_progress_gate({}, time.time())
        assert out and out[0] == "maxinprogress", out
        assert "lowered automatically" in out[1], out[1]
        assert any(c[-1] == "2" for c in env.set_config_calls()), env.calls
    print("OK test_value_three_capped_to_two")


def test_dedup_quiets_report_but_fix_still_ran():
    """A repeat inside the dedup window: the config is fixed, the report stays silent."""
    now = time.time()
    with GateEnv(value=3) as env:
        state = {"maxinprogress": now - 60}
        out = mod.max_in_progress_gate(state, now)
        assert out is None, "inside the dedup window the report is silent: %s" % (out,)
        assert any(c[-1] == "2" for c in env.set_config_calls()), (
            "the fix must run inside the dedup window too: %s" % env.calls)
    print("OK test_dedup_quiets_report_but_fix_still_ran")


def make_board(runs):
    """Mini board with task_runs. Returns (slug, tmpdir)."""
    tmp = tempfile.mkdtemp(prefix="wdgate-")
    slug = "testboard"
    os.makedirs(os.path.join(tmp, slug), exist_ok=True)
    con = sqlite3.connect(os.path.join(tmp, slug, "kanban.db"))
    con.executescript("""
        create table tasks (id text primary key, status text);
        create table task_runs (id integer primary key, task_id text,
            started_at int, ended_at int, outcome text);
    """)
    con.executemany(
        "insert into task_runs (task_id, started_at, ended_at, outcome)"
        " values (?,?,?,?)", runs)
    con.commit()
    con.close()
    return slug, tmp


def test_crash_pair_detector_overlap():
    """Detector of cond 3: overlapping crashed runs are found, separate ones are not."""
    now = time.time()
    overlap = [
        ("t_a", now - 4000, now - 600, "crashed"),
        ("t_b", now - 3800, now - 580, "crashed"),
    ]
    separate = [
        ("t_c", now - 4000, now - 3000, "crashed"),
        ("t_d", now - 2000, now - 580, "crashed"),
    ]
    for runs, expect in ((overlap, True), (separate, False)):
        slug, tmp = make_board(runs)
        old = mod.BOARDS
        mod.BOARDS = tmp
        try:
            got = mod.parallel_crash_pair_recent(30 * 60)
            assert bool(got) == expect, "runs=%s expect=%s got=%s" % (
                runs, expect, got)
            if expect:
                assert "t_a" in got and "t_b" in got, got
        finally:
            mod.BOARDS = old
            shutil.rmtree(tmp)
    print("OK test_crash_pair_detector_overlap")


def test_unclean_exit_from_log():
    """gateway_unclean_exit_recent: a fresh unclean marker is found, an old one is not."""
    tmp = tempfile.mkdtemp(prefix="wdgate-log-")
    old_home = mod.HOME
    mod.HOME = tmp
    os.makedirs(os.path.join(tmp, "logs"))
    os.makedirs(os.path.join(tmp, "state"))
    try:
        from datetime import datetime, timezone
        fresh = datetime.now(timezone.utc).isoformat()
        stale = datetime.fromtimestamp(
            time.time() - 3 * 3600, tz=timezone.utc).isoformat()

        log = os.path.join(tmp, "logs", "gateway-exit-diag.log")
        with open(log, "w") as fh:
            fh.write(json.dumps({"ts": stale, "tag": "gateway.exit_nonzero"})
                     + "\n")
        assert mod.gateway_unclean_exit_recent(30 * 60) is None, "an old record does not count"

        with open(log, "w") as fh:
            fh.write(json.dumps({"ts": fresh, "tag": "gateway.exit_nonzero"})
                     + "\n")
        got = mod.gateway_unclean_exit_recent(30 * 60)
        assert got and "gateway.exit_nonzero" in got, got

        with open(log, "w") as fh:
            fh.write(json.dumps({"ts": fresh, "tag": "gateway.previous_unclean_exit",
                                 "suspected_oom": True}) + "\n")
        got = mod.gateway_unclean_exit_recent(30 * 60)
        assert got and "previous_unclean_exit" in got, got
    finally:
        mod.HOME = old_home
        shutil.rmtree(tmp)
    print("OK test_unclean_exit_from_log")


if __name__ == "__main__":
    test_ceiling_is_two()
    test_two_is_norm_no_action()
    test_rollback_on_swap()
    test_rollback_on_load5_with_two_running()
    test_no_rollback_high_load_single_worker()
    test_rollback_on_crash_pair_with_unclean_gateway()
    test_no_rollback_crash_pair_clean_gateway()
    test_no_second_rollback_when_already_one()
    test_value_three_capped_to_two()
    test_dedup_quiets_report_but_fix_still_ran()
    test_crash_pair_detector_overlap()
    test_unclean_exit_from_log()
    print("ALL MAX_IN_PROGRESS GATE TESTS PASSED")
