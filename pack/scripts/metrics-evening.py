#!/usr/bin/env python3
"""Evening pipeline report: one script instead of several.

Combined here: the spend report, the factory metrics, the pipeline quality
report, the waiting-discipline audit and the retro facts. The collectors
usage-ledger.py (ledger.jsonl) and reviewer-misses.py (misses journal plus
PASS log) stay separate: they have their own storage, and the evening report
catches up with them before printing.

Milestones are detected automatically (gh api milestones?state=open), with no
names in code; the cache cache/metrics-evening-milestones.json survives a gh
failure. Prices come from FACTORY_PRICES; a model without a price shows its
cost as unknown, never as zero or a guess. Money amounts and provider
balances are left out on purpose, and the only spend unit is plan credits.

Besides the spend and merge-path numbers, the report carries the PR path from
card to merge, rework per PR, escaped defects (FACTORY block), the shares of
timed_out/rate_limited/crashed outcomes (QUALITY block) and the mean context
per call, cache misses and compressions (CONTEXT block). Sections are
isolated: one failing section does not take down the others. The reason for
the custom credit accounting is in DEVIATIONS.md.
"""
from __future__ import annotations

import contextlib
import glob
import importlib.util
import io
import json
import os
import re
import sqlite3
import statistics
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from factory_config import cfg, cfg_list, hermes_home  # noqa: E402

# ==================== FACTORY (the former factory-metrics.py) ==========
# hermes_home() returns a string; the code below uses path division.
HOME = Path(hermes_home())
BOARDS = HOME / "kanban" / "boards"
SLUG = cfg("FACTORY_BOARD", "main")
# Fallback for direct calls and stubs; the live path is detect_milestones()
# below. Filled from settings (comma-separated milestone prefixes).
MILESTONES = tuple(cfg_list("FACTORY_MILESTONES", ()))
MILESTONE_CACHE = HOME / "cache" / "metrics-evening-milestones.json"
# Review bot logins: authors of the "## Review verdict" comment. Filled from
# settings.
REVIEWER_LOGINS = tuple(cfg_list("FACTORY_REVIEWER_LOGINS", ()))
# The owner: their CHANGES REQUESTED counts as rework.
OWNER_LOGIN = cfg("FACTORY_OWNER_LOGIN")
BUDGET_MARKER = "Iteration budget exhausted"
# One definition of "progress after burnout" for the metric and the watchdog:
# the watchdog's progress marker, or a substantive (non-system) comment by a
# worker or the owner posted AFTER the end of the run. Two diverging rules (a
# same-profile comment inside a fixed window after the run versus any
# non-system comment after the run) gave different answers for the same card.
# board-watchdog.py loads the definition from here.
BURN_PROGRESS_MARKER = "Watchdog: progress after burnout"
SYSTEM_COMMENT_RE = re.compile(r"^(BLOCKED:|Watchdog:|SCHEDULED:|UNBLOCKED:)")

# One metric for reviewer misses. Counting "PRs where the owner requested
# changes after the reviewer's verdict inside a window" has no tie to the
# commit sha and uses PRs as the unit, which gave a share several times higher
# than the journal. Now the numerator is the misses journal misses.md (kept by
# the watchdog: a pair "PASS on a sha -> owner CHANGES REQUESTED on the same or
# the next sha"), and the denominator is the reviewer's PASS verdicts from
# misses-passes.json, written by the same watchdog. A second way of counting
# is forbidden: the miss definition must be the same everywhere.
MISSES_JOURNAL = HOME / "shared-skills" / "adversarial-review" / "misses.md"
MISSES_PASSES = HOME / "shared-skills" / "adversarial-review" / "misses-passes.json"
MISSES_BASELINE = "2026-10-20"   # earlier CHANGES REQUESTED is prehistory, not in the metric
JOURNAL_ENTRY_RE = re.compile(r"(?m)^### #(\d+) ([0-9a-f]{7,40}) (\S+) (\S+)\s*$")
JOURNAL_PASS_RE = re.compile(r"(?m)^- verdict: PASS (\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})")
JOURNAL_CR_RE = re.compile(
    r"(?m)^- owner: CHANGES REQUESTED (?:\S+ )?(\d{4}-\d{2}-\d{2})")


def closes_burnout_progress(body) -> bool:
    """Whether a comment satisfies the need for progress after a burnout.

    Yes: the watchdog's own marker (it starts with "Watchdog:", but it is
    exactly the record of progress) or any substantive non-system comment by
    a worker or the owner. No: the machine prefixes
    BLOCKED:/Watchdog:/SCHEDULED:/UNBLOCKED:.
    """
    if (body or "").startswith(BURN_PROGRESS_MARKER):
        return True
    return not SYSTEM_COMMENT_RE.match(body or "")

def parse_prices(raw: str) -> dict:
    """FACTORY_PRICES "model=in/cache/out;model2=..." -> {model: (in, cache, out)}.

    Credits of a call = (input * in + cache_read * cache + output * out) / 1e4,
    in the unit of your plan. There is no built-in price list: a model that is
    not listed has an unknown cost.
    """
    prices = {}
    for item in (raw or "").split(";"):
        model, _, nums = item.strip().partition("=")
        if not model or not nums:
            continue
        parts = nums.split("/")
        if len(parts) != 3:
            sys.exit("FACTORY_PRICES: %r must be model=in/cache/out" % item.strip())
        prices[model.strip()] = tuple(float(x) for x in parts)
    return prices


PRICING = parse_prices(cfg("FACTORY_PRICES", ""))


def row_cost(r: dict) -> float | None:
    """Credits of one ledger row; None when the model has no price."""
    k = PRICING.get(r.get("model") or "?")
    if not k:
        return None
    return (r.get("input_tokens", 0) * k[0]
            + r.get("cache_read_tokens", 0) * k[1]
            + r.get("output_tokens", 0) * k[2]) / 1e4


def fmt_credits(value: float, unknown: int) -> str:
    """A credit amount that never hides unpriced runs behind a number."""
    if not unknown:
        return "%.1f" % value
    if not value:
        return "unknown (%d unpriced runs)" % unknown
    return "%.1f + unknown (%d unpriced runs)" % (value, unknown)


VERDICT_RE = re.compile(r"(?im)^#{1,4}\s*(?:re[- ]?)?review\s+verdict\b")
SHA_RE = re.compile(r"\b([0-9a-f]{7,40})\b")
GH_TIMEOUT = 120


def iso_to_epoch(value) -> float | None:
    from datetime import datetime
    try:
        return datetime.fromisoformat(
            (value or "").replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def gh_json(args: list[str]) -> list | dict | None:
    cmd = ["gh"] + args
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=GH_TIMEOUT)
        if out.returncode != 0:
            print(f"    [gh error] {' '.join(args[:3])}: {out.stderr.strip()[:120]}")
            return None
        return json.loads(out.stdout or "null")
    except (subprocess.TimeoutExpired, OSError, ValueError) as exc:
        print(f"    [gh fail] {' '.join(args[:3])}: {exc}")
        return None


def board_repo() -> str:
    meta = BOARDS / SLUG / "board.json"
    try:
        workdir = json.load(open(meta)).get("default_workdir")
        url = subprocess.run(["git", "-C", workdir, "remote", "get-url", "origin"],
                             capture_output=True, text=True, timeout=30)
        m = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?$", url.stdout.strip())
        if m:
            return m.group(1)
    except Exception:
        pass
    return cfg("FACTORY_REPO")


def detect_milestones(repo: str) -> tuple[str, ...]:
    """Open milestones of the repo: detected automatically, not by name.

    gh api repos/<repo>/milestones?state=open; the prefix is the part of the
    title before the first space. The cache in
    cache/metrics-evening-milestones.json survives a gh failure: yesterday's
    milestone list is better than an empty report.
    """
    data = gh_json(["api", "repos/%s/milestones?state=open" % repo])
    if isinstance(data, list) and data:
        titles = tuple(sorted({(m.get("title") or "").split()[0]
                               for m in data
                               if (m.get("title") or "").split()}))
        try:
            MILESTONE_CACHE.parent.mkdir(parents=True, exist_ok=True)
            MILESTONE_CACHE.write_text(json.dumps(list(titles)),
                                       encoding="utf-8")
        except OSError:
            pass
        if titles:
            return titles
    try:
        cached = tuple(json.loads(MILESTONE_CACHE.read_text(encoding="utf-8")))
        if cached:
            return cached
    except (OSError, ValueError):
        pass
    return MILESTONES


# ---------- pure metric functions (testable without network) ----------

def commit_dates(commits: list[dict]) -> dict[str, float]:
    out = {}
    for c in commits or []:
        t = iso_to_epoch(c.get("committedDate"))
        if t and c.get("oid"):
            out[c["oid"]] = t
    return out


def verdict_events(pr: dict, who: str | None = None) -> list[dict]:
    """Verdicts, split by author:

    - reviewer: a comment by the review bot with the heading "## Review
      verdict" (including Re-review) or a FORMAL review by the same login.
      Only these events feed metrics 1 (push->verdict) and 2 (verdict->merge):
      the owner's verdict must not be mixed in (an owner review taken as the
      first verdict made the push->verdict metric look many hours slower than
      the real reviewer verdict).
    - owner: a formal review by the owner (APPROVED/CHANGES_REQUESTED), the
      sha exact from review.commit.oid. Feeds metric 1b (reviewer verdict ->
      owner verdict): this is the owner's delay.

    who=None returns both kinds; who="reviewer"/"owner" filters.
    """
    events = []
    for c in pr.get("comments") or []:
        author = ((c.get("author") or {}).get("login") or "")
        body = c.get("body") or ""
        if author not in REVIEWER_LOGINS or not VERDICT_RE.search(body):
            continue
        t = iso_to_epoch(c.get("createdAt"))
        if not t:
            continue
        m = SHA_RE.search(body)
        sha = m.group(1) if m else (pr.get("headRefOid") or "")
        events.append({"pr": pr["number"], "sha": sha, "t": t,
                       "approx": not m, "who": "reviewer"})
    for r in pr.get("reviews") or []:
        author = ((r.get("author") or {}).get("login") or "")
        state = r.get("state") or ""
        if author == OWNER_LOGIN:
            if state not in ("APPROVED", "CHANGES_REQUESTED"):
                continue
        elif author in REVIEWER_LOGINS:
            if not VERDICT_RE.search(r.get("body") or ""):
                continue
        else:
            continue
        t = iso_to_epoch(r.get("submittedAt"))
        if not t:
            continue
        m = SHA_RE.search(r.get("body") or "")
        sha = ((r.get("commit") or {}).get("oid")
               or (m.group(1) if m else "")
               or (pr.get("headRefOid") or ""))
        events.append({"pr": pr["number"], "sha": sha, "t": t,
                       "approx": not sha,
                       "who": "owner" if author == OWNER_LOGIN else "reviewer"})
    seen, uniq = set(), []
    for e in events:  # one verdict may exist both as a comment and as a review
        if who and e["who"] != who:
            continue
        k = (e["who"], e["sha"][:7], int(e["t"]))
        if k in seen:
            continue
        seen.add(k)
        uniq.append(e)
    return uniq


def push_to_verdict(prs: list[dict], win_start: float, win_end: float) -> list[tuple]:
    """(hours, ref) for each REVIEWER verdict in the window:
    t_verdict - commit(sha). The owner's verdict does not feed the metric
    (metric 1 is the first reviewer verdict on a sha after it appeared).
    """
    first_by_sha: dict[tuple, tuple] = {}
    for pr in prs:
        cd = commit_dates(pr.get("commits"))
        for v in verdict_events(pr, who="reviewer"):
            if not (win_start <= v["t"] <= win_end):
                continue
            t_push = cd.get(v["sha"])
            if t_push is None and len(v["sha"]) >= 7:
                t_push = next((cd[k] for k in cd if k.startswith(v["sha"][:7])), None)
            if t_push is None:
                continue
            k = (v["pr"], v["sha"][:7])
            cand = (v["t"], t_push)
            if k not in first_by_sha or cand[0] < first_by_sha[k][0]:
                first_by_sha[k] = cand
    out = []
    for (num, sha7), (t_v, t_push) in first_by_sha.items():
        out.append((max(0.0, (t_v - t_push) / 3600.0), "PR#%d@%s" % (num, sha7)))
    return sorted(out, key=lambda x: -x[0])


def verdict_to_merge(pr: dict) -> tuple[float, str] | None:
    """(hours, ref): merge minus the FIRST REVIEWER verdict on the final head.

    The owner's verdict (for example an APPROVED a minute before the merge)
    does not feed the metric: it is measured separately as 1b.
    """
    if pr.get("state") != "MERGED" or not pr.get("mergedAt"):
        return None
    t_merge = iso_to_epoch(pr["mergedAt"])
    head = pr.get("headRefOid") or ""
    on_head = [v for v in verdict_events(pr, who="reviewer")
               if head and v["sha"][:7] and v["sha"][:7] == head[:7]]
    if not on_head:
        return None
    t_v = min(v["t"] for v in on_head)
    return (max(0.0, (t_merge - t_v) / 3600.0), "PR#%d@%s" % (pr["number"], head[:7]))


def verdict_to_owner(prs: list[dict], win_start: float,
                     win_end: float) -> list[tuple]:
    """1b. (hours, ref): owner verdict minus the FIRST reviewer verdict on a sha.

    The owner's delay on the same head: take the first reviewer verdict on a
    sha that falls into the window, and the nearest formal owner verdict AFTER
    it on the same sha. No owner verdict means the pair is not counted.
    """
    out = []
    for pr in prs:
        rev = [v for v in verdict_events(pr, who="reviewer")
               if win_start <= v["t"] <= win_end]
        if not rev:
            continue
        first: dict[tuple, dict] = {}
        for v in rev:
            k = (v["pr"], v["sha"][:7])
            if k not in first or v["t"] < first[k]["t"]:
                first[k] = v
        own = verdict_events(pr, who="owner")
        for (num, sha7), v in sorted(first.items()):
            after = [o["t"] for o in own
                     if o["sha"][:7] == sha7 and o["t"] > v["t"]]
            if not after:
                continue
            out.append(((min(after) - v["t"]) / 3600.0,
                        "PR#%d@%s" % (num, sha7)))
    return sorted(out, key=lambda x: -x[0])


def merged_without_verdict(pr: dict) -> str | None:
    """Ref of a PR merged without a REVIEWER verdict on the final head.

    An owner APPROVED without a reviewer verdict on the head counts as a
    reviewer miss.
    """
    if pr.get("state") != "MERGED":
        return None
    head = pr.get("headRefOid") or ""
    if not head:
        return None
    if any(v["sha"][:7] == head[:7]
           for v in verdict_events(pr, who="reviewer")):
        return None
    return "PR#%d@%s" % (pr["number"], head[:7])


def open_summary(prs: list[dict], now: float) -> dict:
    total = conflict = stale = drafts = 0
    conflict_refs, stale_refs = [], []
    for pr in prs:
        if pr.get("state") != "OPEN":
            continue
        if pr.get("isDraft"):
            drafts += 1
        total += 1
        if pr.get("mergeable") == "CONFLICTING":
            conflict += 1
            conflict_refs.append("PR#%d" % pr["number"])
        up = iso_to_epoch(pr.get("updatedAt"))
        if up and (now - up) > 48 * 3600:
            stale += 1
            stale_refs.append("PR#%d (%.0f h)" % (pr["number"], (now - up) / 3600))
    return {"total": total, "drafts": drafts, "conflict": conflict,
            "conflict_refs": conflict_refs, "stale": stale, "stale_refs": stale_refs}


def journal_misses(text: str,
                   baseline: str = MISSES_BASELINE) -> list[dict]:
    """Misses journal entries: [{pr, sha7, pass_ts, cr_date, klass}].

    The verdict date comes from the line "- verdict: PASS YYYY-MM-DD HH:MM"
    (machine-written by the watchdog), not from the entry heading: headings of
    backfilled entries were edited by hand while calibrating the classes, so
    their date can differ from the verdict. The verdict time is UTC, as gh
    returns it. Prehistory (a CHANGES REQUESTED before the baseline, a journal
    rule) is excluded from the metric.
    """
    out: list[dict] = []
    for block in re.split(r"(?m)(?=^### #)", text or ""):
        m = JOURNAL_ENTRY_RE.search(block)
        if not m:
            continue
        p = JOURNAL_PASS_RE.search(block)
        c = JOURNAL_CR_RE.search(block)
        if not p or not c:
            continue
        if c.group(1) < baseline:
            continue
        pass_ts = iso_to_epoch("%sT%s:00+00:00" % (p.group(1), p.group(2)))
        if pass_ts is None:
            continue
        out.append({"pr": int(m.group(1)), "sha7": m.group(2)[:7],
                    "pass_ts": pass_ts, "cr_date": c.group(1),
                    "klass": m.group(4)})
    return out


def miss_share(journal_text: str, passes_data: dict, win_start: float,
               win_end: float) -> dict:
    """The single reviewer-miss metric for a window: {n_miss, n_pass, refs}.

    n_miss: misses journal entries whose PASS date is inside the window
    (prehistory is already excluded in journal_misses). n_pass: the
    reviewer's PASS verdicts from the watchdog's PASS log in the same window.
    refs are sorted by verdict date.
    """
    jm = [m for m in journal_misses(journal_text)
          if win_start <= m["pass_ts"] <= win_end]
    refs = ["PR#%d@%s" % (m["pr"], m["sha7"])
            for m in sorted(jm, key=lambda m: m["pass_ts"])]
    return {"n_miss": len(jm), "n_pass": passes_in_window(
        passes_data, win_start, win_end), "refs": refs}


def passes_in_window(passes_data: dict, win_start: float,
                     win_end: float) -> int:
    """Reviewer PASS verdicts in the window, from the watchdog's misses-passes.json."""
    n = 0
    for p in (passes_data or {}).get("passes") or []:
        t = iso_to_epoch(p.get("ts"))
        if t is not None and win_start <= t <= win_end:
            n += 1
    return n


def burnouts(runs: list[dict], comments: list[dict],
             win_start: float, win_end: float) -> dict:
    """Runs with Iteration budget exhausted; progress comment by the single
    definition (closes_burnout_progress): the watchdog marker or a substantive
    non-system comment AFTER the end of the run, with no fixed time window and
    no requirement that the profile matches. It matches the watchdog's
    burnout_progress_scan word for word: a comment posted long after the end
    of the run counts as "with a comment" on both sides."""
    by_task: dict[str, list[dict]] = {}
    for c in comments:
        by_task.setdefault(c["task_id"], []).append(c)
    burned = []
    for r in runs:
        if BUDGET_MARKER not in (r.get("error") or ""):
            continue
        t_end = r.get("ended_at") or r.get("started_at") or 0
        if not (win_start <= t_end <= win_end):
            continue
        had = any(c["created_at"] >= t_end
                  and closes_burnout_progress(c.get("body"))
                  for c in by_task.get(r["task_id"], []))
        burned.append({"run": r["id"], "task": r["task_id"], "comment": had})
    return {"n": len(burned),
            "commented": sum(1 for b in burned if b["comment"]),
            "items": burned}


def credits(rows: list[dict], card_ids: set[str]) -> dict:
    """Plan credits per PR card; rows of models without a price are counted
    in "unknown", never as zero."""
    total, unknown = 0.0, 0
    per_model: dict[str, float] = {}
    for r in rows:
        if r.get("kind") != "card" or r.get("ref") not in card_ids:
            continue
        model = r.get("model") or "?"
        c = row_cost(r)
        if c is None:
            unknown += 1
            continue
        total += c
        per_model[model] = per_model.get(model, 0.0) + c
    return {"credits": total, "per_model": per_model, "unknown": unknown}


OWNER_WAIT_MARKERS = ("owner", "release condition", "awaiting owner decision")


def needs_input_stale(tasks: list[dict], blocked_events: dict[str, float],
                      now: float, max_age_h: float = 2.0,
                      blocked_reasons: dict[str, str] | None = None) -> list[dict]:
    """Blocked cards waiting for a human, older than max_age_h.

    Catches not only block_kind='needs_input' but also kind=None blocks whose
    reason in the last block event contains a sign of waiting for the owner
    (a card hung invisible for days because the worker called kanban block
    without --kind). kind='dependency'/'transient' are not raised.
    """
    blocked_reasons = blocked_reasons or {}
    out = []
    for t in tasks:
        if t.get("status") != "blocked":
            continue
        kind = (t.get("block_kind") or "").strip().lower()
        if kind == "needs_input":
            pass
        elif not kind:
            reason = (blocked_reasons.get(t["id"]) or "").lower()
            if not any(m in reason for m in OWNER_WAIT_MARKERS):
                continue
        else:
            continue
        since = blocked_events.get(t["id"]) or t.get("created_at") or now
        age_h = (now - since) / 3600.0
        if age_h > max_age_h:
            out.append({"id": t["id"], "age_h": age_h,
                        "title": (t.get("title") or "")[:48]})
    return sorted(out, key=lambda x: -x["age_h"])


# ---------- live data collection ----------

def gather(now: float) -> dict:
    repo = board_repo()
    milestones = detect_milestones(repo)
    prs_all = gh_json(["pr", "list", "-R", repo, "--state", "all", "--limit", "300",
                       "--json", "number,state,isDraft,milestone,updatedAt,"
                       "mergedAt,mergeable,headRefName"]) or []
    prs = [p for p in prs_all
           if (p.get("milestone") or {}).get("title", "").startswith(milestones)]
    detail_needed = [p["number"] for p in prs
                     if p.get("state") == "OPEN" and not p.get("isDraft")]
    merged_recent = [p["number"] for p in prs
                     if p.get("state") == "MERGED"
                     and (iso_to_epoch(p.get("mergedAt")) or 0) >= now - 7 * 86400]
    detail_needed += merged_recent
    for num in detail_needed:
        d = gh_json(["pr", "view", str(num), "-R", repo,
                     "--json", "headRefOid,mergeable,mergedAt,commits,reviews,comments"])
        if not d:
            continue
        for p in prs:
            if p["number"] == num:
                p.update(d)
                break
    # open PRs without details are not needed: metrics cover the open milestones of the window
    return {"repo": repo, "prs": prs, "milestones": milestones}


def gather_board() -> dict:
    db = BOARDS / SLUG / "kanban.db"
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    tasks = [dict(r) for r in con.execute(
        "SELECT id, title, body, status, assignee, branch_name, "
        "idempotency_key, block_kind, created_at FROM tasks")]
    runs = [dict(r) for r in con.execute(
        "SELECT id, task_id, profile, started_at, ended_at, error FROM task_runs")]
    comments = [dict(r) for r in con.execute(
        "SELECT task_id, author, created_at, body FROM task_comments")]
    blocked: dict[str, float] = {}
    blocked_reasons: dict[str, str] = {}
    for r in con.execute(
            "SELECT task_id, created_at, payload FROM task_events "
            "WHERE kind='blocked' ORDER BY id"):
        blocked[r["task_id"]] = r["created_at"]
        try:
            payload = json.loads(r["payload"]) if r["payload"] else {}
            blocked_reasons[r["task_id"]] = (payload or {}).get("reason") or ""
        except (TypeError, ValueError):
            blocked_reasons[r["task_id"]] = ""
    con.close()
    return {"tasks": tasks, "runs": runs, "comments": comments,
            "blocked": blocked, "blocked_reasons": blocked_reasons}


def gather_ledger() -> list[dict]:
    rows = []
    path = HOME / "usage-ledger" / "ledger.jsonl"
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return rows


def gather_misses() -> dict:
    """Misses journal plus the watchdog's PASS log (metric 4)."""
    try:
        journal = MISSES_JOURNAL.read_text(encoding="utf-8")
    except OSError:
        journal = ""
    try:
        passes = json.loads(MISSES_PASSES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        passes = {}
    return {"journal": journal, "passes": passes}


KEY_PR_RE = re.compile(
    r"^(?:reviewer-pr(\d+)|owner-review-#?(\d+)|rebase-(\d+)|coder-pr(\d+))",
    re.IGNORECASE)
TITLE_PR_RE = re.compile(r"#(\d+)\b")
DEFECT_RE = re.compile(r"(bug|defect|regress|breakage|crash|"
                       r"fails|not working)", re.IGNORECASE)


def assign_cards(board: dict, prs: list[dict],
                 milestones: tuple = MILESTONES) -> dict[str, dict[int, float]]:
    """Card -> {PR: weight}. One card = one PR.

    Carriers, strong to weak: the branch (branch_name == headRefName), the
    key (reviewer-pr<N>-, owner-review-#?<N>-, rebase-<N>-, coder-pr<N>-),
    a #N mention in the TITLE. The body does not count: a mention of another
    PR in the body must not pull a card into that PR (counting by body
    mentions made one PR collect a huge number of cards, and twin PRs counted
    shared cards twice).

    Exactly two PRs from DIFFERENT milestones is a twin pair: the pair's
    shared card is split in half. (Twin pairs only occur while two release
    lines are live; the rule stays so that already merged history is counted
    correctly.) With more than two PRs, or one line: the strongest carrier
    wins (branch > key > first #N in the title). A number from a key or
    title is valid only for a PR from the window.
    """
    by_number = {p["number"]: p for p in prs}
    ms_of = lambda p: (p.get("milestone") or {}).get("title") or ""
    out: dict[str, dict[int, float]] = {}
    for t in board["tasks"]:
        cand: dict[int, int] = {}          # PR -> carrier rank (lower is stronger)
        for p in prs:
            if t.get("branch_name") and t["branch_name"] == p.get("headRefName"):
                cand.setdefault(p["number"], 0)
        key = t.get("idempotency_key") or ""
        m = KEY_PR_RE.match(key)
        if m:
            num = int(next(g for g in m.groups() if g))
            if num in by_number:
                cand.setdefault(num, 1)
        for num_s in TITLE_PR_RE.findall(t.get("title") or ""):
            num = int(num_s)
            if num in by_number:
                cand.setdefault(num, 2)
        if not cand:
            continue
        nums = sorted(cand)
        if len(nums) == 2:
            a, b = ms_of(by_number[nums[0]]), ms_of(by_number[nums[1]])
            if (a.startswith(milestones) and b.startswith(milestones)
                    and a[:5] != b[:5]):
                out[t["id"]] = {nums[0]: 0.5, nums[1]: 0.5}   # twin pair
                continue
        best = min(nums, key=lambda n: (cand[n], n))          # a single PR
        out[t["id"]] = {best: 1.0}
    return out


def credits_weighted(rows: list[dict],
                     weights: dict[str, dict[int, float]]) -> dict[int, dict]:
    """Credits per PR with card weights (a twin pair gives half to each)."""
    per_pr: dict[int, dict] = {}
    for r in rows:
        if r.get("kind") != "card":
            continue
        w_map = weights.get(r.get("ref") or "")
        if not w_map:
            continue
        model = r.get("model") or "?"
        c = row_cost(r)
        for num, w in w_map.items():
            acc = per_pr.setdefault(num, {"credits": 0.0, "per_model": {},
                                          "unknown": 0})
            if c is None:
                acc["unknown"] += 1  # no price: unknown, not zero
                continue
            acc["credits"] += c * w
            if c:
                acc["per_model"][model] = acc["per_model"].get(model, 0.0) + c * w
    return per_pr


def ledger_totals(rows: list[dict]) -> dict:
    """Total over all ledger rows of the window: overall spend and its card part."""
    total = cards = 0.0
    unknown = 0
    for r in rows:
        c = row_cost(r)
        if c is None:
            unknown += 1
            continue
        total += c
        if r.get("kind") == "card":
            cards += c
    return {"all": total, "cards": cards, "unknown": unknown}


# ---------- report ----------

def fmt_hours(values: list[float], refs: list[str]) -> str:
    if not values:
        return "no events in the window"
    med = statistics.median(values)
    mx = max(values)
    shown = ", ".join(refs[:6]) + (", ..." if len(refs) > 6 else "")
    return "median %.1f h, max %.1f h (n=%d: %s)" % (med, mx, len(values), shown)


def holdout_section(now: float) -> str:
    """Status of the nightly run of hidden scenarios.

    The milestone in the section heading is filled in by the caller from the
    same detection (detect_milestones) as the rest of the report. Source:
    cache/holdout/latest.md under the Hermes home, written by the nightly
    agent of the tester profile (cron job holdout-nightly). Freshness is 36 h:
    a day plus slack for a shifted start. A failure to read must not take
    down the rest of the report.
    """
    path = HOME / "cache" / "holdout" / "latest.md"
    if not path.exists():
        return "no nightly run yet (latest.md is missing)"
    age_h = (now - path.stat().st_mtime) / 3600.0
    if age_h > 36:
        return "run is stale, %.0f h: see cache/holdout/ under the Hermes home" % age_h
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    lines = [l for l in text.splitlines() if l.strip()]
    head = "run from %s, %d lines" % (
        datetime.fromtimestamp(path.stat().st_mtime).strftime("%d.%m %H:%M"),
        len(lines))
    body = "\n".join(lines[:40])
    if len(lines) > 40:
        body += "\n... %d more lines: cache/holdout/latest.md" % (len(lines) - 40)
    return head + "\n" + body


def report(now: float | None = None, data: dict | None = None) -> str:
    now = now or time.time()
    if data is None:
        data = {"gh": gather(now), "board": gather_board(),
                "ledger": gather_ledger()}
    if "misses" not in data:
        data["misses"] = gather_misses()
    prs, board = data["gh"]["prs"], data["board"]
    # window milestones: auto-detected (gather/detect_milestones); for ready
    # data (tests) they are derived from the PRs themselves
    mss = data["gh"].get("milestones") or tuple(sorted({
        ((p.get("milestone") or {}).get("title") or "").split()[0]
        for p in prs
        if (p.get("milestone") or {}).get("title", "").split()}))
    t_by_id = {t["id"]: t for t in board["tasks"]}
    lines = ["=== FACTORY: metrics for %s, milestones %s ===" % (
        data["gh"]["repo"], " / ".join(mss))]
    for label, win in (("day (24 h)", 86400), ("rolling 7 days", 7 * 86400)):
        ws, we = now - win, now
        lines.append("--- window: %s ---" % label)
        for ms in mss:
            sub = [p for p in prs
                   if (p.get("milestone") or {}).get("title", "").startswith(ms)]
            # 1. push -> reviewer verdict
            ptv = push_to_verdict(sub, ws, we)
            lines.append("1. push->reviewer verdict [%s]: %s" % (ms, fmt_hours(
                [h for h, _ in ptv], [r for _, r in ptv])))
            # 1b. reviewer verdict -> owner verdict (the owner's delay)
            v2o = verdict_to_owner(sub, ws, we)
            lines.append("1b. reviewer verdict->owner verdict [%s]: %s" % (
                ms, fmt_hours([h for h, _ in v2o], [r for _, r in v2o])))
            # 2. verdict -> merge
            v2m = [verdict_to_merge(p) for p in sub
                   if p.get("mergedAt") and ws <= (iso_to_epoch(p["mergedAt"]) or 0) <= we]
            v2m = [x for x in v2m if x]
            lines.append("2. reviewer verdict->merge [%s]: %s" % (ms, fmt_hours(
                [h for h, _ in v2m], [r for _, r in v2m])))
            nov = [merged_without_verdict(p) for p in sub
                   if p.get("mergedAt") and ws <= (iso_to_epoch(p["mergedAt"]) or 0) <= we]
            nov = [x for x in nov if x]
            if nov:
                lines.append("   merged WITHOUT a reviewer verdict on the final head: %s"
                             % ", ".join(nov))
            # 3. open
            os_ = open_summary(sub, now)
            lines.append(
                "3. open [%s]: %d (draft %d), CONFLICTING %d%s, no activity "
                ">48 h: %d%s" % (
                    ms, os_["total"], os_["drafts"], os_["conflict"],
                    " (" + ", ".join(os_["conflict_refs"]) + ")" if os_["conflict_refs"] else "",
                    os_["stale"],
                    " (" + ", ".join(os_["stale_refs"]) + ")" if os_["stale_refs"] else ""))
        # 4. reviewer misses: the single miss metric, over the board as a
        #    whole, not per milestone: the watchdog and the journal do not
        #    know milestones
        ms4 = miss_share(data["misses"]["journal"], data["misses"]["passes"],
                         ws, we)
        jm_len, n_pass, refs_all = ms4["n_miss"], ms4["n_pass"], ms4["refs"]
        gen = (data["misses"]["passes"] or {}).get("generated") or ""
        gen_t = iso_to_epoch(gen)
        stale = ("; WARNING: PASS base is stale, %.0f h"
                 % ((now - gen_t) / 3600.0)
                 if gen_t and now - gen_t > 48 * 3600 else "")
        if not (data["misses"]["passes"] or {}).get("passes"):
            lines.append(
                "4. reviewer misses by journal: NO PASS BASE misses-passes.json"
                " (run scripts/reviewer-misses.py --since %s);"
                " misses in the journal for the window: %d" % (MISSES_BASELINE, jm_len))
        elif n_pass == 0:
            extra = ("; MISMATCH: there are misses, denominator is 0"
                     if jm_len else "")
            lines.append(
                "4. reviewer misses by journal: no PASS in the window"
                " (misses in the journal: %d%s)" % (jm_len, extra))
        else:
            shown = ", ".join(refs_all[:6]) + (", ..." if len(refs_all) > 6 else "")
            refs = (": " + shown) if refs_all else ""
            lines.append(
                "4. reviewer misses by journal: %.0f%% (%d of %d PASS%s)"
                "; watchdog PASS base from %s%s"
                % (100.0 * jm_len / n_pass, jm_len, n_pass, refs,
                   gen or "?", stale))
        # 5. burnouts (whole board)
        b = burnouts(board["runs"], board["comments"], ws, we)
        detail = "; ".join(
            "%s run=%d %s" % (x["task"], x["run"], "with comment" if x["comment"] else "WITHOUT comment")
            for x in b["items"][:8])
        lines.append("5. burnouts (%s): %d runs, with a progress comment %d%s"
                     % (BUDGET_MARKER, b["n"], b["commented"],
                        (": " + detail) if detail else ""))
        # 6. credits: one card = one PR (branch/key/title, the body does not
        #    count), the shared card of a twin pair is split in half
        merged_refs = [p for p in prs if p.get("state") == "MERGED"
                       and ws <= (iso_to_epoch(p.get("mergedAt")) or 0) <= we]
        rows_w = [r for r in data["ledger"] if ws <= (r.get("ts") or 0) <= we]
        weights = assign_cards(board, merged_refs, mss)
        per_pr = credits_weighted(rows_w, weights)
        for p in merged_refs:
            cr = per_pr.get(p["number"]) or {"credits": 0.0, "per_model": {},
                                             "unknown": 0}
            cards_txt = ", ".join(
                "%s%s" % (tid, " (½ with #%d)" % next(
                    n for n in w if n != p["number"]))
                          if len(w) == 2 else tid
                for tid, w in sorted(weights.items())
                if p["number"] in w) or "cards not matched"
            models_txt = ", ".join("%s %.1f" % (m, v)
                                   for m, v in sorted(cr["per_model"].items()))
            lines.append("6. PR#%d (%s): plan credits %s (%s)"
                         % (p["number"], cards_txt,
                            fmt_credits(cr["credits"], cr["unknown"]),
                            models_txt or "no tokens"))
        # 6b. card->merge path: from card creation to merge
        paths = []
        for p in merged_refs:
            t_merge = iso_to_epoch(p.get("mergedAt")) or 0
            for tid, w in weights.items():
                if p["number"] in w and t_by_id.get(tid, {}).get("created_at"):
                    paths.append(max(0.0, (t_merge - float(
                        t_by_id[tid]["created_at"])) / 3600.0))
        lines.append("6b. card->merge path: %s" % (
            "median %.1f h, max %.1f h (n=%d)"
            % (statistics.median(paths), max(paths), len(paths))
            if paths else "no matched cards"))
        # 6c. rework per PR: heads with a reviewer verdict minus the first
        #     submission; next to it the count of owner CHANGES REQUESTED
        reworks, owner_cr = [], 0
        for p in merged_refs:
            shas = {v["sha"][:7] for v in verdict_events(p, who="reviewer")
                    if v["sha"][:7]}
            reworks.append(max(0, len(shas) - 1))
            owner_cr += sum(1 for r in (p.get("reviews") or [])
                            if ((r.get("author") or {}).get("login")
                                == OWNER_LOGIN
                                and r.get("state") == "CHANGES_REQUESTED"))
        lines.append("6c. rework per PR: %s" % (
            "median %.1f per merged PR, owner-CR total %d"
            % (statistics.median(reworks), owner_cr) if reworks
            else "no merged PRs with verdicts"))
        # 6d. escaped defects: defect cards tied to the window's merged PRs
        merged_nums = {p["number"] for p in merged_refs}
        escaped = []
        for tid, w in weights.items():
            hit = [n for n in w if n in merged_nums]
            if not hit:
                continue
            t = t_by_id.get(tid) or {}
            if (ws <= (t.get("created_at") or 0) <= we
                    and DEFECT_RE.search(t.get("title") or "")):
                escaped.append("%s (PR#%s)" % (
                    tid, "/".join(str(n) for n in hit)))
        lines.append("6d. escaped defects: %s" % (
            "; ".join(escaped[:8]) if escaped else "none"))
        tot = ledger_totals(rows_w)
        by_pr = sum(v["credits"] for v in per_pr.values())
        check = ("OK" if by_pr <= tot["all"] + 1e-6
                 else "VIOLATED: sum over PRs exceeds the ledger")
        lines.append(
            "6. credits for the window in total: plan %s; "
            "merged PRs %.1f, cards outside PR windows %.1f; "
            "reconciliation of the sum with the ledger: %s"
            % (fmt_credits(tot["all"], tot["unknown"]), by_pr,
               max(0.0, tot["cards"] - by_pr), check))
        if not merged_refs:
            lines.append("6. no merged PRs in the window")
    # 7. needs_input (a snapshot, not a window)
    stale = needs_input_stale(board["tasks"], board["blocked"], now,
                              blocked_reasons=board.get("blocked_reasons"))
    if stale:
        items = "; ".join('%s %.1f h "%s"' % (s["id"], s["age_h"], s["title"])
                          for s in stale[:10])
        more = "" if len(stale) <= 10 else "; ... and %d more" % (len(stale) - 10)
        lines.append("7. waiting for the owner more than 2 h (needs_input + questions without kind): %d: %s%s"
                     % (len(stale), items, more))
    else:
        lines.append("7. waiting for the owner more than 2 h (needs_input + questions without kind): none")
    # 8. hidden acceptance scenarios: the nightly run of the tester;
    #    the milestone is the same as in the report header (auto-detected)
    ms8 = "/".join(mss)
    try:
        lines.append("8. holdout scenarios %s: " % ms8
                     + holdout_section(now or time.time()))
    except Exception as exc:  # noqa: BLE001 - the report matters more than a section
        lines.append("8. holdout scenarios %s: section failed: %s: %s"
                     % (ms8, type(exc).__name__, exc))
    lines.append("sources: gh pr view <N> --json comments/reviews/commits; "
                 "kanban.db (board %s) tasks/task_runs/task_comments; "
                 "usage-ledger/ledger.jsonl; misses journal misses.md + "
                 "misses-passes.json (watchdog reviewer-misses.py)" % SLUG)
    return "\n".join(lines)



# ==================== WAITING DISCIPLINE (the former wait-audit.py) ====
# Moved here whole; the separate script is gone.
PROFILES = ["coder", "reviewer", "tester", "architect", "ops"]
SLEEP_LIMIT = 30.0  # seconds: a longer sleep is forbidden as a way to wait
KANBAN_DB = os.path.join(HOME, "kanban", "boards", SLUG, "kanban.db")
REWORK_TITLE_RE = re.compile(
    r"rework|owner review|owner verdict|fix owner|owner changes"
    r"|changes requested", re.I)

SHELL_SLEEP_RE = re.compile(r"\bsleep\s+(\d+(?:\.\d+)?)")
PY_SLEEP_RE = re.compile(r"time\.sleep\(\s*(\d+(?:\.\d+)?)\s*\)")
# Invocations of the project's build tool: FACTORY_BUILD_CMD_RE. Inside a
# build, FACTORY_BUILD_FULL_RE marks a full verification run and
# FACTORY_BUILD_SCOPE_RE a run limited to some modules. Without
# FACTORY_BUILD_CMD_RE the build metrics are skipped and the report says so.


def _opt_re(key):
    raw = cfg(key, "")
    return re.compile(raw) if raw else None


BUILD_CMD_RE = _opt_re("FACTORY_BUILD_CMD_RE")
BUILD_FULL_RE = _opt_re("FACTORY_BUILD_FULL_RE")
BUILD_SCOPE_RE = _opt_re("FACTORY_BUILD_SCOPE_RE")
EDIT_CMD_RE = re.compile(r"git commit|git apply|git cherry-pick|sed -i|tee -a|>>\s*\S|cp\s+\S|mv\s+\S")
EDIT_TOOL_RE = re.compile(r"^(patch|write_file)$")


def cmd_of(name, args):
    try:
        a = json.loads(args or "{}")
    except (ValueError, TypeError):
        return ""
    if not isinstance(a, dict):
        return ""
    return a.get("command") or a.get("code") or ""


def bg_of(name, args):
    """Background launch: a terminal call with background=true or process_manage.

    The "& in the command" heuristic does not work: almost every command
    contains the "2>&1" redirect, while workers start a background build
    with the background=true flag.
    """
    if "process_manage" in (name or ""):
        return True
    try:
        a = json.loads(args or "{}")
    except (ValueError, TypeError):
        return False
    return isinstance(a, dict) and bool(a.get("background"))


def sleep_hits(cmd):
    """Seconds of blind sleep in one command: [values > limit]."""
    vals = [float(x) for x in SHELL_SLEEP_RE.findall(cmd or "")]
    vals += [float(x) for x in PY_SLEEP_RE.findall(cmd or "")]
    return [v for v in vals if v > SLEEP_LIMIT]


def build_class(cmd):
    """Class of a build command: full-verify / pl-verify / targeted / bare / None.

    The scope marker counts in any position: a full run with a module scope
    is a targeted run, not a full one. None: not a build, or no
    FACTORY_BUILD_CMD_RE.
    """
    if not BUILD_CMD_RE or not BUILD_CMD_RE.search(cmd or ""):
        return None
    scoped = bool(BUILD_SCOPE_RE and BUILD_SCOPE_RE.search(cmd))
    if BUILD_FULL_RE and BUILD_FULL_RE.search(cmd):
        return "pl-verify" if scoped else "full-verify"
    return "targeted" if scoped else "bare"


def is_edit(name, cmd):
    """Code edit: an editing tool or a terminal command that edits."""
    if EDIT_TOOL_RE.match(name or ""):
        return True
    return bool(EDIT_CMD_RE.search(cmd or ""))


def overlap_minutes(intervals, cross=True):
    """Minutes during which intervals overlap.

    cross=True: time when intervals of at least two DIFFERENT profiles are
    active (workers competing for the host's cores). intervals:
    [(t0, t1, profile)].
    cross=False: time when at least two intervals are active at all (inside
    one profile: a worker holds parallel builds itself).
    Sweep over events, integrating time under the condition.
    """
    ev = []
    for t0, t1, prof in intervals:
        if t1 > t0:
            ev.append((t0, 1, prof))
            ev.append((t1, -1, prof))
    ev.sort()
    active, n_active, total, last_t = {}, 0, 0.0, None
    for t, d, prof in ev:
        busy = len(active) >= 2 if cross else n_active >= 2
        if last_t is not None and busy:
            total += t - last_t
        n_active += d
        active[prof] = active.get(prof, 0) + d
        if active[prof] == 0:
            del active[prof]
        last_t = t
    return total / 60.0


def repeat_verifies(seq):
    """Full verifies in a row with no edit between them.

    seq: [(t0, kind)] for one session, kind in {verify, edit, other}.
    Only an edit resets the chain; reading logs between verifies does not
    justify a repeat.
    """
    reps, saw_any, edited = 0, False, False
    for _t, kind in sorted(seq):
        if kind == "edit":
            edited = True
        elif kind == "verify":
            if saw_any and not edited:
                reps += 1
            saw_any, edited = True, False
    return reps


def profile_stats(db, since, now):
    """Sleep violations, deduplicated tool time, wall time and builds of a profile."""
    empty = dict(viol=[], tool_t=0.0, wall=0.0, builds=[], bg_spawns=0,
                 repeats=0, waits=[])
    if not os.path.exists(db):
        return empty
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    con.row_factory = sqlite3.Row
    viol, tool_t, wall = [], 0.0, 0.0
    builds, bg_spawns, repeats, waits = [], 0, 0, []
    rows = con.execute(
        "select id, started_at, ended_at, last_activity_at from sessions"
        " where started_at > ? and started_at < ? and api_call_count > 5",
        (since, now)).fetchall()
    for s in rows:
        wall += float(s["ended_at"] or s["last_activity_at"]
                      or s["started_at"]) - float(s["started_at"])
        pend, seen = {}, set()
        seq = []
        msgs = con.execute(
            "select role, tool_calls, tool_call_id, timestamp from messages"
            " where session_id = ? order by id", (s["id"],)).fetchall()
        for m in msgs:
            t = float(m["timestamp"]) if m["timestamp"] else None
            if m["role"] == "assistant":
                for tc in json.loads(m["tool_calls"] or "[]"):
                    fn = tc.get("function", {})
                    if tc.get("id") not in seen:
                        pend[tc.get("id")] = (fn.get("name") or "",
                                              fn.get("arguments") or "", t)
            elif m["role"] == "tool" and m["tool_call_id"] in pend \
                    and m["tool_call_id"] not in seen:
                seen.add(m["tool_call_id"])
                name, args, t0 = pend.pop(m["tool_call_id"])
                if t0 is None:
                    continue
                t1 = t or t0
                tool_t += t1 - t0
                cmd = cmd_of(name, args)
                for sec in sleep_hits(cmd):
                    viol.append(dict(sec=sec, t0=t0,
                                     cmd=(cmd or "").replace("\n", " ")[:90]))
                cls = build_class(cmd)
                if cls:
                    if bg_of(name, args):
                        bg_spawns += 1
                    elif t1 - t0 > 20:
                        builds.append(dict(cls=cls, t0=t0, t1=t1))
                    seq.append((t0, "verify" if cls == "full-verify"
                                else "edit" if is_edit(name, cmd) else "other"))
                elif is_edit(name, cmd):
                    seq.append((t0, "edit"))
                if "process_manage" in (name or "") and t1 - t0 > 20:
                    waits.append((t0, t1))
        repeats += repeat_verifies(seq)
    con.close()
    return dict(viol=viol, tool_t=tool_t, wall=wall, builds=builds,
                bg_spawns=bg_spawns, repeats=repeats, waits=waits)


def rework_median(days=7):
    """Median duration of rework runs (task_runs of rework cards)."""
    if not os.path.exists(KANBAN_DB):
        return None
    since = time.time() - days * 86400
    con = sqlite3.connect("file:%s?mode=ro" % KANBAN_DB, uri=True)
    con.row_factory = sqlite3.Row
    durs = []
    try:
        rows = con.execute(
            "select t.title, r.started_at, r.ended_at from task_runs r"
            " join tasks t on t.id = r.task_id"
            " where r.started_at > ? and r.ended_at is not null",
            (since,)).fetchall()
    except sqlite3.DatabaseError:
        con.close()
        return None
    for r in rows:
        if REWORK_TITLE_RE.search(r["title"] or ""):
            durs.append(float(r["ended_at"]) - float(r["started_at"]))
    con.close()
    durs.sort()
    if not durs:
        return None
    n = len(durs)
    mid = n // 2
    return (durs[mid] if n % 2 else (durs[mid - 1] + durs[mid]) / 2,
            n)


def audit(hours=24.0):
    now = time.time()
    since = now - hours * 3600
    data = {p: profile_stats("%s/profiles/%s/state.db" % (HOME, p), since, now)
            for p in PROFILES}
    data["rework_median"] = rework_median(7)
    return data


def render(data, hours):
    out = ["⏱ Waiting discipline over %.0f h (wait-audit):" % hours]
    total_n = sum(len(d["viol"]) for d in data.values()
                  if isinstance(d, dict) and "viol" in d)
    total_sec = sum(v["sec"] for d in data.values()
                    if isinstance(d, dict) and "viol" in d
                    for v in d["viol"])
    if total_n == 0:
        out.append("blind sleep>%ds: 0, target held" % int(SLEEP_LIMIT))
    else:
        parts = []
        for prof in PROFILES:
            v = data[prof]["viol"]
            if v:
                parts.append("%s: %d (%.0f min)" % (
                    prof, len(v), sum(x["sec"] for x in v) / 60))
        out.append("blind sleep>%ds: %d, total %.1f h | %s" % (
            int(SLEEP_LIMIT), total_n, total_sec / 3600, ", ".join(parts)))
        worst = max((x for d in data.values() if isinstance(d, dict)
                     and "viol" in d for x in d["viol"]),
                    key=lambda x: x["sec"], default=None)
        if worst:
            out.append("worst: sleep %.0fs, %s | %r" % (
                worst["sec"], time.strftime("%d.%m %H:%M",
                                            time.localtime(worst["t0"])),
                worst["cmd"][:70]))
    c = data.get("coder") or dict(tool_t=0.0, wall=0.0)
    if c["wall"] > 0:
        out.append("coder waiting share: %.0f%% (tools %.1f h / wall"
                   " %.1f h, deduplicated; target <35%%)" % (
                       c["tool_t"] / c["wall"] * 100,
                       c["tool_t"] / 3600, c["wall"] / 3600))
    # Local builds
    cls_n = {}
    for prof in PROFILES:
        for b in data[prof]["builds"]:
            cls_n[b["cls"]] = cls_n.get(b["cls"], 0) + 1
    iv = [(b["t0"], b["t1"], prof) for prof in PROFILES
          for b in data[prof]["builds"]]
    bg = sum(data[p]["bg_spawns"] for p in PROFILES)
    reps = sum(data[p]["repeats"] for p in PROFILES)
    if BUILD_CMD_RE is None:
        out.append("local builds: skipped, FACTORY_BUILD_CMD_RE is not set")
    else:
        out.append("local builds: targeted %d, full verify %d, bare"
                   " builds without a scope %d, background builds %d, verify repeats"
                   " without edits %d" % (cls_n.get("targeted", 0) + cls_n.get("pl-verify", 0),
                                          cls_n.get("full-verify", 0), cls_n.get("bare", 0),
                                          bg, reps))
        out.append("concurrent builds across profiles: %.0f min in the window"
                   % overlap_minutes(iv))
    same = [(t0, t1, prof) for prof in PROFILES
            for t0, t1 in ([(b["t0"], b["t1"]) for b in data[prof]["builds"]]
                           + list(data[prof]["waits"]))]
    out.append("parallel builds inside a profile: %.0f min in the window,"
               " the norm is one background build per worker"
               % overlap_minutes(same, cross=False))
    rm = data.get("rework_median")
    if rm:
        out.append("median rework run (7 d): %.0f s over %d runs"
                   % (rm[0], rm[1]))
    return out


# ==================== PIPELINE QUALITY ================================
BOARD_DB = str(BOARDS / SLUG / "kanban.db")
DAY = 86400
BAD = ("crashed", "timed_out", "gave_up")


def q_db():
    return sqlite3.connect("file:%s?mode=ro" % BOARD_DB, uri=True, timeout=15)


def day_key(ts):
    return time.strftime("%d.%m", time.localtime(float(ts)))


def q_collect(days):
    since = time.time() - days * DAY
    con = q_db()
    runs = [(r[0], float(r[1]), float(r[2]) if r[2] else None, r[3], r[4] or "", r[5] or "")
            for r in con.execute(
                "select task_id,started_at,ended_at,outcome,profile,coalesce(error,'') from task_runs "
                "where started_at > ?", (since,))]
    tasks = {r[0]: {"title": r[1] or "", "status": r[2], "created": r[3],
                    "done": r[4], "fails": r[5] or 0}
             for r in con.execute(
                 "select id,title,status,created_at,completed_at,consecutive_failures from tasks")}
    comments = [(r[0], float(r[1]), r[2] or "") for r in con.execute(
        "select task_id,created_at,body from task_comments where created_at > ?", (since,))]
    con.close()
    return runs, tasks, comments


def skill_views(days):
    """How many times the accumulated skills were read over the period. Zero
    means the experience is being written into the void."""
    since = time.time() - days * DAY
    total = 0
    paths = [os.path.join(HOME, "skills", ".usage.json")]
    paths += glob.glob(os.path.join(HOME, "profiles", "*", "skills", ".usage.json"))
    for p in paths:
        if not os.path.exists(p):
            continue
        try:
            data = json.load(open(p))
        except Exception:
            continue
        for name, rec in data.items():
            stamp = rec.get("last_viewed_at") or rec.get("last_used_at") or ""
            if not stamp:
                continue
            try:
                seen = time.mktime(time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S"))
            except Exception:
                continue
            if seen > since:
                total += rec.get("view_count") or 0
    return total


def serena_share(days):
    """Share of code search done through serena rather than grep in the terminal."""
    since = time.time() - days * DAY
    search_cmd = re.compile(r"\b(grep|rg|sed -n|cat |head |tail |find )")
    # Build commands are not code search; without FACTORY_BUILD_CMD_RE they
    # are not told apart.
    build_cmd = BUILD_CMD_RE
    lsp = grep = 0
    for path in glob.glob(os.path.join(HOME, "profiles", "*", "state.db")):
        try:
            con = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=10)
            sids = [r[0] for r in con.execute(
                "select id from sessions where started_at > ?", (since,))]
            if not sids:
                continue
            marks = ",".join("?" * len(sids))
            lsp += list(con.execute(
                "select count(*) from messages where tool_name like ? "
                "and session_id in (" + marks + ")",
                ["mcp__serena__%"] + sids))[0][0]
            for (calls,) in con.execute(
                    "select tool_calls from messages where tool_calls is not null "
                    "and session_id in (%s)" % marks, sids):
                try:
                    parsed = json.loads(calls)
                except Exception:
                    continue
                for one in parsed:
                    fn = (one.get("function") or {}).get("name")
                    if fn != "terminal":
                        continue
                    args = str((one.get("function") or {}).get("arguments") or "").lower()
                    if (build_cmd and build_cmd.search(args)) or "git " in args or "gh " in args:
                        continue
                    if search_cmd.search(args):
                        grep += 1
            con.close()
        except Exception:
            continue
    return lsp, grep


def quality_report(days):
    """Quality of autonomous work: one yardstick, comparable across days."""
    runs, tasks, comments = q_collect(days)
    # scheduled is a run that has a worker assigned but has not finished yet:
    # it has no outcome. It does not count as "finished" (that would inflate
    # the picture), it is shown on a separate line and excluded from outcomes.
    scheduled = [r for r in runs if r[3] == "scheduled"]
    runs = [r for r in runs if r[3] != "scheduled"]
    finished = [r for r in runs if r[3]]
    killed = [r for r in finished if r[3] == "crashed" and "not alive" in (r[5] or "")]
    finished = [r for r in finished if r not in killed]
    if not finished:
        return "no runs in %d days" % days

    # review_requested is a normal completion: the work is handed to the
    # built-in review lane. Counting it as a failure would understate the
    # pipeline for using the standard mechanism.
    ok = [r for r in finished if r[3] in ("completed", "review_requested")]
    bad = [r for r in finished if r[3] in BAD]

    import collections
    per_card = collections.Counter(r[0] for r in finished)
    closed = [t for t in per_card if tasks.get(t, {}).get("status") in ("done", "archived")]
    first_pass = [t for t in closed if per_card[t] == 1]

    reviews = [t for t, v in tasks.items()
               if "plan review" in v["title"].lower() or "plan re-review" in v["title"].lower()]
    reworks = [t for t, v in tasks.items() if "Rework the decomposition" in v["title"]]
    fresh_reviews = [t for t in reviews if tasks[t]["created"] and float(tasks[t]["created"]) > time.time() - days * DAY]
    fresh_reworks = [t for t in reworks if tasks[t]["created"] and float(tasks[t]["created"]) > time.time() - days * DAY]

    escalations = [c for c in comments if "ESCALATION" in c[2]]

    lead = []
    for t in closed:
        v = tasks[t]
        if v["created"] and v["done"]:
            lead.append((float(v["done"]) - float(v["created"])) / 3600)
    lead.sort()

    durations = sorted((r[2] - r[1]) / 60 for r in finished if r[2])

    lines = ["=== pipeline quality over %d days ===" % days]
    lines.append("  runs finished           %d" % len(finished))
    if scheduled:
        lines.append("  scheduled (unfinished)  %d (worker assigned, no outcome yet; not counted in the lines above)" % len(scheduled))
    if killed:
        lines.append("  killed by restart       %d (not counted in the success rate)" % len(killed))
    lines.append("  successful              %d (%d%%)" % (len(ok), round(100 * len(ok) / len(finished))))
    lines.append("  failed                  %d (crashes %d, timeouts %d, gave up %d)" % (
        len(bad),
        len([r for r in bad if r[3] == "crashed"]),
        len([r for r in bad if r[3] == "timed_out"]),
        len([r for r in bad if r[3] == "gave_up"])))
    # All task_runs outcomes as they are, the sum equals the total (success
    # plus failure does not cover everything: blocked, dependency_wait,
    # changes_requested, superseded, rate_limited stay outside those lines).
    outcomes_all = collections.Counter(r[3] for r in runs if r[3])
    lines.append("  outcomes (all, as is)   %s (sum %d)" % (
        ", ".join("%s=%d" % (o, n) for o, n in outcomes_all.most_common()),
        sum(outcomes_all.values())))
    # Outcome shares of finished runs (rate_limited separately: it is the
    # provider quota running out, not the pipeline's fault).
    lines.append("  outcome shares          timed_out %d%%, rate_limited %d%%, "
                 "crashed %d%% (of finished)" % (
                     round(100 * len([r for r in finished if r[3] == "timed_out"]) / len(finished)),
                     round(100 * len([r for r in finished if r[3] == "rate_limited"]) / len(finished)),
                     round(100 * len([r for r in finished if r[3] == "crashed"]) / len(finished))))
    lines.append("  cards closed            %d" % len(closed))
    if closed:
        lines.append("  first pass              %d (%d%%)" % (
            len(first_pass), round(100 * len(first_pass) / len(closed))))
        lines.append("  runs per card           %.2f" % (len(finished) / len(closed)))
    if lead:
        lines.append("  card time, median       %.1f h (max %.1f h)" % (lead[len(lead) // 2], lead[-1]))
    if durations:
        lines.append("  run duration            median %.0f min, max %.0f min" % (
            durations[len(durations) // 2], durations[-1]))
    lines.append("  plan reviews created    %d, of which needed rework %d" % (
        len(fresh_reviews), len(fresh_reworks)))
    lines.append("  escalations to a human  %d" % len(escalations))
    lines.append("  reads of accumulated skills %d" % skill_views(days))
    lsp, grep = serena_share(days)
    if lsp + grep:
        lines.append("  code search through serena  %d of %d (%d%%), the rest is grep in the terminal"
                     % (lsp, lsp + grep, round(100 * lsp / (lsp + grep))))

    lines.append("")
    lines.append("  by day:")
    by_day = collections.defaultdict(lambda: [0, 0])
    for r in finished:
        d = by_day[day_key(r[1])]
        d[0] += 1
        if r[3] in ("completed", "review_requested"):
            d[1] += 1
    for d in sorted(by_day, key=lambda x: (x[3:], x[:2])):
        n, good = by_day[d]
        lines.append("    %s  runs %-3d success %d%%" % (d, n, round(100 * good / n)))

    lines.append("")
    lines.append("  by role:")
    by_prof = collections.defaultdict(lambda: [0, 0])
    for r in finished:
        p = by_prof[r[4] or "?"]
        p[0] += 1
        if r[3] in ("completed", "review_requested"):
            p[1] += 1
    for p in sorted(by_prof, key=lambda k: -by_prof[k][0]):
        n, good = by_prof[p]
        lines.append("    %-10s runs %-4d success %d%%" % (p, n, round(100 * good / n)))
    return "\n".join(lines)


# ==================== RETRO FACTS =====================================
# The retro agent and its cron are gone, the facts travel in the evening
# report. Only facts that no other section carries are kept here; the rest
# duplicated the watchdogs and the listing scripts.

def retro_section(now, hours=24.0):
    since = now - hours * 3600
    con = sqlite3.connect("file:%s?mode=ro" % KANBAN_DB, uri=True, timeout=15)
    runs = [(r[0], float(r[1]), float(r[2]) if r[2] else 0.0, r[3] or "?")
            for r in con.execute(
                "select task_id, started_at, ended_at, profile from task_runs "
                "where started_at > ? and ended_at is not null", (since,))]
    con.close()
    lines = ["=== retro facts over %.0f h ===" % hours]
    # Identical duration of repeated runs of one card and profile: the
    # signature of a limit or a timeout, not of a resource shortage.
    by_tp = {}
    for t, s, e, p in runs:
        by_tp.setdefault((t, p), []).append(e - s)
    twins = [(t, p, ds) for (t, p), ds in by_tp.items()
             if len(ds) >= 2 and max(ds) - min(ds) <= 60]
    for t, p, ds in sorted(twins)[:8]:
        lines.append("  identical duration: %s [%s] %d runs, "
                     "spread %.0f s" % (t, p, len(ds), max(ds) - min(ds)))
    if not twins:
        lines.append("  identical duration of repeated runs: none")
    # Cards with 3+ runs: looping
    cnt = {}
    for t, _s, _e, _p in runs:
        cnt[t] = cnt.get(t, 0) + 1
    looped = sorted((t for t, n in cnt.items() if n >= 3),
                    key=lambda t: -cnt[t])
    if looped:
        lines.append("  3+ runs: %s" % "; ".join(
            "%s (%d)" % (t, cnt[t]) for t in looped[:8]))
    else:
        lines.append("  cards with 3+ runs: none")
    return "\n".join(lines)


# ==================== SPEND ============================================
# Day and week boundaries: FACTORY_REPORT_TZ (an IANA zone), UTC when unset.
REPORT_TZ_NAME = cfg("FACTORY_REPORT_TZ", "") or "UTC"
REPORT_TZ = ZoneInfo(REPORT_TZ_NAME)


def report_day(ts):
    return datetime.fromtimestamp(ts, REPORT_TZ).strftime("%Y-%m-%d")


def week_start(ts):
    """Epoch of Monday 00:00 of the week of ts in the report zone."""
    d = datetime.fromtimestamp(ts, REPORT_TZ)
    d = (d - timedelta(days=d.weekday())).replace(hour=0, minute=0, second=0,
                                                  microsecond=0)
    return d.timestamp()


def board_titles():
    """t_id -> (status, title) from the live board; empty when unavailable."""
    titles = {}
    try:
        out = subprocess.run(
            ["hermes", "kanban", "--board", cfg("FACTORY_BOARD", "main"), "list"],
            capture_output=True, text=True, timeout=60).stdout
        for ln in out.splitlines():
            m = re.search(r"\b(t_[0-9a-f]{8})\b\s+(\S+)\s+(\S+)\s+(.*)", ln)
            if m:
                titles[m.group(1)] = (m.group(2), m.group(4).strip()[:70])
    except Exception:
        pass
    return titles


def ledger_row_credits(r) -> float | None:
    """Credits of a ledger row by FACTORY_PRICES; None = a model with no price.

    Dollars and provider balances are left out of the report: the unit of
    spend is credits by the price table only.
    """
    return row_cost(r)


def usage_section(now, titles=None):
    titles = titles if titles is not None else board_titles()
    week_start_ts = week_start(now)
    today = report_day(now)
    ledger = Path(HOME) / "usage-ledger" / "ledger.jsonl"

    from collections import defaultdict
    day_agg = [0, 0, 0, 0.0, 0]                 # in, out, cache, credits, unpriced
    week_agg = [0, 0, 0, 0.0, 0]
    cards_day = defaultdict(lambda: [0.0, 0, 0, 0])  # ref -> credits, in, out, unpriced
    cards_week = defaultdict(lambda: [0.0, 0])        # ref -> credits, unpriced
    kinds_day = defaultdict(lambda: [0.0, 0])
    models_day = defaultdict(lambda: [0.0, 0])
    if ledger.exists():
        for line in ledger.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            ts = r["ts"]
            cr = ledger_row_credits(r)
            # an unpriced row adds to the unknown count, never to the sum
            add, unk = (0.0, 1) if cr is None else (cr, 0)
            if ts >= week_start_ts:
                week_agg[0] += r["input_tokens"]
                week_agg[1] += r["output_tokens"]
                week_agg[2] += r.get("cache_read_tokens", 0)
                week_agg[3] += add
                week_agg[4] += unk
                if r["kind"] == "card":
                    cards_week[r["ref"]][0] += add
                    cards_week[r["ref"]][1] += unk
            if report_day(ts) == today:
                day_agg[0] += r["input_tokens"]
                day_agg[1] += r["output_tokens"]
                day_agg[2] += r.get("cache_read_tokens", 0)
                day_agg[3] += add
                day_agg[4] += unk
                for agg in (kinds_day[r["kind"]], models_day[r["model"]]):
                    agg[0] += add
                    agg[1] += unk
                if r["kind"] == "card":
                    c = cards_day[r["ref"]]
                    c[0] += add
                    c[1] += r["input_tokens"]
                    c[2] += r["output_tokens"]
                    c[3] += unk

    lines = ["=== Spend for %s (%s), provider credits ===" % (today, REPORT_TZ_NAME)]
    if not PRICING:
        lines.append("no prices: FACTORY_PRICES is not set, every cost is unknown")
    lines.append("in=%.1fM out=%.2fM cache=%.0fM credits=%s" % (
        day_agg[0] / 1e6, day_agg[1] / 1e6, day_agg[2] / 1e6,
        fmt_credits(day_agg[3], day_agg[4])))
    lines.append("by kind: " + ", ".join(
        "%s=%s" % (k, fmt_credits(*v)) for k, v in sorted(kinds_day.items(), key=lambda x: -x[1][0])))
    lines.append("by model (price table): " + ", ".join(
        "%s=%s" % (m, fmt_credits(*v)) for m, v in sorted(models_day.items(), key=lambda x: -x[1][0])[:5]))
    lines.append("")
    lines.append("=== Week from %s ===" % report_day(week_start_ts))
    lines.append("in=%.1fM out=%.2fM cache=%.0fM credits=%s" % (
        week_agg[0] / 1e6, week_agg[1] / 1e6, week_agg[2] / 1e6,
        fmt_credits(week_agg[3], week_agg[4])))
    lines.append("")
    lines.append("=== Top cards today ===")
    for ref, (c, i, o, u) in sorted(cards_day.items(), key=lambda x: -x[1][0])[:7]:
        st, tt = titles.get(ref, ("?", "?"))
        lines.append("%s [%s] %s cr (in=%.1fM out=%.2fM) %s"
                     % (ref, st, fmt_credits(c, u), i / 1e6, o / 1e6, tt))
    lines.append("")
    lines.append("=== Top cards this week ===")
    for ref, (c, u) in sorted(cards_week.items(), key=lambda x: -x[1][0])[:7]:
        st, tt = titles.get(ref, ("?", "?"))
        lines.append("%s [%s] %s cr %s" % (ref, st, fmt_credits(c, u), tt))
    return "\n".join(lines)


# ==================== CONTEXT: compactions, cache, average context =====
COMPACT_MARK = "[CONTEXT COMPACT"


def _ctx_profile(db_path, since):
    con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=10)
    try:
        row = con.execute(
            "select coalesce(sum(input_tokens),0), coalesce(sum(cache_read_tokens),0), "
            "coalesce(sum(api_call_count),0) from sessions "
            "where started_at > ? and coalesce(api_call_count,0) > 0",
            (since,)).fetchone()
        in_tok, cache_tok, calls = float(row[0]), float(row[1]), int(row[2] or 0)
        comp = con.execute(
            "select count(*) from messages m join sessions s on m.session_id = s.id "
            "where s.started_at > ? and m.content like ?",
            (since, "%" + COMPACT_MARK + "%")).fetchone()[0]
    finally:
        con.close()
    return in_tok, cache_tok, calls, int(comp)


def context_section(now, hours=24.0):
    since = now - hours * 3600
    dbs = [("default", str(Path(HOME) / "state.db"))]
    prof_dir = Path(HOME) / "profiles"
    if prof_dir.is_dir():
        for p in sorted(prof_dir.iterdir()):
            if (p / "state.db").exists():
                dbs.append((p.name, str(p / "state.db")))
    lines = ["=== context per call, cache misses, compactions (%.0f h) ===" % hours]
    for name, path in dbs:
        try:
            in_tok, cache_tok, calls, comp = _ctx_profile(path, since)
        except Exception as exc:
            lines.append("  %-9s unavailable: %s" % (name, exc))
            continue
        if not calls:
            lines.append("  %-9s no calls" % name)
            continue
        miss = 100.0 * in_tok / max(1.0, in_tok + cache_tok)
        lines.append("  %-9s calls %5d, context/call %7.0f tok, "
                     "cache misses %3.0f%%, compactions %d"
                     % (name, calls, (in_tok + cache_tok) / calls, miss, comp))
    return "\n".join(lines)


# ==================== EVENING REPORT ===================================
def _as_text(res):
    """The wait-audit render() returns a list of lines, not a string."""
    return "\n".join(res) if isinstance(res, list) else res


# --- MERGE READINESS: one evening report about PRs ----------------------
# Data and rules come from pr-hygiene.py: one understanding of the review
# verdict, green CI and the final summary comment. The file name has a
# hyphen, so it is loaded through importlib.
#
# CYCLE GUARD: pr-hygiene.py must NOT be loaded at module level. At module
# level pr-hygiene loads metrics-evening and board-watchdog, and this file
# loads pr-hygiene in the other direction: without a shared sys.modules key
# the chain metrics-evening -> pr-hygiene -> metrics-evening -> ... grows
# into endless mutual exec recursion and exhausts memory. Rules: (1) load
# lazily, on the first call of the section; (2) use one sys.modules key per
# file path, shared by all scripts, registered BEFORE exec: a re-entry gets
# the same object, not a fresh copy of the file.
# Sibling scripts are found next to this file, not through the Hermes home.
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_HY_PATH = os.path.abspath(Path(SCRIPTS_DIR) / "pr-hygiene.py")
_hy = None   # cache; tests replace it with a stub


def _hygiene():
    """pr-hygiene.py: the cache, a test substitute, or None (unavailable)."""
    global _hy
    if _hy is not None:
        return _hy
    key = "hermes_sibling:" + _HY_PATH
    mod = sys.modules.get(key)
    if mod is None:
        spec = importlib.util.spec_from_file_location("pr_hygiene_me", _HY_PATH)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[key] = mod          # before exec: breaks the mutual recursion
        try:
            spec.loader.exec_module(mod)
        except Exception:
            sys.modules.pop(key, None)
            return None
    _hy = mod
    return _hy
# Base branches the PRs target; a PR whose base is another branch is stacked.
_DEVELOP = tuple(cfg_list("FACTORY_BASE_BRANCHES", ("main",)))


def merge_failed_jobs(number):
    """Red jobs of a PR. CI and review rules come from pr-hygiene.py: one
    understanding of the verdict and of green CI for the whole layer."""
    hy = _hygiene()
    if hy is None:
        return []
    raw = hy.gh("pr", "view", str(number), "--json", "statusCheckRollup", timeout=60)
    if not raw:
        return []
    bad = ("FAILURE", "TIMED_OUT", "CANCELLED", "ERROR")
    return [c.get("name") or c.get("context") or "?"
            for c in json.loads(raw).get("statusCheckRollup") or []
            if (c.get("conclusion") or c.get("state")) in bad]


def merge_classify(prs):
    """{number: (class, order)}. There is a single line of base branches, so
    there is no twin class; stack = the base is not a configured base branch
    (the branch of another PR)."""
    out = {}
    for pr in prs:
        if pr["baseRefName"] not in _DEVELOP:
            out[pr["number"]] = ("stack", 2)
    for pr in prs:
        if pr["number"] not in out and pr["headRefName"] in {
                p["baseRefName"] for p in prs if p["baseRefName"] not in _DEVELOP}:
            out[pr["number"]] = ("stack", 1)
    for pr in prs:
        out.setdefault(pr["number"], ("single", 1))
    return out


def merge_readiness():
    """The "MERGE READINESS" section: one evening report about PRs.
    ready = green CI + reviewer verdict + mergeable + "What changes for
    you"; waiting = awaiting review or CI; red = failed CI."""
    hy = _hygiene()
    if hy is None:
        return "MERGE READINESS: pr-hygiene.py unavailable, section skipped"
    raw = hy.gh("pr", "list", "--state", "open", "--limit", "100", "--json",
                "number,title,baseRefName,headRefName,isDraft")
    prs = [p for p in (json.loads(raw) if raw else []) if not p.get("isDraft")]
    if not prs:
        return "MERGE READINESS: no open non-draft PRs"
    cls = merge_classify(prs)
    ready, waiting, red = [], [], []
    for pr in sorted(prs, key=lambda p: p["number"]):
        n = pr["number"]
        d = hy.detail(n)
        if d is None:
            continue
        line = '#%d "%s" (%s)' % (n, pr["title"], pr["baseRefName"])
        if d["failed"]:
            red.append("%s, failed: %s" % (line, ", ".join(merge_failed_jobs(n)) or "?"))
            continue
        state, why = hy.review_state(n)
        if state in ("none", "rejected", "stale"):
            waiting.append("%s%s" % (line, "" if state == "none" else " (%s)" % why))
            continue
        if d["pending"] or d["mergeable"] != "MERGEABLE" or not hy.has_user_summary(n):
            reasons = []
            if d["pending"]:
                reasons.append("CI still running")
            if d["mergeable"] != "MERGEABLE":
                reasons.append("mergeable=%s" % d["mergeable"])
            if not hy.has_user_summary(n):
                reasons.append('no "What changes for you"')
            waiting.append("%s (review accepted, but: %s)" % (line, "; ".join(reasons)))
            continue
        kind, order = cls[n]
        ready.append((kind, order, "%s, class: %s, order: %d" % (line, kind, order)))
    ready.sort(key=lambda r: (r[0], r[1]))
    out = ["✅ Ready to merge:"]
    out += ["- " + r[2] for r in ready] or ["- none"]
    out += ["", "⏳ Waiting for review:"]
    out += ["- " + w for w in waiting] or ["- none"]
    out += ["", "🔴 Red CI:"]
    out += ["- " + r for r in red] or ["- none"]
    return "\n".join(out)


def _digest_section():
    """Unread sections of the daily digest of the quiet crons."""
    sys.path.insert(0, str(Path(SCRIPTS_DIR)))
    from cron_quiet import digest_report
    return digest_report()


def evening(hours=24.0, now=None):
    """All sections in a row; a failure of one does not take down the others."""
    now = now or time.time()
    # Catch up the collectors: the ledger is idempotent, the reviewer
    # watchdog's PASS log must be fresh for the factory metric.
    for cmd in ([sys.executable, str(Path(SCRIPTS_DIR) / "usage-ledger.py")],
                [sys.executable, str(Path(SCRIPTS_DIR) / "reviewer-misses.py"),
                 "--since", MISSES_BASELINE]):
        try:
            subprocess.run(cmd, capture_output=True, timeout=600)
        except Exception:
            pass
    titles = board_titles()
    sections = [
        ("SPEND", lambda: usage_section(now, titles)),
        ("FACTORY", lambda: report(now)),
        ("MERGE READINESS", merge_readiness),
        ("QUALITY", lambda: quality_report(7)),
        ("WAITING", lambda: _as_text(render(audit(hours), hours))),
        ("CONTEXT", lambda: context_section(now, hours)),
        ("RETRO FACTS", lambda: retro_section(now, hours)),
        # Routine output of the quiet crons (board-watchdog, pr-hygiene,
        # pr-risk-labels, worktree-gc) goes into the evening report, not
        # into a chat push.
        ("DAILY DIGEST", _digest_section),
    ]
    out = []
    for name, fn in sections:
        try:
            text = fn()
            out.append(text if (text or "").strip() else "%s: empty" % name)
        except Exception as exc:  # noqa: BLE001 - the report matters more than a section
            out.append("%s: section failed: %s: %s"
                       % (name, type(exc).__name__, exc))
    return "\n\n".join(out)


def main() -> int:
    hours = float(sys.argv[1]) if len(sys.argv) > 1 else 24.0
    print(evening(hours))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
