#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pr-hygiene: a PR delivered in conflict does not stay without an owner.

Origin: a rebase card closed with green CI on its head while merges into
the base had already turned the PR CONFLICTING. Checks are bound to the
head sha and stay green after the base moves, so the native acceptance let
them through. Two safety nets were missing:
- the merge watcher consumed the base head change while GitHub was still
  computing mergeable (UNKNOWN), recorded an empty conflict list and then
  stayed silent for good;
- nobody gave a CONFLICTING PR without a live card an owner on a recurrent
  basis (the unowned deliver gate deliberately ignores non-MERGEABLE PRs).

Rules pinned here:
- post_merge_watch does not consume a base head change while a PR of that
  base has mergeable None or UNKNOWN: the watch keeps the previous sha and
  the next run handles the change with the full conflict list; with every
  mergeable decided the event is consumed as before;
- conflicting_owner_gate gives an open CONFLICTING PR of a base line with
  no live card of any rank and no live rebase card ONE rebase card keyed by
  the current base head; MERGEABLE and UNKNOWN stay silent, a refused
  admission gives only a report line, dry-run creates nothing, and the gate
  runs from check() before post_merge_watch.

Red before green: in-test mutants of pr-hygiene.py (copies in a temp dir,
never a re-run of this file) must turn the battery red.

Run: python3 test_pr_hygiene_conflicting_owner.py
"""
import importlib.util
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.update({
    "HERMES_HOME": tempfile.mkdtemp(prefix="ph-conflict-owner-"),
    "FACTORY_REPO": "acme/widget",
    "FACTORY_REPO_PATH": "/srv/hermes/widget",
    "FACTORY_BOARD": "main",
    "FACTORY_OWNER_LOGIN": "owner-login",
    "FACTORY_OWNER_CHAT": "telegram:000000000",
    "FACTORY_BASE_BRANCHES": "develop/1.x,develop/2.x",
})
SCRIPT = os.environ.get("PR_HYGIENE_SCRIPT") or os.path.join(
    HERE, "pr-hygiene.py")

BASE = "develop/2.x"
OLD_SHA = "0" * 39 + "1"            # base head before the merges
NEW_SHA = "abcdef12" + "0" * 32     # base head after the merges


def load(path, name="pr_hygiene_conflict_owner"):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        return None
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def pr(number, mergeable, base=BASE):
    return {number: {"baseRefName": base, "url": "https://example/%d" % number,
                     "headRefName": "b%d" % number, "isDraft": False,
                     "detail": {"mergeable": mergeable}}}


def run(mod, fn, info, by_pr=None, dry_run=False, admit=True, live=None,
        watch=None):
    """Runs one gate with git, gh, the board and card creation stubbed.

    Returns (finding keys, created cards, total or None, watch after)."""
    created = []
    names = ("line_heads", "live_rebase_cards", "review_state",
             "create_rebase_card", "comment_card", "board_watchdog",
             "MERGE_WATCH", "DEBT_CACHE")
    saved = {n: getattr(mod, n) for n in names}

    class _Watchdog:
        gen_admission = staticmethod(lambda slug, n: (admit, "test admission"))
        svc_priority = staticmethod(lambda slug, n, d: 123)

    tmp = tempfile.mkdtemp(prefix="ph-conflict-owner-run-")
    mod.MERGE_WATCH = os.path.join(tmp, "merge-watch.json")
    mod.DEBT_CACHE = os.path.join(tmp, "debt.json")
    with open(mod.MERGE_WATCH, "w") as fh:
        json.dump(watch if watch is not None else {BASE: {"sha": OLD_SHA}}, fh)
    mod.line_heads = lambda: {BASE: NEW_SHA}
    mod.live_rebase_cards = lambda: dict(live or {})
    mod.review_state = lambda n: ("none", "")
    mod.create_rebase_card = (
        lambda n, base, sha7, parent, url, head_ref, prio:
        created.append((n, base, sha7)) or (True, "created"))
    mod.comment_card = lambda tid, text: True
    mod.board_watchdog = _Watchdog
    total = None
    try:
        if fn == "watch":
            findings, total = mod.post_merge_watch(info, {}, by_pr or {},
                                                   dry_run=dry_run)
        else:
            findings = mod.conflicting_owner_gate(info, by_pr or {},
                                                  dry_run=dry_run)
        with open(mod.MERGE_WATCH) as fh:
            after = json.load(fh)
    finally:
        for n, v in saved.items():
            setattr(mod, n, v)
    return [f[0] for f in findings], created, total, after


def check_unknown_defers_event(mod):
    keys, created, _, after = run(mod, "watch", pr(334, "UNKNOWN"))
    assert "mergeflow-merge-watch-defer:%s" % BASE in keys, keys
    assert after[BASE]["sha"] == OLD_SHA, after
    assert not created, created
    keys, created, _, after = run(mod, "watch", pr(334, None))
    assert after[BASE]["sha"] == OLD_SHA, ("null mergeable", after)


def check_decided_conflict_creates_card(mod):
    # The next run after the deferral: mergeable settled as CONFLICTING, the
    # head change is still pending, so the card targets the NEW head.
    keys, created, total, after = run(mod, "watch", pr(334, "CONFLICTING"))
    assert created == [(334, BASE, NEW_SHA[:7])], created
    assert total == 1, total
    assert after[BASE]["sha"] == NEW_SHA, after


def check_all_decided_consumes(mod):
    keys, created, total, after = run(mod, "watch", pr(500, "MERGEABLE"))
    assert after[BASE]["sha"] == NEW_SHA, after
    assert not created and total == 0, (created, total)
    assert not keys, keys


def check_bootstrap_not_deferred(mod):
    # No previous observation: nothing to keep, the watcher bootstraps.
    keys, created, _, after = run(mod, "watch", pr(334, "UNKNOWN"), watch={})
    assert after[BASE]["sha"] == NEW_SHA, after
    assert not any(k.startswith("mergeflow-merge-watch-defer") for k in keys)


def check_conflict_without_owner_gets_card(mod):
    keys, created, _, _ = run(mod, "gate", pr(334, "CONFLICTING"))
    assert created == [(334, BASE, NEW_SHA[:7])], created
    assert keys == ["mergeflow-conflicting-owner-new:334"], keys


def check_mergeable_and_unknown_silent(mod):
    info = dict(pr(341, "MERGEABLE"))
    info.update(pr(342, "UNKNOWN"))
    keys, created, _, _ = run(mod, "gate", info)
    assert not keys and not created, (keys, created)


def check_live_owner_silences(mod):
    keys, created, _, _ = run(mod, "gate", pr(334, "CONFLICTING"),
                              by_pr={334: [("t_owner", "t", 1)]})
    assert not keys and not created, (keys, created)
    live = {334: ("t_reb", "coder-pr334-rebase-%s" % NEW_SHA[:7], "ready")}
    keys, created, _, _ = run(mod, "gate", pr(334, "CONFLICTING"), live=live)
    assert not keys and not created, ("live rebase card", keys, created)


def check_foreign_base_silent(mod):
    keys, created, _, _ = run(mod, "gate",
                              pr(334, "CONFLICTING", base="feature/x"))
    assert not keys and not created, (keys, created)


def check_admission_refusal_reports(mod):
    keys, created, _, _ = run(mod, "gate", pr(334, "CONFLICTING"),
                              admit=False)
    assert not created, created
    assert keys == ["mergeflow-conflicting-owner-noadmit:334"], keys


def check_dry_run(mod):
    keys, created, _, _ = run(mod, "gate", pr(334, "CONFLICTING"),
                              dry_run=True)
    assert not created, created
    assert keys == ["mergeflow-conflicting-owner-would:334"], keys


CHECKS = [check_unknown_defers_event, check_decided_conflict_creates_card,
          check_all_decided_consumes, check_bootstrap_not_deferred,
          check_conflict_without_owner_gets_card,
          check_mergeable_and_unknown_silent, check_live_owner_silences,
          check_foreign_base_silent, check_admission_refusal_reports,
          check_dry_run]


def battery(mod):
    failed = []
    for check in CHECKS:
        try:
            check(mod)
        except Exception as exc:  # noqa: BLE001
            failed.append("%s: %s" % (check.__name__, exc))
    return failed


WIRE = ("    found.extend(conflicting_owner_gate(info, by_pr, dry_run=l5_dry,\n"
        "                                        ancestors=ancestors))\n"
        "    l5_findings, debt_count = post_merge_watch(")

# (name, old, new, checks that must go red)
MUTANTS = [
    ("no-defer: head change consumed while mergeable is undecided",
     'if undecided and (prev.get(base) or {}).get("sha"):',
     'if False and undecided and (prev.get(base) or {}).get("sha"):',
     {"check_unknown_defers_event"}),
    ("any-mergeable: gate fires on every PR",
     '        if (p.get("detail") or {}).get("mergeable") != "CONFLICTING":\n'
     '            continue\n'
     '        if number in live or by_pr.get(number):',
     '        if number in live or by_pr.get(number):',
     {"check_mergeable_and_unknown_silent"}),
    ("live owner ignored",
     "        if number in live or by_pr.get(number):\n"
     "            continue                      # an owner is alive",
     "        if False:\n"
     "            continue                      # an owner is alive",
     {"check_live_owner_silences"}),
    ("admission ignored",
     "            if not admit:\n                findings.append((\n"
     "                    \"mergeflow-conflicting-owner-noadmit",
     "            if False:\n                findings.append((\n"
     "                    \"mergeflow-conflicting-owner-noadmit",
     {"check_admission_refusal_reports"}),
    ("gate not wired before post_merge_watch",
     WIRE,
     "    l5_findings, debt_count = post_merge_watch(",
     {"check_wired"}),
]


def check_wired(src):
    assert src.count(WIRE) == 1, "gate is not called right before" \
        " post_merge_watch in check()"


def run_mutants(src):
    tmp = tempfile.mkdtemp(prefix="ph-conflict-owner-mut-")
    for i, (name, old, new, must_fail) in enumerate(MUTANTS):
        assert src.count(old) == 1, "mutant anchor drifted: %s" % name
        mutated = src.replace(old, new)
        if must_fail == {"check_wired"}:
            try:
                check_wired(mutated)
            except AssertionError:
                print("OK mutant red: %s (check_wired)" % name)
                continue
            raise AssertionError("mutant %r stayed green" % name)
        path = os.path.join(tmp, "pr_hygiene_mut_%d.py" % i)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(mutated)
        failed = {f.split(":")[0] for f in battery(
            load(path, "pr_hygiene_conflict_owner_m%d" % i))}
        missing = must_fail - failed
        assert not missing, "mutant %r stayed green on %s" % (name, missing)
        print("OK mutant red: %s (%s)" % (name, ", ".join(sorted(failed))))


def main():
    mod = load(SCRIPT)
    if mod is None:
        print("SKIP: cannot load pr-hygiene.py")
        return 0
    with open(SCRIPT, encoding="utf-8") as fh:
        src = fh.read()
    failed = battery(mod)
    try:
        check_wired(src)
    except AssertionError:
        failed.append("check_wired: gate is not called from check()")
    for f in failed:
        print("FAIL", f)
    if failed:
        return 1
    print("OK %d checks green on pr-hygiene.py" % (len(CHECKS) + 1))
    run_mutants(src)
    print("PASS conflicting PR owner and merge-watch deferral")
    return 0


if __name__ == "__main__":
    sys.exit(main())
