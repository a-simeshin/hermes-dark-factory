#!/usr/bin/env python3
"""Stop the kanban dispatcher from handing out cards on a live gateway
(without `hermes pause`).

Mechanism: hold flock(LOCK_EX|LOCK_NB) on <db>.dispatch.lock of every board.
The gateway tick (gateway/kanban_watchers.py -> dispatch_once ->
_dispatch_tick_lock) cannot get the lock, finishes as skipped_locked and
spawns no workers. Workers already running finish their work, the owner's
chat stays alive: ESTOP is not touched.

Why not config.yaml: the dispatcher settings (max_in_progress, max_spawn,
dispatch_in_gateway) are read ONCE at gateway start
(gateway/kanban_watchers.py, _resolve_dispatcher_settings outside the tick
loop), and max_in_progress=0 counts as "not set" for _positive_int_setting
(gateway/kanban_watchers_dispatcher.py) and is replaced by a memory-based
default.

The death or kill of the holder drops all locks automatically (the kernel
closes the fd): the next gateway tick resumes spawning by itself, with no
restart of anything.

Owner stop (so that an automated cleanup never lifts a stop the owner put up):
  start --owner O --reason R    the holder is marked as an owner stop: it writes
                                a meta file next to the pid file (owner,
                                reason, since, pid);
  annotate --owner O --reason R attach the mark to a LIVE holder without a
                                restart: a window without the lock is ruled
                                out entirely;
  release                       refuses (code 3) to lift a holder that has an
                                owner mark; only --force lifts it;
  status                        shows pid/locks/owner/reason/age.
The health watchdog and the board watchdog never lift such a stop: health only
alerts when the stop has stood for more than 24 h or the holder has vanished.

Commands:
  kanban-dispatch-hold.py start [--owner O] [--reason R] [--wait]
  kanban-dispatch-hold.py annotate --owner O --reason R
  kanban-dispatch-hold.py status
  kanban-dispatch-hold.py release [--force] [--by NAME]
"""
from __future__ import annotations

import fcntl
import glob
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import IO

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from factory_config import hermes_home  # noqa: E402

HOME = Path(hermes_home())
PIDFILE = HOME / "cache" / "kanban-dispatch-hold.pid"
METAFILE = HOME / "cache" / "kanban-dispatch-hold.meta.json"
LOG = HOME / "logs" / "kanban-dispatch-hold.log"

_held: dict[str, IO[bytes]] = {}
_stop = False
_owner: str | None = None
_reason: str | None = None


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def board_dbs() -> list[str]:
    """All kanban databases of this home: default + the board directory.

    An extra lock (an archived board) is harmless: the dispatcher does not
    tick it, we just hold the file. A missed board would be a hole in the stop.
    """
    paths = [str(HOME / "kanban.db")]
    paths += sorted(glob.glob(str(HOME / "kanban" / "boards" / "*" / "kanban.db")))
    return [p for p in paths if os.path.exists(p)]


def acquire_all() -> None:
    for db in board_dbs():
        if db in _held:
            continue
        lock_path = db + ".dispatch.lock"
        try:
            os.makedirs(os.path.dirname(lock_path), exist_ok=True)
            fh = open(lock_path, "a+b")
            ok = False
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                ok = True
            except OSError:
                pass
            if ok:
                _held[db] = fh
                log(f"HELD {lock_path}")
            else:
                fh.close()
                # The lock is taken: a live dispatcher tick holds it for
                # seconds; if we are not stopping anything, the holder is not
                # us. Complain loudly.
                log(f"CONTESTED {lock_path} (tick in flight or foreign holder)")
        except OSError as exc:
            log(f"ERROR open {lock_path}: {exc}")


def on_term(_sig, _frm):
    global _stop
    _stop = True


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def current_holder() -> int | None:
    try:
        return int(PIDFILE.read_text().strip())
    except (OSError, ValueError):
        return None


def write_meta(owner: str, reason: str, holder_pid: int) -> dict:
    """Meta file of an owner stop: atomic (tmp + rename), so that status and
    release never see half a write."""
    meta = {"owner": owner, "reason": reason,
            "since": int(time.time()), "holder_pid": holder_pid}
    METAFILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = METAFILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, METAFILE)
    return meta


def read_meta() -> dict | None:
    try:
        return json.loads(METAFILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def drop_meta_for(pid: int | None) -> None:
    """Delete the meta if it belongs to pid (or no pid is given at all)."""
    meta = read_meta()
    if not meta:
        return
    mp = meta.get("holder_pid")
    if mp == pid or pid is None:
        try:
            METAFILE.unlink()
            log(f"META-REMOVED holder pid={mp}")
        except OSError:
            pass


def meta_summary(meta: dict | None) -> str:
    if not meta:
        return "(meta missing)"
    age_h = (time.time() - meta.get("since", time.time())) / 3600.0
    return (f"owner={meta.get('owner', '?')} reason=\"{meta.get('reason', '?')}\" "
            f"since={time.strftime('%Y-%m-%d %H:%M', time.localtime(meta.get('since', 0)))} "
            f"({age_h:.1f} h) holder_pid={meta.get('holder_pid', '?')}")


def wait_for_holder_gone(prev: int) -> None:
    """--wait: queue up until the live holder exits, then take its place at
    once. The lock-free window is closed by the fact that we are already
    waiting here: less than a second passes between the previous holder's
    death and our acquire_all, and the gateway tick (once per 60 s) usually
    does not make it in."""
    log(f"WAIT for holder pid={prev} to exit")
    while pid_alive(prev):
        time.sleep(0.5)
    log(f"WAIT done: holder pid={prev} gone")


def cmd_start(owner: str | None, reason: str | None, wait: bool) -> int:
    global _owner, _reason
    _owner, _reason = owner, reason
    prev = current_holder()
    if prev is not None and pid_alive(prev):
        if not wait:
            extra = ""
            meta = read_meta()
            if meta and meta.get("holder_pid") == prev:
                extra = " (" + meta_summary(meta) + ")"
            print(f"already running, pid={prev}{extra}")
            return 0
        wait_for_holder_gone(prev)
    else:
        # The slot is free: the meta of the previous holder (if it died
        # silently) is stale.
        meta = read_meta()
        if meta:
            mp = meta.get("holder_pid")
            if mp is None or mp == prev or not pid_alive(mp):
                drop_meta_for(mp)
    PIDFILE.parent.mkdir(parents=True, exist_ok=True)
    PIDFILE.write_text(str(os.getpid()) + "\n")
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    note = ""
    if owner:
        write_meta(owner, reason or "", os.getpid())
        note = f" owner={owner} reason=\"{reason or ''}\""
    log(f"START pid={os.getpid()} holding dispatch locks; ready queue frozen{note}")
    while not _stop:
        acquire_all()
        # New boards are picked up within 30 s; what is held is not released.
        time.sleep(30)
    log("STOP released all dispatch locks")
    for fh in _held.values():
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        fh.close()
    _held.clear()
    try:
        PIDFILE.unlink()
    except OSError:
        pass
    drop_meta_for(os.getpid())
    return 0


def cmd_annotate(owner: str, reason: str) -> int:
    """Owner-stop mark on a live holder: no restart, the lock is not released
    for a single microsecond. This is how a holder started by hand before the
    owner mechanism existed gets marked."""
    pid = current_holder()
    if pid is None or not pid_alive(pid):
        print("not running: nothing to annotate, run start --owner ... --reason ...")
        return 1
    meta = write_meta(owner, reason, pid)
    log(f"ANNOTATED pid={pid} {meta_summary(meta)}")
    print(f"annotated pid={pid}: {meta_summary(meta)}")
    return 0


def cmd_status() -> int:
    pid = current_holder()
    meta = read_meta()
    if pid is not None and pid_alive(pid):
        held = "\n".join(f"  held: {p}" for p in sorted(_held)) or _held_from_log()
        print(f"running pid={pid}, locks held:\n{held}")
        if meta and meta.get("holder_pid") == pid and meta.get("owner"):
            print(f"owner-stop: {meta_summary(meta)}")
        return 0
    print("not running")
    if meta and meta.get("owner"):
        print(f"STALE owner-stop meta (no holder): {meta_summary(meta)}")
    return 1


def _held_from_log() -> str:
    """Locks of the live holder, taken from its log (status is called from a
    different process)."""
    try:
        lines = LOG.read_text(encoding="utf-8").splitlines()
    except OSError:
        return "  (no log)"
    seen = set()
    for line in lines:
        if " HELD " in line:
            seen.add(line.split(" HELD ", 1)[1].strip())
        elif "STOP released" in line:
            seen.clear()
    return "\n".join(f"  held: {p}" for p in sorted(seen)) or "  (no locks found)"


def cmd_release(force: bool, by: str) -> int:
    pid = current_holder()
    meta = read_meta()
    owns_stop = bool(meta and meta.get("owner") and meta.get("holder_pid") == pid)
    if owns_stop and not force:
        print("REFUSED: owner stop, release without --force does not lift it.")
        print("  " + meta_summary(meta))
        print("  the owner can lift it: kanban-dispatch-hold.py release --force")
        log(f"RELEASE-REFUSED pid={pid} by={by or '?'}: {meta_summary(meta)}")
        return 3
    if pid is None or not pid_alive(pid):
        print("not running")
        try:
            PIDFILE.unlink()
        except OSError:
            pass
        if meta and meta.get("owner"):
            try:
                METAFILE.unlink()
            except OSError:
                pass
            print("stale owner-stop meta removed")
        return 0
    tag = " (force, owner-stop)" if owns_stop else ""
    log(f"RELEASE pid={pid} by={by or '?'}{tag}")
    os.kill(pid, signal.SIGTERM)
    # PEP 475: the handler runs, but time.sleep(30) still sleeps out its time;
    # wait 40 s.
    for _ in range(200):
        if not pid_alive(pid):
            print(f"released pid={pid}{tag}")
            if owns_stop:
                try:
                    METAFILE.unlink()
                except OSError:
                    pass
            return 0
        time.sleep(0.2)
    print(f"pid={pid} ignored SIGTERM")
    return 1


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    args = sys.argv[2:]
    if cmd == "start":
        owner = args[args.index("--owner") + 1] if "--owner" in args else None
        reason = args[args.index("--reason") + 1] if "--reason" in args else None
        return cmd_start(owner, reason, "--wait" in args)
    if cmd == "annotate":
        if "--owner" not in args or "--reason" not in args:
            print("annotate requires --owner and --reason")
            return 2
        owner = args[args.index("--owner") + 1]
        reason = args[args.index("--reason") + 1]
        return cmd_annotate(owner, reason)
    if cmd == "status":
        return cmd_status()
    if cmd == "release":
        by = args[args.index("--by") + 1] if "--by" in args else ""
        return cmd_release("--force" in args, by)
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
