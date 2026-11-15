#!/usr/bin/env python3
"""visual-delta-shot: Before/After screenshots for PRs and issues.

Rule of the factory: the body of every PR and issue starts with a
"## Before / After" section (UI: a table of two images, non-UI: a mermaid
diagram). This script makes the capture cheaper than an excuse: one call shoots
the same screen in two states (before/after), puts the PNGs into the assets
branch and prints a ready markdown table with raw links.

Both sides are shot in one Chrome through CDP (by default a live browser on
127.0.0.1:9222; for a headless binary see --chrome-bin, then its own instance
is started). The page state is brought to the needed one by a JS steps file,
the same on both sides.

Example:
  visual-delta-shot.py --label pr292 \\
      --url-before http://127.0.0.1:8191/ \\
      --url-after  http://127.0.0.1:8192/ \\
      --steps-file /tmp/steps-leak.js \\
      --shot timeline='#timeline-panel' --shot results='#results-panel' \\
      --annotate 'leak badge'

--annotate: a caption line under the table (what to look at on After).

Settings (factory.env): FACTORY_REPO_PATH (local clone, its origin names the
GitHub repository), FACTORY_ASSETS_DIR (clone of the assets branch),
FACTORY_ASSETS_BRANCH (default hermes-assets), FACTORY_CDP_PORT (default 9222).
"""
import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

import websockets

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from factory_config import cfg, hermes_home  # noqa: E402

ASSETS_DIR = cfg("FACTORY_ASSETS_DIR",
                 os.path.join(hermes_home(), "cache", "visual-delta-assets"))


def git(*args, cwd, timeout=120):
    done = subprocess.run(("git",) + args, cwd=cwd, capture_output=True,
                          text=True, timeout=timeout)
    if done.returncode != 0:
        raise RuntimeError("git %s: %s" % (args[:2], done.stderr.strip()[:400]))
    return done.stdout.strip()


def repo_slug(repo_dir):
    url = git("remote", "get-url", "origin", cwd=repo_dir)
    tail = url.rstrip("/").split(":")[-1]
    if tail.endswith(".git"):
        tail = tail[:-4]
    parts = tail.split("/")
    return "/".join(parts[-2:])


class Cdp:
    """A thin CDP client: one page websocket, request-response by id."""

    def __init__(self, ws):
        self.ws = ws
        self.next_id = 0
        self.pending = {}

    async def call(self, method, **params):
        self.next_id += 1
        rid = self.next_id
        await self.ws.send(json.dumps({"id": rid, "method": method,
                                       "params": params}))
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("id") == rid:
                if "error" in msg:
                    raise RuntimeError("%s: %s" % (method, msg["error"]))
                return msg.get("result", {})

    async def eval(self, expression, await_promise=False):
        res = await self.call("Runtime.evaluate",
                              expression=expression,
                              awaitPromise=await_promise,
                              returnByValue=True)
        if res.get("exceptionDetails"):
            raise RuntimeError("JS error: %s"
                               % json.dumps(res["exceptionDetails"])[:600])
        return res.get("result", {}).get("value")

    async def wait_ready(self, url, timeout=60):
        """A navigate/about:blank race: the steps could run in the empty
        context. Wait for BOTH the address and complete."""
        for _ in range(timeout * 4):
            try:
                href = await self.eval("location.href")
                state = await self.eval("document.readyState")
            except Exception:
                href, state = "", ""
            host = url.split("//", 1)[-1].split("/", 1)[0]
            if host in href and state == "complete":
                return
            await asyncio.sleep(0.25)
        raise RuntimeError("page not loaded in %ds: %s" % (timeout, url))


def new_tab(port, url, chrome_bin=None):
    """Opens a tab, returns (targetId, wsUrl)."""
    if chrome_bin:
        subprocess.Popen([chrome_bin, "--headless=new",
                          "--remote-debugging-port=%d" % port,
                          "--user-data-dir=%s/chrome-vd-%d"
                          % (tempfile.gettempdir(), port),
                          "--no-sandbox", "--disable-gpu",
                          "about:blank"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        import time
        time.sleep(3)
    req = urllib.request.Request(
        "http://127.0.0.1:%d/json/new?%s" % (port, urllib.parse.quote(url)),
        method="PUT")
    data = json.loads(urllib.request.urlopen(req, timeout=10).read())
    return data["id"], data["webSocketDebuggerUrl"]


def close_tab(port, target_id):
    try:
        urllib.request.urlopen(
            "http://127.0.0.1:%d/json/close/%s" % (port, target_id),
            timeout=10).read()
    except Exception:
        pass


async def shoot_side(ws_url, url, steps_js, shots, width, height):
    async with websockets.connect(ws_url, max_size=32 * 1024 * 1024) as ws:
        cdp = Cdp(ws)
        await cdp.call("Page.enable")
        await cdp.call("Emulation.setDeviceMetricsOverride",
                       width=width, height=height, deviceScaleFactor=1,
                       mobile=False)
        await cdp.call("Page.navigate", url=url)
        await cdp.wait_ready(url)
        await asyncio.sleep(1.5)             # let the page finish rendering
        if steps_js:
            await cdp.eval(steps_js, await_promise=True)
        out = {}
        for name, selector, hook in shots:
            if hook:
                await cdp.eval(hook, await_promise=True)
                await asyncio.sleep(0.8)
            if selector != "full":
                box = await cdp.eval(
                    "(() => {const el = document.querySelector(%r);"
                    " if (!el) return null; el.scrollIntoView("
                    "{block:'center'});"
                    " const r = el.getBoundingClientRect();"
                    " return {x: r.x, y: r.y, w: r.width, h: r.height};})()"
                    % selector)
                if not box:
                    raise RuntimeError("selector %r not found on %s"
                                       % (selector, url))
                await asyncio.sleep(0.3)
                shot = await cdp.call(
                    "Page.captureScreenshot", format="png",
                    clip={"x": box["x"], "y": box["y"],
                          "width": box["w"], "height": box["h"], "scale": 1},
                    captureBeyondViewport=False)
            else:
                shot = await cdp.call("Page.captureScreenshot", format="png")
            out[name] = shot["data"]
        return out


def publish(pngs, label, repo_dir, assets_dir, assets_branch, subdir):
    """Puts the PNGs into the assets branch, returns {name: raw_url}."""
    slug = repo_slug(repo_dir)
    os.makedirs(assets_dir, exist_ok=True)
    if not os.path.exists(os.path.join(assets_dir, ".git")):
        git("clone", "--branch", assets_branch, "--no-checkout",
            "https://github.com/%s.git" % slug, assets_dir, cwd=repo_dir,
            timeout=300)
    git("fetch", "origin", assets_branch, cwd=assets_dir)
    git("checkout", "-B", assets_branch,
        "origin/" + assets_branch, cwd=assets_dir)
    target = os.path.join(assets_dir, subdir)
    os.makedirs(target, exist_ok=True)
    import base64
    wrote = {}
    for (side, name), data in sorted(pngs.items()):
        path = os.path.join(target, "%s-%s.png" % (name, side))
        with open(path, "wb") as fh:
            fh.write(base64.b64decode(data))
        wrote[(side, name)] = ("https://raw.githubusercontent.com/%s/%s/%s/%s"
                               % (slug, assets_branch, subdir,
                                  os.path.basename(path)))
    git("add", "-A", cwd=assets_dir)
    if subprocess.run(["git", "-C", assets_dir, "diff", "--cached",
                       "--quiet"]).returncode != 0:
        git("commit", "-m", "visual-delta %s: %s" % (label, ", ".join(
            sorted(set(n for _, n in pngs)))), cwd=assets_dir)
    git("push", "origin", assets_branch, cwd=assets_dir, timeout=180)
    return wrote


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--label", required=True, help="file prefix, e.g. pr292")
    ap.add_argument("--url-before", required=True)
    ap.add_argument("--url-after", required=True)
    ap.add_argument("--steps-file",
                    help="JS file: an async IIFE that brings the page into a state")
    ap.add_argument("--shot", action="append", default=[],
                    metavar="NAME=SELECTOR", help="'full' = the whole viewport")
    ap.add_argument("--annotate", default="", help="what to look at on After")
    ap.add_argument("--cdp-port", type=int,
                    default=int(cfg("FACTORY_CDP_PORT", "9222")))
    ap.add_argument("--chrome-bin", default="",
                    help="start its own headless Chrome instead of the CDP port")
    ap.add_argument("--width", type=int, default=1600)
    ap.add_argument("--height", type=int, default=1000)
    ap.add_argument("--repo", default="",
                    help="local clone (default FACTORY_REPO_PATH)")
    ap.add_argument("--assets-branch",
                    default=cfg("FACTORY_ASSETS_BRANCH", "hermes-assets"))
    ap.add_argument("--subdir", default="",
                    help="subdirectory in assets (default screenshots/visual-delta/<label>)")
    ap.add_argument("--no-push", action="store_true",
                    help="do not push to the assets branch (debugging)")
    args = ap.parse_args()
    args.repo = os.path.expanduser(args.repo or cfg("FACTORY_REPO_PATH"))

    shots = []
    for item in args.shot:
        hook = ""
        if "@" in item:
            item, hook = item.rsplit("@", 1)
        name, _, selector = item.partition("=")
        shots.append((name, selector or "full",
                      open(hook).read() if hook else ""))
    steps_js = (open(args.steps_file).read() if args.steps_file else "")

    pngs = {}
    for side, url in (("before", args.url_before),
                      ("after", args.url_after)):
        target_id, ws_url = new_tab(args.cdp_port, "about:blank",
                                    args.chrome_bin or None)
        try:
            side_pngs = asyncio.run(shoot_side(ws_url, url, steps_js,
                                               shots, args.width, args.height))
            for name, data in side_pngs.items():
                pngs[(side, name)] = data
        finally:
            close_tab(args.cdp_port, target_id)

    subdir = args.subdir or ("screenshots/visual-delta/%s" % args.label)
    if args.no_push:
        import base64
        outdir = "/tmp/visual-delta/%s" % args.label
        os.makedirs(outdir, exist_ok=True)
        urls = {}
        for (side, name), data in sorted(pngs.items()):
            path = "%s/%s-%s.png" % (outdir, name, side)
            open(path, "wb").write(base64.b64decode(data))
            urls[(side, name)] = path
        print("[no-push] files in %s" % outdir)
    else:
        urls = publish(pngs, args.label, args.repo, ASSETS_DIR,
                       args.assets_branch, subdir)

    names = sorted(set(n for _, n in pngs))
    print()
    print("## Before / After")
    print()
    print("| Before | After |")
    print("|---|---|")
    for name in names:
        print("| ![before %s](%s) | ![after %s](%s) |"
              % (name, urls[("before", name)], name,
                 urls[("after", name)]))
    if args.annotate:
        print()
        print(args.annotate)


if __name__ == "__main__":
    main()
