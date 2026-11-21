#!/usr/bin/env python3
"""The evening report reaches the owner's chat as ONE message.

Hermes cron delivery has no length setting and no attachments, and the
Telegram adapter splits long output: the 50-100 KB report arrived as about
ten messages every evening. metrics-evening.py therefore prints a digest
(one line per section) capped at TG_LIMIT characters by cap_message(), and
writes the full report to cache/metrics-evening/latest.md plus a dated copy;
the message carries the path.

Checks:
  D1 the digest of a full fixture fits TG_LIMIT;
  D2 it keeps the decision facts (spend, plan, quota, owner waits, ready to
     merge, conflicts, merge without verdict, quality) and the path;
  D3 it stays short: at most 16 non-empty lines;
  D4 a quiet day still yields a message with the path and "none";
  D5 an unknown format does not crash and still carries the path;
  C1 cap_message cuts any input to TG_LIMIT and keeps the path;
  C2 a short message passes unchanged;
  C3 digest plus cap on a huge report fits TG_LIMIT;
  F1 save_full_report writes latest.md and a dated copy, keeps REPORT_KEEP.
The checks run on the real script (all must pass) and then on mutants of the
cap (each must be killed by at least one check).

Run: python3 test_metrics_evening_one_message.py
"""
import os
import sys
import tempfile
import time
import types

os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="me-one-msg-home-")
os.environ.update({
    "FACTORY_REPO": "acme/widget",
    "FACTORY_REPO_PATH": "/srv/hermes/widget",
    "FACTORY_BOARD": "main",
    "FACTORY_OWNER_LOGIN": "owner-login",
    "FACTORY_OWNER_CHAT": "telegram:000000000",
})

HERE = os.path.dirname(os.path.abspath(__file__))
ME_PATH = os.path.join(HERE, "metrics-evening.py")
sys.path.insert(0, HERE)

MUTANTS = {
    # the cap is gone: a long digest goes to the chat as it is
    "no-truncation": (
        "    if len(msg) <= TG_LIMIT:\n        return msg\n",
        "    return msg\n"),
    # the limit is out of reach: the cap never fires
    "limit-disabled": (
        "TG_LIMIT = 3500\n",
        "TG_LIMIT = 10_000_000\n"),
}

PATH = "/srv/hermes/.hermes/cache/metrics-evening/latest.md"

FULL = """=== Spend for 2026-11-21 (UTC), provider credits (estimate) ===
in=52.7M out=3.14M cache=394M credits=2809.2
by kind: card=2700.0
by model (price table): model-a=2500.0

=== Week from 2026-11-16 ===
in=300.1M out=18.20M cache=2100M credits=130243.2
estimate vs plan weekly limit 140,000: 93%
quota errors in the logs (real events): today 0, this week 2

=== Top cards today ===
t_abc12345 [done] 500.0 credits (in=10.0M out=0.50M) Title

=== FACTORY: metrics of acme/widget ===
--- window: rolling 7 days ---
2. reviewer verdict->merge [v2]: median 4.0 h (n=2)
   merged WITHOUT a reviewer verdict on the final head: PR#411 8f2e05e, PR#412
3. open [v2]: 5 (draft 1), CONFLICTING 1 (PR#420), no activity >48 h: 2 (PR#401, PR#402)
5. burnouts (Iteration budget exhausted): 9 runs, with a progress comment 7
6d. escaped defects: t_9def8765 (PR#430)
7. waiting for the owner more than 2 h (needs_input + questions without kind): 2: t_36c2dd67 580.4 h "Gate for the alert of the watchdog"; t_7131f439 550.0 h "Implement the transport switch"

✅ Ready to merge:
- #415 "Fix reconnect" (main), class: single, order: 1
- #417 "Docs parity" (main), class: single, order: 1

⏳ Waiting for review:
- #420 "Stacked fix" (main)

🔴 Red CI:
- #418 "Flaky IT" (main), failed: verify

=== pipeline quality, 7 days ===
  runs finished           120
  successful              100 (83%)
  escalations to a human  1

blind sleep>30s: 2, total 0.1 h | coder: 2 (6 min)

  default   calls   410, context/call   52000 tok, cache misses  40%, compactions 3
  coder     calls   200, context/call   30000 tok, cache misses  10%, compactions 1

  3+ runs: t_aa11bbbb (4); t_cc33dddd (3)

[board-watchdog]
label repainted without a push
"""

QUIET = """in=1.0M out=0.10M cache=10M credits=100.0
in=5.0M out=0.50M cache=50M credits=500.0
quota errors in the logs (real events): today 0, this week 0
7. waiting for the owner more than 2 h (needs_input + questions without kind): none
"""


def load_module(src):
    mod = types.ModuleType("me_one_message")
    mod.__file__ = ME_PATH
    exec(compile(src, ME_PATH, "exec"), mod.__dict__)
    return mod


def checks(mod):
    fails = []

    def check(name, cond, detail=""):
        if not cond:
            fails.append("%s %s" % (name, detail))

    lim = mod.TG_LIMIT
    msg = mod.digest(FULL, PATH)
    check("D1", len(msg) <= lim, "digest %d > %d" % (len(msg), lim))
    for needle in ("2809.2", "130243.2", "93%", "quota errors: today 0, week 2",
                   "t_36c2dd67", "580h", "#415", "PR#420",
                   "merged without a verdict", "83%", PATH):
        check("D2", needle in msg, "lost %r" % needle)
    n = len([x for x in msg.splitlines() if x.strip()])
    check("D3", n <= 16, "digest grew to %d lines" % n)

    quiet = mod.digest(QUIET, PATH)
    check("D4", PATH in quiet and "waiting for you: none" in quiet
          and len(quiet) <= lim, quiet)
    junk = mod.digest("SPEND: section failed: X", PATH)
    check("D5", bool(junk) and PATH in junk, junk)

    # The cap is checked against the real limit of the pack: a mutant that
    # moves TG_LIMIT out of reach must not pass by grading itself.
    huge = "\n".join("anomaly %d %s" % (i, "x" * 200) for i in range(100))
    cut = mod.cap_message(huge, PATH)
    check("C1", len(cut) <= 3500 and PATH in cut,
          "cap let %d characters through" % len(cut))
    check("C2", mod.cap_message("short", PATH) == "short")
    big = FULL + "\n" + "\n".join(
        '- #4%03d "PR with a very long title %s" (main)' % (i, "y" * 150)
        for i in range(100))
    big = big.replace("✅ Ready to merge:\n",
                      "✅ Ready to merge:\n" + "\n".join(
                          "- #5%03d %s" % (i, "z" * 900) for i in range(3)) + "\n")
    whole = mod.cap_message(mod.digest(big, PATH), PATH)
    check("C3", len(whole) <= 3500, "pipeline gave %d" % len(whole))

    now = time.time()
    for days in range(mod.REPORT_KEEP + 5):
        old = mod.REPORT_DIR / time.strftime(
            "%Y-%m-%d.md", time.localtime(now - (days + 1) * 86400))
        old.parent.mkdir(parents=True, exist_ok=True)
        old.write_text("old", encoding="utf-8")
    latest = mod.save_full_report(FULL, now=now)
    dated = sorted(mod.REPORT_DIR.glob("????-??-??.md"))
    check("F1", latest.read_text(encoding="utf-8") == FULL
          and (mod.REPORT_DIR / time.strftime(
              "%Y-%m-%d.md", time.localtime(now))).read_text(
              encoding="utf-8") == FULL
          and len(dated) == mod.REPORT_KEEP,
          "latest/dated/kept=%d" % len(dated))
    return fails


def main():
    src = open(ME_PATH, encoding="utf-8").read()
    failed = 0
    for name, (old, _new) in MUTANTS.items():
        if src.count(old) != 1:
            print("FAIL: the anchor of mutant %s occurs %d times"
                  % (name, src.count(old)))
            failed += 1
    if failed:
        return 1

    fails = checks(load_module(src))
    if fails:
        for f in fails:
            print("FAIL %s" % f)
        return 1
    print("ok  real script: D1-D5, C1-C3, F1 pass")

    for name, (old, new) in MUTANTS.items():
        try:
            fails = checks(load_module(src.replace(old, new, 1)))
        except Exception as exc:
            fails = ["crashed: %s: %s" % (type(exc).__name__, exc)]
        if fails:
            print("ok  mutant %s killed (%s)" % (name, fails[0][:90]))
        else:
            print("FAIL mutant %s survived, the test is blind to it" % name)
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    rc = main()
    print("\nTOTAL metrics-evening-one-message: %s" % ("OK" if rc == 0 else "FAIL"))
    sys.exit(rc)
