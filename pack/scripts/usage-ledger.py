#!/usr/bin/env python3
"""Per-card and per-research token accounting. Token totals come from the
built-in `hermes insights`, prices from FACTORY_PRICES (parsed by
metrics-evening.py); only what the built-in tooling lacks is left here:
attributing sessions to cards t_XXXX, the ledger.jsonl ledger and the final
kanban comment on completed cards. Unit of spend: plan credits by
FACTORY_PRICES; a model without a price has an unknown cost.

A session is tied to its card through the board's owner_admitted event,
whose payload carries the session_id and the profile db: a kanban session
title holds the card's topic, not its id, so a title regex alone sends card
runs to "other" (it stays only as a fallback). Rows carry source, st and en,
so the evening report can split spend by source and rate each session by
its interval.

Idempotent: state in $HERMES_HOME/usage-ledger/state.json, ledger in
ledger.jsonl (per session and model; rows without a price keep their tokens
and are reported as unpriced, they are not dropped). The "seen" set marks a
session once and forever, so the last BACKFILL_DAYS are rebuilt on every run:
a better classification and new fields reach rows already written.

Comments on done cards are posted once. A run killed in the middle of the
comment loop must not post them again, so the state is saved after each
comment; and on the first run (when re-attributed history suddenly gives
totals to long-closed cards) the done cards found then are recorded as a
baseline and marked handled without posting."""
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

SESS_RE = re.compile(r'"session_id":\s*"([^"]+)"')
TID_RE = re.compile(r"\bt_[0-9a-f]{8}\b")
BACKFILL_DAYS = 14          # window rebuilt on every run


def session_card_map() -> dict:
    """session_id -> t_XXXX from the owner_admitted events of every board.

    The payload carries the session_id, the event row the task_id. On
    repeated admissions the last one wins (a run can restart on another
    session)."""
    out = {}
    for db in sorted(glob.glob(f"{HOME}/kanban/boards/*/kanban.db")):
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        except Exception:
            continue
        try:
            rows = c.execute("select task_id, payload from task_events "
                             "where kind='owner_admitted' order by id").fetchall()
        except Exception:
            rows = []
        finally:
            c.close()
        for tid, pl in rows:
            m = SESS_RE.search(pl or "")
            if m and TID_RE.fullmatch(tid or ""):
                out[m.group(1)] = tid
    return out


def classify(source, title, sid="", cards=None):
    t = title or ""
    if source == "kanban":
        # the board event first, the id in the title only as a fallback
        if sid and sid in (cards or {}):
            return "card", cards[sid]
        m = TID_RE.search(t)
        if m:
            return "card", m.group(0)
        return "other", "kanban"
    if source == "cron":
        n = t.split("·")[0].strip() or "cron"
        return "research", n
    if source == "webhook":
        return "webhook", "github-comments"
    return "other", source or "unknown"


def collect_db(db, cards):
    """Ledger rows of every finished session of one state.db."""
    prof = db.split("/")[-2] if "/profiles/" in db else "default"
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except Exception:
        return []
    try:
        sess = c.execute("select id,source,title,model,input_tokens,output_tokens,"
                         "cache_read_tokens,reasoning_tokens,started_at,ended_at,"
                         "profile_name from sessions where ended_at is not null").fetchall()
        # per-model breakdown, if the table exists
        pmu = {}
        try:
            for sid, model, i, o, cr, rt in c.execute(
                    "select session_id,model,sum(input_tokens),sum(output_tokens),"
                    "sum(cache_read_tokens),sum(reasoning_tokens) "
                    "from session_model_usage group by session_id,model"):
                pmu.setdefault(sid, []).append((model, i or 0, o or 0, cr or 0, rt or 0))
        except Exception:
            pass
    except Exception:
        return []
    finally:
        c.close()
    rows = []
    for sid, source, title, model, itok, otok, ctok, rtok, st, en, pname in sess:
        kind, ref = classify(source, title, sid, cards)
        parts = pmu.get(sid) or [(model or "?", itok or 0, otok or 0, ctok or 0, rtok or 0)]
        for pm, pi, po, pc, prz in parts:
            rows.append({"ts": int(en or st or time.time()), "profile": pname or prof,
                         "session": sid, "source": source or "unknown",
                         "kind": kind, "ref": ref, "model": pm,
                         "input_tokens": pi, "output_tokens": po,
                         "cache_read_tokens": pc, "reasoning_tokens": prz,
                         "st": int(st or en or 0), "en": int(en or st or 0)})
    return rows


def save_state(state, seen, commented):
    state["seen"] = sorted(seen)
    state["commented"] = sorted(commented)
    with open(STATE_F, "w") as fh:
        json.dump(state, fh)


def main():
    os.makedirs(LDIR, exist_ok=True)
    state = json.load(open(STATE_F)) if os.path.exists(STATE_F) else {"seen": [], "commented": []}
    seen = set(state["seen"]); commented = set(state["commented"])
    cards = session_card_map()
    dbs = [f"{HOME}/state.db"] + sorted(glob.glob(f"{HOME}/profiles/*/state.db"))
    collected = [rec for db in dbs for rec in collect_db(db, cards)]

    # 1) new finished sessions
    rows_out, card_new = [], set()
    for rec in collected:
        key = f"{rec['profile']}:{rec['session']}"
        if key in seen:
            continue
        rows_out.append(rec)
        if rec["kind"] == "card":
            card_new.add(rec["ref"])
    seen.update(f"{r['profile']}:{r['session']}" for r in rows_out)

    # 2) rebuild the last BACKFILL_DAYS: rows of those sessions are replaced by
    # fresh ones (new rows included); older rows stay as they are.
    cutoff = time.time() - BACKFILL_DAYS * 86400
    rebuilt = sorted((r for r in collected if r["en"] >= cutoff),
                     key=lambda r: (r["ts"], r["session"], r["model"]))
    rebuilt_sessions = {r["session"] for r in rebuilt}
    kept = []
    if os.path.exists(LEDGER_F):
        for line in open(LEDGER_F):
            line = line.strip()
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("session") not in rebuilt_sessions:
                kept.append(line)
    rest = [r for r in rows_out if r["session"] not in rebuilt_sessions]
    if rebuilt or rest:
        tmp = LEDGER_F + ".tmp"
        with open(tmp, "w") as f:
            for line in kept:
                f.write(line + "\n")
            for r in rest + rebuilt:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, LEDGER_F)

    # 3) comments on completed cards (once)
    done_ids, listed = set(), False
    try:
        res = subprocess.run(["hermes", "kanban", "--board", BOARD, "list"],
                             capture_output=True, text=True, timeout=60)
        listed = res.returncode == 0
        for line in res.stdout.splitlines():
            if "done" in line or "archived" in line:
                m = TID_RE.search(line)
                if m:
                    done_ids.add(m.group(0))
    except Exception:
        pass
    # full per-card aggregate from the ledger (not only the new rows);
    # credits by FACTORY_PRICES; rows without a price are counted as unpriced.
    # Only cards with rows of the rebuilt window (they carry st) are fresh:
    # re-attributed old history must not comment long-closed cards.
    totals = {}
    if os.path.exists(LEDGER_F):
        for line in open(LEDGER_F):
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("kind") != "card":
                continue
            a = totals.setdefault(r["ref"], {"in": 0, "out": 0, "credits": 0.0,
                                             "unpriced": 0, "models": set(),
                                             "fresh": False})
            a["in"] += r["input_tokens"]; a["out"] += r["output_tokens"]
            c = row_credits(r)
            if c is None:
                a["unpriced"] += 1
            else:
                a["credits"] += c
            a["fresh"] = a["fresh"] or bool(r.get("st"))
            a["models"].add(r["model"])
    # One-off baseline: the done cards seen on the first successful board
    # listing are marked handled without a comment.
    if listed and "done_baseline" not in state:
        state["done_baseline"] = sorted(done_ids & set(totals))
        commented |= set(state["done_baseline"])
        save_state(state, seen, commented)
    for tid in sorted(done_ids):
        if tid in commented or tid not in totals or not totals[tid]["fresh"]:
            continue
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
            # saved at once: a run killed mid-loop must not repeat comments
            save_state(state, seen, commented)
        except Exception:
            pass
    save_state(state, seen, commented)
    costs = [row_credits(r) for r in rows_out]
    tot_cr = sum(c for c in costs if c is not None)
    unpriced = sum(1 for c in costs if c is None)
    extra = f" + unknown ({unpriced} unpriced rows)" if unpriced else ""
    print(f"new rows: {len(rows_out)}; new credits ~{tot_cr:,.0f}{extra}; cards updated: {len(card_new)}; "
          f"rebuilt {BACKFILL_DAYS}d: {len(rebuilt)}; commented: {len(commented)}")

if __name__ == "__main__":
    main()
