#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""One helper for the owner's rule "is the native mechanism or a live card
already doing it?" (RULE_50B, the native-or-live rule).

RULE_50B (a general rule, not a special case):

"Before creating a card, reassigning, restoring or removing something on the
board, check whether a native Hermes mechanism or a live card already does
it: the review lane after request_review, the native respawn and its guards
(active_pr, recent_success), task_links and dependency_wait, a verdict on
this head, a live card with the same key (issue or PR + role + head). If it
does, create nothing: extend it or wait for it. Our mechanism may act only
where the native one will not, and the code next to it says which native
path was checked and why it is not enough. A manual action during a
teaching round obeys the same rule: restoring things by hand past the gates
is forbidden."

Where it came from: four forms of one defect on one day, each time our
mechanism on top of a native one created duplicate work:
  * three coder cards for one patch (closed by dedup in the admission gate);
  * a worker's own handoff comment switched on the native active_pr guard,
    and the watchdog kept running reclaim/respawn against it;
  * a stability check sampled a head that was still under rework instead
    of waiting for the native review (fixed with a dependency chain);
  * an auto-armed review ran in parallel with the native review lane
    (fixed with a live re-read of the board before create).

Single point of answer: native_or_live(key, ...). Every card generator and
every reassign/restore operation MUST call it and get a GuardNote receipt:
covered=True -> do not create, extend or wait; covered=False -> the receipt
(which native path was checked and why it is not enough) goes into the card
body. _kanban_create in board-watchdog requires the receipt by type, and the
static test test_board_native_guard.py fails the suite when a new card
generator does not call the helper (scan_generator_violations).

The full text of the rule also belongs in the kanban-discipline skill (the
layer every role reads).
"""
import ast
import re
import sys

RULE_50B = (
    "Before creating a card, reassigning, restoring or removing something "
    "on the board, check whether a native Hermes mechanism or a live card "
    "already does it: the review lane after request_review, the native "
    "respawn and its guards (active_pr, recent_success), task_links and "
    "dependency_wait, a verdict on this head, a live card with the same "
    "key (issue or PR + role + head). If it does, create nothing: extend "
    "it or wait for it. Our mechanism may act only where the native one "
    "will not, and the code next to it says which native path was checked "
    "and why it is not enough. A manual action during a teaching round "
    "obeys the same rule: restoring things by hand past the gates is "
    "forbidden.")

# Native paths every generator must keep in mind.
NATIVE_REVIEW_LANE = "review_lane"
NATIVE_RESPAWN = "respawn"
NATIVE_TASK_LINKS = "task_links"
NATIVE_VERDICT = "verdict"
NATIVE_TITLES = {
    NATIVE_REVIEW_LANE: "review lane after request_review",
    NATIVE_RESPAWN: "native respawn and its guards (active_pr, recent_success)",
    NATIVE_TASK_LINKS: "task_links and dependency_wait",
    NATIVE_VERDICT: "verdict on this head",
}
_ALL_NATIVE = frozenset(NATIVE_TITLES)


class GuardNote(str):
    """The helper's receipt. Created only inside native_or_live: a plain
    string cannot pass the isinstance check."""


_FAM_SUFFIX = re.compile(r"-(?:r|p)\d+$")


def _fam(key):
    """Base of a key family: reviewer-pr12-abc1234-r3 and -p2 ->
    reviewer-pr12-abc1234. A repeated arm (-rN) and a paired arm (-pN) stay
    in the same family: a live relative means taken."""
    return _FAM_SUFFIX.sub("", (key or "").strip())


def _check_flags(flags, arg):
    for f in flags:
        if f not in _ALL_NATIVE:
            raise ValueError(
                "native_or_live: unknown native path %r in %s "
                "(allowed: %s)" % (f, arg, ", ".join(sorted(_ALL_NATIVE))))


def native_or_live(key, live_keys=(), native=(), native_checked=(), why=""):
    """Is there already native or live coverage for key X.

    key: the generator's dedup key (issue or PR + role + head).
    live_keys: keys of LIVE board cards (status not done/archived).
    native: native paths (NATIVE_*) that ALREADY cover the key ->
        covered=True, creating is forbidden.
    native_checked: native paths the caller checked that do NOT cover the
        key. Must be non-empty: "checked nothing" does not exist, the right
        to act appears only after the check.
    why: why the native path is not enough. Must be non-empty when
        covered=False: it goes into the receipt and the card body.

    Returns (covered: bool, note: GuardNote). covered=True: do not create,
    extend or wait for the existing one.
    """
    key = (key or "").strip()
    if not key:
        raise ValueError("native_or_live: empty generator key")
    _check_flags(native, "native")
    _check_flags(native_checked, "native_checked")
    for lk in live_keys:
        if lk and _fam(lk) == _fam(key):
            return True, GuardNote(
                "live card with key %s already covers key %s: "
                "extend it or wait for it, a duplicate is forbidden "
                "(native-or-live rule)" % (lk, key))
    for f in native:
        return True, GuardNote(
            "a native mechanism already does this (%s): do not create, "
            "extend it or wait for it (native-or-live rule)"
            % NATIVE_TITLES[f])
    if not native_checked:
        raise ValueError(
            "native_or_live: native_checked is empty for %s: the right to "
            "create appears only after checking the native paths "
            "(native-or-live rule)" % key)
    if not (why or "").strip():
        raise ValueError(
            "native_or_live: empty reason why for %s: the code next to the "
            "action must say which native path was checked and why it is "
            "not enough (native-or-live rule)" % key)
    return False, GuardNote(
        "native checked, does not cover: %s. Missing: %s"
        % ("; ".join(NATIVE_TITLES[f] for f in native_checked),
           why.strip()))


def guard_section(note):
    """Receipt section for a card body (which native path was checked)."""
    return "## Native guard (native-or-live rule)\n%s\n\n" % note


# --- static check of generators -------------------------------------------
#
# A card generator is a function that creates a card (a call to
# _kanban_create, kb("create", ...) or a command list with "kanban" and
# "create") or reassigns/restores (a command with "kanban" and "reassign").
# Each such generator must call native_or_live or take the receipt as a
# guard= parameter (it delegates the check to its caller; the runtime gate
# in _kanban_create checks the type).

_CREATOR_FILES_HINT = ("board-watchdog.py", "pr-hygiene.py",
                       "plan-review-gate.py")


def _cmd_constants(node):
    """String constants of a call or a list, including list elements."""
    out = []
    if isinstance(node, ast.List):
        for el in node.elts:
            if isinstance(el, ast.Constant) and isinstance(el.value, str):
                out.append(el.value)
    elif isinstance(node, ast.Call):
        for a in node.args:
            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                out.append(a.value)
    return out


def scan_generator_violations(source, name="<src>"):
    """[(func, reason)] of card/reassign generators without the helper.

    A static check over the sources: a new generator that creates a card or
    reassigns without calling native_or_live (and without taking the guard=
    receipt) fails the test suite.
    """
    tree = ast.parse(source)
    violations = []

    def names_in(fn):
        return ({n.id for n in ast.walk(fn)
                 if isinstance(n, ast.Name)}
                | {n.attr for n in ast.walk(fn)
                   if isinstance(n, ast.Attribute)})

    def creates(fn):
        kind = None
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and isinstance(
                    node.func, ast.Name):
                if node.func.id == "_kanban_create":
                    kind = kind or "create"
                if node.func.id == "kb" and node.args and isinstance(
                        node.args[0], ast.Constant) \
                        and node.args[0].value == "create":
                    kind = kind or "create"
            if kind:
                continue
            consts = _cmd_constants(node)
            if "kanban" in consts and "create" in consts:
                kind = "create"
            elif "kanban" in consts and "reassign" in consts:
                kind = kind or "reassign"
        return kind

    def delegates_guard(fn):
        """The function takes guard and passes it on as guard=guard."""
        params = {a.arg for a in fn.args.args}
        if "guard" not in params:
            return False
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "guard" and isinstance(
                            kw.value, ast.Name) and kw.value.id == "guard":
                        return True
        return False

    for fn in [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        kind = creates(fn)
        if not kind:
            continue
        names = names_in(fn)
        if "native_or_live" in names or "GuardNote" in names:
            continue
        if kind == "create" and delegates_guard(fn):
            continue
        violations.append((
            fn.name,
            "%s of a card or reassign without native_or_live and without "
            "a guard= receipt (native-or-live rule)" % kind))
    return violations


def rework_key(number, head):
    """One key for the rework card of an open PR.

    coder-pr<N>-rework-<head7>: a verdict is bound to the PR head, so the key
    names the pair (PR, verdict head) unambiguously. One helper for every
    rework generator: the watchdog's arm, pr-hygiene create_rework_card, and
    the reviewer on a RETURN verdict. Origin: a reviewer took its own key
    (reviewer-pr<N>-return) and the priority gate, which recognised rework
    only by words ("rework" in the key or title), missed it. Every generator
    with its own key is a new spelling that neither the dedup nor the
    priority gate knows. head: full sha or its first 7+ characters.
    """
    h = str(head or "").strip().lower()
    if len(h) < 7:
        raise ValueError(
            "rework_key: the verdict head is required (7+ hex), got %r"
            % head)
    return "coder-pr%d-rework-%s" % (int(number), h[:7])


def scan_files(paths):
    """[(file, func, reason)] over a list of source paths."""
    out = []
    for path in paths:
        try:
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
        except OSError as exc:
            out.append((path, "<read>", "unreadable: %s" % exc))
            continue
        for fn, why in scan_generator_violations(src, path):
            out.append((path, fn, why))
    return out


def main(argv=None):
    """CLI of the helper for webhook prompts.

    When the GitHub webhook also listens to issue events, the card for an
    issue is created by the agent of that run, not by a script. So that the
    creation still goes through the shared helper, the prompt calls this CLI
    and puts the receipt into the card body.

    Exit: rc 0 + 'RECEIPT: ...' (create, receipt below),
    rc 3 + 'COVERED: ...' (do not create, extend the existing one),
    rc 2 + 'ERROR: ...' (bad arguments).
    """
    import argparse
    ap = argparse.ArgumentParser(
        description="native_or_live from the command line: the "
                    "native-or-live receipt for webhook prompts")
    ap.add_argument("--key", required=True,
                    help="dedup key, for example issue-383")
    ap.add_argument("--live-keys", default="",
                    help="keys of live cards, comma-separated "
                         "(for example issue-383 if a card already exists)")
    ap.add_argument("--native", default="",
                    help="native paths already covering the key, "
                         "comma-separated")
    ap.add_argument("--native-checked", default="",
                    help="checked native paths, comma-separated "
                         "(must be non-empty)")
    ap.add_argument("--why", default="",
                    help="why the native path is not enough (required when "
                         "there is no live coverage)")
    args = ap.parse_args(argv)
    live = [k.strip() for k in args.live_keys.split(",") if k.strip()]
    native = [k.strip() for k in args.native.split(",") if k.strip()]
    checked = [k.strip() for k in args.native_checked.split(",")
               if k.strip()]
    try:
        covered, note = native_or_live(args.key, live_keys=live,
                                       native=native,
                                       native_checked=checked,
                                       why=args.why)
    except ValueError as exc:
        print("ERROR: %s" % exc)
        return 2
    print("%s: %s" % ("COVERED" if covered else "RECEIPT", note))
    print(guard_section(note), end="")
    return 3 if covered else 0


if __name__ == "__main__":
    sys.exit(main())
