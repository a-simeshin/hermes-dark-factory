#!/usr/bin/env python3
"""Self-check of the pure verdict parser of plan-review-gate.py (no hermes calls)."""
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
print("test_plan_review_gate: %d/%d passed" % (len(CASES), len(CASES)))
