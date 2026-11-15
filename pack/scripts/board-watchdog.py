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
  6. ready without an assignee: a card with an empty assignee, or an assignee
     without a live profile, for longer than 30 minutes. The dispatcher never
     spawns it (skipped_unassigned / skipped_nonspawnable) and nobody caught
     such a stop before: owner review cards waited 30 hours for an assignee.
     The gate beacon is excluded from the check by construction: the watchdog
     blocks it with needs_input itself when it creates it (_park_beacon);
  6b. review layer: an open PR whose head is older than 30 minutes with no
     verdict on that sha and no live reviewer card gets a card from the
     watchdog (key reviewer-pr<N>-<head7>, p100); while such heads exist,
     ready cards below p50 are parked in scheduled and come back when the
     layer is empty;
  7. subscription cleanup: card events (crashed, timed_out, gave_up, blocked,
     completed, review) are delivered by the stock gateway notifier through
     subscriptions; the watchdog subscribes the owner to open cards created
     without a session anchor (crons, gates); the report carries only what
     the events do not: diag aggregates (thresholds, not transitions),
     silence, a dead dispatcher, gates.

Lessons for future watchdog rules (grows over time):
  - the absence of movement on a card that waits for a dependency or an owner
    decision is not a sign that it went stale: a todo card with an open parent
    or a live child is "without movement" by definition; auto-archive may touch
    only cards without live links (parents and children not in done/archived;
    a scheduled parking by an owner gate is a live link);
  - the archive is terminal for the CLI (there is no kanban unarchive), the only
    supported way back is a dashboard PATCH status (drag out of the archive).
    Before a rule that archives, answer: how is it rolled back.

PR/issue numbers are pulled from the root cards, their state is asked from
GitHub (gh), and the plan is given from simple to complex: first what is
already ready to merge and unblocks the most, at the end what needs thinking.

Dedup: the same problem does not shout again more often than once a day.
"""
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
# watchdog rule count it the same way.
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

HOME = hermes_home()
BOARDS = os.path.join(HOME, "kanban", "boards")
STATE = os.path.join(HOME, "kanban", "watchdog-state.json")
HERMES = os.path.expanduser("~/.local/bin/hermes")
PROFILES_ROOT = os.path.join(HOME, "profiles")
# Mirror of hermes_constants._PROFILE_IDENTITY_MARKERS: what makes a profile
# different from a random folder under profiles/.
_PROFILE_IDENTITY_MARKERS = ("config.yaml", ".env", "SOUL.md",
                             "profile.yaml", "auth.json", "state.db")
UNASSIGNED_AFTER = 30 * 60   # ready without a spawnable assignee for longer than this: alert
SILENCE_HOURS = 6
REPEAT_AFTER = 24 * 3600
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

    # 6b. Review layer: open PRs without a verdict on the head get a reviewer
    #     card p100 (auto-arm), ready <p50 are parked until the layer is
    #     empty. gh or code errors do not bring down the other checks.
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

    # 6c-bis-2. Serialization of rework of one PR: live coder cards of one PR
    #           are linked in a chain, only cards of different PRs work in
    #           parallel (a race of pushes into one branch). A frequent pass:
    #           --rework-gate-only (cron every 5 minutes).
    try:
        found.extend(serial_rework_gate(slug))
    except Exception as exc:
        found.append(("%s:serial-rework:err" % slug, 1, (
            "    Rework serialization: the check failed (%s: %s). Retry "
            "in an hour." % (type(exc).__name__, exc))))

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
REVIEW_ARM_MAX_AGE_S = 14 * 24 * 3600   # old PRs are not auto-armed: separate hygiene
REVIEW_ARM_MAX_CREATE = 5               # guard against a storm of rebases
REVIEW_PARK_BELOW_P = 50
# Hold: while the file exists, new arms of branches A (repeat review of a
# pair) and B (WCFY) run in dry-run: no cards are created, the plan with the
# hold path goes into the findings. Removing the hold means "accepted by
# the owner".
PAIR_WCFY_HOLD = os.path.join(HOME, "cache", "pair-wcfy-hold")
REVIEW_PARK_MARKER = "review-layer hold"
# Pilot of "one PR = one card": a PR from such a branch goes through the
# regular review cycle on the executor's own card (request-review
# reviewer=reviewer, rework and WCFY there too). No auto-arm of a reviewer
# card and no parallel review lane is created for a pilot branch. The list
# is removed after the owner's decision on the pilot together with the
# auto-arm and serial_rework_gate. The table is filled from settings
# (branch names, comma-separated); the card id value is not used.
PILOT_EXEMPT_BRANCHES = dict.fromkeys(
    cfg_list("FACTORY_PILOT_EXEMPT_BRANCHES", ()), "")
_VERDICT_HEADER_RE = re.compile(
    r"(?im)^#{1,4}\s*(?:re[- ]?)?review\s+verdict\b")


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

# The rule "promises of the PR body and WCFY are pinned by a mutant-killing
# test" was loaded by the reviewer but applied partially: one mutant per
# fix line and a prose Calibration line ("- contract: every PR-body promise
# mapped ...") passed the Calibration gate and closed a PASS with two
# promises that had no mutants at all. A mechanical support of the form: a
# PASS verdict published from this date must carry, ANYWHERE in the
# comment, a table "promise / mutant / test / result", otherwise the verdict
# does not count, as with a missing Calibration. RETURN without the table is
# acceptable: a return does not accept the work, the error is in the safe
# direction. The position of the table in the comment is NOT critical: the
# goal of the gate is "a PASS without the promise/mutant/test/result table
# does not count", not "the table sits in a particular section". A table
# is recognised by a header row with promise and mutant in the same row.
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
    'Re-review verdict') that contains the full head sha or a prefix of at
    least 7 characters. A coder comment "pushed head X, awaiting verdict" is
    not a verdict: it has no verdict header.

    A verdict comment published since CALIBRATION_REQUIRED_SINCE without a
    '## Calibration' block does NOT count as a verdict: re-arm, as when
    there is no verdict. The most recent verdict on the head decides: older
    ones without the block are grandfathered. Formal reviews are the owner's
    verdicts, the requirement does not apply to them.
    """
    out = run(["gh", "pr", "view", str(number), "--repo", repo, "--json",
               "headRefOid,baseRefName,commits,reviews,comments"],
              timeout=90)
    try:
        data = json.loads(out)
    except Exception:
        return None
    head = data.get("headRefOid") or ""
    if not head:
        return None
    age = None
    commits = data.get("commits") or []
    if commits:
        committed = _iso_to_epoch((commits[-1] or {}).get("committedDate"))
        if committed is not None:
            age = max(0.0, time.time() - committed)
    info = _latest_verdict(data, head)
    # verdict: strict binding to the exact head (the main auto-arm).
    # info: the latest significant verdict of the PR (the owner on any head),
    # branches A/B live on it (WCFY is not created on top of a CR).
    verdict_head = _latest_verdict(data, head, owner_any_head=False)
    return {"head": head, "age": age, "verdict": verdict_head is not None,
            "info": info, "wcfy_at": _wcfy_latest(data),
            "base": data.get("baseRefName") or ""}


# Two verdict carriers compete by time. GitHub's formal review is placed by
# the owner (the bot cannot review a PR opened by the same App, so the
# reviewer writes a comment), and a live case mixed both: a reviewer PASS
# comment, then the owner's formal CR "code accepted, only WCFY left". The
# verdict class is taken from the verdict line: an essay below the header
# quotes old verdicts ("this closes the CHANGES REQUESTED of ..." inside an
# APPROVED), and a class taken from the whole body flips the verdict.
# A window of 240 characters turned out to be too small. A live verdict:
# "PASS on head `d4c776a...` (re-review after CHANGES REQUESTED on
# `0d272f9`; ...)": the mention of the past CR stands in the MOST deciding
# line, reject was checked first and flipped the PASS; branch B did not
# create a WCFY card and the PR with a PASS stood for 7 hours. Now the
# class is read only from the deciding line (_verdict_deciding_line), see
# below.
_VERDICT_REJECT_RE = re.compile(r"(?i)changes\s+requested")
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
    """accept/reject/other by the deciding line of a verdict.

    The class used to be taken from the first 240 characters of the body,
    reject first: a PASS of the second round that mentioned the past CR in
    parentheses on the deciding line ("PASS on head X (re-review after
    CHANGES REQUESTED on Y)") was read as reject. The decision of a verdict
    is read from its deciding line, not from mentions in history: a
    second-round review almost always refers to the past CR. Inside the
    deciding line the class is given by the word that stands further left:
    the verdict token opens the line in the reviewer's format, and the
    history comes to the right in parentheses. The reverse is symmetric: a
    CR that mentions a past APPROVE stays reject.
    """
    line = _verdict_deciding_line(body) or ""
    acc = _VERDICT_ACCEPT_WORD_RE.search(line)
    rej = _VERDICT_REJECT_RE.search(line)
    if acc and (not rej or acc.start() < rej.start()):
        return "accept"
    if rej:
        return "reject"
    return "other"


def _latest_verdict(data, head, owner_any_head=True):
    """The freshest valid verdict: dict(kind, at, body) or None.

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
            r.get("body") or ""))
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
        events.append((_iso_to_epoch(created), _verdict_kind(body), body))
        break
    if not events:
        return None
    events.sort(key=lambda e: (e[0] is not None, e[0] or 0))
    return {"kind": events[-1][1], "at": events[-1][0],
            "body": events[-1][2]}


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


CONVENTIONS_SKILL = cfg("FACTORY_CONVENTIONS_SKILL", "project-conventions")


def _review_arm_card(slug, number, head, age_h, key=None, rearm_of=None):
    """Creates a reviewer card on the exact head. The id of the new one or None.

    key: the dedup key. By default the base reviewer-pr<N>-<head7>; on a
    repeated arm after a card closed without a verdict a fresh key with the
    suffix -rN is passed (the base one is deduplicated against the done
    card). rearm_of: ids of earlier cards of the family for the card body.
    """
    key = key or "reviewer-pr%d-%s" % (number, head[:7])
    rearm_note = ""
    if rearm_of:
        rearm_note = (
            "\n\nRepeated arm: the previous card(s) %s were closed by the "
            "reviewer WITHOUT a verdict on this head. Key %s is fresh, there "
            "is no dedup against the done card. A verdict is mandatory "
            "before this card is closed."
            % (rearm_of, key))
    title = ("[reviewer] PR #%d (head %s): review of head without verdict "
             "(watchdog auto-arm)" % (number, head[:7]))
    body = (
        "PR #%d, exact head for the verdict: %s.\n\n"
        "Created by the watchdog automatically: the head has had no verdict "
        "for %.1f h and there was no live reviewer card (the executor card "
        "closed after the push without a reviewer card)."
        "%s\n\n"
        "Task: adversarial review of the exact head %s against the branch "
        "base: the diff since the last verdict, CI on the head, mergeable, "
        "consistency of the PR body with the code. Line conventions: in the "
        "system prompt (skill %s).\n\n"
        "Verdict: a comment in PR #%d with the header \"## Review verdict\" "
        "and the exact sha of the head, in English. Before the verdict read "
        "the last 10 entries of the misses journal of the adversarial-review "
        "skill and add a \"## Calibration\" block: a line for every "
        "applicable class of misses (contract, mutation, ui-input, "
        "pr-texts; N/A with a reason for inapplicable ones). The review "
        "layer does not count a verdict without the block: re-arm as when "
        "there is no verdict. The card is not closed without a verdict on "
        "this sha: done without a verdict in the PR is a downtime "
        "incident, the watchdog will see it and recreate the card."
        % (number, head, age_h, rearm_note, head[:7], CONVENTIONS_SKILL, number))
    body_path = None
    try:
        with tempfile.NamedTemporaryFile(
                "w", suffix=".md", encoding="utf-8", delete=False) as fh:
            fh.write(body)
            body_path = fh.name
        return _kanban_create(slug, title, body, "reviewer", 100, key,
                              ("serena-lsp", "adversarial-review"))
    finally:
        if body_path:
            try:
                os.unlink(body_path)
            except OSError:
                pass


def _kanban_create(slug, title, body, assignee, priority, key, skills=()):
    """hermes kanban create via body-file. The id of the new card or None."""
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


def _pair_rearm_card(slug, number, head, age_h, twin, key, rearm_of):
    """A reviewer card for a repeat review after a paired blocker is closed.

    A reviewer CR on the head of one PR of a pair referred to the twin's
    blocker ("push a new head on #N, then re-request review here"), the twin
    got a fix and a PASS, and nobody requested the repeat review: a PR with
    a verdict does not enter the main auto-arm, and the pair stood forever
    until the owner intervened manually.
    """
    rearm_note = ""
    if rearm_of:
        rearm_note = (
            "\n\nRepeated arm: the previous card(s) %s were closed without a "
            "verdict. Key %s is fresh, there is no dedup against the done "
            "card." % (rearm_of, key))
    title = ("[reviewer] PR #%d (head %s): re-review, paired blocker "
             "#%d closed (watchdog auto-arm)" % (number, head[:7], twin))
    body = (
        "PR #%d, exact head for the verdict: %s.\n\n"
        "Created by the watchdog automatically: the latest verdict on this "
        "head, CHANGES REQUESTED, referred to the paired blocker of the twin "
        "PR #%d, and the twin now has a reviewer PASS fresher than this "
        "verdict, that is the blocker is closed by the reviewer's own "
        "instruction (\"push a new head on #%d, then re-request review "
        "here\"). The head is %.1f h old and nobody requested a repeat "
        "review: without a card the pair would stand forever.%s\n\n"
        "Task: a repeat adversarial review of the exact head %s against the "
        "branch base, with the focus on the paired blocker: whether the "
        "twin's fix #%d really closes it, consistency of the pair "
        "(contract, mutations, texts), CI on the head, mergeable, "
        "consistency of the PR body with the code. Line conventions: in the "
        "system prompt (skill %s).\n\n"
        "Verdict: a comment in PR #%d with the header \"## Review verdict\" "
        "and the exact sha of the head, in English, the \"## Calibration\" "
        "block is mandatory (a verdict without the block does not count). "
        "The card is not closed without a verdict: the watchdog will see "
        "done without a verdict and recreate it."
        % (number, head, twin, twin, age_h, rearm_note, head[:7], twin,
           CONVENTIONS_SKILL, number))
    return _kanban_create(slug, title, body, "reviewer", 100, key,
                          ("serena-lsp", "adversarial-review"))


def _wcfy_arm_card(slug, number, head, wcfy_at, key, rearm_of):
    """A coder card for a fresh WCFY on the final head after a PASS.

    Priority 95: the last link before the merge, but below the p100 of
    reviewer cards, so that a repeat review goes before the final text.
    """
    rearm_note = ""
    if rearm_of:
        rearm_note = (
            "\n\nRepeated arm: the previous card(s) %s were closed without a "
            "published comment. Key %s is fresh."
            % (rearm_of, key))
    title = ("[coder] PR #%d (head %s): fresh \"What changes for you\" "
             "(watchdog auto-arm)" % (number, head[:7]))
    body = (
        "PR #%d, exact head: %s.\n\n"
        "Created by the watchdog automatically: the head carries a reviewer "
        "PASS, but there is no fresh \"What changes for you\" for it (the "
        "latest WCFY comment: %s, the head is newer than it). A stale WCFY "
        "describes an old form of the change."
        "%s\n\n"
        "Task: publish in PR #%d a NEW comment with the header exactly "
        "\"## What changes for you\" (English): what changes for the user, "
        "what proves it (command output, screenshot), what was run; bind it "
        "to the exact sha of the head %s. Editing only the PR body does not "
        "count: an edit of the body has no date, the machine freshness check "
        "(the board-watchdog review layer) goes by dated comments, with a "
        "marker shared with pr-hygiene. The card is closed after the "
        "comment is published."
        % (number, head, _fmt_utc(wcfy_at), rearm_note, number, head[:7]))
    return _kanban_create(slug, title, body, "coder", 95, key,
                          ("pr-deliverable",))


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


def open_issue_states(repo):
    """Open issues of the repo: number -> (milestone title, label set)."""
    out = run(["gh", "issue", "list", "--repo", repo, "--state", "open",
               "--limit", "300", "--json", "number,milestone,labels"],
              timeout=90)
    try:
        parsed = json.loads(out)
    except Exception:
        return None
    states = {}
    for it in parsed:
        labels = {lab.get("name") for lab in it.get("labels") or []}
        milestone = (it.get("milestone") or {}).get("title")
        states[it.get("number")] = (milestone, labels)
    return states


def open_pr_issues(repo, states):
    """Open PRs of the repo: number -> issue numbers of the body, live in states.

    For the service exception: a PR "against an approved issue" if the PR
    body references at least one issue with a milestone.
    """
    out = run(["gh", "pr", "list", "--repo", repo, "--state", "open",
               "--limit", "100", "--json", "number,body"], timeout=90)
    try:
        parsed = json.loads(out)
    except Exception:
        return None
    live = set(states)
    res = {}
    for p in parsed:
        nums = {int(n) for n in re.findall(r"#(\d{1,5})\b",
                                           p.get("body") or "")}
        res[p.get("number")] = sorted(nums & live)
    return res


def admission_gate(slug):
    """Admission gate (white list).

    A card is admitted if it (itself or an ancestor by task_links) has a
    binding to an open issue with a milestone, or it is a service card of an
    open PR against an approved issue, or it has the infrastructure marker.
    A binding = an "issue-link: #N" comment (survives specify) or a live
    reference #N in title/body. A non-admitted card in ready/todo/review is
    taken off the queue by the stock schedule with a marker comment
    (idempotent); running is not preempted, only a finding; blocked is not
    touched (it already waits for a human). While references are live in the
    body, the gate stamps an issue-link comment so that specify cannot erase
    the binding.
    """
    repo = board_repo(slug)
    if not repo:
        return []
    states = open_issue_states(repo)
    if states is None:
        return []                     # gh failure: retry in an hour
    pr_issues = open_pr_issues(repo, states)
    if pr_issues is None:
        return []
    db = os.path.join(BOARDS, slug, "kanban.db")
    if not os.path.exists(db):
        return []
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
    live = list(con.execute(
        "SELECT id, title, body, status, priority FROM tasks "
        "WHERE status NOT IN ('done','archived')"))
    done_refs = list(con.execute(
        "SELECT title, body FROM tasks WHERE status = 'done' "
        "AND completed_at > ?", (time.time() - 14 * 24 * 3600,)))
    comments = {}
    for tid, cbody in con.execute("SELECT task_id, body FROM task_comments"):
        comments.setdefault(tid, []).append(cbody)
    parents = {}
    for pid, cid in con.execute(
            "SELECT parent_id, child_id FROM task_links"):
        parents.setdefault(cid, []).append(pid)
    con.close()
    live_ids = {row[0] for row in live}
    titles = {row[0]: row[1] or "" for row in live}
    bodies = {row[0]: row[2] or "" for row in live}

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
        seen, stack = set(), list(parents.get(tid, []))
        while stack:
            p = stack.pop()
            if p in seen or p not in live_ids:
                continue
            seen.add(p)
            stack.extend(parents.get(p, []))
        return seen

    findings = []
    refs_live = set()
    for t_title, t_body in done_refs:
        refs_live.update(refs_in({"title": t_title or "",
                                  "body": t_body or ""}))
    for task_id, title, body, status, prio in live:
        own = set(refs_in({"title": title or "", "body": body or ""}))
        nums = own | marker_links(task_id)
        for anc in ancestors(task_id):
            nums |= marker_links(anc)
            nums |= set(refs_in({"title": titles[anc],
                                 "body": bodies[anc]}))
        refs_live.update(own)
        issues = {n for n in nums if n in states}
        prs = {n for n in nums if n in pr_issues}
        if any(INFRA_MARKER in cbody for cbody in comments.get(task_id, ())):
            continue
        # a stable binding: stamp issue-link while references are live in the
        # body (blocked is not stamped: the card is already with a human)
        stamp = own & set(states)
        if stamp and status != "blocked" \
                and not (marker_links(task_id) & stamp):
            run([HERMES, "kanban", "--board", slug, "comment", task_id,
                 ISSUE_LINK_MARKER + " %s (automatic from the body, kept by "
                 "the admission gate, because specify and the decomposer rewrite the body)"
                 % " ".join("#%d" % n for n in sorted(stamp))], timeout=90)
        approved = sorted(n for n in issues if states[n][0])
        svc = sorted(n for n in prs
                     if any(states[i][0] for i in pr_issues[n]))
        if approved or svc:
            continue
        if issues:
            why = ", ".join("#%d without a milestone" % n for n in sorted(issues))
        elif prs:
            why = ", ".join("PR #%d against an unapproved issue" % n
                            for n in sorted(prs))
        else:
            why = "no binding to an issue of the repository"
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
    approved = [n for n, (milestone, labels) in sorted(states.items())
                if "hermes" in labels
                and "wait-for-triage" not in labels
                and milestone
                and n not in refs_live]
    if approved:
        findings.append(("%s:admission:unclaimed:%s" % (
            slug, ",".join(str(n) for n in approved)), 1, (
            "    Approved issues without a card: %s. Create a card "
            "(the owner assigned a milestone)."
            % ", ".join("#%d" % n for n in approved))))
    return findings


def review_layer_check(slug):
    """Review layer: auto-arm reviewer cards, park ready cards below p50.

    Returns findings; performs the actions immediately (like idempotency_gate).
    gh errors on individual PRs are skipped silently: the hourly run retries.
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
    # Pilot branches are never armed (see PILOT_EXEMPT_BRANCHES).
    pilot_numbers = {p["number"] for p in parsed
                     if p.get("headRefName") in PILOT_EXEMPT_BRANCHES
                     and not p.get("isDraft")}
    # A draft PR is not armed. The numbers filter above prevents a NEW card
    # for a draft; here we retire cards that already exist, because the
    # owner can turn a PR into a draft after it was armed.
    draft_numbers = {p["number"] for p in parsed if p.get("isDraft")}
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
    # with the same key is DEDUPLICATED against it (dedup catches any
    # non-archived card, including done): no new card would appear, the PR
    # would stay without a verdict and the watchdog would be silent. So the
    # keyed family is collected from done cards too.
    done_keyed = list(con.execute(
        "SELECT id, idempotency_key FROM tasks WHERE status = 'done' "
        "AND (idempotency_key LIKE 'reviewer-pr%' "
        "     OR idempotency_key LIKE 'coder-pr%-wcfy-%')"))
    # In the normal review cycle the executor card often does NOT name the PR
    # in its body: the number and head appear only in the handoff comment and
    # in the summary of the review runs. To check whether a PR is already in
    # the normal cycle, look at the latest comments, the review-cycle events
    # and the run summaries of live executor cards.
    cycle_refs = {}
    for (t_id,) in con.execute(
            "SELECT id FROM tasks WHERE status IN "
            "('running','ready','review')"):
        parts = []
        parts += [r[0] or "" for r in con.execute(
            "SELECT body FROM task_comments WHERE task_id=? "
            "ORDER BY id DESC LIMIT 3", (t_id,))]
        parts += [r[0] or "" for r in con.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind IN "
            "('review_requested','changes_requested') "
            "ORDER BY id DESC LIMIT 3", (t_id,))]
        parts += [r[0] or "" for r in con.execute(
            "SELECT summary FROM task_runs WHERE task_id=? "
            "AND summary IS NOT NULL ORDER BY id DESC LIMIT 2", (t_id,))]
        cycle_refs[t_id] = " ".join(parts)
    con.close()

    states = {}   # number -> review state (cache for both check phases)
    pending = []  # (number, head, age) without a verdict inside the arm window
    for number in numbers:
        state = _pr_review_state(repo, number)
        if not state:
            continue
        states[number] = state
        if state["verdict"] or state["age"] is None:
            continue
        if state["age"] < REVIEW_ARM_MIN_AGE_S \
                or state["age"] > REVIEW_ARM_MAX_AGE_S:
            continue
        pending.append((number, state["head"], state["age"]))

    text_refs = ["%s" % (t[1] or "") + " " + (t[2] or "") for t in live]
    armed, blocked_alive, unhandled, closed_nv = [], [], [], []
    cycle_busy = []   # (number, head7, task_id, status) already in a cycle
    deferred = []   # (number, head7, task_id): review deferred by a decision
    for number, head, age in sorted(pending, key=lambda item: -item[2]):
        if number in pilot_numbers:
            continue   # pilot: one PR = one card
        base = "reviewer-pr%d-%s" % (number, head[:7])
        match = [t for t, refs in zip(live, text_refs)
                 if t[6] == base
                 or (t[3] == "reviewer"
                     and re.search(r"#%d\b" % number, refs)
                     and head[:7] in refs)]
        if match:
            task_id, status = match[0][0], match[0][4]
            if status == "blocked":
                blocked_alive.append("%s #%d %s [%s]"
                                     % (task_id, number, head[:7], status))
            elif status == "scheduled" \
                    and _owner_deferred_review(db, task_id):
                # The card sits in scheduled because of someone else's
                # decision, so the review will not start before the owner
                # decides. The head stays without a verdict but holds no
                # slot: the parking branch below counts the layer by
                # `holding`.
                deferred.append((number, head[:7], task_id))
            continue
        # A live card with a suffixed key (-r2 ...): the base text did not
        # match but the family is alive, so no duplicate may be created.
        # Owner decision: a double arm of one head is forbidden. ANY key of
        # this head's family counts as live: the base key, -rN (re-arm
        # after a card closed without a verdict) and -pN (paired arm). One
        # live card of any suffix is enough not to create a new one.
        fam_live_keys = [t[6] for t in live
                         if (t[6] or "") == base
                         or (t[6] or "").startswith(base + "-r")
                         or (t[6] or "").startswith(base + "-p")]
        if fam_live_keys:
            continue
        # The watchdog used to arm a reviewer card a minute before the
        # executor card handed off to review; the verdict then went through
        # both channels, one fix was made twice and reviewed twice. A live
        # executor card (assignee != reviewer) in running/ready/review that
        # names this PR and head in its text, comments or review-cycle
        # events already carries the normal review cycle: its
        # changes_requested goes back to the same executor, so no parallel
        # auto-arm lane is needed.
        for t, refs in zip(live, text_refs):
            if t[3] == "reviewer" \
                    or t[4] not in ("running", "ready", "review"):
                continue
            full = refs + " " + cycle_refs.get(t[0], "")
            if re.search(r"#%d\b" % number, full) \
                    and head[:7] in full:
                cycle_busy.append((number, head[:7], t[0], t[4]))
                break
        if cycle_busy and cycle_busy[-1][0] == number:
            continue
        fam_done = [d_id for d_id, d_key in done_keyed
                    if d_key == base or d_key.startswith(base + "-r")]
        key = base
        rearm_of = None
        if fam_done:
            # All cards of the family are closed but the head has no
            # verdict: the reviewer closed a card without a verdict. The new
            # arm must use a FRESH key: the base key would be deduplicated
            # against the done card.
            max_r = 1
            for k in list(fam_live_keys) + [d_key for _, d_key in done_keyed]:
                m = re.fullmatch(re.escape(base) + r"-r(\d+)", k or "")
                if m:
                    max_r = max(max_r, int(m.group(1)))
            key = "%s-r%d" % (base, max_r + 1)
            rearm_of = ", ".join(fam_done)
            closed_nv.append((number, head[:7], rearm_of, key))
        if len(armed) >= REVIEW_ARM_MAX_CREATE:
            unhandled.append("#%d %s" % (number, head[:7]))
            continue
        new_id = _review_arm_card(slug, number, head, age / 3600.0,
                                  key=key, rearm_of=rearm_of)
        if new_id:
            armed.append((new_id, number, head[:7], key, age / 3600.0))
        else:
            unhandled.append("#%d %s (create failed)" % (number, head[:7]))

    # --- Heads WITH a verdict are checked too ------------------------------
    # A change-request verdict from the reviewer may point at a blocker in a
    # twin PR; the twin then gets its fix and a PASS, and the pair would stand
    # forever, because a PR with a verdict never enters `pending`. Two
    # branches:
    #   A: the latest verdict on the head is reject, its body references #N
    #      (N != the PR itself), and the latest verdict on PR #N is accept,
    #      FRESHER than our reject: the paired blocker is closed, so a
    #      repeated review of the same head is armed. A twin/pair word
    #      without a number cannot be checked and is not armed. The guard
    #      "twin PASS is fresher than the reject" is mandatory: otherwise
    #      the reviewer already saw the PASS and rejected for another reason,
    #      and a repeat would loop the arms.
    #   B: the latest verdict on the head is accept and there is no fresh
    #      WCFY for the head (no comment with the marker at all, or it is
    #      older than the head): a coder card is armed to write a dated WCFY
    #      comment. Only when all REQUIRED checks of the head are SUCCESS
    #      (branch protection of the base): a PASS on a red head is not a
    #      PASS, and a head under a stack rebuild changes its sha, so the
    #      WCFY would go stale for nothing.
    hold = os.path.exists(PAIR_WCFY_HOLD)
    pair_armed, wcfy_armed, l35_unhandled = [], [], []
    twin_cache = {}
    protect_cache = {}
    for number in sorted(states):
        st = states[number]
        info = st.get("info")
        if not info or st["age"] is None:
            continue
        if st["age"] < REVIEW_ARM_MIN_AGE_S \
                or st["age"] > REVIEW_ARM_MAX_AGE_S:
            continue
        head = st["head"]
        if info["kind"] == "reject" and TWIN_ERA:
            # The paired arm lives only in the twin era (TWIN_ERA); once the
            # old release line is end-of-life, branch A does not run. Without
            # a pair word (pair/twin) references #N are not counted as
            # paired blockers: a false positive was seen when an unrelated
            # reference was treated as a pair.
            if not _PAIR_WORD_RE.search(info["body"] or ""):
                continue
            refs = [int(x) for x in _PAIR_REF_RE.findall(info["body"] or "")]
            refs = [r for r in dict.fromkeys(refs) if r != number]
            for twin in refs:
                if twin not in twin_cache:
                    try:
                        twin_cache[twin] = _pr_review_state(repo, twin)
                    except Exception:
                        twin_cache[twin] = None
                tinfo = (twin_cache[twin] or {}).get("info")
                if not tinfo or tinfo["kind"] != "accept":
                    continue
                if info["at"] is None or tinfo["at"] is None \
                        or tinfo["at"] <= info["at"]:
                    continue
                # A repeated review of an UNCHANGED head makes sense only if
                # the blocker is lifted without new code in this PR (the
                # paired PR was merged or accepted). If the verdict demands
                # new work in the PR itself, the machine sign of that work
                # is a LIVE work card for this PR: assignee != reviewer and
                # the PR number in the TITLE (the body is no good: the twin's
                # card references the paired number in its body, which is not
                # work on this PR). Otherwise the reviewer would occupy a
                # slot and return the same change request on the same head
                # while other cards wait for a slot. The review arrives on
                # the new head through the ordinary auto-arm once the work
                # card closes. The same sign also suppresses a paired arm
                # that would duplicate the main arm of the same head.
                work_live = [t[0] for t in live
                             if t[3] != "reviewer"
                             and re.search(r"#%d\b" % number, t[1] or "")]
                if work_live:
                    findings.append((
                        "%s:revlayer:pairwork:%d:%s"
                        % (slug, number, head[:7]), 1, (
                        "    PR #%d %s: paired blocker #%d is closed, "
                        "but live work card %s requires new code in "
                        "the PR itself: no repeated review of the unchanged "
                        "head is created, the verdict will arrive on the "
                        "new head through the ordinary auto-arm."
                        % (number, head[:7], twin,
                           ", ".join(work_live)))))
                    break
                base = "reviewer-pr%d-%s-p%d" % (number, head[:7], twin)
                fam_state, key, rearm_of = _family_arm_key(
                    base, live, done_keyed)
                if fam_state == "live":
                    break
                if any(t[3] == "reviewer"
                       and re.search(r"#%d\b" % number, refs_txt)
                       and head[:7] in refs_txt
                       for t, refs_txt in zip(live, text_refs)):
                    break
                if len(armed) + len(pair_armed) + len(wcfy_armed) \
                        >= REVIEW_ARM_MAX_CREATE:
                    l35_unhandled.append("pair #%d %s" % (number, head[:7]))
                    break
                if hold:
                    findings.append((
                        "%s:revlayer:pairdry:%d:%s" % (slug, number, head[:7]),
                        1, (
                        "    Pair re-review (dry, hold): repeated review of "
                        "PR #%d %s after the paired blocker #%d was closed "
                        "(twin PASS is fresher than the change request on "
                        "the head). Hold: %s" % (
                            number, head[:7], twin, PAIR_WCFY_HOLD))))
                    break
                new_id = _pair_rearm_card(slug, number, head,
                                          st["age"] / 3600.0, twin,
                                          key, rearm_of)
                if new_id:
                    pair_armed.append((new_id, number, head[:7], twin, key))
                else:
                    l35_unhandled.append(
                        "pair #%d %s (create failed)" % (number, head[:7]))
                break
        elif info["kind"] == "accept":
            head_at = time.time() - st["age"]
            wcfy_at = st.get("wcfy_at")
            if wcfy_at is not None and wcfy_at >= head_at:
                continue
            base = "coder-pr%d-wcfy-%s" % (number, head[:7])
            fam_state, key, rearm_of = _family_arm_key(base, live, done_keyed)
            if fam_state == "live":
                continue
            if any(t[3] == "coder"
                   and re.search(r"#%d\b" % number, refs_txt)
                   and head[:7] in refs_txt
                   for t, refs_txt in zip(live, text_refs)):
                continue
            # The "after PASS" arm goes only onto a head whose required
            # checks are green. Otherwise a reviewer verdict on a head with
            # a red required check would start a card for a head that is
            # waiting for a stack rebuild.
            ci_ok, ci_why = _head_mandatory_ci(
                repo, number, st.get("base") or "", protect_cache)
            if not ci_ok:
                findings.append((
                    "%s:revlayer:wcfyci:%d:%s" % (slug, number, head[:7]),
                    1, (
                    "    PR #%d %s: the accept verdict stands on a head where"
                    " not all required checks are green (%s): no WCFY is"
                    " created. The head is under a fix or a stack rebuild"
                    " and will change its sha, the comment would go stale"
                    " before it is read. A verdict on such a head must be"
                    " CHANGES REQUESTED or \"blocked by #N\", not PASS. The"
                    " WCFY arrives through the auto-arm on the new head with"
                    " green CI." % (number, head[:7], ci_why))))
                continue
            if len(armed) + len(pair_armed) + len(wcfy_armed) \
                    >= REVIEW_ARM_MAX_CREATE:
                l35_unhandled.append("wcfy #%d %s" % (number, head[:7]))
                continue
            if hold:
                findings.append((
                    "%s:revlayer:wcfydry:%d:%s" % (slug, number, head[:7]),
                    1, (
                    "    (dry, hold): PR #%d %s has a reviewer PASS without "
                    "a fresh WCFY (latest %s, the head is newer). Hold: %s" % (
                        number, head[:7], _fmt_utc(wcfy_at),
                        PAIR_WCFY_HOLD))))
                continue
            new_id = _wcfy_arm_card(slug, number, head, wcfy_at,
                                    key, rearm_of)
            if new_id:
                wcfy_armed.append((new_id, number, head[:7], key))
            else:
                l35_unhandled.append(
                    "wcfy #%d %s (create failed)" % (number, head[:7]))
    for new_id, number, head7, twin, key in pair_armed:
        findings.append((
            "%s:revlayer:pair:%d:%s" % (slug, number, head7), 1, (
            "    PR #%d %s: the change request referenced paired blocker "
            "#%d, the twin's PASS is fresher than the change request: the "
            "watchdog created repeated review %s (p100, reviewer, key %s)."
            % (number, head7, twin, new_id, key))))
    for new_id, number, head7, key in wcfy_armed:
        findings.append((
            "%s:revlayer:wcfy:%d:%s" % (slug, number, head7), 1, (
            "    PR #%d %s has a reviewer PASS without a fresh WCFY: the "
            "watchdog created %s (p95, coder, key %s)."
            % (number, head7, new_id, key))))
    if l35_unhandled:
        findings.append((
            "%s:revlayer:l35unhandled:%d" % (slug, len(l35_unhandled)), 2, (
            "    Left without a card: %s (limit of %d creations per run or "
            "a create failure): the next pass will pick them up."
            % (", ".join(l35_unhandled), REVIEW_ARM_MAX_CREATE))))

    for number, head7, prev, key in closed_nv:
        findings.append(("%s:revlayer:closed:%d:%s" % (slug, number, head7), 1, (
            "    Review layer: the reviewer closed card(s) %s WITHOUT a "
            "verdict on the head of PR #%d %s (key %s*). Root of the idle "
            "time: a create with the base key is deduplicated against the "
            "done card. Re-armed under key %s." % (prev, number, head7,
                            "reviewer-pr%d-%s" % (number, head7), key))))
    for new_id, number, head7, key, age_h in armed:
        findings.append(("%s:revlayer:arm:%d:%s" % (slug, number, head7), 1, (
            "    Review layer: PR #%d head %s has had no verdict for %.1f h: "
            "the watchdog created %s (p100, reviewer, key %s)."
            % (number, head7, age_h, new_id, key))))
    for number, head7, tid, st in cycle_busy:
        findings.append((
            "%s:revlayer:cycle:%d:%s" % (slug, number, head7), 1, (
            "    PR #%d %s: live executor card %s [%s] already carries the "
            "normal review cycle for this head: no parallel review lane is "
            "auto-armed, its changes_requested goes back to the same "
            "executor."
            % (number, head7, tid, st))))
    for line in blocked_alive:
        task_id = line.split()[0]
        findings.append(("%s:revlayer:stuck:%s" % (slug, task_id), 2, (
            "    Review layer: %s: the reviewer card is alive but not in "
            "ready, and the head has no verdict. Investigate or unblock."
            % line)))
    if unhandled:
        findings.append(("%s:revlayer:unhandled:%d" % (slug, len(unhandled)),
                         2, (
            "    Review layer: left without a card: %s (limit of %d "
            "creations per run or a create failure): the next pass will "
            "pick them up."
            % (", ".join(unhandled), REVIEW_ARM_MAX_CREATE))))

    # The layer is held only by heads whose review can actually run. A head
    # whose reviewer card sits in scheduled because of someone else's
    # decision drops out of `holding`. Without this, "parking reviews of
    # heads, while the layer waits for the same review and keeps the
    # parking" closed on itself: ready cards below p50 stayed in scheduled
    # forever.
    deferred_numbers = {n for n, _, _ in deferred}
    # A head in the normal review cycle does not hold the layer either: the
    # cycle of the executor card will bring the verdict, so other ready
    # cards need not be parked for that wait.
    cycle_numbers = {n for n, _, _, _ in cycle_busy}
    holding = [p for p in pending
               if p[0] not in deferred_numbers
               and p[0] not in cycle_numbers]
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
            if deferred:
                text += ("Also deferred by a decision, not holding the "
                         "layer: %s. "
                         % ", ".join("#%d(%s)" % (n, tid)
                                     for n, _, tid in deferred))
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
        held = [row[0] for row in con.execute(
            "SELECT DISTINCT t.id FROM tasks t JOIN task_comments c "
            "ON c.task_id = t.id WHERE t.status = 'scheduled' "
            "AND c.body LIKE ?",
            ("%SCHEDULED: " + REVIEW_PARK_MARKER + "%",))]
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
        if deferred:
            # The layer is empty for slots not because there are no reviews
            # but because they are deferred by a decision. Say so
            # explicitly, otherwise the alert gives no reason for the
            # silence and the cause has to be hunted by hand.
            findings.append(("%s:revlayer:deferred:%d"
                             % (slug, len(deferred)), 1, (
                "    Review layer is empty for slots, but there are heads "
                "without a verdict: %s. Their review is deferred by a "
                "decision (reviewer cards in scheduled/backlog), they hold "
                "no slots. Verdicts will appear when the owner returns the "
                "cards to ready."
                % ", ".join("#%d %s (%s)" % (n, h7, tid)
                            for n, h7, tid in deferred))))
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
    reason = "%d BLOCK in the body" % blocks
    body = (
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
    for tid, title, blocks, nlines, prio in hits:
        key = "decompose-%s" % tid
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
    for tid, dec_id, status in candidates:
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


# === Serialization of rework on one PR =====================================
#
# When an umbrella card for a verdict closes, the platform releases ALL of
# its children at once, and two coders push and rebase the same branch in
# parallel. The result is double context, double tests, push races, a lost
# commit, a repeated CI run, and a review layer that arms a head which goes
# stale within an hour. The owner's rule: one verdict per PR = one coder
# card in progress; splitting a package to fit the iteration budget is
# allowed, but the children run as a chain strictly in sequence
# (block1 -> block2 -> minors), with rebase and WCFY only in the last link.
# Only cards for different PRs may run in parallel. The gate builds the
# chain post factum: the platform releases the first link, the others wait
# on links.

OWNER_REVIEW_KEY_PR_RE = re.compile(
    r"^owner-review-(\d+)-[0-9a-f]{6,40}(?:[.\-]|$)", re.I)
SERIAL_REWORK_STATUSES = ("todo", "ready", "running", "review")
SERIAL_REWORK_MARKER = "Watchdog: serialization of cards of one PR."
SERIAL_TAIL_RE = re.compile(r"rebase|wcfy|what.?s? changed", re.I)


def _serial_card_pr(title, key):
    """PR number from the key owner-review-N-sha... or the first #N of the
    title. None if the card is not about a PR."""
    m = OWNER_REVIEW_KEY_PR_RE.match(key or "")
    if m:
        return m.group(1)
    m = VERDICT_PR_RE.search(title or "")
    return m.group(1) if m else None


def serial_rework_order(cards):
    """Chain order: tail cards (rebase/WCFY, whose head must be final) go
    last, the rest by created."""
    def rank(c):
        return (1 if SERIAL_TAIL_RE.search(c["title"] or "") else 0,
                c["created"])
    return sorted(cards, key=rank)


def _serial_has_path(adj, a, b, _seen=None):
    """Whether there is a path a -> ... -> b in adjacency {parent:
    {children}} (protection against a repeated link and cycles)."""
    if a == b:
        return True
    _seen = _seen if _seen is not None else set()
    if a in _seen or a not in adj:
        return False
    _seen.add(a)
    return any(_serial_has_path(adj, n, b, _seen) for n in adj[a])


def serial_rework_scan(slug):
    """Live coder rework cards grouped by PR: only groups of 2 or more.
    Returns [(pr, [card...])], card = {id,title,status,created}.
    The umbrella (a parent of a group member) is not part of the chain: it
    releases the children and does not push next to them."""
    db = os.path.join(BOARDS, slug, "kanban.db")
    if not os.path.exists(db):
        return []
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
    rows = con.execute(
        "SELECT id, title, body, status, assignee, idempotency_key,"
        " created_at FROM tasks"
        " WHERE status IN ('todo','ready','running','review','blocked')"
        " ORDER BY created_at").fetchall()
    link_rows = con.execute(
        "SELECT parent_id, child_id FROM task_links").fetchall()
    con.close()
    adj = {}
    for p, c in link_rows:
        adj.setdefault(p, set()).add(c)
    by_pr = {}
    for tid, title, body, status, assignee, key, created in rows:
        title, body, key = title or "", body or "", key or ""
        if assignee != "coder" or status not in SERIAL_REWORK_STATUSES:
            continue
        pr = _serial_card_pr(title, key)
        if pr is None:
            continue
        if lint_card_kind(title, key) not in ("rework", "rebase", None) \
                and not REWORK_GATE_RE.search(title + " " + body):
            continue
        by_pr.setdefault(pr, []).append(
            {"id": tid, "title": title, "status": status, "key": key,
             "created": float(created or 0)})
    out = []
    for pr, cards in by_pr.items():
        ids = {c["id"] for c in cards}
        sibs = [c for c in cards
                if not any(m in ids for m in adj.get(c["id"], ()))]
        if len(sibs) > 1:
            out.append((pr, sibs))
    return out


def serial_rework_gate(slug, apply=True):
    """Cards of one PR do not work in parallel.

    Builds a link chain <earlier> <later> between live coder rework cards of
    one PR: the platform releases the first link, the others sit on
    dependency_wait. A card in running/review is not put under a link
    (completing under a live parent is a deadlock): a pair in progress is a
    recorded race, the gate alerts without mutating. Idempotency: a repeated
    link is excluded by the path check, the marker is not duplicated.
    Returns findings [(key, rank, text)]."""
    groups = serial_rework_scan(slug)
    if not groups:
        return []
    db = os.path.join(BOARDS, slug, "kanban.db")
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
    link_rows = con.execute(
        "SELECT parent_id, child_id FROM task_links").fetchall()
    marked = {r[0] for r in con.execute(
        "SELECT task_id FROM task_comments WHERE body LIKE ?",
        (SERIAL_REWORK_MARKER[:32] + "%",)).fetchall()}
    con.close()
    adj = {}
    for p, c in link_rows:
        adj.setdefault(p, set()).add(c)
    findings = []
    for pr, cards in groups:
        order = serial_rework_order(cards)
        for prev, nxt in zip(order, order[1:]):
            if _serial_has_path(adj, prev["id"], nxt["id"]) \
                    or _serial_has_path(adj, nxt["id"], prev["id"]):
                continue
            key = "%s:serial:%s:%s-%s" % (slug, pr, prev["id"], nxt["id"])
            text = ("    PR #%s serialization: %s [%s] -> %s [%s]\n"
                    "      %s\n      %s"
                    % (pr, prev["id"], prev["status"], nxt["id"],
                       nxt["status"], prev["title"][:70],
                       nxt["title"][:70]))
            if nxt["status"] in ("running", "review"):
                findings.append((key + ":race", 1, text + (
                    "\n      Race: both links are in progress on one branch."
                    "\n      Runs are not interrupted. The later worker must"
                    " fetch the fresh head before rebase/push and make sure"
                    " the earlier link's commit reached the base.")))
                continue
            if not apply:
                findings.append((key, 1,
                                 text + " [dry-run: link not created]"))
                continue
            run([HERMES, "kanban", "--board", slug, "link", prev["id"],
                 nxt["id"]], timeout=90)
            adj.setdefault(prev["id"], set()).add(nxt["id"])
            if nxt["id"] not in marked:
                run([HERMES, "kanban", "--board", slug, "comment",
                     nxt["id"],
                     SERIAL_REWORK_MARKER + " Link %s -> %s created:"
                     " cards of one PR #%s run strictly in sequence"
                     " (one verdict = one branch; parallel only for"
                     " cards on different PRs). The platform will pick up"
                     " this card by itself when %s closes."
                     % (prev["id"], nxt["id"], pr, prev["id"]),
                     "--author", "watchdog"], timeout=90)
            findings.append((key, 1, text + (
                "\n      The gate created a link: the card waits for %s to"
                " finish." % prev["id"])))
    return findings


# === Priority for the card that closes the last merge preconditions ========
#
# Coder cards with minutes of work (WCFY, update-branch, green CI on a PR
# whose code the owner accepted) can sit in ready for hours: both slots are
# taken by large blocks, and at equal p80 the dispatcher takes the older
# card (ORDER BY priority DESC, created_at ASC in the dispatcher core).
# Meanwhile the coder closes older visual-delta p80 cards by date, merges
# wait on formalities, and the queue goes to informal volume.
#
# Rule: a ready+coder card whose (1) body holds an owner verdict CHANGES
# REQUESTED: minor, (2) mark of accepted code ("code accepted", "fix is
# accepted", "only merge prerequisites remain", APPROVED), (3) blocking
# items are all merge formalities (WCFY, update-branch, green CI, the
# Before / After section, merge as a pair), (4) the same PR has no live card
# with code or test work, is raised to p100. The dispatcher core is not
# touched: selection already goes by priority DESC.
#
# Two extensions of the rule:
# 1. The formality class is recognised by the card's work, not by a rework
#    title. [visual-delta] (the Before / After item of a verdict), the
#    watchdog's WCFY cards, update-branch and "green CI" carry the same
#    formality but without verdict text in the body: the acceptance of the
#    PR's code is looked up in verdict cards (any status) and in owner
#    review mirrors (comments with author=github on PR cards). Without this
#    a card of such a class would not be seen by the gate: its body has
#    neither CHANGES REQUESTED nor minor. Rebase cards of pr-hygiene
#    ("rebase PR #N onto ...") do not count as a class: their priority
#    96-99 is managed by pr-hygiene itself.
# 2. p100, not p90: the last formality before the merge of a PR whose code
#    the owner accepted goes before any work on PRs not ready to merge,
#    including rebases of the conflict debt (p96-p99). Numerically p100
#    equals review arms (assignee reviewer), but for the coder queue there
#    is no overlap: the dispatcher sorts by priority inside the pool of one
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
    # --- Children of one PR under one blocked parent go parallel after
    #     unlink (two coders on one branch). Before the parent is released,
    #     the children of one PR are chained with link <earlier> <later>
    #     (rebase/WCFY as the last link): the first one is released, the
    #     others wait in dependency_wait.
    i1_pkg = {}
    for parent, child, kind in i1_planned:
        if kind != "rework":
            continue
        pr = _serial_card_pr(tasks[child]["title"], tasks[child]["key"])
        if pr:
            i1_pkg.setdefault((parent, pr), []).append(child)
    for (parent, pr), members in sorted(i1_pkg.items()):
        if len(members) < 2:
            continue
        order = serial_rework_order(
            [{"id": c, "title": tasks[c]["title"], "created": tasks[c]["created"]}
             for c in members])
        ids = [c["id"] for c in order]
        for prev, nxt in zip(ids, ids[1:]):
            if (prev, nxt) in links or (nxt, prev) in links:
                continue
            if dry_run:
                found.append(("%s:lint:i1c:%s:%s-%s" % (slug, parent, prev, nxt), 1, (
                    "    I1 (dry-run): children of one PR #%s are chained "
                    "%s -> %s before releasing %s." % (pr, prev, nxt, parent))))
                continue
            kanban("link", prev, nxt)
            links.add((prev, nxt))
            if not any(SERIAL_REWORK_MARKER in b for b in comments.get(nxt, [])):
                kanban("comment", nxt,
                       SERIAL_REWORK_MARKER + " Link %s -> %s was added "
                       "before releasing the blocked parent %s: children of "
                       "one PR #%s run strictly in sequence. "
                       "The platform will pick the card up when %s closes."
                       % (prev, nxt, parent, pr, prev))
            found.append(("%s:lint:i1c:%s:%s-%s" % (slug, parent, prev, nxt), 1, (
                "    I1: children of one PR #%s are chained %s -> %s "
                "before releasing %s (parallel release is ruled out)."
                % (pr, prev, nxt, parent))))
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
        kanban("move", tid, "todo")
        if not has_marker(tid, 3):
            kanban("comment", tid,
                   "Watchdog lint I3: the card went back to todo: with %d "
                   "ready cards p>=%d (first %s) slots are not held by "
                   "cards p<%d. It returns to work when the layer empties."
                   % (len(high_ready), LINT_HIGH_PRIO, high_ready[0],
                      LINT_START_FLOOR))
        t2 = _lint_data(slug)[0].get(tid)
        if t2 and t2["status"] == "running" and (t2["priority"] or 0) < LINT_START_FLOOR:
            found.append(("%s:lint:i3f:%s" % (slug, tid), 1, (
                "    I3 NOT FIXED: %s (p%d) did not leave running with %d ready "
                "p>=%d.\n      %s" % (tid, t["priority"], len(high_ready),
                                      LINT_HIGH_PRIO, title))))
        else:
            found.append((key, 1, (
                "    I3 FIXED: %s (p%d) had started with %d ready p>=%d, "
                "moved to todo with a comment.\n      %s"
                % (tid, t["priority"], len(high_ready), LINT_HIGH_PRIO,
                   title))))

    # --- I4: ready without an assignee: the stock stranded_in_ready diagnostic
    #     (a custom detector was removed as a duplicate of the stock one)

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
        run([HERMES, "kanban", "--board", slug, "create",
             "[ops] Twin blocked: port prerequisite module for %s" % task_id[:12],
             "--body",
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


# ---------------- Backlog owner gate ----------------
# Owner rules:
#   1) cards of the factory's own initiative live in triage; once a day the
#      watchdog collects a triage digest (id, why, estimate) into the owner
#      report; only what the owner raises, or what carries the label of a
#      milestone issue, goes to work;
#   2) ready = cards of issues/PRs of the repository's OPEN milestones
#      (an issue by its GitHub milestone, a PR by Refs/Closes of such an
#      issue) plus ops; everything else goes back to triage with a comment
#      giving the reason. The list of active milestones comes from GitHub
#      state=open, no release names live in the code. A closed milestone
#      stops applying by itself, a new one starts without a code change.
#      With no open milestones at all the gate parks nothing and says so in
#      the report.
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


def backlog_refs(card):
    text = "%s\n%s" % (card.get("title") or "", card.get("body") or "")
    refs = set(int(x) for x in re.findall(r"#(\d+)", text))
    refs |= set(int(x) for x in re.findall(r"/issues/(\d+)", text))
    return refs


def backlog_classify(card, linkage):
    """(action, category, info): keep|demote by rule 2 of the backlog gate."""
    if (card.get("assignee") or "") == "ops":
        return "keep", "ops", None
    refs = backlog_refs(card)
    if not refs:
        return "demote", "no_ref", None
    ms_issues = linkage.get("ms_issues") or {}
    open_pr_refs = linkage.get("open_pr_refs") or {}
    direct = set(linkage.get("direct_ms_prs") or [])
    linked_issues = sorted(n for n in refs if n in ms_issues)
    via_pr = sorted(set(n for n in refs if n in open_pr_refs
                        for m in open_pr_refs[n] if m in ms_issues))
    if linked_issues or via_pr:
        if via_pr or any(ms_issues[n] == "open" for n in linked_issues):
            return "keep", "milestone", linked_issues or via_pr
        return "keep", "milestone_stale_closed", linked_issues
    pr_refs = sorted(n for n in refs if n in open_pr_refs)
    if pr_refs:
        if set(pr_refs) & direct:
            # Owner decision: a direct milestone label on a PR counts as
            # linkage, the card stays in ready. A missing Refs/Closes on the
            # issue stays a hygiene violation, which pr-hygiene catches; the
            # backlog gate no longer demotes for it.
            return "keep", "milestone", pr_refs
        if set(pr_refs) & set(linkage.get("owner_review_prs") or []):
            # The owner's formal review (APPROVED/CHANGES_REQUESTED) is the
            # owner's decision to work on the PR. Cards of such a PR are not
            # parked: otherwise the Before/After item of the verdict can end
            # up in a parked visual-delta card and the merge stalls over a
            # two-line formality. The backlog parks what is "outside the
            # owner's attention", and a review is attention.
            return "keep", "pr_owner_review", pr_refs
        return "demote", "pr_not_milestone", pr_refs
    return "demote", "issue_not_milestone", sorted(refs)


def backlog_estimate(card):
    nlines = len((card.get("body") or "").splitlines())
    return "S" if nlines <= 12 else ("M" if nlines <= 30 else "L")


BACKLOG_PARK_MARKER = "backlog: outside milestone"


def backlog_park(slug, task_id, cat, ms_names):
    """Parks a card outside the open milestones through kanban schedule.

    ms_names: names of the open milestones for the reason text (taken from
    GitHub, none in the code).

    A block/unblock cycle was rejected by the owner: block_task
    (kanban_db.py) has three side effects in the core code: every call
    synthesizes a task_run with outcome='blocked' (_end_or_synthesize_run),
    fires the task hook 'kanban_task_blocked' (the notifier fans out to the
    notify subscriptions, which are inherited from parents), and the feed
    gets blocked/unblock/block_loop_detected events. So N parkings mean 2N
    synthetic runs and notifications. schedule parks without runs and hooks,
    and the reason is written as a comment by itself (kanban_parser.py).
    Return: unblock, by the owner; the digest collects parkings by
    BACKLOG_PARK_MARKER in the comment.
    """
    ms_label = "/".join(ms_names) if ms_names else "none open"
    reason = ("%s (backlog gate, rule 2, category: %s): no link to the"
              " open milestones (%s); return to ready with the unblock"
              " command after the owner decides"
              % (BACKLOG_PARK_MARKER, cat, ms_label))
    out = run([HERMES, "kanban", "--board", slug, "schedule", task_id,
               reason], timeout=90)
    if not out.startswith("Scheduled"):
        return False
    return _card_status(slug, task_id) == "scheduled"


# The reverse side of parking. A verdict card can hand its Before/After item
# to a parked visual-delta card (the dedup subtracts the item from the
# verdict); that card then sits in scheduled and the merge stands still over
# a formality nobody is left to do. Rule: the owner's formal review on a PR
# (APPROVED/CHANGES_REQUESTED) is the owner's decision to work on that PR;
# cards of such a PR are not parked (classify above), and cards already
# parked as pr_not_milestone are raised. The raise is an unblock with a
# reason (core: scheduled -> ready); the pair "parking marker + category" in
# the comment is the source of truth and survives a loss of the watchdog's
# state.
BACKLOG_RAISE_MARKER = "raise by owner review"


def backlog_raise_owner_reviewed(slug, linkage, apply=True):
    """Raises parked pr_not_milestone cards of PRs with a formal owner
    review. Returns [(tid, prs, title)], at most BACKLOG_SWEEP_CAP actions
    per run. Idempotent: a raised card leaves scheduled and drops out of the
    selection; it is not parked again (classify returns keep by
    owner_review_prs).

    Other parking categories (issue_not_milestone, no_ref) are not touched:
    they have no PR to which the owner's decision applies.
    """
    owner = set(linkage.get("owner_review_prs") or [])
    if not owner:
        return []
    db = os.path.join(BOARDS, slug, "kanban.db")
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
    rows = {r[0]: r[1:] for r in con.execute(
        "select id, title, body from tasks where status = 'scheduled'")}
    con.close()
    todo = []
    for tid, cat in backlog_parked_by_marker(slug).items():
        if cat != "pr_not_milestone" or tid not in rows:
            continue
        title, body = rows[tid]
        hits = sorted(backlog_refs(
            {"title": title, "body": body}) & owner)
        if not hits:
            continue
        todo.append((tid, hits, (title or "").strip()))
    if not apply:
        return todo
    for tid, hits, _title in todo[:BACKLOG_SWEEP_CAP]:
        run([HERMES, "kanban", "--board", slug, "unblock", tid,
             "--reason",
             "Backlog gate, %s: PR %s has a formal owner review"
             " (APPROVED/CHANGES_REQUESTED), which is the owner's decision"
             " to work on the PR. The pr_not_milestone parking is lifted,"
             " the card goes to work." % (
                 BACKLOG_RAISE_MARKER,
                 ", ".join("#%d" % p for p in hits))], timeout=90)
    return todo


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
    """Backlog gate: triage digest (rule 1), ready cleanup (rule 2), p0
    archive (rule 3).

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
    ms_titles = linkage.get("ms_titles") or {}
    if apply:
        state6 = state.setdefault("backlog", {})
    else:
        state6 = dict(state.get("backlog") or {})
    swept = dict(state.get("backlog_swept") or {})
    archived = dict(state.get("backlog_archived") or {})
    # Rule 2: ready = issues/PRs of the open milestones plus ops; everything
    # else goes back to triage with a comment giving the category. Actions
    # go only by a fresh cache; cards already returned (in swept) are not
    # touched again.
    # No open milestones at all: the gate parks nothing and says so in the
    # report (parking "everything outside an empty focus" is not allowed:
    # it would stop the pipeline once the release milestones are closed).
    if fresh and not ms_titles:
        out.append(("backlog-nomilestones",
                    "  BACKLOG GATE: the repository has no open milestones,"
                    " rule 2 parks nothing. Create a milestone or lift the"
                    " gate's focus."))
    if fresh and ms_titles:
        ms_names = sorted(ms_titles.values())
        db = os.path.join(BOARDS, slug, "kanban.db")
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
        ready_rows = list(con.execute(
            "select id, title, body, assignee, coalesce(priority,0)"
            " from tasks where status = 'ready'"))
        con.close()
        keep, todo = 0, []
        for tid, title, body, assignee, prio in ready_rows:
            action, cat, info = backlog_classify(
                {"title": title, "body": body, "assignee": assignee},
                linkage)
            if action == "keep":
                keep += 1
                continue
            if tid in swept:
                continue
            todo.append((prio, tid, cat, info, title))
        todo.sort(key=lambda item: -item[0])
        if todo:
            lines = ["  BACKLOG GATE, rule 2: outside the open milestones"
                     " (%s) there are %d cards in ready (milestone/ops"
                     " stays: %d). Parking through schedule with the marker"
                     " '%s', up to %d per run; the owner returns cards with"
                     " unblock."
                     % (", ".join(ms_names), len(todo), keep,
                        BACKLOG_PARK_MARKER, BACKLOG_SWEEP_CAP)]
            for prio, tid, cat, info, title in todo[:BACKLOG_SWEEP_CAP]:
                if apply:
                    if backlog_park(slug, tid, cat, ms_names):
                        swept[tid] = {"cat": cat, "ts": now}
                    else:
                        lines.append("    %s NOT parked (status %s),"
                                     " resolve manually"
                                     % (tid, _card_status(slug, tid)))
                        continue
                lines.append("    %s p%d %s %s"
                             % (tid, prio, cat, (title or "")[:70]))
            if apply:
                state["backlog_swept"] = swept
            out.append(("backlog-sweep:%d" % len(todo), "\n".join(lines)))
    # Raise parked pr_not_milestone cards of PRs with a formal owner review
    # (the owner decided to work on the PR). Only by a fresh cache, like
    # parking.
    if fresh:
        raised = backlog_raise_owner_reviewed(slug, linkage, apply=apply)
        if raised:
            lines = ["  BACKLOG GATE, owner review: owner review on a PR,"
                     " %d to raise from parking (up to %d per run):"
                     % (len(raised), BACKLOG_SWEEP_CAP)]
            for tid, hits, title in raised:
                lines.append("    %s PR %s | %s"
                             % (tid, ",".join(str(p) for p in hits),
                                title[:76]))
            out.append(("backlog-raise:%d" % len(raised), "\n".join(lines)))
    # Rule 3: a p0 in todo/ready/triage with no movement for more than 14
    # days is archived with a reason comment. Actions only by a fresh cache.
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
    if ":cap:" in key or ":budget:" in key or ":budget-gate:" in key:
        return "owner"
    if key.startswith(("staleblock:", "samepr:", "dup:", "dup2:", "twin")):
        return "owner"
    # An owner question open for more than a day is an owner action, not
    # routine.
    if key.startswith("ownerq:"):
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
            digest.append("  [%s] Backlog-owner gate failed: %s: %s"
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
        try:
            found = check(slug, state)
        except sqlite3.Error as exc:
            # A broken schema or a truncated db on one board must not kill
            # the whole run: the other boards would be left unsupervised
            # (an archived dispatch-proof board with a 0-byte kanban.db
            # once crashed main() in graph() before the remaining boards
            # were processed). Supervision impossible is a real stop: a
            # stop-class push, deduplicated for a day by a synthetic key
            # (a repeat no more than once a day).
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
            elif now - state.get(key, 0) <= REPEAT_AFTER:
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
        # What the backlog-owner gate would do right now. Read only: the
        # board and state are not touched, the hold does not interfere (no
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
        print("TOTAL (backlog-owner dry-run): entries %d" % total)
    elif "--prototype-dry-run" in sys.argv:
        # Dry run of the prototype gate. Read only: the board and state are
        # not touched, the hold does not interfere (no mutations).
        protogate_dry_run()
    elif "--rework-gate-only" in sys.argv:
        # A fast pass of only the size gate, for a cron every 5 minutes. It
        # closes the race with fresh rework cards (the dispatcher ticks
        # every ~6 minutes, the full watchdog once an hour). It also does
        # the automatic return on a "fits-in-one-run" verdict (the
        # decomposition is closed, the parent is stuck at the architect),
        # the dedup of verdict items (a verdict card subtracts items
        # covered by a live card of the same PR before the dispatcher hands
        # it to a worker), and the merge-unblocker priority (a card with
        # only merge formalities on a PR accepted by code is raised to
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
                # Serialisation of cards of one PR (a race of pushes to one
                # branch). Must manage to run before the second link
                # starts: the dispatcher ticks every ~6 minutes.
                for _, _, text in serial_rework_gate(slug):
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
    elif "--serial-rework-only" in sys.argv:
        # A single pass of the gate that serialises rework of one PR
        # (manual application to a live board and diagnosis). Actions at
        # once, output like --rework-gate-only.
        for slug in board_slugs():
            try:
                for _, _, text in serial_rework_gate(slug):
                    print("[%s]\n%s" % (slug, text))
            except Exception as exc:
                print("[%s] serial-rework failed: %s: %s"
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
    else:
        main()
