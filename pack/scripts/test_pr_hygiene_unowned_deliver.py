#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pr-hygiene: a MERGEABLE PR without an owner card gets one.

Origin: the stale-rebase gate archived the rebase card whose own push made
the PR MERGEABLE. The review lives in the owner card's native review lane,
so with no card nobody requested the re-review of the new head, and the PR
of the active milestone sat still on an empty board with only a report
line.

Rules pinned here: an open non-draft MERGEABLE PR with no live card of any
rank, no accept verdict on the current head (review_state none or stale),
a head older than the grace period, admitted by the admission gate, gets
ONE owner card (key coder-pr<N>-deliver-<head7>). A live card, a fresh
head, an accept or reject verdict, a conflict or a draft keep the gate
silent; a refused admission gives only a report line; dry-run creates
nothing.

Red before green: in-test mutants of pr-hygiene.py (copies in a temp dir,
never a re-run of this file) must turn the battery red.

Run: python3 test_pr_hygiene_unowned_deliver.py
"""
import importlib.util
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.update({
    "HERMES_HOME": tempfile.mkdtemp(prefix="ph-unowned-"),
    "FACTORY_REPO": "acme/widget",
    "FACTORY_REPO_PATH": "/srv/hermes/widget",
    "FACTORY_BOARD": "main",
    "FACTORY_OWNER_LOGIN": "owner-login",
    "FACTORY_OWNER_CHAT": "telegram:000000000",
    "FACTORY_BASE_BRANCHES": "main",
})
os.environ.pop("FACTORY_DELIVER_GRACE_HOURS", None)
SCRIPT = os.environ.get("PR_HYGIENE_SCRIPT") or os.path.join(
    HERE, "pr-hygiene.py")

NOW = 1800000000
OLD = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 3 * 3600))
FRESH = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 30 * 60))
PAYLOAD = {
    101: {"headRefOid": "a24ff95b" * 5, "commits": [{"committedDate": OLD}]},
    102: {"headRefOid": "cafe0000" * 5, "commits": [{"committedDate": OLD}]},
    103: {"headRefOid": "abba0000" * 5, "commits": [{"committedDate": FRESH}]},
    104: {"headRefOid": "f00d0000" * 5, "commits": [{"committedDate": OLD}]},
    105: {"headRefOid": "cede0000" * 5, "commits": [{"committedDate": OLD}]},
}


def load(path, name="pr_hygiene_unowned"):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        return None
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def pr(number, mergeable="MERGEABLE", draft=False):
    return {number: {"isDraft": draft, "url": "https://example/%d" % number,
                     "detail": {"mergeable": mergeable}}}


def run(mod, info, by_pr=None, dry_run=False, admit=True, review="stale"):
    """Runs the gate with gh, board-watchdog and card creation stubbed."""
    created = []
    saved = (mod.create_deliver_card, mod.review_state, mod.pr_payload,
             mod.board_watchdog)

    class _Watchdog:
        gen_admission = staticmethod(lambda slug, n: (admit, "test admission"))
        svc_priority = staticmethod(lambda slug, n, d: 123)

    mod.create_deliver_card = (
        lambda n, url, head7, vs, prio:
        created.append((n, head7, vs, prio)) or (True, "created"))
    mod.review_state = lambda n: (review, "")
    mod.pr_payload = lambda n: PAYLOAD.get(n, {})
    mod.board_watchdog = _Watchdog
    try:
        findings = mod.unowned_deliver_gate(info, by_pr or {},
                                            dry_run=dry_run, now=NOW)
    finally:
        (mod.create_deliver_card, mod.review_state, mod.pr_payload,
         mod.board_watchdog) = saved
    return [f[0] for f in findings], created


def check_owner_card_created(mod):
    keys, created = run(mod, pr(101))
    assert created == [(101, "a24ff95", "stale", 123)], created
    assert keys == ["mergeflow-unowned-new:101"], keys
    keys, created = run(mod, pr(104), review="none")
    assert [c[0] for c in created] == [104], created


def check_live_owner_silences(mod):
    keys, created = run(mod, pr(101), by_pr={101: [("t_owner", "t", 1)]})
    assert not keys and not created, (keys, created)


def check_fresh_head_silent(mod):
    keys, created = run(mod, pr(103))
    assert not keys and not created, (keys, created)


def check_verdicts_silent(mod):
    for state in ("ok", "rejected"):
        keys, created = run(mod, pr(102), review=state)
        assert not keys and not created, (state, keys, created)


def check_conflict_and_draft_silent(mod):
    keys, created = run(mod, pr(105, mergeable="CONFLICTING"))
    assert not keys and not created, (keys, created)
    keys, created = run(mod, pr(105, draft=True))
    assert not keys and not created, (keys, created)


def check_admission_refusal_reports(mod):
    keys, created = run(mod, pr(101), admit=False)
    assert not created, created
    assert keys == ["mergeflow-unowned-noadmit:101"], keys


def check_dry_run(mod):
    keys, created = run(mod, pr(101), dry_run=True)
    assert not created, created
    assert keys == ["mergeflow-unowned-would:101"], keys


def check_key_and_grace(mod):
    assert mod.DELIVER_KEY_TMPL % (101, "a24ff95") \
        == "coder-pr101-deliver-a24ff95", mod.DELIVER_KEY_TMPL
    assert mod.DELIVER_GRACE_S == 2 * 3600, mod.DELIVER_GRACE_S


CHECKS = [check_owner_card_created, check_live_owner_silences,
          check_fresh_head_silent, check_verdicts_silent,
          check_conflict_and_draft_silent, check_admission_refusal_reports,
          check_dry_run, check_key_and_grace]


def battery(mod):
    failed = []
    for check in CHECKS:
        try:
            check(mod)
        except Exception as exc:  # noqa: BLE001
            failed.append("%s: %s" % (check.__name__, exc))
    return failed


# (name, old, new, checks that must go red)
MUTANTS = [
    ("live owner ignored",
     "        if by_pr.get(number):\n            continue",
     "        if False:\n            continue",
     {"check_live_owner_silences"}),
    ("grace period ignored",
     "        if now - head_ts < DELIVER_GRACE_S:",
     "        if False:",
     {"check_fresh_head_silent"}),
    ("accept verdict not respected",
     'if verdict_state not in ("none", "stale"):',
     'if verdict_state == "rejected":',
     {"check_verdicts_silent"}),
    ("admission ignored",
     "            if not admit:\n                findings.append((\n"
     "                    \"mergeflow-unowned-noadmit",
     "            if False:\n                findings.append((\n"
     "                    \"mergeflow-unowned-noadmit",
     {"check_admission_refusal_reports"}),
    ("dry-run creates",
     "                % (number, head7, verdict_state, key)))\n"
     "            continue\n",
     "                % (number, head7, verdict_state, key)))\n",
     {"check_dry_run"}),
    ("gate not wired into the run",
     "    found.extend(unowned_deliver_gate(info, by_pr, dry_run=l5_dry))",
     "    pass",
     {"check_wired"}),
]


def check_wired(src):
    assert "found.extend(unowned_deliver_gate(info, by_pr," in src


def run_mutants(src):
    tmp = tempfile.mkdtemp(prefix="ph-unowned-mut-")
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
        failed = {f.split(":")[0]
                  for f in battery(load(path, "pr_hygiene_unowned_m%d" % i))}
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
    print("PASS unowned PR deliver gate")
    return 0


if __name__ == "__main__":
    sys.exit(main())
