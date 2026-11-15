#!/usr/bin/env python3
"""Deterministic PR risk class: critical / normal / trivial.

Why it exists: the change class used to be assigned by the reviewer's own
opinion, and the class decides whether the owner looks at the PR. The
reviewer approved changes that the owner then found critical. The party
being checked must not decide for itself whether it needs a check, so the
class is computed from the facts of the diff, without an LLM, by the rules in
pr-risk.yaml next to the script.

Modes:
  pr-risk.py <N> [--json]        class of one PR (files come from gh api)
  pr-risk.py --shadow DATA.json  shadow run: an "owner versus script" table.
                                 A new miss = owner critical/major while the
                                 script is not critical and the PR is not in
                                 the YAML known_misses (known exceptions are
                                 owner decisions, printed separately, and do
                                 not turn the gate red). A critical share
                                 above 60% is red as well.
                                 Exit 1 = calibrate the YAML.
  pr-risk.py --label-sync [N..]  risk:<class> labels on open PRs.
                                 Idempotent: one risk:* label per PR.
                                 While the hold file
                                 $HERMES_HOME/cache/pr-risk-label-hold exists
                                 only a dry run happens: the plan is printed
                                 and the repository is not touched.

The repository comes from FACTORY_REPO (factory.env, see factory_config.py).

Dependencies: stdlib only (PyYAML is absent from the system python, so the
rules file is read by a strict mini parser of a YAML subset: comments,
nested mappings by indentation, dash lists, strings and integers).
"""
import fnmatch
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from factory_config import cfg, hermes_home  # noqa: E402

HOME = hermes_home()
# The rules path override (PR_RISK_YAML) is for calibration runs of the
# shadow mode and for mutant tests: the live file stays untouched.
YAML_PATH = (os.environ.get("PR_RISK_YAML")
             or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "pr-risk.yaml"))
REPO = cfg("FACTORY_REPO")
LABEL_HOLD = os.path.join(HOME, "cache", "pr-risk-label-hold")
# The order does not affect the class (rules are combined with OR), only the
# order of lines in the output.
RULE_ORDER = ("proxy_transport", "auth_headers", "ci_build",
              "migrations_default_config", "test_deletion", "prod_churn",
              "ui_state_plus_view")
CLASSES = ("critical", "normal", "trivial")


# ---------------------------------------------------------------- yaml-mini
def load_yaml_subset(path):
    """Strict parse of a YAML subset: map/list/int/str, indent of 2.

    Some features (anchors, multiline strings, inline comments) are
    deliberately unsupported: the rules file is small and covered by tests,
    so an unknown construct is an error, not a silent skip.
    """
    with open(path, encoding="utf-8") as fh:
        raw = fh.read().splitlines()

    def scalar(text):
        text = text.strip()
        if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
            return text[1:-1]
        if re.fullmatch(r"-?\d+", text):
            return int(text)
        if text in ("true", "false"):
            return text == "true"
        return text

    def parse_block(lines, idx, indent):
        """Returns (value, next index)."""
        if idx >= len(lines):
            return None, idx
        line = lines[idx]
        cur_indent = len(line) - len(line.lstrip(" "))
        if cur_indent < indent:
            return None, idx
        base = cur_indent
        # a list?
        if line.lstrip(" ").startswith("- "):
            items = []
            while idx < len(lines):
                line = lines[idx]
                ci = len(line) - len(line.lstrip(" "))
                if ci != base or not line.lstrip(" ").startswith("- "):
                    break
                rest = line.lstrip(" ")[2:]
                m = re.match(r"^(\w[\w-]*):(?:\s+(.*))?$", rest)
                if not m:
                    items.append(scalar(rest))
                    idx += 1
                    continue
                # a mapping item (known_misses): the first pair is on the
                # dash line, the other pairs are indented by base+2, until the
                # next list item or the end of the block.
                item = {}
                key, val = m.group(1), m.group(2)
                idx += 1
                if val is not None and val != "":
                    item[key] = scalar(val)
                else:
                    item[key], idx = parse_block(lines, idx, base + 2)
                while idx < len(lines):
                    line2 = lines[idx]
                    ci2 = len(line2) - len(line2.lstrip(" "))
                    if ci2 != base + 2 or line2.lstrip(" ").startswith("- "):
                        break
                    m2 = re.match(r"^(\w[\w-]*):(?:\s+(.*))?$", line2.strip())
                    if not m2:
                        break
                    k2, v2 = m2.group(1), m2.group(2)
                    idx += 1
                    if v2 is not None and v2 != "":
                        item[k2] = scalar(v2)
                    else:
                        item[k2], idx = parse_block(lines, idx, base + 4)
                items.append(item)
            return items, idx
        # a mapping
        out = {}
        while idx < len(lines):
            line = lines[idx]
            ci = len(line) - len(line.lstrip(" "))
            if ci != base:
                if ci < base:
                    break
                raise ValueError(f"{path}:{idx+1}: indent {ci}, expected {base}")
            m = re.match(r"^(\w[\w-]*):(?:\s+(.*))?$", line.strip())
            if not m:
                raise ValueError(f"{path}:{idx+1}: cannot parse line: {line!r}")
            key, rest = m.group(1), m.group(2)
            idx += 1
            if rest is not None and rest != "":
                out[key] = scalar(rest)
            else:
                value, idx = parse_block(lines, idx, base + 2)
                out[key] = value
        return out, idx

    clean = []
    for n, text in enumerate(raw, 1):
        stripped = text.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "\t" in text:
            raise ValueError(f"{path}:{n}: tabs are not supported")
        clean.append(text)
    value, end = parse_block(clean, 0, 0)
    if end != len(clean):
        raise ValueError(f"{path}: not parsed to the end, index {end}")
    if not isinstance(value, dict):
        raise ValueError(f"{path}: the root must be a mapping")
    return value


# ----------------------------------------------------------------- classify
def match_any(path, globs):
    """fnmatch: * crosses /, so root files are also compared with the
    pattern without the **/ prefix (so **/pom.xml catches a root pom.xml)."""
    low = path.lower()
    for g in globs:
        g = g.lower()
        if fnmatch.fnmatch(low, g):
            return True
        if g.startswith("**/") and fnmatch.fnmatch(low, g[3:]):
            return True
    return False


def file_category(filename, cfg):
    for cat in ("test", "docs", "lock", "build"):
        if match_any(filename, cfg["categories"].get(cat, [])):
            return cat
    return "prod"


def _short(path):
    parts = path.split("/")
    return "/".join(parts[-2:]) if len(parts) > 1 else path


def classify(files, cfg):
    """files: [{filename, additions, deletions, status}] -> (class, reasons).

    A reason: {"rule": name, "detail": string, "files": [paths]}.
    """
    reasons = []
    for rule in RULE_ORDER:
        rcfg = cfg["critical_rules"].get(rule)
        if not rcfg:
            continue
        hits = [f["filename"] for f in files
                if match_any(f["filename"], rcfg.get("globs", []))]
        if hits:
            reasons.append({"rule": rule, "detail": rcfg.get("comment", ""),
                            "files": hits})
    # test deletion
    td = cfg["critical_rules"].get("test_deletion") or {}
    deleted = []
    for f in files:
        if file_category(f["filename"], cfg) != "test":
            continue
        gone = f.get("status") in ("removed",)
        carved = (f["deletions"] >= td.get("partial_min_deletions", 0)
                  and f["deletions"] > f["additions"])
        if gone or carved:
            deleted.append(f'{_short(f["filename"])} '
                           f'+{f["additions"]}/-{f["deletions"]} ({f.get("status")})')
    if deleted:
        reasons.append({"rule": "test_deletion",
                        "detail": td.get("comment", ""), "files": deleted})
    # production code churn
    pc = cfg["critical_rules"].get("prod_churn") or {}
    churn = sum(f["additions"] + f["deletions"] for f in files
                if file_category(f["filename"], cfg) == "prod")
    limit = pc.get("max_lines")
    if limit is not None and churn > limit:
        prod_names = [_short(f["filename"]) for f in files
                      if file_category(f["filename"], cfg) == "prod"]
        reasons.append({"rule": "prod_churn",
                        "detail": f"{churn} > {limit}",
                        "files": prod_names[:6] + ([f"... {len(prod_names)-6} more"]
                                                  if len(prod_names) > 6 else [])})
    # UI: the state layer and the view layer at once (production files only)
    uv = cfg["critical_rules"].get("ui_state_plus_view") or {}
    state_hits = [f["filename"] for f in files
                  if file_category(f["filename"], cfg) == "prod"
                  and match_any(f["filename"], uv.get("state_globs", []))]
    view_hits = [f["filename"] for f in files
                 if file_category(f["filename"], cfg) == "prod"
                 and match_any(f["filename"], uv.get("view_globs", []))]
    if state_hits and view_hits:
        reasons.append({"rule": "ui_state_plus_view",
                        "detail": uv.get("comment", ""),
                        "files": [_short(p) for p in state_hits + view_hits]})
    if reasons:
        return "critical", reasons
    trivial_globs = cfg["trivial_files"]
    if files and all(match_any(f["filename"], trivial_globs) for f in files):
        return "trivial", []
    return "normal", []


# ------------------------------------------------------------------- gh io
def gh_json(args, timeout=120):
    cmd = ["gh", "-R", REPO, *args] if args and args[0] != "api" \
        else ["gh", *args]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)}: {out.stderr[:300]}")
    return json.loads(out.stdout)


def pr_files(number):
    """The file diff of a PR with pagination (gh api --paginate concatenates documents)."""
    raw = subprocess.run(
        ["gh", "api", "--paginate",
         f"repos/{REPO}/pulls/{number}/files?per_page=100"],
        capture_output=True, text=True, timeout=180)
    if raw.returncode != 0:
        raise RuntimeError(f"gh api pulls/{number}/files: {raw.stderr[:300]}")
    files, dec, idx, buf = [], json.JSONDecoder(), 0, raw.stdout.strip()
    while idx < len(buf):
        obj, end = dec.raw_decode(buf, idx)
        files.extend(obj)
        idx = end
        while idx < len(buf) and buf[idx] in " \n\r\t":
            idx += 1
    return [{"filename": f["filename"], "additions": f["additions"],
             "deletions": f["deletions"], "status": f["status"]}
            for f in files]


# ------------------------------------------------------- owner's severity
SEV_ORDER = {"trivial": 0, "minor": 1, "normal": 2, "major": 3, "critical": 4}
# Two formats of owner verdicts:
#   new:  "CHANGES REQUESTED: major (owner, 2026-01-01, head ...)"
#   old:  "CHANGES REQUESTED: major (core code on the live traffic path)"
#         and even without a parenthesis ("APPROVED: normal"). A deferred
#         sample showed that without the second form the BLOCK floor raised
#         the severity from normal/minor to major (false misses).
SEV_RE = re.compile(
    r"(?:^|\n)\s*(?:#+\s*)?(?:CHANGES[\s_]REQUESTED|APPROVED|COMMENTED)"
    r"\s*:?\s*(critical|major|normal|minor|trivial)\b", re.IGNORECASE)


def owner_max_severity(pr_entry):
    """The owner's maximum finding severity across all rounds.

    The severity word lives in a verdict like "CHANGES REQUESTED: major
    (owner, ...)". Texts with [BLOCK] but without a severity word give at
    least major: a blocking finding is never lighter. Returns
    (severity|None, source).
    """
    texts = ([r.get("body") or "" for r in pr_entry.get("owner_reviews", [])]
             + [c.get("body") or "" for c in pr_entry.get("owner_comments", [])])
    best, src = None, None
    for text in texts:
        words = [w.lower() for w in SEV_RE.findall(text)]
        if words:
            local = max(words, key=lambda w: SEV_ORDER[w])
        elif "[BLOCK]" in text:
            local = "major"
        else:
            continue
        if best is None or SEV_ORDER[local] > SEV_ORDER[best]:
            best, src = local, text[:60].replace("\n", " ")
    return best, src


# --------------------------------------------------------------------- modes
def cmd_one(number, as_json=False):
    cfg = load_yaml_subset(YAML_PATH)
    files = pr_files(number)
    klass, reasons = classify(files, cfg)
    if as_json:
        print(json.dumps({"pr": number, "class": klass, "reasons": reasons,
                          "files": len(files)}, ensure_ascii=False, indent=1))
        return 0
    print(f"#{number} {klass}")
    for r in reasons:
        print(f"  {r['rule']}: {r['detail']}")
        for f in r["files"]:
            print(f"    {f}")
    if not reasons:
        print("  (no critical rule fired)")
    return 0


def shadow_compare(data, cfg):
    """The core of the shadow run, without printing.

    Returns: rows (the table), new_misses (owner critical/major, script not
    critical, PR NOT in known_misses), known_hits (the same, but the PR is in
    known_misses: an owner decision, not a signal), stale (known_misses
    entries that no longer reproduce or whose severity diverged: a signal to
    clean the list).
    """
    known = {}
    for entry in cfg.get("known_misses") or []:
        if isinstance(entry, dict) and "pr" in entry:
            try:
                known[int(entry["pr"])] = entry
            except (TypeError, ValueError):
                continue
    rows, new_misses, known_hits, stale = [], [], [], []
    n_crit = 0
    for pr in sorted(data, key=lambda p: -p["number"]):
        klass, reasons = classify(pr["files"], cfg)
        sev, _src = owner_max_severity(pr)
        cause = "; ".join(f'{r["rule"]}({r["detail"]})' for r in reasons) or "-"
        rows.append((pr["number"], sev or "-", klass, cause))
        if klass == "critical":
            n_crit += 1
        if sev in ("critical", "major") and klass != "critical":
            entry = known.get(pr["number"])
            if entry is None:
                new_misses.append((pr["number"], sev, klass))
            else:
                if entry.get("owner") and entry["owner"] != sev:
                    stale.append((pr["number"],
                                  "severity in the entry is %s, live is %s"
                                  % (entry["owner"], sev)))
                known_hits.append((pr["number"], sev, klass,
                                   entry.get("reason", "")))
    seen = ({num for num, _sev, _klass, *_r in known_hits}
            | {num for num, _sev, _klass in new_misses})
    for num in sorted(set(known) - seen):
        stale.append((num, "the divergence does not reproduce, the entry can be removed"))
    return {"rows": rows, "new_misses": new_misses, "known_hits": known_hits,
            "stale": stale, "n_crit": n_crit, "total": len(rows)}


def cmd_shadow(data_path):
    cfg = load_yaml_subset(YAML_PATH)
    with open(data_path, encoding="utf-8") as fh:
        data = json.load(fh)
    res = shadow_compare(data, cfg)
    rows, misses = res["rows"], res["new_misses"]
    known_hits, stale = res["known_hits"], res["stale"]
    total, n_crit = res["total"], res["n_crit"]
    share = 100.0 * n_crit / total if total else 0.0
    print("| PR | owner (max) | script | reasons |")
    print("|---|---|---|---|")
    for num, sev, klass, cause in rows:
        print(f"| #{num} | {sev} | {klass} | {cause} |")
    print()
    print(f"PRs: {total}, critical: {n_crit} ({share:.0f}%), "
          f"new critical/major misses: {len(misses)}, "
          f"known exceptions: {len(known_hits)}")
    for num, sev, klass, reason in known_hits:
        print(f"  known #{num}: owner {sev}, script {klass} "
              f"({reason})")
    for num, why in stale:
        print(f"  STALE #{num}: {why}")
    for num, sev, klass in misses:
        print(f"  MISS #{num}: owner {sev}, script {klass}")
    if misses:
        print("RESULT: red, calibrate pr-risk.yaml")
        return 1
    if share > 60.0:
        print("RESULT: red, critical is above 60%, the rules are too broad")
        return 1
    print("RESULT: green")
    return 0


def cmd_label_sync(numbers=None):
    """risk:<class> labels on open PRs. While the hold file exists: dry run only."""
    cfg = load_yaml_subset(YAML_PATH)
    hold = os.path.exists(LABEL_HOLD)
    if numbers:
        prs = gh_json(["pr", "view", ",".join(str(n) for n in numbers),
                       "--json", "number,labels"])
        prs = [prs] if isinstance(prs, dict) else prs
    else:
        prs = gh_json(["pr", "list", "--state", "open", "--limit", "50",
                       "--json", "number,labels"])
    plan = []
    for pr in prs:
        number = pr["number"]
        current = {lbl["name"] for lbl in pr.get("labels", [])}
        files = pr_files(number)
        klass, _ = classify(files, cfg)
        desired = f"risk:{klass}"
        stale = sorted(l for l in current
                       if l.startswith("risk:") and l != desired)
        if desired in current and not stale:
            continue
        plan.append((number, desired, stale))
    # Owner decision: no changes = silence (empty stdout, the standard
    # no-agent cron idiom); relabelling is routine and goes into the daily
    # digest; only gh errors (lines with ERROR) are pushed.
    if not plan:
        return 0
    from cron_quiet import digest_add
    lines = []
    if hold:
        lines.append(f"HOLD {LABEL_HOLD}: dry run, the repository is not touched")
        for number, desired, stale in plan:
            extra = f", remove {','.join(stale)}" if stale else ""
            lines.append(f"  #{number}: set {desired}{extra}")
        digest_add("pr-risk-labels", "\n".join(lines))
        return 0
    errors = []
    for number, desired, stale in plan:
        subprocess.run(["gh", "-R", REPO, "label", "create", desired,
                        "--color",
                        "b60205" if desired == "risk:critical" else
                        ("0e8a16" if desired == "risk:normal" else "c2e0c6"),
                        "--force"], capture_output=True, text=True, timeout=60)
        args = ["-R", REPO, "pr", "edit", str(number),
                "--add-label", desired]
        for lbl in stale:
            args += ["--remove-label", lbl]
        out = subprocess.run(["gh", *args], capture_output=True, text=True,
                             timeout=60)
        line = f"  #{number}: {desired} -> " + (
            "ok" if out.returncode == 0 else f"ERROR {out.stderr[:120]}")
        (errors if "ERROR" in line else lines).append(line)
    if lines:
        digest_add("pr-risk-labels", "\n".join(lines))
    for line in errors:  # a failure needs attention, the only push
        print(line)
    return 0


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    if args[0] == "--shadow":
        if len(args) != 2:
            print("usage: pr-risk.py --shadow shadow-data.json")
            return 2
        return cmd_shadow(args[1])
    if args[0] == "--label-sync":
        nums = [int(a) for a in args[1:] if a.isdigit()]
        return cmd_label_sync(nums or None)
    if args[0].isdigit():
        as_json = "--json" in args[1:]
        return cmd_one(int(args[0]), as_json)
    print("could not understand the arguments:", args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
