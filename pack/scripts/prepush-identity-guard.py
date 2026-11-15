#!/usr/bin/env python3
"""Git identity gate for push.

The stock e-mail address of the Hermes agent (hermes@nousresearch.com) spread
through the default git configs of clones and ended up in commits. GitHub
maps that address to an unrelated third-party account (the address is
verified there, with tens of thousands of commits attributed to it across
GitHub), so the authorship of our commits lands in the contributor list of a
stranger. The sources of the leak (a user.email in the .git/config of the main
clone, of the board clone and of worker clones) are cleaned up; this gate
catches a repetition before it reaches the server.

Rule: a push is cancelled if, among the commits it carries to the server,
there is at least one whose author OR committer is on the domain
nousresearch.com (subdomains too, case-insensitive).

Differences from prepush-link-guard.py: no bypass variable (a foreign
identity is always an error, fixed by correcting user.email and re-signing
with --reset-author, not by a flag), and the gate works in any session, not
only in a Hermes session.

The address to configure instead is FACTORY_BOT_EMAIL (the noreply address
of the agent's own GitHub account or app), printed in the refusal message.

Manual audit of a branch without pushing: prepush-identity-guard.py --scan-ref <ref>.
Tests: python3 test_prepush_identity_guard.py
"""
import os
import subprocess
import sys

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)
from factory_config import cfg  # noqa: E402

MAX_COMMITS = 2000
BAD_DOMAIN = "nousresearch.com"
CANONICAL = cfg("FACTORY_BOT_EMAIL",
                "<id>+<bot-name>[bot]@users.noreply.github.com")


class GuardError(Exception):
    pass


def sh(args):
    """A git subcommand; a failure = GuardError (fail closed)."""
    try:
        r = subprocess.run(["git"] + args, capture_output=True,
                           text=True, timeout=120)
    except (subprocess.TimeoutExpired, OSError) as e:
        raise GuardError("git %s: %s" % (args[0], e))
    if r.returncode != 0:
        tail = (r.stderr or "").splitlines()
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


def bad_email(email):
    """An address on the forbidden domain (the domain itself, @domain, a subdomain)."""
    e = (email or "").strip().lower()
    return (e == BAD_DOMAIN
            or e.endswith("@" + BAD_DOMAIN)
            or e.endswith("." + BAD_DOMAIN))


def identity_findings(commit_shas):
    """[(sha, role, 'Name <email>')] for commits with an address on the domain.

    Role: author or committer; one commit can give two findings.
    """
    findings = []
    if not commit_shas:
        return findings
    out = sh(["show", "-s", "--format=%H%x1f%an%x1f%ae%x1f%cn%x1f%ce"]
             + list(commit_shas[:MAX_COMMITS]))
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 5:
            continue
        sha, an, ae, cn, ce = parts
        if bad_email(ae):
            findings.append((sha, "author", "%s <%s>" % (an, ae)))
        if bad_email(ce):
            findings.append((sha, "committer", "%s <%s>" % (cn, ce)))
    return findings


def run(refs):
    """(rc, findings). rc=1 = refuse the push."""
    commits, seen = [], set()
    for _lr, local_sha, _rr, remote_sha in refs:
        for c in new_commit_shas(local_sha, remote_sha):
            if c not in seen:
                seen.add(c)
                commits.append(c)
    findings = identity_findings(commits)
    return (1 if findings else 0), findings


def report(findings, to):
    for sha, role, who in findings:
        print("prepush-identity-guard: %s: %s %s" % (sha[:12], role, who),
              file=to)
    if findings:
        print("prepush-identity-guard: push cancelled, it carries commits with"
              " an address on %s: GitHub hands their authorship to an"
              " unrelated third-party account. Fix user.email in the"
              " repository config (%s) and re-sign the commits with"
              " git rebase --reset-author." % (BAD_DOMAIN, CANONICAL),
              file=to)


def scan_commits(ref, base):
    sha = sh(["rev-parse", ref]).strip().splitlines()[0]
    if base:
        return sh(["rev-list", sha, "^" + base]).split()
    return sh(["rev-list", sha, "--not", "--remotes"]).split()


def main(argv=None, stream=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        sh(["rev-parse", "--git-dir"])
    except GuardError as e:
        print("prepush-identity-guard: not in a git repository, skipping: %s" % e,
              file=sys.stderr)
        return 0
    if "--scan-ref" in argv:
        i = argv.index("--scan-ref")
        ref = argv[i + 1] if i + 1 < len(argv) else ""
        base = None
        if "--base" in argv:
            j = argv.index("--base")
            base = argv[j + 1] if j + 1 < len(argv) else None
        try:
            commits = scan_commits(ref, base)
            findings = identity_findings(commits)
        except (GuardError, IndexError) as e:
            print("prepush-identity-guard: ref %r not found: %s" % (ref, e),
                  file=sys.stderr)
            return 2
        report(findings, sys.stdout)
        return 1 if findings else 0
    refs = collect_refs(stream if stream is not None else sys.stdin)
    try:
        rc, findings = run(refs)
    except GuardError as e:
        print("prepush-identity-guard: git unavailable, fail closed: %s" % e,
              file=sys.stderr)
        return 1
    report(findings, sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
