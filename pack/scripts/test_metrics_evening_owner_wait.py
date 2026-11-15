#!/usr/bin/env python3
"""A question to the owner must reach the evening report even when the block
has no kind.

A card stood blocked for 13 days with the reason "Awaiting owner decision:
...", but its block event was written with kind=None (the worker called
`kanban block` without `--kind needs_input`), while needs_input_stale
filtered strictly by block_kind == 'needs_input'. The card was invisible to
section 7 of the evening report.

Rule: a blocked card with kind=None whose last block reason carries a sign of
waiting for the owner must reach needs_input_stale the same way as
block_kind='needs_input'.
"""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Never read or write the real Hermes home; neutral project values.
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="owner-wait-home-")
os.environ["FACTORY_REPO"] = "acme/widget"
os.environ["FACTORY_REPO_PATH"] = "/srv/hermes/widget"
os.environ["FACTORY_BOARD"] = "main"
os.environ["FACTORY_OWNER_LOGIN"] = "owner-login"
os.environ["FACTORY_OWNER_CHAT"] = "telegram:000000000"

sys.path.insert(0, str(HERE))

_spec = importlib.util.spec_from_file_location(
    "metrics_evening", HERE / "metrics-evening.py")
metrics_evening = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(metrics_evening)

NOW = 1_794_748_000.0
H = 3600.0


def _task(tid, kind, age_h=320.0, title="Plan review gate"):
    return {"id": tid, "status": "blocked", "block_kind": kind,
            "created_at": NOW - age_h * H, "title": title}


def test_owner_wait_without_kind_is_reported():
    """Regression: kind=None plus "Awaiting owner decision" in the reason."""
    tasks = [_task("t_owner1", None,
                   title="[reviewer] Plan review: issue #236 decomposition")]
    events = {"t_owner1": NOW - 307 * H}
    reasons = {"t_owner1": (
        "Verdict CHANGES published (comments 6379 + 6380). Awaiting owner "
        "decision: (1) archive the duplicates t_dup1, t_dup2; (2) priorities "
        "of the children.")}
    stale = metrics_evening.needs_input_stale(tasks, events, NOW,
                                              blocked_reasons=reasons)
    assert [s["id"] for s in stale] == ["t_owner1"], (
        "a question to the owner with kind=None dropped out of the report: %r"
        % stale)
    assert stale[0]["age_h"] > 300.0


def test_owner_wait_marker_release_condition():
    """The second marker: "Release condition" without a kind is also an owner wait."""
    tasks = [_task("t_cond1", None)]
    events = {"t_cond1": NOW - 100 * H}
    reasons = {"t_cond1": (
        "Two budget burnouts in a row. Release condition: decompose the body "
        "into cards of smaller scope each.")}
    stale = metrics_evening.needs_input_stale(tasks, events, NOW,
                                              blocked_reasons=reasons)
    assert [s["id"] for s in stale] == ["t_cond1"]


def test_kindless_block_without_marker_stays_hidden():
    """kind=None without a sign of the owner (transient/CI) is not raised."""
    tasks = [_task("t_ff", None)]
    events = {"t_ff": NOW - 100 * H}
    reasons = {"t_ff": "rebase pending: CI on the new head is still running"}
    stale = metrics_evening.needs_input_stale(tasks, events, NOW,
                                              blocked_reasons=reasons)
    assert stale == []


def test_needs_input_kind_still_reported_without_reasons():
    """The old behaviour is kept: kind='needs_input' is caught without a reason."""
    tasks = [_task("t_ni", "needs_input")]
    events = {"t_ni": NOW - 50 * H}
    stale = metrics_evening.needs_input_stale(tasks, events, NOW)
    assert [s["id"] for s in stale] == ["t_ni"]


def test_dependency_kind_never_reported():
    """kind='dependency' is not an owner wait, even if the reason mentions
    the owner in another sense."""
    tasks = [_task("t_dep", "dependency")]
    events = {"t_dep": NOW - 100 * H}
    reasons = {"t_dep": "waiting for the owner of PR #123"}
    stale = metrics_evening.needs_input_stale(tasks, events, NOW,
                                              blocked_reasons=reasons)
    assert stale == []


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS %s" % name)
            except Exception as exc:  # noqa: BLE001 - red is red
                failures += 1
                print("FAIL %s: %s: %s" % (name, type(exc).__name__, exc))
    sys.exit(1 if failures else 0)
