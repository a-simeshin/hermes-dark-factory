#!/usr/bin/env python3
"""An issue of the active milestone gets an executor card within one cycle.

Why: an urgent bug issue of the active milestone ("Owner: start now. This
blocks the release tag") stood an hour without an executor. Issue events did
not arrive (the webhook subscription listened only to issue_comment and
pull_request_review), and the admission no-executor finding has a 24 h
threshold. A card appeared only after a separate "start now" comment.

Three closing paths and their checks:

  A. The webhook filter github-pr-filter.sh: issues opened/milestoned/labeled
     by the owner pass (pr_number is enriched); the bot, other people,
     mirrors ("Mirrored from kanban card") and other actions are dropped.
  B. The watchdog: a blocker (label bug or an owner mark OWNER_MARK_RE)
     without a live card -> admission:blk-no-executor within one cycle
     (10 min, not 24 h), class owner (a push to the owner); an ordinary
     issue stays silent until 24 h; wait-for-triage dominates label bug.
  C. The poll issue_executor_poll (5-minute cron): an open issue of the
     active milestone gets a coder card with the milestone_bands band,
     idempotency key owner-issue-N and a native_or_live receipt; a live
     card, an open PR, wait-for-triage, a bot mirror, a bot author and old
     issues outside the 48 h window are filtered out.
  D. The CLI of board_native_guard.py for the webhook prompt: rc 0 RECEIPT
     with a receipt, rc 3 COVERED, rc 2 on empty native-checked.

Mutants: each must turn the battery red. "Red before the fix" is a copy of
the source with every change of the mechanism reverted at once (a mutant
inside the test, not a walk over git history).

The board harness is imported from test_board_watchdog_board_lint.py.

Run: python3 test_board_watchdog_issue_executor_poll.py
"""
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.join(HERE, "test_board_watchdog_board_lint.py")
spec = importlib.util.spec_from_file_location("wd_lint_harness_poll", HARNESS)
if spec is None or spec.loader is None:
    print("SKIP: cannot load harness")
    sys.exit(0)
h = importlib.util.module_from_spec(spec)
sys.modules["wd_lint_harness_poll"] = h
spec.loader.exec_module(h)

# The harness fixed HERMES_HOME and the FACTORY_* settings; the owner and
# the bot login of this test are set here (read at call time).
os.environ["FACTORY_OWNER_LOGIN"] = "owner-login"
os.environ["FACTORY_BOT_LOGINS"] = "widget-bot"

NOW = time.time()
FAILS = []


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


OPEN_MILESTONES = json.dumps([
    {"title": "1.3.0", "due_on": "2026-11-22T00:00:00Z"},
    {"title": "1.4.0", "due_on": "2026-12-20T00:00:00Z"},
])

# --- B: admission gate fixtures -----------------------------------------
ISSUES_B = json.dumps([
    # blocker by label bug, 30 min -> blk-no-executor (not 24 h)
    {"number": 501, "milestone": {"title": "1.3.0"},
     "labels": [{"name": "bug"}], "updatedAt": _iso(NOW - 30 * 60)},
    # blocker by an owner mark in the body
    {"number": 502, "milestone": {"title": "1.3.0"},
     "labels": [{"name": "hermes"}],
     "body": "Owner: start now. This blocks the v1.3.0 tag",
     "updatedAt": _iso(NOW - 20 * 60)},
    # ordinary, 30 min -> silence (threshold 24 h)
    {"number": 503, "milestone": {"title": "1.3.0"},
     "labels": [{"name": "hermes"}], "updatedAt": _iso(NOW - 30 * 60)},
    # blocker, but a live card exists -> silence
    {"number": 504, "milestone": {"title": "1.3.0"},
     "labels": [{"name": "bug"}], "updatedAt": _iso(NOW - 30 * 60)},
    # blocker outside the active milestone -> silence
    {"number": 505, "milestone": {"title": "1.4.0"},
     "labels": [{"name": "bug"}], "updatedAt": _iso(NOW - 30 * 60)},
    # label bug under wait-for-triage -> silence (the owner decision wins)
    {"number": 506, "milestone": {"title": "1.3.0"},
     "labels": [{"name": "bug"}, {"name": "wait-for-triage"}],
     "updatedAt": _iso(NOW - 30 * 60)},
    # ordinary, 30 h -> no-executor
    {"number": 507, "milestone": {"title": "1.3.0"},
     "labels": [{"name": "hermes"}], "updatedAt": _iso(NOW - 30 * 3600)},
    # blocker, but work is in flight (PR Closes #508) -> silence
    {"number": 508, "milestone": {"title": "1.3.0"},
     "labels": [{"name": "bug"}], "updatedAt": _iso(NOW - 30 * 60)},
])
PRS_B = json.dumps([
    {"number": 91, "body": "Closes #508", "milestone": None,
     "latestReviews": []},
])

# --- C: poll fixtures -----------------------------------------------------
ISSUES_C = json.dumps([
    # the only candidate: create
    {"number": 601, "milestone": {"title": "1.3.0"},
     "labels": [{"name": "hermes"}], "body": "Crash on SSE connect",
     "updatedAt": _iso(NOW - 10 * 60)},
    # a bot mirror by its footer -> filtered (its source card is alive)
    {"number": 602, "milestone": {"title": "1.3.0"},
     "labels": [{"name": "bug"}],
     "body": "Mirrored from kanban card `t_604`",
     "updatedAt": _iso(NOW - 10 * 60)},
    # owner triage -> filtered
    {"number": 603, "milestone": {"title": "1.3.0"},
     "labels": [{"name": "wait-for-triage"}],
     "updatedAt": _iso(NOW - 10 * 60)},
    # a live card -> filtered
    {"number": 604, "milestone": {"title": "1.3.0"},
     "labels": [{"name": "hermes"}], "updatedAt": _iso(NOW - 10 * 60)},
    # next milestone -> filtered
    {"number": 605, "milestone": {"title": "1.4.0"},
     "labels": [{"name": "hermes"}], "updatedAt": _iso(NOW - 10 * 60)},
    # a bot author -> filtered
    {"number": 606, "milestone": {"title": "1.3.0"},
     "labels": [{"name": "hermes"}], "updatedAt": _iso(NOW - 10 * 60)},
    # old, outside the 48 h window -> left to the no-executor finding
    {"number": 607, "milestone": {"title": "1.3.0"},
     "labels": [{"name": "hermes"}], "updatedAt": _iso(NOW - 3 * 86400)},
    # an open PR -> filtered
    {"number": 608, "milestone": {"title": "1.3.0"},
     "labels": [{"name": "hermes"}], "updatedAt": _iso(NOW - 10 * 60)},
])
PRS_C = json.dumps([
    {"number": 92, "body": "Closes #608", "milestone": None,
     "latestReviews": []},
])
VIEWS_C = {
    "601": {"number": 601, "title": "Crash on SSE connect",
            "body": "Crash on SSE connect", "author": {"login": "owner-login"}},
    "606": {"number": 606, "title": "Mirrored card work",
            "body": "x", "author": {"login": "widget-bot"}},
}

BW_PATH = os.environ.get("BOARD_WATCHDOG_SCRIPT") or os.path.join(
    HERE, "board-watchdog.py")
SH_PATH = os.path.join(HERE, "github-pr-filter.sh")
GUARD_PATH = os.path.join(HERE, "board_native_guard.py")
_SANDBOXES = []


def load_bw(src=None, tag="poll"):
    """The board-watchdog module from the real file or from a mutation src.

    Mutant copies live outside scripts/: the siblings factory_config.py,
    milestone_bands.py and board_native_guard.py are copied next to them,
    otherwise the loader of board-watchdog stubs them with "no data" and
    the mutants turn red because of the sandbox, not their own check.
    """
    sbx = tempfile.mkdtemp(prefix="bw-sbx-poll-")
    _SANDBOXES.append(sbx)
    path = BW_PATH if src is None else os.path.join(sbx, "wd-mut.py")
    if src is not None:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(src)
        for nb in ("factory_config.py", "milestone_bands.py",
                   "board_native_guard.py"):
            shutil.copy(os.path.join(HERE, nb), os.path.join(sbx, nb))
    sp = importlib.util.spec_from_file_location("bw_mod_%s" % tag, path)
    m = importlib.util.module_from_spec(sp)
    sys.modules["bw_mod_%s" % tag] = m
    sp.loader.exec_module(m)
    m.STATE = os.path.join(sbx, "state.json")
    m.BACKLOG_CACHE = os.path.join(sbx, "backlog.json")
    if hasattr(m, "milestone_bands"):
        m.milestone_bands.CACHE_PATH = os.path.join(sbx, "ms.json")
        m.milestone_bands._MEMO = {}
    return m, sbx


def battery(mod, sh_path):
    """Problems of the A-D battery on module mod and filter sh_path."""
    problems = []

    def want(name, cond, detail=""):
        if not cond:
            problems.append("%s :: %s" % (name, detail))

    # --- B: admission gate ---------------------------------------------
    slug, db, old_boards = h.make_board(tasks=[
        {"id": "t_504", "status": "ready", "priority": 80,
         "title": "[coder] issue #504", "body": "work on #504",
         "assignee": "coder", "created_by": "user"},
    ])
    mod.BOARDS = os.path.dirname(os.path.dirname(db))

    def run_b(cmd, timeout=120):
        if cmd[:2] == ["gh", "api"]:
            return OPEN_MILESTONES
        if cmd[:2] == ["gh", "issue"] and "list" in cmd:
            return ISSUES_B
        if cmd[:2] == ["gh", "pr"] and "list" in cmd:
            return PRS_B
        return "ok"

    real_run = mod.run
    mod.run = run_b
    mod.board_repo = lambda s: "acme/demo"
    try:
        findings = mod.admission_gate(slug)
    except Exception as exc:
        findings = []
        problems.append("B: admission_gate crashed: %s: %s"
                        % (type(exc).__name__, exc))
    finally:
        mod.run = real_run
    keys = {k for k, _, _ in findings}
    want("B1 #501 (bug, 30 min) -> blk-no-executor",
         any(":admission:blk-no-executor:501" in k for k in keys)
         and not any(":admission:no-executor:501" in k for k in keys),
         sorted(keys))
    want("B2 #502 (Owner: start now, 20 min) -> blk-no-executor",
         any(":admission:blk-no-executor:502" in k for k in keys),
         sorted(keys))
    want("B3 #503/#504/#505/#506/#508 are silent",
         not any((":no-executor:503" in k or ":no-executor:504" in k
                  or ":no-executor:505" in k or ":no-executor:506" in k
                  or ":no-executor:508" in k) for k in keys), sorted(keys))
    want("B4 #507 (30 h, no mark) -> no-executor",
         any(":admission:no-executor:507" in k for k in keys), sorted(keys))
    try:
        want("B5 blk-no-executor is classified as owner",
             mod.classify_finding("testboard:admission:blk-no-executor:501")
             == "owner")
        want("B6 no-executor stays digest",
             mod.classify_finding("testboard:admission:no-executor:507")
             == "digest")
    except Exception as exc:
        problems.append("B5/B6 classify_finding: %s: %s"
                        % (type(exc).__name__, exc))

    # --- C: 5-minute poll -------------------------------------------------
    slug2, db2, _ = h.make_board(tasks=[
        {"id": "t_604", "status": "ready", "priority": 80,
         "title": "[coder] issue #604", "body": "work on #604",
         "assignee": "coder", "created_by": "user"},
    ])
    mod.BOARDS = os.path.dirname(os.path.dirname(db2))
    creates = []

    def run_c(cmd, timeout=120):
        if cmd[:2] == ["gh", "api"]:
            return OPEN_MILESTONES
        if cmd[:2] == ["gh", "issue"] and "list" in cmd:
            return ISSUES_C
        if cmd[:2] == ["gh", "pr"] and "list" in cmd:
            return PRS_C
        if cmd[:2] == ["gh", "issue"] and "view" in cmd:
            return json.dumps(VIEWS_C.get(cmd[3], {}))
        if len(cmd) > 4 and cmd[1] == "kanban" and cmd[4] == "create":
            creates.append(cmd)
            body_file = cmd[cmd.index("--body-file") + 1]
            body = open(body_file, encoding="utf-8").read()
            title = cmd[5]
            con = sqlite3.connect(db2, timeout=10)
            con.execute(
                "insert into tasks (id, title, body, status, priority,"
                " assignee, created_by, created_at, idempotency_key)"
                " values ('t_new601',?,?,'ready',?,?,'watchdog',?,?)",
                (title, body,
                 int(cmd[cmd.index("--priority") + 1]),
                 cmd[cmd.index("--assignee") + 1], int(time.time()),
                 cmd[cmd.index("--idempotency-key") + 1]))
            con.commit()
            con.close()
            return json.dumps({"id": "t_new601"})
        return "ok"

    mod.run = run_c
    try:
        plan = mod.issue_executor_poll()
    except AttributeError as exc:
        plan = None
        problems.append("C: issue_executor_poll is missing: %s" % exc)
    except Exception as exc:
        plan = None
        problems.append("C: the poll crashed: %s: %s"
                        % (type(exc).__name__, exc))
    finally:
        mod.run = real_run
    if plan is not None:
        want("C1 exactly one card created (#601)",
             plan == [(slug2, 601, "t_new601")], str(plan))
        want("C2 one kanban create", len(creates) == 1,
             "%d calls" % len(creates))
        if creates:
            con = sqlite3.connect(db2)
            row = con.execute(
                "select title, body, priority, assignee,"
                " idempotency_key from tasks where id='t_new601'"
            ).fetchone()
            con.close()
            want("C3 band of the active milestone p80, assignee coder",
                 row and row[2] == 80 and row[3] == "coder", str(row))
            want("C4 idempotency owner-issue-601",
                 row and row[4] == "owner-issue-601", str(row))
            want("C5 the body carries Issue: #601 and a native guard receipt",
                 row and "Issue: #601" in row[1]
                 and "Native guard" in row[1],
                 (row[1][:120] if row else "no row"))

    # --- A: webhook filter --------------------------------------------------
    owner = {"login": "owner-login", "type": "User"}
    bot = {"login": "widget-bot", "type": "Bot"}
    repo = {"full_name": "acme/widget"}
    cases = [
        ("A1 issues.opened by the owner passes",
         {"action": "opened", "sender": owner,
          "issue": {"number": 383, "body": "start now"},
          "repository": repo}, 0, "383"),
        ("A2 issues.opened by the bot (mirror) is dropped",
         {"action": "opened", "sender": bot,
          "issue": {"number": 380, "body": "Mirrored from kanban card t_x"},
          "repository": repo}, 1, None),
        ("A3 issues.opened by another person is dropped",
         {"action": "opened", "sender": {"login": "someone"},
          "issue": {"number": 1, "body": "hi"},
          "repository": repo}, 1, None),
        ("A4 issues.milestoned by the owner passes",
         {"action": "milestoned", "sender": owner,
          "issue": {"number": 383, "body": "x"},
          "repository": repo}, 0, "383"),
        ("A5 issues.closed is dropped",
         {"action": "closed", "sender": owner,
          "issue": {"number": 383, "body": "x"},
          "repository": repo}, 1, None),
        ("A6 issue_comment by the owner passes (regression)",
         {"action": "created", "sender": owner,
          "issue": {"number": 383, "body": "x"}, "comment": {"body": "go"},
          "repository": repo}, 0, "383"),
        ("A7 issue_comment by the bot is dropped (regression)",
         {"action": "created", "sender": bot,
          "issue": {"number": 380, "body": "x"}, "comment": {"body": "go"},
          "repository": repo}, 1, None),
        ("A8 review by the owner passes (regression)",
         {"action": "submitted", "sender": owner,
          "pull_request": {"number": 381},
          "repository": repo}, 0, "381"),
        ("A9 the owner labels a bot mirror: the footer drops it",
         {"action": "labeled", "sender": owner,
          "issue": {"number": 380, "body": "Mirrored from kanban card t_x"},
          "repository": repo}, 1, None),
    ]
    for name, payload, rc_exp, num in cases:
        p = subprocess.run(["bash", sh_path], input=json.dumps(payload),
                           capture_output=True, text=True, timeout=60)
        ok = p.returncode == rc_exp
        if ok and rc_exp == 0:
            out = json.loads(p.stdout or "{}")
            ok = str(out.get("pr_number")) == num
        want(name, ok, "rc=%d out=%s" % (p.returncode,
                                         (p.stdout or "")[:80]))

    return problems


def guard_cli_battery():
    problems = []
    p = subprocess.run(
        [sys.executable, GUARD_PATH, "--key", "issue-5",
         "--native-checked", "task_links", "--why", "new issue"],
        capture_output=True, text=True, timeout=60)
    if not (p.returncode == 0 and "RECEIPT" in p.stdout
            and "Native guard" in p.stdout):
        problems.append("D1 rc0 RECEIPT: rc=%d %s"
                        % (p.returncode, p.stdout[:80]))
    p = subprocess.run(
        [sys.executable, GUARD_PATH, "--key", "issue-5",
         "--live-keys", "issue-5", "--native-checked", "task_links",
         "--why", "x"],
        capture_output=True, text=True, timeout=60)
    if not (p.returncode == 3 and "COVERED" in p.stdout):
        problems.append("D2 rc3 COVERED: rc=%d %s"
                        % (p.returncode, p.stdout[:80]))
    p = subprocess.run(
        [sys.executable, GUARD_PATH, "--key", "issue-5", "--why", "x"],
        capture_output=True, text=True, timeout=60)
    if p.returncode != 2:
        problems.append("D3 rc2 on empty native-checked: rc=%d"
                        % p.returncode)
    return problems


BW_MUTANTS = {
    "blocker-threshold-back-to-24h": (
        "NO_EXECUTOR_BLOCKER_MIN_AGE_S = 10 * 60",
        "NO_EXECUTOR_BLOCKER_MIN_AGE_S = 24 * 3600"),
    "blocker-without-label-bug": (
        'blocker = (not triage and ("bug" in labels',
        'blocker = (not triage and ("no-such-label" in labels'),
    "blocker-without-owner-mark": (
        'or bool(OWNER_MARK_RE.search(body or ""))))',
        "or False))"),
    "blocker-goes-to-digest": (
        'if ":admission:blk-no-executor:" in key:',
        'if ":admission:blk-no-executor-mutant:" in key:'),
    "poll-without-48h-window": (
        "ISSUE_POLL_WINDOW_S = 48 * 3600",
        "ISSUE_POLL_WINDOW_S = 48 * 36000"),
    "poll-without-mirror-filter": (
        # a mirror suppresses the issue only while its source card is alive
        "src = mirror_source(body or \"\")\n"
        "            if src and src in live_ids:",
        "if False:\n                continue"),
    "poll-without-bot-author-filter": (
        'author = ((meta.get("author") or {}).get("login") or "")',
        'author = ""'),
}

SH_MUTANTS = {
    "filter-without-mirror-drop": (
        '  jq -r \'.issue.body // ""\' <<<"$payload" \\\n'
        '    | grep -qF "$footer" && exit 1\n',
        ""),
    "filter-accepts-any-action": (
        '|| [ "$action" = "labeled" ] || exit 1',
        '|| [ "$action" = "labeled" ] || true'),
}


def main():
    bw_src = open(BW_PATH, encoding="utf-8").read()
    sh_src = open(SH_PATH, encoding="utf-8").read()
    for name, (old, _new) in {**BW_MUTANTS, **SH_MUTANTS}.items():
        if (bw_src + sh_src).count(old) != 1:
            print("FAIL: mutant anchor %s does not occur exactly once" % name)
            return 1

    # clean run
    mod, _ = load_bw()
    problems = battery(mod, SH_PATH) + guard_cli_battery()
    for p in problems:
        print("FAIL %s" % p)
    if problems:
        print("\nRED: clean run, problems %d" % len(problems))
        return 1
    print("OK: clean run (A filter, B watchdog blocker, C poll, D CLI)")

    failed = 0
    for name, (old, new) in BW_MUTANTS.items():
        mut = load_bw(bw_src.replace(old, new, 1), tag=name)[0]
        try:
            probs = battery(mut, SH_PATH)
        except Exception as exc:
            probs = ["crashed: %s: %s" % (type(exc).__name__, exc)]
        if probs:
            print("OK   %s: mutant caught (%s)"
                  % (name, " | ".join(p[:70] for p in probs[:3])))
        else:
            print("FAIL %s: mutant survived" % name)
            failed += 1
    for name, (old, new) in SH_MUTANTS.items():
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(sh_src.replace(old, new, 1))
            mut_sh = fh.name
        mod2, _ = load_bw(tag=name)
        try:
            probs = battery(mod2, mut_sh)
        except Exception as exc:
            probs = ["crashed: %s: %s" % (type(exc).__name__, exc)]
        if probs:
            print("OK   %s: mutant caught (%s)"
                  % (name, " | ".join(p[:70] for p in probs[:3])))
        else:
            print("FAIL %s: mutant survived" % name)
            failed += 1
        os.unlink(mut_sh)

    # Red before the fix: a copy of the source with every change of the
    # mechanism reverted at once (all BW + SH pairs).
    refix_bw = bw_src
    for _n, (old, new) in BW_MUTANTS.items():
        refix_bw = refix_bw.replace(old, new, 1)
    refix_sh = sh_src
    for _n, (old, new) in SH_MUTANTS.items():
        refix_sh = refix_sh.replace(old, new, 1)
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False,
                                     encoding="utf-8") as fh:
        fh.write(refix_sh)
        mut_sh = fh.name
    mut = load_bw(refix_bw, tag="refix")[0]
    try:
        probs = battery(mut, mut_sh)
    except Exception as exc:
        probs = ["crashed: %s: %s" % (type(exc).__name__, exc)]
    finally:
        os.unlink(mut_sh)
    if probs:
        print("OK   fix reverted (code before the fix, mutant) is red: %s"
              % probs[0][:80])
    else:
        print("FAIL fix reverted is green: the test is blind before the fix")
        failed += 1

    if failed:
        print("\nTOTAL: FAIL, problems %d" % failed)
        return 1
    print("\nTOTAL: OK")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        for _d in _SANDBOXES:
            shutil.rmtree(_d, ignore_errors=True)
