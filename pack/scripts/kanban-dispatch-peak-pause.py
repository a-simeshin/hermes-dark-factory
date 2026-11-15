#!/usr/bin/env python3
"""Peak-hours pause of the kanban dispatcher (default: Mon-Fri 09:00-13:00 Moscow).

During the peak window the LLM provider bills quota at a higher rate (the
defaults describe the Z.AI GLM Coding plan; the window is provider-specific).
Handing out cards in that window is frozen with the same mechanism as the
owner stop: a holder of flock on <db>.dispatch.lock of all boards
(kanban-dispatch-hold.py). The gateway tick gets skipped_locked and spawns no
workers; workers already running finish their work.

The window is configuration, the defaults are the original values:
  FACTORY_PEAK_TZ     IANA time zone of the window   (default Europe/Moscow)
  FACTORY_PEAK_START  window start, HH:MM            (default 09:00)
  FACTORY_PEAK_END    window end, HH:MM, exclusive   (default 13:00)
  FACTORY_PEAK_DAYS   weekdays, 0=Mon, comma-separated (default 0,1,2,3,4)

Main mode `tick` (a cron every 15 min, no-agent): decides on its own whether
to hold or release, from the current time in the configured zone.
Self-healing: a holder that died inside the window is restarted by the next
tick, a forgotten peak holder outside the window is lifted by the first tick.

Priorities (an owner stop always outranks the peak):
- inside the window a foreign owner stop (meta.owner != peak-pause) => the
  peak does nothing: the dispatcher is already stopped, the foreign stop is
  neither overwritten nor lifted;
- inside the window a live holder without meta (a quota stop from the health
  watchdog) => the peak attaches its own meta with annotate, WITHOUT lifting
  the locks: then the quota recovery (hold_owner_stop) will not lift the stop
  in the middle of the peak;
- outside the window ONLY a holder with meta.owner == peak-pause is lifted
  (release --force): foreign and nameless stops are never touched.

Commands:
  kanban-dispatch-peak-pause.py tick      decide by the current time
  kanban-dispatch-peak-pause.py begin     hold (explicit, no window check)
  kanban-dispatch-peak-pause.py end       release, if the holder is ours
  kanban-dispatch-peak-pause.py status    window, holder, decision
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from factory_config import cfg, cfg_list, hermes_home  # noqa: E402

HOME = Path(hermes_home())
HOLD = Path(os.path.dirname(os.path.abspath(__file__))) / "kanban-dispatch-hold.py"
PIDFILE = HOME / "cache" / "kanban-dispatch-hold.pid"
METAFILE = HOME / "cache" / "kanban-dispatch-hold.meta.json"
LOGFILE = HOME / "logs" / "kanban-dispatch-peak-pause.log"


def _hm(text: str) -> tuple[int, int]:
    h, _, m = text.partition(":")
    return int(h), int(m or 0)


# Provider-specific peak window; the defaults are the original values.
_TZ_NAME = cfg("FACTORY_PEAK_TZ", "Europe/Moscow")
TZ = ZoneInfo(_TZ_NAME)
_START = cfg("FACTORY_PEAK_START", "09:00")
_END = cfg("FACTORY_PEAK_END", "13:00")
PEAK_START = _hm(_START)
PEAK_END = _hm(_END)
PEAK_DAYS = frozenset(int(d) for d in cfg_list("FACTORY_PEAK_DAYS", ("0", "1", "2", "3", "4")))  # 0=Mon
PEAK_OWNER = "peak-pause"
PEAK_REASON = ("provider peak window %s-%s %s, days %s (quota billed at a higher rate)"
               % (_START, _END, _TZ_NAME, ",".join(str(d) for d in sorted(PEAK_DAYS))))


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        LOGFILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOGFILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def now_msk() -> datetime:
    # name kept from the original (Moscow default); the zone is FACTORY_PEAK_TZ
    return datetime.now(TZ)


def in_peak(dt: datetime) -> bool:
    """Peak weekdays, start (inclusive) .. end (exclusive), in the configured zone."""
    if dt.weekday() not in PEAK_DAYS:
        return False
    hm = (dt.hour, dt.minute)
    return PEAK_START <= hm < PEAK_END


def holder_pid() -> int | None:
    try:
        return int(PIDFILE.read_text().strip())
    except (OSError, ValueError):
        return None


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def read_meta() -> dict | None:
    try:
        return json.loads(METAFILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _hold(*args: str, wait: bool = True) -> int:
    """Call kanban-dispatch-hold.py with the same HERMES_HOME."""
    cmd = [sys.executable, str(HOLD), *args]
    if wait:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=120).returncode
    subprocess.Popen(cmd, start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return 0


def ensure_hold(why: str) -> str:
    """Hold the stop during the peak. Never touches a foreign owner stop."""
    pid = holder_pid()
    meta = read_meta()
    if pid_alive(pid):
        own_meta = bool(meta and meta.get("holder_pid") in (None, pid))
        if meta and own_meta and meta.get("owner") == PEAK_OWNER:
            return "peak-holder already running"
        if meta and own_meta and meta.get("owner"):
            # A foreign owner stop: the dispatcher is already stopped, the
            # peak does not touch it.
            log(f"SKIP {why}: owner-stop active owner={meta.get('owner')}")
            return "owner-stop active, skipped"
        # A live holder without meta (a quota stop) or with a stale meta:
        # attach our own mark WITHOUT lifting the locks (annotate), so that
        # the quota recovery does not lift the stop in the middle of the peak
        # (hold_owner_stop).
        code = _hold("annotate", "--owner", PEAK_OWNER, "--reason", PEAK_REASON)
        if code == 0:
            log(f"ANNOTATED pid={pid} with peak meta ({why})")
            return "annotated existing holder"
        log(f"WARN annotate failed rc={code} ({why})")
        return "annotate failed"
    # No holder: remove a stale meta (if its owner is dead) and start our own
    # peak holder (detached, like the quota stop in the health watchdog).
    if meta and meta.get("owner") and meta.get("owner") != PEAK_OWNER:
        mp = meta.get("holder_pid")
        if mp is None or not pid_alive(mp):
            try:
                METAFILE.unlink()
                log(f"META-REMOVED stale (owner={meta.get('owner')}, pid={mp})")
            except OSError:
                pass
        else:
            log(f"SKIP {why}: owner-stop alive pid={mp} without a pid file")
            return "owner-stop alive, skipped"
    _hold("start", "--owner", PEAK_OWNER, "--reason", PEAK_REASON, wait=False)
    log(f"STARTED peak holder ({why})")
    return "started"


def ensure_released(why: str) -> str:
    """Outside the window lift ONLY our own peak holder. Foreign stops are untouched."""
    meta = read_meta()
    pid = holder_pid()
    if not (meta and meta.get("owner") == PEAK_OWNER):
        if pid_alive(pid):
            log(f"SKIP release {why}: holder pid={pid} is foreign/nameless")
            return "foreign holder, skipped"
        return "nothing to do"
    # Ours (or a stale one of ours): release lifts a live one and cleans up a dead one.
    code = _hold("release", "--force", "--by", "peak-schedule")
    log(f"RELEASE peak holder pid={pid} rc={code} ({why})")
    return "released" if code == 0 else "release failed"


def cmd_tick() -> int:
    dt = now_msk()
    if in_peak(dt):
        ensure_hold(f"tick in-peak {dt:%H:%M}")
    else:
        ensure_released(f"tick out-of-peak {dt:%H:%M}")
    return 0


def cmd_status() -> int:
    dt = now_msk()
    pid = holder_pid()
    meta = read_meta()
    print(f"now={dt:%Y-%m-%d %H:%M %a} tz={_TZ_NAME} in_peak={in_peak(dt)}")
    print(f"holder pid={pid} alive={pid_alive(pid)}")
    print(f"meta={json.dumps(meta, ensure_ascii=False) if meta else None}")
    return 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "tick":
        return cmd_tick()
    if cmd == "begin":
        ensure_hold("manual begin")
        return 0
    if cmd == "end":
        ensure_released("manual end")
        return 0
    if cmd == "status":
        return cmd_status()
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
