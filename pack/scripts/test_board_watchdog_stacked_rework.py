#!/usr/bin/env python3
"""Stacked rework gate: a rework PR stacked on a live PR without an owner
decision.

Fixture: a real git repo in a temp dir (real commits and a real
merge-base --is-ancestor, no ancestry mocks), a board with a board.json that
points to this repo, and a recorder over run(): gh answers with canned data
(a PR list with real OIDs from the repo, PR comments), git remote get-url
answers with a fake github URL, git fetch is silently swallowed.

PR grid (bot = app/test-bot, owner = test-owner):
  #11 head B sits on the head of A (#10), same base develop/2.x, author is
     the bot, no marker -> the ONLY finding;
  #12 also sits on A, the body has a stack-owner-approve line -> silent;
  #14 also sits on A, author is the owner -> silent;
  #13 a separate branch from the base, not an ancestor -> silent;
  #15 sits on the head of #13 but the base is develop/1.x (another line)
     -> silent.

Mutants (each must turn the test red):
  M_ANCESTOR ancestry is always False -> #11 is not found;
  M_OWNER    the owner-author filter is off -> #14 is raised;
  M_MARKER   the stack-owner-approve line is no longer matched -> #12 is
             raised;
  M_IDEMPOT  the marker comment no longer silences a repeat -> the second
             pass alerts again;
  M_BASE     the same-base-line filter is off -> #15 is raised.
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sqlite3
import sys
import tempfile

# Project settings are read at import time: set neutral values first.
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="bw-test-home-")
os.environ.update({
    "FACTORY_REPO": "acme/widget",
    "FACTORY_REPO_PATH": "/srv/hermes/widget",
    "FACTORY_BOARD": "main",
    "FACTORY_OWNER_LOGIN": "owner-login",
    "FACTORY_OWNER_CHAT": "telegram:000000000",
    "FACTORY_BASE_BRANCHES": "develop/1.x,develop/2.x",
})

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
# An external copy (a manual mutant) can be checked directly:
#   BW_WATCHDOG_PATH=/tmp/mutant.py python3 test_board_watchdog_stacked_rework.py
BW_PATH = os.environ.get("BW_WATCHDOG_PATH") or os.path.join(
    SCRIPTS, "board-watchdog.py")
SLUG = "stackfix"
MARKER = "watchdog: stacked-rework"
CYRILLIC_RE = re.compile("[" + chr(0x400) + "-" + chr(0x4FF) + "]")


def _git(repo, *args):
    subprocess.run(["git", "-C", repo] + list(args), check=True,
                   capture_output=True, text=True)


def _head(repo):
    out = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"],
                         check=True, capture_output=True, text=True)
    return out.stdout.strip()


def make_repo(root):
    """base -> A -> B; A -> C; base -> D -> E; A -> F. Returns the OIDs."""
    repo = os.path.join(root, "repo")
    os.makedirs(repo)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")

    def commit(name):
        open(os.path.join(repo, "f.txt"), "w").write(name + "\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", name)
        return _head(repo)
    base = commit("base")
    oid_a = commit("a")            # head of #10
    oid_b = commit("b")            # head of #11: stack on #10
    _git(repo, "reset", "-q", "--hard", oid_a)
    oid_c = commit("c")            # head of #12: stack on #10 with a marker
    oid_f = commit("f")            # head of #14: stack on #10 by the owner
    _git(repo, "reset", "-q", "--hard", base)
    oid_d = commit("d")            # head of #13: its own branch from the base
    oid_e = commit("e")            # head of #15: stack on #13, base 1.x
    return repo, dict(a=oid_a, b=oid_b, c=oid_c, d=oid_d, e=oid_e, f=oid_f)


def make_board(root, workdir):
    bdir = os.path.join(root, "boards", SLUG)
    os.makedirs(bdir)
    con = sqlite3.connect(os.path.join(bdir, "kanban.db"))
    con.execute("""CREATE TABLE tasks (
        id TEXT PRIMARY KEY, title TEXT, body TEXT, assignee TEXT,
        status TEXT NOT NULL, priority INTEGER DEFAULT 0,
        created_by TEXT, created_at INTEGER NOT NULL,
        started_at INTEGER, completed_at INTEGER)""")
    con.execute("""CREATE TABLE task_comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
        author TEXT NOT NULL, body TEXT NOT NULL,
        created_at INTEGER NOT NULL)""")
    con.commit()
    con.close()
    json.dump({"default_workdir": workdir},
              open(os.path.join(bdir, "board.json"), "w"))
    return bdir


def prs_json(oids):
    bot = {"login": "app/test-bot"}
    owner = {"login": "test-owner"}
    return [
        {"number": 10, "title": "feat: base feature", "headRefOid": oids["a"],
         "baseRefName": "develop/2.x", "author": bot, "body": ""},
        {"number": 11, "title": "fix: stacked rework without owner decision",
         "headRefOid": oids["b"], "baseRefName": "develop/2.x",
         "author": bot, "body": "Rework of the verdict, opened as a stack."},
        {"number": 12, "title": "fix: approved stack", "headRefOid": oids["c"],
         "baseRefName": "develop/2.x", "author": bot,
         "body": "Owner allowed this layer: stack-owner-approve"},
        {"number": 13, "title": "feat: separate line", "headRefOid": oids["d"],
         "baseRefName": "develop/2.x", "author": bot, "body": ""},
        {"number": 14, "title": "feat: owner opened the stack himself",
         "headRefOid": oids["f"], "baseRefName": "develop/2.x",
         "author": owner, "body": ""},
        {"number": 15, "title": "feat: other release line",
         "headRefOid": oids["e"], "baseRefName": "develop/1.x",
         "author": bot, "body": ""},
    ]


class Recorder(object):
    """run() of the gate: gh from canned data, a fake git remote get-url,
    a silent git fetch. PR comments are remembered and start to be answered
    in view (as a live gh does after a comment is published)."""

    def __init__(self, prs):
        self.prs = prs
        self.calls = []
        self.commented = set()

    def __call__(self, cmd, timeout=120):
        self.calls.append(cmd)
        if cmd[0] == "git":
            if "get-url" in cmd:
                return "https://github.com/test-owner/repo.git"
            if "fetch" in cmd:
                return ""
            return ""
        if cmd[0] == "gh" and len(cmd) > 3:
            if "list" in cmd:
                return json.dumps(self.prs)
            if "view" in cmd:
                if cmd[3] in self.commented:
                    # The shape of a live gh: an object {"comments": [...]},
                    # not a list (a list fixture once hid a shape mismatch
                    # and the markers were duplicated).
                    return json.dumps({"comments": [
                        {"body": MARKER + " seen"}]})
                return json.dumps({"comments": []})
            if "comment" in cmd:
                self.commented.add(cmd[3])
                return "ok"
        return "ok"


def load_bw(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def prepare(root, src_path=BW_PATH, name="bw_stacked", prs=None):
    repo, oids = make_repo(root)
    bdir = make_board(root, repo)
    mod = load_bw(src_path, name)
    mod.BOARDS = bdir.rsplit(os.sep, 1)[0]
    rec = Recorder(prs if prs is not None else prs_json(oids))
    mod.run = rec
    return mod, rec, oids


def comment_calls(rec):
    return [c for c in rec.calls if c[0] == "gh" and "comment" in c]


def finding_prs(findings):
    return sorted(int(f[0].rsplit(":", 1)[1]) for f in findings)


def check_positive(mod, rec):
    """dry-run: exactly one finding (#11), #10 is mentioned, no marker."""
    problems = []
    findings = mod.stacked_rework_gate(SLUG, apply=False)
    if finding_prs(findings) != [11]:
        problems.append("dry-run findings %s, expected [11]"
                        % finding_prs(findings))
    if findings and ("#10" not in findings[0][2] or "#11" not in findings[0][2]):
        problems.append("the finding text has no #10/#11 pair")
    if comment_calls(rec):
        problems.append("dry-run placed a marker: %s" % comment_calls(rec))
    if findings and findings[0][2].find("dry-run") < 0:
        problems.append("the dry-run finding has no dry-run note")
    return problems, findings


def run_apply(mod, rec):
    """A pass with application: the marker is placed, findings returned."""
    rec.calls = []
    findings = mod.stacked_rework_gate(SLUG)
    comments = comment_calls(rec)
    return findings, comments


def full_run(mod, rec):
    """dry-run + apply + a repeat pass. Returns all problems."""
    problems, _ = check_positive(mod, rec)
    findings, comments = run_apply(mod, rec)
    if finding_prs(findings) != [11]:
        problems.append("apply: findings %s, expected [11]"
                        % finding_prs(findings))
    if len(comments) != 1 or comments[0][3] != "11":
        problems.append("the marker is not placed on #11: %s"
                        % [c[3] for c in comments])
    rec.calls = []
    findings2, comments2 = run_apply(mod, rec)
    if findings2 or comments2:
        problems.append("the repeat pass is not silent: %s / %s"
                        % (finding_prs(findings2),
                           [c[3] for c in comments2]))
    return problems, comments


def main():
    failures = []
    root = tempfile.mkdtemp(prefix="stacked-")
    try:
        mod, rec, oids = prepare(os.path.join(root, "main"))

        # 1-3. a full run of the production code: finding, marker, idempotency
        problems, comments = full_run(mod, rec)
        if problems:
            failures.append("main run: %s" % problems)

        # 3b. marker content: English text, the lower PR is named
        if not comments:
            failures.append("the marker is not placed at all")
        else:
            body = comments[0][comments[0].index("--body") + 1]
            if not body.startswith(MARKER):
                failures.append("the marker does not start with the marker")
            if "#10" not in body:
                failures.append("the marker does not name the lower PR #10")
            if CYRILLIC_RE.search(body):
                failures.append("the GitHub comment has non-English text")

        # 4. mutants
        mutants = [
            ("M_ANCESTOR",
             "        return done.returncode == 0",
             "        return False",
             "#11 is not found"),
            ("M_OWNER",
             "        if (x.get(\"author\") or {}).get(\"login\") == owner:",
             "        if (x.get(\"author\") or {}).get(\"login\") == owner"
             " and False:",
             "#14 (owner) is raised"),
            ("M_MARKER",
             "        if STACK_OWNER_APPROVE_RE.search(x.get(\"body\") or \"\"):",
             "        if False and STACK_OWNER_APPROVE_RE.search("
             "x.get(\"body\") or \"\"):",
             "#12 (with the owner marker) is raised"),
            ("M_IDEMPOT",
             "        if already:\n            continue",
             "        if already and False:\n            continue",
             "the repeat pass alerts again"),
            ("M_BASE",
             "            if y.get(\"baseRefName\") != x.get(\"baseRefName\"):",
             "            if False:",
             "#15 (another base line) is raised"),
        ]
        for mname, old, new, expect in mutants:
            src = open(BW_PATH).read()
            if src.count(old) != 1:
                failures.append("%s: the anchor occurs %d times"
                                % (mname, src.count(old)))
                continue
            mutated = src.replace(old, new)
            if mutated == src:
                failures.append("%s: the mutation changed nothing" % mname)
                continue
            mroot = os.path.join(root, mname.lower())
            os.makedirs(mroot, exist_ok=True)
            mpath = os.path.join(mroot, "board-watchdog.py")
            open(mpath, "w").write(mutated)
            # the mutant copy loads its settings module as a sibling
            shutil.copy(os.path.join(SCRIPTS, "factory_config.py"), mroot)
            mmod, mrec, _ = prepare(mroot, src_path=mpath,
                                    name="bw_stacked_" + mname.lower())
            mprobs, _ = full_run(mmod, mrec)
            if not mprobs:
                failures.append("%s NOT killed: %s" % (mname, expect))
            else:
                print("  mutant %s killed: %s" % (mname, mprobs[0]))
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print("=" * 64)
    if failures:
        for f in failures:
            print("FAIL %s" % f)
        sys.exit(1)
    print("PASS stacked rework: the only finding is #11 (a stack on #10 "
          "without an owner decision), the marker and an owner author "
          "silence it, another base line is silent, the marker comment is "
          "English, idempotent, 5 mutants killed.")


if __name__ == "__main__":
    main()
