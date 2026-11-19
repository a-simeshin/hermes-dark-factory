#!/usr/bin/env python3
"""Tests for kanban-dispatch-peak-pause.py (a normal run plus 5 mutants).

Each run of this file tests the module from env PEAK_MODULE (by default the
sibling kanban-dispatch-peak-pause.py) in a sandbox HERMES_HOME that the test
creates itself in a temp directory. Exit 0 = all scenarios green. A mutant is
killed if the run is red.

The test never touches the real Hermes home: it refuses to run if HERMES_HOME
points at it (a bare run against a real home once spun up holders in a live
installation).

Scenarios:
  A  in-peak, no holder -> peak holder started, meta owner=peak-pause
  B  in-peak, foreign owner stop -> pid and meta untouched
  C  out-of-peak, peak holder -> lifted, meta removed
  D  out-of-peak, foreign owner stop -> alive, meta untouched
  E1 in-peak, nameless (quota) holder -> meta attached with annotate, same pid
  E2 end of window lifts the attached peak holder (annotate + release)
  F  Saturday 10:00 -> no holder is started
  G  window edges: 08:59 no, 09:00 yes, 12:59 yes, 13:00 no
  U  window keys unset -> the pause is off: never in peak, tick holds nothing

The scenarios set the window explicitly (Mon-Fri 09:00-13:00, UTC+3); the
pack has no default window.

Run: python3 test_peak_pause.py
"""
import fcntl
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(HERE))

REAL_HOME = Path(os.path.expanduser("~/.hermes"))
SANDBOX = Path(tempfile.mkdtemp(prefix="peak-pause-"))
# The module under test reads HERMES_HOME at import time: set the sandbox first.
os.environ["HERMES_HOME"] = str(SANDBOX)
# hermes-health.py requires an owner chat at import (used by the guard checks).
os.environ["FACTORY_OWNER_CHAT"] = "telegram:000000000"
PEAK_KEYS = {"FACTORY_PEAK_TZ": "Etc/GMT-3", "FACTORY_PEAK_START": "09:00",
             "FACTORY_PEAK_END": "13:00", "FACTORY_PEAK_DAYS": "0,1,2,3,4"}
os.environ.update(PEAK_KEYS)

MODULE_PATH = Path(os.environ.get("PEAK_MODULE", HERE / "kanban-dispatch-peak-pause.py"))
HOLD_PATH = HERE / "kanban-dispatch-hold.py"
FAILS: list[str] = []


def load_module(name="peak_pause_under_test"):
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = load_module()

# Safety net against running in the real home.
if SANDBOX.resolve() == REAL_HOME.resolve():
    print("REFUSED: HERMES_HOME points at the real home. Run only in a sandbox.")
    sys.exit(2)


def prepare_sandbox() -> None:
    """Prepare the sandbox: directories and an empty board db."""
    for sub in ("cache", "logs", "scripts"):
        (SANDBOX / sub).mkdir(parents=True, exist_ok=True)
    (SANDBOX / "kanban.db").touch()


prepare_sandbox()

# Every child of the test must inherit the sandbox HERMES_HOME explicitly:
# relying on process environment inheritance is fragile.
SANDBOX_ENV = dict(os.environ, HERMES_HOME=str(SANDBOX))


def detach(cmd: list[str]) -> None:
    """Start cmd as a background process that is re-parented to init once the
    shell exits. Without this, a dead holder stays a zombie child of the test
    and pid_alive() lies (release then thinks "ignored SIGTERM")."""
    subprocess.Popen(["sh", "-c", '"$@" &', "sh", *cmd],
                     env=SANDBOX_ENV, start_new_session=True,
                     stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


_orig_hold = MOD._hold


def _detached_hold(*args: str, wait: bool = True) -> int:
    cmd = [sys.executable, str(MOD.HOLD), *args]
    if wait:
        return _orig_hold(*args, wait=True)
    detach(cmd)
    return 0


MOD._hold = _detached_hold


def dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def check(name: str, cond: bool, detail: str = "") -> None:
    tag = "PASS" if cond else "FAIL"
    print(f"  [{tag}] {name} {detail}")
    if not cond:
        FAILS.append(f"{name} {detail}")


def start_holder(owner: str | None = None, reason: str = "test") -> None:
    args = [sys.executable, str(HOLD_PATH), "start"]
    if owner:
        args += ["--owner", owner, "--reason", reason]
    detach(args)


def kill_holder():
    pid = MOD.holder_pid()
    if pid and MOD.pid_alive(pid):
        try:
            os.kill(pid, 9)
        except OSError:
            pass
    for _ in range(100):
        if not MOD.pid_alive(MOD.holder_pid()):
            break
        time.sleep(0.1)
    for f in (MOD.PIDFILE, MOD.METAFILE):
        try:
            f.unlink()
        except OSError:
            pass


def wait_holder(timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        pid = MOD.holder_pid()
        if pid and MOD.pid_alive(pid):
            return pid
        time.sleep(0.2)
    return None


def lock_held() -> bool:
    """True if the dispatch lock is held by someone else (the holder holds it)."""
    p = SANDBOX / "kanban.db.dispatch.lock"
    fh = open(p, "a+b")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        fh.close()


def meta_dict():
    m = MOD.read_meta()
    return m


def health_hold_owner_stop():
    """hermes-health.hold_owner_stop() with the sandbox paths (the guard that
    keeps the quota recovery from lifting a stop in the middle of the peak)."""
    spec = importlib.util.spec_from_file_location("hh", HERE / "hermes-health.py")
    hh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hh)
    hh.HOLD_META = str(MOD.METAFILE)
    hh.HOLD_PID = str(MOD.PIDFILE)
    return hh.hold_owner_stop()


def main() -> int:
    (SANDBOX / "kanban.db").touch()
    print(f"module={MODULE_PATH}")
    print(f"sandbox={SANDBOX}")

    print("G: window edges")
    check("08:59 not peak", not MOD.in_peak(dt("2026-11-10T08:59+03:00")))
    check("09:00 peak", MOD.in_peak(dt("2026-11-10T09:00+03:00")))
    check("12:59 peak", MOD.in_peak(dt("2026-11-10T12:59+03:00")))
    check("13:00 not peak", not MOD.in_peak(dt("2026-11-10T13:00+03:00")))

    print("U: window keys unset -> pause off")
    saved = {k: os.environ.pop(k) for k in PEAK_KEYS}
    try:
        off = load_module("peak_pause_unset")
    finally:
        os.environ.update(saved)
    check("off: not enabled", off.ENABLED is False, str(off.MISSING))
    check("off: Tuesday 10:00 is not peak",
          not off.in_peak(dt("2026-11-10T10:00+03:00")))
    off.now_local = lambda: dt("2026-11-10T10:00+03:00")
    off._hold = _detached_hold
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        off.cmd_tick()
    time.sleep(1.0)
    check("off: tick names the missing keys",
          "peak pause off: FACTORY_PEAK_TZ, FACTORY_PEAK_START, FACTORY_PEAK_END not set"
          in buf.getvalue(), buf.getvalue().strip()[:120])
    check("off: holder not started",
          MOD.holder_pid() is None or not MOD.pid_alive(MOD.holder_pid()))
    kill_holder()

    print("F: Saturday 10:00, no holder -> tick starts nothing")
    MOD.now_local = lambda: dt("2026-11-14T10:00+03:00")  # Saturday
    MOD.cmd_tick()
    time.sleep(1.0)
    check("holder not started", MOD.holder_pid() is None or not MOD.pid_alive(MOD.holder_pid()))
    kill_holder()

    print("A: in-peak (Tue 10:00), no holder -> peak holder")
    MOD.now_local = lambda: dt("2026-11-10T10:00+03:00")  # Tuesday
    MOD.cmd_tick()
    pid_a = wait_holder()
    check("holder started", pid_a is not None)
    m = meta_dict()
    check("meta owner=peak-pause", bool(m and m.get("owner") == "peak-pause"), str(m and m.get("owner")))
    check("lock held", lock_held())
    check("health sees owner-stop", health_hold_owner_stop() is not None)

    print("C: out-of-peak (Tue 14:00), peak holder -> lifted")
    MOD.now_local = lambda: dt("2026-11-10T14:00+03:00")
    MOD.cmd_tick()
    for _ in range(120):
        if not MOD.pid_alive(pid_a):
            break
        time.sleep(0.5)
    check("holder dead", not MOD.pid_alive(pid_a))
    check("meta removed", meta_dict() is None)
    check("lock free", not lock_held())

    print("B: in-peak, foreign owner stop -> untouched")
    start_holder("owner-login", "owner stop (test)")
    pid_b = wait_holder()
    check("foreign holder up", pid_b is not None)
    meta_before = MOD.METAFILE.read_text()
    MOD.now_local = lambda: dt("2026-11-10T10:00+03:00")
    MOD.cmd_tick()
    time.sleep(1.0)
    check("same pid", MOD.holder_pid() == pid_b, f"{MOD.holder_pid()} vs {pid_b}")
    check("meta unchanged", MOD.METAFILE.read_text() == meta_before)
    check("health sees owner=owner-login", health_hold_owner_stop() is not None)

    print("D: out-of-peak, foreign owner stop -> alive")
    MOD.now_local = lambda: dt("2026-11-10T14:00+03:00")
    MOD.cmd_tick()
    time.sleep(1.5)
    check("foreign holder alive", MOD.pid_alive(MOD.holder_pid()))
    check("owner meta in place", (meta_dict() or {}).get("owner") == "owner-login")
    kill_holder()

    print("E1: in-peak, nameless (quota) holder -> annotate without lifting locks")
    start_holder()  # like the quota stop: no --owner
    pid_e = wait_holder()
    check("nameless holder up", pid_e is not None)
    check("before: health does NOT see owner-stop", health_hold_owner_stop() is None)
    MOD.now_local = lambda: dt("2026-11-10T10:00+03:00")
    MOD.cmd_tick()
    time.sleep(1.0)
    check("same pid (locks never released)", MOD.holder_pid() == pid_e, f"{MOD.holder_pid()} vs {pid_e}")
    m = meta_dict()
    check("meta attached by peak-pause", bool(m and m.get("owner") == "peak-pause"), str(m and m.get("owner")))
    check("health sees owner-stop (quota recovery will not lift it)", health_hold_owner_stop() is not None)
    kill_holder()

    print("E2: end of window lifts the attached peak holder")
    MOD.now_local = lambda: dt("2026-11-10T14:00+03:00")
    start_holder()  # nameless again, as before the annotation
    pid_e2 = wait_holder()
    MOD.cmd_tick()  # out-of-peak with a nameless holder: skipped
    # attach the peak mark by hand (simulates the annotation done in the morning)
    subprocess.run([sys.executable, str(HOLD_PATH),
                    "annotate", "--owner", "peak-pause", "--reason", "test"],
                   env=SANDBOX_ENV, capture_output=True, timeout=60)
    MOD.cmd_tick()  # now ours -> lift
    for _ in range(120):
        if not MOD.pid_alive(pid_e2):
            break
        time.sleep(0.5)
    check("attached peak holder lifted at window end", not MOD.pid_alive(pid_e2))
    kill_holder()

    print()
    if FAILS:
        print(f"RED: {len(FAILS)} failure(s):")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("GREEN: all scenarios passed")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        # finish off whatever is still alive in the sandbox
        pid = MOD.holder_pid() if MOD else None
        if pid:
            subprocess.run([sys.executable, str(HOLD_PATH), "release", "--force"],
                           env=SANDBOX_ENV, capture_output=True, timeout=90)
    sys.exit(rc)
