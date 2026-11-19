#!/usr/bin/env python3
"""worker-api-watchdog: level-2 findings and level-3 actions on live kanban runs.

Scheduled every 10 minutes (--no-agent); an empty stdout is a quiet tick.

Signature A (API silence): the run is `running`, the client heartbeat is fresh
(the client is alive), but the last API call of its session is older than
GAP_MIN minutes.

Signature B (compression thrash): the run's session compressed its context
many times since the run started: the irreducible context floor sits at the
compression threshold, every compression is wasted time.

Signature C (poll-only): for the last POLL_WIN_MIN minutes the run's session
only polls GitHub (gh pr view/checks/list, gh api, gh run view), with no
edits, commits, gh mutations or builds: at least POLL_MIN_BATCHES polling
batches and zero signs of work. A run does not wait for someone else's result
(a verdict, a merge, CI longer than its own cycle); see the
worker-run-discipline skill. A live run once held a dispatcher slot for 4.4
hours polling `gh pr view` every 7 minutes. Tier 2 catches a slow cadence: the
window starts at the run's last piece of work, so CI samples every ~20 minutes
are caught too. A repeated C finding POLL_ESCALATE_AFTER_MIN minutes later
with nothing changed is an ACTION: `kanban schedule` closes the run with a
SCHEDULED_UNTIL mark, the card returns to ready when the mark expires.

Signature D (self-review): the run was claimed from the review column by the
same profile that delivered the work, so the card reviews itself.

Signature E (duplicate review): the native review lane started a second review
of a PR head that an earlier armed reviewer card is already reviewing. The
late run is reclaimed and parked on a non-profile until the verdict exists.

Signature F (orphan claim): a card is `running`, its worker pid is gone and its
heartbeat is long stale (the gateway and the worker were killed together, for
example by the OOM killer). The native reclaim does not run while the
dispatcher tick is held (kanban-dispatch-hold silences the whole tick, reclaim
phase included), so the watchdog reclaims the card itself.

Parking until a moment: the only form is `kanban schedule` with the mark
SCHEDULED_UNTIL=<ISO8601 UTC> in the reason, and this watchdog executes it
(wake_scheduled_cards). A `block` with SCHEDULED_UNTIL in its free text is a
violation: nobody reads marks in block reasons. It gets a level-3 finding and
a safety return to ready once the mark expires.

Merge waits: a blocked card whose unblock condition is a single line "unblock
when PR #N is merged" is returned to ready by the watchdog once every PR on
that line is MERGED (wake_merged_pr_waits). The native dependency wait fires
when a parent CARD closes, which happens at delivery, long before the merge;
SCHEDULED_UNTIL wakes at a moment in time, and the merge moment is unknown.

Output: one finding line naming the card on stdout (a no-agent cron sends it
to the digest) plus a comment on the card (at most once per ALERT_COOLDOWN_MIN).

Selftest: --selftest <task_id> evaluates the signatures on that card's run as
if it were alive (fresh heartbeat), without acting.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from factory_config import cfg, hermes_home  # noqa: E402

HOME = Path(hermes_home())
BOARDS_GLOB = str(HOME / "kanban" / "boards" / "*" / "kanban.db")
STATE_FILE = HOME / "cron" / "worker-api-watchdog.state.json"
GAP_MIN = 20.0            # signature A: minutes without API calls

# Signature C: the run only polls GitHub. 30-minute window, at least 3 unique
# polling batches and zero signs of work (edits, commits, gh mutations,
# builds, tests). Commands are read from tool_calls of the session's messages
# in the profile's state.db.
POLL_WIN_MIN = 30.0       # signature C: the "gh polling only" window
POLL_MIN_BATCHES = 3      # unique polling batches per window
# The first and the last batch must be at least this far apart: "3 batches in
# 47 seconds" is the initial read of a PR, not half an hour of polling.
POLL_SPREAD_MIN = 10.0
# Tier 2, slow cadence. A tester sampling CI reruns every ~20 minutes put 1-2
# batches into the 30-minute window, the spread stayed below POLL_SPREAD_MIN
# and tier 1 was silent on exactly the case it exists for. Tier 2 looks at
# the window since the run's LAST piece of work: if only gh polling followed
# it (>= 3 batches spread over >= 30 minutes, work older than 40 minutes),
# it is the same poll-only, just rare. The thresholds keep every known false
# positive quiet: clustered batches do not spread, a run younger than 40
# minutes has no window, reviewer and architect are exempt. 40/30 rather than
# 60/40: a run whose last work was 28 minutes before the check must be caught
# within the hour, not near the end of the second.
POLL_LONG_SPAN_MIN = 40.0    # tier 2: the window counts only if work is older
POLL_LONG_MIN_BATCHES = 3
POLL_LONG_SPREAD_MIN = 30.0
# A C finding went out as a comment and the run kept waiting for CI inside
# itself for 20 more minutes. A repeated C finding on the same state key
# POLL_ESCALATE_AFTER_MIN minutes later with nothing changed is not a
# message but an action: the run is closed by `kanban schedule` (waiting on
# time), the card returns to ready by its SCHEDULED_UNTIL mark after
# POLL_WAKE_DELAY_MIN minutes (one CI attempt of ~25 minutes plus margin).
# Waiting for CI does not live inside a run.
POLL_ESCALATE_AFTER_MIN = 20.0
POLL_WAKE_DELAY_MIN = 35.0
# A worker once closed a counting run with `block` and the free text
# SCHEDULED_UNTIL=2026-11-17T07:30:00+03:00 (a moment already in the past,
# the time zone mixed up). Nothing read a mark in a block reason, so the card
# would have stayed blocked forever. Rule: parking until a moment is one
# native form, `kanban schedule` with SCHEDULED_UNTIL=<ISO8601> in the reason
# (UTC in data, local time only in prose for people), and this watchdog
# executes the mark. `block` with SCHEDULED_UNTIL is forbidden: a level-3
# finding plus a safety return to ready once the mark expires. A naive time
# without a zone is read as UTC (data lives in one zone).
SCHEDULED_UNTIL_RE = re.compile(
    r"SCHEDULED_UNTIL=(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
    r"(?:Z|z|[+-]\d{2}:?\d{2})?)")
# Reading PRs and CI is the job of review profiles, not poll-only.
POLL_EXEMPT_PROFILES = {"reviewer", "architect"}
POLL_RE = re.compile(r"gh (pr (view|checks|list)|api|run (view|list))")
MUT_RE = re.compile(
    r"gh api[^\"\']*(--method\s+(POST|PATCH|PUT|DELETE)|-f\s|-F\s|--input)"
    r"|gh pr (create|merge|comment|edit|ready|close|reopen|review)")
WORK_RE = re.compile(
    r"git (commit|push|add|apply)|jest|mvn|pytest|npm (test|run)")

# Signature E, the reverse race of "do not arm a review when the native review
# is already running". The watchdog armed a reviewer card for a PR head, and
# 12 minutes later the implementer delivered with kanban_request_review: the
# native review lane started a SECOND reviewer of the same head. One head is
# reviewed by one session, the earlier one. The late duplicate (review lane on
# the implementer's card while a live review run of an armed card for the same
# PR and head exists, started earlier, with a fresh heartbeat) is put out:
# the run is reclaimed and the implementer's card is assigned to the
# placeholder REVIEW_HOLD_PROFILE. No such profile exists, so the review lane
# does not spawn it. The return is native: once the armed family of this head
# has a final verdict (the summary of a finished review run), the card goes
# back to reviewer and the review lane completes it by that verdict.
REVIEW_HOLD_PROFILE = "rev-hold"
ARM_KEY_RE = re.compile(r"^reviewer-pr(\d+)-([0-9a-f]{7})(?:-[rp]\d+)?$")
# Summaries of finished armed reviews look like "finished with verdict PASS",
# "at head 626b46a: CHANGES REQUESTED (issuecomment-...)". The verdict word
# follows the word "verdict" or a colon that ties the result to the head.
VERDICT_DONE_RE = re.compile(
    r"(?i)(?:verdict\w*|head\s+[0-9a-f]{7,}|:)"
    r"\s*(?:PASS\b|CHANGES\s+REQUESTED|APPROVED\b|ACCEPTED\b)")

# Merge waits. A card blocked "until PR #N is merged" was still blocked 20
# minutes after the merge, and only the owner woke it: a condition written as
# text has no executor. Native mechanisms do not cover it: the dependency wait
# fires when the parent CARD closes, and the predecessor card closes when the
# rework is delivered, long before the PR merges; SCHEDULED_UNTIL wakes at a
# MOMENT, and the merge moment is unknown (an early wake is an empty run, a
# late one is the same hole). This is an extension of the watchdog layer, not
# a new daemon: the watchdog checks the written merge condition and returns
# the card to ready once it holds. The condition is recognised by a LINE that
# carries a wait marker ("Unblock", "Unblock condition", "BLOCKED until"), a
# PR reference and a merge word on the same line: PRs on lines without a
# marker (how-to instructions) do not count. Compound conditions (milestone,
# owner decision, re-review, awaiting plan) are left alone: only an
# unambiguous merge trigger unblocks.
MERGE_WAIT_MARKER_RE = re.compile(
    r"(?i)unblock|blocked\s+until|lift\s+the\s+block")
MERGE_WAIT_PR_RE = re.compile(r"(?i)(?:gh\s+pr\s+view\s+|PR\s*#)(\d+)")
MERGE_WAIT_MERGE_RE = re.compile(r"(?i)merge")
MERGE_WAIT_DENY_RE = re.compile(
    r"(?i)milestone|wait-for-triage|not[.\s-]?planned|re-?review|\basked\b"
    r"|owner\s+decision|awaiting\s+plan|plan\s+review")
MERGE_WAIT_PR_TTL_SEC = 900.0   # PR state cache: one watchdog tick


def poll_window(profile, session, win_end, run_start=None, win_min=None,
                floor_ts=None):
    """(n_batch, n_work, spread_min, last_work_ts) for the window up to win_end.

    The window is win_min minutes (POLL_WIN_MIN by default), cut at the run
    start (run_start) and, for the slow-cadence tier, at floor_ts = the run's
    last piece of work. last_work_ts: timestamp of the last work row inside
    the window (for tier 2), None when there was no work.

    A batch is a unique timestamp of an assistant message with a gh poll
    (view/checks/list/api without mutations). Work: patch/write_file, git
    commit/push/add/apply, gh api/gh pr mutations, builds and tests. Errors
    reading state.db (a live gateway holds an exclusive lock at checkpoint)
    give (0, 0, 0, None), not a false finding.

    The window is cut at the run start because a session outlives runs:
    without the cut a fresh run inherits gh polls from the session's history
    (a run was once flagged 47 seconds after start for the previous run's
    activity). spread_min is the distance between the first and the last
    polling batch in the window.
    """
    if not session:
        return 0, 0, 0.0, None
    db = HOME / "profiles" / profile / "state.db"
    if not db.exists():
        return 0, 0, 0.0, None
    cutoff = win_end.timestamp() - (win_min or POLL_WIN_MIN) * 60
    if run_start is not None:
        cutoff = max(cutoff, run_start.timestamp())
    if floor_ts is not None:
        cutoff = max(cutoff, floor_ts)
    rows = None
    for attempt in range(2):
        try:
            con = sqlite3.connect(str(db), timeout=15)
            rows = con.execute(
                "SELECT tool_name, tool_calls, timestamp FROM messages "
                "WHERE session_id=? AND timestamp>=? ORDER BY timestamp",
                (session, cutoff)).fetchall()
            con.close()
            break
        except sqlite3.Error:
            if attempt:
                return 0, 0, 0.0, None
            time.sleep(2)
    if rows is None:
        return 0, 0, 0.0, None
    seen, n_batch, n_work = set(), 0, 0
    first_batch = last_batch = None
    last_work = None
    for tool_name, tc, ts in rows:
        t = tc or ""
        if (tool_name in ("patch", "write_file")
                or WORK_RE.search(t) or MUT_RE.search(t)):
            n_work += 1
            last_work = ts
            continue
        if POLL_RE.search(t) and ts not in seen:
            seen.add(ts)
            n_batch += 1
            first_batch = ts if first_batch is None else first_batch
            last_batch = ts
    spread_min = 0.0
    if first_batch is not None and last_batch is not None:
        spread_min = (last_batch - first_batch) / 60
    return n_batch, n_work, spread_min, last_work


HEARTBEAT_FRESH_SEC = 900  # the client is alive: heartbeat at most 15 min old
ORPHAN_HB_STALE_SEC = 1800  # signature F: orphan when the heartbeat is older
ORPHAN_SPAWN_GRACE_SEC = 600  # signature F: a fresh spawn may not show a pid yet
THRASH_PER_HOUR = 6       # signature B: compressions per hour
ALERT_COOLDOWN_MIN = 120  # alert one card at most every 2 hours
LOGS_PER_PROFILE = ("agent.log", "agent.log.1")

API_RE = re.compile(r"^(\S+ \S+) INFO \[(local-[0-9a-f]+)\] agent\.conversation_loop: API call #")
COMP_RE = re.compile(r"^(\S+ \S+).*context compression started: session=(local-[0-9a-f]+)")
EXH_RE = re.compile(r"attempts_exhausted")
SESS_RE = re.compile(r'"session_id":\s*"(local-[0-9a-f]+)"')


def board_dbs() -> list[str]:
    import glob
    return sorted(glob.glob(BOARDS_GLOB))


def running_runs(db: str) -> list[dict]:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT id, task_id, profile, worker_pid, started_at, last_heartbeat_at "
            "FROM task_runs WHERE status='running'")]
    finally:
        con.close()
    for r in rows:
        r["board"] = Path(db).parent.name
    return rows


def session_of_run(db: str, run_id: int) -> str | None:
    """Session of a run: the last owner_admitted event with a session_id."""
    con = sqlite3.connect(db)
    try:
        row = con.execute(
            "SELECT payload FROM task_events WHERE run_id=? AND kind='owner_admitted' "
            "ORDER BY id DESC LIMIT 1", (run_id,)).fetchone()
    finally:
        con.close()
    if not row or not row[0]:
        return None
    m = SESS_RE.search(row[0])
    return m.group(1) if m else None


def scan_profile_logs(profile: str, session: str | None = None):
    """(last API call of the session|None, [(ts, sid) api], [(ts, sid) comp], n_exhausted)."""
    d = HOME / "profiles" / profile / "logs"
    api, comp, exh = [], [], 0
    for name in LOGS_PER_PROFILE:
        p = d / name
        if not p.exists():
            continue
        with open(p, errors="replace") as f:
            for line in f:
                m = API_RE.match(line)
                if m:
                    api.append((m.group(1), m.group(2)))
                    continue
                m = COMP_RE.match(line)
                if m:
                    comp.append((m.group(1), m.group(2)))
                    continue
                if EXH_RE.search(line):
                    exh += 1
    last_api = None
    if session:
        for ts, sid in api:
            if sid == session:
                last_api = max(last_api or ts, ts)
    return last_api, api, comp, exh


def candidate_sessions(run: dict, db: str) -> list[str]:
    """Candidate working sessions of a run.

    owner_admitted records the gateway request session; the worker's real
    session may differ, so the most active sessions of the profile since the
    run started are added.
    """
    cands = []
    adm = session_of_run(db, run["id"])
    if adm:
        cands.append(adm)
    started = datetime.fromtimestamp(float(run["started_at"])).strftime("%Y-%m-%d %H:%M")
    from collections import Counter
    _, api, _, _ = scan_profile_logs(run["profile"])
    cnt = Counter(s for t, s in api if t >= started)
    for sid, _ in cnt.most_common(2):
        if sid not in cands:
            cands.append(sid)
    return cands


def parse_ts(ts: str) -> datetime:
    return datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_state(st: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, indent=1))


def comment(board: str, task: str, text: str) -> None:
    try:
        subprocess.run(
            ["hermes", "kanban", "comment", task, text, "--author", "watchdog"],
            capture_output=True, timeout=60,
            env={**os.environ, "HERMES_KANBAN_BOARD": board} if board else os.environ,
        )
    except Exception as exc:  # the comment is not critical for the finding
        print(f"  (comment not sent: {exc})", file=sys.stderr)


def _terminate_worker(pid) -> bool:
    """SIGTERM to the live worker client (schedule does not stop it). Linux only."""
    if not pid:
        return False
    try:
        with open(f"/proc/{int(pid)}/cmdline", "rb") as f:
            if b"kanban_worker_client" not in f.read():
                return False          # pid reused: not our process
        os.kill(int(pid), 15)
        return True
    except (OSError, ValueError):
        return False


def escalate_poll_only(board: str, run: dict, age_min: float, state: dict) -> str:
    """A repeated signature C with nothing changed: an action, not a message.

    The run is closed by the native schedule (waiting on time: the run gets
    outcome=scheduled, the reason becomes a card comment), the worker client
    gets SIGTERM. The card returns to ready by the SCHEDULED_UNTIL mark in the
    schedule reason, executed by wake_scheduled_cards. -> level-3 finding."""
    env = {**os.environ, "HERMES_KANBAN_BOARD": board}
    until = datetime.now(timezone.utc) + timedelta(minutes=POLL_WAKE_DELAY_MIN)
    reason = (
        f"SCHEDULED_UNTIL={until:%Y-%m-%dT%H:%M:%SZ} poll-only with nothing "
        f"changed after the signature C finding ({age_min:.0f} min): waiting "
        "for CI does not live inside a run. The run is closed, the card "
        "returns to ready by the SCHEDULED_UNTIL mark (executed by the "
        "watchdog).")
    done = subprocess.run(
        ["hermes", "kanban", "schedule", run["task_id"], reason],
        capture_output=True, timeout=90, env=env)
    text = (
        f"Action (level 3), signature C: poll-only of run {run['id']} on card "
        f"{run['task_id']} went on for {age_min:.0f} min after the finding "
        f"with nothing changed. Waiting for CI does not live inside a run: the "
        f"run is closed through kanban schedule with the mark SCHEDULED_UNTIL="
        f"{until:%Y-%m-%dT%H:%M:%SZ}, the watchdog returns the card to ready "
        f"by the mark.")
    if done.returncode != 0:
        text += " WARNING: schedule failed, retry on the next tick."
    else:
        _terminate_worker(run.get("worker_pid"))
    state[f"{board}:{run['task_id']}:poll-only"] = time.time()
    return text


def parse_scheduled_until(reason):
    """Epoch seconds of the SCHEDULED_UNTIL mark in a reason, or None.

    Times in data live in one zone. A naive value without a zone is read as
    UTC, a zone in the mark is respected (07:30+03:00 is 04:30Z). A broken
    mark is not parsed: None, the card is left alone."""
    m = SCHEDULED_UNTIL_RE.search(reason or "")
    if not m:
        return None
    raw = m.group(1).replace("z", "Z").replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _latest_event_reason(con, task: str, kind: str):
    row = con.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? "
        "ORDER BY id DESC LIMIT 1", (task, kind)).fetchone()
    if not row or not row[0]:
        return None
    try:
        payload = json.loads(row[0])
    except (ValueError, TypeError):
        return None
    return payload.get("reason") if isinstance(payload, dict) else None


def wake_scheduled_cards(state: dict, dbs: list[str]) -> list[str]:
    """Executes SCHEDULED_UNTIL marks.

    The only parking until a moment is `kanban schedule` with the mark in the
    reason; here the watchdog returns the card to ready once the moment has
    come. A `block` with the mark is forbidden: before the moment it gets a
    level-3 finding (cooldown ALERT_COOLDOWN_MIN), after it a safety unblock,
    so the card does not hang forever. Cards without a mark (orchestrator
    schedules) are left alone. -> [finding lines]."""
    out = []
    for db in dbs:
        board = Path(db).parent.name
        con = sqlite3.connect(db)
        try:
            try:
                rows = con.execute(
                    "SELECT id, status FROM tasks "
                    "WHERE status IN ('scheduled', 'blocked')").fetchall()
                marked = [
                    (task, status, _latest_event_reason(
                        con, task,
                        "scheduled" if status == "scheduled" else "blocked"))
                    for task, status in rows]
            except sqlite3.OperationalError:
                continue    # trimmed harness without a tasks table
        finally:
            con.close()
        for task, status, reason in marked:
            ts = parse_scheduled_until(reason)
            if ts is None:
                continue
            env = {**os.environ, "HERMES_KANBAN_BOARD": board}
            if time.time() >= ts:
                marker = SCHEDULED_UNTIL_RE.search(reason).group(1)
                subprocess.run(
                    ["hermes", "kanban", "unblock", task], capture_output=True,
                    timeout=90, env=env)
                extra = (
                    "" if status == "scheduled"
                    else " Parking rule violation: SCHEDULED_UNTIL in a block "
                         "reason; parking until a moment is done only through "
                         "kanban schedule.")
                text = (
                    f"Card {task} returned to ready by the mark "
                    f"SCHEDULED_UNTIL={marker} in the reason of its "
                    f"{status} parking.{extra}")
                comment(board, task, text)
                out.append(f"[{board}] {text}")
            elif status == "blocked":
                # the moment has not come, but the form is already forbidden
                key = f"{board}:{task}:sched-until-in-block"
                if time.time() - state.get(key, 0) < ALERT_COOLDOWN_MIN * 60:
                    continue
                text = (
                    f"Action (level 3): card {task} is blocked with a free-text "
                    f"SCHEDULED_UNTIL in the reason. This is forbidden: nobody "
                    f"executes a mark in a block. The watchdog will return the "
                    f"card to ready by the mark; next time park with kanban "
                    f"schedule and SCHEDULED_UNTIL=<UTC> in the reason.")
                comment(board, task, text)
                out.append(f"[{board}] {text}")
                state[key] = time.time()
    return out


def _pr_state_cached(pr: str, state: dict) -> str | None:
    """PR state ('MERGED'/'OPEN'/...) cached for one watchdog tick.

    None means gh did not answer (network, token): the condition is NOT taken
    as met, the card is left alone, the next tick retries. The cache lives in
    state, main() saves it at the end of the tick."""
    key = f"merge-wait:pr-state:{pr}"
    hit = state.get(key)
    if isinstance(hit, list) and len(hit) == 2 and \
            time.time() - hit[0] < MERGE_WAIT_PR_TTL_SEC:
        return hit[1]
    # gh has no repository context in the Hermes home, so the repository is
    # named explicitly.
    repo = cfg("FACTORY_REPO", "")
    cmd = ["gh", "pr", "view"] + (["-R", repo] if repo else []) + \
        [pr, "--json", "state"]
    res = subprocess.run(cmd, capture_output=True, timeout=60)
    try:
        st = json.loads(res.stdout or b"{}").get("state")
    except ValueError:
        st = None
    if res.returncode != 0:
        return None    # no cache: a transient gh failure must not mask the PR
    state[key] = [time.time(), st]
    return st


def _merge_wait_texts(con, task: str, body: str | None) -> list[str]:
    """Texts where a written unblock condition lives: the worker's last output
    (worker_result.last_output), recent comments, the block reason, the card
    body."""
    texts = []
    row = con.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind="
        "'worker_result' ORDER BY id DESC LIMIT 1", (task,)).fetchone()
    if row and row[0]:
        try:
            payload = json.loads(row[0])
            if isinstance(payload, dict) and payload.get("last_output"):
                texts.append(payload["last_output"])
        except (ValueError, TypeError):
            pass
    for (cbody,) in con.execute(
            "SELECT body FROM task_comments WHERE task_id=? "
            "ORDER BY id DESC LIMIT 5", (task,)).fetchall():
        texts.append(cbody or "")
    reason = _latest_event_reason(con, task, "blocked")
    if reason:
        texts.append(reason)
    if body:
        texts.append(body)
    return texts


def wake_merged_pr_waits(state: dict, dbs: list[str]) -> list[str]:
    """Executor of written merge conditions on blocked cards.

    A blocked card with a written external merge condition (a line with a wait
    marker + a PR + a merge word) is returned to ready by the watchdog once
    every PR on the condition line is MERGED. Compound conditions and texts
    without a merge trigger are left alone, with no finding: that is the
    native silence, human decisions are not the watchdog's call.
    -> [finding lines]."""
    out = []
    for db in dbs:
        board = Path(db).parent.name
        con = sqlite3.connect(db)
        try:
            try:
                rows = con.execute(
                    "SELECT id, body FROM tasks WHERE status='blocked'"
                ).fetchall()
            except sqlite3.OperationalError:
                continue    # trimmed harness without a tasks table
            texts_by_task = {
                task: _merge_wait_texts(con, task, body)
                for task, body in rows}
        finally:
            con.close()
        for task, texts in texts_by_task.items():
            prs = None
            for text in texts:
                for line in (text or "").splitlines():
                    if not MERGE_WAIT_MARKER_RE.search(line):
                        continue
                    if not MERGE_WAIT_MERGE_RE.search(line):
                        continue
                    if MERGE_WAIT_DENY_RE.search(line):
                        continue
                    found = MERGE_WAIT_PR_RE.findall(line)
                    if not found:
                        continue
                    # the first matching line decides: the condition is one line
                    prs = sorted(set(found))
                    break
                if prs:
                    break
            if not prs:
                continue
            states = {pr: _pr_state_cached(pr, state) for pr in prs}
            if not all(s == "MERGED" for s in states.values()):
                continue
            env = {**os.environ, "HERMES_KANBAN_BOARD": board}
            subprocess.run(
                ["hermes", "kanban", "unblock", task, "--reason",
                 "The watchdog executed the written merge condition (PR "
                 + ", ".join("#" + p for p in prs) + " MERGED, gh pr view). "
                 "The card is returned to ready automatically."],
                capture_output=True, timeout=90, env=env)
            text = (
                f"Card {task} returned to ready by its external merge "
                f"condition ("
                + ", ".join(f"PR #{p} MERGED" for p in prs)
                + "). The native dependency wait and SCHEDULED_UNTIL do not "
                  "cover such waits, so the watchdog executes the condition.")
            comment(board, task, text)
            out.append(f"[{board}] {text}")
    return out


def evaluate(run: dict, db: str, now: datetime, force_alive: bool = False):
    """-> [(signature, finding text)] for one run (over all candidate sessions)."""
    findings = []
    sig_d = self_review_loop(run, db)
    if sig_d:
        findings.append(sig_d)
    sig_e = duplicate_review_lane(run, db, act=not force_alive)
    if sig_e:
        findings.append(sig_e)
    for sess in candidate_sessions(run, db):
        _evaluate_session(run, db, now, force_alive, sess, findings)
    return findings


def _evaluate_session(run, db, now, force_alive, sess, findings):
    run_start = datetime.fromtimestamp(float(run["started_at"]))
    last_api, api, comp, exh = scan_profile_logs(run["profile"], sess)
    hb_age = None
    if not force_alive and run.get("last_heartbeat_at"):
        hb_age = time.time() - float(run["last_heartbeat_at"])
    client_alive = force_alive or (hb_age is not None and hb_age <= HEARTBEAT_FRESH_SEC)
    if client_alive and last_api:
        gap = (now - parse_ts(last_api)).total_seconds() / 60
        if gap > GAP_MIN:
            findings.append((
                "api-silence",
                f"Finding (level 2), signature A: the worker client is alive "
                f"(fresh heartbeat), but the last API call of session {sess[:18]} "
                f"was {gap:.0f} min ago ({parse_ts(last_api):%H:%M}). Run "
                f"{run['id']} of card {run['task_id']} is not moving: check "
                f"context compression (floor above the threshold) and free the slot.",
            ))
    n_comp = sum(1 for ts, sid in comp if sid == sess and parse_ts(ts) >= run_start)
    if n_comp >= 8:
        findings.append((
            "compression thrash",
            f"Finding (level 2), signature B: session {sess[:18]} of run {run['id']} "
            f"on card {run['task_id']} compressed its context {n_comp} times since "
            f"the run started. The irreducible context floor is at the compression "
            f"threshold: compressions do not help and the run burns time. Raise "
            f"the compression threshold above the measured floor.",
        ))
    # Five out of five C findings in one day were false, for three reasons:
    # the window was not tied to the run start (it inherited the session's
    # history), "3 batches in 47 seconds" counted as "half an hour of
    # polling", and reviewer/architect, for whom reading PRs and CI is the
    # work, were flagged as idle. Hence: the window lives only inside the
    # run, the run is at least as old as the window, the batches must spread
    # over at least POLL_SPREAD_MIN, and review profiles are not signature C
    # (their signal is silence A, not polling).
    run_age_min = (now - run_start).total_seconds() / 60
    n_batch, n_work, spread_min, _ = poll_window(
        run["profile"], sess, now, run_start)
    if (run["profile"] not in POLL_EXEMPT_PROFILES
            and run_age_min >= POLL_WIN_MIN
            and n_batch >= POLL_MIN_BATCHES and n_work == 0
            and spread_min >= POLL_SPREAD_MIN):
        findings.append((
            "poll-only",
            f"Finding (level 2), signature C: session {sess[:18]} of run {run['id']} "
            f"on card {run['task_id']} has only polled GitHub for the last "
            f"{POLL_WIN_MIN:.0f} minutes ({n_batch} batches of gh pr view/checks/api, "
            f"cadence {spread_min:.0f}+ min) without edits, commits or builds. "
            f"Someone else's result (a reviewer verdict, a merge, CI longer than "
            f"the run's own cycle) is not awaited inside a run: "
            f"kanban_request_review (reviewer=reviewer explicitly) or "
            f"kanban_complete and exit, the verdict arrives as an event.",
        ))
    elif (run["profile"] not in POLL_EXEMPT_PROFILES
            and run_age_min >= POLL_LONG_SPAN_MIN):
        # Tier 2: the same poll-only but rare (CI rerun samples every ~20
        # minutes put 1-2 batches into the 30-minute window and tier 1 was
        # silent). The window starts at the run's last work: only polls,
        # 3+ batches spread over 30+ minutes.
        _, _, _, last_work = poll_window(
            run["profile"], sess, now, run_start,
            win_min=max(run_age_min, POLL_WIN_MIN))
        floor = last_work if last_work is not None else run_start.timestamp()
        ln_batch, ln_work, ln_spread, _ = poll_window(
            run["profile"], sess, now, run_start,
            win_min=POLL_LONG_SPAN_MIN, floor_ts=floor)
        if (ln_batch >= POLL_LONG_MIN_BATCHES and ln_work == 0
                and ln_spread >= POLL_LONG_SPREAD_MIN):
            since_work = (now.timestamp() - floor) / 60
            findings.append((
                "poll-only",
                f"Finding (level 2), signature C (slow cadence): session "
                f"{sess[:18]} of run {run['id']} on card {run['task_id']} has only "
                f"polled GitHub since its last work ({since_work:.0f} min ago): "
                f"{ln_batch} batches of gh pr/api spread over {ln_spread:.0f} min, "
                f"no edits, commits or builds. Slow external results (CI reruns, "
                f"a verdict) are not awaited inside one run: start, exit, and "
                f"continue with a scheduled card or on the CI completion event.",
            ))
    return findings, sess


def duplicate_review_lane(run: dict, db: str, act: bool = True):
    """Signature E: a duplicate review session.

    Our run: profile=reviewer, claimed with source_status=review, on a card
    WITHOUT an armed-family key (the implementer's card: the review lane
    started the review on its kanban_request_review). The duplicate is
    confirmed when the same PR and head has a LIVE review run of an armed card
    (key reviewer-pr<N>-<head7>[-rN|-pN]) that started EARLIER and has a fresh
    heartbeat. Action: reclaim of the late run + the implementer's card on
    REVIEW_HOLD_PROFILE (a non-profile: the review lane does not spawn it).
    -> (signature, text) or None. restore_parked_review returns the card by
    the verdict of the armed family."""
    if run["profile"] != "reviewer":
        return None
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        try:
            mine = con.execute(
                "SELECT idempotency_key FROM tasks WHERE id=?",
                (run["task_id"],)).fetchone()
        except sqlite3.OperationalError:
            return None        # trimmed harness without a tasks table
        if mine and ARM_KEY_RE.match(mine["idempotency_key"] or ""):
            return None      # the arm itself: "arm on top of native" is the other order
        claimed = con.execute(
            "SELECT payload FROM task_events WHERE run_id=? AND kind='claimed' "
            "ORDER BY id DESC LIMIT 1", (run["id"],)).fetchone()
        if not claimed or '"review"' not in (claimed[0] or ""):
            return None      # not the review lane
        ev = con.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND "
            "kind='review_requested' AND created_at <= ? "
            "ORDER BY id DESC LIMIT 1",
            (run["task_id"], float(run["started_at"]))).fetchone()
        try:
            rows = con.execute(
                "SELECT t.id AS card, t.idempotency_key AS key, "
                "r.id AS run_id, r.started_at AS started, "
                "r.last_heartbeat_at AS hb FROM tasks t "
                "JOIN task_runs r ON r.task_id=t.id AND r.status='running' "
                "AND r.profile='reviewer' "
                "WHERE t.status NOT IN ('done','archived')").fetchall()
        except sqlite3.OperationalError:
            rows = []
    finally:
        con.close()
    if not ev or not ev[0]:
        return None
    payload = ev[0]
    m = (re.search(r"\b([0-9a-f]{40})\b", payload)
         or re.search(r"head[ :]*`?([0-9a-f]{7,40})", payload, re.I))
    if not m:
        return None          # the head is not named: busy-ness not confirmed
    head7 = m.group(1)[:7]
    mpr = re.search(r"#(\d+)", payload)
    keepers = []
    for r in rows:
        key = r["key"] or ""
        km = ARM_KEY_RE.match(key)
        if not km or r["run_id"] == run["id"]:
            continue
        if km.group(2) != head7:
            continue
        if mpr and "pr%s-" % mpr.group(1) not in key + "-":
            continue
        if r["started"] is None or float(r["started"]) >= float(run["started_at"]):
            continue         # the earlier session is ours: the other order applies
        if not r["hb"] or time.time() - float(r["hb"]) > HEARTBEAT_FRESH_SEC:
            continue         # the arm is dead: the review lane may be the only review
        keepers.append(r)
    if not keepers:
        return None
    keeper = keepers[0]
    text = (
        f"Finding (level 2), signature E: run {run['id']} of card "
        f"{run['task_id']} is a duplicate review: the review lane started it "
        f"on kanban_request_review, while head {head7} is already reviewed by "
        f"the earlier armed card {keeper['card']} (run {keeper['run_id']}, "
        f"started {datetime.fromtimestamp(float(keeper['started'])):%H:%M}). "
        f"One head is reviewed by one session: the run is reclaimed, the card "
        f"is parked on the non-profile {REVIEW_HOLD_PROFILE} until the "
        f"verdict. Return: a verdict in the PR, then restore puts the card "
        f"back on reviewer.")
    if act:
        env = {**os.environ, "HERMES_KANBAN_BOARD": run["board"]}
        subprocess.run(
            ["hermes", "kanban", "reclaim", run["task_id"], "--reason",
             "Duplicate review: head %s is reviewed by the armed card %s"
             % (head7, keeper["card"])],
            capture_output=True, timeout=90, env=env)
        done = subprocess.run(
            ["hermes", "kanban", "assign", run["task_id"],
             REVIEW_HOLD_PROFILE],
            capture_output=True, timeout=90, env=env)
        if done.returncode != 0:
            text += " WARNING: parking failed, retry on the next tick."
    return ("review-dup", text)


def _our_worker_alive(pid) -> bool:
    """Is THIS worker alive: the pid exists and its cmdline is kanban_worker_client.

    The cmdline check is mandatory (as in _terminate_worker): a foreign pid
    reusing our dead worker's number does not save the card from reclaim,
    and a live foreign process is not our worker. Linux only (/proc)."""
    if not pid:
        return False
    try:
        with open(f"/proc/{int(pid)}/cmdline", "rb") as f:
            return b"kanban_worker_client" in f.read()
    except (OSError, ValueError):
        return False


def reclaim_orphan_claims(state: dict, dbs: list[str]) -> list[str]:
    """Signature F: orphans after an OOM or a gateway kill.

    An orphan: the card is `running`, its recorded pid does not exist (or is
    a foreign process) and the heartbeat is staler than ORPHAN_HB_STALE_SEC.
    The native reclaim does not pick such cards up while the dispatcher tick
    is held (kanban-dispatch-hold silences the WHOLE tick, reclaim phase
    included: agent.log "Last tick held back: skipped_locked=N"), and after
    owner_admitted + worker_bound the stale-claim and max-runtime reclaims
    hand the bound run to the crash sweep of that same held tick. The CLI
    `kanban reclaim` does not depend on the tick lock (verified live: reclaim
    went through with an active hold). A card once held a dispatcher slot for
    1.5 hours this way.

    -> [level-3 finding lines]; action: reclaim + comment, cooldown
    ALERT_COOLDOWN_MIN; a failed reclaim writes no cooldown (retry next tick)."""
    out: list[str] = []
    now = time.time()
    for db in dbs:
        board = Path(db).parent.name
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        try:
            try:
                rows = con.execute(
                    "SELECT t.id AS tid, t.worker_pid AS pid, "
                    "       COALESCE(r.started_at, t.started_at) AS started, "
                    "       t.last_heartbeat_at AS hb "
                    "FROM tasks t LEFT JOIN task_runs r ON r.id=t.current_run_id "
                    "WHERE t.status='running'").fetchall()
            except sqlite3.OperationalError:
                continue          # trimmed harness without a tasks table
        finally:
            con.close()
        for row in rows:
            tid, pid = row["tid"], row["pid"]
            key = f"{board}:{tid}:orphan-claim"
            if now - state.get(key, 0) < ALERT_COOLDOWN_MIN * 60:
                continue
            started = float(row["started"]) if row["started"] else None
            if started and now - started < ORPHAN_SPAWN_GRACE_SEC:
                continue          # fresh spawn: the pid may not be in /proc yet
            hb = float(row["hb"]) if row["hb"] else None
            if hb is not None and now - hb <= ORPHAN_HB_STALE_SEC:
                continue          # the client is alive (fresh heartbeat)
            if hb is None and started and now - started <= ORPHAN_HB_STALE_SEC:
                continue          # no heartbeat yet, but not stale by time either
            if _our_worker_alive(pid):
                continue          # the worker is alive: signatures A/B, not an orphan
            hb_age = int((now - hb) / 60) if hb else None
            hb_txt = (f"heartbeat stale for {hb_age} min" if hb_age is not None
                      else "heartbeat never written")
            text = (
                f"Action (level 3), signature F: card {tid} is listed as "
                f"running, but pid {pid or 'not recorded'} is not alive, "
                f"{hb_txt}. An orphan of the crash class (the gateway and the "
                f"worker killed together): the dispatcher tick may have been "
                f"held, the crash sweep did not run. The card is reclaimed, its "
                f"max_in_progress slot is free.")
            env = {**os.environ, "HERMES_KANBAN_BOARD": board}
            done = subprocess.run(
                ["hermes", "kanban", "reclaim", tid, "--reason",
                 "Orphan claim (running without a live pid, %s)" % hb_txt],
                capture_output=True, timeout=90, env=env)
            if done.returncode != 0:
                out.append(
                    f"[{board}] Signature F: {tid} looks like an orphan ({hb_txt}, "
                    f"pid {pid}), but the reclaim failed (rc={done.returncode}): "
                    f"retry on the next tick. stderr: "
                    f"{(done.stderr or b'')[:160].decode(errors='replace')}")
                continue          # no cooldown: try again next tick
            out.append(f"[{board}] {text}")
            comment(board, tid, text)
            state[key] = now
    return out


def restore_parked_review(db: str, act: bool = True):
    """Return of parked review cards (signature E).

    A card on REVIEW_HOLD_PROFILE goes back to reviewer once the armed family
    of its head has a final verdict (the summary of a finished review run
    carries "verdict PASS/CHANGES REQUESTED/APPROVED/ACCEPTED"). Without a
    verdict the card stays parked: a second review before the verdict is the
    same duplicate. -> [finding lines]."""
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    board = Path(db).parent.name
    out = []
    try:
        try:
            parked = con.execute(
                "SELECT id FROM tasks WHERE assignee=? AND status='review'",
                (REVIEW_HOLD_PROFILE,)).fetchall()
        except sqlite3.OperationalError:
            parked = []       # trimmed harness without a tasks table
        for p in parked:
            ev = con.execute(
                "SELECT payload FROM task_events WHERE task_id=? AND "
                "kind='review_requested' ORDER BY id DESC LIMIT 1",
                (p["id"],)).fetchone()
            if not ev or not ev[0]:
                continue
            m = (re.search(r"\b([0-9a-f]{40})\b", ev[0])
                 or re.search(r"head[ :]*`?([0-9a-f]{7,40})", ev[0], re.I))
            if not m:
                continue
            head7 = m.group(1)[:7]
            verdict = con.execute(
                "SELECT r.summary FROM tasks t JOIN task_runs r ON "
                "r.task_id=t.id AND r.summary IS NOT NULL "
                "WHERE t.idempotency_key LIKE ? "
                "ORDER BY r.ended_at DESC LIMIT 1",
                ("reviewer-pr%%-%s%%" % head7,)).fetchone()
            if not verdict or not VERDICT_DONE_RE.search(verdict[0] or ""):
                continue
            out.append(
                f"[{board}] Card {p['id']} returned to reviewer: head {head7} "
                f"has a verdict of its armed family "
                f"(\"{(verdict[0] or '')[:80]}...\"), the review lane completes "
                f"the card by that verdict.")
            if act:
                subprocess.run(
                    ["hermes", "kanban", "assign", p["id"], "reviewer"],
                    capture_output=True, timeout=90,
                    env={**os.environ, "HERMES_KANBAN_BOARD": board})
    finally:
        con.close()
    return out


def self_review_loop(run: dict, db: str):
    """Signature D: the run was claimed from the review column (claimed with
    source_status=review) by the same profile as the implementer of the last
    review_requested delivery, and the event's reviewer is empty or equal to
    it. kanban_request_review without reviewer= does not reassign the card,
    and the review dispatch spawns the assigned profile: the coder "reviews"
    itself and delivers to review again, no verdict ever comes.
    -> (signature, text)."""
    con = sqlite3.connect(db)
    try:
        claimed = con.execute(
            "SELECT payload FROM task_events WHERE run_id=? AND kind='claimed' "
            "ORDER BY id DESC LIMIT 1", (run["id"],)).fetchone()
        if not claimed or '"review"' not in (claimed[0] or ""):
            return None
        ev = con.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='review_requested' "
            "AND created_at <= ? ORDER BY id DESC LIMIT 1",
            (run["task_id"], float(run["started_at"]))).fetchone()
    finally:
        con.close()
    if not ev or not ev[0]:
        return None
    try:
        payload = json.loads(ev[0])
    except ValueError:
        return None
    implementer = payload.get("implementer")
    reviewer = payload.get("reviewer")
    if implementer and implementer == run["profile"] and reviewer in (None, run["profile"]):
        return (
            "self-review",
            f"Finding (level 2), signature D: run {run['id']} of card "
            f"{run['task_id']} was claimed from the review column by profile "
            f"{run['profile']}, the same as the implementer of the last delivery "
            f"(implementer={implementer}, reviewer={reviewer!r}): the card "
            f"reviews itself, no verdict will come. Fix: kanban reassign <id> "
            f"reviewer (or request-review again with --reviewer reviewer); "
            f"cause: kanban_request_review without reviewer= does not reassign, "
            f"the review dispatch spawns the assignee.",
        )
    return None


def main() -> int:
    selftest_task = None
    if "--selftest" in sys.argv:
        i = sys.argv.index("--selftest")
        selftest_task = sys.argv[i + 1]
    now = datetime.now()
    state = load_state()
    fired = 0
    for db in board_dbs():
        board = Path(db).parent.name
        if selftest_task is None:
            # signature E: return parked duplicate cards by verdict
            for text in restore_parked_review(db, act=True):
                print(f"[{board}] {text}")
                fired += 1
            # SCHEDULED_UNTIL marks (schedule and the forbidden block form)
            for text in wake_scheduled_cards(state, [db]):
                print(text)
                fired += 1
            # written external merge conditions of blocked cards
            for text in wake_merged_pr_waits(state, [db]):
                print(text)
                fired += 1
            # signature F: orphans (running without a live pid, stale
            # heartbeat) are reclaimed even under a dispatch hold
            for text in reclaim_orphan_claims(state, [db]):
                print(text)
                fired += 1
        for run in running_runs(db):
            if selftest_task is not None and run["task_id"] != selftest_task:
                continue
            findings = evaluate(run, db, now, force_alive=selftest_task is not None)
            for sig, text in findings:
                key = f"{board}:{run['task_id']}:{sig}"
                last = state.get(key, 0)
                age_min = (time.time() - last) / 60
                # A repeated signature C POLL_ESCALATE_AFTER_MIN minutes later
                # with nothing changed is an action (schedule + wake), not a
                # message. It bypasses ALERT_COOLDOWN_MIN on purpose.
                if (sig == "poll-only" and selftest_task is None and last
                        and age_min >= POLL_ESCALATE_AFTER_MIN):
                    fired += 1
                    print(f"[{board}] " + escalate_poll_only(
                        board, run, age_min, state))
                    continue
                if selftest_task is None and time.time() - last < ALERT_COOLDOWN_MIN * 60:
                    continue
                fired += 1
                print(f"[{board}] {text}")
                if selftest_task is None:
                    comment(board, run["task_id"], text)
                    state[key] = time.time()
    if selftest_task is None:
        save_state(state)
        # silence = empty stdout (the native idiom of cron --no-agent)
    else:
        print(f"selftest({selftest_task}): signatures fired: {fired}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
