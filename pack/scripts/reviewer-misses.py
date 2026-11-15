#!/usr/bin/env python3
"""Watchdog for reviewer misses.

A miss = a PASS/ACCEPTED verdict of the reviewer on head sha X, after which
the owner set CHANGES REQUESTED on the same sha or on the next one. Before
this watchdog existed, misses were recorded nowhere and the reviewer kept
repeating the same mistakes.

Mechanics:
- a reviewer verdict = a comment of the app account with the heading
  "## Review verdict" / "## Re-review verdict" and the head sha in the body;
  a formal APPROVED is impossible: the same App opens the PR;
- an owner verdict = a formal review by the owner with state
  CHANGES_REQUESTED and a commit_id;
- if the reviewer returned the PR between the PASS and the CR on the same
  shas, there is no miss: the last word before the CR belongs to the return;
- the pair window: the same sha or exactly the next one; wider is
  unprovable, a pair is not forced.

The verdict words are a contract with the agent prompts (see the
adversarial-review skill): the defaults below match it, and they can be
overridden with FACTORY_VERDICT_PASS_WORDS and FACTORY_VERDICT_RETURN_WORDS
(comma-separated, case-insensitive; a space in a word also matches an
underscore).

One journal: shared-skills/adversarial-review/misses.md (format in the
skill's references/misses-template.md). Deduplicated by PR + verdict sha.
Long dashes are scrubbed from generated lines: a house style rule.

PASS log: every live (not dry-run) scan overwrites misses-passes.json next to
the journal with all PASS verdicts of the scan. The journal holds only
misses, while the miss-rate metric needs a denominator: reviewer PASS
verdicts on heads per week. The watchdog writes it and the evening metrics
script reads it: counting the denominator a second way is forbidden, there is
one definition.

Modes:
  reviewer-misses.py --since 2026-10-20 [--dry-run] [--journal PATH]
      scans PRs with activity after the date (all states), finds pairs,
      appends new misses to the journal, prints the table of misses and the
      miss share per week (the metric: the goal is a fall two weeks in a
      row). --dry-run: leave the journal alone, print only.
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

from factory_config import cfg, cfg_list, hermes_home

HOME = hermes_home()
REPO = cfg("FACTORY_REPO")
OWNER = cfg("FACTORY_OWNER_LOGIN")
DEFAULT_JOURNAL = os.path.join(HOME, "shared-skills", "adversarial-review",
                               "misses.md")


def _words_re(words):
    """Whole-word, case-insensitive alternation; a space in a word also
    matches an underscore (CHANGES REQUESTED / CHANGES_REQUESTED)."""
    alts = ["[ _]".join(re.escape(part) for part in w.split()) for w in words]
    return re.compile(r"(?i)\b(?:%s)\b" % "|".join(alts))


# Verdict vocabulary: a contract with the reviewer prompts (adversarial-review
# skill: ACCEPTED/RETURN for the card verdict, PASS/CHANGES REQUESTED in the
# PR comment, APPROVED as the formal owner word). Override per installation.
PASS_WORDS = cfg_list("FACTORY_VERDICT_PASS_WORDS",
                      ("PASS", "APPROVED", "ACCEPTED"))
RETURN_WORDS = cfg_list("FACTORY_VERDICT_RETURN_WORDS",
                        ("CHANGES REQUESTED", "REQUEST CHANGES", "RETURN"))
# A bot echo of the owner's comment starts with this prefix; such a comment
# is a reprint of the owner's verdict, not a reviewer event.
OWNER_ECHO_PREFIX = cfg("FACTORY_OWNER_ECHO_PREFIX", "owner comment")

VERDICT_HEADER_RE = re.compile(r"(?im)^#{1,4}\s*(?:re[- ]?)?review\s+verdict\b")
PASS_RE = _words_re(PASS_WORDS)
RETURN_RE = _words_re(RETURN_WORDS)
SHA_RE = re.compile(r"(?i)\bhead\b[^\w]{0,24}?([0-9a-f]{7,40})")
SEV_RE = re.compile(
    r"CHANGES[ _]REQUESTED\s*:?\s*\*{0,2}\s*(critical|major|normal|minor|"
    r"trivial)", re.IGNORECASE)

# Miss classes and hints for the heuristic. The order of CLASSES is the
# tie-break order. "unclassified" = manual labelling.
CLASSES = ("contract", "mutation", "twin", "ui-input", "pr-texts")
# The "twin" class only appears in verdicts from the era of two release
# lines. The classifier reads history and keeps it; new verdicts do not need
# the class.
CLASS_HINTS = (
    ("contract", ("contract", "issue #", "acceptance", "criteria", "AC",
                  "requirement")),
    ("mutation", ("mutation", "mutant", "red/green", "kills no", "survive")),
    ("twin", ("twin", "ported", "1.x", "2.x")),
    ("ui-input", ("live", "viewport", "375", "click", "screenshot",
                  "probe", "ui", "input", "keyboard")),
    ("pr-texts", ("wcfy", "what changes", "pr body", "notice",
                  "description", "readme")),
)

ENTRY_RE = re.compile(r"(?m)^### #(\d+) ([0-9a-f]{7,40}) (\S+) (\S+)\s*$")


def _no_dash(text):
    return (text or "").replace("\u2014", "-").replace("\u2013", "-")


# ------------------------------------------------------- verdict parsing
def parse_verdict(body):
    """(kind, sha) from a reviewer verdict comment.

    kind: 'pass' | 'return' | None (not a verdict). The verdict word is the
    first occurrence before the head sha: PASS bodies sometimes mention
    earlier CHANGES REQUESTED, the first word is always the real one.
    """
    if not VERDICT_HEADER_RE.search(body or ""):
        return None, None
    body = body or ""
    sm = SHA_RE.search(body)
    region = body[:sm.start() + 80] if sm else body[:300]
    p, r = PASS_RE.search(region), RETURN_RE.search(region)
    if p and (not r or p.start() < r.start()):
        kind = "pass"
    elif r:
        kind = "return"
    else:
        kind = None
    return kind, (sm.group(1).lower() if sm else None)


def resolve_sha(sha, commits):
    """Prefix -> full sha; ambiguous or missing = None."""
    if not sha:
        return None
    if sha in commits:
        return sha
    hits = [c for c in commits if c.startswith(sha)]
    return hits[0] if len(hits) == 1 else None


def extract_events(comments, reviews, commits):
    """Reviewer verdicts and owner CRs from raw gh api responses.

    Bot echo comments "owner comment: ... ## Review verdict" are a reprint
    of the owner's verdict and not a reviewer event (otherwise the owner's
    APPROVED turns into a reviewer PASS and breeds phantom misses).
    """
    verdicts_raw = []
    for c in comments:
        body = c.get("body") or ""
        if (c.get("user") or {}).get("login") == OWNER:
            continue
        if body.lstrip().startswith(OWNER_ECHO_PREFIX):
            continue
        kind, sha = parse_verdict(body)
        if kind:
            verdicts_raw.append((c.get("created_at") or "", sha, kind,
                                 c.get("id")))
    crs = []
    for r in reviews:
        if ((r.get("user") or {}).get("login") != OWNER
                or r.get("state") != "CHANGES_REQUESTED"):
            continue
        cr_sha = (r.get("commit_id") or "").lower()
        if not cr_sha:
            # old reviews without commit_id: the head sha from the owner's body
            bm = SHA_RE.search(r.get("body") or "")
            if bm:
                cr_sha = bm.group(1).lower()
        crs.append((r.get("submitted_at") or "", cr_sha,
                    r.get("id"), r.get("body") or ""))
    # A rebase can remove the old head from the commit list (the CR and the
    # PASS lived on a sha that is no longer in the branch). Then the verdict
    # sha is resolved against the owner review's commit_id: full against
    # full, or prefix against full.
    extra = [c[1] for c in crs if c[1]]
    domain = sorted(set(commits) | set(extra))
    verdicts = [(ts, resolve_sha(sha, domain), kind, cid)
                for ts, sha, kind, cid in verdicts_raw]
    return verdicts, crs


def detect_misses(number, verdicts, crs, commits):
    """Pairs "PASS on X -> CR on X or on the next sha".

    The pair window: the same sha always (the CR sha equals the verdict sha
    even after a rebase), the next sha only by the order of the current
    commit list. A miss exists when the reviewer's last word before the CR
    on the window is a PASS. Returns (misses, unprovable CRs with a reason).
    """
    misses, unprovable = [], []
    for cr_ts, cr_sha, cr_id, body in sorted(crs):
        if not cr_sha:
            unprovable.append((number, cr_id, "CR without a recognised sha"))
            continue
        window = {cr_sha}
        if cr_sha in commits:
            idx = commits.index(cr_sha)
            if idx > 0:
                window.add(commits[idx - 1])
        prior = [v for v in verdicts if v[1] in window and v[0] < cr_ts]
        if not prior:
            continue
        latest = max(prior, key=lambda v: v[0])
        if latest[2] != "pass":
            continue
        if any(m["sha"] == latest[1] for m in misses):
            continue
        misses.append({"pr": number, "sha": latest[1], "pass_ts": latest[0],
                       "verdict_id": latest[3], "cr_ts": cr_ts,
                       "cr_id": cr_id, "cr_body": body})
    return misses, unprovable


# ------------------------------------------------------------- classification
def classify(text):
    """Heuristic miss class from the text of the owner's findings.

    The winner is the class with most hits, a tie is resolved by the order
    of CLASSES (contract beats all on equality). Zero hits = "unclassified":
    manual labelling, the script has no right to guess.
    """
    text = (text or "").lower()
    best, best_hits = "unclassified", 0
    for klass, hints in CLASS_HINTS:
        hits = 0
        for hint in hints:
            if len(hint) <= 4:
                hits += len(re.findall(
                    r"(?<![\w.])" + re.escape(hint) + r"(?![\w.])", text))
            else:
                hits += text.count(hint)
        if hits > best_hits:
            best, best_hits = klass, hits
    return best


# --------------------------------------------------------------- journal
def journal_keys(text):
    return {(int(m.group(1)), m.group(2)[:7]) for m in ENTRY_RE.finditer(text)}


def render_entry(m, klass, missed, source):
    sev_m = SEV_RE.search(m["cr_body"])
    sev = sev_m.group(1).lower() if sev_m else ""
    found_lines = [ln.strip() for ln in m["cr_body"].splitlines()
                   if ln.strip().startswith(("-", "*", "["))]
    found = " ".join(found_lines[:2])[:200] or m["cr_body"].splitlines()[0][:200]
    date = m["pass_ts"][:10]
    head = "### #%d %s %s %s" % (m["pr"], m["sha"][:7], date, klass)
    body = "\n".join([
        "- pr: %d" % m["pr"],
        "- sha: %s" % m["sha"][:7],
        "- verdict: PASS %s (issuecomment-%s)" % (
            m["pass_ts"][:16].replace("T", " "), m["verdict_id"]),
        "- owner: CHANGES REQUESTED %s%s (review-%s)" % (
            (sev + " ") if sev else "", m["cr_ts"][:16].replace("T", " "),
            m["cr_id"]),
        "- found: %s" % _no_dash(found),
        "- class: %s" % klass,
        "- missed: %s" % _no_dash(missed),
        "- source: %s" % source,
    ])
    return _no_dash(head) + "\n" + _no_dash(body)


def merge_journal(old_text, blocks):
    """Appends entry blocks to the journal, deduplicated by (pr, sha7).

    Returns (new text, number added). A block without an entry heading is a
    format error, not a silent skip.
    """
    have = journal_keys(old_text)
    fresh = []
    for b in blocks:
        m = ENTRY_RE.search(b)
        if not m:
            raise ValueError("block without an entry heading: %r" % b[:80])
        key = (int(m.group(1)), m.group(2)[:7])
        if key not in have:
            fresh.append(b)
            have.add(key)
    if not fresh:
        return old_text, 0
    text = old_text.rstrip("\n")
    marker = "(no entries yet: filled by the history review and then by the watchdog)"
    if marker in text:
        text = text.replace(marker, "").rstrip("\n")
    if fresh:
        sep = "\n\n" if text else ""
        text = text + sep + "\n\n".join(fresh) + "\n"
    return text, len(fresh)


# ---------------------------------------------------------------- metric
def week_key(ts):
    d = datetime.fromisoformat((ts or "").replace("Z", "+00:00"))
    iso = d.isocalendar()
    return "%04d-W%02d" % (iso[0], iso[1])


def weekly_shares(passes, misses):
    """passes: [(ts, ...)] all PASS verdicts; misses: the watchdog's dicts.

    Share = misses of the week / PASS verdicts of the week, the week of the
    verdict.
    """
    total, miss = {}, {}
    for v in passes:
        total[week_key(v[0])] = total.get(week_key(v[0]), 0) + 1
    for m in misses:
        miss[week_key(m["pass_ts"])] = miss.get(week_key(m["pass_ts"]), 0) + 1
    rows = []
    for wk in sorted(set(total) | set(miss)):
        t, mm = total.get(wk, 0), miss.get(wk, 0)
        share = 100.0 * mm / t if t else 0.0
        rows.append((wk, t, mm, share))
    return rows


# --------------------------------------------------------------- PASS log
def write_pass_log(all_passes, since, path):
    """misses-passes.json: all PASS verdicts of the scan, the metric denominator.

    Rewritten entirely, not appended: the scan rebuilds all pairs since
    --since anyway, a partial update would lie in the denominator (a PASS
    could vanish or appear a minute before the scan). The all_passes tuple:
    (ts, sha, kind, id, pr); sha is cut to 7, ts is written as gh gave it.
    Returns the number of records.
    """
    payload = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "since": since,
        "passes": [{"pr": p[4], "sha": (p[1] or "")[:7], "ts": p[0], "id": p[3]}
                   for p in sorted(all_passes, key=lambda p: (p[0] or "", p[4]))],
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    return len(payload["passes"])


# ------------------------------------------------------------------- gh
def gh_paginate(path):
    raw = subprocess.run(["gh", "api", "--paginate", path],
                         capture_output=True, text=True, timeout=180)
    if raw.returncode != 0:
        raise RuntimeError("gh api %s: %s" % (path, raw.stderr[:200]))
    items, dec, idx, buf = [], json.JSONDecoder(), 0, raw.stdout.strip()
    while idx < len(buf):
        obj, end = dec.raw_decode(buf, idx)
        items.extend(obj)
        idx = end
        while idx < len(buf) and buf[idx] in " \n\r\t":
            idx += 1
    return items


def scan(since, journal_path, dry_run=False, limit=None):
    listing = subprocess.run(
        ["gh", "pr", "list", "-R", REPO, "--state", "all", "--limit",
         str(limit or 300), "--search", "updated:>=%s" % since,
         "--json", "number"], capture_output=True, text=True, timeout=120)
    if listing.returncode != 0:
        raise RuntimeError("gh pr list: %s" % listing.stderr[:200])
    numbers = [p["number"] for p in json.loads(listing.stdout)]

    all_passes, all_misses, unprov = [], [], []
    for number in numbers:
        try:
            comments = gh_paginate("repos/%s/issues/%d/comments"
                                   % (REPO, number))
            reviews = gh_paginate("repos/%s/pulls/%d/reviews"
                                  % (REPO, number))
            commits = [c.get("sha") for c in gh_paginate(
                "repos/%s/pulls/%d/commits" % (REPO, number))]
        except RuntimeError as exc:
            print("  (PR #%d skipped: %s)" % (number, exc))
            continue
        verdicts, crs = extract_events(comments, reviews, commits)
        all_passes.extend((v[0], v[1], v[2], v[3], number)
                          for v in verdicts if v[2] == "pass")
        misses, up = detect_misses(number, verdicts, crs, commits)
        all_misses.extend(misses)
        unprov.extend(up)

    with open(journal_path, encoding="utf-8") as fh:
        journal = fh.read()
    have = journal_keys(journal)
    blocks, printed = [], []
    for m in all_misses:
        klass = classify(m["cr_body"])
        missed = ("PASS issuecomment-%s, then the owner's CHANGES REQUESTED "
                  "review-%s" % (m["verdict_id"], m["cr_id"]))
        block = render_entry(m, klass, missed,
                             "watchdog %s" % datetime.now(timezone.utc).date())
        printed.append((m, klass))
        if (m["pr"], m["sha"][:7]) not in have:
            blocks.append(block)
    if blocks and not dry_run:
        journal, added = merge_journal(journal, blocks)
        with open(journal_path, "w", encoding="utf-8") as fh:
            fh.write(journal)
        print("journal: added %d, there were %d" % (added, len(have)))
    elif blocks:
        print("dry-run: would add %d entries" % len(blocks))

    if not dry_run:
        pass_log = os.environ.get("MISSES_PASS_LOG") or os.path.join(
            os.path.dirname(os.path.abspath(journal_path)),
            "misses-passes.json")
        n_pass = write_pass_log(all_passes, since, pass_log)
        print("PASS log: %d verdicts -> %s" % (n_pass, pass_log))

    print("\n| PR | verdict sha | PASS | owner CR | class |")
    print("|---|---|---|---|---|")
    for m, klass in printed:
        print("| #%d | %s | %s | %s | %s |" % (
            m["pr"], m["sha"][:7], m["pass_ts"][:16].replace("T", " "),
            m["cr_ts"][:16].replace("T", " "), klass))
    print("\nMiss share per week (the week's PASS count = the denominator):")
    print("| week | PASS | misses | share |")
    print("|---|---|---|---|")
    for wk, t, mm, share in weekly_shares(all_passes, all_misses):
        print("| %s | %d | %d | %.0f%% |" % (wk, t, mm, share))
    for number, cr_id, why in unprov:
        print("unprovable: #%d review-%s: %s" % (number, cr_id, why))
    unclassified = [m["pr"] for m, k in printed if k == "unclassified"]
    if unclassified:
        print("ATTENTION: set the class by hand: PR %s"
              % ", ".join("#%d" % n for n in unclassified))
    return 0


def main():
    args = sys.argv[1:]
    if not args or args[0] != "--since" or len(args) < 2:
        print(__doc__)
        return 2
    since = args[1]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", since):
        print("date format is YYYY-MM-DD:", since)
        return 2
    journal = os.environ.get("MISSES_JOURNAL") or DEFAULT_JOURNAL
    if "--journal" in args:
        journal = args[args.index("--journal") + 1]
    return scan(since, journal, dry_run="--dry-run" in args,
                limit=int(args[args.index("--limit") + 1])
                if "--limit" in args else None)


if __name__ == "__main__":
    sys.exit(main())
