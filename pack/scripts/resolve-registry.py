#!/usr/bin/env python3
"""Resolves a merge conflict in an append-only numbered registry file when the
base branch is merged into a PR branch.

Both sides append entries to the same numbered list, so even a pair with the
same text conflicts: a number that grows from one digit to two shifts the
alignment of the block by a column. The resolution is mechanical: take the
list from the base as the foundation, add the entries it does not have,
renumber consecutively.

Usage: resolve-registry.py <path to the registry file> <base version text> <branch version text>
The texts are passed as files so that nothing depends on shell escaping.
"""
import re
import sys

ENTRY = re.compile(r"^(\d+)\.[ \t]+(.*)$")


def parse(text):
    """Returns (header, [lines of an entry body]) keeping nested indentation.

    Exactly the indentation that aligned the entry under its number (the width
    of the "N. " prefix) is removed, not a greedy three or four spaces: entries
    contain nested lists, and a greedy cut collapsed them into one level, which
    made an unchanged entry look rewritten.
    """
    lines = text.splitlines()
    head, entries, current, indent = [], [], None, 0
    for line in lines:
        match = ENTRY.match(line)
        if match:
            indent = len(match.group(0)) - len(match.group(2))
            current = [match.group(2)]
            entries.append(current)
            continue
        if current is None:
            head.append(line)
        elif line[:indent].strip() == "":
            current.append(line[indent:])
        else:
            current.append(line.lstrip())
    for body in entries:
        while body and not body[-1].strip():
            body.pop()
    return head, entries


def key(body):
    """The key of an entry: the first significant words, without punctuation
    and line breaks.

    Punctuation is dropped on purpose: a branch may rewrite the same entry
    changing only its punctuation (a style rule replacing one dash character
    with a colon), and such an edit must count as an update of the entry, not
    as a second entry about the same change.
    """
    joined = " ".join(body).lower()
    joined = re.sub(r"[^a-z0-9]+", " ", joined).strip()
    return " ".join(joined.split()[:12])


def render(head, entries):
    out = list(head)
    while out and not out[-1].strip():
        out.pop()
    for number, body in enumerate(entries, start=1):
        out.append("")
        prefix = "%d. " % number
        pad = " " * len(prefix)
        out.append(prefix + body[0])
        for line in body[1:]:
            out.append((pad + line) if line.strip() else "")
    return "\n".join(out) + "\n"


def main():
    target, base_file, branch_file = sys.argv[1:4]
    head, base = parse(open(base_file, encoding="utf-8").read())
    _, theirs = parse(open(branch_file, encoding="utf-8").read())

    index = {key(b): i for i, b in enumerate(base)}
    merged = list(base)
    added, updated = [], []
    for body in theirs:
        position = index.get(key(body))
        if position is None:
            merged.append(body)
            added.append(body[0][:60])
        elif body != merged[position]:
            merged[position] = body
            updated.append(body[0][:60])
    open(target, "w", encoding="utf-8").write(render(head, merged))
    print("entries: base %d, branch %d, result %d" % (len(base), len(theirs), len(merged)))
    for line in added:
        print("  added from the branch: " + line)
    for line in updated:
        print("  updated by the branch: " + line)


if __name__ == "__main__":
    main()
