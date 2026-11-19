#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""visual-delta only for UI issues.

Owner decision: the visual-delta rule (Before/After in the issue) applies
only to issues whose change is visible in the interface: a ui/ux/mobile
label or a [ux]/[ui] title prefix. Bugs in a proxy, CI, tests or
infrastructure fall outside it. A card removed by the owner is not
recreated: it is a question to the owner, not a new card.

Precedent: pr-hygiene created a [visual-delta] card for a proxy
serialization bug (no screenshot exists or ever will) after the owner had
removed the previous one and the issue got a milestone.

Checks:
  A. A non-UI issue (bug + hermes, active milestone, no section): neither a
     card nor a violation.
  B. A UI issue by the ui label: a card is created as before.
  C. UI by the hermes-ui label (suffix) and by a [ux] title prefix without a
     label: a card is created.
  D. A suppressed issue (FACTORY_VISUAL_DELTA_SUPPRESSED, even with a ui
     label and the active milestone): no card, the report has a "question to
     the owner" line.
  E. visual_delta_is_ui: a direct table of labels and titles.

Run: python3 test_pr_hygiene_visual_delta_ui_only.py
"""
import importlib.util
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.update({
    "HERMES_HOME": tempfile.mkdtemp(prefix="ph-vd-ui-"),
    "FACTORY_REPO": "acme/widget",
    "FACTORY_REPO_PATH": "/srv/hermes/widget",
    "FACTORY_BOARD": "main",
    "FACTORY_OWNER_LOGIN": "owner-login",
    "FACTORY_OWNER_CHAT": "telegram:000000000",
    "FACTORY_BASE_BRANCHES": "main",
    "FACTORY_VISUAL_DELTA_SUPPRESSED": "325",
})
PH_SRC = os.environ.get("PR_HYGIENE_SCRIPT") or os.path.join(
    HERE, "pr-hygiene.py")

ACTIVE = "2.3.0"
OPEN_MILESTONES = [{"title": ACTIVE, "due_on": "2026-11-29T00:00:00Z"},
                   {"title": "2.4.0", "due_on": "2026-12-20T00:00:00Z"}]
# Hermetic: milestone_bands.resolve writes a cache, keep it in a temp dir.
TMP = tempfile.mkdtemp(prefix="vd-ui-")


def _load():
    spec = importlib.util.spec_from_file_location("ph_vd_ui", PH_SRC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ph_vd_ui"] = mod
    spec.loader.exec_module(mod)
    if hasattr(mod, "milestone_bands"):
        mod.milestone_bands.CACHE_PATH = os.path.join(TMP, "ms-cache.json")
    return mod


def _issue(number, labels, title="t", body="text without a section",
           milestone=ACTIVE):
    return {"number": number, "title": title, "url": "u%d" % number,
            "labels": [{"name": l} for l in labels],
            "body": body,
            "milestone": {"title": milestone} if milestone else None}


def main():
    ph = _load()
    fails = []

    def check(name, fn):
        try:
            fn()
            print("ok   %s" % name)
        except AssertionError as e:
            fails.append(name)
            print("FAIL %s: %s" % (name, e))

    def run(issues):
        """visual_delta_check with the network calls replaced; cards go into
        the made list, returns (made, lines, numbers)."""
        if hasattr(ph, "_ms_open_milestones"):
            ph._ms_open_milestones = lambda repo: OPEN_MILESTONES
        made = []
        ph.visual_delta_card = lambda kind, number, bad, url: \
            made.append(number) or "t_vd%d" % number
        _ok, lines, _cards, numbers = ph.visual_delta_check(
            issues, dry_run=False)
        return made, lines, numbers

    def a_nonui_no_card():
        # bug + hermes, without ui; a body without a section; the active
        # milestone (the admission gate would have let a card through).
        made, _lines, numbers = run([
            _issue(400, ["bug", "hermes", "severity: major"],
                   title="proxy serializes concurrent tool calls")])
        assert made == [], \
            "a card for a non-UI issue was created: %s" % made
        assert numbers == [], \
            "a non-UI issue reached the violations: %s" % numbers
    check("A: non-UI issue, no card and no violation", a_nonui_no_card)

    def b_ui_label_card():
        made, _lines, _numbers = run([
            _issue(401, ["bug", "hermes", "ui", "severity: normal"]),
            _issue(402, ["bug", "hermes"],
                   title="[ux] Reconnect wedges with timeout"),
            _issue(403, ["enhancement", "hermes", "hermes-ui"])])
        assert made == [401, 402, 403], \
            "UI issues (ui label, [ux] prefix, hermes-ui suffix)" \
            " must give cards: %s" % made
    check("B/C: UI by label, prefix and suffix creates", b_ui_label_card)

    def d_suppressed_no_recreate():
        made, lines, _numbers = run([
            _issue(325, ["bug", "hermes", "ui"],
                   title="proxy serialization")])
        assert made == [], \
            "a card removed by the owner was recreated: %s" % made
        assert any("question to the owner" in ln and "#325" in ln
                   for ln in lines), \
            "no \"question to the owner\" line for #325: %s" % lines
    check("D: a suppressed issue is not recreated, question to the owner",
          d_suppressed_no_recreate)

    def e_is_ui_table():
        if not hasattr(ph, "visual_delta_is_ui"):
            raise AssertionError("no visual_delta_is_ui")
        cases = [
            (["bug", "hermes", "ui"], "t", True),
            (["hermes", "mobile"], "t", True),
            (["hermes", "ux"], "t", True),
            (["hermes-ui"], "t", True),
            (["hermes"], "[ux] bug", True),
            (["hermes"], "[UI] bug", True),
            (["bug", "hermes", "severity: normal"], "proxy bug", False),
            (["hermes", "uxor"], "t", False),      # uxor is not ux
            (["hermes"], "t without prefix", False),
            ([], None, False),
        ]
        for labels, title, want in cases:
            got = ph.visual_delta_is_ui(labels, title)
            assert got == want, \
                "is_ui(labels=%r, title=%r) = %r, expected %r" % (
                    labels, title, got, want)
    check("E: visual_delta_is_ui table", e_is_ui_table)

    if fails:
        print("\nRED: %d/%d checks failed: %s" % (len(fails), 4, fails))
        sys.exit(1)
    print("\nOK: visual-delta only for UI issues")


if __name__ == "__main__":
    main()
