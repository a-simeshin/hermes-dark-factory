#!/usr/bin/env python3
"""Kanban board watchdog. Alerts the owner when work has stopped and is waiting for a human.

Installed as an hourly cron job (--no-agent); stays silent while the boards move.

Why it exists: a gate card went to blocked while waiting for a manual PR merge.
The dispatcher was alive and ticking, but the whole task graph was stuck behind
four blocked cards and no todo card could become ready. The owner learned about
the stall days later and not from Hermes. Hermes had the detector
(`hermes kanban diag` showed stuck_in_blocked and repeated_failures), but not the
delivery: only someone who ran the command ever saw the diagnostics.

Checks:
  1. dispatcher heartbeat: the last 'kanban dispatcher' line in the log is not
     older than 15 min while ready>0, otherwise an alert about a dead or hung
     process;
  2. board diag (severity >= warning): the stock Hermes detectors;
  3. graph stuck: ready+running == 0 with non-empty todo, with a breakdown of
     the roots and the cost of inaction (how many cards each root unblocks);
  4. silence: no finished run for longer than SILENCE_HOURS with a non-empty
     backlog, catches a dead dispatcher that the first two do not see;
  5. max_in_progress gate: a machine check of parallelism. The ceiling is
     kanban.max_in_progress = 2 (owner decision), plus the conditions for
     rolling back to 1 from the project conventions: swap above 3.5 GB, load5
     above 12 with two running, a double crashed failure within a 30 min window
     with a trace of an abnormal gateway stop. A violation or rollback is fixed
     at once through `hermes config set` and accompanied by an alert comment on
     the board. The dispatcher built into the gateway reads max_in_progress
     only once at start (gateway/kanban_watchers.py: _kanban_dispatcher_boot,
     v0.23.1): changing the limit through `hermes config set` takes effect only
     after a gateway restart; only the separate deprecated loop
     `hermes kanban daemon --force` recomputes it every tick. So a rollback to 1
     carries the note "gateway restart needed";
  6. ready without an assignee: lint I4 of board_lint (an alert, no automatic
     fix). The stock stranded_in_ready skips an empty assignee on purpose
     (kanban_diagnostics, skipped_unassigned), and skipped_unassigned is a
     field of the dispatcher tick that nobody delivers: owner review cards once
     waited 30 hours for an assignee. The watchdog does not fill in an
     assignee: cards must be created with one (the creator picks the profile),
     and a silent kanban.default_assignee would break profile routing. The
     gate beacon is excluded by construction: the watchdog blocks it with
     needs_input itself when it creates it (_park_beacon);
  6b. review layer: findings about open PR heads and the live review lane;
     cards are no longer armed per head (one PR = one card);
  7. subscription cleanup: card events (crashed, timed_out, gave_up, blocked,
     completed, review) are delivered by the stock gateway notifier through
     subscriptions; the watchdog subscribes the owner to open cards created
     without a session anchor (crons, gates); the report carries only what
     the events do not: diag aggregates (thresholds, not transitions),
     silence, a dead dispatcher, gates;
  8. gates and lints that keep the board honest: admission by milestone and
     owner approval, milestone priority bands, an executor card for every
     issue of the active milestone (issue_executor_poll), no decomposition
     of a card whose PR is already delivered, the tail of a closed issue and
     duplicate chains, rework priority and stale rework or review arms, the
     owner-merge hold of a closed PR card, stuck initial_status blocks of
     accepted plans, guarded ready cards, an idle factory with a non-empty
     backlog, plus the gates listed in main() (rework size, verdict dedup,
     conflict debt, backlog, prototype first, idempotency keys, board lint).

Lessons for future watchdog rules (grows over time):
  - the absence of movement on a card that waits for a dependency or an owner
    decision is not a sign that it went stale: a todo card with an open parent
    or a live child is "without movement" by definition; auto-archive may touch
    only cards without live links (parents and children not in done/archived;
    a scheduled parking by an owner gate is a live link, backlog_p0_stale);
  - the archive is terminal for the CLI (there is no kanban unarchive), the only
    supported way back is a dashboard PATCH status (drag out of the archive).
    Before a rule that archives, answer: how is it rolled back.

PR/issue numbers are pulled from the root cards, their state is asked from
GitHub (gh), and the plan is given from simple to complex: first what is
already ready to merge and unblocks the most, at the end what needs thinking.

Dedup: the same problem does not shout again more often than once a day.

Native-or-live rule (owner rule): before creating a card, reassigning,
restoring or removing anything on the board, check that a stock Hermes
mechanism or a live card does not already do it. The single helper
board_native_guard.native_or_live is mandatory for every card generator and
every reassignment; the full rule text and the list of stock paths live in
board_native_guard.py (RULE_50B) and the kanban-discipline skill. Violations
are caught by test_board_native_guard.py (statically) and by _kanban_create
(it requires a GuardNote).
"""
import calendar
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from factory_config import cfg, cfg_list, hermes_home  # noqa: E402

# One definition of progress after a burnout: it lives in metrics-evening.py
# (the factory metrics are gathered there), so metric 5 of the summary and the
# rework size gate count it the same way.
# The file name has a hyphen, so it is loaded through importlib; for isolated
# copies (mutation test runs) an equivalent stub remains.
# CYCLE GUARD: the key "hermes_sibling:<path>" is shared by all scripts/*, and
# the module is registered BEFORE exec; without it metrics-evening <->
# pr-hygiene <-> board-watchdog close into an endless mutual exec recursion
# (gigabytes of memory, OOM).
import importlib.util as _ilu

_FM_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "metrics-evening.py")
if os.path.exists(_FM_PATH):
    _fm_key = "hermes_sibling:" + os.path.abspath(_FM_PATH)
    _fm = sys.modules.get(_fm_key)
    if _fm is None:
        _fm_spec = _ilu.spec_from_file_location("factory_metrics_wd", _FM_PATH)
        _fm = _ilu.module_from_spec(_fm_spec)
        sys.modules[_fm_key] = _fm
        _fm_spec.loader.exec_module(_fm)
else:                              # a copy of the script outside scripts: mutants
    import types as _types
    _fm = _types.ModuleType("factory_metrics_stub")
    _fm.BURN_PROGRESS_MARKER = "Watchdog: progress after burnout"
    _fm.SYSTEM_COMMENT_RE = re.compile(
        r"^(BLOCKED:|Watchdog:|SCHEDULED:|UNBLOCKED:)")

    def _stub_closes(body):
        if (body or "").startswith(_fm.BURN_PROGRESS_MARKER):
            return True
        return not _fm.SYSTEM_COMMENT_RE.match(body or "")
    _fm.closes_burnout_progress = _stub_closes

# Milestone priority bands are computed from GitHub on every run. The logic
# lives in milestone_bands.py, the single definition for board-watchdog and
# pr-hygiene (static per-script tables silently drifted apart at every
# release). Here only the sibling is loaded (same cycle guard, the key
# "hermes_sibling:<path>" is shared by all scripts/*) and thinly wrapped
# through the local run() where it is used. A copy of the script outside
# scripts (mutation runs) has no sibling and no bands; consumers treat that
# as "no data".
_MB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "milestone_bands.py")
if os.path.exists(_MB_PATH):
    _mb_key = "hermes_sibling:" + os.path.abspath(_MB_PATH)
    milestone_bands = sys.modules.get(_mb_key)
    if milestone_bands is None:
        _mb_spec = _ilu.spec_from_file_location("milestone_bands", _MB_PATH)
        milestone_bands = _ilu.module_from_spec(_mb_spec)
        sys.modules[_mb_key] = milestone_bands
        _mb_spec.loader.exec_module(milestone_bands)
else:
    import types as _mbtypes
    milestone_bands = _mbtypes.ModuleType("milestone_bands_stub")
    milestone_bands.ACTIVE_BAND = 80
    milestone_bands.NEXT_BAND = 40
    milestone_bands.OTHER_BAND = 20
    milestone_bands.resolve = lambda repo, fetch=None: {
        "active": None, "next": None, "bands": {}, "source": "nodata"}

HOME = hermes_home()
BOARDS = os.path.join(HOME, "kanban", "boards")

# The single "is it already stock or live?" helper for all card generators
# and reassignments. The full owner rule: board_native_guard.py (RULE_50B)
# and the kanban-discipline skill. _kanban_create requires a GuardNote
# receipt by type; test_board_native_guard.py fails the suite on a generator
# that skips the helper.
try:
    _bng_spec = _ilu.spec_from_file_location(
        "board_native_guard",
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "board_native_guard.py"))
    if _bng_spec is None or _bng_spec.loader is None:
        raise RuntimeError("board_native_guard: spec could not be built")
    _existing = sys.modules.get("board_native_guard")
    if _existing is not None and hasattr(_existing, "native_or_live"):
        # one copy of the module for all scripts: a single GuardNote class
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
STATE = os.path.join(HOME, "kanban", "watchdog-state.json")
HERMES = os.path.expanduser("~/.local/bin/hermes")
PROFILES_ROOT = os.path.join(HOME, "profiles")
# Mirror of hermes_constants._PROFILE_IDENTITY_MARKERS: what makes a profile
# different from a random folder under profiles/.
_PROFILE_IDENTITY_MARKERS = ("config.yaml", ".env", "SOUL.md",
                             "profile.yaml", "auth.json", "state.db")
# The only consumer is lint I4 (alert on ready without an assignee). The
# stock stranded_in_ready skips an empty assignee on purpose, and nobody
# delivers the dispatcher's skipped_unassigned.
UNASSIGNED_AFTER = 30 * 60   # ready without a spawnable assignee for longer than this: alert
SILENCE_HOURS = 6
REPEAT_AFTER = 24 * 3600
# Owner rule: an eternal block without "Release condition:" is a question to
# the owner ONCE, or the archive; the watchdog must not repeat the same line
# for weeks. staleblock repeats at most once a week.
STALEBLOCK_REPEAT = 7 * 24 * 3600
OWNERQ_AFTER_H = 24   # a question to the owner older than a day must reach
                      # the "needs attention" report
MAX_CHARS = 3600           # Telegram cuts at 4096, leave room for the cron header
LOG = os.path.join(HOME, "logs", "gateway.log")
OPEN = ("todo", "triage", "ready", "running", "blocked", "scheduled", "review")
QUEUED = ("todo", "triage")
MOVING = ("running",)
HELD = ("blocked", "scheduled")


def run(cmd, timeout=120):
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return done.stdout.strip()
    except Exception:
        return ""


# memory_pressure was removed. It was dead code (no calls), and the stock
# respawn guard measures memory pressure itself (_memory_pressure_level,
# kanban_db_dispatch.py) and limits spawns (the guard in _dispatch_tick).
# There were no tests for the function.


def board_slugs():
    if not os.path.isdir(BOARDS):
        return []
    return sorted(d for d in os.listdir(BOARDS)
                  if os.path.exists(os.path.join(BOARDS, d, "kanban.db")))


def board_repo(slug):
    """owner/repo of a board: from default_workdir of its board.json via git remote."""
    meta = os.path.join(BOARDS, slug, "board.json")
    if not os.path.exists(meta):
        return None
    try:
        workdir = json.load(open(meta)).get("default_workdir")
    except Exception:
        return None
    if not workdir or not os.path.isdir(workdir):
        return None
    url = run(["git", "-C", workdir, "remote", "get-url", "origin"], timeout=15)
    match = re.search(r"github\.com[:/](.+?)(?:\.git)?$", url)
    return match.group(1) if match else None


def graph(slug):
    """Tasks, links and the time of the last run. Read only: the dispatcher writes to the same database."""
    db = os.path.join(BOARDS, slug, "kanban.db")
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
    tasks = {}
    for row in con.execute("select id, status, title, assignee, body from tasks"):
        tasks[row[0]] = {"status": row[1], "title": row[2] or "",
                         "assignee": row[3] or "-", "body": row[4] or ""}
    kids, blockers = {}, {}
    for parent, child in con.execute("select parent_id, child_id from task_links"):
        kids.setdefault(parent, []).append(child)
        blockers.setdefault(child, []).append(parent)
    last_end = con.execute(
        "select max(ended_at) from task_runs where ended_at is not null").fetchone()[0]
    recent = list(con.execute(
        "select outcome from task_runs where started_at > ?", (time.time() - 6 * 3600,)))
    # A card can legitimately stand blocked: it waits not for another card but
    # for an external event (a merged PR, a human's answer, a release of another
    # project). Dependencies cannot express that, and the CLI does not change the
    # kind of a block after the fact, so the condition is written in words in a
    # comment and read from here.
    # The plan review gate blocks the children of a root with the phrase
    # "awaiting plan review" and removes the block itself on the ACCEPTED
    # verdict. It must not be removed when the review card completes: the
    # RETURN verdict completes it too, and the work would go past the repeated
    # review. So this is a legitimate wait, not a forgotten card.
    waited = set()
    for task_id, body in con.execute("select task_id, body from task_comments"):
        low = (body or "").lower()
        if "release condition" in low or "awaiting plan review" in low:
            waited.add(task_id)
    con.close()
    return tasks, kids, blockers, last_end, waited, recent


def owner_question_pending(slug, tasks, waited, now=None):
    """A blocked card with no comment at all for a day.

    Why the hole existed: a plan review gate stood blocked for two weeks with an
    open question to the owner and never reached the "KANBAN NEEDS ATTENTION"
    report. The stock _rule_stuck_in_blocked (hermes_cli/kanban_diagnostics.py)
    resets its stale timer on ANY commented event after the block, and the
    reviewer kept appending verdict corrections to the blocked card. The core is
    not edited (changes of the layer live in scripts/), so the rule here does not
    look at "were there comments after the block" but at the age of the last
    one: a day of silence on a blocked card means nobody closed its question.

    The exception is the same as before (a sanctioned wait for an external
    event): cards with "Release condition:" or "awaiting plan review" in any
    comment (waited from graph()).
    """
    if now is None:
        now = time.time()
    db = os.path.join(BOARDS, slug, "kanban.db")
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
    try:
        last = {}
        # The report carries the gist of the question (an excerpt of the last
        # comment), not only the card number.
        for task_id, ts, body in con.execute(
                "select task_id, created_at, body from task_comments"
                " order by created_at"):
            last[task_id] = (float(ts), body or "")
    finally:
        con.close()
    rows = []
    for task_id in sorted(tasks):
        task = tasks[task_id]
        if task["status"] != "blocked" or task_id in waited:
            continue
        last_ts = last.get(task_id)
        if last_ts is None or now - last_ts[0] < OWNERQ_AFTER_H * 3600:
            continue
        rows.append((task_id, task, last_ts))
    # Fresh questions first: when the report is cut at MAX_CHARS it drops the
    # ancient debt, not the question that hung since yesterday.
    rows.sort(key=lambda r: r[2])
    out = []
    for task_id, task, (last_ts, last_body) in rows:
        age_h = int((now - last_ts) / 3600)
        out.append((
            "ownerq:%s:%s" % (slug, task_id), 1,
            "  Question to the owner unanswered for %d h: %s [%s] %s\n"
            "    Last comment %s UTC. The core (stuck_in_blocked)\n"
            "    is reset by any comment, so it is caught here.\n"
            "    Gist: %s\n"
            "    Unblock on the merits, escalate, or write\n"
            "    \"Release condition: ...\" for an external wait."
            % (age_h, task_id, task["assignee"], task["title"],
               datetime.fromtimestamp(last_ts, timezone.utc)
               .strftime("%Y-%m-%d %H:%M"),
               " ".join(last_body.split())[:160])))
    return out


def waiting_on(root, tasks, kids):
    """Open cards that wait for this root directly or through a chain."""
    seen, queue = set(), list(kids.get(root, []))
    while queue:
        node = queue.pop()
        if node in seen or node not in tasks:
            continue
        if tasks[node]["status"] not in OPEN:
            continue
        seen.add(node)
        queue.extend(kids.get(node, []))
    return seen


# --- A card whose PR is already delivered is not decomposed -------------

_PR_REF_RE = re.compile(r"PR #(\d+)")


def _pr_refs_before(db_path, task_id, before_ts):
    """PR numbers mentioned in a comment or the body of a card before before_ts.

    A "PR #N" link in a worker's checkpoint comment records delivery: the
    work already lives in a PR and the card body is stale by then. Links
    like "issue #N" do not count (issue mirrors are legitimate).
    """
    refs = set()
    con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=10)
    try:
        rows = con.execute(
            "select body from task_comments"
            " where task_id = ? and created_at <= ?",
            (task_id, before_ts)).fetchall()
        row = con.execute("select body from tasks where id = ?",
                          (task_id,)).fetchone()
        if row and row[0]:
            rows.append((row[0],))
    finally:
        con.close()
    for (body,) in rows:
        refs.update(int(m) for m in _PR_REF_RE.findall(body or ""))
    return refs


def _last_routing_before(db_path, task_id, before_ts):
    """The latest routing event of a card before before_ts.

    block_loop_detected means "returned to triage through a block loop",
    review_requested/changes_requested/completed are the review cycle. How
    the card ended before the decomposition decides whether it is legitimate.
    """
    kinds = ("block_loop_detected", "review_requested", "changes_requested",
             "completed")
    con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=10)
    try:
        row = con.execute(
            "select kind, created_at from task_events where task_id = ?"
            " and created_at < ? and kind in (%s)"
            " order by created_at desc limit 1" % ",".join("?" * len(kinds)),
            (task_id, before_ts) + kinds).fetchone()
    finally:
        con.close()
    return (row[0], float(row[1])) if row else None


def decomposed_after_delivery(slug, now=None):
    """A card whose PR is delivered does not go back to triage and is not
    decomposed; the continuation after delivery is a new card for the
    remainder, with a body checked against the PR.

    The failure chain: a worker blocked a card twice with kind=transient
    ("run budget exhausted"), _route_block (hermes_cli/kanban_db.py) moved it
    to triage at block_recurrences >= LIMIT (event block_loop_detected), and
    the gateway's auto_decompose_tick (kanban.auto_decompose) decomposed it
    minutes later from the STALE body of the issue mirror: the children
    repeated work already delivered in a PR. The core is not edited; the
    watchdog catches the transition from board events:

    1) decomposed on a card preceded by block_loop_detected and a "PR #N"
       link: the children come from a stale body. Action: archive the
       duplicate children with a comment, the remainder as a new card checked
       against the PR, close the root with the result "PR #N".
    2) a card in triage after block_loop_detected with a "PR #N" link: the
       next auto_decompose tick will decompose it the same way. Action: close
       the root with the result "PR #N", the remainder as a new card.

    The review cycle (changes_requested before decomposed) is legitimate:
    rework packages are decomposed by verdict this way, and a PR link is
    expected there. The gate is silent once the root is closed and no live
    children remain.
    """
    if now is None:
        now = time.time()
    db = os.path.join(BOARDS, slug, "kanban.db")
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
    try:
        decomposed = list(con.execute(
            "select task_id, created_at, payload from task_events"
            " where kind = 'decomposed' order by created_at"))
        statuses = dict(con.execute("select id, status from tasks"))
    finally:
        con.close()
    found = []

    for task_id, ts, payload in decomposed:
        ts = float(ts)
        live = []
        try:
            child_ids = json.loads(payload or "{}").get("child_ids") or []
        except Exception:
            child_ids = []
        for kid in child_ids:
            if statuses.get(kid) not in ("done", "archived"):
                live.append(kid)
        if statuses.get(task_id) in ("done", "archived") and not live:
            continue  # already sorted out, history must not shout forever
        routing = _last_routing_before(db, task_id, ts)
        if not routing or routing[0] != "block_loop_detected":
            continue  # legitimate decomposition (review cycle or specifier)
        refs = _pr_refs_before(db, task_id, ts)
        if not refs:
            continue
        pr_list = ", ".join("#%d" % r for r in sorted(refs))
        found.append((
            "decpr:%s:%s" % (slug, task_id), 1,
            "  Decomposed after block_loop with a delivered PR %s: %s [%s]\n"
            "    The auto-decomposer created the children from a stale body.\n"
            "    Live children: %s.\n"
            "    Action: archive the duplicate children with a comment, the\n"
            "    remainder as a new card with a body checked against the PR,\n"
            "    close the root with the result \"PR %s\"."
            % (pr_list, task_id, statuses.get(task_id, "?"),
               ", ".join(live) or "none", pr_list)))

    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
    try:
        loops = {}
        for task_id, ts in con.execute(
                "select task_id, created_at from task_events"
                " where kind = 'block_loop_detected'"):
            if task_id not in loops or ts > loops[task_id]:
                loops[task_id] = float(ts)
    finally:
        con.close()
    for task_id, loop_ts in sorted(loops.items()):
        if statuses.get(task_id) != "triage":
            continue
        routing = _last_routing_before(db, task_id, now)
        if not routing or routing[0] != "block_loop_detected":
            continue  # a review event followed triage, the path is legitimate
        refs = _pr_refs_before(db, task_id, loop_ts)
        if not refs:
            continue
        pr_list = ", ".join("#%d" % r for r in sorted(refs))
        found.append((
            "prloop:%s:%s" % (slug, task_id), 1,
            "  In triage after block_loop with a delivered PR %s: %s [%s]\n"
            "    The next auto_decompose tick will decompose it from a stale\n"
            "    body. Action: close the root with the result \"PR %s\",\n"
            "    the remainder as a new card checked against the PR, do not decompose."
            % (pr_list, task_id, statuses.get(task_id, "?"), pr_list)))

    return found


def diagnostics(slug):
    out = run([HERMES, "kanban", "--board", slug, "diag", "--json", "--severity", "warning"])
    try:
        return json.loads(out) if out else []
    except Exception:
        return []


# --- The subscription sweep was rolled back ------------------------------
# Reason for the rollback: the stock subscription is per task and has no filter
# by event kind (gateway/kanban_watchers_notifier.py delivers all
# TERMINAL_KINDS: completed, review_requested, changes_requested, status,
# archived, unblocked, etc.), and alerts to the owner only about problems
# cannot be made with stock means (suppress_warning_notifications silences only
# the diagnostic ones, gateway/warning_notifications.py). The sweep subscribed
# over a hundred cards in one run, and each would have pinged on any board
# event. Delivery of problems stays with the watchdog: the report of check()
# to the owner chat.


def pr_state(repo, number):
    """State of one PR: an exact mergeable needs view, list lies with UNKNOWN."""
    fields = "number,title,url,state,isDraft,baseRefName,mergeable,mergeStateStatus,statusCheckRollup"
    out = run(["gh", "pr", "view", str(number), "--repo", repo, "--json", fields], timeout=60)
    if not out:
        return None
    try:
        data = json.loads(out)
    except Exception:
        return None
    checks = [c.get("conclusion") or c.get("state") for c in (data.get("statusCheckRollup") or [])]
    failed = [c for c in checks if c in ("FAILURE", "TIMED_OUT", "CANCELLED", "ERROR")]
    pending = [c for c in checks if c in ("PENDING", "IN_PROGRESS", "QUEUED", None)]
    if data.get("state") == "MERGED":
        verdict, rank = "merged", 0
    elif data.get("state") == "CLOSED":
        verdict, rank = "closed", 0
    elif failed:
        verdict, rank = "CI red (%d)" % len(failed), 3
    elif pending:
        verdict, rank = "CI still running", 2
    elif data.get("mergeable") == "CONFLICTING":
        verdict, rank = "conflict, rebase needed", 2
    elif data.get("mergeStateStatus") in ("CLEAN", "UNSTABLE", "HAS_HOOKS"):
        verdict, rank = "ready to merge", 1
    else:
        verdict, rank = "not ready: %s" % data.get("mergeStateStatus"), 2
    return {"number": number, "url": data.get("url"), "base": data.get("baseRefName"),
            "title": data.get("title") or "", "verdict": verdict, "rank": rank,
            "draft": data.get("isDraft")}


def refs_in(task):
    """PR/issue numbers mentioned in a card."""
    text = task["title"] + "\n" + task["body"]
    return sorted({int(n) for n in re.findall(r"#(\d{1,5})\b", text)})


def runtime_cap_hits(slug):
    """Cards that hit the same time ceiling twice or more."""
    db = os.path.join(BOARDS, slug, "kanban.db")
    if not os.path.exists(db):
        return []
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
    since = time.time() - 48 * 3600
    runs = {}
    for task_id, started, ended in con.execute(
            "select task_id,started_at,ended_at from task_runs "
            "where outcome in ('timed_out','gave_up') and ended_at is not null "
            "and started_at > ?", (since,)):
        runs.setdefault(task_id, []).append((float(ended) - float(started)) / 60)
    out = []
    for task_id, spans in runs.items():
        if len(spans) < 2:
            continue
        spans.sort()
        median = spans[len(spans) // 2]
        if not median:
            continue
        tight = [s for s in spans if abs(s - median) <= 0.1 * median]
        if len(tight) < 2:
            continue
        row = list(con.execute(
            "select title,status,max_runtime_seconds from tasks where id=?", (task_id,)))
        if not row:
            continue
        title, status, cap = row[0]
        if status in ("done", "archived"):
            continue
        out.append((task_id, title or "", len(tight), median, cap))
    con.close()
    return out


def budget_exhausted(slug):
    """Open cards that burned the iteration budget and were not moved to goal mode."""
    db = os.path.join(BOARDS, slug, "kanban.db")
    if not os.path.exists(db):
        return []
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
    since = time.time() - 48 * 3600
    hits = {}
    for task_id, error in con.execute(
            "select task_id, coalesce(error,'') from task_runs where started_at > ?", (since,)):
        if "Iteration budget" in error or "budget exhausted" in error:
            hits[task_id] = hits.get(task_id, 0) + 1
    out = []
    for task_id, times in hits.items():
        row = list(con.execute(
            "select title,status,coalesce(goal_mode,0) from tasks where id=?", (task_id,)))
        if not row:
            continue
        title, status, goal = row[0]
        if status in ("done", "archived") or goal:
            continue
        out.append((task_id, title or "", times, status))
    con.close()
    return out


def consecutive_budget_gave_up(slug):
    """The tail of consecutive budget burnouts on an open card.

    Signal: two OR MORE consecutive runs with a budget burnout ('Iteration
    budget' / 'budget exhausted' in error) with no completed run between them.
    One burned run is the signal "the card is big" (budget_exhausted relays it);
    the second in a row is proof: a retry does not fit the same budget, and the
    dispatcher would burn a third run on the same wall.

    Returns (task_id, title, status, tail_len, goal_mode, block_kind) for cards
    where tail >= 2, the card is open and not in goal mode. Cards with
    block_kind='needs_input' already stand in the gate: they are not returned.

    Precedent: several cards burned 200/200 iterations (1.4-1.8 h) on the first
    run; the second run reached review only thanks to the commits of the first.
    A card with a package of 9+ owner review findings does not fit one run.
    """
    db = os.path.join(BOARDS, slug, "kanban.db")
    if not os.path.exists(db):
        return []
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
    since = time.time() - 7 * 24 * 3600
    # A manual unblock gives no immunity, but it must not turn into a war of
    # "a human unblocked, the watchdog blocked again on the next tick from the
    # old runs": the series is counted from the last unblocked event. Two NEW
    # burnouts after an unblock are a repeated block, exactly as in the gate
    # specification (worker-run-discipline).
    unblocked_at = {}
    for task_id, created in con.execute(
            "select task_id, max(created_at) from task_events "
            "where kind='unblocked' group by task_id"):
        unblocked_at[task_id] = float(created)
    by_task = {}
    for task_id, outcome, error, started in con.execute(
            "select task_id, outcome, coalesce(error,''), started_at from "
            "task_runs where started_at > ? order by task_id, started_at",
            (since,)):
        if started and task_id in unblocked_at and float(started) <= unblocked_at[task_id]:
            continue
        burned = outcome in ("gave_up", "timed_out") and (
            "Iteration budget" in error or "budget exhausted" in error)
        # A completed run between burnouts resets the series: the work reached
        # hand-in. Other outcomes (crashed, blocked, review_requested...) do
        # not extend the series, but do not break it either: burnout -> crash
        # -> burnout is the same card that cannot be finished.
        if outcome == "completed":
            entry = False
        elif burned:
            entry = True
        else:
            continue
        by_task.setdefault(task_id, []).append(entry)

    out = []
    for task_id, marks in by_task.items():
        tail = 0
        for mark in reversed(marks):
            if mark:
                tail += 1
            else:
                break
        if tail < 2:
            continue
        row = list(con.execute(
            "select title, status, coalesce(goal_mode,0), "
            "coalesce(block_kind,'') from tasks where id=?", (task_id,)))
        if not row:
            continue
        title, status, goal, block_kind = row[0]
        if status in ("done", "archived") or goal:
            continue
        if block_kind == "needs_input":
            continue  # already in the gate, do not block again
        out.append((task_id, title or "", status, tail, goal, block_kind))
    con.close()
    return out


def output_stalled(slug, repo):
    """There are runs but no pull requests in the window. Returns text or None."""
    if not repo:
        return None
    db = os.path.join(BOARDS, slug, "kanban.db")
    if not os.path.exists(db):
        return None
    window = 6 * 3600
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
    since = time.time() - window
    runs = list(con.execute(
        "select count(*) from task_runs where started_at > ? and outcome = 'completed'",
        (since,)))[0][0]
    queue = list(con.execute(
        "select coalesce(assignee,'-'), count(*) from tasks "
        "where status in ('ready','todo') group by 1 order by 2 desc"))
    con.close()
    if runs < 4:
        return None

    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - window))
    # The signal window is set by an exact time. In a GitHub search ">date"
    # means "after that whole day", so cutting the stamp to stamp[:10] threw out
    # the whole current UTC day: on a day with merges the watchdog saw zero PRs
    # and shouted "nothing released" all day. An exact stamp with >= matches in
    # meaning the local updatedAt filter below. --limit is explicit: without it
    # gh silently returns at most 30.
    out = run(["gh", "pr", "list", "--repo", repo, "--state", "all",
               "--limit", "200",
               "--search", "updated:>=%s" % stamp,
               "--json", "number,updatedAt"], timeout=90)
    try:
        touched = len([p for p in json.loads(out or "[]")
                       if p.get("updatedAt", "") >= stamp])
    except Exception:
        # Could not ask GitHub: stay silent so as not to shout blindly.
        return None
    if touched:
        return None
    plan = sum(n for who, n in queue if who == "reviewer")
    work = sum(n for who, n in queue if who in ("coder", "tester"))
    lines = [
        "    The pipeline is working but releases nothing.",
        "      In 6 hours %d runs completed successfully, yet no PR was created or updated." % runs,
        "      Queue: planning %d cards, work %d." % (plan, work),
        "      Check in this order:",
        "        1. priorities: hermes kanban --board %s list --json | grep priority" % slug,
        "           (code must go ahead of plan reviews, the priority-normalizer cron job);",
        "        2. gate escalations: cards with an ESCALATION comment wait for a human decision;",
        "        3. blocked cards that a red CI depends on.",
    ]
    return "\n".join(lines)


# dependency_wait_loops was removed. It caught cards spinning on a dependency
# block with finished parents (14 spawns in an hour and a half). Since v0.23.1
# the core handles this at the block itself: block_task moves a
# kind='dependency' block without open parents to needs_input (sticky)
# immediately (hermes_cli/kanban_db.py, recompute_ready no longer raises such a
# card), and block/unblock cycles reach triage through BLOCK_RECURRENCE_LIMIT
# (hermes_cli/kanban_db.py: _route_block). Cycle diagnostics are stock:
# kanban_diagnostics.py block_unblock_cycling/stranded_in_ready.
# There were no tests for the detector.


def profile_live(name):
    """Mirror of hermes_constants.named_profile_is_live: a profile that the
    dispatcher can really raise as ``hermes -p <assignee>``."""
    if name == "default":
        return True
    home = os.path.join(PROFILES_ROOT, name)
    if not os.path.isdir(home):
        return False
    if os.path.exists(os.path.join(PROFILES_ROOT, ".deleted", name)):
        return False
    return any(os.path.isfile(os.path.join(home, m)) or
               os.path.islink(os.path.join(home, m))
               for m in _PROFILE_IDENTITY_MARKERS)


def idle_factory_lint(slug, st, tasks, blockers, now=None):
    """The factory stands still.

    Case: running 0 and ready 0 for hours while there was a backlog of the
    admissible band (cards parked by the review layer, which a single
    non-admitted PR head kept locked). The existing checks were silent:
    stalled fired once and was deduplicated, silent needs 6 h without runs.
    Owner rule: idle for more than an hour with a non-empty backlog of the
    admissible band is ONE finding for the owner.

    The admissible backlog is work that would run if no foreign gate or
    parking held it: (a) todo without open blockers (the dispatcher had to
    take it; any band: if it does not take p20 for an hour, that is idle
    too), (b) scheduled cards parked by the review layer itself (marker in
    the last scheduled reason: the return must happen, otherwise the layer
    is locked) and (c) open issues of the active milestone without a live
    card (active_milestone_orphans): a change of the active milestone after
    a release leaves a backlog even when the board has no cards at all.
    Admission parkings do NOT count: that is a deliberate wait outside the
    active milestone, not idling.

    st is mutable: the key "idlefactory:<slug>" holds {"since": epoch} from
    the first observation of idling; no finding earlier than an hour. The
    dedup key is per day: at most one push a day, no noise.
    """
    now = now or time.time()
    moving = [i for i, t in tasks.items() if t["status"] == "running"]
    ready = [i for i, t in tasks.items() if t["status"] == "ready"]
    startable = [i for i, t in tasks.items()
                 if t["status"] == "todo"
                 and not [b for b in blockers.get(i, ())
                          if b in tasks
                          and tasks[b]["status"] not in ("done",
                                                         "archived")]]
    db = os.path.join(BOARDS, slug, "kanban.db")
    parked = []
    orphans = []
    if not moving and not ready:
        try:
            con = sqlite3.connect("file:%s?mode=ro" % db, uri=True,
                                  timeout=10)
            for tid, payload in con.execute(
                    "SELECT t.id, (SELECT e.payload FROM task_events e "
                    "WHERE e.task_id = t.id AND e.kind = 'scheduled' "
                    "ORDER BY e.id DESC LIMIT 1) FROM tasks t "
                    "WHERE t.status = 'scheduled'"):
                if payload and REVIEW_PARK_MARKER in payload:
                    parked.append(tid)
            con.close()
        except sqlite3.Error:
            parked = []
        # No startable cards at all (every todo is held by open parents,
        # parked is empty), yet there is work: open issues of the active
        # milestone without a live card. That is the admissible backlog: a
        # change of the active milestone after a release once left several
        # issues without an executor while the watchdog stayed silent all
        # night.
        if not (startable or parked):
            orphans = active_milestone_orphans(slug)
    if moving or ready or not (startable or parked or orphans):
        st.pop("idlefactory:" + slug, None)
        return []
    key_state = "idlefactory:" + slug
    since = (st.get(key_state) or {}).get("since")
    if not since:
        st[key_state] = {"since": now}
        return []
    hours = (now - since) / 3600.0
    if hours < 1.0:
        return []
    key = "%s:idlefactory:%s" % (slug, time.strftime("%Y-%m-%d", time.gmtime(now)))
    tail = []
    for tid in (startable + parked)[:5]:
        t = tasks.get(tid, {})
        tail.append("    %s [%s, p%s, %s] %s" % (
            tid, t.get("assignee", "-"), t.get("priority", "?"),
            t.get("status", "?"), (t.get("title") or "")[:60]))
    for n in orphans[:5]:
        tail.append("    issue #%d (active milestone, no live"
                    " card)" % n)
    body = [
        "  The factory has stood for %.1f h: running 0, ready 0, and the backlog is not empty." % hours,
        "  Work that should be running (admissible band):",
    ] + tail + [
        "  The dispatcher has nothing to take: check parkings and gates",
        "  (hermes kanban ls --board %s)." % slug,
    ]
    return [(key, 0, "\n".join(body))]


def check(slug, state=None):
    """List of (dedup_key, text) for one board."""
    st = state if state is not None else _load_state()
    tasks, kids, blockers, last_end, waited, recent = graph(slug)
    repo = board_repo(slug)
    found = []
    cache = {}

    def describe(numbers):
        lines, ranks = [], []
        for number in numbers:
            if number not in cache:
                cache[number] = pr_state(repo, number) if repo else None
            info = cache[number]
            if not info:
                continue
            ranks.append(info["rank"])
            mark = {0: "  ", 1: "OK", 2: "!!", 3: "XX"}[info["rank"]]
            lines.append("      %s #%s %s [%s] %s\n         %s" % (
                mark, info["number"], info["verdict"], info["base"],
                info["title"], info["url"]))
        return lines, (max(ranks) if ranks else 9)

    # Checks that became stock were removed (the rule: what becomes stock is
    # deleted): dispatcher heartbeat, rate_limited storm, repeated timed_out,
    # repeated long failures, block cycles, ready-without-assignee and
    # duplicate spawn. v0.23.1 covers them natively
    # (kanban_diagnostics.py: stranded_in_ready + repeated_failures with
    # kanban.failure_limit, block_unblock_cycling, stuck_in_blocked; the respawn
    # guard in kanban_db_dispatch.py:check_respawn_guard: rate_limit_cooldown,
    # active_pr, recent_success; the dispatcher itself writes "kanban dispatcher
    # stuck" to the log). Board diagnostics already run through the stock engine
    # via diagnostics() below and are delivered by this same watchdog.

    # 6. Lint of board invariants I1-I6: I4 is the ready-without-assignee
    #     check, the others fix or shout by themselves. gh or code errors do
    #     not bring down the other checks.
    try:
        found.extend(board_lint(slug))
    except Exception as exc:
        found.append(("%s:lint:err" % slug, 1, (
            "    Board lint failed (%s: %s). Retry in an hour."
            % (type(exc).__name__, exc))))

    # A question to the owner older than a day must reach the "needs
    # attention" report. The core (stuck_in_blocked) resets its timer on any
    # comment, so a gate once lived for two weeks unnoticed.
    try:
        found.extend(owner_question_pending(slug, tasks, waited))
    except Exception as exc:
        found.append(("%s:ownerq:err" % slug, 1, (
            "    Questions to the owner: the check failed (%s: %s). Retry in an hour."
            % (type(exc).__name__, exc))))

    # Children inherit the priority of a live parent (the core
    # _insert_decomposed_child sets 0, the layer compensates).
    try:
        found.extend(ancestor_priority_lint(slug, kids))
    except Exception as exc:
        found.append(("%s:i7:err" % slug, 1, (
            "    I7 ancestor priority: the check failed (%s: %s). Retry in an hour."
            % (type(exc).__name__, exc))))

    # A map of open PR overlaps by file after every merge, one decision
    # "rebase needed because of #N" per PR.
    try:
        found.extend(pr_overlap_map(slug, st))
    except Exception as exc:
        found.append(("%s:overlap:err" % slug, 1, (
            "    PR overlap map: the check failed (%s: %s). Retry in an hour."
            % (type(exc).__name__, exc))))

    # The admission gate on a whitelist. Only cards linked to an approved issue
    # (milestone assigned), their descendants, service PR cards and
    # infrastructure go into work; the rest is taken off the queue and shows up
    # in the report.
    try:
        found.extend(admission_gate(slug))
    except Exception as exc:
        found.append(("%s:admission:err" % slug, 1, (
            "    Admission gate failed (%s: %s). Retry in an hour."
            % (type(exc).__name__, exc))))

    # 6b. Review layer: only the rework arm for a CHANGES REQUESTED verdict
    #     without a rework card, and the finding about active-milestone PRs
    #     without a card. The review and WCFY auto-arms were removed (one
    #     PR = one card).
    try:
        found.extend(review_layer_check(slug))
    except Exception as exc:
        found.append(("%s:revlayer:err" % slug, 1, (
            "    Review layer: the check failed (%s: %s). Retry in an hour."
            % (type(exc).__name__, exc))))

    # 6c. Size gate for rework cards: ready+coder with 3+ BLOCK or a body of
    #     more than 25 lines is reassigned to the architect, and a decomposition
    #     with the key decompose-<task_id> is created. Idempotent.
    try:
        found.extend(rework_oversize_gate(slug))
    except Exception as exc:
        found.append(("%s:reworkgate:err" % slug, 1, (
            "    Rework size gate: the check failed (%s: %s). Retry in an hour."
            % (type(exc).__name__, exc))))

    # 6c-bis. Verdict item dedup gate: a card made from an owner verdict
    #         subtracts the items already covered by a live card of the same PR
    #         (visual-delta, update-branch, and the like), and a line "item N
    #         is covered by t_..." stays in the body. A frequent pass for fresh
    #         cards: --rework-gate-only (cron every 5 minutes).
    try:
        found.extend(verdict_dedup_gate(slug))
    except Exception as exc:
        found.append(("%s:verdict-dedup:err" % slug, 1, (
            "    Verdict dedup: the check failed (%s: %s). Retry in an hour."
            % (type(exc).__name__, exc))))

    # 6c-3. Merge-unblocker priority: a ready+coder card for a PR whose code
    #       the owner accepted, with only merge formalities left in the items
    #       and no live code blockers of the same PR, is raised to p90 and is
    #       taken before ordinary work at p80. A frequent pass:
    #       --rework-gate-only (cron every 5 minutes).
    try:
        found.extend(merge_unblocker_gate(slug))
    except Exception as exc:
        found.append(("%s:merge-unblocker:err" % slug, 1, (
            "    Merge-unblocker: the check failed (%s: %s). Retry in an hour."
            % (type(exc).__name__, exc))))

    # 6c-3-ter. Close gate "remaining: owner merge": a closed PR card with an
    #           unmerged PR keeps the signal until the merge (one PR = one
    #           card). The same function holds a closed PR card whose PR has
    #           an accept verdict on the head and no fresh WCFY.
    try:
        found.extend(owner_merge_hold_gate(slug))
    except Exception as exc:
        found.append(("%s:owner-merge-hold:err" % slug, 1, (
            "    owner-merge close gate: the check failed (%s: %s). Retry "
            "in an hour." % (type(exc).__name__, exc))))

    # 6c-3-bis. Rework priority of an open PR: a ready/todo coder card that
    #           closes an already open PR (rework by verdict, a reviewer's
    #           fix) is raised to p90 inside the active band: before new
    #           work, after review p100 and WCFY p95. Stale arms are retired
    #           in every status except running; the same mechanism retires
    #           review and WCFY arms on a head that moved on. A frequent
    #           pass: --rework-gate-only (cron every 5 minutes).
    try:
        found.extend(rework_priority_lift(slug))
    except Exception as exc:
        found.append(("%s:rework-lift:err" % slug, 1, (
            "    Rework priority: the check failed (%s: %s). Retry "
            "in an hour." % (type(exc).__name__, exc))))

    # 6c-4. Stuck active_pr guard: a ready card stands under
    #       respawn_guarded(active_pr) after an owner decision, and the handoff
    #       is set by a reassign through another profile. A frequent pass:
    #       --rework-gate-only (cron every 5 minutes).
    try:
        found.extend(active_pr_handoff_gate(slug))
    except Exception as exc:
        found.append(("%s:active-pr-stuck:err" % slug, 1, (
            "    active_pr guard: the check failed (%s: %s). Retry in an hour."
            % (type(exc).__name__, exc))))

    # 6d. Progress comment after a burnout: a run with Iteration budget
    #     exhausted and no comment after its end gets a "done / left" comment
    #     from the watchdog, based on the artifact. Idempotent per run.
    try:
        found.extend(burnout_progress_comment(slug))
    except Exception as exc:
        found.append(("%s:burnprogress:err" % slug, 1, (
            "    Progress after burnout: the check failed (%s: %s). "
            "Retry in an hour." % (type(exc).__name__, exc))))

    # 6e. Prototype gate: an implementation for a feature/ux issue without an
    #     accepted prototype is blocked, accepted cards are raised. The actions
    #     are held by the dispatch hold. gh errors do not bring down the other
    #     checks.
    try:
        found.extend(prototype_gate(slug))
    except Exception as exc:
        found.append(("%s:proto:err" % slug, 1, (
            "    Prototype gate failed (%s: %s). Retry in an hour."
            % (type(exc).__name__, exc))))

    # While all parallelism slots (kanban.max_in_progress) are busy, a queue in
    # ready is normal pipeline work, not a stall. The stock Hermes detector
    # does not know about the limit and reports stranded_in_ready for every
    # waiting card.
    busy = any(tasks[i]["status"] == "running" for i in tasks)

    diags, diag_keys = {}, {}
    for entry in diagnostics(slug):
        task_id = entry.get("task_id", "?")
        for diag in entry.get("diagnostics", []):
            if diag.get("kind") == "stranded_in_ready" and busy:
                continue
            diags.setdefault(task_id, []).append(
                "      %s: %s" % (diag.get("severity"), diag.get("title") or ""))
            diag_keys.setdefault(task_id, []).append(
                "%s:diag:%s:%s" % (slug, task_id, diag.get("kind")))


    for task_id, title, tries, minutes, cap in runtime_cap_hits(slug):
        limit = ("the card's own limit of %d s" % cap) if cap else "the general run limit"
        found.append(("%s:cap:%s" % (slug, task_id), 1, (
            "    The card hits a ceiling instead of failing: " + task_id + "\n"
            "      " + title[:70] + "\n"
            "      %d attempts in a row, each ~%.0f min, working under %s." % (tries, minutes, limit) + "\n"
            "      An identical duration is the signature of a limit, not of a lack of resources." + "\n"
            "      Check whether part of the work is done, and decide: raise the limit or split the card."
        )))

    for task_id, title, times, status in budget_exhausted(slug):
        found.append(("%s:budget:%s" % (slug, task_id), 1, (
            "    One run is too small for the card, the task is not hard: " + task_id + " [" + str(status) + "]" + "\n"
            "      " + title[:70] + "\n"
            "      The iteration budget was exhausted %d times in two days. A successful run needs" % times + "\n"
            "      about 55 iterations with a ceiling of 120: the work is more than one run." + "\n"
            "      Recreate the card in goal mode, it exists for exactly this:" + "\n"
            "        hermes kanban --board " + slug + " create ... --goal --goal-max-turns 40" + "\n"
            "      The worker continues in the same session under a judge instead of starting from zero."
        )))

    # 2b. A second consecutive budget burnout: a machine block of the card.
    #     One gave_up is a signal (see budget_exhausted above), the second in a
    #     row is proof: a retry does not fit the same budget, and the dispatcher
    #     would burn a third run on the same wall. The gate violation signal:
    #     the third in a row (the detector had to block after the second).
    for task_id, title, status, tail, goal, block_kind in consecutive_budget_gave_up(slug):
        key = "%s:budget-gate:%s" % (slug, task_id)
        gate_violation = tail >= 3
        if status not in ("blocked",):
            comment = (
                "Watchdog: %d consecutive runs with the iteration budget exhausted. "
                "Auto-retry stopped." % tail + "\n"
                "A retry with the same body does not fit the same budget: "
                "a package of owner review findings in one card does not fit "
                "one run (precedent: three cards burned 200/200 iterations, "
                "1.4-1.8 h each)." + "\n"
                "Decompose: rework on the BLOCK items as separate cards "
                "with a common umbrella parent; minor as a separate card; "
                "each with its own budget. The commits of the first run are "
                "already in the branch: the second run spent iterations on the "
                "remainder, so splitting saves at least one burned run."
            )
            run([HERMES, "kanban", "--board", slug, "comment", task_id,
                 comment], timeout=90)
            run([HERMES, "kanban", "--board", slug, "block", task_id,
                 "Two consecutive budget burnouts (%d x Iteration budget). "
                 "Release condition: decompose the body into cards with a "
                 "smaller scope each, recreate in goal mode, or an explicit "
                 "limit raise by the owner." % tail,
                 "--kind", "needs_input"], timeout=90)
        else:
            # the card already stands blocked (an explicit block): the dispatcher
            # does not take it, and a repeated block would only send it to triage
            # through the unblock-loop detector.
            pass
        prefix = ("GATE VIOLATION: third consecutive budget run, "
                  "the detector did not block after the second." if gate_violation
                  else "The card burns the iteration budget repeatedly:")
        action_note = (
            "The card already stands blocked (the block kind is not the budget gate): "
            "the dispatcher does not take it, there is no auto-retry. See the block reason."
            if status == "blocked"
            else "The watchdog blocked the card (needs_input): auto-retry "
                 "stopped until decomposition.")
        found.append((key, 0, (
            "    " + prefix + " " + task_id + " [" + str(status) + "]" + "\n"
            "      " + title[:70] + "\n"
            "      Tail of consecutive budget burnouts: %d in 7 days." % tail + "\n"
            "      " + action_note +
            (" The gate fired late: check why the card was not "
             "blocked after the second burnout." if gate_violation else "")
        )))


    stalled = output_stalled(slug, repo)
    if stalled:
        # The key is per day: an hour in the key meant a new push every hour
        # for the same stop (a repeat is not allowed more often than once a day).
        found.append(("%s:nooutput:%s" % (slug, time.strftime("%Y-%m-%d")), 0, stalled))


    queued = [i for i in tasks if tasks[i]["status"] in QUEUED]
    moving = [i for i in tasks if tasks[i]["status"] in MOVING]
    if queued and not moving:
        roots = []
        for task_id in tasks:
            if tasks[task_id]["status"] not in HELD:
                continue
            own = [b for b in blockers.get(task_id, [])
                   if b in tasks and tasks[b]["status"] in OPEN]
            if own:
                continue
            held = waiting_on(task_id, tasks, kids)
            if held:
                roots.append((len(held), task_id))
        if roots:
            blocks = []
            for count, task_id in roots:
                task = tasks[task_id]
                pr_lines, rank = describe(refs_in(task))
                head = "    %s [%s] %s\n      unblocks cards: %d" % (
                    task_id, task["assignee"], task["title"], count)
                extra = diags.pop(task_id, [])
                if extra and not pr_lines:
                    extra.append("      analysis: hermes kanban log %s" % task_id)
                pr_lines = extra + pr_lines
                if extra:
                    rank = max(rank if rank != 9 else 0, 3)
                # Sorting: first those whose PR is already ready to merge, then
                # conflicts and red CI, at the end cards without a PR (those
                # need thinking).
                blocks.append((rank, -count, "\n".join([head] + pr_lines)))
            blocks.sort()
            body = ["  %d cards in the queue, none can start." % len(queued),
                    "  Work from the top down, the top is cheaper:"]
            body += [b[2] for b in blocks]
            key = "%s:stalled:%s" % (slug, ",".join(sorted(r[1] for r in roots)))
            found.append((key, 1, "\n".join(body)))

    for task_id, lines in diags.items():
        title = tasks.get(task_id, {}).get("title", "")
        assignee = tasks.get(task_id, {}).get("assignee", "-")
        found.append((diag_keys[task_id][0], 2, "  %s [%s] %s\n%s" % (
            task_id, assignee, title, "\n".join(lines))))

    # 4. A block stands but there is nothing to wait for: an explicit block
    # (--initial-status blocked or kanban block) is not removed by the
    # completion of a parent, unlike a dependency. The parents go to done, the
    # card stays blocked forever, and the queue empties silently: formally all
    # is fine.
    stale_blocks = []
    for task_id, task in tasks.items():
        if task["status"] != "blocked":
            continue
        own = blockers.get(task_id, [])
        if not own:
            continue
        if task_id in waited:
            continue
        if all(tasks.get(p, {}).get("status") in ("done", "archived") for p in own):
            stale_blocks.append(task_id)
    if stale_blocks:
        lines = ["  Blocked, but there is nothing to wait for: all their dependencies are finished.",
                 "  An explicit block is not removed automatically, only by hermes kanban unblock.",
                 "  If a card waits for an external event rather than a card, write in a comment",
                 "  the line \"Release condition: ...\": it is respected and the card will not get into this list."]
        for task_id in stale_blocks[:8]:
            lines.append("    %s [%s] %s" % (
                task_id, tasks[task_id]["assignee"], tasks[task_id]["title"]))
        found.append(("staleblock:%s" % ",".join(sorted(stale_blocks)), 1, "\n".join(lines)))

    # 5. Mass failure: agents fail not one by one but in a row. That looks not
    # like a hard task but like a broken environment: a rejected key, a
    # nonexistent model, an unreachable endpoint. One such case: a spend
    # watchdog left a configuration with the wrong provider for the address, and
    # for a day the agents got 401 and exited without calling completion.
    # The thresholds are tuned on board history: 5 runs and 60% filter out a
    # random pair of failures in a row, but catch a real incident (14 bad of 23).
    bad = [r[0] for r in recent if r[0] in ("crashed", "gave_up", "timed_out")]
    if len(recent) >= 5 and len(bad) > 0.6 * len(recent):
        counts = {}
        for outcome in bad:
            counts[outcome] = counts.get(outcome, 0) + 1
        # The dedup key is per day: the numbers change every hour, and a key
        # built from them would mean a new message on every tick.
        found.append(("massfail:%s" % time.strftime("%Y-%m-%d"), 0,
                      "  Most runs fail: %d of %d in 6 hours (%s).\n"
                      "  This looks like a broken environment, not hard tasks.\n"
                      "  Check the provider with one call: hermes -z ping\n"
                      "  and the configuration: hermes config show | grep -A4 Model" % (
                          len(bad), len(recent),
                          ", ".join("%s %d" % (k, v) for k, v in sorted(counts.items())))))

    if queued and last_end:
        idle = (time.time() - float(last_end)) / 3600.0
        if idle > SILENCE_HOURS:
            key = "%s:silent:%d" % (slug, int(idle // 24))
            found.append((key, 3,
                          "  No finished run for %.0f h with %d cards in the queue.\n"
                          "  Check the dispatcher: grep kanban ~/.hermes/logs/gateway.log | tail" % (
                              idle, len(queued))))

    # Idle for more than an hour with a non-empty backlog of the admissible
    # band, even if runs finished recently (stalled already deduplicated,
    # silent not yet at 6 h, while the factory stands). One owner finding.
    try:
        found.extend(idle_factory_lint(slug, st, tasks, blockers))
    except Exception as exc:
        found.append(("%s:idlefactory:err" % slug, 1, (
            "    Idle factory gate failed (%s: %s). Retry in an hour."
            % (type(exc).__name__, exc))))

    # 6. Duplicates of cards created by agents without an idempotency-key.
    found.extend(duplicate_detector(slug))

    # 7. Cards of the last 24h without an idempotency_key from non-human creators.
    #    auto-decomposer is counted on a separate line, escalation at >3 a day.
    found.extend(idempotency_key_check(slug))

    # 7b. Idempotency gate: the same cards, but the watchdog blocks them
    #     itself and does not only report (findings do not reach the roles).
    found.extend(idempotency_gate(slug))

    # 8. A blocked twin card: the module does not exist on the target line.
    #    Switched off (TWIN_ERA=False): the old line is end of life, twin
    #    cards are not created.
    if TWIN_ERA:
        found.extend(twin_blocked_missing_module_detector(slug))

    # 9. Two or more live coder cards refer to one PR #N.
    #    Twin cards (different release lines) do not count as duplicates.
    found.extend(same_pr_coder_duplicates(slug))

    # 10. A twin pair in todo/ready with the same issue: a risk of a
    #     simultaneous start. Switched off (TWIN_ERA=False): there are no twins.
    if TWIN_ERA:
        found.extend(twin_conflict_risk_detector(slug))

    # A card whose PR is delivered does not go back to triage and is not
    # decomposed; the continuation after delivery is a new card for the
    # remainder, with a body checked against the PR. Catches both legs:
    # triage after block_loop with a "PR #N" link (before decomposition)
    # and decomposed after block_loop (after it).
    try:
        found.extend(decomposed_after_delivery(slug))
    except Exception as exc:
        found.append(("%s:decpr:err" % slug, 1, (
            "    Gate \"PR delivered, no decomposition\": the check failed"
            " (%s: %s). Retry in an hour." % (type(exc).__name__, exc))))

    # ready p>=80 under respawn_guarded for more than 30 min. I3 sees only the
    # symptom (the slot goes to p<50); the reason the layer is not spawned
    # was never named. A finding with the reason only, no actions (a card's
    # own handoff comment fails closed).
    try:
        found.extend(guarded_ready_lint(slug))
    except Exception as exc:
        found.append(("%s:r48:err" % slug, 1, (
            "    Gate \"ready under dispatcher guard\" failed (%s: %s)."
            " Retry in an hour." % (type(exc).__name__, exc))))

    return found


def same_pr_coder_duplicates(slug):
    """Finds duplicate coder cards: two cards for the same work.

    A duplicate is not "two cards for one PR" but two cards for one PR AND one
    slice of work. One PR legitimately holds: children of a decomposition
    (rework 1/5..5/5), rework plus a follow-up IT, a main card plus a twin for
    another branch.

    Grouping (owner decision):
      - the card has an idempotency_key of the form owner-review-<pr>-<head7>:
        the group key is (pr, head7, twin flag). Two verdicts on different
        heads are different work, a duplicate is possible only within one head;
      - there is no key: cards count as duplicates only when the NORMALIZED
        title matches: the role prefix ([coder] and the like), round markers
        (round N, rework N/M), case and extra spaces are removed.
        "PR #181 rework 1/5" and "PR #181 rework 2/5" differ after
        normalization by the tail of the description and do not count as
        duplicates.

    Observed: for several PRs a webhook session and a chat session each created
    a pair of cards with the same meaning.
    """
    db = os.path.join(BOARDS, slug, "kanban.db")
    if not os.path.exists(db):
        return []
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
    rows = list(con.execute(
        "SELECT id, title, status, created_by, created_at, idempotency_key "
        "FROM tasks "
        "WHERE assignee = 'coder' "
        "AND status IN ('todo','triage','ready','running','blocked','scheduled','review')"
    ))
    bodies = dict(con.execute(
        "SELECT id, body FROM tasks WHERE assignee = 'coder'"
    ))
    con.close()

    pr_re = re.compile(r"#(\d+)")
    twin_re = re.compile(r"twin|1\.x", re.IGNORECASE)
    key_re = re.compile(r"^owner-review-(\d+)-([0-9a-f]{7})")
    head_re = re.compile(r"head\s+([0-9a-f]{7})\b")
    role_re = re.compile(r"^(\s*\[[a-z]+\])+\s*", re.IGNORECASE)
    round_re = re.compile(r"\b(round|rework)\s+\d+(\s*/\s*\d+)?\b", re.IGNORECASE)
    paren_re = re.compile(r"\s*\([^)]*\)")
    space_re = re.compile(r"\s+")

    def norm_title(t):
        t = role_re.sub("", t or "")
        t = round_re.sub("", t)
        t = paren_re.sub("", t)
        t = space_re.sub(" ", t).strip().lower()
        return t

    groups = {}
    for task_id, title, status, created_by, created_at, ikey in rows:
        nums = pr_re.findall(title or "")
        if not nums:
            continue
        pr = nums[0]
        is_twin = bool(twin_re.search((title or "").split(":", 1)[0]))
        m = key_re.match(ikey or "")
        if m:
            # Key owner-review-<pr>-<head7>: group by the key, not by the first
            # PR in the title. A twin reference like "twin #177" in the title
            # of a card about another PR must not override the key (once a card
            # for one PR ended up in the group of another).
            pr_eff = m.group(1)
            gkey = (pr_eff, m.group(2), "twin" if is_twin else "main")
        else:
            body = (bodies or {}).get(task_id, "") if bodies else ""
            bh = head_re.search(body or "")
            if bh:
                gkey = (pr, bh.group(1), "twin" if is_twin else "main")
            else:
                gkey = (pr, norm_title(title), "twin" if is_twin else "main")
        groups.setdefault(gkey, []).append({
            "id": task_id, "title": title, "status": status,
            "created_by": created_by, "created_at": created_at,
            "is_twin": is_twin,
        })

    found = []
    for (pr, slice_key, kind), cards in groups.items():
        if len(cards) < 2:
            continue
        cards.sort(key=lambda c: c["created_at"] or 0)
        ids = ", ".join("%s[%s]" % (c["id"], c["status"]) for c in cards)
        if slice_key and not slice_key.startswith("#"):
            basis = "one head slice (%s)" % slice_key
        else:
            basis = "identical normalized title"
        found.append(("samepr:%s:%s:%s:%s" % (slug, pr, kind, slice_key), 0,
            "  PR #%s has %d coder-%s cards for one piece of work (%s): %s\n"
            "    Two workers on one branch = conflicting pushes.\n"
            "    Keep one, archive the others: hermes kanban archive <id>"
            % (pr, len(cards), kind, basis, ids)))
    return found

def duplicate_detector(slug):
    """Finds duplicates of open cards created by agents without an idempotency-key.

    Pipeline agents (created_by != user) must pass an idempotency-key to rule
    out creating the same card twice (precedent: duplicate re-checks of the
    same visual evidence for one PR).

    Groups open cards without a key from non-humans by the normalized title
    (the [coder]/[reviewer] prefixes removed, lower case, spaces collapsed).
    If a group has more than one card, it is a duplicate: the IDs are reported
    and archiving the younger one is suggested.

    Dedups by the hash of the normalized title: until the group resolves, the
    message is not repeated (REPEAT_AFTER = 24 hours).
    """
    NORM_ROLE = re.compile(r'^\[[^\]]*\]\s*')
    NORM_SPACE = re.compile(r'\s+')

    db = os.path.join(BOARDS, slug, 'kanban.db')
    if not os.path.exists(db):
        return []
    con = sqlite3.connect('file:%s?mode=ro' % db, uri=True, timeout=10)
    rows = list(con.execute(
        "SELECT id, title, status, created_by, created_at FROM tasks "
        "WHERE (idempotency_key IS NULL OR idempotency_key = '') "
        "AND created_by != 'user' "
        "AND status IN ('todo','triage','ready','running','blocked','scheduled','review')"
        "ORDER BY created_at ASC"))
    con.close()

    groups = {}
    for task_id, title, status, created_by, created_at in rows:
        norm = NORM_ROLE.sub('', title or '')
        norm = NORM_SPACE.sub(' ', norm).strip().lower()
        if not norm:
            continue
        groups.setdefault(norm, []).append({
            'id': task_id, 'title': title, 'status': status,
            'created_by': created_by, 'created_at': created_at
        })

    now = time.time()
    found = []

    # --- First pass: the existing logic (duplicates without an idempotency-key) ---
    for norm, cards in groups.items():
        if len(cards) < 2:
            continue
        # At least one card created in the last 24 hours, otherwise it is a
        # historical group that was already seen.
        if not any(c['created_at'] and c['created_at'] > now - 24 * 3600 for c in cards):
            continue
        cards.sort(key=lambda c: c['created_at'] or 0)
        younger = cards[-1]
        ids = [c['id'] for c in cards]
        key = 'dup:%s:%s' % (slug, hashlib.md5(norm.encode()).hexdigest()[:8])
        text = (
            "    Duplicate cards without an idempotency-key (created by agent %s):\n"
            "      normalized title: %s\n"
            "      cards: %s\n"
            "      Key format: <role>-<pr or issue>-<what we do>\n"
            "      Suggested: archive the younger one (%s): hermes kanban archive %s"
        ) % (cards[0]['created_by'], norm[:80],
             ', '.join('%s[%s]' % (c['id'], c['status']) for c in cards),
             younger['id'], younger['id'])
        found.append((key, 0, text))

    # --- Second pass: twin-aware duplicates ---
    # Group the same rows by a key without the line marker [N.x] / N.x twin.
    # If a group has cards on DIFFERENT lines: a twin pair, not a duplicate.
    # If on ONE line: a duplicate, report it.
    LINE_STRIP = re.compile(
        r'\s*\[[12]\.x\]\s*|'
        r'\s*[12]\.x\s+twin\s*',
        re.IGNORECASE,
    )
    LINE_VAL = re.compile(
        r'\[([12])\.x\]|'
        r'([12])\.x\s+twin',
        re.IGNORECASE,
    )

    def _extract_line(orig_title):
        m = LINE_VAL.search(orig_title or '')
        if m:
            return (m.group(1) or m.group(2)) + '.x'
        return None

    la_groups = {}
    for task_id, title, status, created_by, created_at in rows:
        norm = NORM_ROLE.sub('', title or '')
        norm = NORM_SPACE.sub(' ', norm).strip().lower()
        if not norm:
            continue
        lak = LINE_STRIP.sub('', norm).strip()
        if lak == norm or not lak:
            # No line marker or empty after removal: already in the first pass
            continue
        la_groups.setdefault(lak, []).append({
            'id': task_id, 'title': title, 'status': status,
            'created_by': created_by, 'created_at': created_at,
        })

    for lak, cards in la_groups.items():
        if len(cards) < 2:
            continue
        if not any(c['created_at'] and c['created_at'] > now - 24 * 3600 for c in cards):
            continue

        lines = set()
        for c in cards:
            lv = _extract_line(c['title'])
            if lv:
                lines.add(lv)

        if len(lines) > 1:
            # Different lines = a twin pair, not a duplicate
            continue

        # The same line (or none has a marker): a duplicate
        cards.sort(key=lambda c: c['created_at'] or 0)
        younger = cards[-1]
        key = 'dup2:%s:%s' % (slug, hashlib.md5(lak.encode()).hexdigest()[:8])
        line_label = ', '.join(sorted(lines)) if lines else '(no line marker)'
        text = (
            "    Duplicate cards (identical title + identical line %s):\n"
            "      title: %s\n"
            "      cards: %s\n"
            "      Suggested: archive the younger one (%s): hermes kanban archive %s"
        ) % (line_label, lak[:80],
             ', '.join('%s[%s]' % (c['id'], c['status']) for c in cards),
             younger['id'], younger['id'])
        found.append((key, 0, text))

    return found


def twin_conflict_risk_detector(slug):
    """A twin pair in todo/ready: an identical title (without the line marker),
    the same issue in the body, a risk of a simultaneous start.

    When the gate opens, both twin cards (one per release line) can start at
    the same time and create conflicting PRs for one branch. Reports the risk
    and suggests linking a dependency.
    """
    LINE_STRIP = re.compile(
        r'\s*\[[12]\.x\]\s*|'
        r'\s*[12]\.x\s+twin\s*',
        re.IGNORECASE,
    )
    PAREN_RE = re.compile(r'\s*\([^)]*\)')
    ELLIPSIS_RE = re.compile(r'\s*\.\.\.\s*')
    NORM_ROLE = re.compile(r'^\[[^\]]*\]\s*')
    NORM_SPACE = re.compile(r'\s+')
    ISSUE_RE = re.compile(r'#(\d+)')
    LINE_VAL = re.compile(
        r'\[([12])\.x\]|'
        r'([12])\.x\s+twin',
        re.IGNORECASE,
    )

    def _extract_line(orig_title):
        m = LINE_VAL.search(orig_title or '')
        if m:
            return (m.group(1) or m.group(2)) + '.x'
        return None

    def _core_title(orig_title):
        """Normalized title without the role, the line marker,
        parenthetical metadata and ellipses."""
        t = NORM_ROLE.sub('', orig_title or '')
        t = PAREN_RE.sub('', t)
        t = LINE_STRIP.sub('', t)
        t = ELLIPSIS_RE.sub(' ', t)
        t = NORM_SPACE.sub(' ', t).strip().lower()
        return t

    db = os.path.join(BOARDS, slug, 'kanban.db')
    if not os.path.exists(db):
        return []
    con = sqlite3.connect('file:%s?mode=ro' % db, uri=True, timeout=10)
    rows = list(con.execute(
        "SELECT id, title, status FROM tasks "
        "WHERE assignee = 'coder' "
        "AND status IN ('todo', 'ready')"))
    bodies = dict(con.execute(
        "SELECT id, body FROM tasks WHERE assignee = 'coder' "
        "AND status IN ('todo', 'ready')"))
    con.close()

    # Group by issue + core title
    # First collect all cards with a line marker
    has_line = []
    for task_id, title, status in rows:
        if _extract_line(title):
            has_line.append((task_id, title, status))

    # Group by issue + core title
    groups = {}  # (issue, core) -> [cards]
    for task_id, title, status in has_line:
        core = _core_title(title)
        if not core:
            continue
        body_text = (bodies or {}).get(task_id, '')
        nums = ISSUE_RE.findall((title or '') + ' ' + (body_text or ''))
        if not nums:
            continue
        for num in nums:
            gkey = (num, core)
            groups.setdefault(gkey, []).append({
                'id': task_id, 'title': title, 'status': status,
            })

    found = []
    for (issue, core), cards in groups.items():
        if len(cards) < 2:
            continue

        # Check the lines: if all are on one, it is not a twin risk
        lines = set()
        for c in cards:
            lv = _extract_line(c['title'])
            if lv:
                lines.add(lv)

        if len(lines) <= 1:
            # One line: not a twin pair
            continue

        ids = ', '.join('%s[%s]' % (c['id'], c['status']) for c in cards)
        key = 'twinconflict:%s:%s' % (slug, hashlib.md5(
            ('%s-%s' % (issue, core)).encode()).hexdigest()[:8])
        text = (
            "    Twin pair of coder cards for issue #%s (different lines: %s), "
            "both in todo/ready:\n"
            "      title: %s\n"
            "      cards: %s\n"
            "      Risk: when the gate opens, both start at the same time "
            "and create conflicting PRs for one branch.\n"
            "      Suggested: add a dependency so they start "
            "sequentially:\n"
            "        hermes kanban --board %s link <older-id>"
            " <younger-id>"
        ) % (issue, ', '.join(sorted(lines)), core[:80], ids, slug)
        found.append((key, 0, text))

    return found


def idempotency_key_check(slug):
    """Cards of the last 24h without an idempotency_key from non-human creators.

    A single check instead of a pair (missing_idempotency_key_check, 1h, without
    auto-decomposer, and fresh_cards_without_idempotency_key, 24h): two
    signals on one door. The window is 24h.

    auto-decomposer is part of the Hermes core, it does not set keys and it must
    not be changed (the owner's rule: the core is not edited). Its cards are
    exempt from the key rule: they are shown as an informational line, without
    escalation and without ops cards.
    """
    db = os.path.join(BOARDS, slug, "kanban.db")
    if not os.path.exists(db):
        return []
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
    since = time.time() - 24 * 3600
    rows = list(con.execute(
        "SELECT id, created_by, substr(title, 1, 80) as title, "
        "created_at FROM tasks "
        "WHERE created_at > ? "
        "AND created_by IS NOT NULL AND created_by != 'user' "
        "AND (idempotency_key IS NULL OR idempotency_key = '') "
        "AND status NOT IN ('done', 'archived') "
        "ORDER BY created_at ASC",
        (since,),
    ))
    con.close()
    if not rows:
        return []

    core = [r for r in rows if r[1] == "auto-decomposer"]
    rest = [r for r in rows if r[1] != "auto-decomposer"]

    lines = []
    if core:
        lines.append(
            "    auto-decomposer without idempotency_key in 24h: %d (core, "
            "does not set keys, exempt from the rule, info): %s"
            % (len(core), ", ".join("%s %s" % (t, ti) for t, _, ti, _ in core[:5])))
    if rest:
        count = len(rest)
        by_author = {}
        for task_id, created_by, title, created_at in rest:
            by_author.setdefault(created_by, []).append(
                "%s %s" % (task_id, title)
            )
        authors = []
        for author, cards in sorted(by_author.items(), key=lambda x: -len(x[1])):
            authors.append("      %s (%d cards): %s" % (
                author, len(cards), ", ".join(cards[:5])))
            if len(cards) > 5:
                authors[-1] += " ... and %d more" % (len(cards) - 5)
        lines.append(
            "    %d cards without idempotency_key from non-human creators in 24h "
            "(without the core auto-decomposer):" % count)
        lines.append("      Window: the last 24 hours. Creators:")
        lines.extend(authors)
        lines.append(
            "    The agent must pass --idempotency-key on kanban_create. "
            "Format: <role>-<issue/PR>-<what we do>. Convention: kanban-discipline.")

    if not lines:
        return []
    key = "noikey:%s:%d" % (slug, int(time.time() // 86400))
    return [(key, 2, "\n".join(lines))]


# Idempotency gate: a card of an agent without an idempotency_key is recreated
# by the gate itself with a key and the original is archived (the rule: what the
# gate catches, the gate fixes). Earlier the gate only left a comment, set
# needs_input and stayed silent until the owner, and cards stood blocked for
# days.
IKEYGATE_MIN_AGE_S = 3600  # an hour for the creator to notice and recreate it
IKEYGATE_COMMENT_PREFIX = "Idempotency gate:"


def _gate_comment_exists(db, task_id):
    """A gate comment already exists on the card: do not post a repeat
    (protection against a repeated trigger)."""
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
        row = con.execute(
            "SELECT 1 FROM task_comments WHERE task_id = ? "
            "AND body LIKE ? LIMIT 1",
            (task_id, IKEYGATE_COMMENT_PREFIX + "%",)).fetchone()
        con.close()
        return bool(row)
    except Exception:
        return False


def _ikey_derive(created_by, title, task_id):
    """A deterministic recreation key: the same between passes
    (create with an existing key returns the same card, so a retry after a
    failed archive does not breed copies) and unique between cards (task_id in
    the suffix: two different cards of one PR are not glued by the gate,
    gluing duplicates is the job of duplicate_detector)."""
    ref = ""
    m = re.search(r"\bPR\s*#?(\d+)", title, re.I)
    if m:
        ref = "pr%s" % m.group(1)
    else:
        m = re.search(r"\bissues?\s*#?(\d+)", title, re.I)
        if m:
            ref = "issue%s" % m.group(1)
    return "%s-%s-%s-ikeygate" % (created_by, ref or "card", task_id)


def _ikey_recreate(slug, db, task_id):
    """Recreates a card without a key with a key: create carries over the body,
    assignee, priority, parents and creator; a marker comment goes on both
    cards; the original is archived. Returns the id of the new card or None
    (then the gate falls back to the earlier comment + needs_input +
    escalation)."""
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
    row = con.execute(
        "SELECT title, body, assignee, priority, created_by FROM tasks "
        "WHERE id = ?", (task_id,)).fetchone()
    parents = [r[0] for r in con.execute(
        "SELECT parent_id FROM task_links WHERE child_id = ?", (task_id,))]
    con.close()
    if not row:
        return None
    title, body, assignee, priority, created_by = row
    key = _ikey_derive(created_by, title, task_id)
    # Recreation is a restore: first ask the helper whether a live card of
    # the same family already covers it (a duplicate executor) and whether a
    # stock mechanism already holds the card.
    covered, guard = board_native_guard.native_or_live(
        key,
        live_keys=_live_keys(slug),
        native_checked=[
            board_native_guard.NATIVE_RESPAWN,
            board_native_guard.NATIVE_TASK_LINKS],
        why="the card is live but has no idempotency-key: the repeat check"
            " cannot see it, and there is no stock mechanism that assigns a"
            " key (idempotency gate); parents are carried over as they are")
    if covered:
        return None
    body = board_native_guard.guard_section(guard) + (body or "")
    body_path = None
    try:
        with tempfile.NamedTemporaryFile(
                "w", suffix=".md", encoding="utf-8", delete=False) as fh:
            fh.write(body or "")
            body_path = fh.name
        cmd = [HERMES, "kanban", "--board", slug, "create", title,
               "--body-file", body_path,
               "--priority", str(priority or 0),
               "--idempotency-key", key,
               "--created-by", created_by, "--json"]
        if assignee:
            cmd += ["--assignee", assignee]
        for parent in parents:
            cmd += ["--parent", parent]
        out = run(cmd, timeout=120)
        new_id = None
        try:
            new_id = (json.loads(out) or {}).get("id")
        except Exception:
            m = re.search(r'"id"\s*:\s*"(t_[0-9a-f]+)"', out or "")
            new_id = m.group(1) if m else None
        if not new_id:
            return None
        if not _gate_comment_exists(db, task_id):
            run([HERMES, "kanban", "--board", slug, "comment", task_id,
                 IKEYGATE_COMMENT_PREFIX + " the card has no idempotency-key, "
                 "the repeat check cannot see it. Recreated by the gate as %s "
                 "with key %s (the owner's rule: the gate that catches it "
                 "also fixes it), the original is being archived."
                 % (new_id, key)],
                timeout=90)
        run([HERMES, "kanban", "--board", slug, "archive", task_id],
            timeout=90)
        run([HERMES, "kanban", "--board", slug, "comment", new_id,
             "Recreated by the idempotency gate from %s (that one had no "
             "idempotency-key), key %s, creator of the original: %s. Body, "
             "assignee, priority and parents carried over verbatim."
             % (task_id, key, created_by)], timeout=90)
        return new_id
    finally:
        if body_path:
            try:
                os.unlink(body_path)
            except OSError:
                pass

def idempotency_gate(slug, state=None, now=None):
    """Recreates agent cards that have no idempotency_key: todo/ready older
    than an hour, and blocked ones already marked by a gate comment (migration
    of cards stuck on the old behaviour). The creator is neither user nor
    auto-decomposer (the owner is exempt), and the key is empty.

    Action: _ikey_recreate (create with a deterministic key, a marker
    comment, archive of the original). A second pass after a partial failure
    neither multiplies copies (the key makes create idempotent) nor comments
    (dedup by the presence of the gate comment). If recreation fails, fall
    back to the former behaviour: a comment, block needs_input (the core
    block_task only moves running/ready) and a gate-alert escalation
    (_gate_board_comment, as in max_in_progress_gate).
    """
    db = os.path.join(BOARDS, slug, "kanban.db")
    if not os.path.exists(db):
        return []
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
    rows = list(con.execute(
        "SELECT id, created_by, substr(title, 1, 80), created_at, status "
        "FROM tasks "
        "WHERE ((status IN ('todo', 'ready') AND created_at < ?) "
        "       OR status = 'blocked') "
        "AND created_by IS NOT NULL "
        "AND created_by NOT IN ('user', 'auto-decomposer') "
        "AND (idempotency_key IS NULL OR idempotency_key = '') "
        "ORDER BY created_at ASC",
        (time.time() - IKEYGATE_MIN_AGE_S,)))
    con.close()
    found = []
    # Escalations are aggregated per pass. The gate is called from check(),
    # while main() persists only report keys, so REPEAT_AFTER in
    # _gate_board_comment does not dedup WITHIN a pass: 12 cards without a
    # key produced 12 separate beacon comments in one pass. Collect the
    # first-touch task_ids per loop and call _gate_board_comment once.
    blockfail = []
    recreatefail = []
    for task_id, created_by, title, created_at, status in rows:
        gate_touched = _gate_comment_exists(db, task_id)
        if status == "blocked" and not gate_touched:
            # Blocked by another rule (not by the gate): do not recreate, so
            # an ongoing decomposition or manual triage is not broken, but
            # report a finding: an agent card without a key stays a blind
            # spot of the dedup.
            age_h = (time.time() - (created_at or time.time())) / 3600.0
            found.append(("ikeygate-blocked:%s" % task_id, 0, (
                "    Idempotency gate: agent card without a key "
                "%s [%s] is blocked by another rule, not recreated.\n"
                "      %s\n"
                "      Creator: %s, age %.0f h. The duplicate check does not "
                "see it. Unblocking must be preceded by recreating it with "
                "--idempotency-key and archiving this one."
                % (task_id, status, title, created_by, age_h))))
            continue
        new_id = _ikey_recreate(slug, db, task_id)
        age_h = (time.time() - created_at) / 3600.0
        if new_id:
            found.append(("ikeygate:%s" % task_id, 0, (
                "    Idempotency gate: agent card without a key "
                "%s [%s] recreated as %s.\n"
                "      Creator: %s, age %.0f h. The original is "
                "archived, the new card has a key."
                % (task_id, status, new_id, created_by, age_h))))
            continue
        comment = (
            IKEYGATE_COMMENT_PREFIX + " card without idempotency-key, "
            "the repeat check does not see it. Automatic recreation failed: "
            "the creator (%s) must recreate it with --idempotency-key "
            "<role>-<issue|pr>-<slug> (kanban edit cannot set a key) "
            "and archive this one." % created_by)
        if status == "ready":
            if not gate_touched:
                run([HERMES, "kanban", "--board", slug, "comment", task_id,
                     comment], timeout=90)
            run([HERMES, "kanban", "--board", slug, "block", task_id,
                 IKEYGATE_COMMENT_PREFIX + " automatic recreation failed; "
                 "card without idempotency-key. Release condition: "
                 "recreate with --idempotency-key and archive this one.",
                 "--kind", "needs_input"], timeout=90)
            applied = False
            try:
                con = sqlite3.connect("file:%s?mode=ro" % db, uri=True,
                                      timeout=10)
                row = con.execute("SELECT status FROM tasks WHERE id = ?",
                                  (task_id,)).fetchone()
                con.close()
                applied = bool(row) and row[0] == "blocked"
            except Exception:
                applied = False
            if not gate_touched and not applied:
                blockfail.append(task_id)
            note = ("recreation FAILED, fell back to needs_input."
                    if applied
                    else "recreation and block were NOT applied, "
                         "gate-alert escalation.")
        else:
            # todo (the core does not block it) or already blocked by this gate.
            if not gate_touched:
                run([HERMES, "kanban", "--board", slug, "comment", task_id,
                     comment + " The core does not block cards in todo: when "
                     "promoted to ready it will be caught by the next pass."
                     if status == "todo" else comment], timeout=90)
                recreatefail.append((task_id, status))
            note = ("recreation FAILED, the core block_task only accepts "
                    "running/ready, gate-alert escalation."
                    if status == "todo"
                    else "recreation FAILED, the card is already "
                         "blocked by this gate.")
        found.append(("ikeygate:%s" % task_id, 0, (
            "    Idempotency gate: agent card without a key "
            "%s [%s].\n"
            "      %s\n"
            "      Creator: %s, age %.0f h. %s"
            % (task_id, status, title, created_by, age_h, note))))
    if blockfail:
        _gate_board_comment(
            "ikeygate-blockfail",
            state if state is not None else _load_state(),
            now if now is not None else time.time(),
            IKEYGATE_COMMENT_PREFIX + " automatic recreation and block "
            "were not applied to %s on board %s: agent cards without "
            "idempotency-key may still be open: check "
            "manually." % (", ".join(blockfail), slug))
    if recreatefail:
        _gate_board_comment(
            "ikeygate-recreatefail",
            state if state is not None else _load_state(),
            now if now is not None else time.time(),
            IKEYGATE_COMMENT_PREFIX + " automatic recreation "
            "failed for %s on board %s: recreate with "
            "--idempotency-key and archive manually."
            % (", ".join("%s [%s]" % (tid, st)
                         for tid, st in recreatefail), slug))
    return found


# Review layer. Pairs of PRs once stood for almost a day: the executor cards
# closed after a push without a reviewer card, and the built-in review lane
# left its verdict in the card summary instead of the PR thread on the exact
# head. The idempotency gate is not to blame: it archived only coder cards
# and recreated all of them, while nobody created reviewer cards at all. Two
# rules, both executed by review_layer_check:
#   1. an open PR whose head is older than 30 minutes, without a verdict on
#      this sha and without a live reviewer card: the watchdog creates the
#      card itself, key reviewer-pr<N>-<head7>, priority 100, assignee
#      reviewer;
#   2. while such PRs exist (the review layer is non-empty), ready cards
#      below p50 are parked via `kanban schedule` and returned by `unblock`
#      when the layer is empty. The dispatcher picks by priority, so p100
#      overtakes <p50 even without parking; parking closes the gap when the
#      reviewer card is not in ready yet (todo with a parent, blocked,
#      between the push and the watchdog pass). There is no preemption of
#      running cards and there will not be: the core has no pause API,
#      reclaim kills the run, and the worker's hours cost more than an hour
#      of waiting for a slot.
#   3. a head whose reviewer card is parked in scheduled by someone else's
#      decision (a backlog, a quota hold) holds no slot. Otherwise the layer
#      considered itself non-empty and kept the ready cards parked while
#      those reviews could not start without the owner: the circle closed on
#      itself. The latest SCHEDULED reason of the card decides, see
#      _owner_deferred_review.
REVIEW_ARM_MIN_AGE_S = 30 * 60          # CI on the head usually takes 5-15 minutes
NO_EXECUTOR_MIN_AGE_S = 24 * 3600       # an active-milestone issue without
                                        # a live card for more than a day
# A release blocker is a bug label or an explicit owner mark in the body
# ("Owner: start now", "This blocks the <version> tag"). Its threshold is
# not a day but one watchdog cycle: the card should come from the 5-minute
# poll (issue_executor_poll), the watchdog is only a safety net here.
# 10 minutes is longer than the poll and the webhook take, so the watchdog
# does not shout over a card they have just created.
NO_EXECUTOR_BLOCKER_MIN_AGE_S = 10 * 60
OWNER_MARK_RE = re.compile(              # owner mark in the issue body
    r"(?i)\bowner:\s*start\b|\bblocks the\b")
ISSUE_POLL_WINDOW_S = 48 * 3600          # the poll reacts to fresh owner
                                        # actions, the old backlog is left
                                        # to the no-executor finding (24 h, digest)
# Work on an issue is already delivered when a merged PR refers to it AFTER
# the last scope change. Scope is the creation and the
# labeled/unlabeled/renamed events; milestoned and comments do NOT count as
# scope: moving an issue into the active milestone is bookkeeping, not new
# work (otherwise the watchdog breeds duplicate cards for an issue whose
# prep PR is already merged).
# A release chore (RELEASE_CHORE_RE) stays open on purpose after its prep PR
# is merged, until the tag: it waits for the owner's decision, not for an
# executor.
RELEASE_CHORE_RE = re.compile(
    r"(?i)chore\W*release|release\s+(chore|prep)"
    r"|prepare\s+[\w.]*\s*release")
SCOPE_EVENT_TYPES = frozenset(           # scope changes of an issue
    ("labeled", "unlabeled", "renamed"))
REVIEW_ARM_MAX_AGE_S = 14 * 24 * 3600   # old PRs are not auto-armed: separate hygiene
REVIEW_ARM_MAX_CREATE = 5               # guard against a storm of rebases
REVIEW_PARK_BELOW_P = 50
REVIEW_PARK_MARKER = "review-layer hold"
# A QA verdict and a reviewer verdict are told apart mechanically. A tester
# once titled a verdict "## Review verdict (QA): PASS"; a regex without a
# lookahead counted it as a reviewer verdict, and the PR looked ready for
# the owner without any review. Now the reviewer verdict header does NOT
# allow a QA bracket right after the word verdict; a QA verdict must use the
# QA_VERDICT_MARKER header. The "ready for the owner" gate (a verdict on the
# exact head + WCFY on the same head) reads only reviewer verdicts and
# formal owner reviews.
_VERDICT_HEADER_RE = re.compile(
    r"(?im)^#{1,4}\s*(?:re[- ]?)?review\s+verdict\b(?!\s*\(\s*qa\s*\))")
# QA verdict header: recognition only (reports, dedup of QA runs), never a
# reviewer verdict (it does not match _VERDICT_HEADER_RE).
QA_VERDICT_MARKER = "## QA verdict"
_QA_VERDICT_HEADER_RE = re.compile(
    r"(?im)^#{1,4}\s*(?:review\s+verdict\s*\(\s*qa\s*\)|qa\s+verdict)"
    r"(?=\s|:|$)")


def _iso_to_epoch(value):
    try:
        return datetime.fromisoformat(
            (value or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# Verdicts published from this date must contain a "## Calibration" block (a
# line for every applicable class of misses from the adversarial-review
# misses journal). Before the date old verdicts count as they are: without a
# cutoff the layer would cause a re-arm storm over all heads that carry
# verdicts valid under the old rules.
CALIBRATION_REQUIRED_SINCE = "2026-11-01T00:00:00Z"
_CALIBRATION_RE = re.compile(r"(?im)^#{1,4}\s*calibration\b")

# The rule "promises of the PR body and of WCFY are pinned by a mutant test"
# was loaded by a reviewer but applied partially: one mutant per fix line and
# a prose Calibration line "- contract: every PR-body promise mapped ..."
# passed the Calibration gate and closed a PASS with two promises that had no
# mutants at all. The mechanical support of the form: a PASS verdict
# published since this date must carry, ANYWHERE in the comment, a table
# "promise / mutant / test / result", otherwise the verdict does not count,
# as if Calibration were missing. RETURN without a table is allowed: a return
# does not accept the work, the error is in the safe direction.
# The position of the table in the comment does NOT matter: requiring it
# strictly inside the Calibration block rejected a valid PASS and its
# republication re-ran the whole mutant battery for nothing. The goal of the
# gate is "a PASS without a promise/mutant/test/result table does not count",
# not "a table in a particular section". Sign: a table header row with
# promise and mutant on one line.
PROMISES_TABLE_REQUIRED_SINCE = "2026-11-14T00:00:00Z"
_PROMISES_TABLE_RE = re.compile(
    r"(?im)^\|\s*\**\s*promise[^\n]*mutant")


def _calibration_has_promises_table(body):
    """True: the verdict comment carries a promises table (the name is historical).

    The table counts ANYWHERE in the comment, not only in the Calibration
    block. It is recognised by the header row '| Promise | Mutant | ...':
    promise and mutant in ONE header row. A prose line
    "- contract: every PR-body promise mapped" is not a table: it is what
    let a PASS through. A header without a mutant column
    ('| Promise | Change | Test | Result |') does not count either.
    """
    return bool(_PROMISES_TABLE_RE.search(body or ""))


def _pr_review_state(repo, number):
    """Review state of a PR: a dict, or None when gh fails.

    Fields: head (exact sha), age (age of the head, seconds), verdict (bool:
    whether there is a valid verdict on this sha, the former semantics), info
    (dict kind/at/body of the latest verdict or None), wcfy_at (epoch of the
    latest WCFY comment or None).

    A verdict is a formal APPROVED/CHANGES_REQUESTED review on the exact head
    or a comment with a '## ... review verdict' header (including
    'Re-review verdict') that contains the head sha in full or as a prefix of
    at least 7 characters. A coder comment "pushed head X, waiting for a
    verdict" is not a verdict: it has no verdict header.

    A verdict comment published since CALIBRATION_REQUIRED_SINCE without a
    '## Calibration' block does NOT count as a verdict: re-arm, as if there
    were no verdict. The most recent verdict on the head decides: older ones
    without the block are grandfathered. Formal reviews are the owner's
    verdicts, the requirement does not apply to them.
    """
    out = run(["gh", "pr", "view", str(number), "--repo", repo, "--json",
               "state,headRefOid,baseRefName,author,commits,mergeable,"
               "reviews,comments"],
              timeout=90)
    try:
        data = json.loads(out)
    except Exception:
        return None
    head = data.get("headRefOid") or ""
    if not head:
        return None
    age = None
    head_commit_at = None
    head_by_pr_author = False
    commits = data.get("commits") or []
    if commits:
        last = commits[-1] or {}
        committed = _iso_to_epoch(last.get("committedDate"))
        if committed is not None:
            age = max(0.0, time.time() - committed)
            head_commit_at = committed
        # The head commit belongs to the executor of the PR (the author of
        # the branch), not to an outside edit: the head is moved by its own
        # run. The PR author is an App login ("app/<bot>"), the commit is
        # signed by the bot login ("<bot>[bot]"): compare normalized forms.
        def _actor(s):
            return ((s or "").lower().replace("app/", "")
                    .replace("[bot]", "").strip())
        pr_author = _actor((data.get("author") or {}).get("login"))
        names = set()
        for a in last.get("authors") or []:
            names.add(_actor(a.get("login")))
            names.add(_actor(a.get("name")))
        head_by_pr_author = bool(pr_author and pr_author in names)
    info = _latest_verdict(data, head)
    # The first non-verdict comment AFTER the latest reject (the owner on
    # any head, or the reviewer): "## Rework: ..." is the executor's answer
    # to the verdict. A WCFY comment does not count as an answer.
    reject_at = None
    if info and info.get("kind") == "reject":
        reject_at = info.get("at")
    if reject_at is None:
        for r in reversed(data.get("reviews") or []):
            if (r or {}).get("state") == "CHANGES_REQUESTED":
                reject_at = _iso_to_epoch(r.get("submittedAt"))
                break
    reply_after_reject = None
    if reject_at is not None:
        for comment in data.get("comments") or []:
            body_c = (comment or {}).get("body") or ""
            at = _iso_to_epoch((comment or {}).get("createdAt"))
            if at is None or at < reject_at:
                continue
            if _VERDICT_HEADER_RE.search(body_c):
                continue
            if body_c.lstrip().lower().startswith(
                    WCFY_MARKER.lower()):
                continue
            reply_after_reject = at
            break
    # verdict: strictly bound to the exact head.
    # info: the latest meaningful verdict on the PR (the owner on any head);
    # WCFY is not started on top of a CR.
    verdict_head = _latest_verdict(data, head, owner_any_head=False)
    return {"head": head, "age": age, "verdict": verdict_head is not None,
            "info": info,
            # Class of the strict verdict on the exact head (None = no
            # verdict on the head) and the sign "the owner ever gave a
            # formal review to this PR" (any head). The first feeds the CR
            # rework generator, the second the finding "a PR of the active
            # milestone fell off the board".
            "head_kind": (verdict_head or {}).get("kind"),
            "head_at": (verdict_head or {}).get("at"),
            # The first reject on the exact head (a duplicate verdict must
            # not disqualify the answering card).
            "head_reject_at": _head_reject_first_at(data, head),
            "owner_review": any(
                (r or {}).get("state") in ("APPROVED", "CHANGES_REQUESTED")
                for r in data.get("reviews") or []),
            "wcfy_at": _wcfy_latest(data),
            # Head commit (epoch), whether it belongs to the executor of the
            # PR, and the first answer to a reject verdict. They retire rework
            # arms whose work was done by another run.
            "head_commit_at": head_commit_at,
            "head_by_pr_author": head_by_pr_author,
            "reply_after_reject": reply_after_reject,
            # OPEN/MERGED/CLOSED to retire stale rework arms (they are
            # retired not only when the head moves, but also when the PR
            # itself has left OPEN).
            "pr_state": data.get("state") or "",
            # CONFLICTING/MERGEABLE/UNKNOWN. A conflicting PR has no merge
            # ref, so no pull_request CI run is created at all, and a PASS on
            # its head is impossible.
            "mergeable": data.get("mergeable") or "",
            "base": data.get("baseRefName") or ""}


def _pr_closing_issues(repo, number):
    """Closing issue numbers of PR #N: [] when gh fails.

    The link "an executor card works on this PR" when the card was created
    BEFORE the PR was opened and does not name it: an architect's card names
    the issue ("issue #N C3"), the PR is opened later with the same closing
    issue. Such a running card has only the issue in its body, neither the
    PR nor the head.
    """
    out = run(["gh", "pr", "view", str(number), "--repo", repo, "--json",
               "closingIssuesReferences"], timeout=60)
    try:
        data = json.loads(out)
    except Exception:
        return []
    nums = []
    for r in data.get("closingIssuesReferences") or []:
        try:
            nums.append(int((r or {}).get("number")))
        except (TypeError, ValueError):
            continue
    return nums


# Two carriers of a verdict compete in time. The formal GitHub review is
# given by the owner (a bot cannot review a PR opened by the same App, so the
# reviewer writes a comment), and the two get mixed: a reviewer PASS comment,
# then a formal owner CR "code accepted, WCFY remains". The class of a
# verdict is taken from the verdict line: the essay below the header quotes
# old verdicts ("this closes the CHANGES REQUESTED of ..." inside an
# APPROVED), and a class over the whole body flips the verdict.
# A fixed 240-character window was not enough either: "PASS on head
# `d4c776a...` (re-review after CHANGES REQUESTED on `0d272f9`; ...)" names
# the old CR on the deciding line ITSELF; reject was checked first and
# flipped the PASS. Now the class is read only from the deciding line
# (_verdict_deciding_line), see below.
# A reviewer's deciding line may open with the verb first ("**REQUEST
# CHANGES**, one merge-order blocker plus one major finding") and was parsed
# as other: the CR rework generator did not see the CR. The owner's passive
# form ("CHANGES REQUESTED: minor") stays the first alternative.
_VERDICT_REJECT_RE = re.compile(
    r"(?i)(?:changes\s+requested|request(?:ed|s)?\s+changes)")
# The deciding line may carry the verdict history LEFT of the token
# ("...previous verdict REQUEST CHANGES, issuecomment-...): **PASS**."): the
# "leftmost wins" heuristic flipped the PASS into a reject, and the CR rework
# generator armed a rework on an accepted head. The reviewer puts the
# decision in bold: a bold token beats "leftmost wins"; without bold the
# leftmost heuristic works as before.
_VERDICT_BOLD_RE = re.compile(
    r"(?i)\*\*\s*(pass(?:ed)?|approve[ds]?|accept(?:ed|ance)?|"
    r"changes\s+requested|request(?:ed|s)?\s+changes)\s*\*\*")
# The verdict word on the deciding line, with word forms ("ACCEPTED
# (reviewer, round 3, head ...)"). The old _VERDICT_ACCEPT_RE required
# "verdict ... pass" as one phrase, which does not fire on a deciding line
# "PASS on head X" without the word verdict on it.
_VERDICT_ACCEPT_WORD_RE = re.compile(
    r"(?i)\b(?:pass(?:ed)?|approve[ds]?|accept(?:ed|ance)?)\b")
# The marker of the summary comment is the same as pr-hygiene.SUMMARY_MARKER
# (case does not matter). The PR body does not count: an edit of the body
# has no date, and the freshness of WCFY relative to the head is checked
# mechanically only through a dated comment (a live case: WCFY lived in the
# body for weeks, pr-hygiene stayed silent, and the owner read an old form
# of the change).
WCFY_MARKER = "## What changes for you"
# WCFY is a comment that STARTS with the WCFY_MARKER header (the first
# non-empty line = the header line). A mention of the phrase in other text
# does not make WCFY: verdicts like "the `## What changes for you` section is
# now due on this final head" gave a wcfy_at newer than the head by a
# substring search and silenced the arm: two accepted PRs with green CI
# stood without a WCFY card. pr-hygiene.has_user_summary also reads the PR
# body: a section with this header line, not a word in the text.
_PAIR_REF_RE = re.compile(r"#(\d+)")
# Branch A is armed only when the verdict itself names the blocker as
# paired: a false positive case: a CR on one PR referred to a second PR
# ("Note: PR #189 carries the same blob..."), but the blocker of the first
# was its own, and a PASS of the second does not close it; a true paired CR
# named pair/twin many times. A #N reference without the word of a pair is
# not armed.
_PAIR_WORD_RE = re.compile(r"(?i)(\bpair\b|\btwins?\b)")

# The owner decided that the older release line is closed after its last
# release, and there are no twins anywhere any more. The active twin
# mechanisms of the watchdog are switched off by this flag: the detector of
# twin conflicts (simultaneous start of a pair), the detector of a blocked
# twin card without a module on the target line, the paired arm (branch A:
# a CR referring to the twin's blocker). The history classifiers stay: the
# dup2 pass over duplicates, the twin grouping key of verdicts,
# lint_card_kind='twin-port' and the class "merge as a pair" in
# merge-unblocker read old data and require nothing from new PRs. Only the
# owner's decision may return True.
TWIN_ERA = False


def _verdict_deciding_line(body):
    """The deciding line of a verdict, or None.

    The header '## ... review verdict' may itself carry the class after a
    colon ('## Review verdict: CHANGES REQUESTED'); otherwise the deciding
    line is the first non-empty line under the header ('PASS on head ...',
    'CHANGES REQUESTED: minor (owner, ...)'). A body without a header (the
    owner's verdicts in free form) is scanned up to the first line with a
    verdict word. A body without a deciding line carrying a verdict word
    gets no class at all: other.
    """
    lines = (body or "").splitlines()
    for i, line in enumerate(lines):
        if not _VERDICT_HEADER_RE.search(line):
            continue
        if _VERDICT_REJECT_RE.search(line) \
                or _VERDICT_ACCEPT_WORD_RE.search(line):
            return line
        for nxt in lines[i + 1:]:
            if nxt.strip():
                return nxt
        return None
    for line in lines:
        if _VERDICT_REJECT_RE.search(line) \
                or _VERDICT_ACCEPT_WORD_RE.search(line):
            return line
    return None


def _verdict_kind(body):
    """accept/reject/other from the deciding line of a verdict.

    The class used to be taken from the first 240 characters of the body,
    reject first: a second-round PASS that mentions the old CR in brackets on
    the deciding line ("PASS on head X (re-review after CHANGES REQUESTED on
    Y)") was read as a reject. The decision of a verdict is read from its
    deciding line, not from mentions in its history: a second-round review
    almost always refers to the previous CR. Within the deciding line the
    leftmost word decides: the verdict token opens the line in the reviewer's
    format, the history follows to the right in brackets. The reverse is
    symmetric: a CR that mentions an old APPROVE stays a reject. A bold
    verdict token beats the leftmost rule.
    """
    line = _verdict_deciding_line(body) or ""
    bold = _VERDICT_BOLD_RE.search(line)
    if bold:
        word = bold.group(1)
        if _VERDICT_ACCEPT_WORD_RE.search(word):
            return "accept"
        if _VERDICT_REJECT_RE.search(word):
            return "reject"
        return "other"
    acc = _VERDICT_ACCEPT_WORD_RE.search(line)
    rej = _VERDICT_REJECT_RE.search(line)
    if acc and (not rej or acc.start() < rej.start()):
        return "accept"
    if rej:
        return "reject"
    return "other"


def _latest_verdict(data, head, owner_any_head=True):
    """The freshest valid verdict: dict(kind, at, body, src) or None.

    Two verdict carriers compete by time: the formal GitHub review (the
    owner) and a comment with a '## ... review verdict' header (the
    reviewer; the bot cannot review a PR opened by the same App). The rule
    "the verdict is read as the last one, not any one" is kept for the mixed
    carrier too: a reviewer PASS comment, then the owner's formal CR "code
    accepted, only WCFY left".

    The owner's formal reviews count on ANY head (owner_any_head): his CR
    on a PR is not lifted by the head moving. Branch B created WCFY cards
    on a reviewer PASS, half an hour later the owner gave a CR with code
    blockers, and the WCFY run went to waste (after the fix the head
    changes its sha and the comment goes stale). The reverse time order
    still decides: a reviewer PASS fresher than the owner's CR = the latest
    significant verdict is accept.

    Reviewer verdict comments are bound to a sha in the body, the filter by
    head stays for them. We scan from the end, the first one with a header
    and the head sha decides: an invalid one (after
    CALIBRATION_REQUIRED_SINCE without a Calibration block) extinguishes the
    older valid ones too, as in the former code. owner_any_head=False gives
    the strict variant "a verdict on the exact head" for the main auto-arm:
    a PR without a verdict on the current head must land in pending review
    even if an old head carries a CR.
    """
    events = []
    for r in data.get("reviews") or []:
        if (r or {}).get("state") not in ("APPROVED", "CHANGES_REQUESTED"):
            continue
        if not owner_any_head \
                and ((r.get("commit") or {}).get("oid") != head):
            continue
        events.append((
            _iso_to_epoch(r.get("submittedAt")),
            "accept" if r.get("state") == "APPROVED" else "reject",
            r.get("body") or "", "owner"))
    for comment in reversed(data.get("comments") or []):
        body = (comment or {}).get("body") or ""
        if not _VERDICT_HEADER_RE.search(body):
            continue
        if not (head in body or re.search(
                r"(?i)\b" + re.escape(head[:7]), body)):
            continue
        created = (comment or {}).get("createdAt") or ""
        if created >= CALIBRATION_REQUIRED_SINCE \
                and not _CALIBRATION_RE.search(body):
            break
        # A PASS without a promises table in the comment is an invalid
        # verdict and extinguishes the older valid ones too (see above); the
        # position of the table in the comment does not matter.
        if created >= PROMISES_TABLE_REQUIRED_SINCE \
                and _verdict_kind(body) == "accept" \
                and not _calibration_has_promises_table(body):
            break
        events.append((_iso_to_epoch(created), _verdict_kind(body), body,
                       "reviewer"))
        break
    if not events:
        return None
    events.sort(key=lambda e: (e[0] is not None, e[0] or 0))
    # src names the verdict carrier (owner: formal review; reviewer: a
    # verdict comment of the reviewer agent). Native-or-live receipts and
    # handoff comments must name it correctly: a reviewer verdict was once
    # signed as "the owner's verdict".
    return {"kind": events[-1][1], "at": events[-1][0],
            "body": events[-1][2], "src": events[-1][3]}


def _head_reject_first_at(data, head):
    """Epoch of the FIRST reject verdict on the exact head, or None.

    Why the first and not the latest (_latest_verdict gives the latest): a
    rework card is created as an answer to the verdict, and the reviewer may
    duplicate the same RETURN on the same head AFTER the card was created.
    Comparing with the duplicate would disqualify the answering card.
    "Created after the verdict" means after the first reject on the current
    head. Validity is the same as in _latest_verdict: Calibration is
    required since CALIBRATION_REQUIRED_SINCE; the promises table is needed
    only by accept verdicts and does not affect a reject.
    """
    best = None

    def up(at):
        nonlocal best
        if at is not None and (best is None or at < best):
            best = at

    for r in data.get("reviews") or []:
        if (r or {}).get("state") != "CHANGES_REQUESTED":
            continue
        if ((r.get("commit") or {}).get("oid") or "") != head:
            continue
        up(_iso_to_epoch(r.get("submittedAt")))
    for comment in data.get("comments") or []:
        body = (comment or {}).get("body") or ""
        if not _VERDICT_HEADER_RE.search(body):
            continue
        if not (head in body or re.search(
                r"(?i)\b" + re.escape(head[:7]), body)):
            continue
        created = (comment or {}).get("createdAt") or ""
        if created >= CALIBRATION_REQUIRED_SINCE \
                and not _CALIBRATION_RE.search(body):
            continue
        if _verdict_kind(body) != "reject":
            continue
        up(_iso_to_epoch(created))
    return best


def _wcfy_comment_is(body):
    """True: the comment is the WCFY itself.

    The first non-empty line of the body must be the WCFY_MARKER header line
    (case does not matter, exactly the header with no continuation on the
    line). A mention of "## What changes for you" inside the prose of another
    comment (a verdict, a report) does not make it WCFY.
    """
    for line in (body or "").splitlines():
        if not line.strip():
            continue
        return line.strip().lower() == WCFY_MARKER.lower()
    return False


def _wcfy_latest(data):
    """Epoch of the latest WCFY comment, or None."""
    latest = None
    for comment in data.get("comments") or []:
        body = (comment or {}).get("body") or ""
        if not _wcfy_comment_is(body):
            continue
        at = _iso_to_epoch((comment or {}).get("createdAt"))
        if at is not None and (latest is None or at > latest):
            latest = at
    return latest


# A verdict is issued on the WHOLE head, with its CI. A PASS on a head with
# a red mandatory check is not a PASS: whatever the cause of the failure
# (in the stack, at a twin, a flake), "after PASS" work is not armed on such
# a head. The mandatory set is read from the branch protection of the PR
# base (the app has Administration: read), the check states of the head from
# gh pr checks.
_CI_OK_STATES = ("SUCCESS",)


def _head_mandatory_ci(repo, number, base, protect_cache):
    """(True, "") if all mandatory checks of the head are green.

    A live case: a verdict "PASS on this PR's diff" on a head with a red
    mandatory check (the failure was attributed to a blocker in the stack)
    was read by branch B as accept, and the watchdog created a WCFY card on
    a head that was waiting for the stack to be rebuilt after the fix: a
    coder slot and quota would have gone to waste.

    Semantics:
    - the mandatory set = required_status_checks.contexts of the base
      protection; in the repository the contexts match the check names of
      gh pr checks line by line;
    - the protection cannot be read (network, permission, a branch without
      protection) = the set is unknown: conservatively ALL checks of the
      head count as mandatory, the error only goes in the direction of "do
      not arm", the next hourly pass repeats;
    - protection exists but contexts is empty = there are no mandatory
      checks: a red non-mandatory job does not block the arm;
    - a mandatory check without a run on the head (did not start) or not in
      SUCCESS (FAILURE, PENDING) = not green;
    - the check list of the head cannot be read at all = do not arm, retry
      later.
    """
    checks_out = run(["gh", "pr", "checks", str(number), "--repo", repo,
                      "--json", "name,state"], timeout=90)
    try:
        checks = json.loads(checks_out) or []
        states = {(c or {}).get("name"): (c or {}).get("state")
                  for c in checks if (c or {}).get("name")}
    except Exception:
        return False, "the check list of the head cannot be read"
    if not states:
        return False, "the head has no checks at all"
    if (repo, base) not in protect_cache:
        prot_out = run(["gh", "api",
                        "repos/%s/branches/%s/protection" % (repo, base)],
                       timeout=90)
        try:
            contexts = ((json.loads(prot_out) or {}).get(
                "required_status_checks") or {}).get("contexts")
        except Exception:
            contexts = None
        protect_cache[(repo, base)] = (
            list(contexts) if contexts is not None else None)
    required = protect_cache.get((repo, base))
    if required is None:
        required = list(states)
    bad = [n for n in required if states.get(n) not in _CI_OK_STATES]
    if bad:
        detail = ", ".join("%s=%s" % (n, states.get(n) or "no run")
                           for n in bad)
        return False, detail
    return True, ""


def svc_priority(slug, number, default):
    """Milestone band for a service card of a PR.

    Reads the admission gate cache (refreshed by every hourly run):
    band = max(bands of the PR's issues from Refs/Closes and of the PR's own
    milestone). The top band "finish to merge" (the active milestone, 80)
    keeps the original default (100/95, "80 or higher"), other milestones
    give their exact band (the next milestone, 40). No data -> default: the
    admission gate corrects the priority on its next run.
    """
    try:
        with open(os.path.join(BOARDS, slug, ADMISSION_CACHE_NAME)) as fh:
            cache = json.load(fh)
    except Exception:
        return default
    ms = ms_bands(slug)
    band = None

    def up(b):
        nonlocal band
        if b and (band is None or b > band):
            band = b

    for i in (cache.get("pr_issues") or {}).get(str(number), []):
        up(ms["bands"].get(
            (cache.get("issues") or {}).get(str(i))))
    up(ms["bands"].get(
        (cache.get("pr_milestones") or {}).get(str(number))))
    if band is None:
        return default
    if band == milestone_bands.ACTIVE_BAND:
        return max(default, band)
    return band


def _live_keys(slug):
    """Keys of live board cards (not done/archived) for the native guard."""
    db = os.path.join(BOARDS, slug, "kanban.db")
    if not os.path.exists(db):
        return []
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True,
                              timeout=10)
        rows = con.execute(
            "SELECT idempotency_key FROM tasks "
            "WHERE status NOT IN ('done','archived') "
            "AND idempotency_key IS NOT NULL").fetchall()
        con.close()
        return [r[0] for r in rows]
    except sqlite3.Error:
        return []


def _review_cycle_live(slug, number, head):
    """A fresh re-read of the board right BEFORE create.

    A board snapshot taken earlier in the pass gets about a minute of gh
    requests old. Within that minute an executor card may request review
    and the stock review lane may claim it; a card created from the stale
    snapshot then duplicates a review that is already running.

    Returns [(task_id, reason)]: live cards that name PR #number and already
    carry the STOCK review cycle of THIS head: status review
    (kanban_request_review moves the card into the review lane, the
    dispatcher starts the review run itself) or running with a claim of
    source_status=review. A card that does not name OUR head explicitly
    (another head, or no sha at all) does not count as busy for our head:
    the cycle of an old head does not cover a new one. The stock
    review_requested event always names the sha.
    An empty list = no stock cycle.
    """
    db = os.path.join(BOARDS, slug, "kanban.db")
    if not os.path.exists(db):
        return []
    pat = re.compile(r"#%d\b" % number)
    head7 = (head or "")[:7]
    out = []
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True,
                              timeout=10)
    except sqlite3.Error:
        return []
    try:
        rows = con.execute(
            "SELECT id, status, title, body FROM tasks "
            "WHERE status IN ('review', 'running')").fetchall()
        for tid, status, title, body in rows:
            blob = "%s %s" % (title or "", body or "")
            for (c,) in con.execute(
                    "SELECT body FROM task_comments WHERE task_id=? "
                    "ORDER BY id DESC LIMIT 3", (tid,)):
                blob += " " + (c or "")
            for (p,) in con.execute(
                    "SELECT payload FROM task_events WHERE task_id=? AND "
                    "kind IN ('review_requested','changes_requested') "
                    "ORDER BY id DESC LIMIT 3", (tid,)):
                blob += " " + (p or "")
            for (s,) in con.execute(
                    "SELECT summary FROM task_runs WHERE task_id=? AND "
                    "summary IS NOT NULL ORDER BY id DESC LIMIT 2", (tid,)):
                blob += " " + (s or "")
            if not pat.search(blob):
                continue
            heads = [m[:7] for m in re.findall(
                r"\b([0-9a-f]{40})\b", blob)]
            heads += [m[:7] for m in re.findall(
                r"(?:head|sha)[ :]*`?([0-9a-f]{7,40})",
                blob, re.I)]
            # names ANOTHER head explicitly or none at all: our head is not
            # confirmed busy (the cycle of an old or unknown head does not
            # cover a new one)
            if head7 not in heads:
                continue
            if status == "review":
                out.append((tid, "review"))
                continue
            for (p,) in con.execute(
                    "SELECT payload FROM task_events WHERE task_id=? AND "
                    "kind='claimed' ORDER BY id DESC LIMIT 3", (tid,)):
                try:
                    if json.loads(p or "{}").get(
                            "source_status") == "review":
                        out.append((tid, "running:review-claim"))
                        break
                except ValueError:
                    continue
    finally:
        con.close()
    return out


def _kanban_create(slug, title, body, assignee, priority, key, skills=(),
                   guard=None):
    """hermes kanban create through a body file. The new card id or None.

    assignee is required. Cards created without an assignee are never
    started by the dispatcher (skipped_unassigned): such a card is dead
    from birth. An empty assignee here is a bug of the calling code, so it
    is refused at once instead of leaving a dead card on the board.

    guard is required and must be a GuardNote from
    board_native_guard.native_or_live (a receipt "stock checked, nothing
    live"). There are no direct calls that bypass the receipt: every
    generator asks the helper first, covered=True means "do not create".
    """
    if board_native_guard is None:
        raise ValueError(
            "_kanban_create: native guard unavailable: creating cards "
            "is forbidden while scripts/board_native_guard.py "
            "does not load")
    if not isinstance(guard, board_native_guard.GuardNote):
        raise ValueError(
            "_kanban_create: a guard receipt is required (native guard "
            "rule, key %s): call board_native_guard.native_or_live and pass "
            "its result; covered=True = do not create at all" % key)
    body = board_native_guard.guard_section(guard) + (body or "")
    if not (assignee or "").strip():
        raise ValueError(
            "_kanban_create: empty assignee (slug=%s key=%s): a card"
            " without an assignee is never started by the dispatcher"
            % (slug, key))
    body_path = None
    try:
        with tempfile.NamedTemporaryFile(
                "w", suffix=".md", encoding="utf-8", delete=False) as fh:
            fh.write(body)
            body_path = fh.name
        cmd = [HERMES, "kanban", "--board", slug, "create", title,
               "--body-file", body_path, "--assignee", assignee,
               "--priority", str(priority), "--idempotency-key", key,
               "--created-by", "watchdog"]
        for s in skills:
            cmd += ["--skill", s]
        out = run(cmd + ["--json"], timeout=120)
        try:
            return (json.loads(out) or {}).get("id")
        except Exception:
            m = re.search(r'"id"\s*:\s*"(t_[0-9a-f]+)"', out or "")
            return m.group(1) if m else None
    finally:
        if body_path:
            try:
                os.unlink(body_path)
            except OSError:
                pass


def _family_arm_key(base, live, done_keyed):
    """The arm key for the family base: (state, key, rearm_of).

    state 'live' (key/rearm_of None): live has a card with the key base or
    base-rN, duplicating is forbidden. 'fresh': the key is clean. 'rearm':
    the whole family is done, the key is a fresh base-rN, rearm_of = ids of
    the closed cards of the family. The -rN suffixes are the same as in the
    main auto-arm (dedup against a done card did not re-arm before).
    """
    fam_live = [t[6] for t in live
                if (t[6] or "") == base
                or (t[6] or "").startswith(base + "-r")]
    if fam_live:
        return "live", None, None
    fam_done = [d_id for d_id, d_key in done_keyed
                if d_key == base or (d_key or "").startswith(base + "-r")]
    if not fam_done:
        return "fresh", base, None
    max_r = 1
    for _, d_key in done_keyed:
        m = re.fullmatch(re.escape(base) + r"-r(\d+)", d_key or "")
        if m:
            max_r = max(max_r, int(m.group(1)))
    return "rearm", "%s-r%d" % (base, max_r + 1), ", ".join(fam_done)


def _fmt_utc(at):
    """Epoch -> 'YYYY-MM-DD HH:MMZ' or 'none' for None."""
    if not at:
        return "none"
    return time.strftime("%Y-%m-%d %H:%MZ", time.gmtime(at))


def _title_owns_pr(title, number):
    """True: the card title names PR #N as the object of the work.

    The PR number must be in the title (the body does not count). Mentions
    are filtered out: "(survivor J-H from the review of PR #A)" is work on an
    issue, not on PR #A; "Rebase and land PR #B (waits for #A)" is work on
    #B, not on #A. A mention does not count when a word of another role
    stands within 24 characters before #N: review, waiting, merge source.
    """
    _MENTION_STOP = ("review", "wait", "merge of", "after")
    for m in re.finditer(r"#%d\b" % number, title or ""):
        pre = (title[:m.start()])[-24:].lower()
        if not any(w in pre for w in _MENTION_STOP):
            return True
    return False


# A service card that waits for a merge or for the owner is not live work
# on a PR. Example: "[issue-close] close issue #N after the merge of PR #M"
# (blocked, waits for the merge) carries "#M" in the title and the head in
# the body, and a check "a live coder card works with this head" counted it
# as work. The trigger of such cards is outside the board (a PR merge, an
# owner decision); they never move the PR themselves. Classes: issue-close
# on merge, hold, waiting for a merge or for the owner.
_SERVICE_WAIT_CARD_RE = re.compile(
    r"(?i)\[\s*(?:issue-close|hold|wait-merge|wait-owner|"
    r"wait-for-merge|service)\s*\]"
    r"|issue-close\b"
    r"|\bclose\s+issue\b[^\n]{0,100}(?:after|before)\s+(?:the\s+)?merg\w*"
    r"|(?:\bwait(?:s|ing)?|\bawait(?:s|ing)?)\s+(?:for\s+)?"
    r"(?:(?:the\s+)?merge\s+of\s+)?(?:PR|pull request)\s*#"
    # A follow-up "(issue #N, after the merge of PR #M)" (blocked until the
    # merge, the head of #M quoted in the body as context) is a merge wait
    # without the words "waits"/"close issue". "until/before the merge"
    # counts ONLY with a block marker: "land this before the merge of PR #M"
    # is real pre-merge work on the PR.
    r"|\bafter\s+(?:the\s+)?merg\w*\s+(?:of\s+)?(?:PR|pull request)\s*#"
    r"|\bblock(?:ed|s)?\s+(?:until|till|before)\s+(?:the\s+)?merg\w*")


def _is_service_wait_card(title, body):
    """True: a service card whose trigger is a merge or the owner."""
    return bool(_SERVICE_WAIT_CARD_RE.search(
        (title or "") + "\n" + (body or "")))


# A context quote of a PR head. A card "Issue #N: Before/After from the
# rework head of PR #M (abc1234)" quotes the head of the PR as the SOURCE of
# its screenshots; it does not move the PR, does not request review, and no
# changes_requested will come back to it. Same class as the service cards
# above (a side card silenced live work on the PR), another kind: not a
# merge wait but work on its own artifact with somebody else's head as the
# source. The structural sign is double: the card declares ITS object as an
# issue in the first word of the title ("Issue #N: ..."), and the PR head is
# quoted with source words ("from the rework head of PR #", "frames from
# the head of PR #"). The issue prefix alone, without source words, does NOT
# match: "Issue #N: text fix in PR #M" may be real work on PR #M, and its
# cycle must not be silenced (conservative on purpose).
_OWN_ISSUE_TITLE_RE = re.compile(r"(?i)^issue\s*#(\d+)\s*:")
_CONTEXT_CITE_PR_RE = re.compile(
    r"(?i)(?:from|of)\s+(?:the\s+)?(?:rework\s+)?head\s+of\s+"
    r"(?:the\s+)?(?:rework\s+of\s+)?(?:PR|pull\s*request)\s*#"
    r"|\bheads?\b[^#\n]{0,30}\b(?:context|source)\b")


def _is_context_cite_card(title, body):
    """True: a card of its own issue quotes a PR head as its source."""
    if not _OWN_ISSUE_TITLE_RE.search(title or ""):
        return False
    return bool(_CONTEXT_CITE_PR_RE.search(title or "")
                or _CONTEXT_CITE_PR_RE.search(body or ""))


def cr_rework_gaps(states, live, pilot_numbers=()):
    """[(number, head)] with a CR on the head and no live work.

    PRs with CHANGES REQUESTED stood for hours without a rework card: the
    verdict cleared the pending state and the CR work went to nobody. The
    arm is suppressed by a live executor card with this PR in its title (the
    stock review cycle returns changes_requested to it) or by a live
    reviewer card of the PR (review in flight). pilot_numbers are skipped.
    """
    gaps = []
    for number in sorted(states):
        if number in pilot_numbers:
            continue
        st = states[number]
        if st.get("head_kind") != "reject":
            continue
        if any(t[3] != "reviewer" and _title_owns_pr(t[1], number)
               and not _is_service_wait_card(t[1], t[2])
               for t in live):
            continue
        if any(t[3] == "reviewer"
               and (re.search(r"#%d\b" % number,
                              (t[1] or "") + " " + (t[2] or ""))
                    or ("pr%d-" % number) in (t[6] or ""))
               for t in live):
            continue
        gaps.append((number, st["head"]))
    return gaps


def milestone_pr_orphans(states, live, admit, pilot_numbers=(),
                         owner_by_pr=None):
    """[number] of active-milestone PRs that have no card.

    A PR with admission (active milestone, approved issue, gen_admission),
    without a live card that has this PR in its title and without a formal
    owner review. Such PRs stood green waiting for the owner while the board
    did not show them at all. The head must be at least REVIEW_ARM_MIN_AGE_S
    old: a freshly opened PR may not have its card yet.
    A PR with a live owner card (owner_by_pr, live_cards_by_pr ranks 0-3)
    is not an orphan: a decomposition card without the PR number in its
    title is assigned work.
    """
    orphans = []
    for number in sorted(states):
        if number in pilot_numbers:
            continue
        if (owner_by_pr or {}).get(number):
            continue
        st = states[number]
        age = st.get("age")
        if age is None or age < REVIEW_ARM_MIN_AGE_S:
            continue
        if st.get("owner_review"):
            continue
        if any(_title_owns_pr(t[1], number)
               and not _is_service_wait_card(t[1], t[2])
               for t in live):
            continue
        if not admit(number):
            continue
        orphans.append(number)
    return orphans


def _verdict_source_name(info):
    """The CR verdict source as text for receipts and handoffs.

    "the reviewer agent" (a verdict comment: the bot cannot formally review
    a PR opened by the same App) or "the owner" (a formal review). An
    unknown source is conservatively "the owner" (the former text).
    """
    return "the reviewer agent" if (info or {}).get("src") == "reviewer" \
        else "the owner"


def _sibling_pr_hygiene():
    """pr-hygiene through the cycle guard convention (key hermes_sibling).

    The PR owner lookup lives in pr-hygiene.live_cards_by_pr (the rebase
    generator hands rebases to the owner through it); the watchdog takes
    the same function instead of a second copy of the "one card = one PR"
    rule.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "pr-hygiene.py")
    key = "hermes_sibling:" + os.path.abspath(path)
    mod = sys.modules.get(key)
    if mod is not None and hasattr(mod, "live_cards_by_pr"):
        return mod
    if not os.path.exists(path):
        return None
    spec = _ilu.spec_from_file_location("pr_hygiene_wd", path)
    mod = _ilu.module_from_spec(spec)
    sys.modules[key] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(key, None)
        return None
    return mod


def _rework_owner_map(db, head_of, live):
    """{PR: [(id, title, rank, key)]} of live cards that own a PR.

    The CR rework arm saw only titles with #N and reviewer keys, missed the
    decomposition owner card (rank 3, key coder-<issue>-<slug>) and created
    a second card on a live PR while the owner was already pushing the fix.
    Owners are found by pr-hygiene.live_cards_by_pr (the same function the
    rebase generator uses): branch 0 > key 1 > title 2 > decomposition
    owner 3. Service waits and context quotes do not count as owners. A
    failing or missing sibling = no data = the arm behaves as before.
    """
    mod = _sibling_pr_hygiene()
    if mod is None or not head_of:
        return {}
    old_db = getattr(mod, "BOARD_DB", None)
    try:
        if db:
            mod.BOARD_DB = db
        by_pr = mod.live_cards_by_pr(head_of) or {}
    except Exception:
        return {}
    finally:
        if old_db is not None:
            mod.BOARD_DB = old_db
    by_id = {t[0]: t for t in live}
    out = {}
    for number, cards in by_pr.items():
        keep = []
        for card in cards:
            t = by_id.get(card[0])
            if t is not None and (_is_service_wait_card(t[1], t[2])
                                  or _is_context_cite_card(t[1], t[2])):
                continue
            keep.append((card[0], card[1], card[2], card[3]))
        if keep:
            out[number] = keep
    return out


def _rework_owner_handoff(owner_by_pr, number, head, info):
    """(keeper_id, verdict source name) | None: a live owner retires the arm.

    Keeper = the strongest carrier (lowest rank), on a tie the first by id:
    deterministic between watchdog runs.
    """
    owners = (owner_by_pr or {}).get(number) or []
    if not owners:
        return None
    keeper = min(owners, key=lambda c: (c[2], c[0] or ""))
    return keeper[0], _verdict_source_name(info)


def _card_commented(db, task_id, marker):
    """True: the card already has a comment carrying the marker.

    Makes handoff comments idempotent: the watchdog runs every few minutes,
    and without the marker the owner would get a reminder on every run
    while the CR stands.
    """
    if not db or not os.path.exists(db):
        return False
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
        row = con.execute(
            "SELECT 1 FROM task_comments WHERE task_id = ? AND body LIKE ?"
            " LIMIT 1", (task_id, "%" + marker + "%")).fetchone()
        con.close()
    except sqlite3.Error:
        return False
    return row is not None


OWNER_REWORK_MARKER = "owner-rework"


def _rework_arm_card(slug, number, head, info, key=None, rearm_of=None,
                     guard=None):
    """Coder rework card for a CR verdict on the exact head.

    Priority through svc_priority(default 90): the active milestone band
    keeps 90 (the rework of an open PR goes before new work of the band,
    below review 100 and WCFY 95), other milestones give their exact band.
    Live cards below the target are lifted by rework_priority_lift.
    The key is built by the shared helper board_native_guard.rework_key
    (coder-pr<N>-rework-<head7>), the same for all rework generators.
    """
    key = key or board_native_guard.rework_key(number, head)
    rearm_note = ""
    if rearm_of:
        rearm_note = (
            "\n\nRe-arm: the previous card(s) of the family %s are closed,"
            " but the CR verdict on head %s still stands. Key %s is fresh."
            % (rearm_of, head[:7], key))
    title = ("[coder] PR #%d (head %s): rework by verdict "
             "(watchdog auto-arm)" % (number, head[:7]))
    body = (
        "PR #%d, exact head with the verdict: %s (latest verdict: %s).\n\n"
        "Created by the watchdog automatically: the head carries "
        "CHANGES REQUESTED, and there was no live card working on this PR "
        "(otherwise such PRs stand for hours while the slots go to other "
        "work).%s\n\n"
        "Task: read the verdict in PR #%d (a comment with the header "
        "\\\"## Review verdict\\\" or a formal CR) and close all blockers. "
        "Fixes go to the PR branch, a new head, CI green, then a MANDATORY "
        "rework response: a comment in the PR answering EVERY BLOCK item "
        "of the verdict (what was done, commit, mutation). Without it the "
        "reviewer sees a bare new head and does not know how the verdict "
        "was closed, and the review stalls for hours. The duty is carried "
        "by ANY card that delivers the rework, including a decomposition "
        "child: the template is not visible there, the duty remains (skill "
        "pr-deliverable). After the answer, kanban_request_review "
        "with reviewer=reviewer (explicitly) and leave the run. Without "
        "reviewer= the card is not reassigned and the executor takes the "
        "review column again: a self-review loop. "
        "Before kanban_request_review check for a live reviewer "
        "card on the NEW head (key reviewer-pr%d-<head7>): if it already "
        "reviews this head, do not request review, finish the run with a "
        "link to it (the duplicate session is retired by the watchdog). "
        "If the verdict needs no new code (for example "
        "only a screenshot or text), publish what is required in the PR "
        "bound to the head, request a re-review and leave. Do NOT wait for "
        "the reviewer's verdict inside the run: it arrives as an event on "
        "the new head, and waiting holds a dispatcher slot for hours. Do "
        "not mark the card done before the new verdict (the next run does "
        "it on the event); a reject on a head without live work is a "
        "watchdog finding, it will recreate the rework.\\n\\n"
        "Comment hygiene: until the first run of this card, the PR is "
        "mentioned in its comments ONLY by number, without a URL: an "
        "issuecomment link in a fresh comment freezes the card under the "
        "dispatcher's active_pr guard for 24 h. Pass evidence from a "
        "neighbouring run by PR number and head sha, or accompany the "
        "comment with a handoff event (reassign)."
        % (number, head, _fmt_utc((info or {}).get("at")), rearm_note,
           number, number))
    return _kanban_create(slug, title, body, "coder",
                          svc_priority(slug, number, 90), key,
                          ("pr-deliverable",), guard=guard)


def _owner_deferred_review(db, task_id):
    """True: the card stands in scheduled by a decision outside the review layer.

    Reviewer cards moved to a backlog (outside the milestone, the owner's
    decision) will not start until his decision, while their heads without a
    verdict kept the review layer non-empty. The layer waited for a review
    that could not start and parked ready cards below p50 forever: a circle
    "a backlog parks the review of heads, the layer waits for that same
    review". Rule: a head whose review is deferred by someone else's
    decision holds no slot.

    Deferred = the latest SCHEDULED reason of the card does NOT contain our
    own marker review-layer hold (otherwise the layer would lock itself
    again). No SCHEDULED reason at all = not classified, the layer
    conservatively holds.
    """
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
        row = con.execute(
            "SELECT body FROM task_comments WHERE task_id = ? AND "
            "body LIKE 'SCHEDULED: %' ORDER BY created_at DESC, id DESC "
            "LIMIT 1", (task_id,)).fetchone()
        con.close()
    except sqlite3.Error:
        return False
    if not row:
        return False
    return REVIEW_PARK_MARKER not in (row[0] or "")


# --- the scheme "how the board leads a PR from decomposition to merge" ---


def ancestor_priority_lint(slug, kids):
    """A live child of a live parent is not lower than the parent in priority.

    Reason: the stock decomposer (_insert_decomposed_child,
    hermes_cli/kanban_db_graph.py) gives children priority 0, the root's
    priority is not inherited, and children sink under the work of the
    priority-80 lane. We do not patch the core: the layer raises the child's
    priority to the priority of its highest-priority live parent.
    Idempotent by the marker comment "I7: ancestor priority". Returns
    findings (key, level, text).
    """
    db = os.path.join(BOARDS, slug, "kanban.db")
    if not os.path.exists(db):
        return []
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
    prio = {r[0]: (r[1], r[2]) for r in con.execute(
        "select id, status, priority from tasks")}
    marked = {r[0] for r in con.execute(
        "select task_id from task_comments where body like '%I7: ancestor priority%'")}
    con.close()
    found, raised = [], []
    for parent, children in sorted(kids.items()):
        p = prio.get(parent)
        if not p or p[0] in ("done", "archived"):
            continue
        for child in sorted(children):
            c = prio.get(child)
            if not c or c[0] in ("done", "archived") \
                    or c[1] >= p[1] or child in marked:
                continue
            run([HERMES, "kanban", "--board", slug, "edit", child,
                 "--priority", str(p[1])], timeout=90)
            run([HERMES, "kanban", "--board", slug, "comment", child,
                 "I7: ancestor priority: raised from %d to %d by the live "
                 "parent %s (the core does not inherit priority to children)."
                 % (c[1], p[1], parent)], timeout=90)
            raised.append("%s %d->%d (parent %s)" % (child, c[1], p[1], parent))
    if raised:
        found.append(("%s:i7:raise" % slug, 1, (
            "    I7 child priority by ancestor: raised %d: %s"
            % (len(raised), "; ".join(raised[:5])))))
    return found


def overlap_pairs(open_prs, merged_pr):
    """Pure function of the overlap map: [(open_number, set(files))] against
    (merged_number, set(files)) -> [(open, merged, sorted(shared_files))]."""
    out = []
    for number, files in open_prs:
        shared = sorted(files & merged_pr[1])
        if shared:
            out.append((number, merged_pr[0], shared))
    return out


def merge_order_hint(open_prs, all_pairs):
    """Merge order: the PR with the most overlaps with recent merges goes
    first (it drags the fewest conflicts behind it)."""
    counts = {n: 0 for n, _ in open_prs}
    for open_n, _, _ in all_pairs:
        counts[open_n] = counts.get(open_n, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [n for n, _ in ranked]


def pr_overlap_map(slug, state):
    """After every merge build a map of overlaps of open PRs by files. A
    merged PR that touched an open one gives the card of that PR one decision
    "rebase needed because of #N"; the merge order goes to the report. Dedup:
    state['pr_overlap_seen'][number] = mergedAt, the comment is searched by
    the marker "rebase needed because of #".
    """
    repo = board_repo(slug)
    if not repo:
        return []
    opens_raw = run(["gh", "pr", "list", "--repo", repo, "--state", "open",
                     "--limit", "100", "--json",
                     "number,headRefName,files"], timeout=120)
    merged_raw = run(["gh", "pr", "list", "--repo", repo, "--state",
                      "merged", "--limit", "20", "--json",
                      "number,mergedAt,files"], timeout=120)
    try:
        opens = [(p["number"],
                  {f["path"] for f in (p.get("files") or [])})
                 for p in json.loads(opens_raw)]
        merged = [(p["number"], p.get("mergedAt") or "",
                   {f["path"] for f in (p.get("files") or [])})
                  for p in json.loads(merged_raw)]
    except Exception:
        return []
    seen = state.get("pr_overlap_seen")
    first_run = seen is None
    seen = seen or {}
    findings, fresh_pairs = [], []
    cards = {}
    db = os.path.join(BOARDS, slug, "kanban.db")
    if os.path.exists(db):
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
        for tid, title, body, status in con.execute(
                "select id, title, body, status from tasks "
                "where status not in ('done','archived')"):
            cards[tid] = (title or "") + " " + (body or "")
        commented = {r[0] for r in con.execute(
            "select task_id from task_comments "
            "where body like '%rebase needed because of #%'" )}
        con.close()
    else:
        commented = set()
    for mnum, when, mfiles in merged:
        if seen.get(str(mnum)) == when:
            continue   # already handled by this same watchdog
        seen[str(mnum)] = when
        if first_run:
            continue   # seeding: comment only on merges AFTER the switch-on
        for open_n, shared in overlap_pairs(opens, (mnum, mfiles)):
            fresh_pairs.append((open_n, mnum, shared))
    for open_n, mnum, shared in fresh_pairs:
        marker = "rebase needed because of #%d" % mnum
        hit = [tid for tid, text in cards.items()
               if re.search(r"#%d\b" % open_n, text) and tid not in commented]
        for tid in hit[:1]:
            run([HERMES, "kanban", "--board", slug, "comment", tid,
                 "%s (file overlap: %s). One decision per cycle: "
                 "rebase only when mergeable=CONFLICTING, the last one "
                 "before the final review."
                 % (marker, ", ".join(shared[:3]))], timeout=90)
    if fresh_pairs:
        order = merge_order_hint(opens, fresh_pairs)
        lines = ["      #%d <- #!%s" % (o, m) for o, m, s in fresh_pairs[:6]]
        findings.append(("%s:overlap:%s" % (
            slug, ",".join(str(m) for _, m, _ in fresh_pairs[:6])), 1, (
            "    Overlap map: %d open PRs touched\n%s\n"
            "      merge order: %s"
            % (len(fresh_pairs), "\n".join(lines),
               " -> ".join("#%d" % n for n in order[:5])))))
    state["pr_overlap_seen"] = seen
    return findings


# --- Admission gate on a WHITE LIST ----------------------------------------
# Only a card tied to an open issue of the repository with an assigned
# milestone is admitted to ready/todo/review (assigning a milestone is the
# owner's approval: the current one or an explicitly assigned other one), or
# a descendant of such a card by task_links, or a service card of an open PR
# whose issue is approved (review, WCFY, rebase), or the infrastructure of
# Hermes itself (the marker comment "admission: infra"). Everything else is
# taken off the queue and goes into the report findings, not into the queue.
# Why a white list: a black list only parked cards that referenced an
# unapproved issue in the body. Specify and the decomposer rewrite
# title/body, the reference disappeared, and a card without approval
# returned to ready.
# The binding is stored in a way that survives specify/the decomposer: a
# comment "issue-link: #N" in task_comments (they do not rewrite comments).
# The gate itself stamps the marker from live body references while they are
# not erased.
# Parking = the stock schedule, not block: with block the retry counter takes
# the card to triage, where specify rewrites the body again.
ADMISSION_WORK_STATUSES = ("ready", "todo", "review", "scheduled")
ADMISSION_MARKER = "admission gate:"
ISSUE_LINK_MARKER = "issue-link:"
INFRA_MARKER = "admission: infra"
# Return from parking and milestone priority bands. A return is not
# automatic by itself: when an issue is approved, the gate returns ONE
# working card per issue/PR (one live card per PR/issue), and duplicates are
# archived with a reference to it.
# The milestone priority bands are computed from GitHub on every run
# (scripts/milestone_bands.py, the single definition shared with
# pr-hygiene). Active = the open milestone with the nearest due_on (on a tie
# or without due_on, by version number); next = the open milestone with the
# smallest version above the active one. Bands: active 80, next 40, other
# open ones 20; closed milestones are not in the open list and get no band.
# A static table would silently leave the next milestone on the wrong band
# on release day.
# The active milestone of the factory is the top band. Generators do not
# create cards for work outside it at all: the band is below p50, and the
# review layer would take the card off the queue right after creation.


def _ms_fetch(repo):
    """Open milestones of the repo for milestone_bands.resolve: gh through
    the local run() (tests replace exactly that). Garbage instead of JSON
    or empty output on failure -> None; resolve then takes the last
    successful result from disk or returns "no data"."""
    out = run(["gh", "api",
               "repos/%s/milestones?state=open&per_page=100" % repo],
              timeout=60)
    try:
        items = json.loads(out or "[]")
    except Exception:
        return None
    return [{"title": m.get("title"), "due_on": m.get("due_on")}
            for m in items]


def ms_bands(slug=None, repo=None):
    """{active, next, bands, source} of the board's repository; memoized per
    process (every cron run is a new process, that is "on every run")."""
    if repo is None:
        repo = board_repo(slug or "")
    if not repo:
        return {"active": None, "next": None, "bands": {},
                "source": "nodata"}
    return milestone_bands.resolve(repo, fetch=_ms_fetch)
# Admission cache for the claim guards (the kanban-admission-guard plugin):
# lives next to the board, refreshed by every gate run.
ADMISSION_CACHE_NAME = "admission-approved.json"
NO_LINK_WHY = "no binding to an issue of the repository"


def gen_admission(slug, number):
    """Admission of a service card generator BEFORE creation.

    Returns (ok, why_not). False = do not create the card: the admission
    gate or the review layer would park it right after create. Two cases:
    a PR without a binding to an approved issue (the gate: the admission
    white list), and a PR for an issue in a milestone other than the active
    one (band below p50: the review layer takes it off the queue). The
    active milestone is computed from GitHub (ms_bands).
    The check runs before creation, not after.
    No cache or no bands -> ok: the old behaviour, the next gate run fixes
    it.
    """
    try:
        with open(os.path.join(BOARDS, slug, ADMISSION_CACHE_NAME)) as fh:
            cache = json.load(fh)
    except Exception:
        return True, ""
    if str(number) not in {str(n) for n in cache.get("prs") or []}:
        return False, ("PR without a binding to an approved issue: the "
                       "admission gate would park the card right after "
                       "creation")
    band = None

    def up(b):
        nonlocal band
        if b and (band is None or b > band):
            band = b

    ms = ms_bands(slug)
    active = ms["active"]
    for i in (cache.get("pr_issues") or {}).get(str(number), []):
        up(ms["bands"].get(
            (cache.get("issues") or {}).get(str(i))))
    up(ms["bands"].get(
        (cache.get("pr_milestones") or {}).get(str(number))))
    if band is not None and active and band != ms["bands"][active]:
        return False, ("the PR's issue is in a milestone with band p%d,"
                       " outside the active %s: the card is below p50, the"
                       " review layer would take it off the queue right"
                       " after creation" % (band, active))
    return True, ""


def open_issue_states(repo):
    """Open issues of the repo: number -> (milestone, labels, updated_epoch, body).

    updated_epoch: time of the last issue update, for the check "active
    milestone without a live card for longer than 24 h".
    body: for owner blocker marks (OWNER_MARK_RE) and for filtering out bot
    mirrors ("Mirrored from kanban card").
    A field missing in the gh answer (old test scenarios) -> None.
    """
    out = run(["gh", "issue", "list", "--repo", repo, "--state", "open",
               "--limit", "300",
               "--json", "number,milestone,labels,updatedAt,body"],
              timeout=90)
    try:
        parsed = json.loads(out)
    except Exception:
        return None
    states = {}
    for it in parsed:
        labels = {lab.get("name") for lab in it.get("labels") or []}
        milestone = (it.get("milestone") or {}).get("title")
        upd = None
        raw = it.get("updatedAt")
        if raw:
            try:
                upd = calendar.timegm(time.strptime(
                    raw[:19], "%Y-%m-%dT%H:%M:%S"))
            except Exception:
                upd = None
        states[it.get("number")] = (milestone, labels, upd,
                                    it.get("body"))
    return states


def _iso_epoch(raw):
    """RFC3339 UTC -> epoch or None (no data or garbage)."""
    if not raw:
        return None
    try:
        return calendar.timegm(time.strptime(raw[:19], "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return None


def mirror_source(body):
    """Id of the source card from the footer of an issue mirror, or None."""
    m = re.search(r"Mirrored from kanban card `?(t_[0-9a-zA-Z]+)`?",
                  body or "")
    return m.group(1) if m else None


def closed_milestone_last(repo):
    """Epoch closed_at of the last closed milestone, or None.

    The moment "a milestone became active after a release": a milestone
    becomes active (the nearest due_on among open ones) when the previous one
    is closed by a tag. Its issues may have been created long before the 48 h
    window and their updatedAt may be old too: a change of the active
    milestone does not touch the issues themselves, so a poll window based
    on updatedAt did not see them. gh failure or garbage instead of JSON ->
    None (the old behaviour).
    """
    out = run(["gh", "api",
               "repos/%s/milestones?state=closed&per_page=5" % repo],
              timeout=60)
    try:
        items = json.loads(out or "[]")
    except Exception:
        return None
    best = None
    for m in items or []:
        t = _iso_epoch(m.get("closed_at"))
        if t and (best is None or t > best):
            best = t
    return best


def issue_milestoned_at(repo, n, title):
    """Epoch of the last milestoned event of the issue into milestone
    title, or None. Moving an issue into the active milestone is the moment
    it enters, not its creation date."""
    out = run(["gh", "api",
               "repos/%s/issues/%d/timeline?per_page=100" % (repo, n)],
              timeout=60)
    try:
        events = json.loads(out or "[]")
    except Exception:
        return None
    best = None
    for e in events or []:
        if e.get("event") != "milestoned":
            continue
        if ((e.get("milestone") or {}).get("title") or "") != title:
            continue
        t = _iso_epoch(e.get("created_at"))
        if t and (best is None or t > best):
            best = t
    return best


def active_milestone_orphans(slug, repo=None):
    """Open issues of the active milestone without a live card.

    The same filtering as the issue_executor_poll poll (a live card, an open
    PR, wait-for-triage), but without the window and without the delivery
    check: this is the admissible-band backlog for idle_factory_lint. A
    mirror covers an issue only while its source card is alive: a closed
    source (done) does not hold the work. Without this the watchdog stayed
    silent when there were no cards at all, every todo was held by open
    parents and parked was empty, while the active milestone had open
    issues.
    gh failure -> [] (the old behaviour: the lint is silent, the poll
    remains as the safety net).
    """
    repo = repo or board_repo(slug)
    if not repo:
        return []
    states = open_issue_states(repo)
    if states is None:
        return []
    ms = ms_bands(repo=repo)
    active = ms["active"]
    if not active or active not in (ms["bands"] or {}):
        return []
    db = os.path.join(BOARDS, slug, "kanban.db")
    if not os.path.exists(db):
        return []
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
    live_ids, live_refs = set(), set()
    try:
        for tid, title, body in con.execute(
                "SELECT id, title, coalesce(body,'') FROM tasks"
                " WHERE status NOT IN ('done','archived')"):
            live_ids.add(tid)
            live_refs |= {int(x) for x in re.findall(
                r"#(\d+)", (title or "") + " " + (body or ""))}
    finally:
        con.close()
    pr_of_issue = {}
    for pr_n, iss in ((open_pr_linkage(repo, states) or {})
                      .get("pr_issues") or {}).items():
        for i in iss:
            pr_of_issue.setdefault(i, pr_n)
    out = []
    for n in sorted(states):
        v = states[n]
        if not isinstance(v, tuple):
            continue
        milestone, labels, upd, body = (tuple(v) + (None,) * 4)[:4]
        if milestone != active or n in live_refs or n in pr_of_issue:
            continue
        if "wait-for-triage" in labels:
            continue
        # A mirror covers the issue only while its source is alive; a
        # closed source no longer holds the issue.
        live_src = mirror_source(body or "")
        if live_src and live_src in live_ids:
            continue
        out.append(n)
    return out


def merged_pr_refs(repo):
    """Merged PRs of the repo: number -> {"refs": set, "merged_at": epoch}.

    refs are the issue numbers mentioned in the title/body of the PR
    (Refs/Closes/#N). gh failure or garbage instead of JSON -> {}: the
    delivered-silence does not trigger and the old path (creating a card)
    stays, a safety net against false silence when gh is unavailable.
    """
    out = run(["gh", "pr", "list", "--repo", repo, "--state", "merged",
               "--limit", "100",
               "--json", "number,title,body,mergedAt"], timeout=90)
    try:
        parsed = json.loads(out)
    except Exception:
        return {}
    res = {}
    for p in parsed:
        num = p.get("number")
        if num is None:
            continue
        res[num] = {
            "refs": {int(x) for x in re.findall(
                r"#(\d+)", "%s %s" % (p.get("title") or "",
                                      p.get("body") or ""))},
            "merged_at": _iso_epoch(p.get("mergedAt"))}
    return res


def issue_delivered(slug, repo, n, merged=None):
    """Is the work on the issue already delivered?

    Generators once created three duplicate executor cards within minutes
    for an issue whose PR was already merged.

    Returns (delivered, detail, release_chore). delivered=True means no
    executor card may be created (a question to the owner, or silence):

      a) a merged PR referencing #N, merged after the last scope change of
         the issue (creation + labeled/unlabeled/renamed; milestoned and
         comments do not count as scope);
      b) a done executor card with #N whose text names a merged PR (done by
         itself is not coverage: delivery is a PR).

    No data (gh failure, empty createdAt/events): delivered=False, the old
    path works. release_chore: the title matches RELEASE_CHORE_RE; such an
    issue stays open on purpose after the merged prep PR until the tag and
    waits for the owner's decision.
    """
    if merged is None:
        merged = merged_pr_refs(repo)
    view = run(["gh", "issue", "view", str(n), "--repo", repo,
                "--json", "title,createdAt"], timeout=60)
    try:
        meta = json.loads(view) or {}
    except Exception:
        meta = {}
    title = (meta.get("title") or "").strip()
    chore = bool(title) and bool(RELEASE_CHORE_RE.search(title))
    scope = _iso_epoch(meta.get("createdAt"))
    events = run(["gh", "api", "repos/%s/issues/%d/events" % (repo, n)],
                 timeout=60)
    try:
        for ev in json.loads(events) or []:
            if ev.get("event") in SCOPE_EVENT_TYPES:
                t = _iso_epoch(ev.get("created_at"))
                if t is not None and (scope is None or t > scope):
                    scope = t
    except Exception:
        pass
    if scope is not None:
        for pn in sorted(merged):
            info = merged[pn]
            if n not in info["refs"] or info.get("merged_at") is None:
                continue
            if info["merged_at"] > scope:
                return True, ("PR #%d referencing #%d was merged after the"
                              " last scope change of the issue"
                              % (pn, n)), chore
        # b) a done executor card whose text has #N and a reference to a
        # merged PR (delivery is a PR, not a status)
        db = os.path.join(BOARDS, slug, "kanban.db")
        if os.path.exists(db):
            con = sqlite3.connect("file:%s?mode=ro" % db, uri=True,
                                  timeout=10)
            try:
                rows = list(con.execute(
                    "SELECT id, title, coalesce(body,'') FROM tasks"
                    " WHERE status='done' AND assignee='coder'"))
            finally:
                con.close()
            for tid, t_title, t_body in rows:
                text = "%s %s" % (t_title or "", t_body or "")
                if not re.search(r"#%d\b" % n, text):
                    continue
                for pr_s in re.findall(r"(?i)\bpr\s*#?(\d+)\b", text):
                    pn = int(pr_s)
                    info = merged.get(pn) or {}
                    m_at = info.get("merged_at")
                    if m_at is None:
                        out = run(["gh", "pr", "view", str(pn), "--repo",
                                   repo, "--json", "state,mergedAt"],
                                  timeout=60)
                        try:
                            pm = json.loads(out) or {}
                        except Exception:
                            pm = {}
                        m_at = (_iso_epoch(pm.get("mergedAt"))
                                if pm.get("state") == "MERGED" else None)
                    if m_at is not None and m_at > scope:
                        return True, ("done card %s delivered the work:"
                                      " PR #%d merged" % (tid, pn)), chore
    return False, "", chore


def open_pr_linkage(repo, states):
    """Open PRs of the repo: pr_issues (number -> issue numbers of the body
    that are live in states), pr_milestones (number -> the PR's direct
    milestone), owner_review_prs (PRs with a formal owner review).

    The binding collection of the former backlog gate lives here. A direct
    milestone on a PR is a binding (owner decision); a formal owner review
    (APPROVED/CHANGES_REQUESTED) is the owner's decision to work on the PR,
    and cards of such a PR are not parked. latestReviews gives the latest
    review of every reviewer: COMMENTED/DISMISSED from the owner after
    APPROVED carries no decision (conservative, head semantics).
    """
    out = run(["gh", "pr", "list", "--repo", repo, "--state", "open",
               "--limit", "100",
               "--json", "number,body,milestone,latestReviews"], timeout=90)
    try:
        parsed = json.loads(out)
    except Exception:
        return None
    live = set(states)
    owner = repo.split("/")[0]
    res = {"pr_issues": {}, "pr_milestones": {}, "owner_review_prs": []}
    for p in parsed:
        nums = {int(n) for n in re.findall(r"#(\d{1,5})\b",
                                           p.get("body") or "")}
        res["pr_issues"][p.get("number")] = sorted(nums & live)
        ms = (p.get("milestone") or {}).get("title")
        if ms:
            res["pr_milestones"][p.get("number")] = ms
        for rv in p.get("latestReviews") or []:
            if ((rv.get("author") or {}).get("login") == owner
                    and rv.get("state") in ("APPROVED", "CHANGES_REQUESTED")):
                res["owner_review_prs"].append(p.get("number"))
                break
    return res


def admission_gate(slug):
    """Admission gate (white list, with return from parking and bands).

    A card is admitted when it (or an ancestor by task_links) has a binding
    to an open issue with a milestone, or it is a service card of an open PR
    against an approved issue, or it carries the infrastructure marker.
    A binding = an "issue-link: #N" comment (survives specify) or a live
    reference #N in title/body. A non-admitted card in ready/todo/review is
    taken off the queue with the stock schedule and a marker comment
    (idempotent); running is not preempted, only a finding; blocked is left
    alone (it already waits for a human). While references are live in the
    body, the gate stamps an issue-link comment so specify cannot erase the
    binding.

    Details:
    - the card's own binding to an unapproved issue dominates the PR service
      exception (otherwise a PR could hide an issue without a milestone and
      promote raised the card to ready);
    - return from parking: when the card's issue gets a milestone, ONE
      working card per issue is returned (the oldest of the highest
      priority), duplicates are archived with a reference to it (one live
      card per PR/issue);
    - milestone priority bands (computed from GitHub on every run: active
      80, next 40, other open ones 20);
    - the admission cache (issues/prs) is written next to the board for the
      claim guard.

    The former backlog gate rule "outside a milestone" is merged here (it
    once parked cards from a stale cache). Taken over from it: the
    exception for ops; a direct milestone on a PR is a binding (owner
    decision); a PR with a formal owner review is not parked. The return
    understands both parking markers (admission and backlog); priority bands
    also apply to PR service cards: the band of their issue's milestone, and
    the top band "finish up to merge" keeps a priority above the band.
    """
    repo = board_repo(slug)
    if not repo:
        return []
    states = open_issue_states(repo)
    if states is None:
        return []                     # gh failure: retry in an hour
    # Open issues also carry updatedAt and body; old test stubs return a
    # 2-tuple (milestone, labels): pad to 4, so the open_issue_states
    # contract is extended without breaking existing stubs.
    states = {n: (tuple(v) + (None, None, None, None))[:4]
              if isinstance(v, tuple) else v for n, v in states.items()}
    pr_link = open_pr_linkage(repo, states)
    if pr_link is None:
        return []
    # Bands from GitHub (memoized per process). source != "gh" means the
    # data is stale or missing: the bands are applied, but a finding is
    # raised so a release day does not pass silently. The finding has a key
    # (a keyless 2-tuple crashed consumers that parse findings as
    # (key, sev, text)).
    ms = milestone_bands.resolve(repo, fetch=_ms_fetch)
    ms_bands_map = ms["bands"]
    # Named active_ms: inside pass 1 a local "active" group of cards would
    # overwrite any variable named active.
    active_ms = ms["active"]
    findings_pre = [] if ms["source"] == "gh" or not states else [(
        "%s:admission:ms-bands-stale" % slug, 2,
        "milestone bands are not from GitHub (source=%s, gh failure?): "
        "applied %s, freshness not guaranteed"
        % (ms["source"], "from the disk cache" if ms["bands"] else "none"))]
    # The backlog binding cache is reset when milestones change.
    backlog_cache_reset_on_milestone_change(states)
    db = os.path.join(BOARDS, slug, "kanban.db")
    if not os.path.exists(db):
        return []
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
    live = list(con.execute(
        "SELECT id, title, body, status, priority, created_at,"
        " coalesce(assignee,'') FROM tasks"
        " WHERE status NOT IN ('done','archived')"))
    done_refs = list(con.execute(
        "SELECT title, body FROM tasks WHERE status = 'done' "
        "AND completed_at > ?", (time.time() - 14 * 24 * 3600,)))
    titles = {}
    bodies = {}
    for tid, t_title, t_body in con.execute(
            "SELECT id, title, body FROM tasks"):
        titles[tid] = t_title or ""
        bodies[tid] = t_body or ""
    comments = {}
    for tid, cbody in con.execute("SELECT task_id, body FROM task_comments"):
        comments.setdefault(tid, []).append(cbody)
    # The latest scheduled reason per card. "Return from parking" (below)
    # must look at it: a card parked by the review layer (hold below p50) is
    # NOT "in the gate's parking", even if an old admission marker remains
    # in its history. Otherwise the gate returns the card every hour, the
    # review layer parks it back, and so on in a circle: new scheduled events
    # and comments without movement.
    last_sched = {}
    for tid, payload in con.execute(
            "SELECT task_id, payload FROM task_events"
            " WHERE kind = 'scheduled' ORDER BY id"):
        last_sched[tid] = payload or ""
    parents = {}
    children = {}
    for pid, cid in con.execute(
            "SELECT parent_id, child_id FROM task_links"):
        parents.setdefault(cid, []).append(pid)
        children.setdefault(pid, []).append(cid)
    con.close()
    live_ids = {row[0] for row in live}

    def marker_links(tid):
        """Issue numbers from the issue-link comments of the card."""
        out = set()
        for cbody in comments.get(tid, ()):
            m = re.search(ISSUE_LINK_MARKER + r"[^\n]*", cbody, re.I)
            if m:
                out.update(int(n)
                           for n in re.findall(r"#(\d+)", m.group(0)))
        return out

    def ancestors(tid):
        # archived parents included: admission by ancestor survives the
        # archiving of a duplicate (otherwise its children lost admission
        # between runs)
        seen, stack = set(), list(parents.get(tid, []))
        while stack:
            p = stack.pop()
            if p in seen:
                continue
            seen.add(p)
            stack.extend(parents.get(p, []))
        return seen

    def descendants(tid):
        seen, stack = set(), list(children.get(tid, []))
        while stack:
            c = stack.pop()
            if c in seen or c not in live_ids:
                continue
            seen.add(c)
            stack.extend(children.get(c, []))
        return seen

    def band_of(nums):
        """Priority band by the milestones of approved issues (max)."""
        best = None
        for n in nums:
            band = ms_bands_map.get(states[n][0])
            if band is not None and (best is None or band > best):
                best = band
        return best

    # --- pass 1: admission decision for every live task -------------------
    info = {}
    refs_live = set()
    # Only LIVE cards (queued or in progress), without the done slice: for
    # "active milestone without an executor" a card closed recently does not
    # count as coverage
    live_refs = set()
    for t_title, t_body in done_refs:
        refs_live.update(refs_in({"title": t_title or "",
                                  "body": t_body or ""}))
    for task_id, title, body, status, prio, created, assignee in live:
        own = set(refs_in({"title": title or "", "body": body or ""}))
        own_marker = marker_links(task_id)
        own_all = own | own_marker
        # refs_live also counts markers (specify erases bodies): an issue with
        # a live parked card does not count as "without a card"
        refs_live.update(n for n in own_all if n in states)
        live_refs.update(n for n in own_all if n in states)
        nums = set(own_all)
        for anc in ancestors(task_id):
            nums |= marker_links(anc)
            nums |= set(refs_in({"title": titles[anc],
                                 "body": bodies[anc]}))
        issues = {n for n in nums if n in states}
        prs = {n for n in nums if n in pr_link["pr_issues"]}
        infra = any(INFRA_MARKER in cbody
                    for cbody in comments.get(task_id, ()))
        own_issues = own_all & set(states)
        own_approved = sorted(n for n in own_issues if states[n][0])
        approved = sorted(n for n in issues if states[n][0])
        # The card's anchor for the band = its own issue: #N in the title,
        # in an "Issue:" line of the body or in the issue-link marker (the
        # gate's stamp, not prose), plus the same anchors of ancestors (a
        # chain inherits the root's issue). Side #N mentions in prose give
        # no band: a PR of the active milestone mentioned in the prose of a
        # next-milestone chain once raised that chain to the active band.
        # One anchor definition for all gates: closed_tail_anchor_refs; the
        # marker is equivalent to the Issue: line (it states the binding
        # explicitly).
        anchor = set(closed_tail_anchor_refs(title or "", body or ""))
        anchor |= own_marker
        for anc in ancestors(task_id):
            anchor |= closed_tail_anchor_refs(titles[anc], bodies[anc])
            anchor |= marker_links(anc)
        # PR service exception (extended from the former backlog gate):
        # Refs/Closes of an approved issue, OR a direct milestone of the PR
        # (owner decision), OR a formal owner review
        # (APPROVED/CHANGES_REQUESTED).
        svc = sorted(n for n in prs
                     if any(states[i][0] for i in pr_link["pr_issues"][n])
                     or pr_link["pr_milestones"].get(n)
                     or n in pr_link["owner_review_prs"])
        if infra:
            admitted, why, band = True, None, None
        elif (assignee or "") == "ops":
            # from the former backlog gate: ops cards are not parked
            admitted, why, band = True, None, None
        elif own_issues and not own_approved:
            # the card's own binding to an unapproved issue dominates the
            # service exception of an open PR
            admitted = False
            band = None
            why = ", ".join("#%d without a milestone" % n
                            for n in sorted(own_issues))
        elif approved or svc:
            admitted, why = True, None
            # band only from anchor issues (title or "Issue:" line), not
            # from any #N in the prose of the body
            band = band_of(n for n in approved if n in anchor)
            # a PR service card gets the band of its issue's milestone (and
            # of the PR's direct milestone), not lower
            for n in svc:
                if n not in anchor:
                    # a side PR from prose gives no band
                    continue
                for i in pr_link["pr_issues"][n]:
                    if states[i][0]:
                        b = ms_bands_map.get(states[i][0])
                        if b and (band is None or b > band):
                            band = b
                b = ms_bands_map.get(
                    pr_link["pr_milestones"].get(n))
                if b and (band is None or b > band):
                    band = b
        else:
            admitted, band = False, None
            if issues:
                why = ", ".join("#%d without a milestone" % n
                                for n in sorted(issues))
            elif prs:
                why = ", ".join("PR #%d against an unapproved issue" % n
                                for n in sorted(prs))
            else:
                why = NO_LINK_WHY
        info[task_id] = {
            "status": status, "prio": prio, "created": created or 0,
            "own_issues": own_issues, "own_approved": set(own_approved),
            "admitted": admitted, "why": why, "band": band,
            "svc": bool(svc), "assignee": assignee or "",
            "self_admitted": bool(own_approved or svc or infra),
        }

    # the subtree of an unapproved root is not raised (promote once raised
    # such a child to ready between two hourly gate runs)
    for task_id in list(info):
        root = info[task_id]
        if root["admitted"] or root["status"] not in ADMISSION_WORK_STATUSES:
            continue
        for desc in descendants(task_id):
            d = info.get(desc)
            if (not d or d["admitted"] or d["self_admitted"]
                    or d["status"] not in ADMISSION_WORK_STATUSES
                    or d["why"] != NO_LINK_WHY):
                continue
            d["why"] = ("subtree of the unapproved root %s: %s"
                        % (task_id, root["why"]))

    # --- pass 2: actions ---------------------------------------------------
    findings = []
    findings.extend(findings_pre)
    for task_id, title, body, status, prio, created, assignee in live:
        d = info[task_id]
        if any(INFRA_MARKER in cbody for cbody in comments.get(task_id, ())):
            continue
        # a stable binding: stamp issue-link while references are live in the
        # body (blocked is not stamped: the card is already with a human)
        stamp = set(refs_in({"title": title or "", "body": body or ""})) \
            & set(states)
        if stamp and status != "blocked" \
                and not (marker_links(task_id) & stamp):
            run([HERMES, "kanban", "--board", slug, "comment", task_id,
                 ISSUE_LINK_MARKER + " %s (automatic from the body, kept by "
                 "the admission gate, because specify and the decomposer rewrite the body)"
                 % " ".join("#%d" % n for n in sorted(stamp))], timeout=90)
        if d["admitted"]:
            # a card without an assignee is never started by the dispatcher
            # (skipped_unassigned; owner review cards once waited 30 hours):
            # report it on every run while the assignee is empty
            if (not d["assignee"]
                    and status in ("ready", "todo", "review")):
                findings.append((
                    "%s:admission:unassigned:%s" % (slug, task_id), 1, (
                        "    %s (%s, p%s) has no assignee: the dispatcher does"
                        " not start it. Assign: kanban edit %s"
                        " --assignee coder/architect as appropriate"
                        % (task_id, status, prio, task_id))))
            # Milestone priority band (active 80, next 40). It also applies
            # to service cards (review, WCFY, rebases): the band of their
            # issue's milestone; the top band "finish up to merge" keeps a
            # priority above the band (active = 80+). A merge-unblocker is
            # left alone: its p100 is the last raise before the merge, above
            # the bands by design.
            # It applies to running too: a card of a next-milestone issue
            # created by a webhook with p80 can be claimed within seconds,
            # before the hourly gate run, and would live with the wrong
            # priority to its end. Editing a running card's priority does
            # not preempt the run (the dispatcher reads priority on the next
            # claim).
            if (d["band"] and status in ("ready", "todo", "review",
                                         "scheduled", "running")
                    and prio != d["band"]
                    and not any(MERGE_UNBLOCKER_MARKER in cbody
                                for cbody in comments.get(task_id, ()))):
                if d["svc"] and d["band"] == milestone_bands.ACTIVE_BAND:
                    target = max(prio, d["band"])
                else:
                    target = d["band"]
                if target != prio:
                    run([HERMES, "kanban", "--board", slug, "edit", task_id,
                         "--priority", str(target)], timeout=90)
                    findings.append((
                        "%s:admission:band:%s" % (slug, task_id), 2, (
                            "    %s milestone priority band:"
                            " p%s -> p%s%s"
                            % (task_id, prio, target,
                               " (service card, top band)" if
                               target != d["band"] else ""))))
            continue
        why = d["why"]
        if status == "running":
            findings.append(("%s:admission:%s" % (slug, task_id), 2, (
                "    %s IN PROGRESS, but not admitted by the white list (%s): "
                "the run is not preempted, raise the question after it is "
                "handed in"
                % (task_id, why))))
            continue
        if status not in ADMISSION_WORK_STATUSES:
            continue
        markered = any(ADMISSION_MARKER in cbody
                       for cbody in comments.get(task_id, ()))
        if not markered:
            run([HERMES, "kanban", "--board", slug, "comment", task_id,
                 ADMISSION_MARKER + " not admitted (%s). Taken off the queue "
                 "by the admission white list. To lift: assign the issue a "
                 "milestone, bind the card to an approved issue with an "
                 "'issue-link: #N' comment, or lift the parking by the "
                 "owner's decision." % why], timeout=90)
        if status != "scheduled":
            run([HERMES, "kanban", "--board", slug, "schedule", task_id,
                 ADMISSION_MARKER + " admission white list: %s" % why],
                timeout=90)
        findings.append(("%s:admission:%s" % (slug, task_id), 2, (
            "    %s (%s, p%s) taken off the queue by the admission white list: %s"
            % (task_id, status, prio, why))))

    # --- return from parking when an issue is approved ---------------------
    # One live card per PR/issue: the working card is returned, duplicates
    # are archived with a reference to it.
    # Links of one task_links chain are NOT duplicates. The gate once
    # archived the coder card that wrote a fix and kept the ops card that
    # closes the issue as the working one: review and the stability check
    # waited for a result that would never come. A duplicate without links
    # to the group is archived; the working card is the one that produces
    # the artifact.
    # Both parking markers are understood (the admission gate and the
    # former backlog gate): otherwise backlog parkings stay in scheduled
    # without a return.
    def parked_marker(tid):
        return any(ADMISSION_MARKER in cbody
                   or BACKLOG_PARK_MARKER in cbody
                   for cbody in comments.get(tid, ()))

    parked = [tid for tid, d in info.items()
              if d["status"] == "scheduled" and d["admitted"]
              and parked_marker(tid)
              and REVIEW_PARK_MARKER not in last_sched.get(tid, "")]

    def depth(tid, seen=frozenset()):
        ps = [p for p in parents.get(tid, []) if p in live_ids
              and p not in seen]
        return 1 + max((depth(p, seen | {tid}) for p in ps), default=0)

    def live_kids(tid, exclude):
        return [c for c in children.get(tid, [])
                if c in info and c not in exclude]

    def archive_dup(tid, issue, keeper, exclude, archived_now):
        # children stay alive: separate cards with their own status.
        # Archiving a duplicate used to break chains: live children stayed
        # with a parent in the archive and got the watchdog's I6 block
        # "parent archived, assign a successor". Now the children are
        # relinked to the working card (link keeper kid). A child that is
        # itself the working card is unlinked from the duplicate: a link
        # would point the card at itself, and its prerequisite is already in
        # it.
        kids = live_kids(tid, exclude)
        run([HERMES, "kanban", "--board", slug, "comment", tid,
             ADMISSION_MARKER + " duplicate of issue #%d, working card %s "
             "(one live card per PR/issue)%s"
             % (issue, keeper,
                "; children stay alive: " + ",".join(kids) if kids else "")],
            timeout=90)
        run([HERMES, "kanban", "--board", slug, "archive", tid], timeout=90)
        for kid in kids:
            if kid == keeper:
                run([HERMES, "kanban", "--board", slug, "unlink", tid, kid],
                    timeout=90)
            else:
                run([HERMES, "kanban", "--board", slug, "link", keeper, kid],
                    timeout=90)
        archived_now.add(tid)
        findings.append(("%s:admission:dup:%s" % (slug, tid), 2, (
            "    %s archived as a duplicate of issue #%d, working %s%s"
            % (tid, issue, keeper,
               "; children relinked: " + ",".join(kids) if kids else ""))))

    def chain_peers(tid, peers):
        """Members of the group/active linked to tid by a task_links edge.

        A card linked as parent/child to another card of the same issue is a
        link of a decomposition chain (coder -> review -> stability), not a
        duplicate. Without this a fix card was archived while its review
        children were alive."""
        ids = set(peers)
        return ids & (set(parents.get(tid, [])) | set(children.get(tid, [])))

    KEEPER_ROLE_ORDER = ("coder", "architect", "tester", "reviewer", "ops")

    def pick_keeper(cands):
        """The working card = the one that produces the artifact.

        Picking sorted(active)[0] (the lexicographically smallest id) used to
        make an ops card that closes the issue, or a visual-delta card, the
        working one. Role order: code > architecture > testing > review >
        ops (ops posts a verdict, it produces no artifact). visual-delta
        (capturing a delta for the issue, not work on it) is never the
        working card."""
        def key(t):
            role = (info[t].get("assignee") or "").lower()
            ro = (KEEPER_ROLE_ORDER.index(role)
                  if role in KEEPER_ROLE_ORDER else len(KEEPER_ROLE_ORDER))
            return (1 if "visual-delta" in (titles.get(t) or "").lower()
                    else 0, ro, t)
        return min(cands, key=key)

    def return_link(tid, issue):
        """Return a chain link from parking: not a duplicate, separate work."""
        run([HERMES, "kanban", "--board", slug, "unblock", tid,
             "--reason", ADMISSION_MARKER + " return from parking: issue"
             " #%d approved, the card is linked by task_links to other"
             " cards of this issue (a chain link, not a duplicate)"
             % issue], timeout=90)
        band = info[tid]["band"]
        if band and info[tid]["prio"] != band:
            run([HERMES, "kanban", "--board", slug, "edit", tid,
                 "--priority", str(band)], timeout=90)
        findings.append(("%s:admission:chain-return:%s" % (slug, tid), 1, (
            "    %s returned from parking as a chain link of issue #%d"
            " (not a duplicate), band %s" % (tid, issue, band or "-"))))

    groups = {}
    for tid in parked:
        for n in info[tid]["own_approved"]:
            groups.setdefault(n, []).append(tid)
    archived_now = set()
    for n, members in sorted(groups.items()):
        active = [tid for tid, d in info.items()
                  if d["status"] in ("ready", "todo", "review", "running")
                  and n in d["own_issues"]]
        # a chain link (task_links to another group member or to a live
        # card of the same issue) is not archived as a duplicate
        chain = {tid for tid in members
                 if chain_peers(tid, members) or chain_peers(tid, active)}
        if active:
            keeper = pick_keeper(active)
            for tid in sorted(members, key=lambda t: -depth(t)):
                if tid in chain:
                    return_link(tid, n)
                else:
                    archive_dup(tid, n, keeper,
                                archived_now, archived_now)
            continue
        # working card: highest priority, then the oldest, then the role
        # that produces the artifact
        maxp = max(info[t]["prio"] for t in members)
        oldest = min(info[t]["created"] for t in members
                     if info[t]["prio"] == maxp)
        keeper = pick_keeper([t for t in members
                              if info[t]["prio"] == maxp
                              and info[t]["created"] == oldest])
        run([HERMES, "kanban", "--board", slug, "unblock", keeper,
             "--reason", ADMISSION_MARKER + " return from parking: "
             "issue #%d approved by the owner (milestone)" % n], timeout=90)
        band = info[keeper]["band"]
        if band and info[keeper]["prio"] != band:
            run([HERMES, "kanban", "--board", slug, "edit", keeper,
                 "--priority", str(band)], timeout=90)
        findings.append(("%s:admission:return:%d" % (slug, n), 1, (
            "    issue #%d approved: %s returned from parking (p%s), "
            "band %s" % (n, keeper, band or info[keeper]["prio"],
                         "active=80" if band == 80 else
                         ("next=40" if band == 40 else "-")))))
        ordered = sorted(members, key=lambda t: -depth(t))
        # keeper is not excluded from the duplicate's children: a
        # duplicate->keeper link must be unlinked
        for tid in ordered:
            if tid == keeper:
                continue
            if tid in chain:
                return_link(tid, n)
            else:
                archive_dup(tid, n, keeper, archived_now, archived_now)

    # a parked card admitted as a service card (the PR got a direct
    # milestone or a formal owner review, no issue references): the former
    # backlog raise by owner review, now one mechanism
    grouped = {tid for members in groups.values() for tid in members}
    for tid in parked:
        if tid in grouped:
            continue
        run([HERMES, "kanban", "--board", slug, "unblock", tid,
             "--reason", ADMISSION_MARKER + " return from parking: the PR"
             " got a direct milestone or a formal owner review"
             " (service admission)"],
            timeout=90)
        band = info[tid]["band"]
        if band and info[tid]["prio"] != band \
                and not any(MERGE_UNBLOCKER_MARKER in cbody
                            for cbody in comments.get(tid, ())):
            run([HERMES, "kanban", "--board", slug, "edit", tid,
                 "--priority", str(band)], timeout=90)
        findings.append(("%s:admission:return-svc:%s" % (slug, tid), 1, (
            "    %s returned from parking: PR service admission (milestone/"
            "owner review), band %s"
            % (tid, band or "-"))))

    # --- live duplicates of one issue and producing role ------------------
    # Three coder cards for one patch once appeared: a reviewer created one
    # with a wrong link direction, unlink is forbidden from a worker, so it
    # created a second one, and a manual restore added a third, which the
    # dispatcher started. The gates did not see it: the dedup above scans
    # only parked cards; gen_admission is admission by issue/milestone
    # BEFORE create, not a duplicate check; chain_peers tells links apart by
    # coder->review edges, which both code siblings of one issue have.
    # There is no creation idempotency by the key "issue+role": the keys of
    # the two cards differ and both pass (the idempotency gate catches only
    # a repeated key or a missing one). A live duplicate = the same open
    # issue, the same producing role, an active status and NO direct
    # task_links edge between the copies. Two running at once are not
    # broken automatically: a human decides.
    by_issue_role = {}
    for tid, d in info.items():
        if d["status"] not in ("ready", "todo", "review", "running"):
            continue
        role = (d.get("assignee") or "").lower()
        if role not in KEEPER_ROLE_ORDER[:3]:
            continue
        for n in d["own_issues"]:
            by_issue_role.setdefault((n, role), []).append(tid)
    for (n, role), cands in sorted(by_issue_role.items()):
        cands = [t for t in cands if t not in archived_now]
        if len(cands) < 2:
            continue
        # a direct edge between copies = links of one chain, not duplicates
        if any(q != t and (q in parents.get(t, [])
                           or q in children.get(t, []))
               for t in cands for q in cands):
            continue
        # a shared live child (a shared review/stability link) = the copies
        # feed one chain. Different unlinked cards of one issue and role can
        # be legitimately different work (for example a keepalive card and
        # a working card): they are left alone.
        if not any(set(children.get(t, [])) & set(children.get(q, []))
                   for t in cands for q in cands if t != q):
            continue
        running = [t for t in cands if info[t]["status"] == "running"]
        if len(running) > 1:
            findings.append((
                "%s:admission:live-dup:%d:%s" % (slug, n, role), 1, (
                    "    issue #%d: more than one running %s (%s): a live"
                    " executor duplicate, a human decides whether to break"
                    " active runs" % (n, role, ",".join(sorted(running))))))
            continue
        keeper = (running[0] if running
                  else pick_keeper(sorted(
                      cands, key=lambda t: (-info[t]["prio"],
                                            info[t]["created"]))))
        for tid in cands:
            if tid != keeper and tid not in archived_now:
                archive_dup(tid, n, keeper, archived_now, archived_now)
        findings.append((
            "%s:admission:live-dup:%d:%s" % (slug, n, role), 2, (
                "    issue #%d: live duplicate of role %s archived, working %s"
                % (n, role, keeper))))

    # --- admission cache for the claim guard ------------------------------
    # pr_issues/pr_milestones are included so service card generators
    # create cards right away in the band of their issue's milestone
    # (svc_priority), without relying on the gate's correction.
    try:
        cache = {
            "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "issues": {str(n): states[n][0] for n in states
                       if states[n][0]},
            "prs": sorted(n for n in pr_link["pr_issues"]
                          if any(states[i][0] for i in pr_link["pr_issues"][n])
                          or pr_link["pr_milestones"].get(n)
                          or n in pr_link["owner_review_prs"]),
            "pr_issues": {str(n): pr_link["pr_issues"][n]
                          for n in pr_link["pr_issues"]},
            "pr_milestones": {str(n): ms for n, ms
                              in pr_link["pr_milestones"].items()},
        }
        with open(os.path.join(BOARDS, slug, ADMISSION_CACHE_NAME),
                  "w") as fh:
            json.dump(cache, fh, ensure_ascii=False, indent=1)
    except Exception:
        pass

    # Delivered work (a merged PR after the last scope change, or a done
    # card with a merged PR) needs no executor. The check is expensive
    # (gh view + events): cached per run, computed only for issues of the
    # active milestone.
    merged = None
    delivered_cache = {}

    def _delivered(n):
        nonlocal merged
        if merged is None:
            merged = merged_pr_refs(repo)
        if n not in delivered_cache:
            delivered_cache[n] = issue_delivered(slug, repo, n,
                                                 merged=merged)
        return delivered_cache[n]

    approved = []
    for n, (milestone, labels, *_rest) in sorted(states.items()):
        if "wait-for-triage" in labels or not milestone or n in refs_live:
            continue
        if active_ms is not None and milestone == active_ms \
                and _delivered(n)[0]:
            continue    # work delivered: question or silence below
        approved.append(n)
    if approved:
        findings.append(("%s:admission:unclaimed:%s" % (
            slug, ",".join(str(n) for n in approved)), 1, (
            "    Approved issues without a card: %s. Create a card"
            " with an assignee (--assignee coder/architect as"
            " appropriate): without one the dispatcher does not start it"
            " (the owner assigned a milestone)."
            % ", ".join("#%d" % n for n in approved))))

    # --- active milestone without a LIVE card for longer than 24 h ---------
    # The unclaimed check above counts done cards of the last 14 days as
    # coverage, and wait-for-triage issues are excluded entirely, so an
    # active-milestone issue could stay silent. Here coverage = live cards
    # only; age = updatedAt of the issue itself (no time data -> skip, not a
    # false signal). An issue with an open PR (Refs/Closes in the PR body)
    # is not "without an executor": the work is in flight.
    # A release blocker (label bug or an owner mark OWNER_MARK_RE) does not
    # wait a day: the threshold is one watchdog cycle
    # (NO_EXECUTOR_BLOCKER_MIN_AGE_S), key blk-no-executor, class owner
    # (a push to the owner, REPEAT_AFTER holds the repeat for a day).
    if active_ms is not None and ms["bands"].get(active_ms):
        pr_of_issue = {}
        for pr_n, iss in (pr_link.get("pr_issues") or {}).items():
            for i in iss:
                pr_of_issue.setdefault(i, pr_n)
        merged = None
        for n in sorted(states):
            milestone, labels, upd, body = states[n]
            if milestone != active_ms or n in live_refs:
                continue
            if n in pr_of_issue:
                continue
            triage = "wait-for-triage" in labels
            blocker = (not triage and ("bug" in labels
                       or bool(OWNER_MARK_RE.search(body or ""))))
            min_age = (NO_EXECUTOR_BLOCKER_MIN_AGE_S if blocker
                       else NO_EXECUTOR_MIN_AGE_S)
            if not upd or time.time() - upd < min_age:
                continue
            age_h = int((time.time() - upd) // 3600)
            # Work delivered -> no executor card. A release chore after the
            # merged prep PR stays open on purpose until the tag: silence
            # until the owner decides. Other delivered work -> a question to
            # the owner "what remains".
            delivered, d_detail, chore = _delivered(n)
            if delivered:
                if chore:
                    continue
                findings.append((
                    "%s:admission:delivered-open:%d" % (slug, n), 1, (
                        "    issue #%d is open, but its work is delivered"
                        " (%s). Question to the owner: what remains for #%d?"
                        " Do not create an executor card until the owner"
                        " names the remainder."
                        % (n, d_detail, n))))
                continue
            if "wait-for-triage" in labels:
                findings.append((
                    "%s:admission:triage-stuck:%d" % (slug, n), 1, (
                        "    issue #%d (active %s) has been in the owner's"
                        " triage for %d h without a live card: the owner"
                        " assigned a milestone, but wait-for-triage remains."
                        " Remove the label with a decision (do it / not"
                        " planned) or close the issue."
                        % (n, active_ms, age_h))))
            elif blocker:
                findings.append((
                    "%s:admission:blk-no-executor:%d" % (slug, n), 1, (
                        "    BLOCKER: issue #%d of the active milestone %s"
                        " (label bug or an owner mark) without a live"
                        " card for %d h, the 5-minute poll did not cover it."
                        " Create an executor card with band %d"
                        " (helper: scripts/milestone_bands.py --repo"
                        " <repo> --issue %d); if the poll is stuck, check"
                        " the issue-executor-poll cron."
                        % (n, active_ms, age_h,
                           ms["bands"][active_ms], n))))
            else:
                findings.append((
                    "%s:admission:no-executor:%d" % (slug, n), 1, (
                        "    issue #%d of the active milestone %s without a"
                        " live card for %d h (last issue activity)."
                        " Create an executor card with band %d"
                        " (helper: scripts/milestone_bands.py --repo"
                        " <repo> --issue %d)."
                        % (n, active_ms, age_h,
                           ms["bands"][active_ms], n))))
    return findings


def issue_executor_poll(dry_run=False):
    """An executor card for an issue of the active milestone within one poll
    cycle (a 5-minute cron), not a day later through the no-executor
    finding and not by the owner's hand.

    Why: an urgent bug issue of the active milestone stood an hour without
    an executor. The issues event does not arrive (the webhook subscription
    listens only to issue_comment/pull_request_review), and the admission
    no-executor finding has a 24 h threshold, so the owner had to comment
    "start now".

    Contract (native-or-live rule): before creating, native_or_live by the
    key issue-<N>; a live card mentioning #N, an open PR, wait-for-triage,
    a bot mirror ("Mirrored from kanban card" or a bot author) and an age
    above ISSUE_POLL_WINDOW_S cover or filter the issue. Priority = the
    band of the active milestone (milestone_bands), the assignee is
    mandatory, idempotency-key owner-issue-<N> is shared with the webhook
    path. A dry run (dry_run) prints the plan and creates nothing.

    Returns [(slug, issue, card_id_or_None)] of what was created (or
    planned in dry_run).
    """
    if board_native_guard is None:
        return []                     # guard unavailable: creation is forbidden
    plan = []
    now = time.time()
    for slug in board_slugs():
        repo = board_repo(slug)
        if not repo:
            continue
        states = open_issue_states(repo)
        if states is None:
            continue
        states = {n: (tuple(v) + (None, None, None, None))[:4]
                  if isinstance(v, tuple) else v
                  for n, v in states.items()}
        ms = milestone_bands.resolve(repo, fetch=_ms_fetch)
        active = ms["active"]
        band = (ms["bands"] or {}).get(active) if active else None
        if not band:
            continue
        db = os.path.join(BOARDS, slug, "kanban.db")
        if not os.path.exists(db):
            continue
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
        live_refs = set()
        live_ids = set()
        try:
            for tid, title, body in con.execute(
                    "SELECT id, title, coalesce(body,'') FROM tasks"
                    " WHERE status NOT IN ('done','archived')"):
                live_ids.add(tid)
                live_refs |= {int(x) for x in re.findall(
                    r"#(\d+)", (title or "") + " " + (body or ""))}
        finally:
            con.close()
        pr_link = open_pr_linkage(repo, states) or {}
        pr_of_issue = {}
        for pr_n, iss in (pr_link.get("pr_issues") or {}).items():
            for i in iss:
                pr_of_issue.setdefault(i, pr_n)
        merged = None
        active_since = False       # False = not asked yet; None = no data
        for n in sorted(states):
            milestone, labels, upd, body = states[n]
            if milestone != active or n in live_refs:
                continue
            if n in pr_of_issue or "wait-for-triage" in labels:
                continue
            # A mirror suppresses the issue only while its source card is
            # alive: a mirror footer of a long-closed card used to silence
            # the poll on a critical issue of the active milestone.
            src = mirror_source(body or "")
            if src and src in live_ids:
                continue
            # The window is also counted from the moment the issue ENTERED
            # the active milestone, not only from updatedAt. After a release
            # the next milestone becomes active while its issues may be
            # months old: switching the active milestone does not touch the
            # issues themselves, so a window on updatedAt was blind.
            # Entering = a milestoned move of the issue OR the activation of
            # the milestone itself (closed_at of the previous one, "the
            # milestone became active after the release").
            fresh = upd and now - upd <= ISSUE_POLL_WINDOW_S
            if not fresh:
                if active_since is False:
                    active_since = closed_milestone_last(repo)
                if active_since and now - active_since <= ISSUE_POLL_WINDOW_S:
                    entered = active_since   # activation is fresher than any move
                else:
                    entered = issue_milestoned_at(repo, n, active) or 0
                    if active_since:
                        entered = max(entered, active_since)
                if not entered or now - entered > ISSUE_POLL_WINDOW_S:
                    continue
            # Delivered work gets no card: a release-prep PR was merged
            # while its issue stayed open until the tag, and the poll created
            # duplicates twice within an hour. A release chore and anything
            # covered by a merged PR stays silent; the owner names what is
            # left.
            if merged is None:
                merged = merged_pr_refs(repo)
            if issue_delivered(slug, repo, n, merged=merged)[0]:
                continue
            covered, note = board_native_guard.native_or_live(
                "issue-%d" % n,
                live_keys=["issue-%d" % r for r in sorted(live_refs)],
                native_checked=(board_native_guard.NATIVE_TASK_LINKS,),
                why="the owner opened/moved issue #%d into the active"
                    " milestone %s, there is no executor"
                    % (n, active))
            if covered:
                continue
            view = run(["gh", "issue", "view", str(n), "--repo", repo,
                        "--json", "title,body,author"], timeout=60)
            try:
                meta = json.loads(view) or {}
            except Exception:
                meta = {}
            author = ((meta.get("author") or {}).get("login") or "")
            if author in tuple(cfg_list(
                    "FACTORY_BOT_LOGINS",
                    cfg_list("FACTORY_REVIEWER_LOGINS", ()))) \
                    or author.endswith("[bot]"):
                continue
            title = (meta.get("title") or ("issue #%d" % n)).strip()
            card_body = (
                "Issue: #%d\n\n%s\n\nSource: an issue of the active milestone"
                " %s without an executor card (5-minute poll)."
                " Author: %s.\n\n"
                "Handing over to review: kanban_request_review only with an"
                " explicit reviewer=reviewer; BEFORE the call check for a live"
                " reviewer card on your exact head (key"
                " reviewer-pr<N>-<head7> in `hermes kanban ls`): if such a"
                " card is already alive, do not request review, finish the run"
                " with a comment linking to it. There are never two review"
                " sessions of one head: the later one is retired by the"
                " watchdog."
                % (n, (meta.get("body") or body or "").strip()[:2000],
                   active, author or "unknown"))
            if dry_run:
                plan.append((slug, n, None))
                print("[dry-run %s] issue #%d -> executor card"
                      " (assignee coder, p%d, key owner-issue-%d)"
                      % (slug, n, band, n))
                continue
            card = _kanban_create(
                slug, "%s (issue #%d)" % (title, n), card_body,
                "coder", band, "owner-issue-%d" % n, guard=note)
            if card:
                plan.append((slug, n, card))
    return plan


def review_layer_check(slug):
    """Review layer: only the rework arm for a change-request verdict.

    Scheme "one PR = one card" (owner decision): the auto-arm of reviewer
    cards per head, the paired arm and the WCFY arm per head were removed.
    Review lives in the stock review lane of the executor card
    (kanban_request_review with reviewer=reviewer), and the executor writes
    the WCFY with the same card.

    Returns findings; actions are performed right away (like
    idempotency_gate). gh errors on individual PRs are skipped silently:
    the hourly run retries.
    """
    repo = board_repo(slug)
    if not repo:
        return []
    listing = run(["gh", "pr", "list", "--repo", repo, "--state", "open",
                   "--limit", "100", "--search", "sort:updated-desc",
                   "--json", "number,isDraft,headRefName"], timeout=90)
    try:
        parsed = json.loads(listing)
        numbers = [p["number"] for p in parsed if not p.get("isDraft")]
    except Exception:
        return []
    # A draft PR is not armed. The numbers filter above prevents a NEW card
    # for a draft; here we retire cards that already exist, because the
    # owner can turn a PR into a draft after it was armed.
    draft_numbers = {p["number"] for p in parsed if p.get("isDraft")}
    # Head branches for the PR owner lookup (live_cards_by_pr: carriers of
    # rank 0 and 3 are tied to the branch).
    head_of = {p["number"]: p.get("headRefName") or ""
               for p in parsed if not p.get("isDraft")}
    db = os.path.join(BOARDS, slug, "kanban.db")
    if not os.path.exists(db):
        return []
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
    live = list(con.execute(
        "SELECT id, title, body, assignee, status, priority, "
        "idempotency_key FROM tasks WHERE status NOT IN ('done','archived')"))
    findings = []   # initialized before the draft retirement below
    for tid, t_title, t_body, t_assignee, t_status, t_prio, key in live:
        m = re.fullmatch(
            r"reviewer-pr(\d+)-[0-9a-f]+(?:-r\d+|-p\d+)?", key or "")
        if not m or int(m.group(1)) not in draft_numbers:
            continue
        run([HERMES, "kanban", "--board", slug, "comment", tid,
             ADMISSION_MARKER + " PR #%s was turned into a draft: auto-arming "
             "of reviews does not apply to draft PRs, the card is archived "
             "until the PR leaves draft." % m.group(1)], timeout=90)
        run([HERMES, "kanban", "--board", slug, "archive", tid], timeout=90)
        findings.append(("%s:revlayer:draft:%s" % (slug, tid), 1, (
            "    %s (review of PR #%s) archived: PR is a draft"
            % (tid, m.group(1)))))
    # A done card carrying the key of this head is not in `live`, yet a create
    # with the same key is DEDUPLICATED against it (dedup matches any
    # non-archived card, done included): no new card appears, the PR is again
    # without a verdict and the watchdog stays silent. So the keyed family is
    # collected from done cards as well.
    done_keyed = list(con.execute(
        "SELECT id, idempotency_key FROM tasks WHERE status = 'done' "
        "AND (idempotency_key LIKE 'reviewer-pr%' "
        "     OR idempotency_key LIKE 'coder-pr%-wcfy-%' "
        # the rework family for a change request (the CR rework generator)
        "     OR idempotency_key LIKE 'coder-pr%-rework-%')"))
    # The live-duplicate dedup of the admission gate extended to reviewers.
    # That dedup catches only producing roles by the issue+role key; reviewing
    # one head twice is the same live duplicate, and the reviewer is the most
    # expensive role (max effort). An open PR has one head, so the group is
    # PR+head: the family key reviewer-pr<N>-<head7> (including the -rN/-pN
    # suffixes) or PR#+head from the title/body of a manual card; a card
    # without a head (a planned "review the final head once the rework is
    # done") joins any group of its PR. Exceptions: two paired arms -pN with
    # DIFFERENT suffixes (the twin era) are legitimate; a duplicate with live
    # children is not archived (that would break a chain), only a finding
    # for a human.
    def _rev_head_of(t):
        m = re.fullmatch(
            r"reviewer-pr(\d+)-([0-9a-f]{7})((-r|-p)\d+)?", t[6] or "")
        if m:
            return int(m.group(1)), m.group(2), m.group(3) or ""
        mp = re.search(r"PR #(\d+)", "%s %s" % (t[1] or "", t[2] or ""))
        if not mp:
            return None
        mh = (re.search(r"head[ :]*`?([0-9a-f]{7,40})",
                        t[1] or "", re.I)
              or re.search(r"\bhead[ :]*`?([0-9a-f]{7,40})",
                           (t[1] or "") + " " + (t[2] or ""), re.I)
              or re.search(r"\b([0-9a-f]{40})\b", t[2] or ""))
        return int(mp.group(1)), ((mh.group(1) if mh else "")[:7]
                                  or None), "manual"

    live_ids = {t[0] for t in live}
    rev_keyed = {}
    rev_headless = {}
    for t in live:
        if t[3] != "reviewer" or t[4] not in (
                "ready", "todo", "running", "review"):
            continue
        g = _rev_head_of(t)
        if not g:
            continue
        if g[1]:
            rev_keyed.setdefault((g[0], g[1]), []).append((t, g[2]))
        else:
            rev_headless.setdefault(g[0], []).append(t)
    rev_dup_groups = dict(rev_keyed)
    for gk, members in list(rev_dup_groups.items()):
        members += [(t, "manual") for t in rev_headless.get(gk[0], [])]
        rev_dup_groups[gk] = members
    for n, hl in rev_headless.items():
        if n not in {g[0] for g in rev_keyed} and len(hl) >= 2:
            rev_dup_groups[(n, None)] = [(t, "manual") for t in hl]
    for gk, members in sorted(rev_dup_groups.items()):
        if len(members) < 2:
            continue
        pairs = [suf for _, suf in members if suf.startswith("-p")]
        if len(members) == 2 and len(pairs) == 2 and pairs[0] != pairs[1]:
            continue
        def _rank(m):
            t, suf = m
            order = {"running": 0, "review": 1, "ready": 2, "todo": 3}
            created = con.execute(
                "SELECT created_at FROM tasks WHERE id=?", (t[0],)
            ).fetchone()
            return (order.get(t[4], 9), 0 if suf == "" else 1,
                    created[0] if created else 0, t[0])
        members = sorted(members, key=_rank)
        keeper = members[0][0]
        for t, _suf in members[1:]:
            kids = [c for (c,) in con.execute(
                "SELECT child_id FROM task_links WHERE parent_id=?",
                (t[0],))]
            if any(k in live_ids for k in kids):
                findings.append((
                    "%s:revlayer:dup-review-children:%d:%s"
                    % (slug, gk[0], gk[1]), 1, (
                        "    PR #%s %s: live review duplicate %s next to"
                        " working card %s, but %s has live children (%s):"
                        " a human decides how to break it."
                        % (gk[0], gk[1] or "-", t[0], keeper, t[0],
                           ",".join(k for k in kids if k in live_ids)))))
                continue
            run([HERMES, "kanban", "--board", slug, "comment", t[0],
                 ADMISSION_MARKER + " live review duplicate of PR #%s"
                 " %s: working card %s already covers this head, a review"
                 " run on the duplicate is max effort wasted. Archived."
                 % (gk[0], gk[1] or "-", keeper)], timeout=90)
            run([HERMES, "kanban", "--board", slug, "archive", t[0]],
                timeout=90)
            findings.append((
                "%s:revlayer:dup-review:%d:%s" % (slug, gk[0], gk[1]), 2, (
                    "    PR #%s %s: live review duplicate %s archived,"
                    " working card %s (live-duplicate dedup extended to"
                    " reviewers by the PR+head key)."
                    % (gk[0], gk[1] or "-", t[0], keeper))))
    con.close()

    # Live PR owners in one query (the same live_cards_by_pr the rebase
    # generator uses).
    owner_by_pr = _rework_owner_map(db, head_of, live)

    states = {}   # number -> review state (cache for both check phases)
    for number in numbers:
        state = _pr_review_state(repo, number)
        if not state:
            continue
        states[number] = state

    # Scheme "one PR = one card" (owner decision). The auto-arm of reviewer
    # cards per head (key reviewer-pr<N>-<head7>), the paired arm and the
    # WCFY arm per head were removed: review lives on the executor card
    # (kanban_request_review with reviewer=reviewer), the executor writes
    # the WCFY with the same card, and waiting for the owner goes through
    # needs_input with the condition "PR #N merged by the owner". What is
    # left is the rework arm (a change-request verdict on a head without a
    # rework card).
    rework_armed, ms_rework_wait = [], []
    for number in sorted(states):
        st = states[number]
        info = st.get("info")
        if not info or st["age"] is None:
            continue
        if st["age"] < REVIEW_ARM_MIN_AGE_S \
                or st["age"] > REVIEW_ARM_MAX_AGE_S:
            continue
        head = st["head"]
        if info["kind"] != "reject":
            continue
        # A change-request verdict on a head must have a live rework card.
        # Without this, PRs with change requests stood without a card while
        # the slots were taken by work of the next milestone.
        if not cr_rework_gaps({number: st}, live):
            continue
        # A live card that owns the PR (ranks 0-3) retires the arm: the
        # owner does the rework, no second card is created ("one PR = one
        # card"). Handoff by a comment, once per head (marker).
        handoff = _rework_owner_handoff(owner_by_pr, number, head, info)
        if handoff:
            keeper, src_name = handoff
            marker = "%s:%s" % (OWNER_REWORK_MARKER, head[:7])
            if not _card_commented(db, keeper, marker):
                run([HERMES, "kanban", "--board", slug, "comment", keeper,
                     "Watchdog: a CR verdict (source: %s) on head %s of"
                     " PR #%d, and this card owns the PR (rule \"one PR ="
                     " one card\"): this card does the rework, the"
                     " watchdog does not create a duplicate card. Close the"
                     " verdict blockers, write a rework response to every"
                     " BLOCK item, then kanban_request_review with"
                     " reviewer=reviewer. The marker %s silences repeated"
                     " reminders on this head."
                     % (src_name, head[:7], number, marker)], timeout=90)
            findings.append((
                "%s:revlayer:rework-owner:%d:%s" % (slug, number,
                                                    head[:7]), 1,
                ("    PR #%d %s CR verdict (%s) with a live owner %s: no"
                 " rework card created, handoff sent to the owner."
                 % (number, head[:7], src_name, keeper))))
            continue
        base = "coder-pr%d-rework-%s" % (number, head[:7])
        fam_state, key, rearm_of = _family_arm_key(base, live, done_keyed)
        if fam_state == "live":
            continue
        admit, admit_why = gen_admission(slug, number)
        if admit:
            ms_rework_wait.append(number)
        else:
            findings.append((
                "r36:revlayer:rework-noadmit:%s:%d" % (slug, number), 2,
                ("    PR #%d %s with a change-request verdict has no rework "
                 "card, no arm created: %s (admission is checked before "
                 "create)" % (number, head[:7], admit_why))))
            continue
        if len(rework_armed) >= REVIEW_ARM_MAX_CREATE:
            continue
        # The single native-or-live helper before create.
        covered, guard = board_native_guard.native_or_live(
            key,
            live_keys=[t[6] for t in live],
            native_checked=[
                board_native_guard.NATIVE_VERDICT,
                board_native_guard.NATIVE_REVIEW_LANE],
            why="a change-request verdict of %s on a head without a"
                " rework card: there is no stock mechanism for rework on"
                " such a verdict (the CR rework arm)"
                % _verdict_source_name(info))
        if covered:
            findings.append((
                "%s:revlayer:50b-rework:%d:%s" % (slug, number,
                                                   head[:7]), 1,
                "    Rework arm for PR #%d %s retired: %s"
                % (number, head[:7], guard)))
            continue
        new_id = _rework_arm_card(slug, number, head, info,
                                  key=key, rearm_of=rearm_of,
                                  guard=guard)
        if new_id:
            rework_armed.append((new_id, number, head[:7], key))
    for new_id, number, head7, key in rework_armed:
        findings.append((
            "%s:r36:rework-arm:%d:%s" % (slug, number, head7), 1, (
            "    PR #%d %s with a change-request verdict had no rework "
            "card: the watchdog created %s (coder, band of the active "
            "milestone, key %s)."
            % (number, head7, new_id, key))))
    # A PR of the active milestone without a live card and without a formal
    # owner review: green PRs waiting for the owner were absent from the
    # board, and while the band of the active milestone is empty the
    # dispatcher fills the slots with lower-band work of the next one.
    for number in milestone_pr_orphans(
            states, live, lambda n: gen_admission(slug, n)[0],
            owner_by_pr=owner_by_pr):
        findings.append((
            "%s:r36:ms-orphan:%d" % (slug, number), 2, (
            "    PR #%d of the active milestone has no live card and no "
            "formal owner review: the work fell off the board, the next "
            "step is not assigned." % number)))
    # Only change requests without a rework card hold the layer
    # (ms_rework_wait). Heads without a verdict hold nothing any more:
    # review lives in the stock review lane of the executor card.
    holding = [(n, (states.get(n) or {}).get("head", "")[:7] or "?", 0)
               for n in sorted(set(ms_rework_wait))]
    if holding:
        parkable = [t for t in live
                    if t[4] == "ready" and (t[3] or "")
                    and (t[5] or 0) < REVIEW_PARK_BELOW_P]
        parked = []
        for task_id, title, _, _, _, prio, _ in parkable:
            reason = (REVIEW_PARK_MARKER + ": unchecked heads %d; "
                      "a card below p%d does not take a slot, it returns to "
                      "ready when the review layer empties"
                      % (len(holding), REVIEW_PARK_BELOW_P))
            out = run([HERMES, "kanban", "--board", slug, "schedule", task_id,
                       reason], timeout=90)
            if out.startswith("Scheduled"):
                parked.append("%s(p%d)" % (task_id, prio or 0))
        running_low = ["%s(p%d)" % (t[0], t[5] or 0) for t in live
                       if t[4] == "running"
                       and (t[5] or 0) < REVIEW_PARK_BELOW_P]
        if parked or running_low:
            text = ("    Review layer is not empty (%d heads without a "
                    "verdict): " % len(holding))
            if parked:
                text += "parked ready <p%d: %s. " % (
                    REVIEW_PARK_BELOW_P, ", ".join(parked))
            if running_low:
                text += ("Slots are held by running <p%d: %s: there is no "
                         "preemption, see the comment at review_layer_check."
                         % (REVIEW_PARK_BELOW_P, ", ".join(running_low)))
            findings.append(("%s:revlayer:park:%s"
                             % (slug, ",".join(parked)[:60] or "run"), 2,
                             text))
    else:
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
        cand = [row[0] for row in con.execute(
            "SELECT DISTINCT t.id FROM tasks t JOIN task_comments c "
            "ON c.task_id = t.id WHERE t.status = 'scheduled' "
            "AND c.body LIKE ?",
            ("%SCHEDULED: " + REVIEW_PARK_MARKER + "%",))]
        # Look at the latest scheduled reason, not at any old SCHEDULED
        # comment of the review layer in the history. If the admission gate
        # parked the card after the review parking, a release here would
        # start a loop release -> park -> release.
        held = []
        for tid in cand:
            last = con.execute(
                "SELECT payload FROM task_events WHERE task_id = ?"
                " AND kind = 'scheduled' ORDER BY id DESC LIMIT 1",
                (tid,)).fetchone()
            if last and REVIEW_PARK_MARKER in (last[0] or ""):
                held.append(tid)
        con.close()
        released = []
        for task_id in held:
            if run([HERMES, "kanban", "--board", slug, "unblock", task_id],
                   timeout=90):
                released.append(task_id)
        if released:
            findings.append(("%s:revlayer:release:%s"
                             % (slug, ",".join(released)[:60]), 2, (
                "    Review layer emptied: returned from parking to ready: %s."
                % ", ".join(released))))
    return findings


# === Card budget and task size =============================================
#
# The rule "stop at 150 calls" in a skill does not enforce itself, so the
# mechanism lives in the watchdog. Two places:
#
#   1. Before start: a rework card for coder with 3+ BLOCK items does not go
#      into a run: the watchdog reassigns it to the architect and creates a
#      decomposition card with key decompose-<task_id>. The threshold comes
#      from board data (whole history): the risk of burning out grows with
#      the package size (up to 2 BLOCK is low, 3-4 higher, 5+ about half,
#      7 BLOCK always). Race: the dispatcher ticks every few minutes, the
#      watchdog hourly, and a fresh rework card is picked up within a few
#      minutes, so the hourly check covers the queue, retries and the
#      backlog after a stop; fresh cards need a frequent pass
#      (--rework-gate-only, meant to run from cron every 5 minutes) and a
#      gate at card creation from a verdict.
#
#      The criterion by body lines is NOT used. Body lines are context, not
#      the amount of work: a card with 40 lines and one BLOCK fit into one
#      run, but a line-length rule handed it to the architect. The amount of
#      work = 3+ BLOCK. The reverse move (the architect's "fits in one run"
#      verdict on the decomposition) is automatic too: the gate returns the
#      parent to coder itself (rework_gate_auto_return), without the owner:
#      the architect is a cron job, not a run, and the CLI guard does not
#      restrain its actions.
#      BLOCK items are counted by item, not by word. A body may hold two
#      blockers as lines ("BLOCK 1:", "BLOCK 2:") plus a recap "2 BLOCK, one
#      card" in the header: a plain \bBLOCK\b counted 3 and the gate handed
#      minimal work to the architect, burning a run on decomposition. The
#      source of truth: the "- [BLOCK]" items of the verdict and the same
#      lines carried into the body; a recap does not count a second time.
#   2. After a burnout: a run that ended with Iteration budget exhausted /
#      max_iterations and has not a single comment on the card after its end
#      gets a "done / left" note from the watchdog, built from the artifact:
#      branch, git log base..HEAD, git status --porcelain, the last commit,
#      BLOCK items from the body marked by whether commits mention them.
#      Idempotent per run: any comment after the end of the run (worker,
#      owner, the watchdog itself) closes the need. In the measured window
#      almost no burned-out run had a comment.

REWORK_GATE_BLOCK_MIN = 3   # 3+ BLOCK items in the body: incompatible with one run
# There is no threshold by body lines: lines are context, the amount of work
# is the number of BLOCK items.
REWORK_GATE_RE = re.compile(
    r"rework|owner review|owner verdict|CHANGES REQUESTED|fix owner"
    r"|owner CHANGES|review blockers", re.I)
REWORK_GATE_MARKER = "Watchdog: rework card size gate."
REWORK_RETURN_MARKER = "Watchdog: size gate, automatic return by verdict."
# The architect's verdict "the package fits into one run". Sources scanned:
# the decomposition result, its comments, the run summary. The decomposition
# BODY is NOT scanned: it holds the gate's instruction with that word, which
# is not a verdict.
REWORK_FITS_RE = re.compile(r"fits-in-one-run", re.I)
# A blocker is an ITEM with a BLOCK marker, not a word. Items:
# "- [BLOCK] ..." (verdict checklist, the source of truth), "[BLOCK] 1: ...",
# "BLOCK 1: ...", "BLOCK: ...", including bold/italic formatting. Prose
# recaps ("2 BLOCK, one card", "BLOCK 1 and BLOCK 2 are closed") are not
# items and are not counted a second time.
BLOCK_ITEM_RE = re.compile(
    r"^\s*(?:[-*+>]|\d+[.)])?\s*[*_]{0,2}"
    r"(?:\[BLOCK\]|BLOCK(?:\s*\d+)?\s*[.:)])")
BURN_PROGRESS_MARKER = _fm.BURN_PROGRESS_MARKER
BURN_PROGRESS_WINDOW_S = 7 * 24 * 3600
# System comments (block/watchdog/parking) do not record progress: the budget
# gate must not close the need for a "done / left" note with its own block.
# The regex and the marker live canonically in metrics-evening.py
# (closes_burnout_progress): the evening metric and this watchdog count the
# same way.
SYSTEM_COMMENT_RE = _fm.SYSTEM_COMMENT_RE


def _closes_progress_need(body):
    """Does the comment close the need for a progress comment?

    Delegates to the single shared definition (factory metrics): a watchdog
    marker or any substantive non-system comment.
    """
    return _fm.closes_burnout_progress(body)


def blocker_items(body):
    """Blocker item lines in the body.

    A BLOCK marker at the start of a list item. Recaps in prose ("2 BLOCK,
    one card") are not items and are not counted a second time.
    """
    return [l for l in (body or "").splitlines() if BLOCK_ITEM_RE.match(l)]


def rework_oversize_scan(slug):
    """ready+coder rework cards with 3+ BLOCK items in the body (amount of work).

    Body lines are NOT a criterion: they are context, not amount of work (a
    40-line body with one BLOCK fit into one run). Counting is by blocker
    items, not by occurrences of the word (two blockers with three
    occurrences caused a false interception). Returns [(task_id, title,
    blocks, lines, priority)]; lines is for the report only.
    """
    db = os.path.join(BOARDS, slug, "kanban.db")
    if not os.path.exists(db):
        return []
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
    out = []
    for tid, title, body, prio in con.execute(
            "SELECT id, title, body, priority FROM tasks"
            " WHERE status = 'ready' AND assignee = 'coder'"):
        if not REWORK_GATE_RE.search((title or "") + " " + (body or "")):
            continue
        blocks = len(blocker_items(body))
        nlines = (body or "").count("\n") + 1 if body else 0
        if blocks >= REWORK_GATE_BLOCK_MIN:
            out.append((tid, title or "", blocks, nlines, prio or 0))
    con.close()
    return out


def _decompose_card(slug, tid, title, blocks, nlines, prio, key):
    """Decomposition card for the architect. Returns the id of the new card or None."""
    # Native-or-live helper before create (the calling gate already checked
    # the live key decompose-<tid>; here the family is checked again).
    covered, guard = board_native_guard.native_or_live(
        key,
        live_keys=_live_keys(slug),
        native_checked=[
            board_native_guard.NATIVE_RESPAWN,
            board_native_guard.NATIVE_TASK_LINKS],
        why="rework package with 3+ BLOCK: the stock auto_decompose does not "
            "cut rework bodies with BLOCK items by acceptance criteria, the "
            "rework size gate must intercept it itself")
    if covered:
        return None
    reason = "%d BLOCK in the body" % blocks
    body = (
        board_native_guard.guard_section(guard) +
        "Parent: %s (%s). Reason for interception: %s.\n\n"
        "Rework card size gate: a package with 3+ BLOCK items is not given "
        "to coder (the amount of work = 3+ BLOCK, the body length is not a "
        "criterion). By board data the risk of burning out at the iteration "
        "ceiling grows with the package size (low up to 2 BLOCK, about half "
        "at 5+, always at 7), and every burnout costs hours of machine time "
        "and a run from scratch.\n\n"
        "Task: cut the parent body into cards of 1-2 BLOCK each (minor "
        "findings in one card), each with its own acceptance criterion; the "
        "branch and base are inherited from the parent body, commits of the "
        "first run may already be in the branch.\n\n"
        "A verdict is mandatory and a machine reads it: if the package "
        "really fits into one run, close this card with a comment containing "
        "the word FITS-IN-ONE-RUN, and the gate itself will return the "
        "parent to the coder executor on the next pass (every 5 minutes). If "
        "the package is large, create the children and close with a comment "
        "SPLIT, the parent stays with the architect until the children are "
        "delivered."
        % (tid, (title or "")[:80], reason))
    body_path = None
    try:
        with tempfile.NamedTemporaryFile(
                "w", suffix=".md", encoding="utf-8", delete=False) as fh:
            fh.write(body)
            body_path = fh.name
        out = run([HERMES, "kanban", "--board", slug, "create",
                   "[architect] Decomposition %s (size gate)" % tid,
                   "--body-file", body_path,
                   "--assignee", "architect",
                   "--priority", str(max(prio or 0, 95)),
                   "--idempotency-key", key,
                   "--created-by", "watchdog",
                   "--skill", "decomposition-contract", "--json"],
                  timeout=120)
        try:
            return (json.loads(out) or {}).get("id")
        except Exception:
            m = re.search(r'"id"\s*:\s*"(t_[0-9a-f]+)"', out or "")
            return m.group(1) if m else None
    finally:
        if body_path:
            try:
                os.unlink(body_path)
            except OSError:
                pass


def rework_oversize_gate(slug):
    """Intercept an oversized rework card before its first run. Acts
    immediately, like idempotency_gate. Returns findings.

    Idempotency: a live (non-archived) card with key decompose-<task_id>
    means the gate already ran; the marker comment is not duplicated.
    """
    hits = rework_oversize_scan(slug)
    if not hits:
        return []
    db = os.path.join(BOARDS, slug, "kanban.db")
    findings = []
    live_keys = _live_keys(slug)
    for tid, title, blocks, nlines, prio in hits:
        key = "decompose-%s" % tid
        # Reassignment plus a decomposition create: the native-or-live helper.
        covered, guard = board_native_guard.native_or_live(
            key,
            live_keys=live_keys,
            native_checked=[
                board_native_guard.NATIVE_RESPAWN,
                board_native_guard.NATIVE_TASK_LINKS],
            why="rework package with %d BLOCK: the stock auto_decompose does "
                "not cut rework bodies with BLOCK items, interception by the "
                "rework size gate is required" % blocks)
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
        exists = con.execute(
            "SELECT 1 FROM tasks WHERE idempotency_key = ?"
            " AND status != 'archived' LIMIT 1", (key,)).fetchone()
        commented = con.execute(
            "SELECT 1 FROM task_comments WHERE task_id = ? AND body LIKE ?"
            " LIMIT 1", (tid, REWORK_GATE_MARKER + "%")).fetchone()
        con.close()
        if exists:
            continue
        if covered:
            findings.append((
                "%s:rework-oversize-50b:%s" % (slug, tid), 1,
                "    Size gate: interception of %s suppressed (native or "
                "live): %s" % (tid, guard)))
            continue
        reason = "%d BLOCK in the body" % blocks
        if not commented:
            run([HERMES, "kanban", "--board", slug, "comment", tid,
                 REWORK_GATE_MARKER + " " + reason + " (threshold: 3+ BLOCK, "
                 "amount of work; counted by blocker items, not by words; "
                 "the body length is not a criterion). The card is "
                 "reassigned to the architect, a decomposition is created "
                 "with key " + key + ". Packages of this size burn out at "
                 "the iteration ceiling. A verdict \"fits-in-one-run\" when "
                 "the decomposition is closed returns the card to the coder "
                 "executor automatically (the gate, every 5 minutes)."],
                timeout=90)
        run([HERMES, "kanban", "--board", slug, "reassign", tid, "architect",
             "--reason", "rework-oversize gate: " + reason], timeout=90)
        _decompose_card(slug, tid, title, blocks, nlines, prio, key)
        findings.append(("%s:rework-oversize:%s" % (slug, tid), 1, (
            "    Size gate intercepted a rework package: " + tid + "\n"
            "      " + (title or "")[:70] + "\n"
            "      " + reason + ". Reassigned to the architect, "
            "decomposition: " + key + ".")))
    return findings


def _fit_verdict_text(slug, dec_id):
    """Verdict text of a closed decomposition: result + comments + summary of
    the last run. The card body is NOT read: it holds the gate's instruction
    with the verdict word, which is not the architect's verdict.
    """
    db = os.path.join(BOARDS, slug, "kanban.db")
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
        chunks = []
        row = con.execute(
            "SELECT result FROM tasks WHERE id = ?", (dec_id,)).fetchone()
        if row and row[0]:
            chunks.append(row[0])
        chunks.extend(r[0] or "" for r in con.execute(
            "SELECT body FROM task_comments WHERE task_id = ?"
            " ORDER BY id", (dec_id,)))
        srow = con.execute(
            "SELECT summary FROM task_runs WHERE task_id = ?"
            " ORDER BY id DESC LIMIT 1", (dec_id,)).fetchone()
        if srow and srow[0]:
            chunks.append(srow[0])
        con.close()
        return "\n".join(chunks)
    except Exception:
        return ""


def rework_gate_auto_return(slug, apply=True):
    """A "fits-in-one-run" verdict returns the parent to coder by itself.

    Without this, the gate hands the card to the architect, the
    decomposition closes with the verdict "fits in one run", and the parent
    would stay with the architect until a manual command from the owner. The
    architect is a cron job, not a run: the CLI guard does not restrain its
    actions, so the gate does the return.

    Condition: the decomposition (key decompose-<tid>) is done or archived,
    its verdict text (result, comments, summary; NOT the body) contains
    "fits-in-one-run", the parent is still with the architect and has not
    yet received the return marker comment. Actions: marker comment,
    unblock if blocked, reassign to coder. Idempotency: the parent's
    assignee != architect after the return; the marker comment protects
    against a repeat if a failure falls between the steps. Returns findings.
    """
    db = os.path.join(BOARDS, slug, "kanban.db")
    if not os.path.exists(db):
        return []
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
    candidates = []
    for dec_id, key in con.execute(
            "SELECT id, idempotency_key FROM tasks"
            " WHERE idempotency_key LIKE 'decompose-t_%'"
            " AND status IN ('done', 'archived')").fetchall():
        tid = key[len("decompose-"):]
        parent = con.execute(
            "SELECT status FROM tasks WHERE id = ? AND assignee = 'architect'",
            (tid,)).fetchone()
        if not parent:
            continue
        marked = con.execute(
            "SELECT 1 FROM task_comments WHERE task_id = ? AND body LIKE ?"
            " LIMIT 1", (tid, REWORK_RETURN_MARKER + "%")).fetchone()
        if marked:
            continue
        if REWORK_FITS_RE.search(_fit_verdict_text(slug, dec_id)):
            candidates.append((tid, dec_id, parent[0]))
    con.close()
    findings = []
    live_keys = _live_keys(slug)
    for tid, dec_id, status in candidates:
        # Returning the parent is a restoration: first ask the helper
        # whether the stock respawn would pick the card up by itself (a
        # manual restoration once produced a duplicate executor).
        covered, guard = board_native_guard.native_or_live(
            "rework-return-%s" % tid,
            live_keys=live_keys,
            native_checked=[
                board_native_guard.NATIVE_RESPAWN,
                board_native_guard.NATIVE_TASK_LINKS],
            why="decomposition %s closed with the verdict "
                "\"fits-in-one-run\", the parent is still with architect: "
                "there is no stock mechanism returning the parent after a "
                "decomposition (the CLI guard restrains only runs, the "
                "architect is a cron job)" % dec_id)
        if covered:
            findings.append((
                "%s:rework-fit-return-50b:%s" % (slug, tid), 1,
                "    Size gate: return of %s suppressed (native or live): %s"
                % (tid, guard)))
            continue
        text = ("    Size gate, automatic return: " + tid + "\n"
                "      decomposition " + dec_id + " closed with the verdict"
                " \"fits-in-one-run\", the parent is returned to coder.\n")
        if not apply:
            findings.append(("%s:rework-fit-return:%s" % (slug, tid), 1,
                             text + "      (dry-run: the board is untouched)"))
            continue
        run([HERMES, "kanban", "--board", slug, "comment", tid,
             REWORK_RETURN_MARKER + " decomposition " + dec_id +
             " closed with the verdict \"fits-in-one-run\": returned to the"
             " coder executor automatically."], timeout=90)
        if status == "blocked":
            run([HERMES, "kanban", "--board", slug, "unblock", tid,
                 "--reason", "size gate: verdict \"fits-in-one-run\", the"
                 " block is not needed (automatic)"], timeout=90)
        run([HERMES, "kanban", "--board", slug, "reassign", tid, "coder",
             "--reason", "rework-oversize gate: verdict \"fits-in-one-run\""
             " (decomposition " + dec_id + "), automatic return"],
            timeout=90)
        findings.append(("%s:rework-fit-return:%s" % (slug, tid), 1, text))
    return findings


def _git_ws(ws, args, timeout=30):
    return (run(["git", "-C", ws] + args, timeout=timeout) or "").strip()


def _workspace_artifact(ws, base_hint):
    """(branch, base, log_text, status_text, last_commit) or None.

    base: origin/<hint from the body>, the local branch, then the first
    configured base branch. If there is no base: the last 15 commits of the
    head (better than nothing).
    """
    if not ws or not os.path.exists(os.path.join(ws, ".git")):
        return None
    base_branch = tuple(cfg_list("FACTORY_BASE_BRANCHES", ("main",)))[0]
    branch = _git_ws(ws, ["rev-parse", "--abbrev-ref", "HEAD"]) or "?"
    base = None
    for cand in (("origin/%s" % base_hint) if base_hint else None,
                 base_hint, "origin/%s" % base_branch, base_branch):
        if cand and _git_ws(ws, ["rev-parse", "--verify", "--quiet", cand]):
            base = cand
            break
    if base:
        mb = _git_ws(ws, ["merge-base", base, "HEAD"])
        log = _git_ws(ws, ["log", "%s..HEAD" % (mb or base), "--oneline"])
    else:
        log = _git_ws(ws, ["log", "-15", "--oneline"])
    log = "\n".join([l for l in log.splitlines() if l.strip()][:15])
    status = _git_ws(ws, ["status", "--porcelain"])
    last = _git_ws(ws, ["log", "-1", "--format=%h %s"])
    return branch, base, log, status, last


def _block_lines(body):
    # only blocker item lines, recaps in prose are not listed
    return [l.strip()[:110] for l in blocker_items(body)]


def _burn_progress_body(tid, title, branch, ws, body, run_id, error):
    base_re = "|".join(re.escape(b) for b in
                       tuple(cfg_list("FACTORY_BASE_BRANCHES", ("main",))))
    m = re.search(r"(%s)" % base_re, body or "")
    art = _workspace_artifact(ws, m.group(1) if m else None)
    blocks = _block_lines(body)
    lines = ["%s (run #%d, %s)" % (
        BURN_PROGRESS_MARKER, run_id, (error or "").split("\n")[0][:80])]
    lines.append("The next run starts from here, not from scratch.")
    lines.append("")
    lines.append("Card: %s %s" % (tid, (title or "")[:70]))
    lines.append("Branch: %s" % (branch or (art[0] if art else None) or "unknown"))
    if art:
        _, base, log, status, last = art
        lines.append("Last commit: %s" % (last or "no commits"))
        lines.append("git log %s..HEAD (up to 15):" % (base or "-15"))
        lines.append(log or "  (no commits beyond the base)")
        lines.append("git status --porcelain:")
        lines.append("\n".join([l for l in status.splitlines()][:20])
                     if status.strip() else "  (working tree is clean)")
        ncommits = len([l for l in log.splitlines() if l.strip()])
    else:
        lines.append("Artifact unavailable: the worktree is missing or not git.")
        lines.append("git log / git status were not taken: restore the "
                     "worktree from the branch above.")
        ncommits = None
    done = unknown = 0
    if blocks:
        lines.append("BLOCK items from the card body:")
        log_text = (art[2] if art else "") + " " + ((art[4] if art else "") or "")
        for b in blocks:
            num = re.search(r"BLOCK[- ]?(\d+)", b, re.I)
            if not num:
                unknown += 1
                lines.append("  [?] %s (no number, check by hand)" % b)
            elif re.search(r"block[- ]?%s\b" % num.group(1), log_text, re.I):
                done += 1
                lines.append("  [+] %s (mentioned in the branch commits)" % b)
            else:
                lines.append("  [ ] %s (not mentioned in commits)" % b)
    else:
        lines.append("BLOCK items are not marked in the body: check the "
                     "artifact against the body by hand.")
    left = len(blocks) - done
    lines.append("")
    if ncommits is not None:
        lines.append("Done (from the artifact): %d commit(s) beyond the base." % ncommits)
    lines.append("Left (from the body): %d BLOCK without a [+] mark%s. A "
                 "mention in a commit does not prove closure: the next run "
                 "starts by checking the code against each item."
                 % (left, (", %d without a number" % unknown) if unknown else ""))
    return "\n".join(lines)


def burnout_progress_scan(slug, window_s=BURN_PROGRESS_WINDOW_S):
    """Open cards with a burnout and no comment after the end of the run.

    Returns a list of dict(run_id, task_id, error, title, body, ws, branch,
    ended). Burnout = 'Iteration budget' / 'budget exhausted' /
    'max_iterations' in the run error or metadata. blocked and scheduled are
    NOT excluded (owner decision): a burned-out card that was blocked
    afterwards is exactly the one where the next run needs "done / left",
    otherwise after unblock the run starts from scratch. There is no
    conflict with the budget gate: the block comment and the progress
    comment are different markers, and progress is written once per run.
    done/archived are excluded: the work was delivered.
    """
    db = os.path.join(BOARDS, slug, "kanban.db")
    if not os.path.exists(db):
        return []
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
    since = time.time() - window_s
    tasks = {}
    for row in con.execute(
            "SELECT id, title, body, status, workspace_path, branch_name"
            " FROM tasks WHERE status NOT IN ('done','archived')"):
        tasks[row[0]] = row
    comments = {}
    for tid, created, body in con.execute(
            "SELECT task_id, created_at, body FROM task_comments"):
        if _closes_progress_need(body):
            comments.setdefault(tid, []).append(float(created or 0))
    out = []
    for rid, tid, started, ended, error, metadata in con.execute(
            "SELECT id, task_id, started_at, ended_at, error, metadata"
            " FROM task_runs WHERE ended_at > ? ORDER BY ended_at", (since,)):
        blob = "%s %s" % (error or "", metadata or "")
        if not ("Iteration budget" in blob or "budget exhausted" in blob
                or "max_iterations" in blob):
            continue
        if tid not in tasks:
            continue
        if any(c >= float(ended or 0) for c in comments.get(tid, [])):
            continue  # progress was recorded by a live comment
        t_id, title, body, status, ws, branch = tasks[tid]
        out.append({"run_id": rid, "task_id": tid, "error": error,
                    "title": title, "body": body, "ws": ws,
                    "branch": branch, "ended": ended})
    con.close()
    return out


def burnout_progress_comment(slug, window_s=BURN_PROGRESS_WINDOW_S):
    """Writes "done / left" from the artifact onto burned-out cards that have
    no comment after the run. Idempotent per run: a comment (by any author)
    after the end of the run closes the need."""
    findings = []
    for item in burnout_progress_scan(slug, window_s=window_s):
        body = _burn_progress_body(
            item["task_id"], item["title"], item["branch"], item["ws"],
            item["body"], item["run_id"], item["error"])
        run([HERMES, "kanban", "--board", slug, "comment",
             item["task_id"], body], timeout=90)
        findings.append(("%s:burn-progress:%s:%s" % (
            slug, item["task_id"], item["run_id"]), 1, (
            "    Burnout without recorded progress: " + item["task_id"] +
            " (run #%d)" % item["run_id"] + "\n"
            "      " + (item["title"] or "")[:70] + "\n"
            "      The watchdog wrote done/left from the artifact: branch, "
            "git log base..HEAD, git status, BLOCK items with marks.")))
    return findings


# === Verdict item dedup gate =============================================
#
# A card for an owner verdict is created by a webhook session; its dedup
# looks for a live card of the same PR by PR + head in the body, while
# visual-delta cards (pr-hygiene) carry no head. Result: a card made from a
# verdict carries a Blocking item such as "Before/After" that a live
# [visual-delta] card of the same PR already covers, so two coders make the
# same screenshots. The rule used to live in one session's head; now it is a
# gate.
#
# Mechanism: a pair of live cards of one PR; the later one has a blocker item
# of a known class, the earlier one has the same class in its title. The gate
# subtracts the item from the later body, leaving a line "Item N (...) covers
# t_...", cleans the title and writes a marker comment. The later card always
# yields. The body is edited only before the first run (ready/todo/blocked/
# running/review get a report without an edit: changing a body under a
# working worker breaks the run). The reverse order (the cover is younger
# than the verdict) is not cut by this gate: there the duplicate is created
# by pr-hygiene and its parking is a separate question.
#
# A parked card is NOT a cover. A Before/After item of a verdict once went
# through this gate into a scheduled visual-delta card (parked because the
# PR was not a milestone): a two-line formality nobody would do, with the
# merge standing. With a scheduled cover the scan is silent: either the card
# is raised by the owner-review gate (a PR with a formal owner review), and
# then the cover counts again, or the item stays in the verdict card with a
# live executor.

VERDICT_DEDUP_MARKER = "Watchdog: verdict item dedup."
VERDICT_PR_RE = re.compile(r"#(\d+)")
VERDICT_ITEM_HEAD_RE = re.compile(
    r"^##\s+(?:blocking\s+item|blockers?|blocks?)\s*(\d+)?\s*[:.]-?\s*[:：]?\s*(.*)$",
    re.IGNORECASE)
VERDICT_SECTION_SPLIT_RE = re.compile(r"^#{2,4}\s")
# Heading of the acceptance criteria section, and lines already marked by the
# gate (an inserted "covers t_..." note is not a requirement).
VERDICT_AC_HEAD_RE = re.compile(r"^#{1,4}\s*\**\s*acceptance\s*criteria", re.I)
VERDICT_MARKER_NOTE_RE = re.compile(r"covers t_[0-9a-f]+|verdict dedup")
# An earlier insertion tail claimed "criterion removed" while the AC section
# was not touched; the truth is "marked and not performed".
VERDICT_OLD_TAIL = "the acceptance criterion for the item is removed from this card"
VERDICT_NEW_TAIL = ("the acceptance criteria for the item are marked in the"
                    " Acceptance criteria section and are not performed by"
                    " this card")
# Cover classes: (name, item marker in the body, cover-card marker in the
# title, title cleanup token for the later card). The list is extended by one
# row: update-branch and WCFY are included on the model of real cards.
VERDICT_COVER_CLASSES = (
    ("Before / After section",
     re.compile(r"before\s*/?\s*after", re.I),
     re.compile(r"\[visual-delta\]", re.I),
     re.compile(r"Before\s*/\s*After", re.I)),
    ("update-branch / rebase",
     re.compile(r"update-?branch|\brebase\b", re.I),
     re.compile(r"update-?branch|\brebase\b", re.I),
     re.compile(r"update-?branch|\brebase\b", re.I)),
    ("What changes for you comment (WCFY)",
     re.compile(r"what\s*changes\s*for\s*you|\bWCFY\b", re.I),
     re.compile(r"what-?changes-?for-?you|\bWCFY\b", re.I),
     re.compile(r"What-changes-for-you", re.I)),
)


def verdict_blocking_items(body):
    """Blocker items of a verdict card body.

    Two forms: a heading "## Blocking item N: ..." (the block up to the next
    # heading or the end of the body) and a checklist line matching
    BLOCK_ITEM_RE (one line). The class of an item is determined by its
    first line: prose inside the block ("if update-branch conflicts...") is
    not an item. Returns a list of dict(start, end, num, text); end is
    exclusive.
    """
    out = []
    lines = (body or "").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        head = VERDICT_ITEM_HEAD_RE.match(line)
        if head:
            j = i + 1
            while j < len(lines) and not VERDICT_SECTION_SPLIT_RE.match(lines[j]):
                j += 1
            out.append({"start": i, "end": j, "num": head.group(1),
                        "text": head.group(2) or ""})
            i = j
            continue
        if BLOCK_ITEM_RE.match(line):
            num = re.search(r"BLOCK\W*#?(\d+)", line, re.I)
            out.append({"start": i, "end": i + 1,
                        "num": num.group(1) if num else str(len(out) + 1),
                        "text": line})
        i += 1
    return out


def _verdict_title_strip(title, covers):
    """Title without the tokens of covered classes. Conservative: if after
    cleanup the PR number is gone or the title became shorter than 12
    characters, the original is returned (cleanup is a convenience, not a
    criterion)."""
    t = title or ""
    orig = t
    for cname, _, _, tok_re in VERDICT_COVER_CLASSES:
        if cname in covers:
            t = tok_re.sub(" ", t)
    t = re.sub(r"\s*\+\s*\+\s*", " + ", t)
    t = re.sub(r"\s*,\s*\+\s*", ", ", t)
    t = re.sub(r"\s*\+\s*,\s*", ", ", t)
    t = re.sub(r"\(\s*\+\s*", "(", t)
    t = re.sub(r"\s*:\s*,\s*", ": ", t)
    t = re.sub(r",\s*,", ",", t)
    t = re.sub(r"[ \t]{2,}", " ", t).strip(" ,:+")
    if not VERDICT_PR_RE.search(t) or len(t) < 12:
        return orig
    return t


def _verdict_ac_bounds(lines):
    """(first, one_past_last) line of the Acceptance criteria section, or None.
    The section runs from its heading to the next # heading or the end of the body."""
    start = None
    for i, ln in enumerate(lines):
        if VERDICT_AC_HEAD_RE.match(ln):
            start = i + 1
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start, len(lines)):
        if VERDICT_SECTION_SPLIT_RE.match(lines[j]):
            end = j
            break
    return (start, end)


def _verdict_marker_nums(lines, cover_id):
    """Item number from an inserted line "Item N (...) covers <id>":
    the second pass takes the number from the line inserted by the first."""
    for ln in lines:
        if "covers %s" % cover_id in ln:
            m = re.match(r"^Item\s+([0-9?,]+)\s*\(", ln)
            if m:
                return m.group(1)
    return None


def _verdict_refresh_item_markers(lines, ac_by_class):
    """Brings the tail of the inserted item lines up to date: "criterion
    removed" becomes "marked and not required" for the classes where this
    pass marked acceptance-criteria lines. Returns the number of refreshed
    lines."""
    n = 0
    classes = list(ac_by_class.keys())
    for i, ln in enumerate(lines):
        if VERDICT_OLD_TAIL in ln and any(c in ln for c in classes):
            lines[i] = ln.replace(VERDICT_OLD_TAIL, VERDICT_NEW_TAIL)
            n += 1
    return n


def _verdict_dedup_plan(title, body, covers):
    """(new_body, new_title, removed, ac_marked, refreshed) for a card whose
    items or acceptance criteria are covered by covers {class name: (id,
    covering title)}; None if nothing overlaps.

    The coder closes a card by its Acceptance criteria, so a covered class
    is subtracted from THREE places: the blocking item (the block up to the
    next heading), the acceptance-criteria line (marked "criterion covers
    t_...", not deleted: the section stays complete but the line stops
    being a requirement) and the tail of the inserted item line, which said
    "criterion removed" while the AC section was untouched. The scan fires
    even when no items are left but the AC requirement is still alive (the
    second pass over an already treated card). removed: [(num, class,
    covering id)] in ascending item order. Replacements go bottom to top so
    indexes do not drift."""
    lines: list = (body or "").split("\n")
    item_hits = []
    for it in verdict_blocking_items(body):
        for cname, item_re, _, _ in VERDICT_COVER_CLASSES:
            if cname in covers and item_re.search(it["text"]):
                item_hits.append((it, cname))
                break
    ac_bounds = _verdict_ac_bounds(lines)
    ac_hits = []
    if ac_bounds:
        a, b = ac_bounds
        for idx in range(a, b):
            ln = lines[idx]
            if VERDICT_MARKER_NOTE_RE.search(ln):
                continue  # already marked: a gate insert, not a requirement
            for cname, item_re, _, _ in VERDICT_COVER_CLASSES:
                if cname in covers and item_re.search(ln):
                    ac_hits.append((idx, cname))
                    break
    if not item_hits and not ac_hits:
        return None
    class_nums = {}
    for it, cname in item_hits:
        class_nums.setdefault(cname, []).append(it["num"] or "?")
    ac_by_class = {}
    for idx, cname in ac_hits:
        ac_by_class.setdefault(cname, []).append(idx)
    # phase 1: criteria lines, line-by-line replacement (the list length
    # does not change); phase 2: item blocks, the splice shrinks the list,
    # so it goes strictly AFTER: item indexes were computed on the original
    # body
    for cname, idxs in ac_by_class.items():
        cover_id = covers[cname][0]
        nums = (", ".join(class_nums.get(cname) or [])
                or _verdict_marker_nums(lines, cover_id) or "?")
        for idx in sorted(idxs, reverse=True):
            m = re.match(r"^(\s*(?:[-*+]|\d+[.)])\s+)?", lines[idx])
            prefix = m.group(1) if m and m.group(1) else "- "
            lines[idx:idx + 1] = [
                "%sItem %s (%s): criterion covers %s, not required from this"
                " card (verdict dedup gate)."
                % (prefix, nums, cname, cover_id)]
    removed = []
    item_tail = (" " + VERDICT_NEW_TAIL[0].upper() + VERDICT_NEW_TAIL[1:]
                 + ".")
    for it, cname in sorted(item_hits, key=lambda h: -h[0]["start"]):
        cover_id = covers[cname][0]
        tail = item_tail if cname in ac_by_class else ""
        lines[it["start"]:it["end"]] = [
            "Item %s (%s) covers %s: duplicate removed by the verdict dedup "
            "gate, the work happens in that card.%s"
            % (it["num"] or "?", cname, cover_id, tail)]
        removed.append((it["num"] or "?", cname, cover_id))
    refreshed = _verdict_refresh_item_markers(lines, ac_by_class)
    removed.reverse()
    return ("\n".join(lines), _verdict_title_strip(title, covers), removed,
            len(ac_hits), refreshed)


def verdict_dedup_scan(slug):
    """Pairs "later verdict card, earlier covering card" of one PR.

    Verdict candidate: a live card recognised by REWORK_GATE_RE (as in the
    size gate). Covering card: another live card of the same PR, strictly
    earlier by created_at, with a class marker in its title ([visual-delta],
    update-branch, What-changes-for-you). Returns a list of dict(tid, title,
    status, new_body, new_title, removed).
    """
    db = os.path.join(BOARDS, slug, "kanban.db")
    if not os.path.exists(db):
        return []
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
    rows = con.execute(
        "SELECT id, title, body, status, created_at FROM tasks"
        " WHERE status NOT IN ('done','archived','completed')"
        " ORDER BY created_at").fetchall()
    con.close()
    by_pr = {}
    for tid, title, body, status, created in rows:
        m = VERDICT_PR_RE.search(title or "")
        if not m:
            continue
        by_pr.setdefault(m.group(1), []).append(
            {"id": tid, "title": title or "", "body": body or "",
             "status": status, "created": float(created or 0)})
    out = []
    for cards in by_pr.values():
        for a in cards:
            if not REWORK_GATE_RE.search(a["title"] + " " + a["body"]):
                continue
            covers = {}
            for b in cards:
                if b["id"] == a["id"] or not b["created"] < a["created"]:
                    continue
                if b["status"] == "scheduled":
                    # A parked card is not coverage: otherwise an item can
                    # move into a scheduled card and the merge stalls.
                    # Picking such a card up again is the job of the gate
                    # for owner-reviewed PRs; while the card is scheduled,
                    # the item stays in the verdict card.
                    continue
                for cname, _, cover_re, _ in VERDICT_COVER_CLASSES:
                    if cover_re.search(b["title"]) and cname not in covers:
                        covers[cname] = (b["id"], b["title"])
            if not covers:
                continue
            plan = _verdict_dedup_plan(a["title"], a["body"], covers)
            if plan:
                new_body, new_title, removed, ac_marked, refreshed = plan
                out.append({"tid": a["id"], "title": a["title"],
                            "status": a["status"], "new_body": new_body,
                            "new_title": new_title, "removed": removed,
                            "ac_marked": ac_marked, "refreshed": refreshed})
    return out


def verdict_dedup_gate(slug, apply=True):
    """Subtracts from verdict cards the items covered by live cards of the
    same PR. Acts immediately, like idempotency_gate.
    Idempotency: after the edit the item is gone from the body, so the scan
    no longer fires; the marker comment is not duplicated. Returns
    findings."""
    hits = verdict_dedup_scan(slug)
    if not hits:
        return []
    db = os.path.join(BOARDS, slug, "kanban.db")
    findings = []
    for h in hits:
        parts = ["item %s (%s) -> %s" % (n, c, cid)
                 for n, c, cid in h["removed"]]
        if h["ac_marked"]:
            parts.append("acceptance criteria: %d line(s) marked"
                         % h["ac_marked"])
        if h["refreshed"]:
            parts.append("earlier insert corrected: %d"
                         % h["refreshed"])
        summary = "; ".join(parts)
        text = ("    Verdict dedup: " + h["tid"] + "\n"
                "      " + (h["title"] or "")[:70] + "\n"
                "      " + summary + ".")
        if h["status"] not in ("ready", "todo", "blocked", "scheduled"):
            findings.append(("%s:verdict-dedup:%s" % (slug, h["tid"]), 1, (
                text + "\n      Status %s: the body is not edited under a "
                "live run, remove the item manually." % h["status"])))
            continue
        if not apply:
            findings.append(("%s:verdict-dedup:%s" % (slug, h["tid"]), 1,
                             text + " [dry-run: edit not performed]"))
            continue
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
        commented = con.execute(
            "SELECT 1 FROM task_comments WHERE task_id = ? AND body LIKE ?"
            " LIMIT 1", (h["tid"], VERDICT_DEDUP_MARKER + "%")).fetchone()
        con.close()
        cmd = [HERMES, "kanban", "--board", slug, "edit", h["tid"],
               "--body", h["new_body"]]
        if h["new_title"] != h["title"]:
            cmd += ["--title", h["new_title"]]
        run(cmd, timeout=90)
        if not commented:
            run([HERMES, "kanban", "--board", slug, "comment", h["tid"],
                 VERDICT_DEDUP_MARKER + " " + summary + ". What a live card "
                 "of the same PR covers has been subtracted: items replaced "
                 "by \"covers t_...\" lines, acceptance-criteria lines "
                 "marked.", "--author", "watchdog"],
                timeout=90)
        extra = ("\n      Title cleaned: " + h["new_title"]
                 if h["new_title"] != h["title"] else "")
        findings.append(("%s:verdict-dedup:%s" % (slug, h["tid"]), 1,
                         text + "\n      The gate subtracted the item from"
                         " the body." + extra))
    return findings


# === Merge unblocker: priority for a card that closes the last merge preconditions =
#
# Why: coder cards with minutes of work (WCFY, update-branch, green CI on a
# PR whose code the owner accepted) stood in ready for hours: both slots
# were taken by large blocks, and at equal priority the dispatcher takes the
# older card (ORDER BY priority DESC, created_at ASC in the core
# kanban_db_dispatch.py). Merges waited for formalities while the queue went
# to non-formal volume.
#
# Rule: a ready+coder card whose (1) body holds an owner verdict CHANGES
# REQUESTED: minor, (2) carries a mark of accepted code ("code accepted",
# "fix is accepted", "only merge prerequisites remain", APPROVED), (3) all
# blocking items are merge formalities (WCFY, update-branch, green CI,
# Before / After section, merge as a pair), (4) and whose PR has no live
# card with code or test work, is raised to p100. The dispatcher core is
# not touched: selection already goes by priority DESC.
#
# Two extensions of the rule:
# 1. The formality class is recognised by the card's work, not by a rework
#    title. [visual-delta] (the Before / After item of a verdict), WCFY
#    cards, update-branch and "green CI" carry the same formality but
#    without the verdict text in the body: acceptance of the code of the
#    same PR is looked up in verdict cards (any status) and in owner review
#    mirrors (comments author=github on the PR cards). Rebase cards of
#    pr-hygiene ("rebase PR #N onto ...") are not this class: pr-hygiene
#    manages their priority 96-99 itself.
# 2. p100, not p90: the last formality before merging a PR whose code the
#    owner accepted goes before any work on PRs that are not ready to
#    merge, including conflict-debt rebases (p96-p99). Numerically p100
#    equals review arms (assignee reviewer, I2); there is no overlap in the
#    coder queue: the dispatcher sorts by priority inside the pool of one
#    executor.

MERGE_UNBLOCKER_MARKER = "Watchdog: merge-unblocker priority."
# p100 (not p90): above conflict-debt rebases p96-p99, see the comment above.
MERGE_UNBLOCKER_PRIO = 100
# Mark of accepted code in the body of a verdict card. The APPROVED
# alternative is case-sensitive: "reviewer APPROVED" from a verdict is
# caught, while "round-1 approved" from a retelling of the card's origin is
# not.
MERGE_UNBLOCKER_ACCEPTED_RE = re.compile(
    r"(?i:code is accepted|code accepted|change (?:is )?accepted"
    r"|fix (?:itself )?is accepted"
    r"|only merge prerequisites remain)|\bAPPROVED\b")
# "change accepted" is the wording of owner review mirrors ("change
# accepted, two merge prerequisites block the merge"): the words code/fix
# are absent there.
# Classes of merge formalities: the first three match VERDICT_COVER_CLASSES,
# added are "base moved ahead" (a BLOCK without the word update-branch in
# the first line), "green CI" and "merge as a pair".
MERGE_UNBLOCKER_FORMAL_CLASSES = (
    VERDICT_COVER_CLASSES[0][:2],
    VERDICT_COVER_CLASSES[1][:2],
    VERDICT_COVER_CLASSES[2][:2],
    ("base moved ahead", re.compile(
        r"moved\s+\d+\s+commits?|\d+\s+commits?\s+past|ahead[_ ]?by",
        re.I)),
    ("green CI on the new head", re.compile(
        r"green\s+CI|green\s+run", re.I)),
    ("merge as a pair", re.compile(
        r"merges?\s+together|"
        r"merge\s+(?:together|pair)|pair\s+merge", re.I)),
)


# Title of a live card of the same PR with code/test work: verdict test
# blockers often have no blocking item in the body at all (the work is
# described in prose). visual-delta titles have no such words.
MERGE_UNBLOCKER_CODE_TITLE_RE = re.compile(
    r"\btests?\b|mutants?", re.I)

# Formality class by the card TITLE (not by a blocking item in the body).
# These are cards whose work is itself a merge formality: there is no
# verdict text in their body, the acceptance of the code is looked up by
# merge_unblocker_pr_accepted. \brebase\b is deliberately not among the
# classes: pr-hygiene rebase cards ("rebase PR #N onto ...", key
# coder-pr<N>-rebase-*) manage themselves (p96-p99) and the gate does not
# re-prioritise them.
MERGE_UNBLOCKER_CARD_CLASSES = (
    ("Before / After section (visual-delta)",
     re.compile(r"\[visual-delta\]", re.I)),
    ("What changes for you comment (WCFY)",
     re.compile(r"what\s+changes\s+for\s+you|\bWCFY\b", re.I)),
    ("update-branch", re.compile(r"update-?branch", re.I)),
    ("green CI on the new head",
     re.compile(r"green\s+CI|green\s+run", re.I)),
)
# Verdict context around the code acceptance: the accepted mark counts only
# from an owner-review verdict in ONE text with the mark. APPROVED is
# deliberately not part of the context: a foreign APPROVED quoted about
# another PR must not make this PR accepted.
MERGE_UNBLOCKER_CONTEXT_RE = re.compile(
    r"CHANGES\s+REQUESTED|only\s+merge\s+prerequisites\s+remain", re.I)


def _mu_item_class(text):
    """Formality class of a blocking item, or None (the item is code work)."""
    for name, rx in MERGE_UNBLOCKER_FORMAL_CLASSES:
        if rx.search(text or ""):
            return name
    return None


def merge_unblocker_card_class(title):
    """Formality class by card title, or None.

    [visual-delta], the watchdog's WCFY, update-branch, green CI: the work of
    the card is itself a merge formality, there is no verdict text in the
    body.
    """
    for name, rx in MERGE_UNBLOCKER_CARD_CLASSES:
        if rx.search(title or ""):
            return name
    return None


def merge_unblocker_pr_accepted(pr, rows, comments):
    """Whether the owner accepted the PR's code. Source label, or None.

    Looks in the cards of the same PR (any status: a verdict card may
    already be done) and in the comments on them (owner review mirrors are
    written by the gateway with author=github). Membership of a PR is the
    same as for neighbours in the scan: the number in the title, otherwise
    "#N" in the body. BOTH are needed in one text: the accepted-code mark
    (MERGE_UNBLOCKER_ACCEPTED_RE) and the verdict context
    (MERGE_UNBLOCKER_CONTEXT_RE). Example mirror: "CHANGES REQUESTED: minor
    ... change accepted, two merge prerequisites block the merge".
    """
    card_ids = set()
    texts = []
    for oid, otitle, obody, _ostatus in rows:
        om = VERDICT_PR_RE.search(otitle or "")
        if om:
            belongs = om.group(1) == pr
        else:
            belongs = ("#%s" % pr) in (obody or "")
        if belongs:
            card_ids.add(oid)
            texts.append("%s\n%s" % (otitle or "", obody or ""))
    for text in texts:
        if (MERGE_UNBLOCKER_ACCEPTED_RE.search(text)
                and MERGE_UNBLOCKER_CONTEXT_RE.search(text)):
            return "verdict in the PR card"
    for cid, author, cbody in comments:
        if cid not in card_ids:
            continue
        ctext = cbody or ""
        if (MERGE_UNBLOCKER_ACCEPTED_RE.search(ctext)
                and MERGE_UNBLOCKER_CONTEXT_RE.search(ctext)):
            return "owner review mirror (author=%s)" % author
    return None


def merge_unblocker_scan(slug):
    """ready+coder cards that close the last merge preconditions.

    A rework verdict card (criteria in the comment of the section above),
    plus cards of a formal work CLASS by title (merge_unblocker_card_class)
    whose PR code acceptance is confirmed by neighbours
    (merge_unblocker_pr_accepted). Returns a list of dict(tid, title, pr,
    prio, classes). "Last" is checked over the live (not done/archived)
    cards of the same PR: any card with a non-formal item (test, code)
    removes the candidacy (an open test blocker means the merge
    prerequisites are not the last ones).
    """
    db = os.path.join(BOARDS, slug, "kanban.db")
    if not os.path.exists(db):
        return []
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
    try:
        rows = list(con.execute(
            "SELECT id, title, body, status FROM tasks"))
        ready = list(con.execute(
            "SELECT id, title, body, priority FROM tasks"
            " WHERE status = 'ready' AND assignee = 'coder'"))
        # Owner review mirrors (author=github) on PR cards. Old fixture
        # boards have no comments table: no mirrors then.
        try:
            comments = list(con.execute(
                "SELECT task_id, author, body FROM task_comments"))
        except sqlite3.OperationalError:
            comments = []
    finally:
        con.close()
    live = {}
    live_status = {}
    for oid, otitle, obody, ostatus in rows:
        if ostatus not in ("done", "archived"):
            live[oid] = (otitle, obody)
        live_status[oid] = ostatus
    hits = []
    for tid, title, body, prio in ready:
        text = "%s\n%s" % (title or "", body or "")
        classes = None
        pr = None
        if (re.search(r"CHANGES\s+REQUESTED", text, re.I)
                and re.search(r"\bminor\b", text, re.I)):
            # path of a verdict card
            m = VERDICT_PR_RE.search(title or "")
            if m and MERGE_UNBLOCKER_ACCEPTED_RE.search(text):
                pr = m.group(1)
                items = verdict_blocking_items(body)
                if items:
                    classes = []
                    ok = True
                    for it in items:
                        cls = _mu_item_class(it["text"])
                        if cls is None:
                            ok = False
                            break
                        classes.append((it["num"], cls))
                    if not ok:
                        classes = None
                        pr = None
        if classes is None:
            # path of a work class: a formality by title, code acceptance
            # of the PR by a verdict in neighbours/mirrors. The candidate's
            # own blocking items, if any, must also be only formalities
            # (otherwise it is a card with code work and the title class
            # does not save it).
            cls = merge_unblocker_card_class(title)
            m = VERDICT_PR_RE.search(title or "")
            own_code = any(_mu_item_class(it["text"]) is None
                           for it in verdict_blocking_items(body))
            if cls and m and not own_code:
                pr = m.group(1)
                ev = merge_unblocker_pr_accepted(pr, rows, comments)
                if ev:
                    classes = [("-", "%s; %s" % (cls, ev))]
        if classes is None:
            continue
        code_left = False
        for oid, (otitle, obody) in live.items():
            if oid == tid:
                continue
            om = VERDICT_PR_RE.search(otitle or "")
            if om:
                belongs = om.group(1) == pr
            else:
                belongs = ("#%s" % pr) in (obody or "")
            if not belongs:
                continue
            # Active statuses (ready/running/todo): the work on the PR is
            # still to be done. Partners in review/scheduled/blocked are
            # past review rounds: their work is closed or parked, and the
            # last instance is the owner's last verdict (such cards must
            # not cancel the "code accepted" of the latest verdict).
            if live_status[oid] not in ("ready", "running", "todo"):
                continue
            for it in verdict_blocking_items(obody):
                if _mu_item_class(it["text"]) is None:
                    code_left = True
                    break
            if not code_left and MERGE_UNBLOCKER_CODE_TITLE_RE.search(
                    otitle or ""):
                code_left = True
            if code_left:
                break
        if code_left:
            continue
        hits.append({"tid": tid, "title": title, "pr": pr,
                     "prio": prio or 0, "classes": classes})
    return hits


def merge_unblocker_gate(slug, apply=True):
    """Raises merge-unblocker cards to p100.

    Acts immediately: edit --priority, then a marker comment for the audit.
    Idempotency: a card with p>=MERGE_UNBLOCKER_PRIO is no longer touched,
    a repeated pass is silent. Returns findings (key, rank, text).
    """
    hits = merge_unblocker_scan(slug)
    if not hits:
        return []
    findings = []
    for h in hits:
        if h["prio"] >= MERGE_UNBLOCKER_PRIO:
            continue
        classes = ", ".join("%s:%s" % (n, c) for n, c in h["classes"])
        text = ("    Merge-unblocker: " + h["tid"] + "\n"
                "      " + (h["title"] or "")[:70] + "\n"
                "      PR #%s: code accepted by the owner (minor verdict), "
                "items are only merge formalities (%s)." % (h["pr"], classes))
        if not apply:
            findings.append(("%s:merge-unblocker:%s" % (slug, h["tid"]), 1,
                             text + " [dry-run: priority not raised]"))
            continue
        run([HERMES, "kanban", "--board", slug, "edit", h["tid"],
             "--priority", str(MERGE_UNBLOCKER_PRIO)], timeout=90)
        run([HERMES, "kanban", "--board", slug, "comment", h["tid"],
             MERGE_UNBLOCKER_MARKER + " PR #%s is accepted by code, only "
             "merge formalities remain (%s). Priority p%d -> p%d: the card "
             "is taken before ordinary p80 work and before conflict-debt "
             "rebases p96-p99: the last formality before the merge of an "
             "accepted PR goes before any work on PRs that are not ready "
             "to merge." % (h["pr"], classes, h["prio"],
                            MERGE_UNBLOCKER_PRIO),
             "--author", "watchdog"], timeout=90)
        findings.append(("%s:merge-unblocker:%s" % (slug, h["tid"]), 1,
                         text + "\n      Priority raised: p%d -> p%d."
                         % (h["prio"], MERGE_UNBLOCKER_PRIO)))
    return findings


# === Rework of an open PR before new work of the band ======================
#
# Why: PRs stood with a CHANGES REQUESTED verdict while dispatcher slots
# went to cards that open new PRs: within one band the core orders by
# created_at (kanban_db_dispatch.py, ORDER BY priority DESC, created_at
# ASC), there is no stock way to order inside a priority, and new work
# created earlier overtakes the rework. Owner rule: finish before starting.
# Review (p100) and WCFY (p95) are already ordered by priority; rework of an
# open PR is raised by the same mechanism to p90 inside the active band
# (svc_priority default 90 in _rework_arm_card and in pr-hygiene
# create_rework_card). This gate raises cards that already exist (including
# reviewer fixes with keys reviewer-PRN-rework-verdict-*, which the arms did
# not create). Idempotent: p >= target is not touched, priority never moves
# down, the band of an inactive milestone (svc below the current one) is not
# touched. Frequent pass: --rework-gate-only (cron every 5 minutes), not a
# new cron.

REWORK_LIFT_PRIO = 90
REWORK_LIFT_MARKER = "Watchdog: rework priority of an open PR."
# A stale rework arm is archived, not raised. Why: a rework arm raised to
# p90 started when the PR already lived on a newer head with APPROVED, and
# the run was spent on work already done. A rework arm is built on the
# verdict of an EXACT head; its basis disappears when the head moves or a
# PASS arrives. Archive it: comment + archive (running cards are not
# touched, a mid-run directive in comments serves them).
REWORK_STALE_MARKER = "Watchdog: stale rework arm archived."
_REWORK_LIFT_KEY_RE = re.compile(r"rework", re.I)
_REWORK_LIFT_TITLE_RE = re.compile(
    r"rework (?:by|per|after) (?:the )?verdict"
    r"|^\s*(?:\[[^\]]*\]\s*)?fix PR #\d+", re.I)
_REWORK_HEAD_KEY_RE = re.compile(r"rework-([0-9a-f]{7,40})", re.I)
_REWORK_HEAD_TITLE_RE = re.compile(
    r"(?:head)[ :]*`?([0-9a-f]{7,40})", re.I)
# === Close gate "remaining: owner merge" ===================================
#
# One PR = one card: the executor card is not closed until the owner merges
# the PR; the wait goes through needs_input with the condition "Release
# condition: PR #N merged by the owner". The core has no done -> blocked
# transition, so the hold lives in this layer: the gate finds closed
# (done/archived) PR cards whose result reads like "remaining: owner merge"
# and whose PR is not merged yet, keeps a signal (a finding every pass) and
# leaves a comment with a checkable condition on the card. Why: a card was
# closed before its PR was merged and the WCFY had to be chased by hand.
OWNER_MERGE_LEFT_RE = re.compile(
    r"(?i)(?:remain(?:s|ing|ed)?|left)\W{0,10}(?:owner\W{0,10})?merge"
    r"|(?i:awaits?|waiting(?:\s+on)?)\W{0,10}(?:owner\W{0,10})?merge"
    r"|owner'?s?\W+merge|merge\W+by\W+(?:the\W+)?owner")
OWNER_MERGE_HOLD_MARKER = ("Watchdog: close gate (one PR = one"
                           " card).")
OWNER_MERGE_HOLD_WINDOW_S = 14 * 24 * 3600
# WCFY close gate: once review lives on the owner card, the WCFY step
# belongs to that card too. A card was closed with its PR open, an accept
# verdict on the head and no WCFY; the owner does not merge without WCFY
# (pr-deliverable), so the gate holds a signal on the closed card until a
# WCFY is posted on the head.
WCFY_HOLD_MARKER = ("Watchdog: WCFY close gate (WCFY after accept, one PR"
                    " = one card).")


def _closed_card_pr_numbers(result, title, summary=""):
    """PR number candidates of a closed card, most precise first: the form
    "PR #N" in the result, the title and the summary of the latest run, then
    any "#N" in the result and the title. Duplicates are dropped, order kept.

    A PR can be named only in the summary of the finishing run ("Closed the
    review cycle on PR #N") while the title carries the issue number and an
    already merged PR. Issue numbers are filtered by the gate itself: gh pr
    view fails on them, _pr_merged gives None and the candidate is skipped.
    """
    out = []

    def add(n):
        if n and n not in out:
            out.append(n)

    for text in ((result or ""), (title or ""), (summary or "")):
        m = _REWORK_PR_TITLE_RE.search(text)
        if m:
            add(int(m.group(1)))
    for text in ((result or ""), (title or "")):
        m = re.search(r"#(\d+)", text)
        if m:
            add(int(m.group(1)))
    return out


def _pr_merged(repo, number, memo):
    """True: the PR is merged or closed. A gh error -> None (the pass skips)."""
    if number in memo:
        return memo[number]
    memo[number] = None
    try:
        view = run(["gh", "pr", "view", str(number), "--repo", repo,
                    "--json", "state,mergedAt"], timeout=60)
        data = json.loads(view) or {}
        memo[number] = data.get("state") in ("MERGED", "CLOSED")
    except Exception:
        pass
    return memo[number]


def owner_merge_hold_gate(slug, now=None, apply=True):
    """Hold of closed PR cards whose result says "remaining: owner merge",
    and of closed PR cards without WCFY on an accepted head.

    One PR = one card: a PR card is not closed before the owner merges the
    PR. The gate scans recently closed cards (a 14-day window) whose result
    names "remaining: merge" and checks the PR: not merged -> a finding
    (owner class) plus a comment with a checkable release condition (dedup
    by marker). The CLI archive is terminal (there is no unarchive) and the
    status of a closed card cannot be returned, so the signal and the
    comment are held, not the status.

    Second branch (WCFY close gate): a closed card whose PR is open, has a
    reviewer accept verdict on the exact head and no WCFY newer than the
    head keeps a signal and a comment (marker WCFY_HOLD_MARKER). The PR
    number is also looked up in the summary of the finishing run.
    """
    repo = board_repo(slug)
    if not repo:
        return []
    now = now if now is not None else time.time()
    db = os.path.join(BOARDS, slug, "kanban.db")
    if not os.path.exists(db):
        return []
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
    try:
        rows = con.execute(
            "SELECT id, title, result, status, completed_at FROM tasks "
            "WHERE status IN ('done', 'archived') "
            "AND completed_at IS NOT NULL AND completed_at > ? "
            "AND result IS NOT NULL "
            "ORDER BY completed_at DESC",
            (now - OWNER_MERGE_HOLD_WINDOW_S,)).fetchall()
        findings = []
        memo = {}
        review_memo = {}
        for tid, title, result, status, completed_at in rows:
            # The PR can be named only in the summary of the finishing run.
            last_summary = (con.execute(
                "SELECT summary FROM task_runs WHERE task_id = ? AND "
                "summary IS NOT NULL ORDER BY id DESC LIMIT 1",
                (tid,)).fetchone() or ("",))[0] or ""
            if OWNER_MERGE_LEFT_RE.search(result):
                m = (re.search(r"#(\d+)", result)
                     or re.search(r"#(\d+)", title or ""))
                if not m:
                    continue
                number = int(m.group(1))
                merged = _pr_merged(repo, number, memo)
                if merged is None or merged:
                    continue
                findings.append((
                    "%s:owner-merge-hold:%s:%d" % (slug, tid, number), 1, (
                    "    Close gate: card %s (%s) was closed with the result "
                    "\"remaining: owner merge\", PR #%d is not merged yet "
                    "(completed %s). Release condition: PR #%d merged by the "
                    "owner."
                    % (tid, (title or "")[:60], number,
                       time.strftime("%Y-%m-%d %H:%MZ",
                                     time.gmtime(completed_at)), number))))
                if not apply:
                    continue
                seen = con.execute(
                    "SELECT COUNT(*) FROM task_comments WHERE task_id = ? "
                    "AND body LIKE ?",
                    (tid, OWNER_MERGE_HOLD_MARKER[:40] + "%")).fetchone()[0]
                if not seen:
                    run([HERMES, "kanban", "--board", slug, "comment", tid,
                         "%s the card was closed before PR #%d was merged. "
                         "Check the WCFY and the merge steps. Release "
                         "condition: PR #%d merged by the owner (one PR = one "
                         "card)." % (OWNER_MERGE_HOLD_MARKER, number, number)],
                        timeout=90)
                continue
            # WCFY close gate. The native completion contract checks only
            # the CI gates of the head and cannot express WCFY, so this gate
            # is extended instead of building a second watchdog.
            for number in _closed_card_pr_numbers(result, title,
                                                  last_summary):
                merged = _pr_merged(repo, number, memo)
                if merged is None or merged:
                    continue
                if number not in review_memo:
                    try:
                        review_memo[number] = _pr_review_state(repo, number)
                    except Exception:
                        review_memo[number] = None
                st = review_memo[number]
                if st is None or st.get("pr_state") != "OPEN":
                    continue
                if st.get("head_kind") != "accept":
                    continue
                wcfy_at = st.get("wcfy_at")
                head_at = st.get("head_commit_at")
                if wcfy_at is not None and (
                        head_at is None or wcfy_at >= head_at):
                    continue
                findings.append((
                    "%s:owner-wcfy-hold:%s:%d" % (slug, tid, number), 1, (
                    "    WCFY close gate: card %s (%s) is closed, PR #%d is "
                    "open with a reviewer accept verdict on head %s, but no "
                    "WCFY (## What changes for you) stands on that head. The "
                    "owner does not merge without WCFY (pr-deliverable). "
                    "Release condition: WCFY posted on the head of PR #%d, "
                    "or the PR merged or closed."
                    % (tid, (title or "")[:60], number,
                       (st.get("head") or "")[:7], number))))
                if not apply:
                    break
                seen = con.execute(
                    "SELECT COUNT(*) FROM task_comments WHERE task_id = ? "
                    "AND body LIKE ?",
                    (tid, WCFY_HOLD_MARKER[:40] + "%")).fetchone()[0]
                if not seen:
                    run([HERMES, "kanban", "--board", slug, "comment", tid,
                         "%s the card is closed, PR #%d is open with an "
                         "accept verdict on the head and no WCFY. The owner "
                         "does not merge without WCFY (pr-deliverable). "
                         "Release condition: WCFY on the head of PR #%d, or "
                         "the PR merged or closed."
                         % (WCFY_HOLD_MARKER, number, number)],
                        timeout=90)
                break
    finally:
        con.close()
    return findings


# PR number from a title. First the exact form "PR #N" (a rework card names
# its PR), then any #N as the last resort.
_REWORK_PR_TITLE_RE = re.compile(r"(?i)\bPR\s*#?(\d+)")


def rework_arm_head7(key, title, body=""):
    """head7 of the target of a rework arm: delegates to arm_head7."""
    return arm_head7(key, title, body)


# The head may be named only in the card body ("Rework PR #N (branch ...,
# head 1228c709)"), while neither the key nor the title carries it. Body
# forms: "head <sha>", "@ <sha>" (with an optional colon or backtick between
# the word and the sha).
_REWORK_HEAD_BODY_RES = (
    re.compile(r"(?:head)[ \t]*:?[ \t]*`?([0-9a-f]{7,40})", re.I),
    re.compile(r"@[ \t]*`?([0-9a-f]{7,40})", re.I),
)


def arm_head7(key, title, body=""):
    """Single helper for the head of an arm, for rework, review and WCFY.

    Order: key, title, body. The first match decides. None: the head is
    named nowhere, a comparison with the live head is impossible
    (conservatively, nothing is archived).
    """
    m = (_REWORK_HEAD_KEY_RE.search(key or "")
         or _REWORK_HEAD_TITLE_RE.search(title or ""))
    if m:
        return m.group(1)[:7].lower()
    for rx in _REWORK_HEAD_BODY_RES:
        m = rx.search(body or "")
        if m:
            return m.group(1)[:7].lower()
    return None


def _rework_arm_stale(slug, number, head7, memo):
    """Why the arm is stale, or None. memo caches _pr_review_state by PR
    number for one pass of the gate (one gh call per PR). A gh failure or a
    head not named in the card = not stale (conservative: archiving without
    a basis costs more than a redundant run)."""
    if not head7:
        return None
    repo = board_repo(slug)
    if not repo:
        return None
    if number not in memo:
        try:
            memo[number] = _pr_review_state(repo, number)
        except Exception:
            memo[number] = None
    st = memo[number]
    if not st:
        return None
    if st.get("pr_state") in ("MERGED", "CLOSED"):
        return ("PR #%d %s: the rework is no longer needed"
                % (number, st.get("pr_state")))
    live7 = (st.get("head") or "")[:7].lower()
    if live7 and live7 != head7:
        return ("head of PR #%d moved %s -> %s: the verdict the arm was "
                "built on lived on the old head; on the new one the review "
                "layer arms afresh if the CR repeats" % (number, head7, live7))
    if live7 == head7 and st.get("head_kind") == "accept":
        return ("PASS on the same head %s: the rework is already accepted"
                % head7)
    return None


REWORK_DONE_ELSEWHERE_MARKER = (
    "Watchdog: rework done by another run.")


def _card_claims_head(db, tid, head7):
    """Does the card claim head head7 in its own text?

    The card's own push must differ from somebody else's: a run pushed a
    commit and left a comment/summary with that sha; archiving on "the head
    moved" would kill a live delivery cycle (the CI snapshot and
    request_review are planned inside the card itself). A foreign push is
    not named by the card: neither in its last comments, nor in run
    summaries, nor in the reason of a scheduled parking.
    """
    if not head7:
        return False
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
    except Exception:
        return False
    parts = []
    try:
        parts += [r[0] or "" for r in con.execute(
            "SELECT body FROM task_comments WHERE task_id=? "
            "ORDER BY id DESC LIMIT 5", (tid,))]
        parts += [r[0] or "" for r in con.execute(
            "SELECT summary FROM task_runs WHERE task_id=? "
            "AND summary IS NOT NULL ORDER BY id DESC LIMIT 2", (tid,))]
        parts += [r[0] or "" for r in con.execute(
            "SELECT payload FROM task_events WHERE task_id=? "
            "AND kind='scheduled' ORDER BY id DESC LIMIT 2", (tid,))]
    except Exception:
        parts = []
    finally:
        con.close()
    return head7 in " ".join(parts).lower()


def _rework_done_elsewhere(slug, number, created_at, memo):
    """The rework for this verdict was done by another run.

    The head is named neither in the key, nor in the title, nor in the body
    (the stale check cannot see such a card). Archive it if:
      - the PR is open and its head changed AFTER the card was created
        (a commit),
      - the head commit belongs to the PR executor (the branch author),
      - the PR holds an answer to the reject verdict (a non-verdict comment
        after it and after the card was created: "## Rework: ... round N").
    The card's own push (the card names the live head itself) is checked by
    the caller through _card_claims_head: that is its own delivery cycle,
    not a duplicate. Returns the reason or None (a gh failure =
    conservatively None).
    """
    repo = board_repo(slug)
    if not repo:
        return None
    if number not in memo:
        try:
            memo[number] = _pr_review_state(repo, number)
        except Exception:
            memo[number] = None
    st = memo[number]
    if not st or st.get("pr_state") != "OPEN":
        return None
    head_at = st.get("head_commit_at")
    if not head_at or not created_at or head_at <= created_at:
        return None
    if not st.get("head_by_pr_author"):
        return None
    reply = st.get("reply_after_reject")
    if not reply or reply < created_at:
        return None
    live7 = (st.get("head") or "")[:7].lower()
    return ("head of PR #%d moved to an executor commit AFTER the card was "
            "created (created %s, head now %s), the PR holds an answer to "
            "the verdict: the rework for this verdict was done by another run"
            % (number,
               time.strftime("%m-%d %H:%MZ", time.gmtime(created_at)),
               live7))


def _rework_by_structure(slug, number, created_at, memo):
    """Rework of an open PR recognised by structure, not by words.

    Why: word lists ("rework" in the key, "rework by verdict" / "Fix PR #N"
    in the title) missed the very first new spelling of a rework card; the
    same disease hit the service-wait vocabulary before.

    Structural signs of a rework (they decide; words are only a hint):
      1. the card is bound to an OPEN PR (number in the key or the title):
         gh pr view must resolve the number to OPEN. An issue number
         resolves to None (it is not a PR), so a card of new work on an
         issue is not raised;
      2. the CURRENT head of this PR carries a reject verdict (CHANGES
         REQUESTED / RETURN): head_kind from _pr_review_state, strictly
         bound to the head. PASS or no verdict = not a rework;
      3. the card was created AFTER the verdict (created_at >=
         head_reject_at, the time of the FIRST reject on the head: a later
         duplicate of the same RETURN does not disqualify the card): work
         created before the verdict is not an answer to it.

    "Does not start a new PR": a rework card does not open its own PR, it
    names an existing open PR with a verdict on its head.

    memo caches _pr_review_state by number for one pass of the gate (one gh
    call per PR, shared with _rework_arm_stale). Returns an evidence string
    (why this is a rework) or None.
    """
    repo = board_repo(slug)
    if not repo:
        return None
    if number not in memo:
        try:
            memo[number] = _pr_review_state(repo, number)
        except Exception:
            memo[number] = None
    st = memo[number]
    if not st:
        return None
    if st.get("pr_state") != "OPEN":
        return None
    if st.get("head_kind") != "reject":
        return None
    at = st.get("head_reject_at") or st.get("head_at")
    if created_at and at and created_at < at:
        return None
    return ("open PR #%d, reject verdict on the current head %s, "
            "card created after the verdict"
            % (number, (st.get("head") or "")[:7]))


def rework_priority_lift(slug, apply=True):
    """Archive stale rework arms and lift live ones of an open PR to p90.

    Archiving sees ALL statuses except running (a blocked arm on a head that
    already moved used to stay as a live tail through a head change, and
    the hourly pass did not touch it). The priority lift stays on
    ready/todo.

    Card class: by STRUCTURE (_rework_by_structure: an open PR with a reject
    verdict on the current head, card created after the verdict) or by a
    hint word: a key with rework (coder-prN-rework-*,
    reviewer-PRN-rework-verdict-*) or a title "rework by verdict" / "Fix PR
    #N". Words stay a hint so that a worded card does not lose its priority
    when gh fails (conservative). PR number from the key (more reliable),
    otherwise the first #N of the title. Target = svc_priority(slug, number,
    90): active band 90, inactive exact band, no data 90. Only a priority
    below the target is edited.

    Before the lift an arm with a named head is compared with the live head
    of the PR. The head moved, PASS on the same head, or the PR is not OPEN:
    the card is archived (comment + archive), its priority is not raised.
    Arms in any status except running are archived.
    Returns findings (key, rank, text).
    """
    db = os.path.join(BOARDS, slug, "kanban.db")
    if not os.path.exists(db):
        return []
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
    rows = list(con.execute(
        "SELECT id, title, body, priority, idempotency_key, created_at,"
        " status FROM tasks WHERE status NOT IN"
        " ('done','archived','running')"
        " AND assignee = 'coder'"))
    con.close()
    findings = []
    memo = {}
    for tid, title, body, prio, key, created_at, st in rows:
        title = title or ""
        body = body or ""
        key = key or ""
        # Structure decides, words are only a hint.
        word_hint = bool(_REWORK_LIFT_KEY_RE.search(key)
                         or _REWORK_LIFT_TITLE_RE.search(title))
        structural = None
        if not word_hint:
            m_num = (re.search(r"[Pp][Rr](\d+)", key)
                     or _REWORK_PR_TITLE_RE.search(title)
                     or re.search(r"#(\d+)", title))
            if not m_num:
                continue
            structural = _rework_by_structure(
                slug, int(m_num.group(1)), created_at, memo)
            if not structural:
                continue
        m = re.search(r"[Pp][Rr](\d+)", key) or re.search(r"#(\d+)", title)
        if not m:
            continue
        number = int(m.group(1))
        # A stale arm is archived before the lift. The head is also looked
        # up in the card body (the single helper arm_head7), and a live head
        # claimed by the card's own run (comment / summary / scheduled
        # reason with that sha) is not archived: that is its own delivery
        # cycle, not a foreign duplicate.
        head7_card = rework_arm_head7(key, title, body)
        why_stale = _rework_arm_stale(slug, number, head7_card, memo)
        live_head7 = ((memo.get(number) or {}).get("head") or "")[:7].lower()
        if why_stale and live_head7 \
                and _card_claims_head(db, tid, live_head7):
            why_stale = None
        if not why_stale and not head7_card:
            # The head is named nowhere, but the work on the verdict was
            # already done by another run (the head moved by an executor
            # commit after the card was created, the PR holds an answer to
            # the verdict, the card did not claim the push as its own).
            why_stale = _rework_done_elsewhere(slug, number, created_at,
                                               memo)
            if why_stale and live_head7 \
                    and _card_claims_head(db, tid, live_head7):
                why_stale = None
        if why_stale and why_stale.startswith("head of PR #"):
            marker = (REWORK_DONE_ELSEWHERE_MARKER
                      if "another run" in why_stale
                      else REWORK_STALE_MARKER)
        else:
            marker = REWORK_STALE_MARKER
        if why_stale:
            text = ("    Stale rework arm: " + tid + "\n"
                    "      " + title[:70] + "\n"
                    "      " + why_stale + ".")
            if not apply:
                findings.append(("%s:rework-stale:%s" % (slug, tid), 1,
                                 text + " [dry-run: not archived]"))
                continue
            run([HERMES, "kanban", "--board", slug, "comment", tid,
                 marker + " " + why_stale + ". The basis of the arm is "
                 "gone: the watchdog archives the card, its priority is not "
                 "raised. If the PR needs a fresh fix, the review layer "
                 "arms a new card on the current head.",
                 "--author", "watchdog"], timeout=90)
            run([HERMES, "kanban", "--board", slug, "archive", tid],
                timeout=90)
            findings.append(("%s:rework-stale:%s" % (slug, tid), 1,
                             text + "\n      Archived: comment + archive."))
            continue
        target = svc_priority(slug, number, REWORK_LIFT_PRIO)
        if (prio or 0) >= target:
            continue
        # The lift stays on ready/todo. blocked/review are scanned only to
        # archive stale arms above; raising the priority of a card that does
        # not start is pointless.
        if st not in ("ready", "todo"):
            continue
        text = ("    Rework of an open PR: " + tid + "\n"
                "      " + title[:70] + "\n"
                + (("      Sign (structure): " + structural
                    + ".\n") if structural else "")
                + ("      PR #%d, target p%d (band of the active milestone)."
                   % (number, target)))
        if not apply:
            findings.append(("%s:rework-lift:%s" % (slug, tid), 1,
                             text + " [dry-run: priority not raised]"))
            continue
        run([HERMES, "kanban", "--board", slug, "edit", tid,
             "--priority", str(target)], timeout=90)
        run([HERMES, "kanban", "--board", slug, "comment", tid,
             REWORK_LIFT_MARKER + " PR #%d is open with a verdict, this card"
             " closes it before the band starts new PRs. Priority"
             " p%d -> p%d: below review p100 and WCFY p95, above new work of"
             " the band p80 (finish before starting)."
             % (number, prio or 0, target), "--author", "watchdog"],
            timeout=90)
        findings.append(("%s:rework-lift:%s" % (slug, tid), 1,
                         text + "\n      Priority raised: p%d -> p%d."
                         % (prio or 0, target)))
    return findings


# A live review arm in ready/todo is retired when the same PR+head already
# has a stock review lane or a verdict on the exact head. Race it closes: the
# watchdog arms a p100 card on an honest snapshot (no lane yet), seconds later
# the executor requests review and the stock lane starts, while the arm waits
# in ready for a free slot to run a SECOND review of the same head. The
# create-time checks are honest only at the moment of create; nothing covered
# the class "arm still in ready, stock lane already working". Same class as
# retiring stale rework arms: a continuous rule in the 5-minute
# --rework-gate-only pass, not a new cron. running is not touched: the run is
# already paid for and its verdict is required anyway.
REVIEW_ARM_STALE_MARKER = "Watchdog: review arm retired."
_REVIEW_ARM_KEY_RE = re.compile(
    r"^reviewer-pr(\d+)-([0-9a-f]{7,40})(?:-[rp]\d+)?$", re.I)

# Arms on a head that has MOVED are retired too: an arm created on an honest
# snapshot while the executor keeps pushing waits for a slot to review a head
# that no longer exists. The stale-rework retirement (head moved = no reason
# left), extended to every status except running, is shared by review and
# WCFY arms.
WCFY_ARM_STALE_MARKER = "Watchdog: WCFY arm retired."
_WCFY_ARM_KEY_RE = re.compile(
    r"^coder-pr(\d+)-wcfy-([0-9a-f]{7,40})(?:-r\d+)?$", re.I)


def review_arm_liveness_gate(slug, apply=True):
    """Live automatic review and WCFY arms are retired when their reason is gone.

    Card class: a review arm (assignee reviewer, key
    reviewer-pr<N>-<head7> with -rN/-pN suffixes) and a WCFY arm (assignee
    coder, key coder-pr<N>-wcfy-<head7>[-rN]). Retirement sees ALL statuses
    except running (the run is paid for, its verdict is required anyway).
    Terminal statuses (done/completed/archived) are not scanned.

    Reasons to retire a review arm:
      1. the same PR+head already has a stock review lane
         (_review_cycle_live: a card in review, or running with claim
         source_status=review): the verdict comes through the stock channel;
      2. the EXACT head already has a verdict (fresh _pr_review_state, the
         verdict field is strictly bound to the head);
      3. the PR head moved: the arm stands on a head that is no longer the
         PR head. The review layer arms the final head once the executor
         delivers or leaves the board;
      4. the PR is not OPEN (MERGED/CLOSED): reviewing the head is pointless;
      5. the PR is CONFLICTING: the merge ref is not computed, no
         pull_request CI run is created at all, a PASS on this head is
         impossible; after the rebase the head changes and the verdict
         would be stale.
    Reasons to retire a WCFY arm:
      1. the head moved (same mechanism as for the review arm);
      2. the PR is CLOSED without a merge: MERGED does not retire, a WCFY
         after a merge still makes sense (the comment for users lives in
         the merged branch too);
      3. a fresh WCFY already stands on the EXACT head (wcfy_at not older
         than the head): another channel did the card's work;
      4. the PR is CONFLICTING: a PASS on such a head is impossible (no CI
         run), so a WCFY "after PASS" cannot appear before the rebase that
         changes the head.
    A gh failure = conservatively not retired (retiring without a reason
    costs more than an extra run). Returns findings (key, rank, text).
    """
    db = os.path.join(BOARDS, slug, "kanban.db")
    if not os.path.exists(db):
        return []
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
    rows = list(con.execute(
        "SELECT id, title, body, idempotency_key, status FROM tasks"
        " WHERE status NOT IN ('done','completed','archived','running')"
        " AND ((assignee = 'reviewer'"
        "       AND idempotency_key LIKE 'reviewer-pr%')"
        "      OR (assignee = 'coder'"
        "          AND idempotency_key LIKE 'coder-pr%'))"))
    con.close()
    findings = []
    repo = None
    memo = {}
    for tid, title, body, key, status in rows:
        key = key or ""
        m_rev = _REVIEW_ARM_KEY_RE.match(key)
        m_wcfy = None if m_rev else _WCFY_ARM_KEY_RE.match(key)
        if m_rev or m_wcfy:
            m = m_rev or m_wcfy
            number, head7 = int(m.group(1)), m.group(2)[:7].lower()
        else:
            # A key without a head (reviewer-prN, coder-prN-wcfy-N without a
            # sha): the head is taken from the title/body by the same helper
            # arm_head7 used for rework (the head may be named only in the
            # body). No head found = conservatively not touched. Non-arm keys
            # (rework and others) are not classified: the class is set by the
            # prefix reviewer-prN / coder-prN-wcfy.
            m_rev = (re.match(r"reviewer-pr(\d+)", key, re.I)
                     if key.lower().startswith("reviewer-pr") else None)
            m_wcfy = (re.match(r"coder-pr(\d+)-wcfy", key, re.I)
                      if m_rev is None else None)
            if not m_rev and not m_wcfy:
                continue
            head7 = arm_head7("", title or "", body or "")
            if not head7:
                continue
            number = int((m_rev or m_wcfy).group(1))
        why = None
        tail = None
        if m_rev:
            tail = ("The arm's reason disappeared after create (the stock"
                    " lane started or a verdict landed on the head): the"
                    " watchdog archives the card, the review of this head"
                    " goes through the stock channel.")
            busy = _review_cycle_live(slug, number, head7)
            if busy:
                why = ("card %s already carries the stock review cycle (%s)"
                       " on PR #%d %s: the verdict comes through the stock"
                       " lane, a second run on the same head is a duplicate"
                       " review"
                       % (busy[0][0], busy[0][1], number, head7))
        else:
            tail = ("The arm's reason disappeared after create: the"
                    " watchdog archives the card.")
        if why is None:
            if repo is None:
                repo = board_repo(slug)
            if repo and number not in memo:
                try:
                    memo[number] = _pr_review_state(repo, number)
                except Exception:
                    memo[number] = None
            st = memo.get(number)
            if st is None:
                continue   # gh failure: conservatively not retired
            live_head7 = (st.get("head") or "")[:7].lower()
            if live_head7 and live_head7 != head7:
                why = ("the head of PR #%d moved: the arm stands on head %s,"
                       " the live head is %s (the executor keeps pushing or"
                       " the history was rewritten). A head that no longer"
                       " exists carries no verdict: the review layer arms"
                       " the final head once the PR work is delivered"
                       % (number, head7, live_head7))
            elif (st.get("mergeable") or "") == "CONFLICTING":
                # A CONFLICTING head can neither merge nor get a CI run: a
                # verdict on it would be preliminary and would not count as
                # PASS.
                why = ("PR #%d is CONFLICTING: the merge ref is not computed,"
                       " no pull_request CI run is created, a PASS on head"
                       " %s is impossible; after the rebase the head changes"
                       " and the verdict would be stale"
                       % (number, head7))
                tail = ("A head under rebase is not confirmed by CI: the"
                        " watchdog archives the card, the review comes back"
                        " as an arm on the fresh head.")
            elif m_wcfy:
                if (st.get("pr_state") or "") == "CLOSED":
                    why = ("PR #%d is closed without a merge: no WCFY is"
                           " needed on its head" % number)
                elif st.get("wcfy_at") is not None \
                        and st.get("age") is not None \
                        and st["wcfy_at"] >= time.time() - st["age"]:
                    why = ("the exact head %s of PR #%d already has a fresh"
                           " \"What changes for you\""
                           % (head7, number))
            else:
                if st.get("verdict"):
                    why = ("the exact head %s of PR #%d already has a verdict"
                           % (head7, number))
                elif (st.get("pr_state") or "") in ("MERGED", "CLOSED"):
                    why = ("PR #%d %s: reviewing the head is no longer"
                           " needed" % (number, st.get("pr_state")))
                    tail = ("The PR left OPEN: the watchdog archives the"
                            " card.")
        if not why:
            continue
        marker = WCFY_ARM_STALE_MARKER if m_wcfy else REVIEW_ARM_STALE_MARKER
        text = ("    %s arm retired: %s\n"
                "      %s [%s]\n"
                "      %s."
                % ("WCFY" if m_wcfy else "Review", tid,
                   (title or "")[:70], status, why))
        if not apply:
            findings.append(("%s:rev-arm-stale:%s" % (slug, tid), 1,
                             text + " [dry-run: not retired]"))
            continue
        run([HERMES, "kanban", "--board", slug, "comment", tid,
             marker + " " + why + ". " + tail,
             "--author", "watchdog"], timeout=90)
        run([HERMES, "kanban", "--board", slug, "archive", tid],
            timeout=90)
        findings.append(("%s:rev-arm-stale:%s" % (slug, tid), 1,
                         text + "\n      Retired: comment + archive."))
    return findings


# === Board invariants I1-I6 ================================================
#
# The board kept breaking in the same ways, and the owner fixed it by hand
# each time: rework under a blocked parent, an idempotency gate that held
# cards for a day, both slots taken by p0/p40 cards while reviews were
# waiting. The lint runs hourly together with the watchdog (the cron job is
# already there): unambiguous violations are fixed immediately and confirmed
# by re-reading the database, unfixable ones give one line to the owner chat.
#
#   I1. rework / twin port / re-review are never children of a card in
#       blocked/gave_up: the link is removed automatically, with a comment
#       on both cards;
#   I2. a reviewer card for an open PR = p100, rework from an owner review
#       is not below p80: below the threshold it is raised automatically;
#   I3. while the ready layer with p>=80 is non-empty, no card with p<50
#       starts: the offender is moved to todo with a comment until the layer
#       is empty;
#   I4. ready without an assignee or with a dead profile for over 30 min:
#       alert. The own detector was removed: the invariant is covered by the
#       built-in diagnostic stranded_in_ready (kanban_diagnostics.py), which
#       the watchdog already delivers through diagnostics();
#   I5. a card in a non-standard status (completed and the like) for over an
#       hour: alert, fix by hand (move todo/done/archived);
#   I6. a live verify/port/force-push/re-review card whose parents are ALL
#       archived (the parent went to the archive, not to done) was released
#       by mistake: the archive closed the links, the work was absorbed by a
#       successor card, and the successor is not linked. It is blocked
#       needs_input with a comment "parent is archived, assign a successor
#       with a link".

LINT_STATUSES = frozenset(("triage", "todo", "scheduled", "ready", "running",
                           "blocked", "review", "done", "archived"))
LINT_REVIEWER_PRIO = 100       # I2: reviewer card for an open PR
LINT_REWORK_MIN_PRIO = 80      # I2: rework from an owner review
LINT_HIGH_PRIO = 80            # I3: layer that has priority on slots
LINT_START_FLOOR = 50          # I3: below this, start is forbidden while the layer is non-empty
LINT_NONSTANDARD_AFTER = 3600  # I5: non-standard status older than an hour
LINT_KEY_PR_RE = re.compile(
    r"^(?:reviewer-pr(\d+)|owner-review-#?(\d+)|rebase-(\d+)|coder-pr(\d+))",
    re.IGNORECASE)
LINT_REWORK_TITLE_RE = re.compile(r"\brework\b", re.IGNORECASE)
LINT_TWIN_TITLE_RE = re.compile(r"\btwin\b|\bport\b", re.IGNORECASE)
# The rebase kind: key conventions coder-pr<N>-rebase-<sha7> (machine) and
# rebase-<N>-<sha7> (worker), the word rebase in a title ("Rebase PR #224
# onto..."). "verify rebased" and "force-push rebased" do not match: \b
# after rebase prevents it.
LINT_REBASE_KEY_RE = re.compile(
    r"^(?:coder-pr\d+-rebase-[0-9a-f]{7,40}|rebase-\d+-[0-9a-f]{7,40})$",
    re.IGNORECASE)
LINT_REBASE_TITLE_RE = re.compile(r"\brebase\b", re.IGNORECASE)
# I6: downstream cards of a chain whose premise lives in the parent.
# verify: decomposition children of the tester ("Adversarially verify
# rebased #224..."); force-push: "Force-push rebased #224 and update PR
# comment"; port: the same twin/port marker as in lint_card_kind;
# re-review: key reviewer-pr<N>.
LINT_I6_VERIFY_RE = re.compile(r"\bverif", re.IGNORECASE)
LINT_I6_FORCEPUSH_RE = re.compile(r"\bforce[- ]push", re.IGNORECASE)
LINT_STATUS_EVENT_KINDS = ("created", "promoted", "promoted_manual", "blocked",
                           "unblocked", "scheduled", "archived", "completed",
                           "edited", "gave_up", "changes_requested", "unlinked")


# === Rework for a verdict lives in the verdict PR's branch =================
#
# An owner verdict on a PR (CHANGES REQUESTED: major) can be split into
# several child cards, and two of them may run in parallel. If a block
# delivers its work not into the PR's branch but into a new PR "stacked on"
# the first, the next block does the same. The result is a chain of 3-4 PRs
# instead of one PR with one review, one Before/After section and one merge,
# each with its own review, verdict and merge order.
#
# The owner's rule: rework for a verdict on a PR is committed to the branch
# of THAT PR. Several blocks of one verdict run sequentially (chain
# dependency), otherwise they have nowhere to push without conflict. A new
# PR only by an explicit owner decision, recorded as a stack-owner-approve
# line in the PR body.
#
# The gate looks at the live PRs of the board's repo: if the head of an open
# PR is an ancestor of the head of another open PR with the same base
# branch (a virtual stack: the base may then stand on a long-lived branch),
# this is a stack without an owner decision. Action: an English marker
# comment on the PR (the worker and the owner see it where they worked),
# the finding goes to the owner. Idempotency: while the marker is there,
# repeated passes are silent. gh or git errors do not bring down the other
# checks: the gate is silent until the next pass.

STACKED_REWORK_MARKER = "watchdog: stacked-rework"
STACK_OWNER_APPROVE_RE = re.compile(r"stack-owner-approve", re.I)


# A stuck active_pr guard on a ready card after an owner decision. The core
# (kanban_db_dispatch.py, _respawn_blocker, case 4) holds a card while a
# fresh comment has a PR link, and lifts the guard only with a handoff event
# (assigned to ANOTHER profile, changes_requested, review_reopened) strictly
# AFTER the last PR comment. An unblock and an owner decision in the PR do
# not lift the guard: the dispatcher then writes respawn_guarded active_pr
# every ~2 minutes with empty slots until a manual reassign
# coder->architect->coder. The watchdog repeats this reassign automatically,
# but only when a human decision is visible after the PR comment: an
# unblocked event or a foreign (not the assignee's) comment with an owner
# decision/verdict (handle <owner> from board_repo). Without a decision the
# card is not touched: there the guard against a duplicate PR is right, and
# the worker must not run against its own PR.
#
# Second reason: a PR link in a card comment can be left by ANOTHER profile
# (reviewer verdict, a github-role mirror, an architect final). The core
# checks only the presence of a URL, not the author: a never-run card (no
# runs except the scheduler's scheduled stubs) hangs under active_pr while
# the link is within the 24h window. Rule: a PR link in a comment of a
# foreign profile does not mean the card's work is done. If the guard holds
# for more than two ticks in a row (>= 3) a card with zero own runs and the
# last PR comment was written by not the assignee, the watchdog lifts the
# guard with the same built-in bounce reassign. A card with its own run or
# with a link from the executor itself is NOT touched.
ACTIVE_PR_STUCK_TICKS = 12      # respawn_guarded ticks, ~25 min at a ~2 min tick
ACTIVE_PR_FOREIGN_TICKS = 3     # more than 2 ticks in a row for a foreign link
ACTIVE_PR_GUARD_WINDOW_S = 3 * 3600
ACTIVE_PR_PR_WINDOW_S = 24 * 3600       # mirror of the core's _RESPAWN_GUARD_PR_WINDOW
ACTIVE_PR_PR_URL_RE = re.compile(
    r"https?://github\.com/[^/\s]+/[^/\s]+/pull/\d+", re.IGNORECASE)
ACTIVE_PR_HANDOFF_KINDS = ("assigned", "changes_requested", "review_reopened")
ACTIVE_PR_BOUNCE_ORDER = ("architect", "reviewer", "tester", "ops", "coder")


def active_pr_handoff_gate(slug, apply=True):
    """Gate for a stuck active_pr guard.

    A ready card that the dispatcher holds with respawn_guarded(active_pr)
    for N ticks while a live human decision exists AFTER the PR comment gets
    a handoff event (reassign through another profile and back, the same
    move the owner uses by hand) and a reason comment with the prefix
    "Watchdog:". A card without a decision after the PR comment is not
    touched. Idempotency without state: after the reassign the database has
    an assigned event after the PR comment, so the next pass is silent.
    Timestamps are filtered by the parsed epoch: a TEXT stamp in the
    database (written outside the mutators) is always "greater" in an SQL
    comparison, as in the core.

    A second reason to lift the guard: the last PR comment was written by
    ANOTHER profile (not the assignee) and the card has no own run (runs
    other than the scheduler's scheduled stubs). A duplicate PR is then
    physically impossible and the guard holds for nothing; threshold
    ACTIVE_PR_FOREIGN_TICKS ticks in a row. The action is the same built-in
    one: bounce reassign + a "Watchdog:" comment without a URL (a URL would
    re-arm the guard).
    Returns findings (key, rank, text)."""
    db = os.path.join(BOARDS, slug, "kanban.db")
    if not os.path.exists(db):
        return []
    now = time.time()

    def _epoch(value):
        # Mirror of _kb._to_epoch: int/float/numeric string/ISO-8601,
        # otherwise None. An ISO stamp (written outside the mutators) is not
        # converted by INTEGER affinity, and the SQL window created_at > ?
        # always lets it through (TEXT is greater than INTEGER): only this
        # check decides the time.
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return int(value)
        s = str(value).strip()
        if not s:
            return None
        try:
            return int(s)
        except ValueError:
            pass
        try:
            return int(datetime.fromisoformat(s).timestamp())
        except (ValueError, OSError):
            return None

    cands = []
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
    try:
        rows = con.execute(
            "SELECT DISTINCT e.task_id FROM task_events e "
            "JOIN tasks t ON t.id = e.task_id "
            "WHERE e.kind = 'respawn_guarded' AND e.created_at > ? "
            "AND e.payload LIKE '%active_pr%' AND t.status = 'ready' "
            "AND t.assignee IS NOT NULL AND t.assignee != '' "
            "AND t.claim_lock IS NULL",
            (now - ACTIVE_PR_GUARD_WINDOW_S,)).fetchall()
        for (tid,) in rows:
            title, assignee = con.execute(
                "SELECT title, assignee FROM tasks WHERE id = ?",
                (tid,)).fetchone()
            guard_count = 0
            for (t,) in con.execute(
                    "SELECT created_at FROM task_events WHERE task_id = ? "
                    "AND kind = 'respawn_guarded' "
                    "AND payload LIKE '%active_pr%' AND created_at > ?",
                    (tid, now - ACTIVE_PR_GUARD_WINDOW_S)):
                x = _epoch(t)
                if x and x > now - ACTIVE_PR_GUARD_WINDOW_S:
                    guard_count += 1
            if guard_count < ACTIVE_PR_FOREIGN_TICKS:
                continue     # a guard younger than three ticks is not stuck
            # We need the author of the NEWEST comment with a PR URL. A link
            # from a foreign profile (not the assignee) with zero own runs
            # of the card is not a protection against a duplicate PR but a
            # lock: a reviewer verdict on a PR can hold a never-run card
            # for a hundred ticks with empty slots.
            pr_rows = []
            for t, body, author in con.execute(
                    "SELECT created_at, body, author FROM task_comments "
                    "WHERE task_id = ? AND created_at > ?",
                    (tid, now - ACTIVE_PR_PR_WINDOW_S)):
                x = _epoch(t)
                if x and x > now - ACTIVE_PR_PR_WINDOW_S and \
                        ACTIVE_PR_PR_URL_RE.search(body or ""):
                    pr_rows.append((x, author))
            if not pr_rows:
                continue   # the PR comment fell out of the core's window: the guard will go by itself
            pr_time, pr_author = max(pr_rows, key=lambda r: r[0])
            handoff = 0
            for (t,) in con.execute(
                    "SELECT created_at FROM task_events WHERE task_id = ? "
                    "AND kind IN ('assigned', 'changes_requested', "
                    "'review_reopened') AND created_at > ?", (tid, pr_time)):
                x = _epoch(t)
                if x and x > pr_time:
                    handoff += 1
            if handoff:
                continue             # the guard is already lifted by a handoff event
            unblocked = 0
            for (t,) in con.execute(
                    "SELECT created_at FROM task_events WHERE task_id = ? "
                    "AND kind = 'unblocked' AND created_at > ?",
                    (tid, pr_time)):
                x = _epoch(t)
                if x and x > pr_time:
                    unblocked += 1
            owner = (board_repo(slug) or "").split("/")[0]
            owner_decided = 0
            if owner:
                for t, body in con.execute(
                        "SELECT created_at, body FROM task_comments "
                        "WHERE task_id = ? AND created_at > ? "
                        "AND author != ?", (tid, pr_time, assignee)):
                    x = _epoch(t)
                    if x and x > pr_time and owner in (body or ""):
                        owner_decided += 1
            # A foreign PR link with zero own runs. An own run = anything
            # except the scheduler's scheduled stubs (a card whose only run
            # has outcome='scheduled' has no real work). No runs table (old
            # boards) = unknown = fail closed, the branch is silent.
            foreign = None
            if pr_author and pr_author != assignee:
                try:
                    real_runs = con.execute(
                        "SELECT COUNT(*) FROM task_runs WHERE task_id = ? "
                        "AND (outcome IS NULL OR outcome != 'scheduled')",
                        (tid,)).fetchone()[0]
                except sqlite3.OperationalError:
                    real_runs = 1
                if not real_runs:
                    foreign = pr_author
            if not foreign:
                if guard_count < ACTIVE_PR_STUCK_TICKS:
                    continue            # a young guard: the dispatcher holds it for a reason
                if not unblocked and not owner_decided:
                    continue   # silence after the PR comment: a correct guard against a duplicate PR
            cands.append(dict(tid=tid, title=title or "", assignee=assignee,
                              guard_count=guard_count, pr_time=pr_time,
                              unblocked=unblocked, owner=owner,
                              owner_decided=owner_decided,
                              foreign=foreign))
    finally:
        con.close()

    findings = []
    for c in cands:
        reasons = []
        if c["unblocked"]:
            reasons.append("unblocked")
        if c["owner_decided"]:
            reasons.append("owner decision @%s" % c["owner"])
        if c.get("foreign"):
            reasons.append("foreign PR link from @%s, own runs 0"
                           % c["foreign"])
        pr_h = datetime.fromtimestamp(c["pr_time"]).strftime("%d.%m %H:%M")
        if c.get("foreign"):
            detail = ("      The PR link was given by @%s, not the executor; "
                      "the card has no runs of its own (scheduled stubs do "
                      "not count): a duplicate PR is impossible, the guard "
                      "holds for nothing.\n"
                      "      PR comment %s, no handoff event after it "
                      "(many ticks with empty slots).\n"
                      % (c["foreign"], pr_h))
        else:
            detail = ("      PR comment %s, a decision AFTER it, "
                      "no handoff event:\n"
                      "      the dispatcher is silent while slots are "
                      "empty.\n" % pr_h)
        text = ("    Stuck active_pr guard: %s, %d respawn_guarded ticks "
                "(%s).\n"
                "      %s\n%s"
                % (c["tid"], c["guard_count"], " + ".join(reasons),
                   c["title"][:70], detail))
        key = "%s:active-pr-stuck:%s" % (slug, c["tid"])
        if not apply:
            findings.append((key, 0,
                             text + "      [dry-run: no reassign is done]"))
            continue
        bounce = next((p for p in ACTIVE_PR_BOUNCE_ORDER
                       if p != c["assignee"] and profile_live(p)), None)
        if bounce is None:
            findings.append((key, 0, text +
                             "      No live bounce profile: lift it by hand "
                             "(hermes kanban reassign %s <profile> and back)."
                             % c["tid"]))
            continue
        # Bounce = a reassignment: it goes through the single helper. The
        # detector itself is the check of the stock respawn: %d
        # respawn_guarded ticks without a handoff event (the stock mechanism
        # is silent), and that is written into why.
        covered, guard = board_native_guard.native_or_live(
            "active-pr-bounce-%s" % c["tid"],
            live_keys=_live_keys(slug),
            native_checked=[board_native_guard.NATIVE_RESPAWN],
            why="%d respawn_guarded ticks without a handoff event: the stock "
                "respawn does not lift the card, a bounce reassign cycle is "
                "needed" % c["guard_count"])
        if covered:
            findings.append((key, 0,
                             text + "      50b: bounce suppressed: "
                             + str(guard)))
            continue
        for target in (bounce, c["assignee"]):
            run([HERMES, "kanban", "--board", slug, "reassign", c["tid"],
                 target], timeout=90)
        # A handoff is not words but an event in the database: first check
        # that an assigned event appeared AFTER the PR comment, and only
        # then report and tell the card what was done to it.
        verified = 0
        try:
            vcon = sqlite3.connect("file:%s?mode=ro" % db, uri=True,
                                   timeout=10)
            for (t,) in vcon.execute(
                    "SELECT created_at FROM task_events WHERE task_id = ? "
                    "AND kind IN ('assigned', 'changes_requested', "
                    "'review_reopened') AND created_at > ?",
                    (c["tid"], c["pr_time"])):
                x = _epoch(t)
                if x and x > c["pr_time"]:
                    verified += 1
            vcon.close()
        except sqlite3.Error:
            verified = 0
        if not verified:
            findings.append((key, 0, text + (
                "      reassign sent, but there is no handoff event in the "
                "database: check by hand with hermes kanban show %s."
                % c["tid"])))
            continue
        if c.get("foreign"):
            body = ("Watchdog: lifted a false active_pr guard (%d "
                    "respawn_guarded ticks). The PR link in the comment "
                    "was given by profile %s, not by the card's executor, "
                    "and the card has no run of its own: the duplicate PR "
                    "that the core (kanban_db_dispatch._respawn_blocker) "
                    "protects against is impossible. The handoff was set by "
                    "reassign %s->%s->%s (a built-in core move). If the "
                    "card was not supposed to start, block it again by "
                    "hand."
                    % (c["guard_count"], c["foreign"], c["assignee"],
                       bounce, c["assignee"]))
        else:
            body = ("Watchdog: lifted a stuck active_pr guard (%d ticks of "
                    "respawn_guarded). After the PR comment %s there is a "
                    "decision (%s), but the core "
                    "(kanban_db_dispatch._respawn_blocker) waits for a "
                    "handoff event AFTER the comment: an unblock and a "
                    "verdict do not lift the guard. The handoff was set by "
                    "reassign %s->%s->%s. If the card was not supposed to "
                    "go to work against this PR, block it again by hand."
                    % (c["guard_count"], pr_h, " + ".join(reasons),
                       c["assignee"], bounce, c["assignee"]))
        run([HERMES, "kanban", "--board", slug, "comment", c["tid"],
             body, "--author", "watchdog"], timeout=90)
        findings.append((key, 0, text + (
            "      The gate set a handoff: reassign %s->%s->%s, confirmed by "
            "an event in the database, + a comment with the reason."
            % (c["assignee"], bounce, c["assignee"]))))
    return findings


# --- The cause of a silent p>=80 layer, not the symptom of a busy slot ---

GUARDED_READY_PRIO = 80          # the layer whose silence is a finding
GUARDED_READY_MIN_S = 30 * 60    # a guard held for more than 30 minutes
GUARDED_READY_KINDS = ("spawned", "assigned", "respawn_guarded")


def guarded_ready_lint(slug):
    """A ready p>=80 card held by respawn_guarded for more than 30 minutes: finding.

    I3 (board_lint) fixes the symptom: it takes the slot from a p<50 card
    while the p>=80 layer is non-empty, and says nothing about the cause. A
    p80 card can sit in ready for hours under active_pr after its OWN
    handoff comment with a PR URL (a case the foreign-link branch of
    active_pr_handoff_gate deliberately does not touch: author == assignee,
    fail closed), while I3 keeps cycling reclaim p40 -> respawn p40. Nobody
    named the cause: the dispatcher quietly writes respawn_guarded every
    ~2 min.

    Condition: status=ready, priority>=80, the card's current event streak
    (after the last spawned/assigned) consists of respawn_guarded and lasts
    longer than 30 min. The cause in the text: counts by reason, who gave
    the PR link within the 24 h window (assignee-self / another profile),
    how many own runs. NO actions: lifting the guard for an own comment
    stays a human decision (fail closed).
    """
    db = os.path.join(BOARDS, slug, "kanban.db")
    if not os.path.exists(db):
        return []
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
    now = time.time()

    def _ts(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return _iso_to_epoch(value) or None

    findings = []
    try:
        rows = con.execute(
            "select id, title, assignee, priority from tasks "
            "where status = 'ready' and priority >= ?",
            (GUARDED_READY_PRIO,)).fetchall()
        for tid, title, assignee, prio in rows:
            events = con.execute(
                "select kind, payload, created_at from task_events "
                "where task_id = ? and kind in (?, ?, ?) "
                "order by created_at desc", (tid,) + GUARDED_READY_KINDS
            ).fetchall()
            streak = []
            for kind, payload, at in events:
                if kind != "respawn_guarded":
                    break
                streak.append((payload, _ts(at)))
            if not streak:
                continue
            ats = [at for _, at in streak if at]
            if not ats:
                continue
            first_at = min(ats)
            if now - first_at < GUARDED_READY_MIN_S:
                continue
            reasons = {}
            for payload, _ in streak:
                try:
                    reason = json.loads(payload or "{}").get("reason", "?")
                except ValueError:
                    reason = "?"
                reasons[reason] = reasons.get(reason, 0) + 1
            runs = con.execute(
                "select count(*) from task_runs where task_id = ? and "
                "outcome != 'scheduled'", (tid,)).fetchone()[0]
            pr_author = None
            for author, body, at in con.execute(
                    "select author, body, created_at from task_comments "
                    "where task_id = ? and created_at > ? order by created_at",
                    (tid, now - ACTIVE_PR_PR_WINDOW_S)):
                if at and ACTIVE_PR_PR_URL_RE.search(body or ""):
                    pr_author = ("assignee-self (own handoff comment)"
                                 if author == assignee
                                 else "another profile %s" % author)
                    break
            cause = ", ".join("%s x%d" % kv for kv in sorted(reasons.items()))
            src = ""
            if "active_pr" in reasons:
                src = (" The PR link within the 24 h window was given by %s,"
                       " own runs %d." % (pr_author or "not found", runs))
            findings.append(("%s:r48:guarded:%s" % (slug, tid), 1, (
                "    READY-UNDER-GUARD: %s (p%d) in ready for %.0f min, the "
                "dispatcher does not spawn it: %s.%s Lifting the active_pr "
                "guard: a handoff event after the comment with the link "
                "(reassign there and back) or the foreign-link branch of the "
                "active_pr gate.\n      %s"
                % (tid, prio, (now - first_at) / 60.0, cause, src,
                   title[:70]))))
    finally:
        con.close()
    return findings
def _stacked_workdir(slug):
    """default_workdir of the board from board.json (the same file board_repo reads)."""
    meta = os.path.join(BOARDS, slug, "board.json")
    try:
        workdir = json.load(open(meta)).get("default_workdir")
    except Exception:
        return None
    return workdir if workdir and os.path.isdir(workdir) else None


def _git_is_ancestor(workdir, ancestor, descendant):
    """merge-base --is-ancestor called directly: exit code 0 means ancestor.
    Uses subprocess instead of run(): tests replace run() with a recorder,
    while ancestry must be computed on a real temp repo with real commits."""
    try:
        done = subprocess.run(
            ["git", "-C", workdir, "merge-base", "--is-ancestor",
             ancestor, descendant],
            capture_output=True, text=True, timeout=30)
        return done.returncode == 0
    except Exception:
        return False


def stacked_rework_scan(slug):
    """Open PRs whose head sits on the head of another open PR with the same
    base (a virtual stack). Returns (repo, workdir, stacked) or None."""
    repo = board_repo(slug)
    workdir = _stacked_workdir(slug)
    if not repo or not workdir:
        return None
    out = run(["gh", "pr", "list", "--repo", repo, "--state", "open",
               "--limit", "100", "--json",
               "number,title,headRefOid,baseRefName,author,body"],
              timeout=60)
    try:
        prs = json.loads(out or "[]")
    except Exception:
        return None
    if not prs:
        return None
    # Refresh remote refs. A network failure does not cancel the scan: a stack
    # is opened by pushing to the same remote, and the objects are usually
    # already visible locally.
    run(["git", "-C", workdir, "fetch", "--quiet", "origin"], timeout=60)
    owner = repo.split("/")[0]
    stacked = []
    for x in prs:
        if (x.get("author") or {}).get("login") == owner:
            continue  # the owner's own PR: the PR itself expresses their decision
        if STACK_OWNER_APPROVE_RE.search(x.get("body") or ""):
            continue  # explicit owner decision in the PR body
        for y in prs:
            if y.get("number") == x.get("number"):
                continue
            if y.get("baseRefName") != x.get("baseRefName"):
                continue  # a stack stays on its own line: line A on A, line B on B
            if _git_is_ancestor(workdir, y.get("headRefOid", ""),
                                x.get("headRefOid", "")):
                stacked.append((x, y))
                break
    return repo, workdir, stacked


STACKED_REWORK_COMMENT = (
    STACKED_REWORK_MARKER + "\n\n"
    "This open PR is stacked on open PR #%d (that PR's head commit is an "
    "ancestor of this branch's head). Per the board rule, rework of an "
    "owner verdict lands in that PR's branch, not a new PR.\n\n"
    "Resolution is owned by the architect: the commits are moved into the "
    "#%d branch and this PR is closed with a reference. If this stack is "
    "an explicit owner decision, add a line `stack-owner-approve` to the "
    "PR body and the alert is dropped. No action is expected from the PR "
    "author.")


# False needs_input blocks on agent-only steps. The owner's only part in a PR
# is the merge: update-branch, waiting for required checks and rerun are the
# agent's work, done with the app's rights. A PR was once left waiting for the
# owner on steps that the app performs on the same board.
NEEDS_INPUT_TECH_RE = re.compile(
    r"update[- ]branch"
    r"|updat\w*\s+(the\s+)?branch"
    r"|\brerun\b"
    r"|wait\w*\b.{0,60}?\b(CI|checks?)\b",
    re.IGNORECASE)
# Fail closed: any sign of an owner event cancels the gate; an extra unblock
# costs more than an extra hour of blocking.
NEEDS_INPUT_OWNER_RE = re.compile(
    r"merg"
    r"|approv"
    r"|accept"
    r"|decision"
    r"|milestone"
    r"|prototype"
    r"|budget|quota|balance"
    r"|\bpermissions?\b|\brights\b"
    r"|\b2fa\b|verification"
    r"|question",
    re.IGNORECASE)


def needs_input_tech_gate(slug, apply=True):
    """Gate for false needs_input blocks.

    A card in blocked with kind='needs_input' whose condition (the reason of
    the last block plus the last run summary) requires only technical steps
    (update-branch, waiting for CI, rerun) and names no owner event (merge,
    approve, decision, milestone, acceptance, budget, rights) is a false
    block: the owner's only part in a PR is the merge. The gate returns the
    card to the assignee (unblock) and writes a "Watchdog:" comment. A repeat
    false block of the same card is not handled again: only a finding goes
    into the report (anti ping-pong; after that the core unblock-loop
    detector sends the card to triage).
    Returns findings (key, rank, text).
    """
    db = os.path.join(BOARDS, slug, "kanban.db")
    if not os.path.exists(db):
        return []
    findings = []
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
    try:
        rows = con.execute(
            "SELECT id, title, assignee FROM tasks "
            "WHERE status = 'blocked' "
            "AND coalesce(block_kind, '') = 'needs_input'").fetchall()
        for tid, title, assignee in rows:
            texts = []
            ev = con.execute(
                "SELECT payload FROM task_events WHERE task_id = ? "
                "AND kind = 'blocked' ORDER BY id DESC LIMIT 1",
                (tid,)).fetchone()
            if ev and ev[0]:
                try:
                    texts.append(json.loads(ev[0]).get("reason") or "")
                except (ValueError, TypeError):
                    texts.append("")
            sm = con.execute(
                "SELECT summary FROM task_runs WHERE task_id = ? "
                "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
            if sm and sm[0]:
                texts.append(sm[0])
            cond = " ".join(t for t in texts if t)
            if not NEEDS_INPUT_TECH_RE.search(cond):
                continue          # no technical steps in the condition
            if NEEDS_INPUT_OWNER_RE.search(cond):
                continue   # an owner event is present: the block is not false
            key = "%s:needs-input-tech:%s" % (slug, tid)
            text = ("    False needs_input block: %s, the condition "
                    "requires only agent steps (update-branch / CI / "
                    "rerun).\n      %s\n"
                    "      Rule: the owner's only part in a PR is the merge."
                    % (tid, (title or "")[:70]))
            already = con.execute(
                "SELECT COUNT(*) FROM task_comments WHERE task_id = ? "
                "AND body LIKE 'Watchdog: false needs_input block%'",
                (tid,)).fetchone()[0]
            if already:
                findings.append((key, 1, text +
                                 " Repeat false block (the watchdog comment "
                                 "is already there): return it to the "
                                 "assignee by hand."))
                continue
            if not apply:
                findings.append((key, 1, text +
                                 " [dry-run: no unblock is done]"))
                continue
            run([HERMES, "kanban", "--board", slug, "unblock", tid],
                timeout=90)
            # The unblock must become an event in the database: verify first,
            # then report and comment on the card.
            st = None
            try:
                vcon = sqlite3.connect("file:%s?mode=ro" % db, uri=True,
                                       timeout=10)
                row = vcon.execute(
                    "SELECT status FROM tasks WHERE id = ?",
                    (tid,)).fetchone()
                st = row[0] if row else None
                vcon.close()
            except sqlite3.Error:
                st = None
            if st == "blocked":
                findings.append((key, 1, text +
                                 " unblock was sent but the status did not "
                                 "change: check hermes kanban show %s." % tid))
                continue
            run([HERMES, "kanban", "--board", slug, "comment", tid,
                 "Watchdog: false needs_input block: the condition requires "
                 "only technical steps (update-branch / waiting for CI / "
                 "rerun). That is the agent's work, done with the app's "
                 "rights, not the owner's: the owner's only part in a PR is "
                 "the merge. The card is returned to the assignee: do the "
                 "steps yourself, and set needs_input only for a merge or a "
                 "decision that has no technical path. The condition was: "
                 "%s" % cond[:400]], timeout=90)
            findings.append((key, 1, text + " Unblocked, the assignee "
                             "got a comment."))
    finally:
        con.close()
    return findings


def initial_status_unstick_gate(slug, apply=True):
    """An eternal initial_status block on the children of an accepted plan.

    The architect creates decomposition children with
    ``create --initial-status blocked`` + ``link <root> <child>`` and hands
    the plan to the built-in review lane (``kanban_request_review``). The
    core completes the root on ACCEPTED but does not lift the children:
    recompute_ready treats any blocked status as sticky (the last event
    kind='blocked' -> "only an explicit kanban unblock"), and the block with
    reason='initial_status' hangs forever. The plan review gate runs in
    DRAIN_ONLY and does not lead such roots.

    Gate rule: a card in blocked whose last blocked/unblocked event carries
    reason='initial_status' AND which has parents by links, ALL of them
    closed (done/archived) -> unblock; the core re-gates by parents itself
    (ready). Children with an open parent are not touched: the hourly pass
    lifts them after the parent closes (the last event stays the
    initial_status block). Operator blocks (another reason) and
    initial_status cards without parents are not touched: the latter go to
    the report as a finding without an action.
    Idempotency: after the unblock the last event is 'unblocked', a repeated
    pass is silent.
    Returns findings (key, rank, text).
    """
    db = os.path.join(BOARDS, slug, "kanban.db")
    if not os.path.exists(db):
        return []
    findings = []
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
    try:
        rows = con.execute(
            "SELECT id, title FROM tasks WHERE status = 'blocked'").fetchall()
        for tid, title in rows:
            ev = con.execute(
                "SELECT payload FROM task_events WHERE task_id = ? "
                "AND kind IN ('blocked', 'unblocked') "
                "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
            if not ev or not ev[0]:
                continue
            try:
                payload = json.loads(ev[0])
            except (ValueError, TypeError):
                continue
            if payload.get("reason") != "initial_status":
                continue          # an operator/work block: not ours
            parents = con.execute(
                "SELECT p.status FROM task_links l "
                "JOIN tasks p ON p.id = l.parent_id "
                "WHERE l.child_id = ?", (tid,)).fetchall()
            key = "%s:initial-status-unstick:%s" % (slug, tid)
            if not parents:
                findings.append((key, 1,
                                 "    Eternal initial_status block without "
                                 "parents: %s\\n      %s\\n"
                                 "      Nobody will lift this card by "
                                 "itself; move it to needs_input with a "
                                 "release condition or close it."
                                 % (tid, (title or "")[:70])))
                continue
            if any(p[0] not in ("done", "archived") for p in parents):
                continue          # waits for a parent through a stock link
            text = ("    Eternal initial_status block of a child of a closed "
                    "root: %s\\n      %s\\n"
                    "      The plan is accepted (root done), the core treats "
                    "the block as sticky and does not lift the child."
                    % (tid, (title or "")[:70]))
            if not apply:
                findings.append((key, 1, text +
                                 " [dry-run: no unblock is done]"))
                continue
            run([HERMES, "kanban", "--board", slug,
                 "unblock", tid], timeout=90)
            st = None
            try:
                vcon = sqlite3.connect("file:%s?mode=ro" % db, uri=True,
                                       timeout=10)
                row = vcon.execute(
                    "SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
                st = row[0] if row else None
                vcon.close()
            except sqlite3.Error:
                st = None
            if st not in ("ready", "todo", "review"):
                findings.append((key, 1, text +
                                 " unblock was sent, but the status is "
                                 "\"%s\": check hermes kanban show %s."
                                 % (st, tid)))
                continue
            run([HERMES, "kanban", "--board", slug, "comment", tid,
                 "Watchdog: lifted an eternal initial_status block. "
                 "The plan root is closed (ACCEPTED), but the core treats "
                 "any blocked status as sticky and waits for an explicit "
                 "unblock; children created with create --initial-status "
                 "blocked are not lifted by themselves. From here the chain "
                 "goes through stock links: the hourly watchdog lifts every "
                 "closed link the same way.",
                 "--author", "watchdog"], timeout=90)
            findings.append((key, 1, text + " Unblocked -> %s." % st))
    finally:
        con.close()
    return findings


# Tails of closed issues: when the owner closes an issue (its PR merged), the
# links of its chain on the board (root architect, stability check, an ops
# "close #N" card) must not stay alive.
CLOSED_TAIL_REPO = cfg("FACTORY_REPO")
CLOSED_TAIL_CACHE = os.path.join(HOME, "cache", "closed-issue-tail.json")
CLOSED_TAIL_TTL_S = 1800
# an hour between closing the issue and retiring its tail: let the delivery
# comments land and the closer card complete by itself
CLOSED_TAIL_GRACE_S = 3600
CLOSED_TAIL_MARKER = "closed-issue-tail"
CLOSED_TAIL_REBIND_MARKER = "closed-dup-rebind"
DUP_OF_RE = re.compile(r"[Dd]uplicate of #(\d{1,5})")
CLOSED_TAIL_DUP_CACHE = os.path.join(HOME, "cache", "closed-issue-dups.json")


def closed_tail_anchor_refs(title, body):
    """Anchors of a card: #N in the title or in a body line "Issue: #N".

    Passing mentions of #N inside the text are NOT anchors: an issue-link
    comment may list an open issue in passing while the card belongs to the
    chain of a closed one.
    """
    out = set()
    out.update(int(n) for n in re.findall(r"#(\d{1,5})", title or ""))
    for ln in (body or "").splitlines():
        if re.match(r"\s*(issues?|mirror)\s*:", ln, re.I):
            out.update(int(n) for n in re.findall(r"#(\d{1,5})", ln))
    return out


def closed_tail_fetch_states(now=None):
    """(closed={N: close epoch}, opens={N}) for the issues of the board repo.

    Two gh calls, cached for 30 minutes. A PR number (shared numbering with
    issues) never appears in these lists: an anchor like "PR #N" is silently
    ignored. Tests replace this function as a whole.
    """
    now = now if now is not None else time.time()
    try:
        data = json.load(open(CLOSED_TAIL_CACHE))
        if now - (data.get("ts") or 0) <= CLOSED_TAIL_TTL_S:
            return ({int(k): v for k, v in (data.get("closed") or {}).items()},
                    set(data.get("open") or ()))
    except Exception:
        pass
    closed = {}
    raw = run(["gh", "issue", "list", "--repo", CLOSED_TAIL_REPO,
               "--state", "closed", "--limit", "500", "--json",
               "number,closedAt"], timeout=90)
    for it in json.loads(raw or "[]"):
        try:
            epoch = datetime.fromisoformat(
                (it.get("closedAt") or "").replace("Z", "+00:00")).timestamp()
        except Exception:
            epoch = 0
        closed[int(it["number"])] = epoch
    opens = set()
    raw = run(["gh", "issue", "list", "--repo", CLOSED_TAIL_REPO,
               "--state", "open", "--limit", "500", "--json", "number"],
              timeout=90)
    opens = {int(it["number"]) for it in json.loads(raw or "[]")}
    try:
        json.dump({"ts": now, "closed": closed, "open": sorted(opens)},
                  open(CLOSED_TAIL_CACHE, "w"))
    except OSError:
        pass
    return closed, opens


def closed_tail_dup_lookup(n, repo=None):
    """Original of a closed duplicate: #N from "Duplicate of #N" (issue body
    or comment). Cached on disk: a duplicate does not change after closing.
    Any failure -> None: no rebind, the closed-tail gate semantics apply.
    """
    repo = repo or CLOSED_TAIL_REPO
    key = "%s#%d" % (repo, n)
    try:
        cache = json.load(open(CLOSED_TAIL_DUP_CACHE))
        if key in cache:
            return cache[key]
    except Exception:
        cache = {}
    target = None
    try:
        raw = run(["gh", "issue", "view", str(n), "--repo", repo,
                   "--json", "body,comments"], timeout=90)
        data = json.loads(raw or "{}") or {}
        texts = [data.get("body") or ""]
        texts += [c.get("body") or ""
                  for c in (data.get("comments") or [])]
        for t in texts:
            m = DUP_OF_RE.search(t)
            if m:
                target = int(m.group(1))
                break
    except Exception:
        target = None
    cache[key] = target
    try:
        json.dump(cache, open(CLOSED_TAIL_DUP_CACHE, "w"),
                  ensure_ascii=False)
    except OSError:
        pass
    return target


def _rebind_anchor_text(title, body, pairs):
    """#n -> #target in the title and in the "Issue:" lines of the body.

    Prose is left alone: incidental #N have other roles (incident context,
    guarantees of neighbouring issues) and are not anchors
    (closed_tail_anchor_refs).
    """

    def sub(s):
        for a, b in pairs.items():
            s = re.sub(r"#%d\b" % a, "#%d" % b, s)
        return s

    title2 = sub(title or "")
    out = []
    for ln in (body or "").splitlines():
        if re.match(r"\s*(issues?|mirror)\s*:", ln, re.I):
            ln = sub(ln)
        out.append(ln)
    body2 = "\n".join(out)
    if (body or "").endswith("\n"):
        body2 += "\n"
    return title2, body2


def closed_duplicate_rebind(slug, apply=True, now=None, fetch=None,
                            dup_of=None):
    """Closing an issue as a DUPLICATE rebinds the live chain.

    Why: an issue was closed with "Duplicate of #N" while a chain of cards
    still carried the closed number as its anchor, and the closed-tail gate
    archived the whole chain an hour later although the work was alive and
    needed. A duplicate is a REBIND, not the death of the work.

    Rule: an anchor of the card (closed_tail_anchor_refs) was closed more
    than an hour ago (the same grace as for tails) and the issue has
    "Duplicate of #N" with an OPEN #N -> along the closed task_links chain
    the anchors are rewritten #duplicate -> #original (title and "Issue:"
    lines) and a marker comment is put on every link. Statuses and runs are
    not touched (the admission gate fixes the band: the anchor is now the
    open original). The original is closed or there is no duplicate ->
    silence, the closed_issue_tail_gate semantics apply.

    Returns findings [(key, severity, text)].
    """
    db = os.path.join(BOARDS, slug, "kanban.db")
    if not os.path.exists(db):
        return []
    now = now if now is not None else time.time()
    fetch = fetch or closed_tail_fetch_states
    dup_of = dup_of or closed_tail_dup_lookup
    try:
        closed, opens = fetch(now)[:2]
    except Exception:
        return []
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
    live = list(con.execute(
        "SELECT id, title, coalesce(body,''), status FROM tasks"
        " WHERE status NOT IN ('done','archived')"))
    links = list(con.execute(
        "SELECT parent_id, child_id FROM task_links"))
    con.close()
    parents, children = {}, {}
    for pid, cid in links:
        parents.setdefault(cid, []).append(pid)
        children.setdefault(pid, []).append(cid)
    info = {tid: (t or "", b or "", st) for tid, t, b, st in live}
    live_ids = set(info)
    findings = []
    handled = set()
    for tid in sorted(info):
        if tid in handled:
            continue
        title, body, _status = info[tid]
        a = closed_tail_anchor_refs(title, body)
        stale = sorted(n for n in a if n in closed
                       and CLOSED_TAIL_GRACE_S < now - closed[n])
        pairs = {}
        for n in stale:
            t = dup_of(n)
            if t and t in opens:
                pairs[n] = t
        if not pairs:
            continue
        # the chain: a closed walk in both directions over live links
        chain, stack = {tid}, [tid]
        while stack:
            x = stack.pop()
            for nxt in parents.get(x, []) + children.get(x, []):
                if nxt in live_ids and nxt not in chain:
                    chain.add(nxt)
                    stack.append(nxt)
        handled |= chain
        note = ", ".join("#%d -> #%d" % (k, v)
                         for k, v in sorted(pairs.items()))
        for x in sorted(chain):
            xtitle, xbody, _xst = info[x]
            title2, body2 = _rebind_anchor_text(xtitle, xbody, pairs)
            if apply and (title2 != xtitle or body2 != xbody):
                cmd = [HERMES, "kanban", "--board", slug, "edit", x]
                if title2 != xtitle:
                    cmd += ["--title", title2]
                if body2 != xbody:
                    cmd += ["--body", body2]
                run(cmd, timeout=90)
            if apply:
                run([HERMES, "kanban", "--board", slug, "comment", x,
                     CLOSED_TAIL_REBIND_MARKER
                     + ": the issue was closed as a duplicate of an open"
                     " original (%s), anchor rebound, the chain lives on"
                     % note], timeout=90)
            findings.append((
                "closedtail-rebind:%s:%s" % (slug, x), 5,
                "    %s rebound to the original (%s): the duplicate is"
                " closed, the work lives on" % (x, note)))
    return findings


def closed_issue_tail_gate(slug, apply=True, now=None, fetch=None):
    """Retire the live links of the chain of a closed issue.

    Stock paths checked before writing this (native-or-live rule; none of
    them fires when an issue is closed):
      - the kanban core reacts to GitHub events (issue closed) only by
        mirroring comments into the card feed ("github: ..."), it takes no
        action;
      - dependency_wait releases children when a CARD closes, not an issue;
      - pr-hygiene closes issues by merged PRs (the forward direction); the
        reverse (issue closed -> retire cards) exists nowhere in scripts/.
    Rule: all anchors of a card (closed_tail_anchor_refs) are issues closed
    more than an hour ago -> the card is dead together with the issue. The
    closed task_links chain of the anchor is retired: todo/ready/scheduled/
    blocked are archived with a marker comment, running gives an owner
    finding (the run burns quota). Follow-up exception: a link with an
    anchor on an OPEN issue continues the work and is not retired.
    """
    db = os.path.join(BOARDS, slug, "kanban.db")
    if not os.path.exists(db):
        return []
    now = now if now is not None else time.time()
    fetch = fetch or closed_tail_fetch_states
    closed, opens = fetch(now)
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
    try:
        rows = list(con.execute(
            "select id, status, title, body from tasks"
            " where status not in ('done','archived','cancelled')"))
        links = list(con.execute(
            "select parent_id, child_id from task_links"))
    except sqlite3.Error:
        return []
    finally:
        con.close()
    live = {r[0] for r in rows}
    info = {r[0]: (r[1], r[2] or "", r[3] or "") for r in rows}
    adj = {}
    for a, b in links:
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)

    def anchors(tid):
        status, title, body = info[tid]
        return closed_tail_anchor_refs(title, body)

    def dead_anchors(tid, a):
        return (a and all(n in closed and now - closed[n] > CLOSED_TAIL_GRACE_S
                          for n in a))

    findings = []
    handled = set()
    mutatable = {"todo", "ready", "scheduled", "blocked"}
    for tid in sorted(live):
        if tid in handled:
            continue
        a = anchors(tid)
        if not dead_anchors(tid, a):
            continue
        chain, stack = set(), [tid]
        while stack:
            x = stack.pop()
            if x in chain or x not in live:
                continue
            chain.add(x)
            stack.extend(adj.get(x, ()))
        for x in sorted(chain):
            if x in handled:
                continue
            handled.add(x)
            status, _title, _body = info[x]
            xa = anchors(x)
            open_a = sorted(n for n in xa if n in opens)
            if open_a:
                findings.append(("closedtail-keep:%s:%s" % (slug, x), 5,
                                 "    %s kept: follow-up on the open "
                                 "issue %s" % (x, open_a)))
                continue
            if status in mutatable:
                if apply:
                    run([HERMES, "kanban", "--board", slug, "comment", x,
                         CLOSED_TAIL_MARKER + ": issue %s closed by the owner, "
                         "the chain link was retired by the watchdog; card "
                         "anchors: %s" % (sorted(a), sorted(xa))], timeout=90)
                    run([HERMES, "kanban", "--board", slug, "archive", x],
                        timeout=90)
                findings.append(("closedtail:%s:%s" % (slug, x), 5,
                                 "    %s archived: tail of the closed "
                                 "issue %s" % (x, sorted(a))))
            elif status == "running":
                findings.append(("closedtail-run:%s:%s" % (slug, x), 5,
                                 "    %s RUNNING on the tail of the closed issue "
                                 "%s: the run burns quota, stop the run or "
                                 "retire the card after it finishes"
                                 % (x, sorted(a))))
    return findings


def stacked_rework_gate(slug, apply=True):
    """Alert on a live stack on top of an open PR without an owner decision.
    Idempotency is a marker comment on the PR: while the marker is there,
    passes stay silent. Returns findings (key, rank, text)."""
    scanned = stacked_rework_scan(slug)
    if not scanned:
        return []
    repo, workdir, stacked = scanned
    findings = []
    for x, y in stacked:
        cmt = run(["gh", "pr", "view", str(x.get("number")), "--repo", repo,
                   "--json", "comments"], timeout=60)
        already = False
        try:
            # gh answers with an object {"comments": [...]}; any other shape
            # is not silent but loud (see below), so the shape is fixed here
            # and mirrored in the test fixture.
            data = json.loads(cmt or "{}")
            comments = (data.get("comments")
                        if isinstance(data, dict) else data)
            already = any(STACKED_REWORK_MARKER in (c.get("body") or "")
                          for c in (comments or []))
        except Exception:
            already = False
        if already:
            continue
        text = ("    Stack on top of a live PR without an owner decision: PR #%d\n"
                "      %s\n"
                "      head of #%d is an ancestor of the head of #%d (%s on top "
                "of %s), both are open, base %s.\n"
                "      Rule: rework of a verdict goes into the branch of the "
                "verdict PR,\n"
                "      blocks of one verdict go sequentially, a new PR only "
                "by an owner\n"
                "      decision (a stack-owner-approve line in the PR body)."
                % (x.get("number"), (x.get("title") or "")[:70],
                   y.get("number"), x.get("number"),
                   x.get("headRefOid", "")[:7], y.get("headRefOid", "")[:7],
                   x.get("baseRefName")))
        if not apply:
            findings.append(("%s:stacked-rework:%d" % (slug, x.get("number")),
                             2, text + " [dry-run: the marker is not placed]"))
            continue
        run(["gh", "pr", "comment", str(x.get("number")), "--repo", repo,
             "--body", STACKED_REWORK_COMMENT % (y.get("number"),
                                                 y.get("number"))],
            timeout=60)
        findings.append(("%s:stacked-rework:%d" % (slug, x.get("number")), 2,
                         text + "\n      The gate placed a marker comment on the PR."))
    return findings


def lint_card_kind(title, key):
    """Card class for I1/I2: 'rework' | 'rebase' | 'twin-port' |
    're-review' | None.

    Carriers: keys owner-review-<N>-... and reviewer-pr<N>-... from the
    review and arm layers, the word rework in the title, twin/port for
    transfers between release lines. The rebase kind exists because the
    merge watchdog (pr-hygiene) absorbs a rebase only through a live coder
    card of kind rework/rebase: key conventions coder-pr<N>-rebase-<sha7>
    and rebase-<N>-<sha7> plus the word rebase in the title.
    """
    key = key or ""
    title = title or ""
    if key.startswith("reviewer-pr"):
        return "re-review"
    if key.startswith("owner-review") or LINT_REWORK_TITLE_RE.search(title):
        return "rework"
    if LINT_REBASE_KEY_RE.match(key) or LINT_REBASE_TITLE_RE.search(title):
        return "rebase"
    if LINT_TWIN_TITLE_RE.search(title):
        return "twin-port"
    return None


def lint_i6_kind(title, key):
    """Card class for I6: 'verify' | 'force-push' | 'port' |
    're-review' | None.

    These are downstream cards whose premise (branch, verdict, transfer)
    lives in the parent: if the parent went to the ARCHIVE (its work was
    absorbed by a successor), releasing the child means working on a
    nonexistent premise. The rebase kind is NOT included: a rebase card is
    self-sufficient (pr-hygiene creates it without parents). Order of checks:
    key before title, force-push before verify ("Adversarially verify
    rebased" must not be confused with re-review even if the words overlap).
    """
    key = key or ""
    title = title or ""
    if key.startswith("reviewer-pr"):
        return "re-review"
    if LINT_I6_FORCEPUSH_RE.search(title):
        return "force-push"
    if LINT_I6_VERIFY_RE.search(title):
        return "verify"
    if LINT_TWIN_TITLE_RE.search(title):
        return "port"
    return None


def _lint_data(slug):
    """Board snapshot for the lint over one read-only connection."""
    db = os.path.join(BOARDS, slug, "kanban.db")
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
    tasks = {}
    for row in con.execute(
            "select id,status,title,assignee,priority,idempotency_key,"
            "branch_name,created_at,started_at,completed_at from tasks"):
        tasks[row[0]] = {"status": row[1], "title": row[2] or "",
                         "assignee": row[3] or "", "priority": row[4] or 0,
                         "key": row[5] or "", "branch": row[6] or "",
                         "created": row[7] or 0, "started": row[8] or 0,
                         "completed": row[9] or 0}
    links = {(p, c) for p, c in con.execute(
        "select parent_id, child_id from task_links")}
    gave_up = set()
    for task_id, outcome in con.execute(
            "select task_id, outcome from task_runs r where ended_at is not null "
            "and id = (select max(id) from task_runs r2 "
            "where r2.task_id = r.task_id)"):
        if outcome == "gave_up":
            gave_up.add(task_id)
    comments = {}
    for task_id, body in con.execute("select task_id, body from task_comments"):
        comments.setdefault(task_id, []).append(body or "")
    status_since = {}
    qmarks = ",".join("?" * len(LINT_STATUS_EVENT_KINDS))
    for task_id, ts in con.execute(
            "select task_id, max(created_at) from task_events "
            "where kind in (%s) group by task_id" % qmarks,
            LINT_STATUS_EVENT_KINDS):
        status_since[task_id] = ts or 0
    con.close()
    return tasks, links, gave_up, comments, status_since


def board_lint(slug, dry_run=False):
    """Invariants I1-I6 for one board. Returns findings like the other
    detectors: [(dedup_key, sort_rank, text)]. Fixes are confirmed by
    re-reading the database: "FIXED" is written only if the violation really
    disappeared, otherwise a separate alert with its own key is raised."""
    tasks, links, gave_up, comments, status_since = _lint_data(slug)
    now = time.time()
    found = []
    live = {tid: t for tid, t in tasks.items()
            if t["status"] not in ("done", "archived")}

    def has_marker(tid, n):
        mark = "Watchdog lint I%d:" % n
        return any(mark in b for b in comments.get(tid, []))

    def kanban(*args):
        return run([HERMES, "kanban", "--board", slug] + list(args), timeout=90)

    # --- I1: children of blocked/gave_up parents of the rework/twin/re-review classes
    i1_planned = []
    for parent, child in sorted(links):
        p, c = tasks.get(parent), tasks.get(child)
        if not p or not c:
            continue
        kind = lint_card_kind(c["title"], c["key"])
        # The rebase kind is in the vocabulary but is not part of invariant
        # I1: changing the composition of I1 is a separate owner decision;
        # the classifier was extended only for the merge watchdog.
        if kind not in ("rework", "twin-port", "re-review") \
                or c["status"] in ("done", "archived"):
            continue
        parent_stuck = (p["status"] == "blocked"
                        or (p["status"] not in ("done", "archived")
                            and parent in gave_up))
        if parent_stuck:
            i1_planned.append((parent, child, kind))
    for parent, child, kind in i1_planned:
        key = "%s:lint:i1:%s:%s" % (slug, parent, child)
        title = tasks[child]["title"][:70]
        if dry_run:
            found.append((key, 1, (
                "    I1 (dry-run): link %s -> %s, %s under blocked/gave_up.\n"
                "      %s" % (child, parent, kind, title))))
            continue
        kanban("unlink", parent, child)
        for tid in (child, parent):
            if not has_marker(tid, 1):
                kanban("comment", tid,
                       "Watchdog lint I1: link %s -> %s removed: the parent "
                       "is blocked/gave_up, and a rework/port/re-review "
                       "under it does not start. The owner restores the "
                       "link if it is needed." % (parent, child))
        tasks2, links2, gave_up2, _, _ = _lint_data(slug)
        p2, c2 = tasks2.get(parent), tasks2.get(child)
        still = ((parent, child) in links2
                 and p2 and p2["status"] not in ("done", "archived")
                 and (p2["status"] == "blocked" or parent in gave_up2))
        if still:
            found.append(("%s:lint:i1f:%s:%s" % (slug, parent, child), 1, (
                "    I1 NOT FIXED: link %s -> %s was not removed (%s).\n"
                "      %s\n      check by hand: hermes kanban --board %s "
                "unlink %s %s" % (child, parent, kind, title, slug, parent,
                                  child))))
        else:
            found.append((key, 1, (
                "    I1 FIXED: link %s -> %s removed (%s under "
                "blocked/gave_up), comment on both.\n      %s"
                % (child, parent, kind, title))))

    # --- I2: priorities of reviewer cards and reworks
    open_numbers = None
    repo = board_repo(slug)
    if repo:
        out = run(["gh", "pr", "list", "--repo", repo, "--state", "open",
                   "--limit", "200", "--json", "number"], timeout=60)
        try:
            open_numbers = ({int(p["number"]) for p in json.loads(out)}
                            if out else None)
        except Exception:
            open_numbers = None
    reviewer_live = [tid for tid, t in live.items()
                     if t["key"].startswith("reviewer-pr")]
    if reviewer_live and open_numbers is None:
        found.append(("%s:lint:i2:gh" % slug, 1, (
            "    I2: the list of open PRs is unavailable (%s), priorities "
            "of %d reviewer cards were not checked. Retry in an hour."
            % (repo or "board repo not determined", len(reviewer_live)))))
    i2_planned = []
    for tid, t in sorted(live.items()):
        key, prio = t["key"], t["priority"] or 0
        m = LINT_KEY_PR_RE.match(key)
        kind = lint_card_kind(t["title"], key)
        # The prefix reviewer-pr<N>- is also carried by manual architect
        # follow-ups (an owner-decision card with a reviewer-pr<N>-... key
        # and assignee=architect). The p100 branch is only for real review
        # cards (assignee=reviewer); the other such keys are rework after an
        # owner review: floor p80.
        if (key.startswith("reviewer-pr") and m and open_numbers
                and int(m.group(1)) in open_numbers
                and t["assignee"] == "reviewer"):
            if prio < LINT_REVIEWER_PRIO:
                i2_planned.append((tid, LINT_REVIEWER_PRIO,
                                   "reviewer card of the open PR #%s"
                                   % m.group(1)))
        elif ((kind == "rework"
               or (key.startswith("reviewer-pr")
                   and t["assignee"] != "reviewer"))
              and prio < LINT_REWORK_MIN_PRIO):
            i2_planned.append((tid, LINT_REWORK_MIN_PRIO,
                               "%s after an owner review"
                               % ("rework" if kind == "rework" else "re-review")))
    for tid, target, why in i2_planned:
        key = "%s:lint:i2:%s" % (slug, tid)
        old = tasks[tid]["priority"] or 0
        title = tasks[tid]["title"][:70]
        if dry_run:
            found.append((key, 1, (
                "    I2 (dry-run): %s p%d < threshold: %s requires p%d.\n      %s"
                % (tid, old, why, target, title))))
            continue
        kanban("edit", tid, "--priority", str(target))
        t2 = _lint_data(slug)[0].get(tid)
        if t2 and (t2["priority"] or 0) < target:
            found.append(("%s:lint:i2f:%s" % (slug, tid), 1, (
                "    I2 NOT FIXED: %s did not rise to p%d (%s).\n      %s"
                % (tid, target, why, title))))
        else:
            found.append((key, 1, (
                "    I2 FIXED: %s p%d -> p%d (%s).\n      %s"
                % (tid, old, target, why, title))))

    # --- I3: a slot is not held by p<50 while the ready layer p>=80 is non-empty
    high_ready = sorted(
        (tid for tid, t in live.items()
         if t["status"] == "ready" and (t["priority"] or 0) >= LINT_HIGH_PRIO))
    for tid, t in sorted(live.items()):
        if t["status"] != "running" or (t["priority"] or 0) >= LINT_START_FLOOR:
            continue
        if not high_ready:
            continue
        key = "%s:lint:i3:%s" % (slug, tid)
        title = t["title"][:70]
        if dry_run:
            found.append((key, 1, (
                "    I3 (dry-run): %s (p%d) running with %d ready p>=%d "
                "(first %s).\n      %s" % (tid, t["priority"],
                                           len(high_ready), LINT_HIGH_PRIO,
                                           high_ready[0], title))))
            continue
        # There is no "move" subcommand in the kanban CLI, so an old call
        # always failed and its "went back to todo" comment was false. The
        # stock action that really takes a card off its run: reclaim
        # (terminates the worker, clears the claim, returns the retry status
        # ready/todo, writes the reason into the event).
        kanban("reclaim", tid, "--reason",
               "board-watchdog lint I3: slot p%d with %d ready p>=%d"
               % (t["priority"], len(high_ready), LINT_HIGH_PRIO))
        t2 = _lint_data(slug)[0].get(tid)
        if t2 and t2["status"] == "running" and (t2["priority"] or 0) < LINT_START_FLOOR:
            found.append(("%s:lint:i3f:%s" % (slug, tid), 1, (
                "    I3 NOT FIXED: %s (p%d) did not leave running after "
                "reclaim with %d ready p>=%d.\n      %s"
                % (tid, t2["priority"], len(high_ready),
                                      LINT_HIGH_PRIO, title))))
        else:
            # The comment is written only after re-reading the board: the
            # reclaim is confirmed, the card is no longer running.
            if not has_marker(tid, 3):
                kanban("comment", tid,
                       "Watchdog lint I3: the run was taken off by reclaim: "
                       "with %d ready cards p>=%d (first %s) slots are not "
                       "held by cards p<%d. It returns to work when the "
                       "layer empties."
                       % (len(high_ready), LINT_HIGH_PRIO, high_ready[0],
                          LINT_START_FLOOR))
            found.append((key, 1, (
                "    I3 FIXED: %s (p%d) taken off its slot by reclaim with "
                "%d ready p>=%d, status after re-read: %s.\n      %s"
                % (tid, t["priority"], len(high_ready), LINT_HIGH_PRIO,
                   (t2 or {}).get("status", "?"), title))))

    # --- I4: ready without an assignee, alert
    #     A custom detector was once removed as a duplicate of the stock
    #     stranded_in_ready, but the stock rule skips an empty assignee on
    #     purpose (kanban_diagnostics.py returns nothing for it and points
    #     at the dispatcher's skipped_unassigned), and skipped_unassigned is
    #     only a field of the tick result: the gateway dispatcher notifies
    #     nobody about it. A card created past admission without an assignee
    #     (a cron, by hand) dies silently. So the detector is back as an
    #     ALERT: the watchdog does not fill in the assignee (the assignee is
    #     required at creation, the creator picks the profile by the meaning
    #     of the card; a silent kanban.default_assignee would break profile
    #     routing).
    for tid, t in sorted(live.items()):
        if t["status"] != "ready" or (t["assignee"] or "").strip():
            continue
        since = status_since.get(tid) or max(t["completed"], t["started"],
                                             t["created"])
        age = now - since
        if age < UNASSIGNED_AFTER:
            continue
        found.append(("%s:lint:i4:%s" % (slug, tid), 1, (
            "    I4: %s has been in ready without an assignee for %.1f h: the "
            "dispatcher skips it silently (skipped_unassigned), the stock "
            "stranded_in_ready does not see an empty assignee.\n"
            "      %s\n      fix by hand: hermes kanban --board %s "
            "assign %s <profile> (or close the card)"
            % (tid, age / 3600, t["title"][:70], slug, tid))))

    # --- I5: nonstandard status for over an hour, alert
    for tid, t in sorted(tasks.items()):
        if t["status"] in LINT_STATUSES:
            continue
        since = status_since.get(tid) or max(t["completed"], t["started"],
                                             t["created"])
        age = now - since
        if age < LINT_NONSTANDARD_AFTER:
            continue
        found.append(("%s:lint:i5:%s" % (slug, tid), 1, (
            "    I5: %s has been in the nonstandard status '%s' for %.1f h: the "
            "dispatcher does not see it in any workflow lane.\n"
            "      %s\n      fix by hand: hermes kanban --board %s "
            "promote %s (to todo) / complete %s (to done) / archive %s "
            "(to archive)" % (tid, t["status"], age / 3600,
                              t["title"][:70], slug, tid, tid, tid))))

    # --- I6: a live downstream card (verify/port/force-push/re-review)
    # whose parents are ALL archived. Archived is not done: the work was
    # absorbed by a successor card, the successor is not linked to the child,
    # and the platform has already released the child (promotion when the
    # parent is archived). Example: a verify card went to ready BEFORE the
    # rebase, when its duplicate parent was archived. Parents in done release
    # legitimately: the condition is exactly all-archived.
    i6_act = ("todo", "ready", "running")
    for tid, t in sorted(live.items()):
        kind = lint_i6_kind(t["title"], t["key"])
        if kind is None:
            continue
        parents = sorted(p for p, c in links if c == tid and p in tasks)
        if not parents or not all(tasks[p]["status"] == "archived"
                                  for p in parents):
            continue
        if t["status"] == "blocked":
            continue    # already waits for a human: a repeat block sends it to triage
        key = "%s:lint:i6:%s" % (slug, tid)
        title = t["title"][:70]
        why = ("%s (%s), parents are archived: %s"
               % (tid, kind, ", ".join(parents)))
        if t["status"] not in i6_act:
            # scheduled/triage/review are not dispatched, no block needed
            # (scheduled is parking, a block would break it).
            # On promotion to todo/ready the next hourly pass catches the card.
            found.append((key, 1, (
                "    I6: %s (no block, status '%s'): all parents are "
                "archived, assign a successor with link.\n      %s"
                % (why, t["status"], title))))
            continue
        if dry_run:
            found.append((key, 1, (
                "    I6 (dry-run): %s: assign a successor with link.\n"
                "      %s" % (why, title))))
            continue
        kanban("block", tid, "--kind", "needs_input",
               "Watchdog lint I6: the parent is archived, assign a successor "
               "with link (%s). Release condition: the card has a live "
               "successor parent (kanban link <successor> %s) or the card "
               "is closed as a duplicate." % (why, tid))
        t2 = _lint_data(slug)[0].get(tid)
        if t2 and t2["status"] == "blocked":
            found.append((key, 1, (
                "    I6 FIXED: %s blocked as needs_input "
                "(the parent is archived, assign a successor with link).\n      %s"
                % (why, title))))
        else:
            found.append(("%s:lint:i6f:%s" % (slug, tid), 1, (
                "    I6 NOT FIXED: %s did not get blocked. Check by hand: "
                "hermes kanban --board %s block %s --kind needs_input"
                % (why, slug, tid))))
    return found


def _load_state():
    """Dedup state for escalations outside main() (main passes its own)."""
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


# Owner decision: a limit of 2 with measurable conditions for rolling back to
# 1. The rollback is decided by the gate from live numbers, not by a rigid
# "no more than N" taken from a one-off measurement. The rule lives in the
# project conventions, section "Parallel workers".
MAX_IN_PROGRESS_ALLOWED = 2
# Rollback thresholds (conditions 1-3 of the rule). The thresholds leave
# headroom over the current values (swap 2.0/4 GB, load ~0.7): crossing one
# is degradation, not noise.
ROLLBACK_SWAP_USED_MB = 3584      # cond 1: swap used over 3.5 GB of 4
ROLLBACK_LOAD5 = 12.0             # cond 2: load5 > 12 with two cards running
ROLLBACK_LOAD5_MIN_RUNNING = 2    # cond 2: counted only with >= 2 running
ROLLBACK_CRASH_WINDOW_S = 30 * 60 # cond 3: window of a double crash
GATE_BOARD = cfg("FACTORY_BOARD", "main")  # board where the rule lives and where the alert goes
GLOBAL_CONFIG = os.path.join(os.environ.get("HERMES_HOME", HOME), "config.yaml")
GATE_ALERT_KEY = "ops-gate-max-in-progress-alert"

HERMES_REPO = os.path.join(HOME, "hermes-agent")
CODE_STALE_GRACE = 15 * 60      # code newer than the gateway for over 15 min: alert


def gateway_pid():
    """pid of the gateway process or None."""
    out = run(["pgrep", "-f", "gateway run"], timeout=10)  # no hermes_cli token: the core guard treats such an -f pattern as targeting the gateway process
    out = out.strip()
    return int(out.split()[0]) if out else None


def code_newer_than_gateway():
    """Detector "core is newer than the gateway".

    An edit of kanban_db.py lands in the working tree, but the gateway runs
    the old code: Python imports the module at start and keeps it in memory.
    Restarting the gateway the stock way (`hermes gateway restart` from an
    outside session, without -y: with live turns the command refuses, and
    the restart is postponed until they finish) picks the edit up.

    Compares the mtime of the freshest .py in hermes_cli/ and gateway/ with
    the start time of the gateway process. If the code is newer than the
    process for longer than CODE_STALE_GRACE (15 min), alert: the edit was
    not picked up and a gateway restart is needed.

    Returns (dedup_key, text) or None.
    """
    pid = gateway_pid()
    if not pid:
        return None
    lstart = run(["ps", "-o", "lstart=", "-p", str(pid)], timeout=10)
    if not lstart:
        return None
    try:
        proc_start = time.strptime(lstart.strip(), "%a %b %d %H:%M:%S %Y")
        proc_start_ts = time.mktime(proc_start)
    except Exception:
        return None

    # mtime of the freshest .py in the core directories on disk
    code_ts = 0
    for sub in ("hermes_cli", "gateway"):
        d = os.path.join(HERMES_REPO, sub)
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if not name.endswith(".py"):
                continue
            try:
                m = int(os.path.getmtime(os.path.join(d, name)))
            except OSError:
                continue
            if m > code_ts:
                code_ts = m

    if not code_ts or not proc_start_ts:
        return None
    if code_ts <= proc_start_ts:
        return None

    gap = int(code_ts - proc_start_ts)
    if gap < CODE_STALE_GRACE:
        return None

    code_when = time.strftime("%Y-%m-%d %H:%M", time.localtime(code_ts))
    proc_when = time.strftime("%Y-%m-%d %H:%M", time.localtime(proc_start_ts))
    return ("code-newer-than-gateway", (
        "  GATE: a core edit was not picked up by the gateway.\n"
        "    Core code (hermes_cli/*.py, gateway/*.py) changed %s,\n"
        "    the gateway process (pid %d) started %s, the code is newer by %d min.\n"
        "    The gateway runs old code: gates in kanban_db.py and others do not apply.\n"
        "    Fix by restarting the gateway from an outside session: hermes gateway restart\n"
        "    (without -y: with live turns the command lists them and refuses, then\n"
        "    wait for them to finish; systemctl restart and --interrupt cut the\n"
        "    turns)."
        % (code_when, pid, proc_when, gap // 60)
    ))


def read_max_in_progress():
    """kanban.max_in_progress from the same config.yaml the dispatcher reads."""
    try:
        import yaml
        cfg = yaml.safe_load(open(GLOBAL_CONFIG)) or {}
        raw = (cfg.get("kanban") or {}).get("max_in_progress")
    except Exception:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 1 else None


def system_memory_sample():
    """/proc/meminfo -> (swap_used_mb, swap_total_mb, mem_available_mb) or None.

    Note: there is no SwapUsed field in /proc/meminfo, used = SwapTotal - SwapFree.
    """
    try:
        fields = {}
        for line in open("/proc/meminfo"):
            key, _, rest = line.partition(":")
            fields[key] = int(rest.split()[0]) // 1024
        swap_total = fields.get("SwapTotal", 0)
        swap_used = max(swap_total - fields.get("SwapFree", 0), 0)
        return (swap_used, swap_total, fields.get("MemAvailable", 0))
    except Exception:
        return None


def load5():
    """5-minute load average or None."""
    try:
        return float(open("/proc/loadavg").read().split()[1])
    except Exception:
        return None


def running_workers_all_boards():
    """How many cards are in running across all boards (int, -1 on error)."""
    total = 0
    for slug in board_slugs():
        db = os.path.join(BOARDS, slug, "kanban.db")
        try:
            con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
            total += con.execute(
                "select count(*) from tasks where status='running'").fetchone()[0]
            con.close()
        except sqlite3.Error:
            return -1
    return total


def gateway_unclean_exit_recent(window_s):
    """Whether there was a trace of an unclean gateway stop within the window.

    Machine-readable sources (as in the rule, condition 3):
    - logs/gateway-exit-diag.log: records with tag=gateway.previous_unclean_exit /
      gateway.exit_nonzero (success=false) / the key suspected_oom;
    - state/gateway.lifecycle.json: a sentinel in phase=running with a dead
      pid is an unclean death without an exit path.
    Returns an evidence string or None.
    """
    cutoff_iso = datetime.fromtimestamp(time.time() - window_s,
                                        tz=timezone.utc).isoformat()
    path = os.path.join(HOME, "logs", "gateway-exit-diag.log")
    try:
        for line in open(path, errors="replace"):
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = str(rec.get("ts", ""))
            if ts < cutoff_iso:
                continue
            tag = str(rec.get("tag", ""))
            if tag in ("gateway.previous_unclean_exit", "gateway.exit_nonzero"):
                if tag == "gateway.exit_nonzero" and rec.get("success") is True:
                    continue
                return "%s @ %s" % (tag, ts)
            if rec.get("suspected_oom"):
                return "suspected_oom @ %s" % ts
    except OSError:
        pass
    sentinel_path = os.path.join(HOME, "state", "gateway.lifecycle.json")
    try:
        sentinel = json.load(open(sentinel_path))
        if sentinel.get("phase") == "running":
            pid = sentinel.get("pid")
            try:
                os.kill(int(pid), 0)
                alive = True
            except (ValueError, TypeError, ProcessLookupError, PermissionError):
                alive = False
            if not alive:
                return "lifecycle sentinel phase=running, pid %s is dead" % pid
    except Exception:
        pass
    return None


def parallel_crash_pair_recent(window_s):
    """Condition 3: two overlapping runs ended crashed within one window_s.

    Signature of a past incident: swap filled up, the gateway got SIGTERM,
    both runs became crashed. Looks at machine traces in task_runs of all
    boards within the window: a pair of crashed runs whose intervals
    [started_at, ended_at] overlapped.
    Returns an evidence string or None.
    """
    now = time.time()
    for slug in board_slugs():
        db = os.path.join(BOARDS, slug, "kanban.db")
        try:
            con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
            rows = list(con.execute(
                "select task_id, started_at, ended_at from task_runs "
                "where outcome='crashed' and ended_at > ? "
                "order by ended_at desc limit 50", (now - window_s,)))
            con.close()
        except sqlite3.Error:
            continue
        crashed = [r for r in rows if r[2] is not None]
        for i in range(len(crashed)):
            for j in range(i + 1, len(crashed)):
                a, b = crashed[i], crashed[j]
                overlap = min(a[2], b[2]) - max(a[1], b[1])
                if overlap > 0 and abs(a[2] - b[2]) <= window_s:
                    return ("board %s: %s and %s crashed, overlap %d s, "
                            "crashes %d s apart"
                            % (slug, a[0], b[0], overlap, abs(a[2] - b[2])))
    return None


def max_in_progress_gate(state, now):
    """Parallelism gate: limit 2, rollback to 1 on measurable conditions.

    An owner decision raised the limit from 1 to 2. The rule lives in the
    project conventions, section "Parallel workers: limit 2 and conditions
    for rolling back to 1".

    Two duties:
    1. Hard ceiling: kanban.max_in_progress in config.yaml is not above
       MAX_IN_PROGRESS_ALLOWED (2). Larger values are fixed at once through
       `hermes config set` plus an alert on the board (dedup once a day).
    2. Conditions for rolling back to 1 (conditions 1-3 of the rule): swap
       > 3.5 GB; load5 > 12 with >= 2 running; a double crashed failure in a
       30-minute window plus a trace of an unclean gateway stop. When any
       condition holds, the watchdog sets the limit back to 1 itself and
       writes the numbers into the alert. Returning to 2 is up to the owner.

    The gateway dispatcher reads max_in_progress once at start
    (gateway/kanban_watchers.py: _kanban_dispatcher_boot): a change through
    `hermes config set` takes effect after a gateway restart. Re-reading on
    every tick exists only in the deprecated loop `hermes kanban daemon
    --force` (kanban_db_dispatch.py), which is not running here. So the
    effective limit of the running gateway may differ from the config until
    a restart, and the rollback alert must say so.

    Returns (dedup_key, text) for the report, or None if all is normal.
    """
    value = read_max_in_progress()
    if value is None:
        return None

    # --- Rollback conditions: live numbers regardless of the config value ---
    swap_used, swap_total, mem_avail = (None, None, None)
    sample = system_memory_sample()
    if sample:
        swap_used, swap_total, mem_avail = sample
    l5 = load5()
    running = running_workers_all_boards()
    reasons = []
    if swap_used is not None and swap_used > ROLLBACK_SWAP_USED_MB:
        reasons.append("cond 1: swap used %d MB of %d MB (threshold %d MB)"
                       % (swap_used, swap_total, ROLLBACK_SWAP_USED_MB))
    if (l5 is not None and running >= ROLLBACK_LOAD5_MIN_RUNNING
            and l5 > ROLLBACK_LOAD5):
        reasons.append("cond 2: load5 %.1f with %d cards in running "
                       "(threshold %.1f)" % (l5, running, ROLLBACK_LOAD5))
    crash = parallel_crash_pair_recent(ROLLBACK_CRASH_WINDOW_S)
    if crash:
        unclean = gateway_unclean_exit_recent(ROLLBACK_CRASH_WINDOW_S)
        if unclean:
            reasons.append("cond 3: %s; gateway: %s" % (crash, unclean))

    stats = ("swap %s/%s MB, MemAvailable %s MB, load5 %s, running workers %s"
             % ("?" if swap_used is None else swap_used,
                "?" if swap_total is None else swap_total,
                "?" if mem_avail is None else mem_avail,
                "?" if l5 is None else "%.1f" % l5,
                "?" if running < 0 else running))

    # --- Rollback to 1: a condition holds ---
    if reasons and value > 1:
        run([HERMES, "config", "set", "kanban.max_in_progress", "1"],
            timeout=60)
        after = read_max_in_progress()
        outcome = ("lowered %d -> 1" % value if after == 1 else
                   "not lowered after `hermes config set` (now %s): "
                   "fix by hand" % after)
        severity = "ROLLBACK ON CONDITION" if after == 1 else "GATE FAILED"
        text = ("  max_in_progress gate (%s).\n"
                "    %s\n"
                "    kanban.max_in_progress: %s. %s.\n"
                "    NOTE: the built-in gateway dispatcher reads the limit once\n"
                "    at start: until `hermes gateway restart` the effective limit\n"
                "    stays as it was.\n"
                "    Returning to 2 is up to the owner after review: the rule is\n"
                "    in the project conventions,\n"
                "    \"Parallel workers\"."
                % (severity, "; ".join(reasons), outcome, stats))
        key = "maxinprogress-rollback"
        _gate_board_comment(
            key, state, now,
            "rollback to 1: %s; %s. kanban.max_in_progress %s. %s"
            % ("; ".join(reasons), stats, value, outcome))
        if now - state.get(key, 0) <= REPEAT_AFTER:
            return None  # fixed and alerted within this window, the report stays silent
        return (key, text)

    # --- Hard ceiling: the config value is above MAX_IN_PROGRESS_ALLOWED ---
    if value > MAX_IN_PROGRESS_ALLOWED:
        run([HERMES, "config", "set", "kanban.max_in_progress",
             str(MAX_IN_PROGRESS_ALLOWED)], timeout=60)
        after = read_max_in_progress()
        if after is not None and after > MAX_IN_PROGRESS_ALLOWED:
            outcome = ("not lowered after `hermes config set` (now %d): "
                       "fix by hand" % after)
            severity = "GATE FAILED"
        else:
            outcome = "lowered %d -> %s" % (value, MAX_IN_PROGRESS_ALLOWED)
            severity = "lowered automatically"
        key = "maxinprogress"
        _gate_board_comment(
            key, state, now,
            "ceiling violation: kanban.max_in_progress = %d in %s; %s. "
            "Rule: the project conventions, \"Parallel workers: limit 2 "
            "and conditions for rolling back to 1\"."
            % (value, GLOBAL_CONFIG, outcome))
        if now - state.get(key, 0) <= REPEAT_AFTER:
            return None  # fixed and alerted within this window, the report stays silent
        return (key, "  max_in_progress gate (%s).\n"
                     "    kanban.max_in_progress in the config was %d "
                     "(rule: no more than %d):\n"
                     "    %s\n"
                     "    The limit is 2: if more is needed, first a new "
                     "rule and a measurement, then edit the project conventions."
                     % (severity, value, MAX_IN_PROGRESS_ALLOWED, outcome))

    # --- Normal: with a pair of parallel E2E cards, write the numbers as routine ---
    if running >= 2 and sample:
        key = "maxinprogress-parallel"
        if now - state.get(key, 0) > 6 * 3600:
            return (key, "  max_in_progress gate: a pair of parallel cards.\n"
                         "    %s. Rollback conditions are not met." % stats)
    return None


def _gate_board_comment(key, state, now, body):
    """Alert comment on the gate board (dedup REPEAT_AFTER, like all others)."""
    if now - state.get(key, 0) <= REPEAT_AFTER:
        return ""
    # Native-or-live rule: the shared helper before create.
    covered, guard = board_native_guard.native_or_live(
        GATE_ALERT_KEY,
        live_keys=_live_keys(GATE_BOARD),
        native_checked=[board_native_guard.NATIVE_RESPAWN],
        why="alert of the kanban.max_in_progress gate for a human: there "
            "is no stock delivery of the gate diagnostics (delivery side)")
    if covered:
        return ""
    card = ""
    out = run([HERMES, "kanban", "--board", GATE_BOARD, "create",
               "max_in_progress gate: watchdog alerts",
               "--body", "Beacon for alerts of the kanban.max_in_progress "
               "gate. No assignee on purpose: the card informs, it does "
               "not work.",
               "--idempotency-key", GATE_ALERT_KEY, "--json"], timeout=90)
    try:
        card = (json.loads(out) or {}).get("id", "")
    except Exception:
        card = ""
    if card:
        run([HERMES, "kanban", "--board", GATE_BOARD, "comment", card,
             body], timeout=90)
        _park_beacon(card)
    return card


def _park_beacon(card):
    """The beacon does not stay in ready: by design it has no assignee, and
    invariant I4 (the stock stranded_in_ready diagnostic) rightly counts such
    a card as a stall.
    It does not stay in todo either: the dispatcher promotes a parentless
    card back to ready within a minute. So a needs_input block: visible on the
    board, outside the dispatcher queue, lifted only by hand. The phrase
    "Release condition" in the reason counts it as a legitimate wait in the
    graph (waited)."""
    reason = ("Beacon for alerts of the max_in_progress gate: it informs, it "
              "does not work, the dispatcher does not spawn it. Release "
              "condition: by hand, when the parallelism ceiling rule leaves "
              "the project conventions or the owner changes the beacon.")
    run([HERMES, "kanban", "--board", GATE_BOARD, "block", card, reason,
         "--kind", "needs_input"], timeout=90)


def twin_blocked_missing_module_detector(slug):
    """Detector: a twin card is blocked because a module is missing on the target line.

    Scenario: a coder card ports a feature from one base branch to another
    (twin), but a module was not ported to the target branch. The worker
    sees the module is missing and blocks with kind=dependency. After >=2
    such runs the watchdog must escalate: create an ops card with options
    for a decision.

    Signal in the payload of a blocked / dependency_wait event:
    - "does not exist on <base branch>"
    - "module does not exist"

    Twin sign: the title contains twin or a base branch name.
    """
    db = os.path.join(BOARDS, slug, "kanban.db")
    if not os.path.exists(db):
        return []
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)

    since = time.time() - 7 * 86400  # 7 days: a twin cycle can stretch over several days

    # All cards with at least one blocked run in the window
    blocked_tasks = list(con.execute("""
        SELECT DISTINCT r.task_id
        FROM task_runs r
        JOIN task_events e ON e.run_id = r.id
            AND e.kind IN ('blocked', 'dependency_wait')
        WHERE r.outcome = 'blocked'
          AND r.started_at > ?
          AND e.payload IS NOT NULL
    """, (since,)))

    base_alt = "|".join(re.escape(b) for b in
                        tuple(cfg_list("FACTORY_BASE_BRANCHES", ("main",))))
    twin_re = re.compile(r'twin|' + base_alt, re.IGNORECASE)
    reason_re = re.compile(
        r'(does not exist on (' + base_alt + r')|module does not exist)',
        re.IGNORECASE,
    )

    found = []

    for (task_id,) in blocked_tasks:
        trow = list(con.execute(
            "SELECT title, status FROM tasks WHERE id=?", (task_id,)))
        if not trow:
            continue
        title, status = trow[0]
        if status in ("done", "archived"):
            continue

        # Check the twin sign in the title
        if not twin_re.search(title or ""):
            continue

        # Count runs with a matching block reason
        events = list(con.execute("""
            SELECT e.payload
            FROM task_runs r
            JOIN task_events e ON e.run_id = r.id
                AND e.kind IN ('blocked', 'dependency_wait')
            WHERE r.task_id = ?
              AND r.outcome = 'blocked'
              AND r.started_at > ?
              AND e.payload IS NOT NULL
            ORDER BY r.started_at DESC
        """, (task_id, since)))

        matched = 0
        for (payload,) in events:
            try:
                p = json.loads(payload)
                reason = p.get("reason", "") if isinstance(p, dict) else ""
            except (json.JSONDecodeError, TypeError):
                reason = ""
            if reason_re.search(reason):
                matched += 1

        if matched >= 2:
            found.append((task_id, title, status, matched))

    con.close()

    if not found:
        return []

    result = []
    for task_id, title, status, count in found:
        key = "twin_missing_module:%s:%s" % (slug, task_id)
        text = (
            "    Twin card is blocked because a module is missing "
            "on the target line: " + task_id + " [" + status + "]\n"
            "      " + title[:80] + "\n"
            "      " + str(count) + " runs with the same reason: "
            "the module was not ported to the target branch.\n"
            "      Options (the owner must choose):\n"
            "        1. Create a parent card to port the missing module;\n"
            "        2. Rephrase the twin as a full port of the module + feature;\n"
            "        3. Close as infeasible."
        )

        # Create an ops card with the escalation (the idempotency key prevents a duplicate)
        # Native-or-live rule: the shared helper before create.
        covered, guard = board_native_guard.native_or_live(
            "twin-missing-module:%s:%s" % (slug, task_id),
            live_keys=_live_keys(slug),
            native_checked=[
                board_native_guard.NATIVE_TASK_LINKS,
                board_native_guard.NATIVE_RESPAWN],
            why="twin blocked by >=2 runs on a missing module: there is no "
                "stock detector for \"the dependency does not exist on the "
                "target branch\", the owner's choice needs an escalation")
        if covered:
            result.append((key, 0,
                           text + "\n      native-or-live: ops card not created: "
                           + str(guard)))
            continue
        run([HERMES, "kanban", "--board", slug, "create",
             "[ops] Twin blocked: port prerequisite module for %s" % task_id[:12],
             "--body",
             board_native_guard.guard_section(guard) +
             "Twin card `%s` (\"%s\") is blocked after >=2 runs:\n"
             "the dependent module does not exist on the target branch.\n\n"
             "Options:\n"
             "1. Create a parent card to port the missing module, "
             "then the twin on top;\n"
             "2. Rephrase this twin card as a full port of the "
             "module + feature (move the whole module to the target branch, "
             "not only the diff changes);\n"
             "3. Close the twin as infeasible on this branch.\n\n"
             "An owner decision is needed."
             % (task_id, title[:60]),
             "--assignee", "ops",
             "--idempotency-key", "twin-missing-module:%s:%s" % (slug, task_id),
             "--json"], timeout=90)

        result.append((key, 0, text))

    return result


# === Conflict-debt gate ====================================================
#
# Rule: when more than 3 PRs are CONFLICTING, new milestone features do not
# start and the slots go to rebases. pr-hygiene writes the debt count to a
# cache (every 2 h); the watchdog runs hourly and parks ready features in
# scheduled until the debt is back under the limit. The dispatcher reads
# priorities, not rules: the restriction is enforced by the scheduled status,
# which the dispatcher does not pick up, and lifted by the same gate, not by
# someone remembering to do it.
DEBT_CACHE = os.path.join(HOME, "cache", "pr-hygiene-conflict-debt.json")
CONFLICT_DEBT_LIMIT = 3
DEBT_CACHE_TTL_S = 3 * 3600
# Hold: while this file exists the debt gate neither parks nor returns
# anything, whatever the cache says. Used while the gate is not yet accepted
# by the owner.
MERGEFLOW_HOLD = os.path.join(HOME, "cache", "mergeflow-hold")
# Owner decision: a defect of the BASE branch marked [release-blocker] in
# the title outranks rebases. The debt gate does not park such cards (the
# exception lives in milestone_feature_cards) and returns them early if it
# parked them before the decision. Reason: a base-branch defect blocking a
# release must not sit in scheduled because of the rebase debt.
RELEASE_BLOCKER_MARK = "[release-blocker]"


def read_debt_cache(now=None):
    """(count, age_seconds, is_fresh) from the pr-hygiene cache.

    Freshness is 3 h: pr-hygiene runs every 2 h, so anything older means it
    has been silent for two runs in a row, and features must not be parked
    on a stale number.
    """
    if not os.path.exists(DEBT_CACHE):
        return None, None, False
    try:
        data = json.load(open(DEBT_CACHE))
    except Exception:
        return None, None, False
    ts, count = data.get("ts"), data.get("count")
    if not isinstance(ts, (int, float)) or not isinstance(count, int):
        return None, None, False
    age = (now or time.time()) - ts
    return count, age, age <= DEBT_CACHE_TTL_S


def milestone_feature_cards(slug):
    """Ready cards of new milestone features.

    The priority boundary is 60: 60 and above is work on already open PRs
    (fixes 80, twin waves 81+), while 30-40 are new features from the plan.
    Rebase cards (key coder-pr<N>-rebase-*) are left alone: they are the
    consumers of the freed slots. Cards carrying RELEASE_BLOCKER_MARK are
    left alone too: a base-branch defect does not wait for the conflict debt
    to return.
    """
    db = os.path.join(BOARDS, slug, "kanban.db")
    if not os.path.exists(db):
        return []
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
    rows = list(con.execute(
        "select id, coalesce(priority,0), substr(coalesce(title,''),1,56)"
        " from tasks where status='ready' and assignee='coder'"
        " and coalesce(priority,0) < 60"
        " and (idempotency_key is null"
        "      or idempotency_key not like 'coder-pr%-rebase-%')"
        " and coalesce(title,'') not like '%' || ? || '%'",
        (RELEASE_BLOCKER_MARK,)))
    con.close()
    return rows


def _card_status(slug, task_id):
    db = os.path.join(BOARDS, slug, "kanban.db")
    if not os.path.exists(db):
        return None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
        row = con.execute("select status from tasks where id=?",
                          (task_id,)).fetchone()
        con.close()
        return row[0] if row else None
    except Exception:
        return None


def _is_release_blocker(slug, task_id):
    """Does the card carry RELEASE_BLOCKER_MARK in its title?"""
    db = os.path.join(BOARDS, slug, "kanban.db")
    if not os.path.exists(db):
        return False
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
        row = con.execute("select title from tasks where id=?",
                          (task_id,)).fetchone()
        con.close()
        return bool(row and RELEASE_BLOCKER_MARK in (row[0] or ""))
    except Exception:
        return False


def conflict_debt_gate(slug, state, now=None, apply=True):
    """CONFLICTING > 3 -> milestone features do not start.

    Parking goes through hermes kanban schedule (the reason is written as a
    comment); the return is an unblock when the debt is <= 3. Only cards in
    state["conflict_debt_parked"] are returned: a scheduled card that is
    not ours is left alone. The action is idempotent: after parking there
    are no ready candidates, so a repeat is safe.

    apply=False is a dry run: it counts and prints, the board is unchanged.
    Returns (key, text) for the report, or None.
    """
    if slug != cfg("FACTORY_BOARD", "main"):
        return None
    if os.path.exists(MERGEFLOW_HOLD):
        return None               # hold: the gate is not accepted yet
    count, age, fresh = read_debt_cache(now)
    if count is None or not fresh:
        return None
    parked = state.get("conflict_debt_parked") or {}
    if count > CONFLICT_DEBT_LIMIT:
        # A parked [release-blocker] is returned early: the debt no longer
        # holds it (a base-branch defect outranks rebases).
        stuck_blockers = [tid for tid in parked
                          if _is_release_blocker(slug, tid)]
        todo = [row for row in milestone_feature_cards(slug)
                if row[0] not in parked]
        if not todo and not stuck_blockers:
            if parked:
                return None            # stable state, already reported
            return None
        lines = ["  CONFLICT DEBT GATE: %d > %d. New milestone features"
                 " do not start, the slots go to rebases."
                 % (count, CONFLICT_DEBT_LIMIT)]
        for tid in stuck_blockers:
            if apply:
                run([HERMES, "kanban", "--board", slug, "unblock", tid,
                     "--reason", "release-blocker: a base-branch defect does"
                     " not wait for the conflict debt to return"],
                    timeout=90)
                status = _card_status(slug, tid)
                lines.append("    [release-blocker] %s -> %s: returned"
                             " early, does not wait for the debt"
                             % (tid, status or "?"))
                if status in ("ready", "todo", "running"):
                    parked.pop(tid, None)
            else:
                lines.append("    [release-blocker] %s -> ready"
                             " (dry-run, untouched)" % tid)
        for tid, prio, title in todo:
            if apply:
                run([HERMES, "kanban", "--board", slug, "schedule", tid,
                     "conflict debt %d > %d: the slots go to rebases"
                     " (board-watchdog gate)"
                     % (count, CONFLICT_DEBT_LIMIT)], timeout=90)
                if _card_status(slug, tid) == "scheduled":
                    parked[tid] = time.time()
                else:
                    lines.append("    %s NOT parked: check manually"
                                 % tid)
            lines.append("    %s p%d %s%s"
                         % (tid, prio, title,
                            "" if apply else " (dry-run, untouched)"))
        if apply:
            state["conflict_debt_parked"] = parked
        return ("mergeflow-debtgate:%d" % count, "\n".join(lines))
    if parked:
        lines = ["  CONFLICT DEBT %d <= %d: returning parked features"
                 " to the queue." % (count, CONFLICT_DEBT_LIMIT)]
        for tid in list(parked):
            if apply:
                run([HERMES, "kanban", "--board", slug, "unblock", tid,
                     "--reason", "conflict debt %d <= %d, returning to the"
                     " queue" % (count, CONFLICT_DEBT_LIMIT)],
                    timeout=90)
                status = _card_status(slug, tid)
                lines.append("    %s -> %s" % (tid, status or "?"))
                if status in ("ready", "todo", "running"):
                    parked.pop(tid, None)
            else:
                lines.append("    %s -> ready (dry-run, untouched)" % tid)
        if apply:
            state["conflict_debt_parked"] = parked
        return ("mergeflow-debtgate-release:%d" % count, "\n".join(lines))
    return None


# ---------------- Backlog gate: the owner of the backlog ----------------
# Owner rules:
#   1) cards of the factory's own initiative live in triage; once a day the
#      watchdog gathers a triage digest (id, why, estimate) into the owner's
#      report; only what the owner raises, or what carries the label of a
#      milestone issue, goes to work;
#   2) REMOVED: the admission mechanics (rule 2 "outside milestone", parking
#      of ready cards and the raise by owner review) were merged into the
#      admission gate (admission_gate), which was built for the same job.
#      Both gates once fired against each other: admission returned a card,
#      the backlog gate parked it again, and cards stayed in scheduled
#      because admission did not know the backlog marker. The exceptions
#      moved to admission_gate (ops, a direct PR milestone, owner review).
#      What remains here is the digest (rule 1) and the p0 archive (rule 3);
#      the linkage cache feeds only them and is reset when milestones change
#      (see backlog_cache_reset_on_milestone_change); board actions are no
#      longer taken from it;
#   3) a p0 in todo/ready/triage with no movement for more than 14 days is
#      archived with a reason; movement = a run or a status event, comments
#      do not count (stale candidates may carry fresh owner comments with
#      zero runs).
# Hold: while this file exists the whole gate is silent. The owner removes
# it after accepting the gate.
BACKLOG_HOLD = os.path.join(HOME, "cache", "backlog-hold")
BACKLOG_CACHE = os.path.join(HOME, "cache", "backlog-milestones.json")
BACKLOG_TTL_S = 6 * 3600          # cache freshness for board actions
BACKLOG_STALE_OK_S = 24 * 3600    # a stale cache is good for the digest only
BACKLOG_SWEEP_CAP = 40            # returns to triage per run
BACKLOG_P0_STALE_S = 14 * 24 * 3600
BACKLOG_REPO = cfg("FACTORY_REPO")
# The repository owner. Their formal review (APPROVED/CHANGES_REQUESTED; the
# bot cannot review the owner's PR) is the owner's decision to work on a PR;
# for the backlog gate that takes the PR out of "outside the owner's
# attention". A CR of the owner is not lifted by movement of the head.
BACKLOG_OWNER = cfg("FACTORY_OWNER_LOGIN")


def backlog_open_milestones():
    """{number: title} of the repository's OPEN milestones.

    The focus gate knows no release names. The list of active milestones is
    state=open from GitHub: a closed milestone stops applying by itself the
    moment it closes, a new one starts without a code change. None = gh is
    unavailable (an API failure, not "no open milestones": an empty dict is
    a valid state, with which the gate parks nothing and says so in the
    report).
    """
    raw_ms = run(["gh", "api",
                  "repos/%s/milestones?state=open" % BACKLOG_REPO],
                 timeout=60)
    if not raw_ms:
        return None
    try:
        items = json.loads(raw_ms)
    except ValueError:
        return None
    if not isinstance(items, list):
        return None
    out = {}
    for ms in items:
        if ms.get("number") is None:
            continue
        out[int(ms["number"])] = (ms.get("title") or "").strip()
    return out


def backlog_fetch_linkage():
    """GitHub linkage of milestones: {ts, ms_titles, ms_issues,
    open_pr_refs, direct_ms_prs, owner_review_prs}.

    ms_titles: number -> title of the open milestones (source: GitHub
    state=open, no names hardcoded). ms_issues: issue number -> state.
    open_pr_refs: open PR number -> numbers from its body. direct_ms_prs:
    open PRs carrying a milestone label directly; by the letter of the rule
    they do NOT count as linked (a PR links only through Refs/Closes of a
    milestone issue), but the digest must show them to the owner. Zero open
    milestones is NOT a failure: a valid linkage with empty lists is
    returned, and the gate stays silent about parking (backlog_gate).
    """
    ms_numbers = backlog_open_milestones()
    if ms_numbers is None:
        return None
    ms_issues, direct_ms_prs = {}, []
    for number in ms_numbers:
        out = run(["gh", "api",
                   "repos/%s/issues?milestone=%d&state=all&per_page=100"
                   % (BACKLOG_REPO, number)], timeout=60)
        for item in json.loads(out or "[]"):
            if "pull_request" in item:
                if item.get("state") == "open":
                    direct_ms_prs.append(item["number"])
            else:
                ms_issues[item["number"]] = item.get("state")
    raw_prs = run(["gh", "pr", "list", "--repo", BACKLOG_REPO,
                   "--state", "open", "--limit", "200", "--json",
                   "number,body,latestReviews"], timeout=90)
    open_pr_refs = {}
    owner_review_prs = []
    for pr in json.loads(raw_prs or "[]"):
        body = pr.get("body") or ""
        refs = set(int(x) for x in re.findall(r"#(\d+)", body))
        refs |= set(int(x) for x in re.findall(r"/issues/(\d+)", body))
        open_pr_refs[pr["number"]] = sorted(refs)
        # The owner's formal review on any head. latestReviews gives the
        # last review of each reviewer: if the owner left a COMMENTED or
        # DISMISSED review after APPROVED/CHANGES_REQUESTED, it carries no
        # decision and the PR does not count as raised (conservative: the
        # card stays parked until an explicit review).
        for rv in pr.get("latestReviews") or []:
            if ((rv.get("author") or {}).get("login") == BACKLOG_OWNER
                    and rv.get("state") in ("APPROVED",
                                            "CHANGES_REQUESTED")):
                owner_review_prs.append(pr["number"])
                break
    data = {"ts": time.time(), "ms_titles": ms_numbers,
            "ms_issues": ms_issues,
            "open_pr_refs": open_pr_refs,
            "direct_ms_prs": sorted(set(direct_ms_prs)),
            "owner_review_prs": sorted(set(owner_review_prs))}
    try:
        os.makedirs(os.path.dirname(BACKLOG_CACHE), exist_ok=True)
        json.dump(data, open(BACKLOG_CACHE, "w"))
    except OSError:
        pass
    return data


def backlog_linkage(now=None):
    """(data, fresh). None instead of data = no linkage at all.

    A fresh cache allows board actions; a stale one (up to a day) is good
    for reading only (fresh=False), no board mutations go by it. The keys
    ms_issues/open_pr_refs are normalized to int: after a json dump they are
    strings in the cache, while classify compares numbers (otherwise all
    issue linkages silently fell apart).
    """
    def _norm(data):
        if not data:
            return data
        data["ms_titles"] = {int(k): v
                             for k, v in (data.get("ms_titles")
                                          or {}).items()}
        data["ms_issues"] = {int(k): v
                             for k, v in (data.get("ms_issues") or {}).items()}
        data["open_pr_refs"] = {int(k): (v if isinstance(v, list) else [])
                                for k, v in (data.get("open_pr_refs")
                                             or {}).items()}
        data["direct_ms_prs"] = [int(x) for x in (data.get("direct_ms_prs")
                                                  or [])]
        data["owner_review_prs"] = [int(x) for x in
                                    (data.get("owner_review_prs") or [])]
        return data

    now = now or time.time()
    raw_cache = None
    try:
        raw_cache = json.load(open(BACKLOG_CACHE))
    except Exception:
        raw_cache = None
    # A cache of the old format (without ms_titles) is incompatible: it was
    # built from a hardcoded list of release names and a state=all query, and
    # going by it the gate would keep parking work of already closed
    # releases for up to six hours. Read it as absent: backlog_fetch_linkage
    # then re-reads GitHub and rewrites the cache.
    data = _norm(raw_cache) if isinstance(raw_cache, dict) \
        and "ms_titles" in raw_cache else None
    if data and now - (data.get("ts") or 0) <= BACKLOG_TTL_S:
        return data, True
    fresh = None
    try:
        fresh = _norm(backlog_fetch_linkage())
    except Exception:
        fresh = None
    if fresh:
        return fresh, True
    if data and now - (data.get("ts") or 0) <= BACKLOG_STALE_OK_S:
        return data, False
    return None, False


def backlog_cache_reset_on_milestone_change(states):
    """Reset the linkage cache when milestones change.

    Why: milestones were assigned to issues, but the gate took the linkage
    from its cache (TTL 6 h, stale-ok up to a day) and parked cards as
    "outside milestone" by a stale cache. Every open issue with a milestone
    that the cache does not know (or the other way round, one that left the
    live list) drops the cache: the next backlog_linkage re-reads GitHub and
    a fresh milestone applies on the next run. Called from admission_gate.
    """
    try:
        with open(BACKLOG_CACHE) as fh:
            data = json.load(fh)
    except Exception:
        return
    if not isinstance(data, dict) or "ms_titles" not in data:
        return
    cached = set()
    for num, st in (data.get("ms_issues") or {}).items():
        try:
            if st == "open":
                cached.add(int(num))
        except (TypeError, ValueError):
            continue
    for num, (milestone, _labels, *_rest) in states.items():
        if bool(milestone) != (num in cached):
            try:
                os.remove(BACKLOG_CACHE)
            except OSError:
                pass
            return


def backlog_estimate(card):
    nlines = len((card.get("body") or "").splitlines())
    return "S" if nlines <= 12 else ("M" if nlines <= 30 else "L")


# classify/park_backlog/raise_owner_reviewed were removed together with
# rule 2: admission is done by admission_gate (see the section comment
# above). BACKLOG_PARK_MARKER stays: the digest collects old parkings by it,
# and admission_gate returns them once a linkage appears (it understands
# both markers).
BACKLOG_PARK_MARKER = "backlog: outside milestone"


def backlog_p0_stale(slug, now):
    """p0 in todo/ready/triage with no movement for more than 14 days.

    Movement = a run (task_runs.started_at) or a status event
    (task_events with kind != 'commented'). Comments do not count: fresh
    owner comments do not move the work.

    A card with a live link is NOT stale. A card in todo waiting for an
    unfinished parent is by definition "without movement": it waits for
    someone else's work, it is not stale. The same goes for a parent whose
    children are still alive (the implementation is scheduled while review
    and QA sit in todo): archiving breaks the chain, and the fix ships
    without a check. Chains parked by the gate (scheduled at a neighbour) are
    covered by the same condition: scheduled is not done/archived. A live
    link = a parent or child whose status is not in (done, archived). A
    board without task_links (old schemas) is treated as "no links": the
    rule degrades to the earlier behaviour.
    """
    db = os.path.join(BOARDS, slug, "kanban.db")
    if not os.path.exists(db):
        return []
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
    rows = list(con.execute(
        "select id, title, created_at from tasks"
        " where coalesce(priority,0) = 0"
        " and status in ('todo','ready','triage')"))
    last_run = {t: ts for t, ts in con.execute(
        "select task_id, max(started_at) from task_runs group by task_id")}
    last_move = {t: ts for t, ts in con.execute(
        "select task_id, max(created_at) from task_events"
        " where kind != 'commented' group by task_id")}
    linked_live = set()
    try:
        for parent, child, p_status, c_status in con.execute(
                "select l.parent_id, l.child_id,"
                " (select status from tasks where id = l.parent_id),"
                " (select status from tasks where id = l.child_id)"
                " from task_links l"):
            if p_status not in ("done", "archived"):
                linked_live.add(child)      # the child has a live parent
            if c_status not in ("done", "archived"):
                linked_live.add(parent)     # the parent has a live child
    except sqlite3.OperationalError:
        pass                # a board without task_links: no live links
    con.close()
    cut = now - BACKLOG_P0_STALE_S
    stale = []
    for tid, title, created in rows:
        created = created or 0
        if created > cut:
            continue
        if max(last_run.get(tid, 0), last_move.get(tid, 0)) > cut:
            continue
        if tid in linked_live:
            continue        # waits for a dependency, or a chain neighbour is alive
        stale.append((tid, title))
    return stale


BACKLOG_CAT_TITLES = {
    "issue_not_milestone":
        "issue outside the open milestones",
    "pr_not_milestone":
        "PR outside the open milestones: no Refs/Closes of a milestone issue",
    "no_ref": "no link to an issue or PR",
    "triage_manual": "other in triage (not parked by the gate)",
}


def backlog_parked_by_marker(slug):
    """tid -> category by the parking marker comment.

    The source of truth for the digest: a comment with BACKLOG_PARK_MARKER
    on a card in scheduled. It survives a loss of the watchdog's state; the
    category is parsed from "category: <cat>" in the comment body.
    """
    db = os.path.join(BOARDS, slug, "kanban.db")
    if not os.path.exists(db):
        return {}
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
    try:
        rows = list(con.execute(
            "select c.task_id, c.body from task_comments c"
            " join tasks t on t.id = c.task_id"
            " where t.status = 'scheduled' and c.body like ?"
            " order by c.created_at", ["%" + BACKLOG_PARK_MARKER + "%"]))
    except sqlite3.OperationalError:
        rows = []
    con.close()
    parked = {}
    for tid, body in rows:
        m = re.search(r"category: (\w+)", body or "")
        if m:
            parked[tid] = m.group(1)
    return parked


def backlog_digest(slug, swept, linkage, now, archived_today):
    """Daily digest for the owner: id, why, estimate.

    Two sources: cards in triage (rule 1: the watchdog's own initiatives)
    and cards parked by the marker 'backlog: outside milestone' (rule 2);
    the category is stored in the marker comment, not in state. Plus the p0
    archive of the last day (rule 3).
    """
    db = os.path.join(BOARDS, slug, "kanban.db")
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
    rows = list(con.execute(
        "select id, title, body, coalesce(priority,0), created_by"
        " from tasks where status = 'triage'"
        " order by coalesce(priority,0) desc, id"))
    con.close()
    parked = backlog_parked_by_marker(slug)
    groups = {}
    for tid, title, body, prio, creator in rows:
        cat = (swept.get(tid) or {}).get("cat") or "triage_manual"
        est = backlog_estimate({"body": body})
        groups.setdefault(cat, []).append(
            (tid, prio, creator or "?", (title or "").strip(), est))
    if parked:
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
        prows = {r[0]: r[1:] for r in con.execute(
            "select id, (select title from tasks where id = t.id),"
            " (select body from tasks where id = t.id),"
            " coalesce(priority,0), coalesce(created_by,'?')"
            " from tasks t where status = 'scheduled'")}
        con.close()
        for tid, cat in parked.items():
            title, body, prio, creator = prows.get(
                tid, ("", "", 0, "?"))
            groups.setdefault(cat, []).append(
                (tid, prio or 0, creator or "?", (title or "").strip(),
                 backlog_estimate({"body": body})))
    day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    lines = ["BACKLOG GATE: backlog digest for %s." % day,
             "Triage: %d, parked outside the milestone: %d. Only what the"
             " owner raises, or what carries the label of a milestone"
             " issue, goes to work. Return from parking: unblock."
             " Estimate: S up to 12 body lines, M up to 30, L more."
             % (len(rows), len(parked))]
    for cat in sorted(groups, key=lambda c: -len(groups[c])):
        lines.append("")
        lines.append("[%s] %s: %d"
                     % (cat, BACKLOG_CAT_TITLES.get(cat, cat),
                        len(groups[cat])))
        for tid, prio, creator, title, est in groups[cat]:
            lines.append("  %s p%d %s | %s | %s"
                         % (tid, prio, est, creator[:12], title[:88]))
    if archived_today:
        lines.append("")
        lines.append("[archive] p0 without movement for more than 14 days,"
                     " archived in the last day: %d" % len(archived_today))
        for tid, title, _ts in archived_today:
            lines.append("  %s | %s" % (tid, (title or "")[:88]))
    direct = [p for p in (linkage.get("direct_ms_prs") or [])
              if not ((linkage.get("open_pr_refs") or {}).get(p)
                      or [None])[0] in (linkage.get("ms_issues") or {})]
    if direct:
        lines.append("")
        lines.append("Hygiene: open PRs with a direct milestone label and no"
                     " Refs to an issue: %s. Their cards stay in ready,"
                     " but Refs #N in the PR body is required by the"
                     " conventions, and pr-hygiene catches the violation."
                     % direct)
    return "\n".join(lines)


def backlog_gate(slug, state, now=None, apply=True,
                         ignore_hold=False):
    """Backlog gate: triage digest (rule 1) and p0 archive (rule 3).

    Returns a list of (key, text) for the watchdog report. An empty list =
    silent. apply=False changes nothing and leaves state untouched.
    """
    now = now or time.time()
    out = []
    if slug != cfg("FACTORY_BOARD", "main"):
        return out
    if not ignore_hold and os.path.exists(BACKLOG_HOLD):
        return out
    linkage, fresh = backlog_linkage(now)
    if linkage is None:
        return [("backlog-nolinkage",
                 "  BACKLOG GATE: no milestone linkage (cache empty, gh"
                 " is silent). Board untouched.")]
    if apply:
        state6 = state.setdefault("backlog", {})
    else:
        state6 = dict(state.get("backlog") or {})
    swept = dict(state.get("backlog_swept") or {})
    archived = dict(state.get("backlog_archived") or {})
    # Rule 2 was removed: parking of ready cards and the raise by owner
    # review moved to admission_gate. Old parkings are still collected by
    # the digest through BACKLOG_PARK_MARKER (categories live in the marker
    # comments and in swept); admission_gate does the return.
    # Rule 3: a p0 in todo/ready/triage with no movement for more than 14
    # days goes to the archive with a reason comment. Actions only by a
    # fresh cache.
    arch_today = []
    if fresh:
        stale = backlog_p0_stale(slug, now)
        if stale:
            lines = ["  BACKLOG GATE, rule 3: p0 without movement for more"
                     " than 14 days: %d. Archived with a reason, the list"
                     " goes to the digest." % len(stale)]
            for tid, title in stale[:BACKLOG_SWEEP_CAP]:
                if apply and tid not in archived:
                    run([HERMES, "kanban", "--board", slug, "comment", tid,
                         "Backlog gate, rule 3: p0 without movement for more"
                         " than 14 days (no runs or status events, comments"
                         " do not count as movement; no live links: parents"
                         " and children are done/archived). Archived with a"
                         " reason; raise it again if still relevant.",
                         "--author", "watchdog"], timeout=90)
                    run([HERMES, "kanban", "--board", slug, "archive", tid],
                        timeout=90)
                    if _card_status(slug, tid) == "archived":
                        archived[tid] = {"ts": now, "title": (title or "")[:80]}
                if tid in archived:
                    arch_today.append((tid, archived[tid].get("title") or "",
                                       archived[tid].get("ts")))
                lines.append("    %s %s" % (tid, (title or "")[:76]))
            if apply:
                state["backlog_archived"] = archived
            out.append(("backlog-p0:%d" % len(stale), "\n".join(lines)))
    day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    if state6.get("digest_day") != day:
        text = backlog_digest(slug, swept, linkage, now, arch_today)
        if apply:
            state6["digest_day"] = day
        out.append(("backlog-digest:%s" % day, "  " + text))
    return out


HOLDOUT_WINDOW_DAYS = 7      # scan window: the owner asked for a week

# The leak was built into the card itself: a holdout run of the tester
# created a card with evidence paths in its body (cache/holdout/report-*.md,
# cache/scratch/rN), and the coder followed them to grep the hidden
# scenarios. The hidden area is therefore defined by PATHS, not by the word
# holdout: the word is structurally carried by the [holdout-...] titles and
# by the names of the holdout stands, and those are not leaks (matching the
# bare substring produced dozens of alert lines for one real leak).
# No physical boundary is possible: all worker profiles run under one OS
# user, and a directory or permissions give no wall without changing the
# sandbox in the core; the compensating controls are the card lint
# (holdout_card_lint) and the HIGH access detector below.
# A hidden-area path is matched WHOLE, with its prefix (~/, /home/...) and
# the tail up to the file name: if only the directory is removed, the coder
# can rebuild the path from a remainder like
# "/home/user/.hermes/[marker]/report-2026-10-30.md".
# The shared per-round scratch is hidden ONLY in the root cache
# (~/.hermes/cache/scratch/rN, where the holdout tester writes); the workers
# have their own profiles/<role>/cache/scratch/rNNN, and those are working
# directories, not the hidden area. So the third alternative requires a
# .hermes segment before cache, or the start of a path without a profile
# prefix (the lookbehind cuts off profiles/.../cache).
_HOPATH = r"(?:[~.\w/@-]*[/])?"
HOLDOUT_HIDDEN_RE = re.compile(
    _HOPATH + r"cache/holdout(?:/[\w.-]+)*"
    r"|" + _HOPATH + r"profiles/tester/(?:cache|holdout)(?:/[\w.-]+)*"
    r"|" + _HOPATH + r"\.hermes/cache/scratch/r\d+(?:/[\w.-]+)*"
    r"|(?<![\w/@.~-])cache/scratch/r\d+(?:/[\w.-]+)*",
    re.I)
HOLDOUT_SQL_PATTERNS = ("%holdout%", "%cache/scratch/r%",
                        "%profiles/tester/%")


def holdout_guard(now, window_days=None, profiles_root=None):
    """The tester's hidden acceptance scenarios stay hidden.

    Two signal levels:
      HIGH, key holdout-access:<profile>:<session>: a tool call of
      coder/reviewer whose arguments contain a hidden-area path
      (cache/holdout, the tester's scratch). This is an attempt to read the
      hidden scenarios, and the line goes to the very top of the report.
      low, key holdout-mention:<profile>:<session>: a hidden-area path
      surfaced in message text, reasoning or a tool result: the content
      leaked into the context, but there was no access.
    The word holdout without a path is NOT a leak (card titles, stand names,
    kanban list output) and does not reach the board-watchdog push. At most
    one item is issued per session, and HIGH absorbs low: otherwise one
    routine board-list output would give dozens of lines.

    Returns a list of (key, text) pairs, HIGH first.
    """
    window_days = window_days or HOLDOUT_WINDOW_DAYS
    root = profiles_root or os.path.join(HOME, "profiles")
    since = now - window_days * 86400
    findings = []
    for prof in ("coder", "reviewer"):
        db = os.path.join(root, prof, "state.db")
        if not os.path.exists(db):
            continue
        try:
            con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
            # Prefilter by substrings of the hidden-area paths, exact
            # matching by regex in python: SQL like cannot do \d
            cond = " or ".join(
                " or ".join("%s like ?" % col
                            for col in ("tool_calls", "content", "reasoning"))
                for _ in HOLDOUT_SQL_PATTERNS)
            args = [since]
            for _pat in HOLDOUT_SQL_PATTERNS:
                args.extend([_pat] * 3)
            rows = con.execute(
                "select session_id, role, content, tool_calls, tool_name,"
                " reasoning, timestamp from messages"
                " where timestamp > ? and active = 1 and (%s)" % cond,
                args).fetchall()
            con.close()
        except sqlite3.Error as exc:
            findings.append(("holdout-scan-fail:%s" % prof,
                             "  [holdout] Scan of the state.db of profile %s"
                             " failed: %s: %s"
                             % (prof, type(exc).__name__, exc)))
            continue
        seen = {}                       # session -> (severity, key, text)
        for sid, role, content, tool_calls, tool_name, reasoning, ts in rows:
            args_str = str(tool_calls) if tool_calls else ""
            hit = HOLDOUT_HIDDEN_RE.search(args_str)
            if hit:
                sev = 2
                key = "holdout-access:%s:%s" % (prof, (sid or "?")[:14])
                chan = "tool call (%s)" % (tool_name or "?")
                frag = args_str
            else:
                text_src = str(content) if content else ""
                chan = None
                if role == "tool" and text_src:
                    chan = "tool result (%s)" % (tool_name or "?")
                elif text_src:
                    chan = "%s message" % role
                hit = HOLDOUT_HIDDEN_RE.search(text_src) if chan else None
                if not hit and reasoning:
                    rsn = str(reasoning)
                    hit = HOLDOUT_HIDDEN_RE.search(rsn)
                    if hit:
                        text_src, chan = rsn, "reasoning"
                if not hit:
                    continue
                sev = 1
                key = "holdout-mention:%s:%s" % (prof, (sid or "?")[:14])
                frag = text_src
            snippet = frag[max(0, hit.start() - 60):hit.end() + 90]
            snippet = snippet.replace("\n", " ")
            when = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()
            if sev == 2:
                text = ("  [holdout] HIGH: profile %s, session %s: %s from"
                        " %s accesses the tester's hidden area:\n"
                        "    ...%s..." % (prof, (sid or "?")[:14], chan,
                                          when.strftime("%d.%m %H:%M"),
                                          snippet))
            else:
                text = ("  [holdout] low: profile %s, session %s: %s from %s"
                        " carries a hidden-area path (no access happened):\n"
                        "    ...%s..." % (prof, (sid or "?")[:14], chan,
                                          when.strftime("%d.%m %H:%M"),
                                          snippet))
            prev = seen.get(sid)
            if prev is None or sev > prev[0]:
                seen[sid] = (sev, key, text)
        findings.extend((k, t) for _, k, t in
                        sorted(seen.values(), key=lambda x: -x[0]))
    return findings


# A card that holdout creates for coder/reviewer carries the symptom and the
# reproduction steps in its OWN words. A path into the tester's hidden area
# in the body or a comment is a ready-made leak: the executor only has to
# follow it. A check at creation time is impossible without changing the
# core (the card is created by the tester's cron agent), so the mechanism is
# a watchdog lint on every tick: cleanup plus an alert. Finished
# (done/archived) cards are not cleaned: workers are not dispatched there,
# and the owner keeps the history as it is.
HOLDOUT_CARD_PROFILES = ("coder", "reviewer")
HOLDOUT_CARD_LIVE = ("todo", "ready", "in_progress", "blocked", "scheduled")
HOLDOUT_REDACT = "[path to the tester's hidden area removed by the watchdog]"

def holdout_card_lint(slug, apply=True, runner=None):
    """Coder and reviewer cards must not contain hidden-zone paths.

    Scans the body and comments of live cards assigned to coder/reviewer
    for HOLDOUT_HIDDEN_RE. The body is cleaned through "kanban edit --body"
    (the path is replaced with the HOLDOUT_REDACT marker). A comment is
    edited in the database: the CLI cannot edit comments, and the platform
    reads the stored text as is. The watchdog then adds a comment to the
    card explaining why. Tester cards are left alone: the tester owns the
    hidden zone and may legitimately write such paths in its reports.

    Returns a list of (key, rank, text) like check(slug) for main();
    rank 4 puts the line at the top of the board section.
    """
    runner = runner or run
    db = os.path.join(BOARDS, slug, "kanban.db")
    con = sqlite3.connect(db, timeout=30)
    findings = []
    try:
        rows = con.execute(
            "select id, title, assignee, status, body from tasks"
            " where assignee in (?,?)", HOLDOUT_CARD_PROFILES).fetchall()
        for tid, title, assignee, status, body in rows:
            if status not in HOLDOUT_CARD_LIVE:
                continue
            dirty = []
            body = body or ""
            if HOLDOUT_HIDDEN_RE.search(body):
                if apply:
                    runner([HERMES, "kanban", "--board", slug, "edit", tid,
                            "--body",
                            HOLDOUT_HIDDEN_RE.sub(HOLDOUT_REDACT, body)])
                dirty.append("body")
            for cid, author, cbody in con.execute(
                    "select id, author, body from task_comments"
                    " where task_id=?", (tid,)).fetchall():
                cbody = cbody or ""
                if not HOLDOUT_HIDDEN_RE.search(cbody):
                    continue
                if apply:
                    con.execute("update task_comments set body=? where id=?",
                                (HOLDOUT_HIDDEN_RE.sub(HOLDOUT_REDACT, cbody),
                                 cid))
                    con.commit()
                    runner([HERMES, "kanban", "--board", slug, "comment", tid,
                            "[holdout-lint] comment #%d contained a path"
                            " from the tester's hidden zone: masked by the"
                            " watchdog" % cid, "--author", "watchdog"])
                dirty.append("comment #%d (%s)" % (cid, author or "?"))
            if dirty:
                findings.append((
                    "holdout-card-path:%s:%s" % (slug, tid), 4,
                    "  [%s] [holdout] card %s (%s, \"%s\") carried a path"
                    " from the tester's hidden zone in %s: replaced with a"
                    " marker, investigate from the symptom in the card body."
                    % (slug, tid, assignee, (title or "")[:56],
                       " and ".join(dirty))))
    finally:
        con.close()
    return findings


# === Prototype before implementation ======================================
#
# Owner rule: for a new feature (an open issue labelled feature or ux) the
# first result is a prototype in the issue's "## Before / After" section,
# not a PR with the implementation. A mechanism, not a text:
#   (a) an implementation card for an issue without an accepted prototype,
#       sitting in todo/ready, is blocked by the watchdog: kind needs_input
#       (an external event, not a dependency), with the "prototype
#       accepted" release condition in the comment;
#   (b) a card in blocked with the "prototype accepted" release condition
#       is lifted automatically as soon as the issue has a comment by the
#       OWNER with the words "prototype accepted". A comment by anyone else
#       does not count: otherwise the executor would accept its own
#       prototype. A card blocked without our condition is not touched by
#       the gate: someone else's block may have its own reasons.
# The gate's actions are held by a hold file (cache/protogate-hold):
# while the file exists, the gate only reports. Removing the hold means
# "accepted".
PROTOGATE_HOLD = os.path.join(HOME, "cache", "protogate-hold")
PROTOGATE_CACHE = os.path.join(HOME, "cache", "protogate-issues.json")
PROTOGATE_REPO = cfg("FACTORY_REPO")
PROTOGATE_BOARD = cfg("FACTORY_BOARD", "main")
PROTOGATE_OWNER = cfg("FACTORY_OWNER_LOGIN")
PROTOGATE_LABELS = ("feature", "ux")
PROTOGATE_ACCEPT_RE = re.compile(r"prototype\s+accepted", re.I)
PROTOGATE_MARKER = "prototype accepted"  # condition in the card body/comment
PROTOGATE_TTL_S = 30 * 60                # cache of issue data for actions
# Recognise both the label and a [ux]/[feature] title prefix: issues may
# carry the prefix without the labels.
PROTOGATE_PREFIX_RE = re.compile(r"^\s*\[\s*(?:ux|feature)\s*\]", re.I)
# Issues that already have an implementation PR: a prototype after the
# implementation checks nothing. Filled from settings (issue numbers,
# comma-separated); empty by default.
PROTOGATE_EXEMPT = {int(n) for n in cfg_list("FACTORY_PROTOTYPE_EXEMPT_ISSUES")}


def protogate_is_feature(labels, title):
    """Label feature/ux OR a [ux]/[feature] prefix in the title.

    The [ux] prefix is also worn by bugs, and their live cards would fall
    under the gate on the first tick. So the prefix counts only for a
    non-bug: a bug fix has no prototype. A feature/ux label set by hand
    works unconditionally.
    """
    names = set(labels or [])
    if names & set(PROTOGATE_LABELS):
        return True
    if "bug" in names:                              # m9-bug
        return False
    return bool(PROTOGATE_PREFIX_RE.match(title or ""))   # m7-prefix
PROTOGATE_BA_RE = re.compile(r"^##\s*Before\s*/\s*After\s*$", re.I | re.M)
PROTOGATE_IMG_RE = re.compile(r"!\[[^\]]*\]\((\S+?)\)")


def protogate_issue_refs(title, body):
    """Issue numbers the card refers to (title or body)."""
    text = "%s\n%s" % (title or "", body or "")
    return {int(n) for n in re.findall(r"#(\d{1,5})", text)}


def protogate_owner_accepted(comments):
    """True: the issue has an owner comment "prototype accepted".
    The author is checked explicitly, not only the text."""
    for c in comments or []:
        user = c.get("user")
        if isinstance(user, dict):
            user = user.get("login")
        if ((user or "") == PROTOGATE_OWNER
                and PROTOGATE_ACCEPT_RE.search(c.get("body") or "")):
            return True
    return False


def protogate_proto_present(body):
    """Whether the issue body has a Before / After section with a prototype
    (two images for a UI, or mermaid)."""
    m = PROTOGATE_BA_RE.search(body or "")
    if not m:
        return False
    rest = (body or "")[m.end():]
    nxt = re.search(r"^##\s", rest, re.M)
    section = rest[:nxt.start()] if nxt else rest
    return (len(PROTOGATE_IMG_RE.findall(section)) >= 2
            or bool(re.search(r"```mermaid", section, re.I)))


def protogate_fetch_issues(now=None):
    """Open issues with feature/ux labels: {str(number): {..., accepted}}.

    Comments are fetched only for these issues: there are few labelled
    ones, and calling gh api for every open issue makes no sense. The cache
    lasts 30 minutes: an unblock must not lag by an hour.
    """
    now = now if now is not None else time.time()
    try:
        data = json.load(open(PROTOGATE_CACHE))
    except Exception:
        data = None
    if data and now - (data.get("ts") or 0) <= PROTOGATE_TTL_S:
        return data.get("issues") or {}
    raw = run(["gh", "issue", "list", "--repo", PROTOGATE_REPO,
               "--state", "open", "--limit", "200", "--json",
               "number,title,labels,body"], timeout=90)
    issues = {}
    for item in json.loads(raw or "[]"):
        labels = [l.get("name") for l in item.get("labels") or []]
        number = item["number"]
        if number in PROTOGATE_EXEMPT:                       # m8-exempt
            continue
        if not protogate_is_feature(labels, item.get("title")):   # m2-labels
            continue
        comments_raw = run(
            ["gh", "api", "repos/%s/issues/%d/comments?per_page=100"
             % (PROTOGATE_REPO, number)], timeout=60)
        comments = [{"user": (c.get("user") or {}).get("login"),
                     "body": c.get("body")}
                    for c in json.loads(comments_raw or "[]")]
        issues[str(number)] = {
            "number": number, "title": item.get("title") or "",
            "labels": labels, "body": item.get("body") or "",
            "accepted": protogate_owner_accepted(comments),
        }
    try:
        json.dump({"ts": now, "issues": issues}, open(PROTOGATE_CACHE, "w"))
    except OSError:
        pass
    return issues


def _protogate_board_cards(slug):
    """Live todo/ready/blocked cards with their comments (read only)."""
    db = os.path.join(BOARDS, slug, "kanban.db")
    if not os.path.exists(db):
        return []
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
    try:
        rows = list(con.execute(
            "select id, status, title, body from tasks"
            " where status in ('todo','ready','blocked')"))
        cards = []
        for tid, status, title, body in rows:
            cmts = [r[0] for r in con.execute(
                "select body from task_comments where task_id = ?",
                (tid,))]
            cards.append({"id": tid, "status": status, "title": title or "",
                          "body": body or "", "comments": cmts})
        return cards
    except sqlite3.Error:
        return []
    finally:
        con.close()


def prototype_gate(slug, apply=None, issues=None, cards=None, hold_path=None):
    """Invariant "prototype before implementation", sides (a) and (b).

    Returns findings [(key, rank, text)] in the format of check(). Actions
    (block/unblock) run only with apply=True; by default apply=False while
    the hold file exists. issues and cards can be injected: tests and
    dry-run work without gh and without a board.
    """
    if slug != PROTOGATE_BOARD:
        return []
    if apply is None:
        apply = not os.path.exists(hold_path or PROTOGATE_HOLD)
    if issues is None:
        issues = protogate_fetch_issues()
    issues = {int(k): v for k, v in (issues or {}).items()
              if int(k) not in PROTOGATE_EXEMPT
              and protogate_is_feature(v.get("labels"), v.get("title"))}
    if cards is None:
        cards = _protogate_board_cards(slug)
    findings = []
    to_block, to_unblock, held_ok = [], [], []
    for card in cards or []:
        refs = protogate_issue_refs(card.get("title"),
                                   card.get("body")) & set(issues)
        if not refs:
            continue
        waiting = sorted(n for n in refs if not issues[n].get("accepted"))
        text = (card.get("body") or "") + "\n" + "\n".join(
            c if isinstance(c, str) else c.get("body") or ""
            for c in card.get("comments") or [])
        has_cond = PROTOGATE_MARKER in text
        if card.get("status") in ("todo", "ready") and waiting:
            to_block.append((card, waiting))
        elif card.get("status") == "blocked" and has_cond:
            if waiting:
                held_ok.append((card, waiting))
            else:
                to_unblock.append((card, refs))
    if to_block:
        lines = ["  PROTOTYPE GATE, (a): implementation without an accepted"
                 " prototype: %d." % len(to_block)]
        for card, waiting in to_block:
            lines.append("    %s [%s] issue #%s %s"
                         % (card["id"], card.get("status"),
                            ",".join(map(str, waiting)),
                            (card.get("title") or "")[:66]))
            if apply:
                reason = ("Prototype gate: the prototype for issue #%s is"
                          " not accepted by the owner. Release condition:"
                          " prototype accepted (an owner comment in the"
                          " issue with the words \"prototype accepted\")."
                          % ",".join(map(str, waiting)))
                run([HERMES, "kanban", "--board", slug, "block", card["id"],
                     reason, "--kind", "needs_input"], timeout=90)
        findings.append(("protogate-block:%s"
                         % ",".join(c["id"] for c, _ in to_block),
                         1, "\n".join(lines)))
    if to_unblock:
        lines = ["  PROTOTYPE GATE, (b): prototype accepted, cards are being"
                 " lifted: %d." % len(to_unblock)]
        for card, refs in to_unblock:
            lines.append("    %s issue #%s %s"
                         % (card["id"], ",".join(map(str, sorted(refs))),
                            (card.get("title") or "")[:66]))
            if apply:
                run([HERMES, "kanban", "--board", slug, "unblock",
                     card["id"]], timeout=90)                       # m3-unblock
                run([HERMES, "kanban", "--board", slug, "comment",
                     card["id"],
                     "Prototype gate: the prototype was accepted by the owner"
                     " (issue #%s), block lifted by the watchdog. Implement"
                     " in a separate branch from %s, not on top of the"
                     " prototype branch."
                     % (",".join(map(str, sorted(refs))),
                        tuple(cfg_list("FACTORY_BASE_BRANCHES", ("main",)))[0]),
                     "--author", "watchdog"], timeout=90)
        findings.append(("protogate-unblock:%s"
                         % ",".join(c["id"] for c, _ in to_unblock),
                         1, "\n".join(lines)))
    if held_ok and not to_block and not to_unblock:
        findings.append(("protogate-held:%d" % len(held_ok), 2,
                         "  PROTOTYPE GATE: correctly held in blocked"
                         " awaiting a prototype: %d." % len(held_ok)))
    if not apply and (to_block or to_unblock):
        findings.append(("protogate-hold:1", 2,
                         "  PROTOTYPE GATE HOLD: gate actions suppressed,"
                         " dry-run. To lift: the owner's \"accepted\"."))
    return findings


def protogate_dry_run():
    """Dry run of the prototype gate: read only (gh, board), no changes.

    Answers three owner questions: which open issues of the milestones
    carry feature/ux labels, how many of them have a Before/After with a
    prototype, and which implementation cards would go to blocked.
    """
    now = time.time()
    # Active milestones are the open ones from GitHub, no names in the code
    # (same approach as the backlog-owner gate).
    ms_map = backlog_open_milestones() or {}
    ms_names = set(ms_map.values())
    raw = run(["gh", "issue", "list", "--repo", PROTOGATE_REPO,
               "--state", "open", "--limit", "200", "--json",
               "number,title,labels,body,milestone"], timeout=90)
    labeled, ms_open = [], 0
    for item in json.loads(raw or "[]"):
        ms = (item.get("milestone") or {}).get("title") or ""
        in_ms = ms in ms_names
        if in_ms:
            ms_open += 1
        labels = [l.get("name") for l in item.get("labels") or []]
        hit = [l for l in labels if l in PROTOGATE_LABELS]
        if hit:
            labeled.append((item["number"], item.get("title") or "",
                            ",".join(hit), in_ms,
                            protogate_proto_present(item.get("body") or "")))
    print("Open issues in open milestones (%s): %d"
          % ("/".join(sorted(ms_names)) or "-", ms_open))
    print("Of them with feature/ux labels: %d" % len(labeled))
    for number, title, hit, in_ms, proto in labeled:
        print("  #%d [%s] milestone=%s prototype=%s %s"
              % (number, hit, "yes" if in_ms else "no",
                 "present" if proto else "MISSING", title[:64]))
    if not labeled:
        print("  (none: the feature/ux labels themselves are absent from the"
              " repo, ux lives in the [ux] title prefix, features carry"
              " enhancement)")
    print("Candidates by meaning (feature/ux labels were not set):")
    cand = 0
    for item in json.loads(raw or "[]"):
        ms = (item.get("milestone") or {}).get("title") or ""
        if ms not in ms_names:
            continue
        title = item.get("title") or ""
        labels = [l.get("name") for l in item.get("labels") or []]
        if "enhancement" in labels or title.lower().startswith("[ux]"):
            cand += 1
            print("  #%d [%s] %s" % (item["number"],
                                     ",".join(labels) or "-", title[:64]))
    if not cand:
        print("  (no candidates in the milestones)")
    issues = protogate_fetch_issues(now=now)
    for slug in board_slugs():
        if slug != PROTOGATE_BOARD:
            continue
        for key, _, text in prototype_gate(slug, apply=False,
                                           issues=issues):
            print("[%s] %s\n%s" % (slug, key, text))


# The dispatcher stands while ready is non-empty: N runs in a row confirm
# it (an hourly watchdog makes that 3 hours).
STUCK_TICKS = 3


def classify_finding(key):
    """('stop'|'owner'|'digest') for a check(slug) finding.

    Owner decision: push only for a real stop or an owner action, the rest
    goes to the evening report. Both owner and stop are pushed; the header
    "KANBAN NEEDS ATTENTION" is added only when there are owner items (a
    signal "owner action needed", not a statement of a stop); stop is the
    line "Dispatcher is stopped: ..." inside the push; digest is silence in
    Telegram, a line in the daily digest.
    """
    if ":diag:" in key:
        kind = key.rsplit(":", 1)[1]
        if kind in ("stuck_in_blocked", "block_unblock_cycling"):
            return "owner"
        if kind in ("stranded_in_ready", "repeated_failures"):
            return "stop"
        return "digest"
    if ":silent:" in key or ":stalled:" in key or ":nooutput:" in key \
            or key.startswith("massfail:"):
        return "stop"
    # The factory idle for more than an hour while the backlog of the
    # admissible band is not empty is an owner finding (one, keyed per day).
    if ":idlefactory:" in key:
        return "owner"
    if ":cap:" in key or ":budget:" in key or ":budget-gate:" in key:
        return "owner"
    if key.startswith(("staleblock:", "samepr:", "dup:", "dup2:", "twin")):
        return "owner"
    # A live run on the tail of a closed issue burns the quota; only the
    # owner can stop it.
    if key.startswith("closedtail-run:"):
        return "owner"
    # An owner question open for more than a day is an owner action, not
    # routine.
    if key.startswith("ownerq:"):
        return "owner"
    # A release blocker without an executor is an owner action (threshold
    # one cycle, not a day). REPEAT_AFTER holds the repeat.
    if ":admission:blk-no-executor:" in key:
        return "owner"
    # A delivered but still open issue is a question to the owner ("what
    # remains?"), not a task for an executor.
    if ":admission:delivered-open:" in key:
        return "owner"
    # A closed PR card whose PR is not merged is an owner action (the
    # merge); the signal is held until the merge.
    if ":owner-merge-hold:" in key:
        return "owner"
    # A closed PR card without WCFY on an accepted head: the owner faces a
    # PR that must not be merged without WCFY.
    if ":owner-wcfy-hold:" in key:
        return "owner"
    # A recorded race (both links in progress on one branch) needs
    # coordination, not routine.
    if ":serial:" in key and key.endswith(":race"):
        return "owner"
    return "digest"


def main():
    state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    now = time.time()
    push, digest, seen_keys, dropped = [], [], [], 0
    push_stop = False
    push_owner = False
    push_stop_count = 0
    # Sources that run before the per-board loop must pass the same
    # REPEAT_AFTER filter as the check(slug) findings below. Without it
    # holdout_guard sent the owner the same leak every hour: the keys were
    # written to state but never read, while the docstring promised "no
    # more than once a day". All of these sources are routine (arms,
    # parking, tree leaks), so they go to the digest, not to the push.
    gate = max_in_progress_gate(state, now)
    stale = code_newer_than_gateway()
    for pair_pre in (gate, stale):
        if pair_pre and now - state.get(pair_pre[0], 0) > REPEAT_AFTER:
            digest.append(pair_pre[1])
            seen_keys.append(pair_pre[0])
    try:
        holdout_findings = list(holdout_guard(now))
    except Exception as exc:
        holdout_findings = []
        digest.append("  [holdout] Leak detector failed: %s: %s"
                      % (type(exc).__name__, exc))
    for key9, text9 in holdout_findings:
        if now - state.get(key9, 0) > REPEAT_AFTER:
            digest.append(text9)
            seen_keys.append(key9)
    ticks_changed = False
    for slug in board_slugs():
        # The subscription sweep was rolled back (see the comment at
        # pr_state): event delivery by subscriptions is not filtered by
        # kind and would ping the owner on every board event. Problems are
        # still delivered by the watchdog report below.
        # The conflict-debt gate runs before the general check: feature
        # parking must fire even when check(slug) fails on a broken base.
        try:
            gate5 = conflict_debt_gate(slug, state, now)
        except Exception as exc:
            gate5 = ("mergeflow-debtgate-fail:%s" % slug,
                     "  [%s] Conflict-debt gate failed: %s: %s"
                     % (slug, type(exc).__name__, exc))
        if gate5:
            digest.append(gate5[1])
            seen_keys.append(gate5[0])
        try:
            for key6, text6 in backlog_gate(slug, state, now=now):
                digest.append(text6)
                seen_keys.append(key6)
        except Exception as exc:
            digest.append("  [%s] Backlog gate failed: %s: %s"
                          % (slug, type(exc).__name__, exc))
        # A stack of rework on top of a live PR without an owner decision.
        # Idempotency lives inside the gate (a marker comment on the PR),
        # so the finding goes straight to the report, like the conflict-debt
        # gate.
        try:
            gate32 = stacked_rework_gate(slug)
        except Exception as exc:
            gate32 = [("stacked32-fail:%s" % slug, 1,
                       "  [%s] Stacked-rework gate failed: %s: %s"
                       % (slug, type(exc).__name__, exc))]
        for key32, rank32, text32 in gate32:
            digest.append(text32)
            seen_keys.append(key32)
        # False needs_input blocks on agent steps (update-branch / waiting
        # for CI / rerun): the card goes back to the executor with a
        # comment, only the merge is left to the owner.
        try:
            gate50 = needs_input_tech_gate(slug)
        except Exception as exc:
            gate50 = [("r50tech-fail:%s" % slug, 1,
                       "  [%s] False needs_input gate failed: %s: %s"
                       % (slug, type(exc).__name__, exc))]
        for key50, rank50, text50 in gate50:
            digest.append(text50)
            seen_keys.append(key50)
        # A permanent initial_status block on a child of a closed root (the
        # plan was ACCEPTED, but the Hermes core treats blocked as sticky
        # and never lifts children created with --initial-status blocked).
        try:
            gate63 = initial_status_unstick_gate(slug)
        except Exception as exc:
            gate63 = [("r63unstick-fail:%s" % slug, 1,
                       "  [%s] initial_status block gate failed: %s: %s"
                       % (slug, type(exc).__name__, exc))]
        for key63, rank63, text63 in gate63:
            digest.append(text63)
            seen_keys.append(key63)
        # Closing an issue as a duplicate rebinds the live chain to the open
        # original BEFORE the closed-tail gate runs (otherwise the tail gate
        # would retire the whole chain an hour later).
        try:
            for key57, rank57, text57 in closed_duplicate_rebind(slug):
                if not digest or not digest[-1].startswith("["):
                    digest.append("[%s]" % slug)
                digest.append(text57)
                seen_keys.append(key57)
        except Exception as exc:
            digest.append("  [%s] Duplicate rebind failed: %s: %s"
                          % (slug, type(exc).__name__, exc))
        # Tails of closed issues. Retires the links of the chain of an issue
        # the owner closed (archive with a marker comment); a running card
        # on the tail goes to the owner (the run burns the quota).
        try:
            for key52, rank52, text52 in closed_issue_tail_gate(slug):
                if key52.startswith("closedtail-run:"):
                    push_owner = True
                    push.append("[%s]\n%s" % (slug, text52))
                else:
                    if not digest or not digest[-1].startswith("["):
                        digest.append("[%s]" % slug)
                    digest.append(text52)
                seen_keys.append(key52)
        except Exception as exc:
            digest.append("  [%s] Closed-issue tail gate failed: %s: %s"
                          % (slug, type(exc).__name__, exc))
        try:
            found = check(slug, state)
        except sqlite3.Error as exc:
            # A broken schema or a truncated db on one board must not kill
            # the whole run: the other boards would be left unsupervised
            # (an archived board with a 0-byte kanban.db once crashed main()
            # in graph() before the remaining boards were processed).
            # Supervision impossible is a real stop: a stop-class push,
            # deduplicated for a day by a synthetic key (a repeat no more
            # than once a day).
            key = "dbread:%s" % slug
            if now - state.get(key, 0) > REPEAT_AFTER:
                push.append("[%s]\n  kanban.db is not readable: %s: %s"
                            % (slug, type(exc).__name__, exc))
                push_stop = True
                push_stop_count += 1
                seen_keys.append(key)
            continue
        try:
            # Hidden-zone paths of the tester in live coder/reviewer cards
            # are cleaned before a worker picks the card up.
            found = holdout_card_lint(slug) + found
        except Exception as exc:
            digest.append("  [%s] Hidden-zone path lint of cards failed:"
                          " %s: %s" % (slug, type(exc).__name__, exc))
        for key, _rank, text in found:
            cls = classify_finding(key)
            kind = key.rsplit(":", 1)[1] if ":diag:" in key else ""
            if cls == "stop" and kind == "stranded_in_ready":
                # The dispatcher stands while ready is non-empty: confirm
                # STUCK_TICKS runs in a row, otherwise every dispatcher
                # pause gives a false push.
                ticks = state.get("ticks:" + key)
                ticks = ticks + 1 if isinstance(ticks, int) else 1
                state["ticks:" + key] = ticks
                state["tick-at:" + key] = now
                ticks_changed = True
                if ticks < STUCK_TICKS:
                    continue
                state["ticks:" + key] = 0
                if now - state.get(key, 0) <= REPEAT_AFTER:
                    continue  # already pushed within a day, stay silent
            elif now - state.get(key, 0) <= (
                    STALEBLOCK_REPEAT if key.startswith("staleblock:")
                    else REPEAT_AFTER):
                continue
            bucket = push if cls in ("stop", "owner") else digest
            if cls == "stop":
                push_stop = True
                push_stop_count += 1
            elif cls == "owner":
                push_owner = True
            if sum(len(x) for x in bucket) + len(text) > MAX_CHARS:
                if bucket is push:
                    dropped += 1
                continue
            if bucket is digest and (not digest
                                     or not digest[-1].startswith("[")):
                digest.append("[%s]" % slug)
            bucket.append(text)
            seen_keys.append(key)
    # An empty report is a quiet tick (the standard no-agent cron idiom).
    # The header "KANBAN NEEDS ATTENTION" is added only when the push has an
    # item that needs an owner action; it is a signal "action needed", not a
    # statement of a stop. A real dispatcher stop (stop class) is printed as
    # a separate line "Dispatcher is stopped: ..." inside the report,
    # routine goes to the digest.
    if not seen_keys and not ticks_changed:
        return
    if dropped:
        push.append("  ... %d more item(s) did not fit: hermes kanban diag"
                    % dropped)
    if push:
        parts = []
        if push_owner:
            parts.append("KANBAN NEEDS ATTENTION")
        if push_stop:
            parts.append("Dispatcher is stopped: %d pipeline-stop finding(s)"
                         " (details below)" % push_stop_count)
        parts.extend(push)
        print("\n".join(parts))
    if digest:
        from cron_quiet import digest_add
        digest_add("board-watchdog", "\n".join(digest))
    for key in seen_keys:
        state[key] = now
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump(state, open(STATE, "w"))


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        # Check of the cron --> telegram delivery. By design the script is
        # silent while the boards are clean: empty stdout delivers nothing,
        # so the delivery path is checked with an explicit line, not with
        # "ran ok" in the cron list.
        print("BOARD-WATCHDOG SELF-TEST: the watchdog lives in the root "
              "profile, delivery works, %s" % datetime.now(timezone.utc).isoformat())
    elif "--lint-dry-run" in sys.argv:
        # List of invariant I1-I6 violations right now, without edits and
        # without writing state. Output: one section per board.
        total = 0
        for slug in board_slugs():
            print("[%s]" % slug)
            try:
                findings = board_lint(slug, dry_run=True)
            except Exception as exc:
                print("  lint failed: %s: %s" % (type(exc).__name__, exc))
                continue
            for _, rank, text in sorted(findings, key=lambda x: x[1]):
                print(text)
            total += len(findings)
        print("TOTAL violations (dry-run): %d" % total)
    elif "--rework-gate-dry-run" in sys.argv:
        # What the size gate would intercept right now, and which burnouts
        # over 30 days need a progress comment. Without edits and without
        # writing state.
        gate_total = burn_total = 0
        for slug in board_slugs():
            print("[%s]" % slug)
            try:
                hits = rework_oversize_scan(slug)
            except Exception as exc:
                print("  gate scan failed: %s: %s" % (type(exc).__name__, exc))
                hits = []
            for tid, title, blocks, nlines, prio in hits:
                print("  GATE %s BLOCK=%d lines=%d p%d %s"
                      % (tid, blocks, nlines, prio, (title or "")[:60]))
            print("  the gate would intercept now: %d" % len(hits))
            gate_total += len(hits)
            try:
                pending = burnout_progress_scan(slug, window_s=30 * 24 * 3600)
            except Exception as exc:
                print("  progress scan failed: %s: %s"
                      % (type(exc).__name__, exc))
                pending = []
            for p in pending:
                print("  COMMENT %s run #%d %s"
                      % (p["task_id"], p["run_id"], (p["error"] or "")[:44]))
            print("  burnouts without a progress comment over 30 days: %d"
                  % len(pending))
            burn_total += len(pending)
        print("TOTAL (dry-run): gate %d, progress comment %d"
              % (gate_total, burn_total))
    elif "--mergeflow-dry-run" in sys.argv:
        # What the conflict-debt gate would do right now. No board edits,
        # no state writes: count and print.
        now = time.time()
        count, age, fresh = read_debt_cache(now)
        print("DEBT CACHE: count=%s age=%s fresh=%s (TTL %d h)"
              % (count, "%.1f h" % (age / 3600) if age is not None else "?",
                 "yes" if fresh else "no", DEBT_CACHE_TTL_S // 3600))
        state = json.load(open(STATE)) if os.path.exists(STATE) else {}
        total = 0
        for slug in board_slugs():
            out = conflict_debt_gate(slug, state, now=now, apply=False)
            if out is None and slug != cfg("FACTORY_BOARD", "main"):
                continue
            print("[%s]" % slug)
            if out is None:
                print("  gate is silent: no debt, no data, or everything"
                      " that was there is already parked")
                continue
            total += 1
            print(out[1])
        print("TOTAL (dry-run): gates fired %d" % total)
    elif "--backlog-dry-run" in sys.argv:
        # What the backlog gate would do right now. Read only: the board
        # and state are not touched, the hold does not interfere (no
        # mutations, so reading through the hold is safe).
        now = time.time()
        state = json.load(open(STATE)) if os.path.exists(STATE) else {}
        total = 0
        for slug in board_slugs():
            if slug != cfg("FACTORY_BOARD", "main"):
                continue
            entries = backlog_gate(
                slug, json.loads(json.dumps(state)), now=now,
                apply=False, ignore_hold=True)
            for key, text in entries:
                print("[%s] %s\n%s" % (slug, key, text))
                total += 1
        print("TOTAL (backlog gate dry-run): entries %d" % total)
    elif "--prototype-dry-run" in sys.argv:
        # Dry run of the prototype gate. Read only: the board and state are
        # not touched, the hold does not interfere (no mutations).
        protogate_dry_run()
    elif "--rework-gate-only" in sys.argv:
        # A fast pass of the size gate and its neighbours, for a cron every
        # 5 minutes. It closes the race with fresh rework cards (the
        # dispatcher ticks every ~6 minutes, the full watchdog once an
        # hour). It also does the automatic return on a "fits-in-one-run"
        # verdict (the decomposition is closed, the parent is stuck at the
        # architect), the dedup of verdict items (a verdict card subtracts
        # items covered by a live card of the same PR before the dispatcher
        # hands it to a worker), and the merge-unblocker priority (a card
        # with only merge formalities on a PR accepted by code is raised to
        # p90). Card recreations and automatic gate actions are routine:
        # they go to the daily digest for metrics-evening, and Telegram
        # stays silent (empty stdout).
        from cron_quiet import digest_add
        _dg = []
        for slug in board_slugs():
            try:
                for _, _, text in rework_oversize_gate(slug):
                    _dg.append("[%s]\n%s" % (slug, text))
                for _, _, text in rework_gate_auto_return(slug):
                    _dg.append("[%s]\n%s" % (slug, text))
                for _, _, text in verdict_dedup_gate(slug):
                    _dg.append("[%s]\n%s" % (slug, text))
                for _, _, text in merge_unblocker_gate(slug):
                    _dg.append("[%s]\n%s" % (slug, text))
                # Rework of an open PR (rework or fix) goes up to p90
                # inside the active band, ahead of new work.
                for _, _, text in rework_priority_lift(slug):
                    _dg.append("[%s]\n%s" % (slug, text))
                # A live review arm (ready/todo) is retired when the stock
                # review lane already runs on the same PR and head, or a
                # verdict exists (race of arm creation against
                # request_review). Retirement applies in any status except
                # running; review AND WCFY arms on a head the PR has moved
                # away from are retired too.
                for _, _, text in review_arm_liveness_gate(slug):
                    _dg.append("[%s]\n%s" % (slug, text))
                # A stack of rework on top of a live PR without an owner
                # decision. A worker opens a stack within minutes, this pass
                # sets the marker and alerts before a review card is hung
                # on the stack.
                for _, _, text in stacked_rework_gate(slug):
                    _dg.append("[%s]\n%s" % (slug, text))
                # A stuck active_pr guard after an owner decision. The
                # dispatcher writes respawn_guarded every ~2 minutes and the
                # slots stand empty, so it belongs here and not only in the
                # hourly watchdog.
                for _, _, text in active_pr_handoff_gate(slug):
                    _dg.append("[%s]\n%s" % (slug, text))
                    _dg.append("[%s]\n%s" % (slug, text))
            except Exception as exc:
                # a gate failure needs investigation, the only push
                print("[%s] gate failed: %s: %s"
                      % (slug, type(exc).__name__, exc))
        if _dg:
            digest_add("board-watchdog-gates", "\n".join(_dg))
    elif "--stacked-rework-only" in sys.argv or "--stacked-rework-dry" in sys.argv:
        # A single pass of the stacked-rework gate. --stacked-rework-dry
        # only reads (sets no markers), for manual diagnosis and a positive
        # control on a live board.
        dry = "--stacked-rework-dry" in sys.argv
        for slug in board_slugs():
            try:
                for _, _, text in stacked_rework_gate(slug, apply=not dry):
                    print("[%s]\n%s" % (slug, text))
            except Exception as exc:
                print("[%s] stacked-rework failed: %s: %s"
                      % (slug, type(exc).__name__, exc))
    elif "--verdict-dedup-only" in sys.argv:
        # A single pass of the verdict-dedup gate (manual run and
        # diagnosis). Actions at once, output like --rework-gate-only.
        for slug in board_slugs():
            try:
                for _, _, text in verdict_dedup_gate(slug):
                    print("[%s]\n%s" % (slug, text))
            except Exception as exc:
                print("[%s] dedup failed: %s: %s"
                      % (slug, type(exc).__name__, exc))
    elif "--merge-unblocker-only" in sys.argv:
        # A single pass of the merge-unblocker gate (manual application to
        # a live board and diagnosis). Actions at once, output like
        # --rework-gate-only.
        for slug in board_slugs():
            try:
                for _, _, text in merge_unblocker_gate(slug):
                    print("[%s]\n%s" % (slug, text))
            except Exception as exc:
                print("[%s] merge-unblocker failed: %s: %s"
                      % (slug, type(exc).__name__, exc))
    elif "--owner-merge-only" in sys.argv:
        # A single pass of the close gate "remaining: owner merge" (manual
        # application and diagnosis).
        for slug in board_slugs():
            try:
                for _, _, text in owner_merge_hold_gate(slug):
                    print("[%s]\n%s" % (slug, text))
            except Exception as exc:
                print("[%s] owner-merge-hold failed: %s: %s"
                      % (slug, type(exc).__name__, exc))
    elif "--needs-input-tech-only" in sys.argv:
        # A single pass of the gate for false needs_input blocks (manual
        # application and diagnosis). Actions at once, output like
        # --rework-gate-only.
        for slug in board_slugs():
            try:
                for _, _, text50 in needs_input_tech_gate(slug):
                    print("[%s]\n%s" % (slug, text50))
            except Exception as exc:
                print("[%s] needs-input-tech failed: %s: %s"
                      % (slug, type(exc).__name__, exc))
    elif "--active-pr-only" in sys.argv:
        # A single pass of the stuck active_pr guard gate (manual
        # application and diagnosis). Actions at once, output like
        # --rework-gate-only.
        for slug in board_slugs():
            try:
                for _, _, text in active_pr_handoff_gate(slug):
                    print("[%s]\n%s" % (slug, text))
            except Exception as exc:
                print("[%s] active-pr failed: %s: %s"
                      % (slug, type(exc).__name__, exc))
    elif "--closed-dup-rebind-only" in sys.argv:
        # A single pass of the duplicate rebind (manual application to a
        # live board and diagnosis). Actions at once, output like
        # --rework-gate-only.
        for slug in board_slugs():
            try:
                for _key, _rank, text in closed_duplicate_rebind(slug):
                    print("[%s]\n%s" % (slug, text))
            except Exception as exc:
                print("[%s] closed-dup-rebind failed: %s: %s"
                      % (slug, type(exc).__name__, exc))
    elif "--initial-status-unstick-only" in sys.argv or \
            "--initial-status-unstick-dry" in sys.argv:
        # A single pass of the gate for permanent initial_status blocks
        # (manual application to a live board and diagnosis). The dry
        # variant only reads (no unblock), for a positive control.
        dry = "--initial-status-unstick-dry" in sys.argv
        for slug in board_slugs():
            try:
                for _, _, text in initial_status_unstick_gate(
                        slug, apply=not dry):
                    print("[%s]\n%s" % (slug, text))
            except Exception as exc:
                print("[%s] initial-status-unstick failed: %s: %s"
                      % (slug, type(exc).__name__, exc))
    else:
        main()

