#!/usr/bin/env python3
"""pr-hygiene: the owner card of a PR is found by a structural link, not names.

Origin: a live owner card (no branch_name, no PR number in its title, a key
without the PR number) requested review with handoff metadata
{"pr": <full PR URL>, "pr_number": N, "head_sha": ...}. Its PR branch carried
no issue number. Every name-based rank of live_cards_by_pr stayed silent, so
the rework arm found no owner and created a duplicate card in the middle of
the owner's review.

Rules pinned here:
  A. task_runs.metadata of the handoff run (pr URL, pr int, published_pr,
     pr_number) and a full PR URL in tasks.completion_contract give rank -1,
     stronger than every name-based carrier.
  B. Prose in other metadata keys is not a carrier; broken JSON is skipped.
  C. Without structure the name-based ranks still work (fallback), and with
     neither there is no owner (old behaviour).
  D. Old board schemas (no completion_contract column, no task_runs table)
     do not crash: the structural carrier is simply not counted.

Red before green: in-test mutants of pr-hygiene.py (copies in a temp dir,
never a re-run of this file) must turn the battery red.

Run: python3 test_pr_hygiene_structural_owner.py
"""
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.update({
    "HERMES_HOME": tempfile.mkdtemp(prefix="ph-struct-owner-"),
    "FACTORY_REPO": "acme/widget",
    "FACTORY_REPO_PATH": "/srv/hermes/widget",
    "FACTORY_BOARD": "main",
    "FACTORY_OWNER_LOGIN": "owner-login",
    "FACTORY_OWNER_CHAT": "telegram:000000000",
    "FACTORY_BASE_BRANCHES": "main",
    "FACTORY_ISSUE_BRANCH_PREFIX": "hermes/",
})
SCRIPT = os.environ.get("PR_HYGIENE_SCRIPT") or os.path.join(
    HERE, "pr-hygiene.py")

PR_URL = "https://github.com/acme/widget/pull/416"
# PR branch without an issue number: rank 3 cannot fire.
HEAD = {416: "hermes/version-badge-legacy-revision"}
OWNER = ("t_owner", "[coder] issue #79: check open acceptance criteria",
         "owner-issue-79-r2", None, "coder", "running")


def load(path):
    spec = importlib.util.spec_from_file_location("pr_hygiene_so", path)
    if spec is None or spec.loader is None:
        return None
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def make_db(cards, runs=(), contracts=None, old_schema=False):
    """A temp board. old_schema: no completion_contract, no task_runs."""
    db = os.path.join(tempfile.mkdtemp(prefix="ph-struct-db-"), "kanban.db")
    con = sqlite3.connect(db)
    cols = ("id text, title text, idempotency_key text, branch_name text,"
            " assignee text, status text")
    if not old_schema:
        cols += ", completion_contract text"
    con.execute("create table tasks (%s)" % cols)
    for c in cards:
        if old_schema:
            con.execute("insert into tasks values (?,?,?,?,?,?)", c)
        else:
            con.execute("insert into tasks values (?,?,?,?,?,?,?)",
                        tuple(c) + ((contracts or {}).get(c[0]),))
    if not old_schema:
        con.execute("create table task_runs (id integer, task_id text,"
                    " metadata text)")
        con.executemany("insert into task_runs values (?,?,?)", runs)
    con.commit()
    con.close()
    return db


def by_pr(mod, db, head=HEAD):
    old = mod.BOARD_DB
    mod.BOARD_DB = db
    try:
        return mod.live_cards_by_pr(head)
    finally:
        mod.BOARD_DB = old


def check_exact_failure_shape(mod):
    """A: the live owner with only handoff metadata is found with rank -1."""
    meta = json.dumps({"pr": PR_URL, "pr_number": 416,
                       "head_sha": "c11d7260" + "0" * 32})
    got = by_pr(mod, make_db([OWNER], runs=[(1, "t_owner", meta)]))
    assert 416 in got, "owner not found: %r" % got
    tid, _title, rank, _key, _as = got[416][0]
    assert tid == "t_owner" and rank == -1, (tid, rank)


def check_metadata_forms(mod):
    """A/B: int pr, published_pr URL, pr_number; prose keys do not count."""
    head = {416: HEAD[416], 412: "feature/other", 7: "feature/seven"}
    db = make_db([
        ("t_a", "[coder] follow-up", "owner-x", None, "coder", "review"),
        ("t_b", "[coder] follow-up", "owner-y", None, "coder", "review"),
        ("t_c", "[coder] follow-up", "owner-z", None, "coder", "review"),
        ("t_d", "[coder] follow-up", "owner-w", None, "coder", "review"),
        ("t_e", "[coder] follow-up", "owner-v", None, "coder", "review"),
    ], runs=[
        (1, "t_a", json.dumps({"pr": 412, "head_sha": "bc14d23"})),
        (2, "t_b", json.dumps({"base": "main", "ci": "6/6",
                               "published_pr": PR_URL})),
        (3, "t_c", json.dumps({"pr_number": 7})),
        (4, "t_d", json.dumps({"note": PR_URL, "ci": "see pull/416"})),
        (5, "t_e", "{not json"),
    ])
    got = by_pr(mod, db, head)
    owners = {n: sorted(c[0] for c in cards) for n, cards in got.items()}
    assert owners == {412: ["t_a"], 416: ["t_b"], 7: ["t_c"]}, owners


def check_contract_outranks_names(mod):
    """A: the completion contract URL beats a title naming another PR."""
    head = {416: HEAD[416], 500: "feature/five-hundred"}
    db = make_db([("t_re", "[coder] Fix PR #500 follow-up", "",
                   None, "coder", "ready")], contracts={"t_re": PR_URL})
    got = by_pr(mod, db, head)
    assert [(c[0], c[2]) for c in got.get(416, [])] == [("t_re", -1)], got
    assert 500 not in got, got


def check_names_fallback(mod):
    """C: no structure: the key still links (rank 1); neither: no owner."""
    db = make_db([("t_name", "[coder] rebase onto main",
                   "coder-pr416-rebase-c11d726", None, "coder", "ready")])
    got = by_pr(mod, db)
    assert [(c[0], c[2]) for c in got.get(416, [])] == [("t_name", 1)], got
    assert by_pr(mod, make_db([OWNER])) == {}


def check_old_schema(mod):
    """D: an old board without the column and the table does not crash."""
    db = make_db([("t_name", "[coder] rebase onto main",
                   "coder-pr416-rebase-c11d726", None, "coder", "ready")],
                 old_schema=True)
    got = by_pr(mod, db)
    assert [(c[0], c[2]) for c in got.get(416, [])] == [("t_name", 1)], got


CHECKS = [check_exact_failure_shape, check_metadata_forms,
          check_contract_outranks_names, check_names_fallback,
          check_old_schema]


def battery(mod):
    """Names of failed checks (an exception counts as a failure)."""
    failed = []
    for check in CHECKS:
        try:
            check(mod)
        except Exception as exc:  # noqa: BLE001
            failed.append("%s: %s" % (check.__name__, exc))
    return failed


# (name, old, new, checks that must go red)
MUTANTS = [
    ("structural rank removed",
     "                cand.append((num, -1))", "                pass",
     {"check_exact_failure_shape", "check_metadata_forms",
      "check_contract_outranks_names"}),
    ("structural rank weaker than names",
     "                cand.append((num, -1))",
     "                cand.append((num, 4))",
     {"check_exact_failure_shape", "check_contract_outranks_names"}),
    ("old schema not tolerated",
     "    except sqlite3.OperationalError:\n        pass\n    return out",
     "    except ValueError:\n        pass\n    return out",
     {"check_old_schema"}),
    ("prose keys counted",
     'for field in ("pr", "published_pr"):',
     'for field in ("pr", "published_pr", "note", "ci"):',
     {"check_metadata_forms"}),
]


def run_mutants(src):
    tmp = tempfile.mkdtemp(prefix="ph-struct-mut-")
    for name, old, new, must_fail in MUTANTS:
        assert src.count(old) == 1, "mutant anchor drifted: %s" % name
        path = os.path.join(tmp, "pr_hygiene_mut_%d.py" % MUTANTS.index(
            (name, old, new, must_fail)))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(src.replace(old, new))
        mutant = load(path)
        failed = {f.split(":")[0] for f in battery(mutant)}
        missing = must_fail - failed
        assert not missing, "mutant %r stayed green on %s" % (name, missing)
        print("OK mutant red: %s (%s)" % (name, ", ".join(sorted(failed))))


def main():
    mod = load(SCRIPT)
    if mod is None:
        print("SKIP: cannot load pr-hygiene.py")
        return 0
    failed = battery(mod)
    for f in failed:
        print("FAIL", f)
    if failed:
        return 1
    print("OK %d checks green on pr-hygiene.py" % len(CHECKS))
    with open(SCRIPT, encoding="utf-8") as fh:
        run_mutants(fh.read())
    print("PASS structural PR owner")
    return 0


if __name__ == "__main__":
    sys.exit(main())
