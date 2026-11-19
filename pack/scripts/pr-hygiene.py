#!/usr/bin/env python3
"""Open-PR hygiene: what can be merged today and what stands in the way.

Scheduled once a day (--no-agent); silent when there are no open PRs.

The checks exist because a review of a backlog of 13 PRs produced five
findings, and four of them are computed by a git command, not by reading code:
  * `mergeable` from GitHub answers "does the PR conflict with the current
    base branch", not "does it conflict with its neighbour". Every PR on one
    line showed CLEAN, but after the first one merged the next became DIRTY:
    each appended to the same file;
  * one PR was a direct ancestor of another, that is, fully absorbed, and
    should have been closed instead of merged. That is one merge-base command;
  * a contract was implemented in one of two paired modules and forgotten in
    the other, although CI runs both;
  * merge readiness needs an exact mergeable value, which `pr list` does not
    have: it returns UNKNOWN because GitHub computes it lazily.

None of the four needs a model, and none should be caught by a human eyeballing
a list of PRs.

The native-or-live rule: before creating a card, check whether a native
Hermes mechanism or a live card already does it. The shared helper
board_native_guard.native_or_live is mandatory for every card generator here
(visual delta, rebase, rework, triage); the full text of the rule is
RULE_50B in board_native_guard.py.
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from factory_config import cfg, cfg_list, hermes_home  # noqa: E402

# The owner's rule "one card = one PR" lives in metrics-evening (branch > key >
# title, the body does not count). The file name has a hyphen, so it is loaded
# through importlib instead of keeping a second copy of the rule: the carriers
# must change together.
#
# CYCLE GUARD: scripts/* with a hyphen load each other through exec_module
# without a shared sys.modules key, and any back edge (metrics-evening ->
# pr-hygiene) closes an endless mutual recursion (gigabytes of memory, OOM,
# gateway stopped by the OOM policy). The key "hermes_sibling:<path>" is shared
# by all scripts/*, and the module is registered BEFORE exec: a repeated entry
# gets the same object, not a fresh copy of the file.
import importlib.util as _ilu

_FM_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "metrics-evening.py")
if os.path.exists(_FM_PATH):
    _fm_key = "hermes_sibling:" + os.path.abspath(_FM_PATH)
    factory_metrics = sys.modules.get(_fm_key)
    if factory_metrics is None:
        _fm_spec = _ilu.spec_from_file_location("factory_metrics", _FM_PATH)
        factory_metrics = _ilu.module_from_spec(_fm_spec)
        sys.modules[_fm_key] = factory_metrics
        _fm_spec.loader.exec_module(factory_metrics)
else:                              # for mutants/copies outside scripts
    import types
    factory_metrics = types.ModuleType("factory_metrics")
    factory_metrics.KEY_PR_RE = re.compile(
        r"^(?:reviewer-pr(\d+)|owner-review-#?(\d+)|rebase-(\d+)|"
        r"coder-pr(\d+))", re.IGNORECASE)
    factory_metrics.TITLE_PR_RE = re.compile(r"#(\d+)\b")

# lint_card_kind (the card kind rework/rebase/...) lives canonically in
# board-watchdog.py: the merge watchdog absorbs a rebase only into a live coder
# card of kind rework/rebase. Same hyphen trick.
_BW_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "board-watchdog.py")
if os.path.exists(_BW_PATH):
    _bw_key = "hermes_sibling:" + os.path.abspath(_BW_PATH)
    board_watchdog = sys.modules.get(_bw_key)
    if board_watchdog is None:
        _bw_spec = _ilu.spec_from_file_location("board_watchdog_ph", _BW_PATH)
        board_watchdog = _ilu.module_from_spec(_bw_spec)
        sys.modules[_bw_key] = board_watchdog
        _bw_spec.loader.exec_module(board_watchdog)
else:                              # for mutants/copies outside scripts
    import types as _t2
    board_watchdog = _t2.ModuleType("board_watchdog_stub")
    _REBASE_KEY = re.compile(
        r"^(?:coder-pr\d+-rebase-[0-9a-f]{7,40}|rebase-\d+-[0-9a-f]{7,40})$",
        re.IGNORECASE)
    _REBASE_TITLE = re.compile(r"\brebase\b", re.IGNORECASE)
    _REWORK_TITLE = re.compile(r"\brework\b", re.IGNORECASE)
    _TWIN_TITLE = re.compile(r"\btwin\b|\bport\b", re.IGNORECASE)

    def _stub_lint_card_kind(title, key):
        key = key or ""
        title = title or ""
        if key.startswith("reviewer-pr"):
            return "re-review"
        if key.startswith("owner-review") or _REWORK_TITLE.search(title):
            return "rework"
        if _REBASE_KEY.match(key) or _REBASE_TITLE.search(title):
            return "rebase"
        if _TWIN_TITLE.search(title):
            return "twin-port"
        return None
    board_watchdog.lint_card_kind = _stub_lint_card_kind

# The active milestone is not a constant: a hard-coded active milestone here
# and a band table in board-watchdog silently drifted apart at every release.
# Bands and the active milestone are computed from GitHub on every run; the
# logic lives in milestone_bands.py, one definition for pr-hygiene and
# board-watchdog. Loaded through the same cycle guard (key
# "hermes_sibling:<path>"); copies outside scripts (mutant runs) stay on the
# "no data" stub: the checks are skipped, which does not mask format mutants.
_MB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "milestone_bands.py")
if os.path.exists(_MB_PATH):
    _mb_key = "hermes_sibling:" + os.path.abspath(_MB_PATH)
    milestone_bands = sys.modules.get(_mb_key)
    if milestone_bands is None:
        _mb_spec = _ilu.spec_from_file_location("milestone_bands_ph",
                                                _MB_PATH)
        milestone_bands = _ilu.module_from_spec(_mb_spec)
        sys.modules[_mb_key] = milestone_bands
        _mb_spec.loader.exec_module(milestone_bands)
else:                              # for mutants/copies outside scripts
    import types as _t3
    milestone_bands = _t3.ModuleType("milestone_bands_stub")
    milestone_bands.resolve = lambda repo, fetch=None: {
        "active": None, "next": None, "bands": {}, "source": "nodata"}

HOME = hermes_home()
STATE = os.path.join(HOME, "kanban", "pr-hygiene-state.json")
REPO_DIR = cfg("FACTORY_REPO_PATH")
BOARD_DB = os.path.join(HOME, "kanban", "boards",
                        cfg("FACTORY_BOARD", "main"), "kanban.db")
REPEAT_AFTER = 20 * 3600
MAX_CHARS = 3600
# Paths that must change together (for example two modules that hold the same
# contract and are both run by CI). Empty by default; set FACTORY_PARITY_PATHS
# to a comma-separated list of path prefixes.
PARITY = tuple(cfg_list("FACTORY_PARITY_PATHS"))
PARITY_SCOPE = cfg("FACTORY_PARITY_SCOPE", "")
# Heading of the summary comment: the owner reads a PR for it, so its presence
# is checked by the machine instead of being a wish in the profile's SOUL.
SUMMARY_MARKER = "## What changes for you"
# Owner decisions about absorbed PRs that override the recommendation "close it
# and merge the host": PR number -> reason. An entry appears only after an
# explicit word from the owner, and is removed only by the owner's word.
# Empty here: it holds dated owner decisions and is filled per project.
ABSORBED_KEEP = {}
# A review rejection and the marker that lifts it. The review lives in a comment
# here, not in a GitHub review event: a bot cannot formally review its own PR.
REJECT_MARKER = "review verdict: changes requested"
ACCEPT_MARKERS = ("verdict: pass", "review verdict: approve", "review verdict: accept")
# A QA verdict is not a reviewer verdict. "## Review verdict (QA): PASS" once
# passed as an accept in board-watchdog. The newer form "## QA verdict: PASS"
# is dangerous here too: it contains the substring "verdict: pass". A QA
# verdict comment is skipped entirely, both heading forms.
QA_VERDICT_RE = re.compile(
    r"(?im)^#{1,4}\s*(?:review\s+verdict\s*\(\s*qa\s*\)|"
    r"qa\s+verdict)(?=\s|:|$)")

def qa_verdict_comment(body):
    """True: the comment is a QA verdict, not a reviewer verdict."""
    return bool(QA_VERDICT_RE.search(body or ""))

def git(*args, cwd=REPO_DIR, timeout=120):
    _t0 = time.time()
    try:
        done = subprocess.run(("git",) + args, cwd=cwd, capture_output=True,
                              text=True, timeout=timeout)
        return done.returncode, done.stdout.strip()
    finally:
        _prof_add("git " + (args[0] if args else "?"), time.time() - _t0)

def gh(*args, timeout=120):
    _t0 = time.time()
    try:
        done = subprocess.run(("gh",) + args, cwd=REPO_DIR, capture_output=True,
                              text=True, timeout=timeout)
        return done.stdout.strip() if done.returncode == 0 else ""
    finally:
        _prof_add("gh " + " ".join(args[:2]), time.time() - _t0)

# Per-stage timing. A run that took several times longer than usual showed that
# without instrumentation there is no telling "gh got slow" from "git
# merge-tree got slow". The accounting is cheap (two time.time() calls per
# call), printing happens only with --profile: the stages are the ones the cron
# runs, wrapping from outside would only see the sum.
PROFILE = {"t0": time.time(), "last": time.time(), "stages": {}, "calls": {}}

def _prof_add(sig, dt):
    slot = PROFILE["calls"].setdefault(sig, [0.0, 0])
    slot[0] += dt
    slot[1] += 1

def _mark(name):
    """Time from the previous mark to this one: stages of check() without reindenting."""
    now = time.time()
    PROFILE["stages"][name] = now - PROFILE["last"]
    PROFILE["last"] = now

def profile_report():
    lines = ["PROFILE (total %.1f s):" % (time.time() - PROFILE["t0"])]
    for name, dt in sorted(PROFILE["stages"].items(), key=lambda x: -x[1]):
        lines.append("  %-28s %7.1f s" % (name, dt))
    calls = sorted(PROFILE["calls"].items(), key=lambda x: -x[1][0])
    tot = sum(v[0] for v in PROFILE["calls"].values())
    n = sum(v[1] for v in PROFILE["calls"].values())
    lines.append("  calls: %d in %.1f s, top:" % (n, tot))
    for sig, (dt, cnt) in calls[:14]:
        lines.append("    %-26s %7.1f s / %d" % (sig, dt, cnt))
    return "\n".join(lines)

def open_prs():
    raw = gh_read("pr", "list", "--state", "open", "--limit", "100",
                  "--json", "number,title,url,body,baseRefName,headRefName,"
                            "updatedAt,isDraft")
    return json.loads(raw) if raw else []

# Read cache for one run. Who duplicated what and why: review_state
# (comments,commits), has_user_summary (comments,body) and pr_activity
# (comments,commits,headRefOid) each pulled their own `gh pr view` for the same
# PR, and section 4 repeated the `pr list --state all` from issue_state. On a
# fast GitHub that is about ten wasted seconds, on a slow one every call is
# expensive. The cache lives for one process: cron starts a process per run,
# and between runs verdicts and commits must be read fresh, so a TTL cache on
# disk would be a mistake (it would serve yesterday's verdict).
GH_READ_CACHE = {}

GH_READ_VERBS = frozenset((("pr", "list"), ("issue", "list"),
                           ("pr", "view"), ("issue", "view")))

def reset_run_caches():
    """Reset the caches of a "new run". Cron does not need it (fresh process),
    tests need it between cases."""
    GH_READ_CACHE.clear()

def gh_read(*args, **kwargs):
    """A gh read with a per-run cache. White list of reads: lists and view.
    A mutation or an unknown verb goes to gh() uncached: caching
    close/comment would lose the action on a repeat."""
    if tuple(args[:2]) not in GH_READ_VERBS:
        return gh(*args, **kwargs)
    key = tuple(args)
    if key not in GH_READ_CACHE:
        GH_READ_CACHE[key] = gh(*args, **kwargs)
    return GH_READ_CACHE[key]

PR_PAYLOAD_FIELDS = "comments,commits,body,headRefOid"

def pr_payload(number):
    """comments/commits/body/headRefOid in one call per PR per run.

    A single field set for review_state, has_user_summary and pr_activity: the
    union of their queries. An empty answer (gh returned '') -> {}: all
    consumers already treat emptiness as "no data".
    """
    key = ("pr", "view", str(number), "--json", PR_PAYLOAD_FIELDS)
    if key not in GH_READ_CACHE:
        GH_READ_CACHE[key] = gh("pr", "view", str(number), "--json",
                                PR_PAYLOAD_FIELDS, timeout=60)
    raw = GH_READ_CACHE[key]
    return json.loads(raw) if raw else {}

def detail(number):
    """Exact state of a PR: mergeable comes as UNKNOWN in `pr list`.

    Deliberately NOT through pr_payload/the cache: GitHub computes mergeable
    and checks lazily and they change during a run (CI catches up), so
    freshness here is the product itself, not an optimization.
    """
    raw = gh("pr", "view", str(number), "--json",
             "mergeable,mergeStateStatus,statusCheckRollup,files", timeout=60)
    if not raw:
        return None
    data = json.loads(raw)
    checks = [c.get("conclusion") or c.get("state")
              for c in (data.get("statusCheckRollup") or [])]
    files = [f["path"] for f in (data.get("files") or [])]
    # `gh pr view --json files` silently returns the first 100 files without
    # pagination: on a large PR the UI part drops out and a UI diff is
    # classified as non-UI. At the ceiling, load the full list page by page
    # through REST.
    if len(files) >= 100:
        full, page = [], 1
        while page <= 5:
            raw_p = gh_read(
                "api",
                "repos/%s/pulls/%d/files?per_page=100&page=%d"
                % (REPO_SLUG, number, page), timeout=60)
            if not raw_p:
                break
            items = json.loads(raw_p)
            full.extend(f.get("filename") or f["path"] for f in items)
            if len(items) < 100:
                break
            page += 1
        if full:
            files = full
    return {
        "mergeable": data.get("mergeable"),
        "state": data.get("mergeStateStatus"),
        "failed": [c for c in checks if c in ("FAILURE", "TIMED_OUT", "CANCELLED", "ERROR")],
        "pending": [c for c in checks if c in ("PENDING", "IN_PROGRESS", "QUEUED", None, "")],
        "files": files,
    }

def issue_state():
    """Open issues and the state of the PRs that reference them.

    Why this check exists: the convention says to write `Refs #N` in a PR, not
    `Closes #N`, for everything except critical work, because a human closes the
    issue. The human does not learn about it: nothing links a merged PR to the
    hanging issue, and it stays open after the work has landed on every line.
    """
    raw = gh_read("pr", "list", "--state", "all", "--limit", "200",
                  "--json", "number,state,title,body,baseRefName")
    prs = json.loads(raw) if raw else []
    refs = {}
    for pr in prs:
        text = (pr.get("body") or "") + " " + (pr.get("title") or "")
        # Only an explicit reference to a task: a mention like "see #56" in
        # prose does not make the PR work on that issue and must not close it.
        found_refs = re.findall(r"\b(?:closes|fixes|resolves|refs|ref)\s+#(\d{1,5})\b",
                                text, re.I)
        for number in {int(n) for n in found_refs}:
            refs.setdefault(number, []).append(pr)
    raw = gh_read("issue", "list", "--state", "open", "--limit", "100",
                  "--json", "number,title,url,labels,body")
    issues = json.loads(raw) if raw else []
    return issues, refs

def review_state(number):
    """What the reviewer said last and whether it was before the last commit.

    A green CI does not mean accepted work. The bot has no right to formally
    review its own PR, so the verdict lives in an ordinary comment, and GitHub
    knows nothing about it: a PR whose last verdict is CHANGES REQUESTED looks
    like an ordinary green one. Such PRs were sent back, reworked and never
    looked at again, while the tool called them ready to merge.

    Returns (state, explanation): none, rejected, stale, ok.
    """
    data = pr_payload(number)
    if not data:
        return "none", "could not read the PR"
    verdicts = []
    for comment in data.get("comments") or []:
        body = comment.get("body") or ""
        low = body.lower()
        if qa_verdict_comment(body):
            continue
        if REJECT_MARKER in low:
            verdicts.append((comment.get("createdAt") or "", "rejected"))
        elif any(m in low for m in ACCEPT_MARKERS):
            verdicts.append((comment.get("createdAt") or "", "ok"))
    if not verdicts:
        return "none", "no review yet"
    verdicts.sort()
    when, kind = verdicts[-1]
    if kind == "rejected":
        return "rejected", "last verdict: returned on %s" % when[:16]
    commits = data.get("commits") or []
    last_commit = ""
    for c in commits:
        stamp = ((c.get("committedDate") or "")
                 or ((c.get("authoredDate")) or ""))
        last_commit = max(last_commit, stamp or "")
    if last_commit and last_commit > when:
        return "stale", "accepted %s, but there were commits after that (%s)" % (when[:16], last_commit[:16])
    return "ok", "accepted %s" % when[:16]

def board_cards():
    """All cards of the board, read only.

    Reads kanban.db directly, as section 4 already does. Returns a list of
    dicts with id, status, assignee, title, body. An empty list if there is no
    database.
    """
    if not os.path.exists(BOARD_DB):
        return []
    con = sqlite3.connect("file:%s?mode=ro" % BOARD_DB, uri=True, timeout=10)
    rows = list(con.execute("select id, status, assignee, title, body from tasks"))
    con.close()
    return [{"id": r[0], "status": r[1], "assignee": r[2] or "-",
             "title": r[3] or "", "body": r[4] or ""} for r in rows]

def close_blockers(issue, cards):
    """Reasons why it is too early to close an issue even if all PRs are merged.

    Why this exists: an issue was closed on "all PRs merged" while the UI card
    had not even started, and two of four acceptance criteria did not exist in
    the code. "All PRs merged" is necessary but not sufficient: every
    acceptance criterion must be ticked, and the board must hold no live cards
    for the issue.

    Returns a list of reason strings. An empty list means: safe to close.
    """
    blockers = []
    body = issue.get("body") or ""
    unchecked = len(re.findall(r"^[-*]\s+\[\s\]", body, re.M))
    if unchecked:
        blockers.append("unchecked acceptance criteria: %d" % unchecked)
    number = issue["number"]
    pattern = re.compile(r"#%d\b" % number)
    live = []
    for card in cards:
        if card["status"] in ("done", "archived"):
            continue
        text = (card.get("title") or "") + " " + (card.get("body") or "")
        if pattern.search(text):
            live.append("%s (%s)" % (card["id"], card["status"]))
    if live:
        blockers.append("live cards on the board: %s" % ", ".join(live))
    return blockers

def open_cards_for_pr(number):
    """Live board cards that name this PR.

    CI answers "does it build", not "is it accepted". A reviewer can send back
    a green PR, and then a rework card appears on the board that GitHub knows
    nothing about. Green PRs were shown as ready to merge while the reviewer
    held a card with two merge blockers: a test checked one panel of three and
    hid that the others overflow the screen.
    """
    if not os.path.exists(BOARD_DB):
        return []
    con = sqlite3.connect("file:%s?mode=ro" % BOARD_DB, uri=True, timeout=10)
    rows = list(con.execute("select id, title, body, status, assignee from tasks"))
    con.close()
    pattern = re.compile(r"(?:PR\s*)?#%d\b" % number)
    return [(r[0], r[3], r[4] or "-", (r[1] or "")[:56]) for r in rows
            if r[3] not in ("done", "archived")
            and pattern.search((r[1] or "") + " " + (r[2] or ""))]

def issues_without_cards(issues, refs):
    """Issues cleared from triage that never made it into work.

    A removed wait-for-triage means the task is confirmed for implementation.
    After that the architect must pick it up and split it into cards. If that
    did not happen, the issue quietly sits: no PR, no card, no deadline. Several
    such issues were found hanging without a single card.
    """
    if not os.path.exists(BOARD_DB):
        return []
    con = sqlite3.connect("file:%s?mode=ro" % BOARD_DB, uri=True, timeout=10)
    rows = list(con.execute("select title, body, status from tasks"))
    con.close()
    stale = []
    for issue in issues:
        number = issue["number"]
        labels = [l["name"] for l in issue.get("labels", [])]
        # wait-for-triage: not confirmed yet. keep-open: the work is wider than
        # what was merged. blocked-upstream: waiting for another project.
        # needs-decision: waiting for the owner. All four mean "lies there for
        # a reason", not "forgotten".
        if {"wait-for-triage", "keep-open", "blocked-upstream",
            "needs-decision"} & set(labels):
            continue
        if refs.get(number):
            continue
        pattern = re.compile(r"#%d\b" % number)
        live = any(pattern.search((r[0] or "") + " " + (r[1] or ""))
                   for r in rows if r[2] not in ("done", "archived"))
        if not live:
            stale.append(issue)
    return stale

# === Visual delta =======================================================
#
# The owner's rule: a wall of text is useless, the owner wants two visual
# prototypes, what it was before and what it is after. The rule once required a
# "## Before / After" section as the first block of every PR and issue.
# Later, to save tokens (the [visual-delta] cards were the most expensive item
# of the whole factory), the section lives only at the issue level, at the
# prototyping stage. If the UI changes, it is a table of two REAL screenshots;
# if the UI does not change, it is a mermaid diagram of the delta. PRs no
# longer carry Before/After sections or [visual-delta] cards: the final
# screenshot from the e2e test is enough when a UI test is run. The PR branch of
# the check was removed, the issue branch (our hermes* labels) remains.
HERMES_BIN = os.path.expanduser("~/.local/bin/hermes")

BOARD_SLUG = cfg("FACTORY_BOARD", "main")

# The shared native-or-live helper (the full rule is in the module).
try:
    _bng_spec = _ilu.spec_from_file_location(
        "board_native_guard",
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "board_native_guard.py"))
    if _bng_spec is None or _bng_spec.loader is None:
        raise RuntimeError("board_native_guard: spec was not built")
    _existing = sys.modules.get("board_native_guard")
    if _existing is not None and hasattr(_existing, "native_or_live"):
        # one copy of the module for all scripts: one GuardNote class
        board_native_guard = _existing
    else:
        board_native_guard = _ilu.module_from_spec(_bng_spec)
        sys.modules["board_native_guard"] = board_native_guard
        try:
            _bng_spec.loader.exec_module(board_native_guard)
        except Exception:
            sys.modules.pop("board_native_guard", None)
            raise
except Exception:
    board_native_guard = None

def _live_keys():
    """Keys of live board cards (not done/archived) for the native guard."""
    if not os.path.exists(BOARD_DB):
        return []
    try:
        con = sqlite3.connect("file:%s?mode=ro" % BOARD_DB, uri=True,
                              timeout=10)
        rows = con.execute(
            "SELECT idempotency_key FROM tasks "
            "WHERE status NOT IN ('done','archived') "
            "AND idempotency_key IS NOT NULL").fetchall()
        con.close()
        return [r[0] for r in rows]
    except sqlite3.Error:
        return []

REPO_SLUG = cfg("FACTORY_REPO")

BA_HEADING = "## Before / After"

def _ms_open_milestones(repo):
    """Open milestones of the repository for milestone_bands.resolve, through
    gh_read (the run cache). Empty or garbage instead of JSON -> None, and
    resolve takes the last successful result from disk or "no data"."""
    out = gh_read("api",
                  "repos/%s/milestones?state=open&per_page=100" % repo,
                  timeout=60)
    try:
        items = json.loads(out or "[]")
    except Exception:
        return None
    return [{"title": m.get("title"), "due_on": m.get("due_on")}
            for m in items]

BA_HEADING_RE = re.compile(r"^##\s*Before\s*/\s*After\s*$", re.I | re.M)

BA_FIRST_RE = re.compile(r"^\s*##\s*Before\s*/\s*After\s*$", re.I | re.M)

MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\((\S+?)\)")

def ba_section(body):
    """(section text up to the next ## heading | None, section_is_first)."""
    text = body or ""
    m = BA_HEADING_RE.search(text)
    if not m:
        return None, False
    rest = text[m.end():]
    nxt = re.search(r"^##\s", rest, re.M)
    end = m.end() + (nxt.start() if nxt else len(rest))
    return text[m.start():end], bool(BA_FIRST_RE.match(text))

def http_fetch(url, timeout=12):
    """(HTTP status, bytes | None). Images must open."""
    _t0 = time.time()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read()
    except Exception:
        return 0, None
    finally:
        _prof_add("http", time.time() - _t0)

def visual_delta_verdict(body, ui, fetcher=None, cache=None):
    """List of Before / After format violations. Empty = format respected.

    ui=True requires a table of two images in every row: both links answer 200,
    Before and After differ byte for byte. ui=False requires at least one
    mermaid block. The section must be first in the body.
    """
    fetcher = fetcher or http_fetch
    cache = cache if cache is not None else {}
    section, first = ba_section(body)
    if section is None:
        return ["no '%s' section (first, before any text)" % BA_HEADING]
    bad = []
    if not first:
        bad.append("the section must be FIRST in the body, before any text")
    if ui:
        rows = [MD_IMAGE_RE.findall(line) for line in section.splitlines()
                if line.startswith("|") and "![" in line]
        rows = [r for r in rows if r]
        if not rows:
            urls = MD_IMAGE_RE.findall(section)
            if len(urls) < 2:
                bad.append("UI diff without images: a table "
                           "| Before | After | with two screenshots is required")
                return bad
            rows = [urls]
        for i, urls in enumerate(rows, 1):
            if len(urls) != 2:
                bad.append("table row %d: there must be exactly two "
                           "images (Before, After), found %d"
                           % (i, len(urls)))
                continue
            for side, url in zip(("Before", "After"), urls):
                if not (url.startswith("https://raw.githubusercontent.com/")
                        and url.endswith(".png")):
                    bad.append("row %d: %s is not a raw PNG in hermes-assets "
                               "(%s)" % (i, side, url))
            blobs = []
            for url in urls:
                if url not in cache:
                    cache[url] = fetcher(url)
                status, blob = cache[url]
                if status != 200:
                    bad.append("row %d: %s does not answer HTTP 200 "
                               "(status %s)" % (i, url, status))
                    blobs.append(None)
                else:
                    blobs.append(blob)
            if len(urls) == 2 and blobs[0] is not None \
                    and blobs[0] == blobs[1]:
                bad.append("row %d: Before and After are byte-identical: "
                           "After must show the change" % i)
    else:
        if not re.search(r"```mermaid", section, re.I):
            bad.append("non-UI diff without a mermaid diagram: Before/After "
                       "of the delta of a process, protocol or structure")
    return bad

def visual_delta_card(kind, number, violations, url):
    """A card to the author with key visual-delta-<N>. Idempotent by key."""
    # The native-or-live helper before create.
    if board_native_guard is None:
        return False, "native guard unavailable: card not created"
    covered, guard = board_native_guard.native_or_live(
        "visual-delta-%d" % number, live_keys=_live_keys(),
        native_checked=[board_native_guard.NATIVE_VERDICT],
        why="the issue has no Before/After section: no native mechanism "
        "requires it (the rule lives only in this pack); a card removed by "
        "the owner is not recreated")
    if covered:
        return False, "native guard: card not created: %s" % guard
    title = "[visual-delta] %s #%d: Before / After section" % (kind, number)
    body = (
        board_native_guard.guard_section(guard) +
        "%s #%d violates the visual-delta rule (visual-delta lives in the "
        "issue, at the prototype stage):\n\n"
        "%s\n\n"
        "Format: the body starts with the section `## Before / After`.\n"
        "Write it right when creating the issue, do not wait for the check.\n"
        "- UI diff (a real visible change on the screen): a table\n"
        "  | Before | After |, one screenshot per cell; one screen in one\n"
        "  state, PNG in hermes-assets (raw). Rendering text or test output\n"
        "  into a PNG to pass the check is forbidden: no visible change\n"
        "  means mermaid, not images.\n"
        "- non-UI (bug, documentation, refactoring): a simple mermaid\n"
        "  process diagram of 5-10 nodes: what changes; for a bug, where it\n"
        "  breaks and what the fix changes. The delta, not the whole system.\n"
        "After the section at most 5 lines, the rest goes into details.\n"
        "Capture in one call: "
        "visual-delta-shot.py (in the factory scripts directory)\n\n%s\n"
        % (kind, number, "\n".join("- %s" % v for v in violations),
           url))
    fd, path = tempfile.mkstemp(suffix=".md")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(body)
    done = subprocess.run(
        [HERMES_BIN, "kanban", "--board", BOARD_SLUG, "create", title,
         "--body-file", path, "--assignee", "coder", "--priority", "80",
         "--idempotency-key", "visual-delta-%d" % number,
         "--created-by", "pr-hygiene", "--skill", "pr-deliverable",
         "--json"],
        capture_output=True, text=True, timeout=120)
    os.unlink(path)
    return done.returncode == 0, (done.stdout or done.stderr).strip()[:200]

# The visual-delta rule applies ONLY to issues whose change is visible in the
# interface (an owner decision): a ui/ux/mobile label (including the suffixes
# -ui/-ux/-mobile) or a [ux]/[ui] prefix in the title. Bugs in a proxy, CI,
# tests or infrastructure fall outside the rule: no cards, no findings.
# Precedent: the generator recreated a card the owner had removed, only
# because the issue got a milestone. A card removed by the owner is not
# recreated: if the generator still thinks it is needed, that is a question
# to the owner (a line in the report), not a new card.
VISUAL_DELTA_UI_RE = re.compile(r"^(?:ui|ux|mobile)(?:$|-)", re.I)

VISUAL_DELTA_UI_SUFFIX = ("-ui", "-ux", "-mobile")

VISUAL_DELTA_PREFIX_RE = re.compile(r"^\s*\[\s*(?:ui|ux)\s*\]", re.I)

# Issues whose visual-delta card the owner removed: comma-separated numbers.
VISUAL_DELTA_SUPPRESSED = {
    int(n) for n in cfg_list("FACTORY_VISUAL_DELTA_SUPPRESSED")}

def visual_delta_is_ui(labels, title):
    """Whether an issue is a UI one for the visual-delta rule: label or prefix."""
    names = [n.lower() for n in (labels or [])]
    if any(VISUAL_DELTA_UI_RE.match(n) or n.endswith(VISUAL_DELTA_UI_SUFFIX)
           for n in names):
        return True
    return bool(VISUAL_DELTA_PREFIX_RE.search(title or ""))

def visual_delta_check(issues, dry_run=False):
    """Format check over our issues (PRs are not checked).

    Our issues: those with our own labels (hermes*) AND only UI ones (a
    ui/ux/mobile label or a [ux]/[ui] title prefix); bugs in a proxy, CI,
    tests and infrastructure are outside the rule. Prototyping stage: if the
    UI changes, Before/After; if it does not, mermaid. Cards are created only
    outside dry-run; the key visual-delta-<N> keeps it idempotent.
    """
    lines, cards, ok, numbers = [], [], 0, []
    # The active milestone comes from GitHub (memo per process), not a
    # constant. None = no data (gh failed and no cache): admission is
    # skipped, the old "no data" behaviour.
    active = milestone_bands.resolve(REPO_SLUG,
                                     fetch=_ms_open_milestones)["active"]
    for issue in issues:
        labels = {l["name"] for l in issue.get("labels", [])}
        if not any(l.startswith("hermes") for l in labels):
            continue                     # the rule does not bind other people's issues
        number = issue["number"]
        if number in VISUAL_DELTA_SUPPRESSED:
            lines.append(
                "    issue #%d: visual-delta card removed by the owner,"
                " the generator does not recreate it: a question to the"
                " owner, not a new card" % number)
            continue
        if not visual_delta_is_ui(labels, issue.get("title")):
            continue          # non-UI issue: outside the visual-delta rule
        body = issue.get("body") or ""
        section, first = ba_section(body)
        bad = []
        if section is None:
            bad = ["no '%s' section (first, before any text)"
                   % BA_HEADING]
        else:
            if not first:
                bad.append("the section must be FIRST in the body, "
                           "before any text")
            has_mermaid = bool(re.search(r"```mermaid", section, re.I))
            has_images = len(MD_IMAGE_RE.findall(section)) >= 2
            if not (has_mermaid or has_images):
                bad.append("the section has neither a mermaid diagram "
                           "nor two Before/After images")
        if not bad:
            ok += 1
            continue
        numbers.append(number)
        lines.append("    issue #%d %s" % (number,
                                           issue.get("title", "")[:56]))
        for v in bad:
            lines.append("      - %s" % v)
        # A [visual-delta] card is created ONLY when the section is missing
        # entirely. A section that exists but is malformed (not first, without
        # mermaid/images) stays a report finding without a card: the author
        # already knew about the section, and the format can be conveyed with a
        # comment in the issue.
        if not dry_run and section is None:
            # Admission BEFORE create. A card for an issue outside the active
            # milestone (or without one) would be parked right away by the
            # admission gate or the review layer.
            ms = (issue.get("milestone") or {}).get("title")
            if active and ms != active:
                lines.append(
                    "      card not created: issue in milestone %s,"
                    " outside the active %s (admission before create;"
                    " the active one comes from GitHub)"
                    % (ms or "none", active))
                continue
            cards.append(visual_delta_card("issue", number, bad,
                                           issue.get("url", "")))
    return ok, lines, cards, numbers

# === Prototype before implementation ====================================
#
# For an issue labelled feature or ux, the first result must be a prototype in
# the "## Before / After" section of the issue itself, not a PR with the
# implementation. This is side (c): an open PR that references
# (Refs/Closes/Fixes/Resolves) a feature/ux issue without an owner comment
# "prototype accepted" is a finding and a [BLOCK] in the reviewer's checklist.
# Sides (a)(b), the block and the lifting of cards, are held by prototype_gate
# in board-watchdog.py.
PROTO_LABELS = ("feature", "ux")

PROTO_OWNER = cfg("FACTORY_OWNER_LOGIN")

PROTO_ACCEPT_RE = re.compile(r"prototype\s+accepted", re.I)

PROTO_REFS_RE = re.compile(
    r"\b(?:closes|fixes|resolves|refs|ref)\s+#(\d{1,5})\b", re.I)

# Recognize both the label and the [ux]/[feature] title prefix.
PROTO_PREFIX_RE = re.compile(r"^\s*\[\s*(?:ux|feature)\s*\]", re.I)

# Issues exempt from the gate by an owner decision (the implementation is
# already open, so a prototype after the fact checks nothing). Empty here: it
# holds dated owner decisions and is filled per project.
PROTO_EXEMPT = {int(n) for n in cfg_list("FACTORY_PROTOTYPE_EXEMPT_ISSUES")}

def proto_is_feature(labels, title):
    """Label feature/ux OR a [ux]/[feature] prefix in the title.

    The [ux] prefix is also worn by bugs: for them the gate does not require a
    prototype, a bugfix has none. The feature/ux label is unconditional.
    """
    names = set(labels or [])
    if names & set(PROTO_LABELS):
        return True
    if "bug" in names:                              # p8-bug
        return False
    return bool(PROTO_PREFIX_RE.match(title or ""))      # p6-prefix

def prototype_verdict(pr_body, issue):
    """List of "implementation before prototype" violations. Empty = allowed.

    issue: {"number", "labels": [names], "comments": [{"user","body"}]}.
    The prototype is accepted only if the OWNER wrote the keywords: a comment
    by anyone else does not lift the gate (otherwise the implementer accepts
    the prototype for themselves).
    """
    if issue.get("number") in PROTO_EXEMPT:            # p7-exempt
        return []
    if not proto_is_feature(issue.get("labels"),       # p-labels
                            issue.get("title")):
        return []
    refs = {int(n) for n in PROTO_REFS_RE.findall(pr_body or "")}
    if issue.get("number") not in refs:            # m-refs
        return []
    for c in issue.get("comments") or []:
        user = c.get("user")
        if isinstance(user, dict):
            user = user.get("login")
        if user == PROTO_OWNER and PROTO_ACCEPT_RE.search(c.get("body")
                                                          or ""):
            return []                              # m-accept
    return ["PR implements feature/ux issue #%s before the prototype is "
            "accepted: an owner comment \"prototype accepted\" is needed "
            "in the issue"
            % issue.get("number")]

# === Promises of work on a retired line in live artifacts ===================
#
# Origin: a "twin plan" section ("This PR is the new-line half; the old-line
# twin PR follows against <old branch> once this lands", "the issue stays
# open until both halves ship") was written while two release lines were
# maintained and lived in the PR body until the merge: through the owner's
# end-of-life decision for the old line and two PASS reviews. The skill
# forbade CREATING twins but did not check bodies already written against the
# decision. The detector catches promises of old-line work in the bodies of
# open PRs and issues; lines already closed by a cancellation (dropped, EOL,
# removed in the same or a neighbouring line) do not count.
#
# Configure FACTORY_EOL_BRANCH (the retired branch, for example
# release/1.x) and optionally FACTORY_EOL_KEEP_BRANCH (the branch that
# remains). Without FACTORY_EOL_BRANCH the detector is off.
EOL_BRANCH = cfg("FACTORY_EOL_BRANCH", "")
EOL_KEEP_BRANCH = cfg("FACTORY_EOL_KEEP_BRANCH", "")
_EOL_LINE = re.escape(EOL_BRANCH.rsplit("/", 1)[-1]) if EOL_BRANCH else ""
EOL_TWIN_PROMISE_RE = re.compile(
    r"twin pr follows|twin follows|follows against [`']?%s"
    r"|%s twin pr|stays open until both halves"
    r"|both halves ship|%s twin plan"
    r"|both (?:release )?lines \(twin"
    % (re.escape(EOL_BRANCH), _EOL_LINE, _EOL_LINE), re.I) \
    if EOL_BRANCH else None
EOL_TWIN_DROP_RE = re.compile(
    r"dropped|removed|eol|end.of.life|not created|no twins"
    r"|removed from scope|cancelled|canceled"
    + (r"|only %s|%s only" % ((re.escape(EOL_KEEP_BRANCH),) * 2)
       if EOL_KEEP_BRANCH else ""), re.I)

def eol_twin_hits(body):
    """Lines promising old-line work outside a cancellation context. Read only."""
    if EOL_TWIN_PROMISE_RE is None:
        return []
    lines = (body or "").splitlines()
    hits = []
    for i, line in enumerate(lines):
        if not EOL_TWIN_PROMISE_RE.search(line):
            continue
        ctx = "\n".join(lines[max(0, i - 1):i + 3]).lower()
        if EOL_TWIN_DROP_RE.search(ctx):
            continue                     # the line is already marked as dropped
        hits.append(line.strip()[:120])
    return hits

def _promise_scan(prs, issues, hits_of):
    """(finding lines, {'pr': [...], 'issue': [...]}). Read only.

    Numbers in kinds go one per artifact (dedup), finding lines one per
    promising line."""
    lines, kinds = [], {"pr": [], "issue": []}
    for kind, label, items in (("pr", "PR", prs), ("issue", "issue", issues)):
        for it in items:
            for hit in hits_of(it.get("body")):
                if it["number"] not in kinds[kind]:
                    kinds[kind].append(it["number"])
                lines.append("    %s #%d: \"%s\"" % (label, it["number"], hit))
    return lines, kinds

def eol_twin_promise(prs, issues):
    """(finding lines, {'pr': [...], 'issue': [...]}). Read only."""
    return _promise_scan(prs, issues, eol_twin_hits)

# === A follow-up promise without a number ===================================
#
# Origin: a PR body had a section "Known limitation: ... which is tracked as a
# follow-up". No such issue existed: the promise belonged to nobody, and the
# owner had to file the issue himself. Rule: a phrase about tracking a
# follow-up in a PR or issue body must reference an issue number (#N) or a
# card (t_xxxxxxxx) in the same or a neighbouring line. Same mechanism as
# eol_twin_promise: read-only detectors over bodies, no new state.
FOLLOWUP_WORD_RE = re.compile(r"follow[ -]?ups?", re.I)
FOLLOWUP_TRACK_RE = re.compile(
    r"track\w*|defer\w*|limitation|postpone\w*|needed|future"
    r"|later|known", re.I)
FOLLOWUP_REF_RE = re.compile(r"#\d+|t_[0-9a-f]{8}", re.I)

def followup_untracked_hits(body):
    """Lines promising to track a follow-up without a number. Read only.

    "follow-up" outside a promise context (track/defer/limitation/known
    nearby) does not count; a reference to a number in the same or a
    neighbouring line cancels the hit."""
    lines = (body or "").splitlines()
    hits = []
    for i, line in enumerate(lines):
        if not FOLLOWUP_WORD_RE.search(line):
            continue
        ctx = "\n".join(lines[max(0, i - 1):i + 3])
        if not FOLLOWUP_TRACK_RE.search(ctx):
            continue          # the word is there, no tracking promise
        if FOLLOWUP_REF_RE.search(ctx):
            continue          # an issue/card number nearby: the promise has
                              # an owner
        hits.append(line.strip()[:120])
    return hits

def followup_untracked_promise(prs, issues):
    """(finding lines, {'pr': [...], 'issue': [...]}). Read only."""
    return _promise_scan(prs, issues, followup_untracked_hits)

# === A claim about external state without an artifact =======================
#
# The third case in two days after the follow-up one: a worker wrote "E2E
# flaked (SomeIT, known)" without any issue (the issue appeared only later,
# after the owner asked). The rule is wider than follow-ups: any claim about
# state OUTSIDE the worker's own run (known, tracked, duplicated, fixed,
# posted, merged, reported) in a PR or issue body must reference an artifact
# (#N, t_xxxxxxxx, URL) in the same or a neighbouring line. Same mechanism: a
# read-only detector over bodies, no new state. A false claim WITH a
# reference ("duplicated in PR #N" when it was not) is not caught here: that
# is the reviewer's job (a live check, the adversarial-review contract).
EXTERNAL_CLAIM_RE = re.compile(
    r"known\s+(?:issue|flake|failure|problem|limitation)"
    r"|(?:already|previously)\s+(?:fixed|merged|posted|reported|tracked"
    r"|duplicated)"
    r"|duplicat\w*\s+(?:in|as|by)\b"
    r"|is\s+tracked\s+in",
    re.I)
EXTERNAL_REF_RE = re.compile(r"#\d+|t_[0-9a-f]{8}|https?://\S+", re.I)

def external_claim_untracked_hits(body):
    """Lines claiming external state without an artifact. Read only.

    Trigger: a known/already-*/duplicated... phrase WITHOUT an issue number,
    a card or a URL in the same or a neighbouring line. "known issue" next to
    #N or t_xxxxxxxx is cancelled: the claim has an owner.
    """
    lines = (body or "").splitlines()
    hits = []
    for i, line in enumerate(lines):
        if not EXTERNAL_CLAIM_RE.search(line):
            continue
        ctx = "\n".join(lines[max(0, i - 1):i + 3])
        if EXTERNAL_REF_RE.search(ctx):
            continue
        hits.append(line.strip()[:120])
    return hits

def external_claim_untracked_promise(prs, issues):
    """(finding lines, {'pr': [...], 'issue': [...]}). Read only."""
    return _promise_scan(prs, issues, external_claim_untracked_hits)

def prototype_check(prs, issues):
    """Findings over open PRs: (lines, PR numbers). Read only."""
    labeled = {}
    for issue in issues:
        names = {l.get("name") for l in issue.get("labels", [])}
        if issue["number"] in PROTO_EXEMPT:
            continue
        if proto_is_feature(names, issue.get("title") or ""):
            labeled[issue["number"]] = {"labels": sorted(names),
                                        "title": issue.get("title") or ""}
    lines, numbers = [], []
    for pr in prs:
        text = "%s %s" % (pr.get("title") or "", pr.get("body") or "")
        refs = {int(n) for n in PROTO_REFS_RE.findall(text)}
        for number in sorted(refs & set(labeled)):
            raw = gh_read("api",
                          "repos/%s/issues/%d/comments?per_page=100"
                          % (REPO_SLUG, number))
            comments = [{"user": (c.get("user") or {}).get("login"),
                         "body": c.get("body")}
                        for c in json.loads(raw or "[]")]
            bad = prototype_verdict(
                pr.get("body") or "",
                {"number": number, "labels": labeled[number]["labels"],
                 "title": labeled[number]["title"], "comments": comments})
            if bad:
                numbers.append(pr["number"])
                lines.append("    PR #%d -> issue #%d: %s"
                             % (pr["number"], number, bad[0]))
    return lines, numbers

# === Library contract: calibrate against a run, not a guess ============
#
# A detector modelled the text of a library's error as "top message + first
# Caused by line", the reviewer gave PASS, and the fixtures were hand-written
# for the same hypothesis and green. The real library (read from its bytecode)
# builds the message from ex.getMessage() plus the ROOT cause, walking getCause()
# to the end of the chain. On a chain of depth 3 or more the detector stayed
# silent. The rule: a PR whose tests model the output of an external library
# must carry in its body a line "Library contract: <artifact>:<version>
# <class>#<method>"; whether the fixture is a real run is checked by the
# reviewer (the Calibration item "library contract" in adversarial-review).
TEST_DIR_RE = re.compile(r"(^|/)(?:src/)?(?:tests?|it|e2e|__tests__)/", re.I)

TEST_NAME_RE = re.compile(
    r"^(?:.*(?:[Tt]est|[Ss]pec)s?\.java$|.*ITs?\.java$"
    r"|.*\.(?:test|spec)\.[a-z0-9]+)$")

_LIB_WORDS = r"upstream\w*|librar\w*"

_FORM_WORDS = r"two[- ]lines?|shape|format|output\w*"

LIBMODEL_RE = re.compile(
    r"(?i)(?:\b(?:%s)\b[^\n]{0,120}\b(?:%s)\b"
    r"|\b(?:%s)\b[^\n]{0,120}\b(?:%s)\b)"
    % (_LIB_WORDS, _FORM_WORDS, _FORM_WORDS, _LIB_WORDS))

LIBCONTRACT_RE = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?library contract:\s*[\w.\-]+:[\w.\-]*\d[\w.\-]*"
    r"\s+[\w.$]+#\w+")

def is_test_file(path):
    """A test file by path or name (src/test/, *Tests.java, *.test.tsx)."""
    p = "/" + (path or "")
    return bool(TEST_DIR_RE.search(p)) or bool(
        TEST_NAME_RE.match(p.rsplit("/", 1)[-1]))

def lib_contract_verdict(body, added_test_lines):
    """(violations, sign). Empty violations = calibration present or not needed.

    The sign "models library output": in the PR body or in the added test
    lines, on one line within 120 characters, there is a library word
    (upstream, library) and a form word (two-line, shape, format, output), in
    either order. The requirement: a Library contract line with
    artifact:version (the version contains a digit) and Class#method in the PR
    body. A hand-written fixture for the same hypothesis as the implementation
    is always green: whether the fixture is a real run is checked by the
    reviewer, the machine checks only that it was recorded.
    """
    text = "%s\n%s" % (body or "", "\n".join(added_test_lines or []))
    m = LIBMODEL_RE.search(text)
    if not m:
        return [], None
    sign = re.sub(r"\s+", " ", m.group(0))[:100]
    if LIBCONTRACT_RE.search(body or ""):
        return [], sign
    return [sign], sign

def lib_contract_scan(prs):
    """{PR number: sign} of open PRs that model library output without a
    contract line. Reads head diffs from refs/prs/<N> (fetch_heads has already
    run in check()). Read only, a finding creates no cards."""
    out = {}
    for p in sorted(prs, key=lambda x: x["number"]):
        files = ((p.get("detail") or {}).get("files")) or []
        tests = sorted({f for f in files if is_test_file(f)})
        if not tests:
            continue
        base = p.get("baseRefName") or ""
        code, diff = git("diff", "--unified=0",
                         "origin/%s...refs/prs/%d" % (base, p["number"]),
                         "--", *tests, timeout=120)
        if code != 0:
            continue                 # the diff cannot be read: do not spam with findings
        added = [ln[1:] for ln in diff.splitlines()
                 if ln.startswith("+") and not ln.startswith("+++")]
        bad, sign = lib_contract_verdict(p.get("body") or "", added)
        if bad:
            out[p["number"]] = sign
    return out

def _is_summary_header_line(line):
    """The line is the section heading SUMMARY_MARKER.

    Exactly a heading on its own line: a mention of the phrase in prose
    (backticks, continuation of a line) is not a heading. Case does not matter.
    """
    return line.strip().lower() == SUMMARY_MARKER.lower()

def _comment_is_summary(body):
    """The comment is a summary: it starts with the heading line."""
    for line in (body or "").splitlines():
        if not line.strip():
            continue
        return _is_summary_header_line(line)
    return False

def _body_has_summary_section(body):
    """The PR body has a section with the heading line SUMMARY_MARKER."""
    return any(_is_summary_header_line(l) for l in (body or "").splitlines())

def has_user_summary(number):
    """Whether the PR has a summary comment about the user-visible effect.

    The summary is a comment that starts with the SUMMARY_MARKER heading, or a
    PR body section with this heading line. Live verdicts mentioned "What
    changes for you" in prose ("published by the coder after this PASS"): a
    substring check counted such PRs as ready for the owner to read without a
    real summary.
    """
    data = pr_payload(number)
    if _body_has_summary_section(data.get("body") or ""):
        return True
    return any(_comment_is_summary(c.get("body") or "")
               for c in (data.get("comments") or []))

def fetch_heads(numbers):
    """Pull PR heads into a separate refs namespace, the working tree is untouched."""
    specs = ["+refs/pull/%d/head:refs/prs/%d" % (n, n) for n in numbers]
    git("fetch", "-q", "origin", *specs, timeout=300)

def merges_clean(base_ref, first, second):
    """Whether second stays mergeable after first has been merged."""
    code, tree = git("merge-tree", "--write-tree", base_ref, "refs/prs/%d" % first)
    if code != 0 or not tree:
        return True                      # the first does not merge by itself, the pair is not to blame
    head = tree.splitlines()[0]
    code, merged = git("commit-tree", head, "-p", base_ref,
                       "-p", "refs/prs/%d" % first, "-m", "probe")
    if code != 0:
        return True
    code, out = git("merge-tree", "--write-tree", "--messages", merged,
                    "refs/prs/%d" % second)
    return "CONFLICT" not in out

def conflict_paths(base_ref, first, second):
    code, tree = git("merge-tree", "--write-tree", base_ref, "refs/prs/%d" % first)
    head = tree.splitlines()[0] if tree else None
    if not head:
        return []
    code, merged = git("commit-tree", head, "-p", base_ref,
                       "-p", "refs/prs/%d" % first, "-m", "probe")
    code, out = git("merge-tree", "--write-tree", "--messages", merged,
                    "refs/prs/%d" % second)
    return sorted({m.group(1) for m in
                   re.finditer(r"CONFLICT \([^)]+\): Merge conflict in (.+)", out)})

# === Merge flow instead of a big jump ===================================
#
# The goal: few CONFLICTING PRs and few PRs without movement for more than 48 h.
# Reason: every merge into the base branch breaks its neighbours, rebases go in
# waves, and pairs wait for each other for days. The rule "rebase in time" does
# not work as text, so the machine tracks the base shift and the rebase card:
# a conflict is caught in the same run, while it is fresh.
LINES = tuple(cfg_list("FACTORY_BASE_BRANCHES", ("main",)))

CONFLICT_DEBT_LIMIT = 3

STALE_PR_HOURS = 48

STALE_GOAL = 2

MERGE_WATCH = os.path.join(HOME, "cache", "pr-hygiene-merge-watch.json")

DEBT_CACHE = os.path.join(HOME, "cache", "pr-hygiene-conflict-debt.json")

# Hold: the live bootstrap of this flow is not accepted by the owner. While the
# file exists, all actions of the flow are dry-run (cards, comments, state
# files). It is lifted by the owner's word "accepted" (the file is deleted).
MERGEFLOW_HOLD = os.path.join(HOME, "cache", "mergeflow-hold")

# Meta of the owner's dispatcher stop: a PR's idle time does not tick while the
# owner holds the dispatcher.
STOP_META = os.path.join(HOME, "cache", "kanban-dispatch-hold.meta.json")

REBASE_KEY_RE = re.compile(r"^coder-pr(\d+)-rebase-([0-9a-f]{7,40})$")

# "One PR = one card": the owner card of a PR is found by the decomposition
# convention. PR branch <prefix><issue>-<tail>, card key coder-<issue>-<slug>;
# a match is tail.endswith(slug) (a real case: branch
# <prefix>120-client-demo-e2e and key coder-120-demo-e2e, the branch slug is
# longer than the key). Keys coder-pr<N>-rebase-... do not match \d+ (the
# letters "pr"). FACTORY_ISSUE_BRANCH_PREFIX is the branch prefix agents use.
ISSUE_BRANCH_PREFIX = cfg("FACTORY_ISSUE_BRANCH_PREFIX", "hermes/")
ISSUE_BRANCH_RE = re.compile(r"^%s(\d+)-(.+)$" % re.escape(ISSUE_BRANCH_PREFIX))

CODER_ISSUE_KEY_RE = re.compile(r"^coder-(\d+)-(.+)$")

TRIAGE_KEY_RE = re.compile(r"^architect-pr(\d+)-triage-48h$")

def line_heads():
    """SHAs of the base-line heads with one ls-remote, the working tree is untouched."""
    code, out = git("ls-remote", "origin",
                    *["refs/heads/" + b for b in LINES], timeout=60)
    heads = {}
    for row in out.splitlines():
        parts = row.split()
        if len(parts) == 2:
            heads[parts[1].replace("refs/heads/", "")] = parts[0]
    return {b: sha for b, sha in heads.items() if b in LINES}

def stack_parents(numbers, info):
    """{child: parent}: the parent's head is an ancestor of the child's head, same base.

    A stack is a conscious choice (skill stacked-layer-card): a layer is built
    on the head of an open PR while that PR is not in the base branch yet. The
    parent here is not necessarily the nearest: any open ancestor PR makes a PR
    a child, and the ancestor the root of the stack.
    """
    parents = {}
    for a in numbers:
        for b in numbers:
            if a == b or b in parents:
                continue
            if info[a]["baseRefName"] != info[b]["baseRefName"]:
                continue
            code, _ = git("merge-base", "--is-ancestor",
                          "refs/prs/%d" % a, "refs/prs/%d" % b)
            if code == 0:
                parents[b] = a
    return parents

def rebase_priority(verdict_ok, is_root):
    """Rebase order of the merge flow: pairs with a reviewer verdict (closer
    to merge) first, then stack roots (they hold their children), then the rest."""
    if verdict_ok:
        return 99
    if is_root:
        return 97
    return 96

def live_cards_by_pr(head_of):
    """{PR: [(id, title, rank, key, assignee)]} of live cards by the owner's
    rule "one card = one PR" from factory-metrics: carriers are branch (0) >
    key (1) > title (2) > decomposition owner (3), the body does not count.
    Live = not done/archived. Assignee is needed by the rebase addressee
    filter: a coder card of kind rework/rebase absorbs, or the owner of the
    branch by the key coder-<issue>-<slug>.

    Live rebase cards carry different key conventions: rebase-<N>-<head7>,
    owner-review-<pr>-<head7>, decomp-<parent>-..., coder-pr<N>-rebase-<sha7>.
    KEY_PR_RE sees the first two, decompositions and keyless cards are caught
    by the title. Rank 3: a decomposition card without a PR number in its
    title ("issue #120 C4") is linked to a PR by the branch
    <prefix><issue>-<tail> and the key coder-<issue>-<slug>; when ambiguous
    (several open PRs of the same issue fit one slug) no carrier is counted.
    """
    if not os.path.exists(BOARD_DB):
        return {}
    con = sqlite3.connect("file:%s?mode=ro" % BOARD_DB, uri=True, timeout=10)
    rows = list(con.execute(
        "select id, coalesce(title,''), coalesce(idempotency_key,''),"
        " coalesce(branch_name,''), coalesce(assignee,'') from tasks"
        " where status not in ('done','archived')"))
    con.close()
    branch_to = {branch: n for n, branch in head_of.items() if branch}
    numbers = set(head_of)
    # (issue, branch tail) -> numbers of open PRs of that line
    issue_pr = {}
    for branch, n in branch_to.items():
        m = ISSUE_BRANCH_RE.match(branch)
        if m:
            issue_pr.setdefault((int(m.group(1)), m.group(2)), []).append(n)
    out = {}
    for tid, title, key, branch, assignee in rows:
        # carriers in order of strength: branch (0), key (1), title (2),
        # decomposition owner (3); the body does not count. The subject of a card at equal rank is the
        # FIRST number in the title ("Rebase #224 onto #223" is the card of
        # #224, not of #223).
        cand = []
        if branch and branch in branch_to:
            cand.append((branch_to[branch], 0))
        m = factory_metrics.KEY_PR_RE.match(key)
        if m:
            num = int(next(g for g in m.groups() if g))
            if num in numbers:
                cand.append((num, 1))
        for num_s in factory_metrics.TITLE_PR_RE.findall(title):
            num = int(num_s)
            if num in numbers:
                cand.append((num, 2))
        m = CODER_ISSUE_KEY_RE.match(key or "")
        if m:
            issue, slug = int(m.group(1)), m.group(2)
            hits = sorted({n2 for (iss, tail), ns in issue_pr.items()
                           if iss == issue and tail.endswith(slug)
                           for n2 in ns})
            if len(hits) == 1:
                cand.append((hits[0], 3))
        if not cand:
            continue
        # rank of a PR: the strongest of all carriers of this PR in the card
        rank_of = {}
        for num, rank in cand:
            rank_of[num] = min(rank_of.get(num, 9), rank)
        # one card = one PR: the strongest carrier, on a tie the first in order
        # of appearance (no twin split is needed here: this is not credits,
        # it is a search for already assigned work)
        best = min(cand, key=lambda c: (rank_of[c[0]], cand.index(c)))[0]
        out.setdefault(best, []).append(
            (tid, title, rank_of[best], key, assignee))
    return out

def rebase_key(number, base7):
    return "coder-pr%d-rebase-%s" % (number, base7)

def live_rebase_cards():
    """{PR number: (card id, key, status)} of live rebase cards."""
    if not os.path.exists(BOARD_DB):
        return {}
    con = sqlite3.connect("file:%s?mode=ro" % BOARD_DB, uri=True, timeout=10)
    rows = list(con.execute(
        "select id, idempotency_key, status from tasks "
        "where idempotency_key like 'coder-pr%-rebase-%' "
        "and status not in ('done','archived')"))
    con.close()
    live = {}
    for task_id, key, status in rows:
        m = REBASE_KEY_RE.match(key or "")
        if m:
            live[int(m.group(1))] = (task_id, key, status)
    return live

def rebase_card_body(number, base, sha7, parent, url, head_ref):
    lines = [
        "PR #%d conflicts with the head of %s (%s): the base has moved." % (
            number, base, sha7),
        "",
        url,
        "",
        "Rebase order: pairs with a reviewer verdict first, then the roots of"
        " stacks. The card priority already reflects the queue.",
    ]
    if parent:
        lines.append("Stack: PR #%d sits on the open PR #%d. A child is not"
                     " rebased onto a parent that still has blockers: settle"
                     " the parent first." % (number, parent))
    lines += [
        "",
        "How to do it (skill stacked-layer-card):",
        "- git fetch origin %s %s;" % (base, head_ref or "<PR head branch>"),
        "- take the base as the foundation and apply only the mechanical"
        " delta of the PR;",
        "- force-with-lease only after a fresh fetch of the head branch;",
        "- the local branch may be named differently: take the head branch"
        " from gh pr view %d --json headRefName." % number,
        "",
        "Acceptance criterion: gh pr view %d --json mergeable gives MERGEABLE,"
        " CI is green, and a summary is published in the PR." % number,
    ]
    return "\n".join(lines)

def create_rebase_card(number, base, sha7, parent, url, head_ref, prio):
    # The native-or-live helper before create.
    if board_native_guard is None:
        return False, "native guard unavailable: card not created"
    covered, guard = board_native_guard.native_or_live(
        rebase_key(number, sha7), live_keys=_live_keys(),
        native_checked=[board_native_guard.NATIVE_VERDICT,
                        board_native_guard.NATIVE_TASK_LINKS],
        why="rebase conflict with %s at head %s: Hermes has no native rebase "
        "generator (stacked-layer-card is a pack layer)" % (base, sha7))
    if covered:
        return False, "native guard: card not created: %s" % guard
    title = "rebase PR #%d onto %s@%s" % (number, base, sha7)
    body = (board_native_guard.guard_section(guard)
            + rebase_card_body(number, base, sha7, parent, url, head_ref))
    fd, path = tempfile.mkstemp(suffix=".md")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(body)
    # Rebasing is mechanical work: a cheaper model is enough when one is
    # configured.
    cheap = []
    if cfg("FACTORY_CHEAP_MODEL", ""):
        cheap += ["--model", cfg("FACTORY_CHEAP_MODEL", "")]
    if cfg("FACTORY_CHEAP_PROVIDER", ""):
        cheap += ["--provider", cfg("FACTORY_CHEAP_PROVIDER", "")]
    done = subprocess.run(
        [HERMES_BIN, "kanban", "--board", BOARD_SLUG, "create", title,
         "--body-file", path, "--assignee", "coder",
         "--priority", str(prio),
         "--idempotency-key", rebase_key(number, sha7),
         "--created-by", "pr-hygiene", "--skill", "stacked-layer-card"]
        + cheap + ["--json"],
        capture_output=True, text=True, timeout=120)
    os.unlink(path)
    return done.returncode == 0, (done.stdout or done.stderr).strip()[:200]

def comment_card(task_id, text):
    done = subprocess.run(
        [HERMES_BIN, "kanban", "--board", BOARD_SLUG, "comment",
         task_id, text],
        capture_output=True, text=True, timeout=90)
    return done.returncode == 0

def absorbs_rebase(title, key, assignee):
    """Whether a live card absorbs the rebase work.

    Yes: assignee is coder AND the kind from lint_card_kind(title, key) is
    rework/rebase, OR the decomposition key coder-<issue>-<slug>: that is the
    owner of the PR branch, under "one PR = one card" it does the rebase and
    no separate card is created (a real case: a rebase card was created on
    top of the live owner, two coder cards of one PR in ready).
    Visual-delta, reviewer, decomposition cards without the key convention
    and cards without an assignee do not absorb a rebase: their executor
    would not do it, so a separate rebase card is created. The
    "one card = one PR" rule stays for credit attribution (factory-metrics)
    but not for absorbing work.
    """
    if (assignee or "") != "coder":
        return False
    if CODER_ISSUE_KEY_RE.match(key or ""):
        return True
    return board_watchdog.lint_card_kind(title, key) in ("rework", "rebase")

def post_merge_watch(info, ancestors, by_pr=None, dry_run=False):
    """Merge watcher: rebase cards for conflicts with the NEW base head.

    Trigger: the head SHA of a long-lived branch moved since the previous
    run (a merge landed) or this is the first observation (bootstrap: cards
    for the whole current conflict debt). For every CONFLICTING PR of that
    base, from the least noisy action up:
    - a live rebase card with the same target -> nothing;
    - a live rebase card with a stale target -> a comment with the new sha7;
    - any other live card of this PR (by_pr, the "one card = one PR" rule)
      -> a comment with the new sha7 on it, NO new card: one card = one PR.
      Only a live CODER card of kind rework/rebase (lint_card_kind) absorbs
      the work; visual-delta, reviewer and decomposition cards and cards
      without an assignee will not do the rebase, so a rebase card is
      created for them. The owner also absorbs: a live coder decomposition
      card (key coder-<issue>-<slug>, rank 3 in live_cards_by_pr) gets a
      comment with the rebase target instead of a new card;
    - no card at all (or the live one does not absorb) -> create one with
      the key coder-pr<N>-rebase-<sha7>.

    Returns (findings, total conflicting). State files are written only in
    live mode: a dry run does not consume the event.
    """
    by_pr = by_pr or {}
    findings = []
    heads = line_heads()
    prev = {}
    if os.path.exists(MERGE_WATCH):
        try:
            prev = json.load(open(MERGE_WATCH))
        except Exception:
            prev = {}
    live = live_rebase_cards()
    roots = set(ancestors.values())
    total = sum(1 for p in info.values()
                if (p.get("detail") or {}).get("mergeable") == "CONFLICTING")
    plan = []
    watch = {}
    for base in LINES:
        sha = heads.get(base)
        if not sha:
            continue
        sha7 = sha[:7]
        watch[base] = {"sha": sha}
        if (prev.get(base) or {}).get("sha") == sha:
            continue                     # no merges, the watcher stays quiet
        conflicting = sorted(
            n for n, p in info.items()
            if p.get("baseRefName") == base
            and (p.get("detail") or {}).get("mergeable") == "CONFLICTING")
        watch[base]["conflicting"] = conflicting
        for n in conflicting:
            verdict_state, _ = review_state(n)
            prio = rebase_priority(verdict_state == "ok", n in roots)
            entry = live.get(n)
            if entry and entry[1] == rebase_key(n, sha7):
                continue                 # a card with this target is alive
            if entry:
                plan.append((prio, n, base, sha7, ancestors.get(n),
                             info[n].get("url", ""),
                             info[n].get("headRefName") or "",
                             "retarget", entry[0]))
                continue
            existing = [c for c in (by_pr.get(n) or [])
                        if absorbs_rebase(c[1], c[3], c[4])]
            if existing:
                best = min(existing, key=lambda c: (c[2], c[0]))
                owner = bool(CODER_ISSUE_KEY_RE.match(best[3] or ""))
                plan.append((prio, n, base, sha7, ancestors.get(n),
                             info[n].get("url", ""),
                             info[n].get("headRefName") or "",
                             "existing-owner" if owner else "existing",
                             best[0]))
                continue
            plan.append((prio, n, base, sha7, ancestors.get(n),
                         info[n].get("url", ""),
                         info[n].get("headRefName") or "",
                         "create", None))
    for prio, n, base, sha7, parent, url, head_ref, action, target in \
            sorted(plan, key=lambda x: (-x[0], x[1])):
        key = rebase_key(n, sha7)
        if action == "create":
            if dry_run:
                findings.append(("mergeflow-rebase-would:%s" % key,
                                 "  REBASE (dry-run) p%d #%d [%s] key %s"
                                 % (prio, n, base, key)))
                continue
            # Admission BEFORE create. A PR outside the active milestone or
            # without a link to an approved issue would be parked by the gate
            # right after creation.
            admit_fn = getattr(board_watchdog, "gen_admission", None)
            if admit_fn:
                admit, why = admit_fn(BOARD_SLUG, n)
                if not admit:
                    findings.append((
                        "mergeflow-rebase-noadmit:%s" % key,
                        "  REBASE #%d not created: %s (admission before"
                        " create)" % (n, why)))
                    continue
            ok, out = create_rebase_card(
                n, base, sha7, parent, url, head_ref, prio)
            findings.append((
                "mergeflow-rebase-new:%s" % key if ok
                else "mergeflow-rebase-fail:%s" % key,
                "  REBASE p%d #%d [%s] card %s%s" % (
                    prio, n, base, "created" if ok else "NOT created",
                    "" if ok else ": %s" % out)))
        elif action == "existing-owner":
            if dry_run:
                findings.append(
                    ("mergeflow-existing-owner-would:%d:%s" % (n, target),
                     "  CONFLICT (dry-run) p%d #%d [%s]: live owner %s"
                     " (decomposition key coder-<issue>-<slug>): it absorbs"
                     " the rebase, no new card"
                     % (prio, n, base, target)))
                continue
            comment_card(target,
                         "Conflict with the new head of %s (%s): rebase"
                         " target of PR #%d. This card does the rebase: it"
                         " owns the PR branch (one PR = one card), no"
                         " separate rebase card is created. Mechanics: the"
                         " stacked-layer-card skill." % (base, sha7, n))
            findings.append((
                "mergeflow-existing-owner:%d:%s" % (n, target),
                "  CONFLICT p%d #%d [%s]: comment on the live owner"
                " %s (decomposition) with the target %s@%s, no new card"
                % (prio, n, base, target, base, sha7)))
        elif action == "existing":
            if dry_run:
                findings.append(("mergeflow-existing-would:%d:%s" % (n, target),
                                 "  CONFLICT (dry-run) p%d #%d [%s]: live"
                                 " card %s is already assigned, a comment"
                                 " with the target %s@%s, no new card"
                                 % (prio, n, base, target, base, sha7)))
                continue
            comment_card(target,
                         "Conflict with the new head of %s (%s): rebase"
                         " target of PR #%d. This card absorbs the rebase"
                         " (coder, kind rework/rebase), no new card is"
                         " created." % (
                             base, sha7, n))
            findings.append(("mergeflow-existing:%d:%s" % (n, target),
                             "  CONFLICT p%d #%d [%s]: comment on the live"
                             " card %s (coder rework/rebase) with the target"
                             " %s@%s, no new card"
                             % (prio, n, base, target, base, sha7)))
        else:
            if dry_run:
                findings.append(("mergeflow-retarget-would:%s" % key,
                                 "  TARGET MOVED (dry-run) #%d: live"
                                 " card %s, new sha %s"
                                 % (n, target, sha7)))
                continue
            comment_card(target, "The base moved again: the rebase target of"
                         " PR #%d is now %s@%s." % (n, base, sha7))
            findings.append(("mergeflow-retarget:%s" % key,
                             "  TARGET MOVED #%d: comment on %s, new"
                             " sha %s" % (n, target, sha7)))
    if not dry_run and watch:
        os.makedirs(os.path.dirname(MERGE_WATCH), exist_ok=True)
        json.dump(watch, open(MERGE_WATCH, "w"))
        os.makedirs(os.path.dirname(DEBT_CACHE), exist_ok=True)
        json.dump({"count": total, "ts": time.time(),
                   "lines": {b: v["sha"][:7] for b, v in watch.items()}},
                  open(DEBT_CACHE, "w"))
    if total > CONFLICT_DEBT_LIMIT:
        top = sorted(n for n, p in info.items()
                     if (p.get("detail") or {}).get("mergeable")
                     == "CONFLICTING")
        findings.append(("mergeflow-debt",  # key without the counter
                         "  CONFLICT DEBT %d > %d (limit): new milestone"
                         " features do not start, slots go to rebases."
                         " First: %s"
                         % (total, CONFLICT_DEBT_LIMIT,
                            ", ".join("#%d" % n for n in top[:5]))))
    return findings, total

REBASE_STALE_MARKER = ("Watchdog: rebase card quenched: its acceptance "
                       "criterion is already met.")

def stale_rebase_gate(info, dry_run=False):
    """Quench rebase cards of PRs that are already MERGEABLE.

    A real case: a rebase card was created while the PR conflicted; by the
    time a worker claimed it, the PR head already sat on the target base,
    MERGEABLE, CI green and a reviewer PASS on that head. Any push from the
    card would have reset the PASS. The same class as a stale rework arm,
    but with a higher stake: not an empty run, a lost ready verdict.

    Rule: a live rebase card is quenched (comment + archive) when its PR is
    already MERGEABLE (the card's acceptance criterion was met another way)
    or left OPEN. running is not archived (the run is paid for): it gets a
    stop directive "do not push, close as already-done". A read failure and
    mergeable UNKNOWN are conservatively not quenched. Called on EVERY run,
    regardless of base movement: a card can go stale at any moment between
    create and claim.
    """
    findings = []
    for number, (task_id, key, status) in sorted(
            live_rebase_cards().items()):
        if number not in info:
            why = ("PR #%d is no longer OPEN: no rebase is needed" % number)
        else:
            mergeable = (info[number].get("detail") or {}).get("mergeable")
            if mergeable != "MERGEABLE":
                continue
            why = ("PR #%d is already MERGEABLE: the card's acceptance "
                   "criterion (gh pr view --json mergeable = MERGEABLE) is "
                   "met without it, any push would reset the green CI and "
                   "the verdict on the head" % number)
        if dry_run:
            findings.append(("mergeflow-stale-rebase-would:%d" % number,
                             "  REBASE QUENCH (dry-run) %s [%s] key %s:"
                             " %s" % (task_id, status, key, why)))
            continue
        if status == "running":
            comment_card(task_id,
                         "STOP directive: %s. Do NOT push: a force push "
                         "would reset the ready verdict. Close the card with "
                         "an already-done comment without changing the "
                         "branch." % why)
            findings.append((
                "mergeflow-stale-rebase-running:%d" % number,
                "  REBASE QUENCH %s [%s]: the PR is already MERGEABLE, a "
                "stop directive was sent to the running card"
                % (task_id, status)))
            continue
        comment_card(task_id, REBASE_STALE_MARKER + " " + why)
        done = subprocess.run(
            [HERMES_BIN, "kanban", "--board", BOARD_SLUG, "archive",
             task_id],
            capture_output=True, text=True, timeout=120)
        findings.append((
            "mergeflow-stale-rebase:%d" % number if done.returncode == 0
            else "mergeflow-stale-rebase-fail:%d" % number,
            "  REBASE QUENCH %s [%s] key %s: %s; card %s"
            % (task_id, status, key, why,
               "archived" if done.returncode == 0
               else "NOT archived: %s"
               % (done.stderr or done.stdout).strip()[:120])))
    return findings

def _iso_to_epoch(value):
    """GitHub ISO time to epoch, or None. None means "unknown", not "never
    happened": triage on broken data produces false cards."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None

def owner_stop_since():
    """Timestamp when the owner's dispatcher stop began, or None (no stop).

    The kanban-dispatch-hold meta file lives while the stop stands: the
    death of the holder removes it, and so does lifting the stop. While the
    meta exists, the PR idle clock does not tick (the owner holds the
    dispatcher on purpose)."""
    try:
        meta = json.load(open(STOP_META))
    except Exception:
        return None
    since = meta.get("since")
    return since if isinstance(since, (int, float)) else None

def effective_age_s(last_ts, now, stop_since=None):
    """Age without the owner-stop window: the idle clock does not tick while
    the stop stands. An event after the stop began does not age at all (the
    whole difference falls inside the stop window); an event before the stop
    ages only up to its start."""
    age = now - last_ts
    if stop_since and stop_since < now:
        if last_ts >= stop_since:
            return 0.0
        age = stop_since - last_ts
    return age

def pr_activity(number):
    """(commit ts, verdict ts, verdict state, head40) in one call.

    Verdicts are recognized by the same markers as in review_state: the bot
    cannot formally review its own PR, so the verdict lives in a comment.
    state: 'ok' | 'rejected' | '' by the last marker comment."""
    data = pr_payload(number)
    if not data:
        return None, None, "", ""
    last_commit, verdict = "", ""
    verdict_state = ""
    for c in data.get("commits") or []:
        stamp = (c.get("committedDate") or "") or (c.get("authoredDate")
                                                   or "")
        last_commit = max(last_commit, stamp)
    for comment in data.get("comments") or []:
        body = comment.get("body") or ""
        if qa_verdict_comment(body):
            continue
        low = body.lower()
        state = ""
        if REJECT_MARKER in low:
            state = "rejected"
        elif any(m in low for m in ACCEPT_MARKERS):
            state = "ok"
        if not state:
            continue
        stamp = comment.get("createdAt") or ""
        if stamp >= verdict:
            verdict, verdict_state = stamp, state
    return (_iso_to_epoch(last_commit), _iso_to_epoch(verdict),
            verdict_state, data.get("headRefOid") or "")

def classify_stale(commit_ts, verdict_ts, now, hours=STALE_PR_HOURS,
                   stop_since=None):
    """A PR without movement: no commits and no verdicts for longer than hours.

    The owner-stop window is subtracted from the age: while the owner holds
    the dispatcher, a PR does not rot through our fault."""
    if commit_ts is None:
        return False
    window = hours * 3600
    if effective_age_s(commit_ts, now, stop_since) < window:
        return False
    if verdict_ts is not None \
            and effective_age_s(verdict_ts, now, stop_since) < window:
        return False
    return True

def rework_card_priority(number):
    """Rework of an open PR goes before new work of the band.

    The svc_priority semantics of board-watchdog (one place for bands:
    milestone_bands.band_of_ref): the active milestone band holds 90 (below
    review 100 and the owner summary 95, above new work 80), an inactive
    milestone gives its exact band, no data -> 90.
    """
    band = None
    fn = getattr(milestone_bands, "band_of_ref", None)
    if fn:
        try:
            band = fn(REPO_SLUG, number, is_pr=True)
        except Exception:
            band = None
    if band is None or band == getattr(milestone_bands, "ACTIVE_BAND", 80):
        return 90
    return band

def create_rework_card(number, url, head40, verdict_ts):
    """Rework card for an owner verdict: one card per verdict.

    Key coder-pr<N>-rework-<head7>: the verdict is tied to the PR head, and
    there are no commits after the verdict, so the verdict head is the
    current head. A repeated run with the same verdict is deduplicated by
    the live card (factory-metrics sees the key too: carrier coder-pr<N>-)."""
    # The native-or-live helper before create.
    if board_native_guard is None:
        return False, "native guard unavailable: card not created"
    # One key through the shared helper (board_native_guard.rework_key),
    # the same as the watchdog and the reviewer on RETURN.
    if not str(head40 or "").strip():
        return False, "rework_key: the PR head is unknown, card not created"
    key = board_native_guard.rework_key(number, head40)
    covered, guard = board_native_guard.native_or_live(
        key,
        live_keys=_live_keys(),
        native_checked=[board_native_guard.NATIVE_VERDICT,
                        board_native_guard.NATIVE_REVIEW_LANE],
        why="owner verdict CHANGES REQUESTED %s ago without an author "
        "answer: Hermes has no native rework on an owner verdict (a pack "
        "generator)" % (
            ("%.0f h" % (effective_age_s(verdict_ts, time.time()) / 3600)
             if verdict_ts else "long")))
    if covered:
        return False, "native guard: card not created: %s" % guard
    now = time.time()
    age_v = ("%.0f h" % (effective_age_s(verdict_ts, now) / 3600)
             if verdict_ts else "unknown")
    head7 = (head40 or "")[:7]
    title = "[coder] PR #%d rework per owner verdict (head %s)" % (
        number, head7)
    body = "\n".join([
        "PR #%d: the last owner verdict CHANGES REQUESTED was %s ago, and"
        " the author has not answered since." % (number, age_v),
        "",
        url,
        "",
        "Rework under the \"one card per verdict\" rule: take the criteria"
        " from the verdict in the PR and close with a summary comment in the"
        " PR.",
        "",
        "Acceptance criterion: pushes after verdict %s and a summary comment"
        " in PR #%d." % (head7, number),
    ])
    fd, path = tempfile.mkstemp(suffix=".md")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(board_native_guard.guard_section(guard) + body)
    done = subprocess.run(
        [HERMES_BIN, "kanban", "--board", BOARD_SLUG, "create", title,
         "--body-file", path, "--assignee", "coder",
         "--priority", str(rework_card_priority(number)),
         "--idempotency-key", key,
         "--created-by", "pr-hygiene", "--skill", "stacked-layer-card",
         "--json"],
        capture_output=True, text=True, timeout=120)
    os.unlink(path)
    return done.returncode == 0, (done.stdout or done.stderr).strip()[:200]

def stale_scan(prs, by_pr=None, dry_run=False):
    """Staleness without a live card.

    Candidate: updatedAt older than 48 h (a raw, wide filter). Then:
    - a live card for the PR exists ("one card = one PR" rule) -> work is
      assigned, no triage needed;
    - the last owner verdict is CHANGES REQUESTED with no author answer ->
      a coder rework card p80 (one card per verdict);
    - otherwise -> an architect triage card p20 (finish, close or merge).

    The age is computed without the owner-stop window. Returns
    (findings, number of stale PRs).
    """
    by_pr = by_pr or {}
    findings = []
    now = time.time()
    stop = owner_stop_since()
    candidates = sorted(
        p["number"] for p in prs
        if (ts := _iso_to_epoch(p.get("updatedAt") or "")) is not None
        and ts < now - STALE_PR_HOURS * 3600)
    triaged = set()
    if os.path.exists(BOARD_DB):
        con = sqlite3.connect("file:%s?mode=ro" % BOARD_DB, uri=True,
                              timeout=10)
        rows = list(con.execute(
            "select idempotency_key from tasks "
            "where (idempotency_key like 'architect-pr%-triage-48h' "
            "or idempotency_key like 'coder-pr%-rework-%') "
            "and status not in ('done','archived')"))
        con.close()
        for (key,) in rows:
            m = TRIAGE_KEY_RE.match(key or "")
            if m:
                triaged.add(int(m.group(1)))
            m = re.match(r"^coder-pr(\d+)-rework-", key or "")
            if m:
                triaged.add(int(m.group(1)))
    url_of = {p["number"]: p.get("url", "") for p in prs}
    stale = []
    stats = {"cand": len(candidates), "already": 0, "young": 0,
             "carded": 0, "acted": 0}
    for n in candidates:
        if n in triaged:
            stats["already"] += 1
            continue
        commit_ts, verdict_ts, verdict_state, head40 = pr_activity(n)
        if not classify_stale(commit_ts, verdict_ts, now,
                              stop_since=stop):
            stats["young"] += 1       # young, or frozen by the owner stop
            continue
        if by_pr.get(n):
            stats["carded"] += 1      # work is assigned: stay quiet
            continue
        stale.append(n)
        verdict_no_answer = (
            verdict_state == "rejected"
            and verdict_ts is not None
            and (commit_ts is None or commit_ts <= verdict_ts))
        reason = ("rework for an owner verdict with no answer"
                  if verdict_no_answer else
                  "no card and no verdict: architect triage")
        text = ("  NO MOVEMENT > %d h (owner stop subtracted): #%d, %s"
                "\n    %s" % (STALE_PR_HOURS, n, reason, url_of.get(n, "")))
        if dry_run:
            findings.append(("mergeflow-stale-would:%d" % n, text))
            continue
        if verdict_no_answer:
            ok, out = create_rework_card(n, url_of.get(n, ""), head40,
                                         verdict_ts)
        else:
            ok, out = create_triage_card(n, url_of.get(n, ""),
                                         commit_ts, verdict_ts)
        text += ("\n    card %s" % ("created" if ok
                                    else "NOT created: %s" % out))
        findings.append(("mergeflow-stale-%s:%d" % ("new" if ok else "fail",
                                                  n), text))
    stats["acted"] = len(stale)
    findings.append((
        "mergeflow-stale-summary",
        "  STALE OVERVIEW: candidates by updatedAt %d; quiet: %d already have"
        " a triage/rework card, %d not stale after subtracting the owner"
        " stop, %d have a live work card; actions %d"
        % (stats["cand"], stats["already"], stats["young"],
           stats["carded"], stats["acted"])))
    return findings, len(stale)

def create_triage_card(number, url, commit_ts, verdict_ts):
    """Architect triage card with the key architect-pr<N>-triage-48h."""
    # The native-or-live helper before create.
    if board_native_guard is None:
        return False, "native guard unavailable: card not created"
    covered, guard = board_native_guard.native_or_live(
        "architect-pr%d-triage-48h" % number, live_keys=_live_keys(),
        native_checked=[board_native_guard.NATIVE_RESPAWN,
                        board_native_guard.NATIVE_TASK_LINKS],
        why="PR without movement for more than %d h: Hermes has no native "
        "stale triage (a live card for the PR was checked above, no live "
        "links)" % STALE_PR_HOURS)
    if covered:
        return False, "native guard: card not created: %s" % guard
    now = time.time()
    age_c = ("%.0f h" % ((now - commit_ts) / 3600)
             if commit_ts else "unknown")
    age_v = ("%.0f h" % ((now - verdict_ts) / 3600)
             if verdict_ts else "none")
    title = "triage PR #%d: finish, close or merge" % number
    body = "\n".join([
        "PR #%d has had no movement for more than %d h: the last commit was"
        " %s ago, the last reviewer verdict %s."
        % (number, STALE_PR_HOURS, age_c, age_v),
        "",
        url,
        "",
        "Three decisions, all legitimate:",
        "1. finish: assign the work, a deadline and an owner;",
        "2. close: gh pr close with a comment giving the reason;",
        "3. merge: name the host PR that absorbs this one.",
        "",
        "Publish the decision as a comment in the PR itself: the observer"
        " reads the PR, not the board.",
        "",
        "Acceptance criterion: PR #%d has a comment with the decision and its"
        " steps." % number,
    ])
    fd, path = tempfile.mkstemp(suffix=".md")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(board_native_guard.guard_section(guard) + body)
    done = subprocess.run(
        [HERMES_BIN, "kanban", "--board", BOARD_SLUG, "create", title,
         "--body-file", path, "--assignee", "architect",
         "--priority", "20",
         "--idempotency-key", "architect-pr%d-triage-48h" % number,
         "--created-by", "pr-hygiene", "--json"],
        capture_output=True, text=True, timeout=120)
    os.unlink(path)
    return done.returncode == 0, (done.stdout or done.stderr).strip()[:200]

def stack_comment_present(child, parent):
    """True/False: our stack advice already hangs in the PR; None = unknown."""
    raw = gh("pr", "view", str(child), "--json", "comments", timeout=60)
    if not raw:
        return None
    data = json.loads(raw)
    for comment in data.get("comments") or []:
        body = comment.get("body") or ""
        if "stack order" in body.lower() and ("#%d" % parent) in body:
            return True
    return False

def post_stack_comment(child, parent):
    body = ("Stack order: PR #%d sits on the open PR #%d."
            " Add the stack order to the PR body: who merges first, who"
            " waits. For example: stack: #%d -> #%d."
            % (child, parent, parent, child))
    gh("pr", "comment", str(child), "--body", body, timeout=60)

def stack_lint(prs, info, ancestors, dry_run=False):
    """Stack check: a PR base is always one of the long-lived branches, and
    the stack order is in the PR body.

    A foreign base breaks CI silently (zero runs on a base outside the
    long-lived branches). A stack order missing from the body makes a wave
    of rebases unmanageable: the reviewer cannot see who goes first.
    Violations: a finding and a single advice comment in the PR (no
    duplicates).
    """
    findings = []
    for p in sorted(prs, key=lambda x: x["number"]):
        n, base = p["number"], p.get("baseRefName") or "?"
        if base not in LINES:
            findings.append((
                "mergeflow-stackbase:%d" % n,
                "  BASE IS NOT A LONG-LIVED BRANCH: #%d on '%s': CI on a"
                " foreign base stays silent. Retarget to %s."
                % (n, base, " or ".join(LINES))))
    for child, parent in sorted(ancestors.items()):
        body = info[child].get("body") or ""
        if re.search(r"#%d\b" % parent, body):
            continue
        findings.append((
            "mergeflow-stackorder:%d-%d" % (child, parent),
            "  STACK ORDER NOT IN THE BODY: #%d sits on #%d, the PR body"
            " must name the parent and the merge order." % (child, parent)))
        if dry_run:
            continue
        seen = stack_comment_present(child, parent)
        if seen is None:
            continue               # comments unreadable: do not spam
        if not seen:
            post_stack_comment(child, parent)
    return findings

def mergeflow_enabled():
    """False = owner hold: the merge-flow mechanisms run as dry-run only."""
    return not os.path.exists(MERGEFLOW_HOLD)

def duplicate_rebase_findings(by_pr):
    """Two or more live rebase cards for one PR: show them to the owner.

    A rebase-like card has the word rebase in its title; verify/force-push
    cards are not rebase work.
    """
    findings = []
    for n, cards in sorted(by_pr.items()):
        rebase_like = [(tid, title) for tid, title, _rank, _key, _as in cards
                       if re.search(r"(?i)\brebase\b", title)]
        if len(rebase_like) > 1:
            findings.append((
                "mergeflow-duplicate-rebase:%d" % n,
                "  DUPLICATE REBASE: #%d has %d live cards: %s."
                % (n, len(rebase_like),
                   ", ".join("%s (%s...)" % (tid, t[:34])
                             for tid, t in rebase_like))))
    return findings

def check():
    prs = open_prs()
    if not prs:
        return []
    numbers = [p["number"] for p in prs]
    fetch_heads(numbers)
    _mark("01_fetch_heads(%dPR)" % len(numbers))
    info = {p["number"]: p for p in prs}
    for number in numbers:
        info[number]["detail"] = detail(number)
    _mark("02_details(%d)" % len(numbers))

    # Library contract: reads only the diffs of the heads.
    lib_bad = lib_contract_scan(prs)
    _mark("02b_libcontract(%d)" % len(numbers))

    found = []

    # 1. Absorbed: the head of one PR is already contained in another.
    swallowed = {}
    for a in numbers:
        for b in numbers:
            if a == b or info[a]["baseRefName"] != info[b]["baseRefName"]:
                continue
            code, _ = git("merge-base", "--is-ancestor",
                          "refs/prs/%d" % a, "refs/prs/%d" % b)
            if code == 0:
                swallowed.setdefault(a, []).append(b)
    for a, hosts in sorted(swallowed.items()):
        if a in ABSORBED_KEEP:
            continue                     # owner decision: do not close
        hosts = sorted(hosts)
        found.append(("absorbed:%d" % a,
                      "  #%d is absorbed entirely, its head is already in %s.\n"
                      "    A separate merge is not needed: close #%d, merge %s" % (
                          a, ", ".join("#%d" % h for h in hosts), a,
                          " or ".join(info[h]["url"] for h in hosts))))

    # 2. Ready to merge, and what will break after the first merge.
    _mark("03_s1_swallowed")
    ready = [n for n in numbers
             if info[n]["detail"] and not info[n]["detail"]["failed"]
             and not info[n]["detail"]["pending"]
             and info[n]["detail"]["mergeable"] == "MERGEABLE"
             and not info[n]["isDraft"]]
    absorbed = set(swallowed)
    ready = [n for n in ready if n not in absorbed]
    # PRs that still have a live card on the board: the work is not accepted.
    in_review = {n: open_cards_for_pr(n) for n in ready}
    in_review = {n: cards for n, cards in in_review.items() if cards}
    ready = [n for n in ready if n not in in_review]

    if in_review:
        lines = ["  Green, but NOT ready: a live card for this PR is on the board.",
                 "  CI says whether the code builds, not whether it is accepted."]
        for number in sorted(in_review):
            lines.append("    #%d %s" % (number, info[number]["url"]))
            for card_id, status, who, title in in_review[number]:
                lines.append("      %s [%s, %s] %s" % (card_id, status, who, title))
        # The key is per family, without the PR list: the list changes every
        # run and would reset REPEAT_AFTER. A finding repeats at most once a
        # day.
        found.append(("inreview", "\n".join(lines)))

    # Review verdict: green CI does not replace accepted work.
    unreviewed = {}
    for n in list(ready):
        state, why = review_state(n)
        if state != "ok":
            unreviewed[n] = (state, why)
    ready = [n for n in ready if n not in unreviewed]
    if unreviewed:
        titles = {"none": "no review was done",
                  "rejected": "the reviewer returned the work and has not looked again",
                  "stale": "new commits appeared after acceptance"}
        lines = ["  Green, but NOT accepted by review. The verdict lives in a comment, GitHub does not",
                 "  show it: the bot has no right to formally review its own PR."]
        for n in sorted(unreviewed):
            state, why = unreviewed[n]
            lines.append("    #%d %s" % (n, titles.get(state, state)))
            lines.append("      %s" % why)
            lines.append("      %s" % info[n]["url"])
        found.append(("unreviewed", "\n".join(lines)))

    no_summary = [n for n in ready if not has_user_summary(n)]
    ready = [n for n in ready if n not in no_summary]

    if no_summary:
        lines = ["  Technically ready, but there is nothing for the owner to read: no summary",
                 "  \"%s\" in the PR body or in the comments." % SUMMARY_MARKER,
                 "  The summary answers three questions: what changes for the user,",
                 "  what proves it (screenshot, code sample, command output), what was run."]
        for n in sorted(no_summary):
            lines.append("    #%d [%s] %s\n      %s" % (
                n, info[n]["baseRefName"], info[n]["title"], info[n]["url"]))
        found.append(("nosummary", "\n".join(lines)))

    # Modelling the output of a library without calibration = not ready.
    # The finding covers ALL open PRs (like visual-delta), not only the ready
    # ones: a PR bounced by review must not vanish from the contract report.
    ready = [n for n in ready if n not in lib_bad]
    if lib_bad:
        lines = [
            "  These model the output of an external library without calibration:",
            "  the PR body needs a line `Library contract: <artifact>:<version> <class>#<method>`",
            "  (a version with a digit, a place in the library code: source or bytecode)",
            "  and at least one fixture must be a run of the real library:",
            "  one handwritten for the same hypothesis as the implementation is always green.",
        ]
        for n in sorted(lib_bad):
            lines.append("    #%d [%s] sign: %s" % (
                n, info[n]["baseRefName"], lib_bad.get(n) or "?"))
            lines.append("      %s" % info[n]["url"])
        found.append(("libcontract", "\n".join(lines)))

    if ready:
        lines = ["  Ready to merge (CI green, no conflicts with the base, summary published):"]
        for n in sorted(ready):
            lines.append("    #%d [%s] %s\n      %s" % (
                n, info[n]["baseRefName"], info[n]["title"], info[n]["url"]))
        found.append(("ready", "\n".join(lines)))

    if ready or no_summary:

        pairs = []
        mergeable_now = sorted(set(ready) | set(no_summary))
        for a in mergeable_now:
            for b in mergeable_now:
                if a >= b or info[a]["baseRefName"] != info[b]["baseRefName"]:
                    continue
                base = "origin/" + info[a]["baseRefName"]
                if not merges_clean(base, a, b):
                    paths = conflict_paths(base, a, b)
                    pairs.append((a, b, paths))
        if pairs:
            lines = ["  But these pairs will collide: after the first merges, the second becomes conflicting.",
                     "  GitHub does not show this, it checks each PR against the current base."]
            for a, b, paths in pairs:
                lines.append("    #%d + #%d: %s" % (a, b, ", ".join(paths) or "content"))
            found.append(("pairs", "\n".join(lines)))

    # 3. An issue whose work is already merged but which is still open.
    _mark("04_s2_ready_pairs")
    issues, refs = issue_state()
    cards = board_cards()
    for issue in issues:
        number = issue["number"]
        linked = [p for p in refs.get(number, []) if p["number"] != number]
        if not linked:
            continue
        merged = [p for p in linked if p["state"] == "MERGED"]
        pending = [p for p in linked if p["state"] == "OPEN"]
        if not merged or pending:
            continue
        labels = [l["name"] for l in issue.get("labels", [])]
        if "wait-for-triage" in labels:
            continue
        # keep-open: the work is merged, but the task is deliberately wider
        # than what was merged (for example, the contract is closed on one
        # stack of two). A human removes the label.
        if {"keep-open", "blocked-upstream", "needs-decision"} & set(labels):
            continue
        blockers = close_blockers(issue, cards)
        merged_numbers = sorted(p["number"] for p in merged)
        listed = ", ".join("#%d" % n for n in merged_numbers)
        if blockers:
            lines = ["  #%d: all PRs are merged (%s), but it is too early to close:" % (number, listed)]
            for b in blockers:
                lines.append("    - %s" % b)
            lines.append("    %s" % issue["title"])
            lines.append("    %s" % issue["url"])
            found.append(("issue-notclosed:%d" % number, "\n".join(lines)))
            continue
        note = ("Closed automatically: every pull request that references this issue is "
                "merged (%s), and none is left open. Reopen if the work turns out to be "
                "incomplete." % listed)
        if DRY_RUN:
            found.append(("issue-dryclose:%d" % number,
                          "  #%d: dry-run, auto-close suppressed (%s merged)"
                          % (number, listed)))
            continue
        # The comment is a separate call and goes BEFORE the close. `issue
        # close --comment` closed silently when the comment failed, leaving an
        # issue closed with no explanation. A task closed without an
        # explanation is worse than an open one: it cannot be reconstructed
        # what was considered done.
        posted = gh("issue", "comment", str(number), "--body", note, timeout=60)
        lines = ["  #%d: all of its work is merged (%s)" % (number, listed),
                 "    %s" % issue["title"],
                 "    %s" % issue["url"]]
        if not posted:
            lines.append("    NOT CLOSED: the comment with the rationale was not posted,")
            lines.append("    closing silently is not allowed. Check gh access and close it by hand.")
            found.append(("issue-closefail:%d" % number, "\n".join(lines)))
            continue
        closed = gh("issue", "close", str(number), timeout=60)
        lines.insert(0, "  #%d closed automatically" % number)
        if closed is None or closed == "":
            lines.append("    WARNING: the close command returned nothing, check by hand")
        found.append(("issue-closed:%d:%s" % (number, ",".join(map(str, merged_numbers))),
                      "\n".join(lines)))

    # 4. Live cards whose PRs are all merged: nothing left to work on.
    _mark("05_s3_issues_cards")
    #
    # Such a card neither fails nor complains: it looks for open PRs that do
    # not exist until the iteration budget runs out. For example, a
    # verification card of one release line was linked by a dependency to a
    # card of the other line, waited in the queue until its PRs were already
    # merged, and burned its whole budget twice in a row for nothing.
    if os.path.exists(BOARD_DB):
        con = sqlite3.connect("file:%s?mode=ro" % BOARD_DB, uri=True, timeout=10)
        board = list(con.execute("select id, status, assignee, title, body from tasks"))
        con.close()
        # open_prs() returns only open PRs and has no state field, so the
        # state of all PRs comes from a separate query. It uses the same
        # arguments as issue_state (gh_read returns the cached answer of
        # section 3, so the second identical query pays no round trip).
        raw_all = gh_read("pr", "list", "--state", "all", "--limit", "200",
                          "--json", "number,state,title,body,baseRefName")
        state_of = {p["number"]: p["state"] for p in (json.loads(raw_all) if raw_all else [])}
        obsolete = []
        for task_id, status, assignee, title, body in board:
            if status not in ("todo", "ready", "running", "blocked"):
                continue
            text = (title or "") + " " + (body or "")
            mentioned = {int(n) for n in re.findall(r"#(\d{1,5})\b", text)}
            known = [n for n in mentioned if n in state_of]
            if len(known) < 2:
                continue                     # one reference means nothing yet
            if all(state_of[n] == "MERGED" for n in known):
                obsolete.append((task_id, status, assignee or "-", (title or "")[:52],
                                 sorted(known)))
        if obsolete:
            lines = ["  Cards whose PRs are all merged: nothing to work on, but they are in the queue.",
                     "  Such a card does not fail, it searches for non-existent PRs until its budget ends."]
            for task_id, status, who, title, prs_list in obsolete[:6]:
                lines.append("    %s [%s, %s] %s" % (task_id, status, who, title))
                lines.append("      merged: %s" % ", ".join("#%d" % n for n in prs_list))
            found.append(("obsolete", "\n".join(lines)))

    # 5. Confirmed issues that nobody took into work.
    _mark("06_s4_obsolete")
    orphans = issues_without_cards(issues, refs)
    if orphans:
        lines = ["  Confirmed for implementation (triage lifted), but neither a card nor a PR exists:"]
        for issue in orphans:
            lines.append("    #%d %s\n      %s" % (
                issue["number"], issue["title"], issue["url"]))
        lines.append("    The architect needs to decompose it, otherwise the task just sits.")
        found.append(("orphan-issues", "\n".join(lines)))

    # 6b. Parity of paired modules: both hold the contract, so change them together.
    _mark("07_s5_orphans")
    for n in numbers:
        d = info[n]["detail"]
        if not d:
            continue
        touched = [p for p in d["files"] if PARITY_SCOPE in p]
        left = [p for p in touched if p.startswith(PARITY[0])]
        right = [p for p in touched if p.startswith(PARITY[1])]
        if bool(left) != bool(right):
            missing = PARITY[1] if left else PARITY[0]
            found.append(("parity:%d" % n,
                          "  #%d changes only one module of a pair, %s is untouched.\n"
                          "    Both modules answer by the same protocol and both run in CI.\n"
                          "    %s" % (n, missing.rstrip("/").split("-")[-1], info[n]["url"])))

    # 7. Visual delta (only our own issues, at the prototyping stage; the PR
    #    is not checked).
    _mark("08_parity")
    vd = visual_delta_check(issues, dry_run=DRY_RUN)
    ok_vd, vd_lines, vd_cards, vd_numbers = vd
    if vd_lines:
        ours = sum(1 for i in issues if any(
            l["name"].startswith("hermes") for l in i.get("labels", [])))
        lines = ["  Without a visual delta (the rule covers only our own"
                 " issues): satisfied %d," % ok_vd,
                 "  violated by our issues %d." % (ours - ok_vd)]
        lines.extend(vd_lines)
        lines.append("  Capture in one call: "
                     "visual-delta-shot.py (in the scripts directory)")
        if not DRY_RUN and vd_cards:
            lines.append("  Visual-delta cards: %d created (key "
                         "visual-delta-<N>, idempotent)."
                         % sum(1 for ok, _ in vd_cards if ok))
        found.append(("visual-delta:%s"
                      % ",".join(map(str, vd_numbers)), "\n".join(lines)))

    # 7b. Prototype before implementation: an open PR for a feature/ux issue
    #     without "prototype accepted" from the owner. Only a finding and a
    #     [BLOCK] for the reviewer; cards are held by the watchdog.
    proto_lines, proto_numbers = prototype_check(prs, issues)
    if proto_lines:
        found.append(("prototype:%s"
                      % ",".join(map(str, sorted(set(proto_numbers)))),
                      "\n".join(
                          ["  Implementation before the prototype:"]
                          + proto_lines
                          + ["  For the reviewer: [BLOCK] until the issue has"
                             " an owner comment saying"
                             " \"prototype accepted\"."])))

    # 7c. Promises of work on a retired release line in live bodies. A
    #     "twin plan" section lived in a PR body from the time of two lines
    #     until the merge: through the end-of-life decision and two PASS
    #     reviews. The skill forbade CREATING twins, but nothing checked the
    #     bodies already written against the decision.
    eol_lines, eol_kinds = eol_twin_promise(prs, issues)
    if eol_lines:
        found.append(("eoltwin:%s" % ",".join(
            "%s%d" % (k, n) for k in ("pr", "issue")
            for n in sorted(set(eol_kinds[k]))), "\n".join(
            ["  Work promised on %s (end of life):" % EOL_BRANCH]
            + eol_lines
            + ["  %s is end of life by an owner decision." % EOL_BRANCH,
               "  For the reviewer: [BLOCK] for a promising section in the"
               " body",
               "  (the PR body must match the code and the owner's"
               " decisions). For the",
               "  author of the next touch of the PR (rework/summary/rebase):"
               " remove the",
               "  section or rewrite it as \"dropped: EOL\"; an issue with"
               " acceptance",
               "  criteria for both lines gets a scope comment \"scope %s"
               " only\"." % (EOL_KEEP_BRANCH or "the remaining line")])))

    # 7d. A follow-up promise without a number. "Known limitation: ... which
    #     is tracked as a follow-up" without an issue number in the body:
    #     the promise belongs to nobody and the work is lost. Same mechanism
    #     as 7c: reading the bodies of open PRs and issues.
    fu_lines, fu_kinds = followup_untracked_promise(prs, issues)
    if fu_lines:
        found.append(("followup:%s" % ",".join(
            "%s%d" % (k, n) for k in ("pr", "issue")
            for n in sorted(set(fu_kinds[k]))), "\n".join(
            ["  A follow-up promised without an issue/card number:"]
            + fu_lines
            + ["  Rule: a phrase about tracking a follow-up must reference",
               "  a number (#N or t_xxxxxxxx) in the same or a neighbouring",
               "  line, otherwise the promise belongs to nobody. Reviewer:",
               "  [BLOCK] until the number appears; author: file the issue",
               "  or card and write its number next to the phrase."])))

    # 7e. A claim about external state (known/tracked/duplicated/already
    #     fixed|merged|posted|reported) without an issue/card number or a
    #     URL in the body. An extension of 7d: "known flake" without an
    #     artifact is a claim nobody owns.
    ec_lines, ec_kinds = external_claim_untracked_promise(prs, issues)
    if ec_lines:
        found.append(("extclaim:%s" % ",".join(
            "%s%d" % (k, n) for k in ("pr", "issue")
            for n in sorted(set(ec_kinds[k]))), "\n".join(
            ["  A claim about external state without an artifact:"]
            + ec_lines
            + ["  Rule: known/tracked/duplicated/fixed/merged/",
               "  posted/reported is written only with a reference to an",
               "  artifact (#N, t_xxxxxxxx, URL) in the same or a",
               "  neighbouring line; otherwise \"not verified\". Reviewer:",
               "  [BLOCK] until the number appears; author: file the issue",
               "  or card and write its number. False claims with a",
               "  reference are caught by the reviewer's live check, not by",
               "  this detector."])))

    # 8. Merge watcher (rebase cards, conflict debt).
    _mark("09_visual_delta")
    # Owner hold: until the owner accepts the bootstrap, everything is dry-run.
    if not mergeflow_enabled():
        found.append(("mergeflow-hold:1",
                      "  HOLD: the merge-flow bootstrap is not accepted,"
                      " actions are suppressed (dry-run). Release: the"
                      " owner says \"accepted\"."))
    l5_dry = DRY_RUN or not mergeflow_enabled()
    ancestors = stack_parents(numbers, info)
    by_pr = live_cards_by_pr(
        {n: info[n].get("headRefName") for n in info})
    _mark("10_l5_ancestors_bypr")
    # Duplicate rebases are always shown: this is a finding, not an action,
    # so the hold does not silence it.
    found.extend(duplicate_rebase_findings(by_pr))
    # Quench rebase cards of already MERGEABLE PRs (every run, before
    # post_merge_watch: a card can go stale between create and claim).
    found.extend(stale_rebase_gate(info, dry_run=l5_dry))
    l5_findings, debt_count = post_merge_watch(info, ancestors, by_pr,
                                               dry_run=l5_dry)
    found.extend(l5_findings)
    _mark("11_l5_post_merge")
    l5_stale_findings, l5_stale = stale_scan(prs, by_pr=by_pr,
                                             dry_run=l5_dry)
    found.extend(l5_stale_findings)
    _mark("12_l5_stale")
    found.extend(stack_lint(prs, info, ancestors, dry_run=l5_dry))
    _mark("13_l5_stack")
    if debt_count > CONFLICT_DEBT_LIMIT or l5_stale > STALE_GOAL:
        found.append((
            "mergeflow-metric",  # key without counters
            "  MERGE-FLOW METRIC: CONFLICTING %d (goal <= %d), no movement"
            " > %d h %d (goal <= %d)"
            % (debt_count, CONFLICT_DEBT_LIMIT, STALE_PR_HOURS,
               l5_stale, STALE_GOAL)))
    return found

DRY_RUN = "--dry-run" in sys.argv or "--mergeflow-dry-run" in sys.argv

VD_ONLY = "--visual-delta" in sys.argv

L5_ONLY = "--mergeflow-dry-run" in sys.argv

LIB_ONLY = "--lib-contract" in sys.argv

def main():
    if DRY_RUN:
        # dry-run: no state file, no character limit, no changes to the world
        # (cards and issue auto-closes are suppressed inside check).
        found = check()
        if VD_ONLY:
            found = [x for x in found if x[0].startswith("visual-delta")]
        if LIB_ONLY:
            found = [x for x in found if x[0].startswith("libcontract")]
            print("LIBRARY CONTRACT (dry-run)\n" + "\n".join(t for _, t in found)
                  if found else "LIBRARY CONTRACT (dry-run): no violations")
            _profile_maybe()
            return
        if L5_ONLY:
            # Merge-flow mechanisms only, a full dump without the MAX_CHARS
            # limit, to see the whole conflict debt.
            found = [x for x in found if x[0].startswith("mergeflow")]
            print("MERGE FLOW (dry-run)\n" + "\n".join(t for _, t in found)
                  if found else "MERGE FLOW (dry-run): no violations")
            _profile_maybe()
            return
        print("PR HYGIENE (dry-run)\n" + "\n".join(t for _, t in found)
              if found else "PR HYGIENE (dry-run): no violations")
        _profile_maybe()
        return
    state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    now = time.time()
    report, keys = [], []
    # Merge-flow findings first: rebase cards matter more than old findings,
    # otherwise MAX_CHARS eats them on the bootstrap run after the visual
    # delta.
    found = list(check())
    found.sort(key=lambda x: not x[0].startswith("mergeflow"))
    for key, text in found:
        if now - state.get(key, 0) < REPEAT_AFTER:
            continue
        if sum(len(x) for x in report) + len(text) > MAX_CHARS:
            continue
        report.append(text)
        keys.append(key)
    if not report:
        _profile_maybe()
        return
    for key in keys:
        state[key] = now
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump(state, open(STATE, "w"))
    # Hygiene findings do not need action from the owner (verdicts are raised
    # by the review layer), so Telegram stays quiet and the routine goes to
    # the daily digest for metrics-evening.
    from cron_quiet import digest_add
    digest_add("pr-hygiene", "PR HYGIENE\n" + "\n".join(report))
    _profile_maybe()

def _profile_maybe():
    if "--profile" in sys.argv:
        print(profile_report(), file=sys.stderr)

if __name__ == "__main__":
    main()

