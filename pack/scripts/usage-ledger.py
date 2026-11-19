#!/usr/bin/env python3
"""Per-card and per-research token accounting. Token totals come from the
built-in `hermes insights`, prices from FACTORY_PRICES (parsed by
metrics-evening.py); only what the built-in tooling lacks is left here:
attributing sessions to cards t_XXXX, the ledger.jsonl ledger and the final
kanban comment on completed cards. Unit of spend: plan credits by
FACTORY_PRICES; a model without a price has an unknown cost.
Idempotent: state in $HERMES_HOME/usage-ledger/state.json, ledger in
ledger.jsonl (per session; rows without a price keep their tokens and are
reported as unpriced, they are not dropped)."""
import sqlite3, json, re, os, glob, time, subprocess, sys, importlib.util as _ilu

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)
from factory_config import cfg, hermes_home

HOME = hermes_home()
LDIR = f"{HOME}/usage-ledger"
STATE_F = f"{LDIR}/state.json"
LEDGER_F = f"{LDIR}/ledger.jsonl"
BOARD = cfg("FACTORY_BOARD", "main")

# Prices live in metrics-evening.py (FACTORY_PRICES + ledger_row_credits):
# one price source for the evening report and the ledger. The file name has a
# hyphen, so it is loaded through importlib (same approach as in
# board-watchdog.py). Without that sibling script every row is unpriced.
_FM_PATH = os.path.join(SCRIPTS, "metrics-evening.py")
_fm_spec = _ilu.spec_from_file_location("factory_metrics_ul", _FM_PATH)
try:
    if _fm_spec is None or _fm_spec.loader is None:
        raise ImportError(f"cannot load {_FM_PATH}")
    _fm = _ilu.module_from_spec(_fm_spec)
    _fm_spec.loader.exec_module(_fm)
    row_credits = _fm.ledger_row_credits
except Exception:
    def row_credits(r):
        return None

def classify(source, title):
    t = title or ""
    m = re.search(r"\bt_[0-9a-f]{8}\b", t)
    if source == "kanban" and m: return "card", m.group(0)
    if source == "cron":
        n = t.split("·")[0].strip() or "cron"
        return "research", n
    if source == "webhook": return "webhook", "github-comments"
    return "other", source or "unknown"

def main():
    os.makedirs(LDIR, exist_ok=True)
    state = json.load(open(STATE_F)) if os.path.exists(STATE_F) else {"seen": [], "commented": []}
    seen = set(state["seen"]); commented = set(state["commented"])
    rows_out, card_new = [], {}
    dbs = [f"{HOME}/state.db"] + sorted(glob.glob(f"{HOME}/profiles/*/state.db"))
    for db in dbs:
        prof = db.split("/")[-2] if "/profiles/" in db else "default"
        try: c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        except Exception: continue
        try:
            sess = c.execute("select id,source,title,model,input_tokens,output_tokens,cache_read_tokens,reasoning_tokens,started_at,ended_at,profile_name from sessions where ended_at is not null").fetchall()
        except Exception: continue
        # per-model breakdown, if the table exists
        pmu = {}
        try:
            for sid, model, i, o, cr, rt in c.execute("select session_id,model,sum(input_tokens),sum(output_tokens),sum(cache_read_tokens),sum(reasoning_tokens) from session_model_usage group by session_id,model"):
                pmu.setdefault(sid, []).append((model, i or 0, o or 0, cr or 0, rt or 0))
        except Exception: pass
        for sid, source, title, model, itok, otok, ctok, rtok, st, en, pname in sess:
            key = f"{prof}:{sid}"
            if key in seen: continue
            kind, ref = classify(source, title)
            parts = pmu.get(sid) or [(model or "?", itok or 0, otok or 0, ctok or 0, rtok or 0)]
            for pm, pi, po, pc, prz in parts:
                rec = {"ts": int(en or st or time.time()), "profile": pname or prof, "session": sid,
                       "kind": kind, "ref": ref, "model": pm, "input_tokens": pi, "output_tokens": po,
                       "cache_read_tokens": pc, "reasoning_tokens": prz}
                rows_out.append(rec)
                if kind == "card":
                    agg = card_new.setdefault(ref, {"in": 0, "out": 0, "models": set(), "sessions": 0})
                    agg["in"] += pi; agg["out"] += po; agg["models"].add(pm); agg["sessions"] += 1
            seen.add(key)
    if rows_out:
        with open(LEDGER_F, "a") as f:
            for r in rows_out: f.write(json.dumps(r, ensure_ascii=False) + "\n")
    # comments on completed cards (once)
    done_ids = set()
    try:
        out = subprocess.run(["hermes", "kanban", "--board", BOARD, "list"], capture_output=True, text=True, timeout=60).stdout
        for line in out.splitlines():
            if "done" in line or "archived" in line:
                m = re.search(r"\bt_[0-9a-f]{8}\b", line)
                if m: done_ids.add(m.group(0))
    except Exception: pass
    # full per-card aggregate from the ledger (not only the new rows);
    # credits by FACTORY_PRICES; rows without a price are counted as unpriced
    totals = {}
    if os.path.exists(LEDGER_F):
        for line in open(LEDGER_F):
            try: r = json.loads(line)
            except Exception: continue
            if r.get("kind") != "card": continue
            a = totals.setdefault(r["ref"], {"in": 0, "out": 0, "credits": 0.0, "unpriced": 0, "models": set()})
            a["in"] += r["input_tokens"]; a["out"] += r["output_tokens"]
            c = row_credits(r)
            if c is None: a["unpriced"] += 1
            else: a["credits"] += c
            a["models"].add(r["model"])
    for tid in sorted(done_ids):
        if tid in commented or tid not in totals: continue
        a = totals[tid]
        if a["unpriced"] and not a["credits"]:
            spend = f"credits unknown ({a['unpriced']} unpriced rows)"
        else:
            extra = f" + unknown ({a['unpriced']} unpriced rows)" if a["unpriced"] else ""
            spend = f"credits ~{a['credits']:,.0f}{extra}"
        msg = (f"Usage: in={a['in']:,} out={a['out']:,} tokens; models={','.join(sorted(a['models']))}; "
               f"{spend}")
        try:
            subprocess.run(["hermes", "kanban", "--board", BOARD, "comment", tid, msg, "--author", "usage-ledger"],
                           capture_output=True, timeout=60)
            commented.add(tid)
        except Exception: pass
    state["seen"] = sorted(seen); state["commented"] = sorted(commented)
    json.dump(state, open(STATE_F, "w"))
    costs = [row_credits(r) for r in rows_out]
    tot_cr = sum(c for c in costs if c is not None)
    unpriced = sum(1 for c in costs if c is None)
    extra = f" + unknown ({unpriced} unpriced rows)" if unpriced else ""
    print(f"new rows: {len(rows_out)}; new credits ~{tot_cr:,.0f}{extra}; cards updated: {len(card_new)}; commented: {len(commented)}")

if __name__ == "__main__":
    main()
