#!/usr/bin/env python3
"""The CR rework arm hands the rework to the card that owns the PR.

The rework arm (cr_rework_gaps) saw only titles with #N and reviewer keys
and missed a decomposition owner card (rank 3 in live_cards_by_pr, key
coder-<issue>-<slug>, no PR number in its title). On a reviewer CR it
created a duplicate rework card while the owner was already pushing the
fix. The rebase generator in pr-hygiene already hands work to the owner;
the rework arm now uses the same lookup.

Rules:

  A. A live card that owns the PR (pr_hygiene.live_cards_by_pr, ranks
     0-3, the same function, not a second copy) retires the rework arm:
     no card is created, the owner gets a handoff comment with the marker
     owner-rework:<head7> (idempotent between watchdog runs).
  B. Service waits and context quotes are not owners: the arm works for
     them as before.
  C. The native-or-live receipt and the handoff name the verdict source:
     the reviewer agent (verdict comment) or the owner (formal review);
     _latest_verdict returns src.
  D. milestone_pr_orphans does not report a PR with a live owner.

A mutant (owner retirement switched off) must turn the battery red.

Run: python3 test_board_watchdog_rework_owner_handoff.py
"""
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time

# Settings and Hermes home are fixed before the script is loaded.
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="wd-home-")
os.environ.update({
    "FACTORY_REPO": "acme/widget",
    "FACTORY_REPO_PATH": "/srv/hermes/widget",
    "FACTORY_BOARD": "main",
    "FACTORY_OWNER_LOGIN": "owner-login",
    "FACTORY_OWNER_CHAT": "telegram:000000000",
    "FACTORY_BASE_BRANCHES": "main",
})

HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.join(HERE, "test_board_watchdog_board_lint.py")
spec = importlib.util.spec_from_file_location("wd_lint_harness_owner_rw",
                                              HARNESS)
if spec is None or spec.loader is None:
    print("SKIP: cannot load harness")
    sys.exit(0)
h = importlib.util.module_from_spec(spec)
sys.modules["wd_lint_harness_owner_rw"] = h
spec.loader.exec_module(h)

NOW = time.time()
BW_PATH = os.path.join(HERE, "board-watchdog.py")

OPEN_MILESTONES = json.dumps([
    {"title": "2.4.0", "due_on": "2026-12-01T00:00:00Z"},   # active, 80
])

HEAD_A = "9356d0f" + "a" * 33
HEAD_B = "1a2b3c4" + "0" * 33
HEAD_C = "5e6f7a8" + "0" * 33
HEAD_D = "9a0b1c2" + "0" * 33
BRANCH_A = "feature/120-client-demo"

PR_LIST = json.dumps([
    {"number": 501, "isDraft": False, "headRefName": BRANCH_A},
    {"number": 502, "isDraft": False, "headRefName": "feature/77-b"},
    {"number": 503, "isDraft": False, "headRefName": "feature/77-c"},
    {"number": 504, "isDraft": False, "headRefName": "feature/77-d"},
])


def _state(head, kind, src, verdict):
    return {"head": head, "age": 3000, "verdict": verdict,
            "info": ({"kind": kind, "at": NOW - 1500, "body": "verdict",
                      "src": src} if kind else None),
            "head_kind": kind, "owner_review": False, "wcfy_at": None,
            "pr_state": "OPEN", "head_by_pr_author": True,
            "head_commit_at": NOW - 3000, "reply_after_reject": None}


# 501: reviewer CR + live decomposition owner (the incident shape).
# 502: owner CR, no owner card -> arm, receipt names the owner.
# 503: reviewer CR, the only "owner" is a service wait -> arm, receipt
#      names the reviewer agent.
# 504: no verdict -> orphan (control for D).
_LIVE = {
    501: _state(HEAD_A, "reject", "reviewer", True),
    502: _state(HEAD_B, "reject", "owner", True),
    503: _state(HEAD_C, "reject", "reviewer", True),
    504: _state(HEAD_D, None, None, False),
}

OWNER_TITLE = "[coder] issue #120 part 4: demo client and end-to-end tests"
SVC_TITLE = "[issue-close] close issue #99 after merge of PR #503"

TASKS = [
    # the owner of PR 501: no PR number in the title, decomposition key
    # (rank 3 in live_cards_by_pr). The old arm did not see it.
    {"id": "t_owner", "status": "scheduled", "assignee": "coder",
     "priority": 90, "title": OWNER_TITLE,
     "key": "coder-120-client-demo", "age": 400000,
     "body": "Issue: #120. Branch: %s. Deliverable: a PR." % BRANCH_A},
    # a service wait on PR 503: not an owner
    {"id": "t_svc", "status": "blocked", "assignee": "coder",
     "priority": 20, "title": SVC_TITLE, "key": None, "age": 400000,
     "body": "blocked until merge of PR #503."},
]


class FakePH(object):
    """pr-hygiene stub: only live_cards_by_pr and BOARD_DB (hermetic, the
    real sibling is not loaded)."""

    def __init__(self):
        self.BOARD_DB = "/nonexistent"
        self.calls = []

    def live_cards_by_pr(self, head_of):
        self.calls.append((dict(head_of), self.BOARD_DB))
        return {
            501: [("t_owner", OWNER_TITLE, 3, "coder-120-client-demo",
                   "coder")],
            503: [("t_svc", SVC_TITLE, 2, "", "coder")],
        }


def load_bw(src=None, tag="owner_rw"):
    """board-watchdog module from the real file or from a mutated src."""
    sbx = tempfile.mkdtemp(prefix="bw-sbx-owner-rw-")
    path = BW_PATH if src is None else os.path.join(sbx, "wd-mut.py")
    if src is not None:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(src)
        for nb in ("milestone_bands.py", "board_native_guard.py",
                   "factory_config.py"):
            shutil.copy(os.path.join(HERE, nb), os.path.join(sbx, nb))
    sp = importlib.util.spec_from_file_location("bw_mod_%s" % tag, path)
    m = importlib.util.module_from_spec(sp)
    sys.modules["bw_mod_%s" % tag] = m
    sp.loader.exec_module(m)
    m.STATE = os.path.join(sbx, "state.json")
    m.LESSON6_CACHE = os.path.join(sbx, "lesson6.json")
    if hasattr(m, "milestone_bands"):
        m.milestone_bands.CACHE_PATH = os.path.join(sbx, "ms.json")
        m.milestone_bands._MEMO = {}
    return m


def make_run(db, calls, bodies):
    """run stub: gh from the fixture, kanban commands applied to the db."""

    def _run(cmd, timeout=120):
        calls.append(cmd)
        if cmd[:3] == ["gh", "pr", "list"]:
            return PR_LIST
        if cmd[:2] == ["gh", "api"]:
            return OPEN_MILESTONES
        if cmd[0] == "gh":
            return ""
        if len(cmd) > 5 and cmd[1] == "kanban":
            action, tid = cmd[4], cmd[5]
            con = sqlite3.connect(db, timeout=10)
            try:
                if action == "comment":
                    con.execute(
                        "insert into task_comments (task_id, body,"
                        " created_at) values (?,?,?)",
                        (tid, cmd[6], int(time.time())))
                elif action == "create":
                    body = ""
                    if "--body-file" in cmd:
                        with open(cmd[cmd.index("--body-file") + 1],
                                  encoding="utf-8") as fh:
                            body = fh.read()
                    key = (cmd[cmd.index("--idempotency-key") + 1]
                           if "--idempotency-key" in cmd else None)
                    prio = int(cmd[cmd.index("--priority") + 1])
                    new_id = "t_new%d" % len(bodies)
                    bodies[new_id] = body
                    if key:
                        bodies[key] = body
                    con.execute(
                        "insert into tasks (id, title, status, assignee,"
                        " priority, created_at, idempotency_key, body)"
                        " values (?,?,?,?,?,?,?,?)",
                        (new_id, tid, "ready", "coder", prio,
                         int(time.time()), key, body))
                    con.commit()
                    return json.dumps({"id": new_id})
                con.commit()
            finally:
                con.close()
        return "ok"

    return _run


def con_key(db, tid):
    con = sqlite3.connect(db)
    row = con.execute("select coalesce(idempotency_key,'') from tasks"
                      " where id=?", (tid,)).fetchone()
    con.close()
    return row[0] if row else ""


def body_of(bodies, key_prefix):
    for k, body in bodies.items():
        if k and k.startswith(key_prefix):
            return body
    return None


def list_live(db):
    con = sqlite3.connect(db)
    rows = list(con.execute(
        "SELECT id, title, body, assignee, status, priority,"
        " idempotency_key FROM tasks WHERE status NOT IN"
        " ('done','archived')"))
    con.close()
    return rows


def run_layer(mod, db, slug, calls, bodies):
    real = (mod.run, mod.board_repo, mod._pr_review_state)
    mod.run = make_run(db, calls, bodies)
    mod.board_repo = lambda s: "acme/demo"
    mod._pr_review_state = lambda repo, number: _LIVE.get(number)
    try:
        return mod.review_layer_check(slug)
    finally:
        mod.run, mod.board_repo, mod._pr_review_state = real


def battery(mod):
    problems = []

    def want(name, cond, detail=""):
        print("%s %s%s" % ("ok  " if cond else "FAIL", name,
                           (" :: %s" % (detail,)) if detail and not cond
                           else ""))
        if not cond:
            problems.append(name)

    # the stub sits under the key _sibling_pr_hygiene looks up for THIS
    # module (a mutant copy lives in its own directory)
    ph = FakePH()
    ph_path = os.path.join(os.path.dirname(os.path.abspath(mod.__file__)),
                           "pr-hygiene.py")
    sys.modules["hermes_sibling:" + os.path.abspath(ph_path)] = ph

    slug, db, _ = h.make_board(tasks=TASKS)
    mod.BOARDS = os.path.dirname(os.path.dirname(db))

    calls, bodies = [], {}
    try:
        found = run_layer(mod, db, slug, calls, bodies)
    except Exception as exc:
        problems.append("A0: review_layer_check failed: %s: %s"
                        % (type(exc).__name__, exc))
        return problems
    ids = [f[0] for f in found]

    con = sqlite3.connect(db)
    rework_tasks = [r[0] for r in con.execute(
        "select id from tasks where idempotency_key like"
        " 'coder-pr%-rework-%'")]
    handoffs = [r[0] for r in con.execute(
        "select body from task_comments where task_id='t_owner'"
        " and body like '%owner-rework:%'")]
    con.close()
    keys = [con_key(db, t) for t in rework_tasks]

    # --- A: the owner retires the arm ------------------------------------
    want("A1 no duplicate rework card on PR 501",
         not [k for k in keys if k.startswith("coder-pr501-rework")],
         str(keys))
    want("A2 a handoff comment went to the owner", len(handoffs) == 1,
         str(handoffs))
    if handoffs:
        want("A3 handoff names the reviewer agent, the rule and the head",
             "the reviewer agent" in handoffs[0]
             and "one PR = one card" in handoffs[0]
             and "owner-rework:" + HEAD_A[:7] in handoffs[0], handoffs[0])
    want("A4 finding revlayer:rework-owner:501",
         any("revlayer:rework-owner:501" in i for i in ids), str(ids))
    want("A5 the owner lookup went through live_cards_by_pr with the board"
         " db (not a second copy)",
         bool(ph.calls) and ph.calls[0][1] == db
         and ph.calls[0][0].get(501) == BRANCH_A, str(ph.calls))
    want("A6 the sibling BOARD_DB is restored after the lookup",
         ph.BOARD_DB == "/nonexistent", ph.BOARD_DB)

    # --- B/C: the arm still works, the receipt names the source ----------
    want("B1 control without an owner: arm for PR 502 created",
         any(k.startswith("coder-pr502-rework") for k in keys), str(keys))
    body_b = body_of(bodies, "coder-pr502-rework")
    want("B2 receipt for PR 502 names the owner (formal review)",
         bool(body_b) and "verdict of the owner on a head" in body_b,
         (body_b or "")[:400])
    want("B3 a service wait is not an owner: arm for PR 503 created",
         any(k.startswith("coder-pr503-rework") for k in keys), str(keys))
    body_c = body_of(bodies, "coder-pr503-rework")
    want("B4 receipt for PR 503 names the reviewer agent",
         bool(body_c) and "verdict of the reviewer agent on a head" in body_c,
         (body_c or "")[:400])

    # --- D: orphans ------------------------------------------------------
    want("D1 PR 501 with a live owner is not an orphan",
         not any("ms-orphan:501" in i for i in ids), str(ids))
    want("D2 control: PR 504 without a card is an orphan",
         any("ms-orphan:504" in i for i in ids), str(ids))

    # --- E: the handoff is idempotent ------------------------------------
    calls2 = []
    run_layer(mod, db, slug, calls2, {})
    con = sqlite3.connect(db)
    cnt2 = con.execute(
        "select count(*) from task_comments where task_id='t_owner'"
        " and body like '%owner-rework:%'").fetchone()[0]
    con.close()
    want("E1 a repeated run does not repeat the handoff", cnt2 == 1,
         str(cnt2))
    want("E2 a repeated run does not create a card for PR 501",
         not [c for c in calls2
              if len(c) > 5 and c[4] == "create" and "PR #501" in c[5]],
         str(calls2))

    # --- F: verdict source units -----------------------------------------
    vbody = ("## Review verdict\n\n**Head: `%s`**: **REQUEST CHANGES**."
             "\n\n## Calibration\n- no misses." % HEAD_A)
    d_rev = mod._latest_verdict(
        {"reviews": [],
         "comments": [{"body": vbody,
                       "createdAt": "2026-11-19T10:52:59Z"}]}, HEAD_A)
    want("F1 a verdict comment gives src=reviewer",
         bool(d_rev) and d_rev.get("src") == "reviewer"
         and d_rev.get("kind") == "reject", str(d_rev))
    d_own = mod._latest_verdict(
        {"reviews": [{"state": "CHANGES_REQUESTED",
                      "submittedAt": "2026-11-19T09:00:00Z",
                      "commit": {"oid": HEAD_B}, "body": ""}],
         "comments": []}, HEAD_B)
    want("F2 a formal review gives src=owner",
         bool(d_own) and d_own.get("src") == "owner", str(d_own))
    want("F3 _verdict_source_name as text",
         mod._verdict_source_name({"src": "reviewer"}) == "the reviewer agent"
         and mod._verdict_source_name({"src": "owner"}) == "the owner"
         and mod._verdict_source_name(None) == "the owner")

    # --- G: the blind spot is closed by the map, gaps stay as they were --
    gaps = mod.cr_rework_gaps({501: _LIVE[501]}, list_live(db))
    want("G1 cr_rework_gaps still sees the gap (title without #501)",
         gaps == [(501, HEAD_A)], str(gaps))
    omap = mod._rework_owner_map(db, {501: BRANCH_A}, list_live(db))
    want("G2 _rework_owner_map finds t_owner with rank 3",
         bool(omap.get(501)) and omap[501][0][0] == "t_owner"
         and omap[501][0][2] == 3, str(omap))
    ho = mod._rework_owner_handoff(omap, 501, HEAD_A, _LIVE[501]["info"])
    want("G3 handoff points at the owner and the source",
         ho == ("t_owner", "the reviewer agent"), str(ho))
    omap_svc = mod._rework_owner_map(db, {503: "feature/77-c"},
                                     list_live(db))
    want("G4 a service wait is filtered out of owners",
         not omap_svc.get(503), str(omap_svc))
    return problems


def main():
    problems = battery(load_bw())

    # mutant: owner retirement of the arm switched off -> a duplicate on
    # PR 501, no handoff. The battery must turn red.
    src = open(BW_PATH, encoding="utf-8").read()
    mut = src.replace(
        "        handoff = _rework_owner_handoff(",
        "        handoff = None and _rework_owner_handoff(")
    assert mut != src, "mutant was not applied"
    if not battery(load_bw(mut, tag="owner_rw_mut")):
        problems.append("MUT: the battery stayed green with owner"
                        " retirement switched off")

    print("\nTOTAL rework owner handoff: %s" % (
        "OK" if not problems else "FAIL %s" % "; ".join(problems)))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
