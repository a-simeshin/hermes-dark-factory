#!/usr/bin/env python3
"""pr-hygiene: eol_twin_promise catches promises of work on a retired line.

Origin: a "twin plan" section ("the old-line twin PR follows against
<old branch>", "the issue stays open until both halves ship") lived in a PR
body from the time of two release lines until the merge: through the
end-of-life decision and two PASS reviews. The detector must fire on that
text and stay silent on cancelled promises (dropped/EOL nearby) and on
"twin" that is not a release line (an SSE/stdio twin of a test case).

Run: python3 test_pr_hygiene_eol_twin.py
"""
import importlib.util
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.update({
    "HERMES_HOME": tempfile.mkdtemp(prefix="ph-eol-"),
    "FACTORY_REPO": "acme/widget",
    "FACTORY_REPO_PATH": "/srv/hermes/widget",
    "FACTORY_BOARD": "main",
    "FACTORY_OWNER_LOGIN": "owner-login",
    "FACTORY_OWNER_CHAT": "telegram:000000000",
    "FACTORY_BASE_BRANCHES": "release/1.x,release/2.x",
    "FACTORY_EOL_BRANCH": "release/1.x",
    "FACTORY_EOL_KEEP_BRANCH": "release/2.x",
})
SCRIPT = os.environ.get("PR_HYGIENE_SCRIPT") or os.path.join(
    HERE, "pr-hygiene.py")
spec = importlib.util.spec_from_file_location("pr_hygiene_eol", SCRIPT)
if spec is None or spec.loader is None:
    print("SKIP: cannot load pr-hygiene.py")
    sys.exit(0)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# The body of a PR written in the two-line era.
BODY_TWIN = """## 1.x twin plan

Issue #235 asks for both lines ("Both 2.x and 1.x lines (twin PR)"). This PR is the 2.x half; the 1.x twin PR follows against `release/1.x` once this lands.

- Ports over as-is (counterpart files verified present on `origin/release/1.x`): `KeepAliveTracker`, `PingDetectingClientTransport`.
- #235 stays open until both halves ship.
"""

# A historical record of both lines, with the owner's scope comment next to
# it (a cancellation). The detector must stay silent.
BODY_SCOPED = """Dropping the `formRefs.current = {}` reset on tool switch keeps
jest green (99/99) on both release lines: 2.x (PR #181, head bb857e7) and
the 1.x twin (PR #186, head 919de9d). Both PRs are merged.

After: a ToolsTab component test goes red without the reset and green
with it; scope release/2.x only (owner dropped the 1.x twin, see scope
comment).
"""

# "Case 1 twin" is an SSE/stdio twin of a test case, not a release line.
# A false positive is not acceptable.
BODY_TESTCASE = """- Case 1 twin: `SseProxyControllerTests$BuildSse.openStdio_...`.
Same `bufferedFrames` early-send-buffer race as case 1, on the stdio
build path; one fix covering the shared helper resolves both.
"""

BODY_DROPPED = """## 1.x twin plan

Dropped: 1.x EOL since 1.2.0, no twins. The 1.x twin PR is not coming.
"""


def test_twin_body_fires():
    hits = mod.eol_twin_hits(BODY_TWIN)
    joined = "\n".join(hits)
    assert "1.x twin PR follows" in joined, hits
    assert "stays open until both halves" in joined, hits


def test_scope_comment_silences():
    assert mod.eol_twin_hits(BODY_SCOPED) == []


def test_testcase_twin_is_not_line_twin():
    assert mod.eol_twin_hits(BODY_TESTCASE) == []


def test_dropped_marker_silences():
    """A promise in the same section as the cancellation: a hit next to
    "Dropped"/"EOL" in a neighbouring line is cancelled."""
    hits = mod.eol_twin_hits(BODY_DROPPED)
    assert hits == [], hits


def test_aggregation_kinds():
    lines, kinds = mod.eol_twin_promise(
        [{"number": 239, "body": BODY_TWIN}], [])
    assert kinds == {"pr": [239], "issue": []}, (kinds, lines)
    assert any("PR #239" in ln for ln in lines), lines

    lines, kinds = mod.eol_twin_promise(
        [], [{"number": 294, "body": BODY_SCOPED},
             {"number": 235, "body": BODY_TWIN}])
    assert kinds == {"pr": [], "issue": [235]}, kinds
    assert any("issue #235" in ln for ln in lines), lines


def test_empty_and_none():
    assert mod.eol_twin_hits("") == []
    assert mod.eol_twin_hits(None) == []
    assert mod.eol_twin_promise([], []) == ([], {"pr": [], "issue": []})


def test_off_without_eol_branch():
    """Without FACTORY_EOL_BRANCH the detector is off: a fresh load sees no
    promise even in the twin body."""
    saved = os.environ.pop("FACTORY_EOL_BRANCH")
    try:
        sp = importlib.util.spec_from_file_location("pr_hygiene_eol_off",
                                                    SCRIPT)
        m2 = importlib.util.module_from_spec(sp)
        sp.loader.exec_module(m2)
        assert m2.eol_twin_hits(BODY_TWIN) == []
    finally:
        os.environ["FACTORY_EOL_BRANCH"] = saved


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok %s" % name)
    print("OK test_pr_hygiene_eol_twin")
