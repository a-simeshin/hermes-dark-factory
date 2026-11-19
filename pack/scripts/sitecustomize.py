"""Hermeticity audit hook for the tests. Picked up automatically through
PYTHONPATH=scripts (run-tests.py sets it). Violations are written to the file
named by HERMES_TEST_AUDIT_LOG as <test>\t<kind>\t<detail>. The hook never
blocks, it only records: run-tests.py --audit turns a record into a red test.

Allowed: the code under test next to this file, temporary directories, system
paths, loopback sockets. Forbidden: anything else on disk (the live Hermes
home with kanban.db, boards/, cache/, logs/), outside connections, real gh
calls and git calls with a remote URL (fixtures clone local bare repos from
temp directories, which is fine).

Without HERMES_TEST_AUDIT_LOG the hook is installed but writes nothing.
"""
import os
import shutil
import sys
import tempfile as _tempfile

_AUDIT_LOG = os.environ.get("HERMES_TEST_AUDIT_LOG")
_HERE = os.path.dirname(os.path.abspath(__file__))

_ALLOWED_ROOTS = tuple(os.path.realpath(p) for p in (
    _HERE,                       # code under test and neighbouring fixtures
    _tempfile.gettempdir(),      # test sandboxes
    "/usr", "/lib", "/lib64", "/etc", "/proc", "/sys", "/dev",
    "/var/tmp",
    os.path.expanduser("~/.local"),          # user site-packages
    sys.prefix, sys.base_prefix,             # interpreter or venv of the run
    "/nonexistent",                          # "file is missing" stubs in tests
)) + (_HERE, _tempfile.gettempdir())
_STUB_ROOTS = (_HERE, _tempfile.gettempdir(), os.path.realpath(_HERE),
               os.path.realpath(_tempfile.gettempdir()), "/var/tmp")
_TEST = os.path.basename(sys.argv[0]) if sys.argv else "?"


def _log(kind, detail):
    if not _AUDIT_LOG:
        return
    try:
        with open(_AUDIT_LOG, "a", encoding="utf-8") as fh:
            fh.write("%s\t%s\t%s\n" % (_TEST, kind, detail))
    except OSError:
        pass


def _allowed(path):
    try:
        p = os.path.abspath(os.fsdecode(path))
    except (TypeError, ValueError):
        return True
    return p.startswith(_ALLOWED_ROOTS) or os.path.realpath(p).startswith(
        _ALLOWED_ROOTS)


def _hook(event, args):
    if not _AUDIT_LOG:
        return
    if event == "open":
        path = args[0]
        if isinstance(path, (str, bytes)) and not _allowed(path):
            _log("file", path)
    elif event == "sqlite3.connect":
        db = args[0]
        if isinstance(db, (str, bytes)) and not _allowed(db):
            _log("live-db", db)
    elif event == "socket.connect":
        addr = args[1] if len(args) > 1 else None
        if isinstance(addr, tuple) and len(addr) >= 2:
            host = addr[0]
            if host not in ("127.0.0.1", "::1", "localhost", ""):
                _log("network", "%s:%s" % (host, addr[1]))
    elif event == "subprocess.Popen":
        # Event args are (executable, argv, cwd, env). A gh stub that a test
        # puts on PATH (a temp dir or scripts/) is fine; a gh resolving
        # anywhere else is the real client.
        argv = args[1] if len(args) > 1 else None
        if isinstance(argv, (list, tuple)) and argv:
            exe = os.fsdecode(argv[0]) if isinstance(argv[0], (str, bytes)) else str(argv[0])
            base = os.path.basename(exe)
            env = args[3] if len(args) > 3 and isinstance(args[3], dict) else None
            if base == "gh":
                path = (env or os.environ).get("PATH") if os.sep not in exe else None
                real = shutil.which(exe, path=path) if os.sep not in exe else exe
                if real and not os.path.realpath(real).startswith(_STUB_ROOTS):
                    _log("process", " ".join(str(a) for a in argv)[:200])
            elif base == "git" and any(
                    str(a).startswith(("http://", "https://", "git@", "ssh://"))
                    for a in argv[1:]):
                _log("process", " ".join(str(a) for a in argv)[:200])


sys.addaudithook(_hook)
