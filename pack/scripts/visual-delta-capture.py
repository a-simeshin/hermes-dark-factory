#!/usr/bin/env python3
"""One-call Before/After capture for a UI change.

One command instead of the chain "two worktrees, dependency install, build,
ports, browser, image library", which cost a median of 33 tool calls and 19
minutes per card when done by hand:

    python3 visual-delta-capture.py \
        --label pr241 \
        --before origin/main \
        --after  feature/connection-timeout-ui \
        --route / --viewport 1280x800 \
        --shot full=full --annotate "what to look at"

A Before/After ref is a git branch, tag or SHA, OR a path to an .html file or
a directory (an HTML prototype: served by python http.server, no build).

What it does:
  1. resolves both refs to SHAs (an html path stays a path);
  2. builds the artifact of a SHA once and caches it in
     $HERMES_HOME/cache/visual-delta-stands/<sha>/ as artifact<ext> (the
     extension of the built file; a repeated run skips the build);
  3. starts the stand (demo application / http.server) through run-bounded.sh;
     it lives in the stands.json registry and is reused while it is alive and
     healthy; readiness is awaited by condition (an HTTP answer), not blindly;
  4. visual-delta-shot.py (CDP) takes the screenshots, the PNGs go to the
     assets branch, and the ready Before/After markdown is printed to stdout.

Settings (factory.env):
  FACTORY_REPO_PATH             local clone the refs are resolved in
  FACTORY_STAND_BUILD_CMD       build command, run in the worktree of a SHA
  FACTORY_STAND_ARTIFACT_GLOB   built artifact, relative to the worktree
  FACTORY_STAND_RUN_CMD         stand start command; {artifact} and {port} are
                                substituted (required for a git side)
  FACTORY_STAND_DEPS_REL        dependency directory (relative to the repo)
                                that a fresh worktree needs and that is
                                symlinked from an existing checkout (optional)
  FACTORY_STAND_ROUTE           default route on the stand (optional, "/")
  FACTORY_CHROME_BIN            headless browser binary (optional; unset: the
                                browser already listening on FACTORY_CDP_PORT)

Flags: --steps-file (JS that brings the page into the needed state), --shot
NAME=SELECTOR (not only full), --fresh (rebuild the artifact), --keep-stands
(leave the stands until the TTL for reuse; by default they are stopped right
after the capture), --ttl-min (TTL with --keep-stands, default 10 min),
--no-push (do not push to the assets branch).
A failure of any step prints "FAIL <phase>: reason" and exits 1, no traceback.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from factory_config import cfg, hermes_home  # noqa: E402

STANDS_DIR = Path(os.environ.get(
    "VISUAL_DELTA_STANDS_DIR",
    os.path.join(hermes_home(), "cache", "visual-delta-stands"))).expanduser()
REGISTRY = STANDS_DIR / "stands.json"
CHROME_BIN = cfg("FACTORY_CHROME_BIN", "")


class StepError(Exception):
    """A phase error with a human-readable reason."""


# ---------- refs ----------

def resolve_ref(ref: str, repo: Path) -> dict:
    """git ref -> {kind: git, sha}; .html/directory -> {kind: html, path}."""
    p = Path(ref).expanduser()
    if p.exists() and (p.suffix == ".html" or p.is_dir()):
        return {"kind": "html", "path": p.resolve()}
    if p.exists():
        raise StepError(
            "path %s exists but is neither .html nor a directory; "
            "pass a branch/SHA for the git side" % ref)
    done = subprocess.run(
        ["git", "rev-parse", "--verify", ref + "^{commit}"],
        cwd=repo, capture_output=True, text=True, timeout=60)
    if done.returncode != 0:
        raise StepError(
            "ref %r not found, neither as a path nor as a git object in %s: %s"
            % (ref, repo, done.stderr.strip()[:200]))
    sha = done.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise StepError("git returned a non-SHA for %r: %r" % (ref, sha))
    return {"kind": "git", "sha": sha, "ref": ref}


# ---------- artifact build ----------

def donor_node_modules(repo: Path) -> Path | None:
    """A ready dependency directory of the same line (recipe: a symlink)."""
    rel = cfg("FACTORY_STAND_DEPS_REL", "")
    if not rel:
        return None
    for cand in [repo / rel] + \
            sorted(glob.glob(str(repo / ".worktrees" / "*" / rel))):
        if Path(cand).is_dir():
            return Path(cand)
    return None


def cached_artifact(stand_dir: Path) -> Path | None:
    """The cached artifact named in BUILD.json, if it is there."""
    try:
        name = json.loads((stand_dir / "BUILD.json").read_text()).get("artifact")
    except (OSError, ValueError):
        return None
    path = stand_dir / name if name else None
    return path if path and path.exists() else None


def needs_build(stand_dir: Path, sha: str, fresh: bool) -> bool:
    if fresh or cached_artifact(stand_dir) is None:
        return True
    meta = json.loads((stand_dir / "BUILD.json").read_text())
    return meta.get("sha") != sha


def build_artifact(repo: Path, sha: str, stand_dir: Path, log: Path) -> Path:
    """Worktree at the SHA + build command -> stand_dir/artifact<ext>. Once per SHA."""
    stand_dir.mkdir(parents=True, exist_ok=True)
    wt = stand_dir / "wt"
    if wt.exists():
        head = subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        if head != sha:
            shutil.rmtree(wt, ignore_errors=True)
    if not wt.exists():
        done = subprocess.run(
            ["git", "worktree", "add", "--detach", str(wt), sha],
            cwd=repo, capture_output=True, text=True, timeout=120)
        if done.returncode != 0:
            raise StepError("worktree add %s: %s"
                            % (sha[:10], done.stderr.strip()[:300]))
    deps_rel = cfg("FACTORY_STAND_DEPS_REL", "")
    if deps_rel:
        donor = donor_node_modules(repo)
        nm = wt / deps_rel
        if donor and not nm.exists():
            nm.parent.mkdir(parents=True, exist_ok=True)
            nm.symlink_to(donor, target_is_directory=True)
        if not donor and not nm.exists():
            raise StepError(
                "no dependency directory %s in the main checkout or in "
                ".worktrees/*; first build any line the usual way (install "
                "the dependencies once)" % deps_rel)
    cmd = shlex.split(cfg("FACTORY_STAND_BUILD_CMD"))
    with open(log, "w") as fh:
        done = subprocess.run(cmd, cwd=wt, stdout=fh, stderr=fh, timeout=1800)
    built = sorted(Path(p) for p in
                   glob.glob(str(wt / cfg("FACTORY_STAND_ARTIFACT_GLOB"))))
    if done.returncode != 0 or not built:
        tail = ""
        try:
            tail = "\n".join(log.read_text().splitlines()[-15:])
        except OSError:
            pass
        raise StepError("build of %s failed (log: %s):\n%s"
                        % (sha[:10], log, tail))
    artifact = stand_dir / ("artifact" + built[-1].suffix)
    shutil.copy2(built[-1], artifact)
    (stand_dir / "BUILD.json").write_text(json.dumps(
        {"sha": sha, "artifact": artifact.name,
         "built_at": time.strftime("%F %T")}, ensure_ascii=False))
    return artifact


# ---------- stands ----------

def load_registry() -> dict:
    try:
        return json.loads(REGISTRY.read_text())
    except (OSError, ValueError):
        return {}


def save_registry(reg: dict) -> None:
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY.write_text(json.dumps(reg, ensure_ascii=False, indent=1))


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def http_ok(port: int, route: str, timeout: float = 3.0) -> bool:
    url = "http://127.0.0.1:%d%s" % (port, route)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status in (200, 301, 302, 401)
    except urllib.error.HTTPError as e:
        return e.code in (200, 301, 302, 401)
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


def pick_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_ready(port: int, route: str, deadline_s: float, what: str) -> None:
    """Wait BY CONDITION (an HTTP answer), not blindly."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < deadline_s:
        if http_ok(port, route):
            return
        time.sleep(1.0)
    raise StepError("stand %s on :%d did not respond within %.0fs; see the log "
                    "next to the registry %s" % (what, port, deadline_s, REGISTRY))


def stand_key(side_ref: dict) -> str:
    if side_ref["kind"] == "git":
        return side_ref["sha"][:12]
    import hashlib
    return "html-" + hashlib.sha1(str(side_ref["path"]).encode()).hexdigest()[:10]


def ensure_stand(side_ref: dict, args, repo: Path, label: str) -> str:
    """The live URL of a side. Reuses a registered stand."""
    reg = load_registry()
    key = stand_key(side_ref)
    route = args.route
    entry = reg.get(key)
    if entry and pid_alive(entry["pid"]) \
            and http_ok(entry["port"], entry.get("url_path", route)):
        print("[stand] %s: reused :%d (pid %d)"
              % (key, entry["port"], entry["pid"]))
        return "http://127.0.0.1:%d%s" % (entry["port"],
                                          entry.get("url_path", route))

    if entry and not pid_alive(entry["pid"]):
        reg.pop(key, None)

    if side_ref["kind"] == "html":
        path: Path = side_ref["path"]
        serve_dir = path if path.is_dir() else path.parent
        rel = path.name if not path.is_dir() else "index.html"
        if not (serve_dir / rel).exists():
            raise StepError("HTML side: no %s in %s" % (rel, serve_dir))
        # http.server serves the directory from its root: its own route,
        # not the route of the application stand.
        route = "/" if rel == "index.html" else "/" + rel
        port = pick_free_port()
        cmd = [sys.executable, "-m", "http.server", str(port),
               "--bind", "127.0.0.1", "--directory", str(serve_dir)]
        log = STANDS_DIR / (key + ".http.log")
    else:
        stand_dir = STANDS_DIR / key
        log = stand_dir / "build.log"
        # The stand start command: a template, {artifact} and {port} are
        # substituted. Checked before the build, which is the expensive part.
        run_tpl = cfg("FACTORY_STAND_RUN_CMD", "")
        if not run_tpl:
            raise StepError("config: FACTORY_STAND_RUN_CMD is not set: add the "
                            "stand start command to factory.env ({artifact} and "
                            "{port} are substituted)")
        if needs_build(stand_dir, side_ref["sha"], args.fresh):
            t0 = time.monotonic()
            print("[build] %s: building%s ..."
                  % (key, " (fresh)" if args.fresh else ""))
            build_artifact(repo, side_ref["sha"], stand_dir, log)
            print("[build] %s: ready in %.0fs"
                  % (key, time.monotonic() - t0))
        else:
            print("[build] %s: artifact from cache" % key)
        port = pick_free_port()
        cmd = [part.format(artifact=str(cached_artifact(stand_dir)), port=port)
               for part in shlex.split(run_tpl)]
        log = stand_dir / "stand.log"

    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "ab") as fh:
        wrap = subprocess.Popen(
            [str(SCRIPTS / "run-bounded.sh"), "--owner",
             "visual-delta:%s" % label, "--ttl-min", str(args.ttl_min),
             "--"] + cmd,
            stdout=fh, stderr=fh, start_new_session=True)

    # The wrapper's pid became the pid of timeout by exec; the stand is its
    # child. Wait for readiness and record the pid of the wrapper.
    try:
        wait_ready(port, route, args.boot_timeout, key)
    except StepError:
        tail = ""
        try:
            tail = "\n".join(log.read_text(errors="replace").splitlines()[-12:])
        except OSError:
            pass
        raise StepError("stand %s did not come up on :%d\nlog %s:\n%s"
                        % (key, port, log, tail)) from None
    reg[key] = {"port": port, "pid": wrap.pid,
                "url_path": route, "started": time.strftime("%F %T")}
    save_registry(reg)
    print("[stand] %s: started :%d (pid %d)" % (key, port, wrap.pid))
    return "http://127.0.0.1:%d%s" % (port, route)


def kill_stands(keys: list[str]) -> None:
    reg = load_registry()
    for key in keys:
        entry = reg.pop(key, None)
        if entry and pid_alive(entry["pid"]):
            try:
                os.kill(entry["pid"], 15)
                print("[stand] %s: stopped (pid %d)" % (key, entry["pid"]))
            except OSError:
                pass
    save_registry(reg)


# ---------- capture ----------

def shoot(urls: dict, args) -> int:
    cmd = [sys.executable, str(SCRIPTS / "visual-delta-shot.py"),
           "--label", args.label,
           "--url-before", urls["before"], "--url-after", urls["after"],
           "--width", str(args.width), "--height", str(args.height)]
    if CHROME_BIN:
        cmd += ["--chrome-bin", CHROME_BIN]
    for shot in args.shot:
        cmd += ["--shot", shot]
    if args.steps_file:
        cmd += ["--steps-file", args.steps_file]
    if args.annotate:
        cmd += ["--annotate", args.annotate]
    if args.no_push:
        cmd += ["--no-push"]
    done = subprocess.run(cmd)
    if done.returncode != 0:
        raise StepError("capture (visual-delta-shot.py) failed, rc=%d"
                        % done.returncode)
    return done.returncode


def main() -> int:
    ap = argparse.ArgumentParser(
        description="one-call Before/After capture of a UI change")
    ap.add_argument("--label", required=True,
                    help="PNG prefix in the assets branch, e.g. pr241-issue240")
    ap.add_argument("--before", required=True,
                    help="branch/SHA or an .html path/directory")
    ap.add_argument("--after", required=True,
                    help="branch/SHA or an .html path/directory")
    ap.add_argument("--route", default=cfg("FACTORY_STAND_ROUTE", "/"),
                    help="route on the stand (default FACTORY_STAND_ROUTE or /)")
    w, h = 1280, 800
    ap.add_argument("--viewport", default="1280x800")
    ap.add_argument("--shot", action="append", default=["full=full"],
                    metavar="NAME=SELECTOR",
                    help="a frame; 'full' is the whole viewport (default)")
    ap.add_argument("--steps-file", default="",
                    help="JS: bring the page into the state before the frame")
    ap.add_argument("--annotate", default="", help="what to look at on After")
    ap.add_argument("--repo", default="",
                    help="local clone (default FACTORY_REPO_PATH)")
    ap.add_argument("--fresh", action="store_true",
                    help="rebuild the artifact even if it is cached")
    ap.add_argument("--keep-stands", action="store_true",
                    help="leave the stands until the TTL for reuse "
                         "(by default they are stopped right after the capture)")
    ap.add_argument("--ttl-min", type=int, default=10,
                    help="run-bounded TTL of the stands with --keep-stands (min)")
    ap.add_argument("--boot-timeout", type=float, default=180.0,
                    help="how long to wait for stand readiness (s)")
    ap.add_argument("--no-push", action="store_true")
    args = ap.parse_args()

    m = re.fullmatch(r"(\d+)x(\d+)", args.viewport)
    if not m:
        print("FAIL viewport: %r, expected WxH, e.g. 1280x800"
              % args.viewport)
        return 2
    args.width, args.height = int(m.group(1)), int(m.group(2))
    repo = Path(args.repo or cfg("FACTORY_REPO_PATH")).expanduser()
    t0 = time.monotonic()

    rc = 0
    sides = {}
    try:
        for side in ("before", "after"):
            sides[side] = resolve_ref(getattr(args, side), repo)
        urls = {side: ensure_stand(sides[side], args, repo, args.label)
                for side in ("before", "after")}
        shoot(urls, args)
    except StepError as e:
        print("FAIL %s" % e)
        rc = 1
    except subprocess.TimeoutExpired as e:
        print("FAIL subprocess timeout: %s" % (e.cmd or e))
        rc = 1

    # Stands are stopped right after the capture (and on FAIL) unless reuse was
    # requested: every application stand holds a running server and must not
    # be kept for hours.
    if not args.keep_stands:
        kill_stands([stand_key(sides[s]) for s in ("before", "after")])
    if rc:
        return rc
    print("[done] label=%s in %.0fs" % (args.label, time.monotonic() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
