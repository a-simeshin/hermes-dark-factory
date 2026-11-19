#!/usr/bin/env python3
"""Self-check of plan-review-gate.py (no hermes calls): the pure verdict
parser, and the native-or-live guard in create() (a live card with the same
key blocks creation; otherwise the receipt section lands in the body)."""
import importlib.util
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="prg-home-")
os.environ["FACTORY_OWNER_CHAT"] = "telegram:000000000"
for k in ("FACTORY_PLAN_ACCEPT_WORDS", "FACTORY_PLAN_RETURN_WORDS"):
    os.environ.pop(k, None)
sys.path.insert(0, HERE)
spec = importlib.util.spec_from_file_location("prg", os.path.join(HERE, "plan-review-gate.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

CASES = [
    ("ACCEPTED: plan is sound\nDEFERRED: none", "accept"),
    ("  **Accepted** with notes", "accept"),
    ("APPROVED", "accept"),
    ("RETURN: two holes", "return"),
    ("reject, the graph is cyclic", "return"),
    ("Verdict after long preamble... ACCEPTED", "accept"),   # fallback: first 200 chars
    ("Verdict after long preamble... RETURN", "return"),
    ("looks fine to me", "unknown"),
    ("", "unknown"),
    (None, "unknown"),
]
for text, want in CASES:
    got = mod.parse_verdict(text)
    assert got == want, (text, got, want)

# create(): the native-or-live guard runs before the kanban call.
calls = []


class _Done:
    stdout = "Created t_0000abcd"
    stderr = ""


def _fake_kb(*args, timeout=60):
    calls.append(args)
    return _Done()


mod.kb = _fake_kb
mod._live_keys = lambda: ["plan-review-t_1-c1"]
assert mod.create("Plan review x", "BODY", "reviewer", "plan-review-t_1-c1") is None
assert calls == [], calls
mod._live_keys = lambda: []
tid = mod.create("Plan review x", "BODY", "reviewer", "plan-review-t_2-c1")
assert tid == "t_0000abcd", tid
body = calls[0][calls[0].index("--body") + 1]
assert body.startswith("## Native guard") and body.endswith("BODY"), body
print("test_plan_review_gate: %d/%d passed" % (len(CASES) + 2, len(CASES) + 2))
