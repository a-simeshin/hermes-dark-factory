#!/usr/bin/env python3
"""Tests for visual-delta-capture.py.

Covers the logic without a network (except localhost) and without a build:
  1  resolve_ref: .html and a directory -> kind html; a git ref -> kind git+SHA
  2  resolve_ref: a missing path and a non-ref -> StepError with a hint
  3  needs_build: the artifact cache works, --fresh forces a rebuild
  4  pick_free_port: does not take an already busy port
  5  stand registry: a dead pid is stale, a live one is reused
  6  wait_ready: by condition (a real http.server), not by sleep
  7  idempotence: a second main() run with --keep-stands does not rebuild the
     artifact and reuses the stand; without the flag the stands are stopped
     after the capture (html sides, build/shoot replaced)
  8  a build failure gives a FAIL line and code 1, not a traceback

Run: python3 test_visual_delta_capture.py (it prepares its own sandbox).
Needs GNU `timeout` on PATH (run-bounded.sh uses it for the html stands).
"""
import http.server
import importlib.util
import contextlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
SRC = SCRIPTS / "visual-delta-capture.py"

fails = []


def check(name, cond, detail=""):
    print("  [%s] %s %s" % ("PASS" if cond else "FAIL", name, detail))
    if not cond:
        fails.append(name)


def load_mod(tmp_root: Path):
    """The module with sandbox stand-cache paths."""
    os.environ["HERMES_HOME"] = str(tmp_root / "hermes-home")
    os.environ["FACTORY_REPO_PATH"] = str(tmp_root / "repo")
    os.environ["VISUAL_DELTA_STANDS_DIR"] = str(tmp_root / "stands")
    spec = importlib.util.spec_from_file_location("vd_capture", SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    (path / "f.txt").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], cwd=path, check=True)
    return path


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="vd-cap-test-"))
    mod = load_mod(tmp)
    repo = git_repo(tmp / "repo")
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                         capture_output=True, text=True).stdout.strip()

    print("1. resolve_ref: html and a directory")
    html = tmp / "proto.html"
    html.write_text("<b>hi</b>")
    r = mod.resolve_ref(str(html), repo)
    check("html file", r["kind"] == "html" and r["path"] == html.resolve(),
          str(r))
    d = tmp / "protodir"
    d.mkdir()
    (d / "index.html").write_text("x")
    r = mod.resolve_ref(str(d), repo)
    check("prototype directory", r["kind"] == "html", str(r))

    print("2. resolve_ref: a junk ref")
    try:
        mod.resolve_ref("no-such-branch-and-path", repo)
        check("StepError on junk", False)
    except mod.StepError as e:
        check("StepError on junk", "not found" in str(e), str(e)[:80])
    notref = tmp / "proto.txt"
    notref.write_text("x")
    try:
        mod.resolve_ref(str(notref), repo)
        check("StepError on a non-html path", False)
    except mod.StepError as e:
        check("StepError on a non-html path", ".html" in str(e), str(e)[:80])

    print("3. needs_build / artifact cache")
    stand = mod.STANDS_DIR / sha[:12]
    stand.mkdir(parents=True)
    check("no artifact -> build", mod.needs_build(stand, sha, False))
    (stand / "exec.jar").write_bytes(b"jar")
    (stand / "BUILD.json").write_text(json.dumps({"sha": "other"}))
    check("foreign sha -> build", mod.needs_build(stand, sha, False))
    (stand / "BUILD.json").write_text(json.dumps({"sha": sha}))
    check("cache_hit", not mod.needs_build(stand, sha, False))
    check("fresh forces", mod.needs_build(stand, sha, True))

    print("4. pick_free_port")
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen(1)
    busy_port = busy.getsockname()[1]
    ports = {mod.pick_free_port() for _ in range(8)}
    check("all free", busy_port not in ports, "busy=%d" % busy_port)
    busy.close()

    print("5. stand registry")
    reg = {"deadpid": {"port": 1, "pid": 99999999, "url_path": "/"},
           "alive": {"port": 2, "pid": os.getpid(), "url_path": "/"}}
    mod.save_registry(reg)
    back = mod.load_registry()
    check("live pid", mod.pid_alive(back["alive"]["pid"]))
    check("dead pid", not mod.pid_alive(back["deadpid"]["pid"]))

    print("6. wait_ready by condition")
    srv = http.server.HTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
    sport = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    t0 = time.monotonic()
    try:
        mod.wait_ready(sport, "/", 10, "test-http")
        ok = True
    except mod.StepError:
        ok = False
    dt = time.monotonic() - t0
    check("ready server: at once", ok and dt < 3, "%.1fs" % dt)
    try:
        mod.wait_ready(mod.pick_free_port(), "/", 2.0, "empty-port")
        check("dead port: StepError", False)
    except mod.StepError as e:
        check("dead port: StepError", "did not respond" in str(e), str(e)[:60])
    srv.shutdown()

    print("7. idempotence: the second run without a rebuild")
    calls = {"build": 0, "shoot": 0, "kill": 0}

    def fake_build(repo_, sha_, stand_dir, log):
        calls["build"] += 1
        stand_dir.mkdir(parents=True, exist_ok=True)
        (stand_dir / "exec.jar").write_bytes(b"jar")
        (stand_dir / "BUILD.json").write_text(json.dumps({"sha": sha_}))
        return stand_dir / "exec.jar"

    def fake_shoot(urls, args):
        calls["shoot"] += 1
        check("side URLs differ", urls["before"] != urls["after"],
              str(urls))
        return 0

    # html sides: the stands really start (http.server, localhost)
    a, b = tmp / "a.html", tmp / "b.html"
    a.write_text("<p>before</p>")
    b.write_text("<p>after</p>")

    mod.build_jar, mod.shoot = fake_build, fake_shoot

    def run_main(argv_extra=()):
        old = sys.argv
        sys.argv = ["visual-delta-capture.py",
                    "--label", "t1", "--before", str(a), "--after", str(b)]
        sys.argv += list(argv_extra)
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = mod.main()
            return rc, buf.getvalue()
        finally:
            sys.argv = old

    rc1, out1 = run_main(["--keep-stands"])
    b1, s1 = calls["build"], calls["shoot"]
    check("first run green", rc1 == 0, "rc=%d" % rc1)
    check("both stands started", "started" in out1, out1.count("started"))

    rc2, out2 = run_main(["--keep-stands"])
    check("second run green", rc2 == 0, "rc=%d" % rc2)
    check("artifact not rebuilt", calls["build"] == b1,
          "build=%d" % calls["build"])
    check("stands reused",
          out2.count("reused") == 2
          and "started" not in out2, out2)

    rc2b, out2b = run_main()
    check("without --keep-stands the stands are stopped",
          rc2b == 0 and out2b.count("stopped") == 2, out2b)

    print("8. a build failure -> FAIL, rc=1, no traceback")

    def broken_build(*_a, **_k):
        raise mod.StepError("build of sha failed (log: x):\nboom")

    mod.build_jar = broken_build
    rc3, out3 = run_main(["--label", "t3", "--repo", str(repo),
                          "--before", "HEAD", "--after", "HEAD", "--fresh"])
    check("rc=1", rc3 == 1, "rc=%d" % rc3)
    fail_lines = [l for l in out3.splitlines() if l.startswith("FAIL ")]
    check("FAIL line present", bool(fail_lines),
          fail_lines[0][:60] if fail_lines else out3[:60])
    check("no traceback", "Traceback" not in out3)
    mod.build_jar = fake_build

    # clean up the http stands of the test
    mod.kill_stands([mod.stand_key({"kind": "html", "path": a.resolve()}),
                     mod.stand_key({"kind": "html", "path": b.resolve()})])
    shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print("RED: %d failures: %s" % (len(fails), ", ".join(fails)))
        return 1
    print("GREEN: all scenarios passed")
    return 0


if __name__ == "__main__":
    # Stands start through run-bounded.sh, which needs GNU timeout (Linux).
    if shutil.which("timeout") is None:
        print("SKIP test_visual_delta_capture (needs GNU timeout for"
              " run-bounded.sh; Linux only)")
        sys.exit(0)
    sys.exit(main())
