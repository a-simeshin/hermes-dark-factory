#!/usr/bin/env python3
"""Machine gate for plan review, for any decompose path. The full cycle:
  create the review -> block the children -> [RETURN -> rework(architect) ->
  re-review -> ...] -> ACCEPTED -> unblock the children.
Hermes link semantics: task.parents = the tasks it depends on (upstream).
The decomposition root depends on all its children => len(root.parents) is
the number of children.
The gate: a root with more than 3 children OR a critical topic must have a
plan review [reviewer].
The verdict is read mechanically from the review card's latest_summary
(ACCEPTED/RETURN at the start of the text); on RETURN the gate itself
creates a rework card for the architect, and after its done creates the
re-review itself. The deadlock "the verdict landed as a comment on the root
and the pipeline has nothing to pick up" is excluded.

The verdict words are a contract with the reviewer prompts (the
adversarial-review skill: ACCEPTED / RETURN). The defaults match it; override
with FACTORY_PLAN_ACCEPT_WORDS and FACTORY_PLAN_RETURN_WORDS (comma-separated,
case-insensitive, matched as a prefix of the summary). The first word of each
list is the one the review card prompt asks the reviewer to write."""
import json, subprocess, re, os, sys
import importlib.util as _ilu

from factory_config import cfg, cfg_list, hermes_home

BOARD = cfg("FACTORY_BOARD", "main")
STATE_F = os.path.join(hermes_home(), "usage-ledger", "plan-review-gate.json")
# FACTORY_OWNER_CHAT is "<platform>:<chat id>"; the notify subscription needs both.
NOTIFY_PLATFORM, _, CHAT = cfg("FACTORY_OWNER_CHAT").partition(":")
# Ceiling of RETURN -> rework -> review loops. Without it the gate loops
# forever: chains once reached double-digit cycles, burned money on the
# architect model and spawned dozens of cards. After the ceiling: escalation
# to the owner, the cycle is closed.
MAX_CYCLES = 2
# Topics that make a plan CRITICAL (a plan review is mandatory, and a plan
# document must be published before implementation). Override per project.
CRIT_RE = re.compile(cfg("FACTORY_CRITICAL_PATTERN",
                         r"authoriz|oauth|\bauth\b|security|migration|\.github|\brelease"), re.I)
REWORK_RE = re.compile(r"Rework the decomposition", re.I)
PLAN_RE = re.compile(r"plan review|plan-review", re.I)
ACCEPT_WORDS = cfg_list("FACTORY_PLAN_ACCEPT_WORDS", ("ACCEPTED", "APPROVE"))
RETURN_WORDS = cfg_list("FACTORY_PLAN_RETURN_WORDS", ("RETURN", "REJECT"))
ACCEPT_RE = re.compile(r"^\W{0,8}(%s)" % "|".join(re.escape(w) for w in ACCEPT_WORDS), re.I)
RETURN_RE = re.compile(r"^\W{0,8}(%s)" % "|".join(re.escape(w) for w in RETURN_WORDS), re.I)
ACCEPT_WORD = ACCEPT_WORDS[0]
RETURN_WORD = RETURN_WORDS[0]

def kb(*args, timeout=60):
    return subprocess.run(["hermes", "kanban", "--board", BOARD, *args],
                          capture_output=True, text=True, timeout=timeout)

def kb_json(*args):
    r = kb(*args, "--json")
    try:
        return json.loads(r.stdout)
    except (json.JSONDecodeError, ValueError):
        return None

def show(tid):
    return kb_json("show", tid) or {}

def notify_subscribe(tid):
    kb("notify-subscribe", tid, "--platform", NOTIFY_PLATFORM, "--chat-id", CHAT,
       "--chat-type", "dm", "--delivery-mode", "notify")

PLANNING_TITLES = ("Plan review", "Plan re-review", "Rework the decomposition")
# Model for plan review (provider-specific, empty = the reviewer profile's own
# model): an error in a plan multiplies over the whole decomposition, so depth
# is worth paying for here. Pull request review stays on the profile's model.
_PLAN_MODEL = cfg("FACTORY_PLAN_REVIEW_MODEL", "")
_PLAN_PROVIDER = cfg("FACTORY_PLAN_REVIEW_PROVIDER", "")
PLAN_REVIEW_MODEL = (("--model", _PLAN_MODEL) if _PLAN_MODEL else ()) + \
                    (("--provider", _PLAN_PROVIDER) if _PLAN_PROVIDER else ())


def _live_keys():
    """Keys of the board's live cards for the native-or-live guard."""
    import sqlite3
    db = os.path.join(hermes_home(), "kanban", "boards", BOARD, "kanban.db")
    if not os.path.exists(db):
        return []
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
        rows = con.execute(
            "SELECT idempotency_key FROM tasks "
            "WHERE status NOT IN ('done','archived') "
            "AND idempotency_key IS NOT NULL").fetchall()
        con.close()
        return [r[0] for r in rows]
    except sqlite3.Error:
        return []


def _guard_module():
    """board_native_guard.py next to this script, shared through sys.modules."""
    existing = sys.modules.get("board_native_guard")
    if existing is not None and hasattr(existing, "native_or_live"):
        return existing
    spec = _ilu.spec_from_file_location(
        "board_native_guard",
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "board_native_guard.py"))
    if spec is None or spec.loader is None:
        raise RuntimeError("board_native_guard spec did not load")
    mod = _ilu.module_from_spec(spec)
    sys.modules["board_native_guard"] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop("board_native_guard", None)
        raise
    return mod


def create(title, body, assignee, idem, *extra):
    # Priority = closeness to merging: planning must not outrun code,
    # otherwise the only worker slot goes to talking about work, not to the work.
    # The native-or-live rule: ask the shared helper before create. Without
    # the helper nothing is created (fail closed).
    try:
        board_native_guard = _guard_module()
    except Exception:
        return None
    covered, guard = board_native_guard.native_or_live(
        idem, live_keys=_live_keys(),
        native_checked=[board_native_guard.NATIVE_TASK_LINKS,
                        board_native_guard.NATIVE_VERDICT],
        why="plan review or rework by plan-review-gate: Hermes has no "
            "native machine review of plans that reads the verdict from "
            "latest_summary")
    if covered:
        return None
    body = board_native_guard.guard_section(guard) + body
    planning = any(p in title for p in PLANNING_TITLES)
    priority = "10" if planning else "20"
    if planning and assignee == "reviewer":
        extra = tuple(extra) + PLAN_REVIEW_MODEL
    r = kb("create", title, "--body", body, "--assignee", assignee,
           "--idempotency-key", idem, "--priority", priority, *extra)
    m = re.search(r"t_[0-9a-f]{8}", r.stdout + r.stderr)
    return m.group(0) if m else None

def review_body(pid, title, kid_lines, crit, cycle):
    if cycle > 1:
        again = (f" RE-REVIEW (cycle {cycle}). STRICTLY INCREMENTAL MODE: "
                 f"1) list the findings of the previous verdict (comments on root {pid} and the review card of the previous cycle); "
                 f"2) for EACH finding answer: closed or not, with proof (card id, file, line); "
                 f"3) you may {RETURN_WORD} ONLY if an unclosed finding from this list remains. "
                 f"Do NOT look for new objections by the checklist: the plan has already passed it. "
                 f"If all previous findings are closed and you see a NEW problem: the verdict is still {ACCEPT_WORD}, "
                 f"and the new problem goes into a separate rework card (kanban create --assignee architect) "
                 f"mentioned in the summary. A new finding must not block the start of work, "
                 f"except when the plan is physically unexecutable (a circular dependency, a nonexistent runner).")
    else:
        again = ""
    return (f"MACHINE GATE (plan-review-gate).{again} Root: {pid} \"{title}\". Children: {kid_lines}. "
            f"Check the decomposition plan against the project conventions (the decomposition-contract skill: acceptance criteria completeness, "
            f"all release lines, parallel edits of the same files, separate test layers, Stack keys in the cards). "
            f"SEPARATELY check the grounding in sources: the plan must name concrete files (file:line) and the external sources "
            f"against which library signatures and behaviour were checked (library documentation for the exact version, SDK sources, official documentation). "
            f"A claim about someone else's API without a source reference counts as unverified: that is how a plan once got a tool matrix "
            f"for a class that does not exist in the library at all. If there are no sources, that is a finding, not nitpicking. "
            f"Use the same tools yourself when checking: a symbol-level code reader for reading code, library documentation for the version"
            + (". CRITICAL: check that a plan document was published to the issue mirror and to the owner BEFORE implementation starts; if not, " + RETURN_WORD if crit else "") +
            f". Verdict: finish the card so that the final summary STARTS with the word {ACCEPT_WORD} or {RETURN_WORD} (machine parsing). "
            f"{ACCEPT_WORD} -> the gate unblocks the children automatically. {RETURN_WORD} -> describe the holes in a comment on {pid}; the gate itself creates a rework card for the architect. "
            f"THE FORMAT IS MANDATORY: write 'DEFERRED: t_xxxxxxxx, t_yyyyyyyy' as the last line of the summary - the ids of the cards "
            f"created for non-blocking findings, or 'DEFERRED: none'. The gate parses it mechanically: "
            f"without the line it creates a card from the verdict text itself, so that findings do not get lost.")

def rework_body(pid, title, prid):
    return (f"Plan review {prid} returned {RETURN_WORD}. Root: {pid} \"{title}\". Steps: "
            f"1) Read the findings: hermes kanban --board {BOARD} show {prid} and the comments on {pid}. "
            f"2) Close every hole: the plan contract (major/critical: a plan document; CRITICAL: publication to the issue mirror and the owner's approval BEFORE implementation), "
            f"the test infrastructure contract, the test pyramid, Stack keys in the children, "
            f"the map of parallel edits of vendored files, measurable acceptance criteria. "
            f"3) Close the findings SURGICALLY, do NOT rebuild the decomposition from scratch: a recreated graph gives the reviewer "
            f"a new surface for new objections, and the cycle repeats (a chain once ran 14 cycles and burned a large budget). "
            f"Archive/create children only where a finding directly requires it. "
            f"In the final summary list the findings line by line: 'finding N -> what was done -> where to look'. "
            f"4) Update/recreate the child cards (edit blocked children with comments and kanban edit, archive the extras). "
            f"Create NEW children ONLY with --initial-status blocked (block reason: awaiting plan review) and parent links to the root: "
            f"otherwise the dispatcher (5 min tick) spawns them before the gate (15 min tick) manages to block them, and the implementation slips past the re-review. "
            f"5) Do NOT unblock the children and do NOT create the re-review: the gate creates it itself after this card completes. "
            f"Conventions: the decomposition-contract skill.")

def block_kids(root_det, by_id, prid):
    blocked = []
    for k in (root_det.get("parents") or []):
        kt = by_id.get(k)
        if kt and kt.get("status") in ("todo", "ready") and kt.get("assignee") != "reviewer":
            if kb("block", k, f"awaiting plan review {prid}").returncode == 0:
                blocked.append(k)
    return blocked

def unblock_kids(root_det, by_id):
    freed = []
    for k in (root_det.get("parents") or []):
        kt = by_id.get(k)
        if kt and kt.get("status") == "blocked":
            if kb("unblock", k).returncode == 0:
                freed.append(k)
    return freed

def parse_verdict(s):
    """'accept' | 'return' | 'unknown' from a review summary (pure function)."""
    s = s or ""
    if ACCEPT_RE.search(s):
        return "accept"
    if RETURN_RE.search(s):
        return "return"
    # fallback: the verdict somewhere in the first 200 characters
    head = s[:200]
    if ACCEPT_WORD in head:
        return "accept"
    if RETURN_WORD in head:
        return "return"
    return "unknown"

def verdict_of(prid):
    det = show(prid)
    if (det.get("task") or {}).get("status") not in ("done",):
        return None
    return parse_verdict(det.get("latest_summary"))


def handle_deferred(pid, review_id, by_id):
    """Deferred findings of an accepted review must not disappear.
    The reviewer must end the summary with 'DEFERRED: <id...>' or 'DEFERRED: none'.
    No line, or the ids do not exist: the gate creates a card from the verdict text itself."""
    det = show(review_id)
    summary = det.get("latest_summary") or ""
    m = re.search(r"DEFERRED\s*:\s*(.+)$", summary, re.M | re.I)
    if m:
        tail = m.group(1).strip()
        if re.match(r"(none|-)", tail, re.I):
            return "none"
        ids = [i for i in re.findall(r"t_[0-9a-f]{8}", tail)
               if i in by_id or (show(i).get("task") or {}).get("id")]
        if ids:
            return ", ".join(ids)
    body = ("Deferred findings of the accepted plan review " + review_id + " (root " + pid + "). "
            "The reviewer left no machine-readable list of cards, so the gate saved the whole verdict.\n"
            "Task: bring the findings into the common development cycle instead of leaving them on the board.\n"
            "1) Go through the verdict: what really remains to be done, what is no longer relevant (close the irrelevant ones with a comment and a reason).\n"
            "2) For each remaining finding create an issue mirror from the convention's template "
            "(Current behavior with a screenshot for UI / After / User benefit / Acceptance criteria), "
            "set the severity label and wait-for-triage, and refer to the root issue of the chain. Deduplication is mandatory: "
            "if an issue on this topic already exists, add a comment there.\n"
            "3) Leave the mirror card on the board in triage: the owner decides the priority, the pipeline does not take it before triage.\n"
            "4) In the summary list: finding -> issue #N (or the reason for rejection).\n\nVerdict:\n" + summary[:2000])
    rid = create("Deferred findings of plan review " + pid + ": add to the backlog", body,
                 "architect", "plan-deferred-" + review_id, "--triage")
    if rid:
        notify_subscribe(rid)
        return rid + " (created by the gate, in triage)"
    return "could not create the card"

def _queue_behind_open_rework(rwid, tasks):
    """Queues a new rework behind an already open one, if there is one.

    Returns the id of the card it queued behind, or None. A dependency-kind
    block is lifted automatically when the parent completes, so the queue
    cannot hang silently (unlike a manual blocked).
    """
    open_reworks = [t["id"] for t in tasks
                    if t["id"] != rwid
                    and REWORK_RE.search(t.get("title") or "")
                    and t.get("status") not in ("done", "archived")]
    if not open_reworks:
        return None
    ahead = sorted(open_reworks)[-1]
    kb("link", ahead, rwid)
    kb("block", rwid,
       "Waiting for the rework " + ahead + ": parallel edits of one graph overwrite "
       "each other. When you take the card, first read the result of " + ahead + ": if "
       "it already closed the same hole, finish this card as covered with a justification, "
       "without redoing the graph.",
       "--kind", "dependency")
    return ahead


def existing_review(pid, tasks):
    """An open review card for this root, whoever created it.

    Searched by title, not by links: a card created by an agent by hand is
    not linked to the root and does not show up in children.
    """
    for t in tasks:
        if t.get("status") in ("done", "archived"):
            continue
        title = t.get("title") or ""
        if pid in title and PLAN_RE.search(title):
            return t["id"]
    return None


def main():
    state = json.load(open(STATE_F)) if os.path.exists(STATE_F) else {}
    handled = set(state.get("handled") or [])
    cycles = state.get("cycles") or {}
    tasks = kb_json("list") or []
    by_id = {t["id"]: t for t in tasks}
    log = []

    # DRAIN MODE: plan review moved to Hermes' built-in review lane, so the
    # gate no longer creates new review cards. Servicing of already open
    # cycles below keeps working until the last one closes. Details are in
    # the architect's SOUL, the section on handing a plan to the built-in
    # review lane.
    DRAIN_ONLY = True

    # Phase 1: new roots without a plan review (as before)
    for t in ([] if DRAIN_ONLY else tasks):
        pid = t["id"]
        if pid in handled or pid in cycles or t.get("status") in ("archived", "done"):
            continue
        det = show(pid)
        kids = [k for k in (det.get("parents") or []) if k in by_id]
        if len(kids) < 2 or (det.get("children") or []):
            continue
        kid_tasks = [by_id[k] for k in kids]
        if all(k.get("status") in ("done", "archived") for k in kid_tasks):
            handled.add(pid); continue
        if any(PLAN_RE.search(k.get("title") or "") for k in kid_tasks):
            handled.add(pid); continue
        crit_text = (t.get("title") or "") + " " + (t.get("body") or "")[:400] + " " + \
                    " ".join(k.get("title") or "" for k in kid_tasks)
        crit = bool(CRIT_RE.search(crit_text))
        if not (len(kids) > 3 or crit):
            handled.add(pid); continue
        kid_lines = "; ".join(f"{k['id']} [{k.get('assignee') or '?'}] {(k.get('title') or '')[:60]}" for k in kid_tasks)
        adopted = existing_review(pid, tasks)
        if adopted:
            # Someone already created a review for this root: take it under
            # the gate's control, otherwise two reviewers issue two verdicts
            # on one plan.
            cycles[pid] = {"review": adopted, "cycle": 1, "crit": crit}
            handled.add(pid)
            kb("comment", pid, f"plan-review-gate: review {adopted} already exists, taking it under the gate's control "
                               f"instead of creating a second card.", "--author", "plan-review-gate")
            log.append(f"adopted {adopted} for {pid}")
            continue
        prid = create(f"Plan review of decomposition {pid}" + (" [CRITICAL]" if crit else ""),
                      review_body(pid, t.get("title", ""), kid_lines, crit, 1),
                      "reviewer", f"plan-review-{pid}-c1")
        if not prid:
            continue
        kb("promote", prid)
        notify_subscribe(prid)
        for k in kid_tasks:
            kb("link", prid, k["id"])
        blocked = block_kids(det, by_id, prid)
        kb("comment", pid, f"plan-review-gate: created {prid}" + (" [CRITICAL]" if crit else "") +
           f"; blocked: {', '.join(blocked) or 'nobody'}", "--author", "plan-review-gate")
        cycles[pid] = {"review": prid, "cycle": 1, "rework": None, "crit": crit}
        log.append(f"new review {pid} -> {prid} crit={crit}")

    # Phase 2: drive the open cycles to ACCEPTED
    for pid, c in list(cycles.items()):
        if c.get("closed"):
            continue
        root_t = by_id.get(pid)
        if root_t is None or root_t.get("status") in ("archived", "done"):
            c["closed"] = True; continue
        det = show(pid)
        # While a cycle is open, every tick blocks freshly created todo/ready
        # children of the root: the architect may have created them without
        # --initial-status blocked, and the dispatcher (5 min tick) spawns
        # them before our tick (15 min). This does not close the race window
        # entirely (a running card cannot be blocked) but narrows it; the full
        # closure is the blocked-created rule in rework_body.
        newly = block_kids(det, by_id, c["review"])
        if newly:
            log.append(f"late-block {pid}: {newly}")
        # 2a: rework finished -> re-review
        if c.get("rework"):
            rw = by_id.get(c["rework"]) or (show(c["rework"]).get("task") or {})
            st = rw.get("status")
            if st in ("done",):
                nxt = c["cycle"] + 1
                kid_tasks = [by_id[k] for k in (det.get("parents") or []) if k in by_id]
                kid_lines = "; ".join(f"{k['id']} [{k.get('assignee') or '?'}] {(k.get('title') or '')[:60]}" for k in kid_tasks)
                prid = create(f"Plan re-review {pid} (cycle {nxt})" + (" [CRITICAL]" if c.get("crit") else ""),
                              review_body(pid, root_t.get("title", ""), kid_lines, c.get("crit"), nxt),
                              "reviewer", f"plan-review-{pid}-c{nxt}")
                if prid:
                    kb("promote", prid)
                    notify_subscribe(prid)
                    for k in kid_tasks:
                        kb("link", prid, k["id"])
                    block_kids(det, by_id, prid)
                    c.update({"review": prid, "cycle": nxt, "rework": None})
                    log.append(f"re-review {pid} -> {prid} cycle={nxt}")
            elif st in ("archived",):
                c["rework"] = None  # the rework was removed: recreate on the next tick
            continue
        # 2b: review finished -> verdict
        v = verdict_of(c["review"])
        if v == "accept":
            freed = unblock_kids(det, by_id)
            deferred = handle_deferred(pid, c["review"], by_id)
            kb("comment", pid,
               f"plan-review-gate: {c['review']} {ACCEPT_WORD}; unblocked: {', '.join(freed) or 'nobody (already free)'}; "
               f"deferred findings: {deferred}",
               "--author", "plan-review-gate")
            c["closed"] = True
            c["deferred"] = deferred
            log.append(f"accepted {pid}: freed {freed}; deferred {deferred}")
        elif v == "return":
            if c["cycle"] >= MAX_CYCLES:
                # The children are unblocked: freezing the subtree costs more
                # than the residual risk, and a review that demands proof from
                # a card the gate itself blocks is a circle (a PR once stood
                # red because of a card frozen by this gate).
                freed_on_escalation = unblock_kids(show(pid), by_id)
                msg = ("plan-review-gate: ESCALATION. " + c["review"] + " is " + RETURN_WORD + " again on cycle "
                       + str(c["cycle"]) + " (ceiling " + str(MAX_CYCLES) + "). New reworks are "
                       "NOT created: the plan needs a human decision (narrow the scope, "
                       "drop the requirement or accept as is). The children are UNBLOCKED and work continues: "
                       + (", ".join(freed_on_escalation) or "there was nobody to unblock") +
                       ". The reviewer's open objections stay in the card and are checked "
                       "at the next review instead of stopping the pipeline.")
                kb("comment", pid, msg, "--author", "plan-review-gate")
                notify_subscribe(pid)
                c["closed"] = True
                c["escalated"] = True
                log.append("ESCALATED " + pid + ": cycle " + str(c["cycle"]))
                continue
            rwid = create(f"Rework the decomposition {pid} per the plan review verdict (cycle {c['cycle']})",
                          rework_body(pid, root_t.get("title", ""), c["review"]),
                          "architect", f"plan-rework-{pid}-c{c['cycle']}")
            if rwid:
                kb("promote", rwid)
                # Reworks go one at a time: parallel edits of one graph
                # overwrite each other (three cards once worked on one hole).
                queued = _queue_behind_open_rework(rwid, tasks)
                kb("comment", pid, f"plan-review-gate: {c['review']} {RETURN_WORD} -> rework {rwid} (architect)"
                   + (f"; waiting for {queued}" if queued else ""),
                   "--author", "plan-review-gate")
                c["rework"] = rwid
                log.append(f"returned {pid} -> rework {rwid}"
                           + (f" (queued behind {queued})" if queued else ""))
        elif v == "unknown" and not c.get("nudged"):
            kb("comment", c["review"], f"plan-review-gate: could not parse the verdict from the summary. Finish the card with a summary starting with {ACCEPT_WORD} or {RETURN_WORD}.",
               "--author", "plan-review-gate")
            c["nudged"] = True
            log.append(f"unknown verdict {pid} review={c['review']}")

    state = {"handled": sorted(handled), "cycles": cycles}
    os.makedirs(os.path.dirname(STATE_F), exist_ok=True)
    json.dump(state, open(STATE_F, "w"), ensure_ascii=False)
    print("\n".join(log) if log else "no changes")

if __name__ == "__main__":
    main()
