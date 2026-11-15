#!/usr/bin/env python3
"""GitHub link gate for Hermes sessions.

An agent once posted several comments whose permalinks pointed at a
repository that does not exist (one extra letter in the owner login). The
typo was not in any knowledge layer: the model generated it, nobody opened
the links before posting, and the reviewer checked the claims but not the
links. Every one of those links returned 404.

The gate is called from a `gh` wrapper in the context of a Hermes session
(HERMES_AGENT=true or a non-empty HERMES_SESSION_ID) and checks, BEFORE
sending, the text that goes to GitHub (a comment, or the body or title of a
PR or issue):

  CLI commands: pr comment/edit/create/review, issue comment/edit/create;
  the text is taken from --body, --body-file, --title, -f/-F/--field/
  --raw-field body=|title= and --input (a JSON file with body/title keys).

  gh api: the endpoints issues/N/comments, issues/comments/N, issues/N,
  pulls/N, pulls/N/comments (creating and editing comments, issues, PRs);
  the text is taken from the same flags.

Rule: every github.com link in the published text must exist.
  - owner/repo: the request repos/{owner}/{repo} answers 200 (404 = broken);
  - blob/tree: the path exists at the given ref (contents API);
  - commit: the SHA exists in the repository (commits API).
No github.com links = the publication goes through unchecked. A failed check
(network, 401, 403) = refusal: fail closed. Outside Hermes sessions the gate
is off: a publication by the owner's own hands bypasses the check.
"""
import json
import os
import re
import subprocess
import sys
from urllib.parse import quote

GH = "/usr/bin/gh"

CLI_PUBLISH = {
    ("pr", "comment"), ("pr", "edit"), ("pr", "create"), ("pr", "review"),
    ("issue", "comment"), ("issue", "edit"), ("issue", "create"),
}

# gh flags that take the next argument as their value
VALUE_FLAGS = {
    "--body", "--body-file", "--title", "--input",
    "-f", "-F", "--field", "--raw-field",
    "-X", "--method", "-H", "--header", "--repo", "--hostname", "--cache",
}

API_ENDPOINTS = [
    re.compile(r"(?:repos/)?[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/\d+/comments/?$"),
    re.compile(r"(?:repos/)?[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/comments/\d+/?$"),
    re.compile(r"(?:repos/)?[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/\d+/?$"),
    re.compile(r"(?:repos/)?[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pulls/\d+/?$"),
    re.compile(r"(?:repos/)?[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pulls/\d+/comments/?$"),
]

GITHUB_LINK_RE = re.compile(
    r"https?://(?:www\.)?github\.com/"
    r"([A-Za-z0-9][A-Za-z0-9_.-]*)"        # owner
    r"/([A-Za-z0-9_.-]+)"                  # repo
    r"(?:/(blob|tree|commit)/([^\s\)\]\"'<>#?]+))?"  # ref(+path) or SHA
)
TRAILING_PUNCT = ".,;:!"


class GuardError(Exception):
    """The check could not be done: treated as a refusal (fail closed)."""


def in_hermes_session(env=None):
    env = env if env is not None else os.environ
    return env.get("HERMES_AGENT") == "true" or bool(env.get("HERMES_SESSION_ID"))


def api_endpoint(args):
    """First positional argument after `gh api` (or None)."""
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("-"):
            if "=" not in arg and arg in VALUE_FLAGS:
                i += 2
                continue
            i += 1
            continue
        return arg
    return None


def is_publish_command(argv):
    """True: the command publishes or edits text on GitHub."""
    if len(argv) >= 2 and argv[0] in ("pr", "issue"):
        if (argv[0], argv[1]) in CLI_PUBLISH:
            return True
    if argv and argv[0] == "api":
        endpoint = api_endpoint(argv[1:])
        if endpoint:
            return any(rx.search(endpoint) for rx in API_ENDPOINTS)
    return False


def extract_texts(argv):
    """[(kind, value)] from the command flags. Kinds: text, file, json-file."""
    found = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if not arg.startswith("-"):
            i += 1
            continue
        name, val = arg, None
        if "=" in arg:
            name, val = arg.split("=", 1)
        if val is None and name in VALUE_FLAGS and i + 1 < len(argv):
            val = argv[i + 1]
            i += 1
        i += 1
        if val in (None, ""):
            continue
        if name in ("--body", "--title"):
            found.append(("text", val))
        elif name == "--body-file":
            found.append(("file", val))
        elif name == "--input":
            found.append(("json-file", val))
        elif name in ("-f", "-F", "--field", "--raw-field") and "=" in val:
            key, v = val.split("=", 1)
            if key in ("body", "title") and v:
                found.append(("text", v))
    return found


def resolve_texts(items):
    """[(kind, value)] -> list of strings to check."""
    texts = []
    for kind, val in items:
        if kind == "text":
            texts.append(val)
        elif kind == "file":
            try:
                with open(val, "r", encoding="utf-8") as fh:
                    texts.append(fh.read())
            except OSError as e:
                raise GuardError("cannot read %s: %s" % (val, e))
        elif kind == "json-file":
            if val == "-":
                raise GuardError("--input - (stdin) is not checked: refusal")
            try:
                with open(val, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError) as e:
                raise GuardError("cannot read JSON %s: %s" % (val, e))
            if not isinstance(data, dict):
                continue
            for key in ("body", "title"):
                v = data.get(key)
                if isinstance(v, str) and v:
                    texts.append(v)
    return texts


def http_page_ok(url):
    """True: HTTP 200 after redirects. GuardError: anything else (fail closed).

    For github.com pages that have no REST API counterpart (advisories).
    """
    try:
        r = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-L", url],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        raise GuardError("curl %s: %s" % (url, e))
    code = (r.stdout or "").strip()
    if code == "200":
        return True
    if code.startswith("4"):
        return False
    raise GuardError("curl %s: HTTP %s" % (url, code))


def gh_api_ok(api_path):
    """True: 200. False: 404 (no such object). GuardError: anything else (fail closed)."""
    try:
        r = subprocess.run(
            [GH, "api", api_path, "--silent"],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        raise GuardError("gh api %s: %s" % (api_path, e))
    if r.returncode == 0:
        return True
    err = r.stderr or ""
    if "404" in err or "Not Found" in err:
        return False
    tail = err.strip().splitlines()[-1] if err.strip() else "rc=%s" % r.returncode
    raise GuardError("gh api %s: %s" % (api_path, tail))


def validate_text(text, check=None):
    """[(link, reason)] for every github.com link in the text. Empty = clean."""
    check = check or gh_api_ok
    bad = []
    cache = {}
    for m in GITHUB_LINK_RE.finditer(text):
        owner = m.group(1).rstrip(TRAILING_PUNCT)
        repo = m.group(2).rstrip(TRAILING_PUNCT)
        kind = m.group(3)
        tail = (m.group(4) or "").rstrip(TRAILING_PUNCT)
        if not owner or not repo:
            continue
        # github.com/advisories/<id> is a site page, not owner/repo: the repos
        # API answers 404 for it, so a live advisory link from a dependency bot
        # was wrongly rejected as broken. Existence is checked over HTTP.
        if owner == "advisories":
            key = ("adv", repo)
            try:
                if key not in cache:
                    cache[key] = http_page_ok("https://github.com/advisories/%s" % repo)
            except GuardError as e:
                return [(m.group(0), "check unavailable: %s" % e)]
            if not cache[key]:
                bad.append((m.group(0), "page advisories/%s does not exist (404)" % repo))
            continue
        api = "repos/%s/%s" % (owner, repo)
        try:
            if api not in cache:
                cache[api] = check(api)
        except GuardError as e:
            return [(m.group(0), "check unavailable: %s" % e)]
        if not cache[api]:
            bad.append((m.group(0), "repository %s/%s does not exist (404)" % (owner, repo)))
            continue
        if kind in ("blob", "tree"):
            mm = re.match(r"([^/]+)(?:/(.*))?", tail)
            if mm is None:
                continue
            ref, path = mm.group(1), mm.group(2) or ""
            api = "repos/%s/%s/contents/%s?ref=%s" % (
                owner, repo, quote(path, safe="/"), quote(ref, safe=""))
            if path == "":
                api = "repos/%s/%s/contents?ref=%s" % (owner, repo, quote(ref, safe=""))
            try:
                if api not in cache:
                    cache[api] = check(api)
            except GuardError as e:
                return [(m.group(0), "check unavailable: %s" % e)]
            if not cache[api]:
                bad.append((m.group(0), "path does not exist at %s (404)" % ref))
        elif kind == "commit":
            api = "repos/%s/%s/commits/%s" % (owner, repo, tail)
            try:
                if api not in cache:
                    cache[api] = check(api)
            except GuardError as e:
                return [(m.group(0), "check unavailable: %s" % e)]
            if not cache[api]:
                bad.append((m.group(0), "commit %s does not exist (404)" % tail))
    return bad


def main(argv):
    if not in_hermes_session():
        return 0
    try:
        if not is_publish_command(argv):
            return 0
        texts = resolve_texts(extract_texts(argv))
        for text in texts:
            bad = validate_text(text)
            if bad:
                for url, why in bad:
                    print("gh-link-guard: broken link: %s: %s" % (url, why), file=sys.stderr)
                print("gh-link-guard: publication cancelled, fix the links", file=sys.stderr)
                return 1
    except GuardError as e:
        print("gh-link-guard: could not check the links: %s; refusing (fail closed)" % e,
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
