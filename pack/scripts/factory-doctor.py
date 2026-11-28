#!/usr/bin/env python3
"""Check a project conventions file against the pack contract.

Usage: factory-doctor.py <conventions-file> <pack-root>

<pack-root> is the `pack/` directory of a pack clone. Checks:
  - every slot heading of the contract is present and has content beyond
    HTML comments and TODO placeholder lines;
  - every "overrides <ID>" line in the Pack overrides slot names a rule id
    tagged `**[<ID>]**` in the pack's skills, souls or stacks.
Prints a short report; exit 1 on any problem.
"""
import os
import re
import sys

SLOTS = [
    "Integration branch and PR base",
    "Build and test commands",
    "Local verification policy",
    "Visual evidence location",
    "Release mechanics",
    "Deprecated surfaces policy",
    "Test stand / environments",
    "Owner decisions (dated)",
    "Persona addendum",
    "Pack overrides",
]
TAG_RE = re.compile(r"\*\*\[([A-Z]+-\d+)\]\*\*")
OVERRIDE_RE = re.compile(r"\boverrides\s+([A-Z]+-\d+)\b")
TAG_DIRS = ("skills", "souls", "stacks")


def sections(text):
    """Map of `## ` heading to its body, frontmatter and comments removed."""
    text = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    out, cur = {}, None
    for line in text.splitlines():
        if line.startswith("## "):
            cur = line[3:].strip()
            out[cur] = []
        elif cur is not None:
            out[cur].append(line)
    return out


def filled(lines):
    return [l for l in lines if l.strip() and not l.strip().startswith("TODO")]


def pack_ids(pack_root):
    ids = set()
    for sub in TAG_DIRS:
        for dirpath, _, files in os.walk(os.path.join(pack_root, sub)):
            for f in files:
                if f.endswith(".md"):
                    with open(os.path.join(dirpath, f), encoding="utf-8") as fh:
                        ids.update(TAG_RE.findall(fh.read()))
    return ids


def check(conventions, pack_root):
    with open(conventions, encoding="utf-8") as fh:
        secs = sections(fh.read())
    problems = []
    for slot in SLOTS:
        if slot not in secs:
            problems.append("missing slot: ## %s" % slot)
        elif not filled(secs[slot]):
            problems.append("empty slot (placeholder only): ## %s" % slot)
    known = pack_ids(pack_root)
    for line in filled(secs.get("Pack overrides", [])):
        for rid in OVERRIDE_RE.findall(line):
            if rid not in known:
                problems.append("override of unknown rule id: %s" % rid)
    return problems, len(known)


def main(argv):
    if len(argv) != 3:
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    problems, n_ids = check(argv[1], argv[2])
    print("factory-doctor: %d slots, %d pack rule ids" % (len(SLOTS), n_ids))
    for p in problems:
        print("  PROBLEM " + p)
    print("factory-doctor: %s" % ("FAIL" if problems else "OK"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
