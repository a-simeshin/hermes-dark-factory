#!/usr/bin/env python3
"""pr-hygiene: a claim about external state without an artifact.

Origin: a worker wrote "E2E flaked (SomePaneUiIT, known)" without any issue
(the issue appeared later, after the owner asked). The detector
external_claim_untracked_hits must fire on known/duplicated/already-fixed
phrases without #N, t_xxxxxxxx or a URL nearby, and stay silent when a
reference is there or the claim is about the worker's own work.

Run: python3 test_pr_hygiene_extclaim.py
"""
import importlib.util
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.update({
    "HERMES_HOME": tempfile.mkdtemp(prefix="ph-extclaim-"),
    "FACTORY_REPO": "acme/widget",
    "FACTORY_REPO_PATH": "/srv/hermes/widget",
    "FACTORY_BOARD": "main",
    "FACTORY_OWNER_LOGIN": "owner-login",
    "FACTORY_OWNER_CHAT": "telegram:000000000",
    "FACTORY_BASE_BRANCHES": "main",
})
SCRIPT = os.environ.get("PR_HYGIENE_SCRIPT") or os.path.join(
    HERE, "pr-hygiene.py")
spec = importlib.util.spec_from_file_location("pr_hygiene_extclaim", SCRIPT)
if spec is None or spec.loader is None:
    print("SKIP: cannot load pr-hygiene.py")
    sys.exit(0)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# The class of the original case: "known" without an artifact.
BODY_KNOWN_FLAKE = """CI leg went red on demo E2E webflux; this is a
known flake of the Resources pane race, rerun is green.
"""

# "Duplicated" without a number.
BODY_DUP_NO_REF = """The pane race red is duplicated in the proxy PR,
nothing to do here.
"""

# A reference cancels: a number in the neighbouring line.
BODY_KNOWN_WITH_ISSUE = """CI leg red on demo E2E webflux; this is a
known flake tracked in #385, rerun green.
"""

# A card reference cancels.
BODY_DUP_WITH_CARD = """Duplicated in PR by t_5d79c790, see the card.
"""

# Own work, not external state: silent.
BODY_OWN_WORK = """Fixed the flaky guard in this PR and merged the
helper into the harness.
"""

# "Already fixed" about someone else's work without a reference: fires.
BODY_ALREADY_FIXED = """The timeline leak was already fixed upstream,
no change needed here.
"""


def test_known_flake_fires():
    hits = mod.external_claim_untracked_hits(BODY_KNOWN_FLAKE)
    assert any("known flake" in h.lower() for h in hits), hits


def test_duplicated_fires():
    hits = mod.external_claim_untracked_hits(BODY_DUP_NO_REF)
    assert hits, hits


def test_issue_number_silences():
    assert mod.external_claim_untracked_hits(BODY_KNOWN_WITH_ISSUE) == []


def test_card_ref_silences():
    assert mod.external_claim_untracked_hits(BODY_DUP_WITH_CARD) == []


def test_own_work_silences():
    assert mod.external_claim_untracked_hits(BODY_OWN_WORK) == []


def test_already_fixed_fires():
    hits = mod.external_claim_untracked_hits(BODY_ALREADY_FIXED)
    assert any("already fixed" in h.lower() for h in hits), hits


def test_empty_body():
    assert mod.external_claim_untracked_hits(None) == []
    assert mod.external_claim_untracked_hits("") == []


if __name__ == "__main__":
    test_known_flake_fires()
    test_duplicated_fires()
    test_issue_number_silences()
    test_card_ref_silences()
    test_own_work_silences()
    test_already_fixed_fires()
    test_empty_body()
    print("OK: external_claim_untracked_hits")
