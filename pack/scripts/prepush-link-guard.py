#!/usr/bin/env python3
"""GitHub link gate for git push.

The gh-link-guard.py gate covers texts that go out through the gh wrapper:
comments, bodies, titles. But markdown and code comments travel in commits
through git push, past that wrapper: a branch once carried two dozen links to
a nonexistent repository (a typo in the owner login) in a docs file, and a
reviewer caught them by hand rather than a mechanism.

The pre-push hook (shared by checkouts and worktrees of the project through
core.hooksPath) calls this script. The rule is the same as in
gh-link-guard.py: a github.com link in the content being pushed must exist
(owner/repo answers 200, the path exists at the ref, the commit sha exists).

What is checked: only the lines ADDED by the commits the push carries:
  - .md/.markdown: all added lines;
  - .java/.ts/.tsx/.js/.jsx/.kt/.kts: only comments and javadoc
    (lines starting with //, /*, * and the tail after //).
A failed check (network, 401, 403) = refusal: fail closed. Outside Hermes
sessions the gate is off: a push by the owner's own hands bypasses the check.
A conscious bypass: SKIP_LINK_GUARD=1 git push ...

Manual audit of a branch without pushing: prepush-link-guard.py --scan-ref <ref>.
The base for that audit is looked up among the long-lived branches listed in
FACTORY_BASE_BRANCHES (see factory_config.py).
Tests: python3 test_prepush_link_guard.py
"""
import importlib.util
import os
import re
import subprocess
import sys

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)
from factory_config import cfg_list  # noqa: E402

_GUARD_CANDIDATES = [
    os.path.join(SCRIPTS, "gh-link-guard.py"),
]
_GUARD_PATH = next(p for p in _GUARD_CANDIDATES if os.path.exists(p))
_spec = importlib.util.spec_from_file_location("gh_link_guard", _GUARD_PATH)
gh_link_guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gh_link_guard)

MAX_COMMITS = 2000

MD_RE = re.compile(r"\.(md|markdown)$", re.I)
CODE_RE = re.compile(r"\.(java|ts|tsx|js|jsx|kt|kts)$", re.I)


class GuardError(gh_link_guard.GuardError):
    pass


def sh(args):
    """A git subcommand; a failure = GuardError (fail closed)."""
    try:
        r = subprocess.run(["git"] + args, capture_output=True,
                           text=True, timeout=120)
    except (subprocess.TimeoutExpired, OSError) as e:
        raise GuardError("git %s: %s" % (args[0], e))
    if r.returncode != 0:
        tail = (r.stderr or "").strip().splitlines()
        tail = tail[-1] if tail else "rc=%s" % r.returncode
        raise GuardError("git %s: %s" % (args[0], tail))
    return r.stdout


def is_zero(sha):
    return len(sha) in (40, 64) and set(sha) == {"0"}


def collect_refs(stream):
    """pre-push lines: <local_ref> <local_sha> <remote_ref> <remote_sha>."""
    refs = []
    for raw in stream:
        parts = raw.split()
        if len(parts) == 4:
            refs.append(tuple(parts))
    return refs


def new_commit_shas(local_sha, remote_sha):
    """Commits the push carries to the server (the remote does not have them yet)."""
    if is_zero(local_sha):
        return []
    if not is_zero(remote_sha):
        out = sh(["rev-list", remote_sha + ".." + local_sha])
    else:
        out = sh(["rev-list", local_sha, "--not", "--remotes"])
    return out.split()[:MAX_COMMITS]


def added_lines_by_file(commit_shas):
    """{path: set(added lines)} over all the commits."""
    files = {}
    for c in commit_shas:
        out = sh(["show", "--format=", "--unified=0", "-M", c])
        path = None
        for line in out.splitlines():
            if line.startswith("+++ b/"):
                path = line[6:]
            elif line.startswith("+++ /dev/null"):
                path = None
            elif line.startswith("+") and path is not None:
                files.setdefault(path, set()).add(line[1:])
    return files


def comment_part(line):
    """The part of a code line that is a comment (or None)."""
    if re.match(r"^\s*(//|/\*|\*)", line):
        return line
    i = line.find(" //")
    if i != -1:
        return line[i:]
    return None


def candidate_lines(path, lines):
    """Lines of a file whose links must be checked."""
    if MD_RE.search(path):
        return [l for l in lines if "github.com" in l]
    if CODE_RE.search(path):
        out = []
        for l in lines:
            c = comment_part(l)
            if c and "github.com" in c:
                out.append(c)
        return out
    return []


def findings_for_commits(commits, check=None):
    """The gate's findings for a list of commits."""
    findings = []
    if not commits:
        return findings
    for path, lines in sorted(added_lines_by_file(commits).items()):
        texts = candidate_lines(path, lines)
        if not texts:
            continue
        bad = gh_link_guard.validate_text("\n".join(sorted(texts)),
                                          check=check)
        for url, why in bad:
            findings.append((path, url, why))
    return findings


def run(refs, check=None):
    """(rc, [(file, link, reason)]). rc=1 = refuse the push."""
    check = check or gh_link_guard.gh_api_ok
    commits, seen = [], set()
    for _lr, local_sha, _rr, remote_sha in refs:
        for c in new_commit_shas(local_sha, remote_sha):
            if c not in seen:
                seen.add(c)
                commits.append(c)
    findings = findings_for_commits(commits, check=check)
    return (1 if findings else 0), findings


def report(findings, to):
    for path, url, why in findings:
        print("prepush-link-guard: %s: %s: %s" % (path, url, why), file=to)
    if findings:
        print("prepush-link-guard: push cancelled, fix the links; "
              "a conscious bypass: SKIP_LINK_GUARD=1 git push ...", file=to)


def ensure_gh_token(script=None):
    """GH_TOKEN for gh api, if the environment has none.

    The gh wrapper exports GH_TOKEN only inside itself; a git hook starts
    without it, gh api answers 401, and the gate (fail closed) cancelled the
    push on every CORRECT link. The token is minted the same way the wrapper
    does it: GH_TOKEN=$(gh-app-token.sh), a sibling script of this one.
    """
    if os.environ.get("GH_TOKEN"):
        return
    script = script or os.path.join(SCRIPTS, "gh-app-token.sh")
    if not (os.path.isfile(script) and os.access(script, os.X_OK)):
        return
    try:
        r = subprocess.run(["bash", script], capture_output=True,
                           text=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError):
        return
    if r.returncode == 0 and r.stdout.strip():
        os.environ["GH_TOKEN"] = r.stdout.strip()


def main(argv=None, stream=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if os.environ.get("SKIP_LINK_GUARD"):
        return 0
    scan = "--scan-ref" in argv
    if not scan and not gh_link_guard.in_hermes_session():
        return 0
    ensure_gh_token()
    try:
        sh(["rev-parse", "--git-dir"])
    except GuardError as e:
        print("prepush-link-guard: not in a git repository, skipping: %s" % e,
              file=sys.stderr)
        return 0
    if scan:
        i = argv.index("--scan-ref")
        ref = argv[i + 1] if i + 1 < len(argv) else ""
        base = None
        if "--base" in argv:
            j = argv.index("--base")
            base = argv[j + 1] if j + 1 < len(argv) else None
        try:
            sha = sh(["rev-parse", ref]).strip().splitlines()[0]
        except (GuardError, IndexError):
            print("prepush-link-guard: ref %r not found" % ref, file=sys.stderr)
            return 2
        if base is None:
            # Auditing an already pushed branch: --not --remotes is empty
            # (the commits are on the remote), so the base is looked up among
            # the long-lived branches.
            for cand in ["origin/" + b for b in
                         cfg_list("FACTORY_BASE_BRANCHES", ("main", "master"))]:
                try:
                    sh(["merge-base", "--is-ancestor", cand, sha])
                    base = cand
                    break
                except GuardError:
                    continue
        if base:
            commits = sh(["rev-list", sha, "^" + base]).split()
        else:
            commits = sh(["rev-list", sha, "--not", "--remotes"]).split()
        findings = findings_for_commits(commits)
        report(findings, sys.stdout)
        return 1 if findings else 0
    else:
        refs = collect_refs(stream if stream is not None else sys.stdin)
    rc, findings = run(refs)
    report(findings, sys.stdout if scan else sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
