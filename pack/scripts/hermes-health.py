#!/usr/bin/env python3
"""Whole-installation Hermes health check: one command, one verdict.

Origin: over one week ten breakages piled up, each found by its traces rather
than by a check: a spend watchdog left a nonexistent provider and address
combination, the fallback chain consisted of deleted models, vision sent
requests to the wrong endpoint, cards sat blocked with finished dependencies,
a gateway restart killed workers. Each cost hours to a day of downtime, and all
of them are visible within seconds if the system is checked as a whole rather
than piece by piece.

Run by a cron job and by hand after any configuration change. Prints only
problems: empty output means everything is fine.

Exit: 0 if all is clean, 1 if there is at least one problem.
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request

# Sibling scripts live next to this file, whatever the Hermes home is.
SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)
from factory_config import cfg, hermes_home, cfg_list
# One digest algorithm for run-tests.py and the drift check below:
# diverging copies would mean false alarms or a hole.
import scripts_digest  # noqa: E402

HOME = hermes_home()
BOARDS = os.path.join(HOME, "kanban", "boards")
HERMES = os.path.expanduser("~/.local/bin/hermes")
STATE = os.path.join(HOME, "kanban", "health-state.json")
# The same problem repeats at most once per 12 hours: the check runs every half
# hour, and without this the report would turn into noise nobody reads. A new
# problem arrives at once.
REPEAT_AFTER = 12 * 3600
problems = []


def note(check, detail, fix):
    problems.append((check, detail, fix))


def run(cmd, timeout=60):
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return done.returncode, (done.stdout or "") + (done.stderr or "")
    except Exception as exc:
        return 1, str(exc)


def env_value(name):
    try:
        for line in open(os.path.join(HOME, ".env")):
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


def load_yaml(path):
    try:
        import yaml
        return yaml.safe_load(open(path, encoding="utf-8")) or {}
    except Exception:
        return {}


def check_services():
    for unit in ("hermes-gateway", "hermes-dashboard"):
        code, out = run(["systemctl", "--user", "is-active", unit], timeout=20)
        if out.strip() != "active":
            note("service", "%s is not active (%s)" % (unit, out.strip()),
                 "systemctl --user status %s" % unit)


def check_provider_config():
    """Provider, address and key must agree with each other.

    A mismatch (provider openrouter with a DeepSeek base_url) once cost a day
    of downtime: the combination does not exist, every call got a 401.
    """
    known = {
        "custom": None,
        "openrouter": "openrouter.ai",
        "openai-codex": None,
        "deepseek": "api.deepseek.com",
    }
    paths = [os.path.join(HOME, "config.yaml")]
    root = os.path.join(HOME, "profiles")
    if os.path.isdir(root):
        paths += [os.path.join(root, p, "config.yaml") for p in sorted(os.listdir(root))]
    for path in paths:
        if not os.path.exists(path):
            continue
        model = load_yaml(path).get("model") or {}
        provider, base = model.get("provider"), (model.get("base_url") or "")
        expect = known.get(provider)
        name = "MAIN" if path == os.path.join(HOME, "config.yaml") else path.split("/")[-2]
        if expect and base and expect not in base:
            note("config", "%s: provider=%s, but base_url=%s" % (name, provider, base),
                 "make the address and key match the provider")
        if provider == "custom" and base and not (model.get("api_key") or ""):
            note("config", "%s: custom provider %s without a key" % (name, base),
                 "set api_key")


# Quota death is provider-specific, so it is configuration: the error codes a
# provider puts on a 429 line when the quota is gone (FACTORY_QUOTA_CODES) and
# the matching message texts (FACTORY_QUOTA_TEXTS, paired with the codes by
# position). A detector that matched only one text once let the second form
# slip by: match by code (in a line with 429) and by every text.
QUOTA_CODES = tuple(cfg_list("FACTORY_QUOTA_CODES"))
QUOTA_TEXTS = tuple(cfg_list("FACTORY_QUOTA_TEXTS"))
QUOTA_CODE_RE = (re.compile(r"429[^\n]*?\b(%s)\b" % "|".join(map(re.escape, QUOTA_CODES)))
                 if QUOTA_CODES else None)
QUOTA_RESET_RE = re.compile(r"reset at (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
OWNER_CHAT = cfg("FACTORY_OWNER_CHAT")


# Root of the quota state (incident, recovery marker, logs for the scan):
# overridden by env QUOTA_STATE_ROOT in tests, like CRON_JOBS_ROOT for crons.
def qroot():
    return os.environ.get("QUOTA_STATE_ROOT") or HOME


def quota_incident_path():
    return os.path.join(qroot(), "cache", "quota-death.json")


# After the quota came back, the detector once reopened the incident twice on
# a 429 1310 line written BEFORE the recovery: the log scan does not know the
# quota had already answered, and until the line left the 45-minute window
# every tick without an incident set a new false stop and sent a signal. The
# marker records the moment of recovery: lines older than it are not evidence.
def quota_recovered_path():
    return os.path.join(qroot(), "cache", "quota-recovered.json")


def quota_recovered_at():
    """Epoch of the last quota recovery or None (no/broken marker)."""
    try:
        ts = json.load(open(quota_recovered_path(), encoding="utf-8")).get("ts")
    except Exception:
        return None
    if isinstance(ts, bool) or not isinstance(ts, (int, float)) or ts <= 0:
        return None
    return ts
# Crons that do not burn LLM quota (gh/git/sqlite/balance statistics), plus
# health itself: while an incident is open, health is the one that probes and
# brings everything back, so it must not be paused. Everything NOT on the list
# (agent ticks, new jobs) is stopped for the duration of the incident:
# stopping one too many is safer than leaving a burner running.
CRON_KEEP = {"hermes-health", "nightly-backup", "usage-ledger", "worktree-gc",
             "pr-hygiene", "metrics-evening",
             "board-watchdog"}


def quota_hit(line):
    """Dead-quota code in a log line ('1310'/'1113') or empty."""
    m = QUOTA_CODE_RE.search(line) if QUOTA_CODE_RE else None
    if m:
        return m.group(1)
    for i, text in enumerate(QUOTA_TEXTS):
        if text in line:
            return QUOTA_CODES[i] if i < len(QUOTA_CODES) else text
    return ""


def cron_jobs():
    """Parse `hermes cron list`: id, status, name, no-agent."""
    _, out = run([HERMES, "cron", "list"], timeout=60)
    jobs, cur = [], None
    for line in out.splitlines():
        m = re.match(r"^  ([0-9a-f]{8,}) \[(\w+)\]", line)
        if m:
            if cur:
                jobs.append(cur)
            cur = {"id": m.group(1), "status": m.group(2),
                   "name": "", "noagent": False}
            continue
        if cur is None:
            continue
        nm = re.match(r"^    Name:\s+(\S+)", line)
        if nm:
            cur["name"] = nm.group(1)
        if "Mode:" in line and "no-agent" in line:
            cur["noagent"] = True
    if cur:
        jobs.append(cur)
    return jobs


def quota_probe_settings():
    """(base URL, key, model) of the quota probe, or None when it is not set up.

    The probe is opt-in: FACTORY_QUOTA_PROBE_BASE (the provider's
    OpenAI-compatible base URL), FACTORY_QUOTA_PROBE_KEY_VAR (the name of the
    variable in the Hermes .env that holds the API key) and a model
    (FACTORY_QUOTA_PROBE_MODEL, else the first fallback model of the config).
    """
    base = cfg("FACTORY_QUOTA_PROBE_BASE", "")
    key_var = cfg("FACTORY_QUOTA_PROBE_KEY_VAR", "")
    model = cfg("FACTORY_QUOTA_PROBE_MODEL", "")
    if not model:
        try:
            model = ((load_yaml(os.path.join(HOME, "config.yaml"))
                      .get("fallback_providers") or [{}])[0]).get("model")
        except Exception:
            model = None
    if not (base and key_var and model):
        return None
    return base, env_value(key_var), model


def quota_probe():
    """A live call of 5 tokens: 200 means the quota answers.

    A plain ping is not enough: it once passed in the middle of a dead quota
    on the second attempt.
    """
    settings = quota_probe_settings()
    if not settings or not settings[1]:
        return False
    base, key, model = settings
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=json.dumps({"model": model,
                         "messages": [{"role": "user", "content": "ok"}],
                         "max_tokens": 5}).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status == 200
    except Exception:
        return False


def read_incident():
    try:
        return json.load(open(quota_incident_path(), encoding="utf-8"))
    except Exception:
        return None


HOLD = os.path.join(SCRIPTS, "kanban-dispatch-hold.py")
# Owner stop: a holder of the dispatch locks with a meta file owner/reason.
# Automation (quota_recover) NEVER releases it: only the owner, by hand with
# release --force. Health only alerts: when it is older than 24 h or the holder
# disappeared. Once a foreign stop was released without asking, and the
# dispatcher burned empty runs for a whole day.
HOLD_META = os.path.join(HOME, "cache", "kanban-dispatch-hold.meta.json")
HOLD_PID = os.path.join(HOME, "cache", "kanban-dispatch-hold.pid")


def hold_owner_stop():
    """Meta of the owner stop if the holder is alive and the label is his. Else None."""
    try:
        meta = json.load(open(HOLD_META, encoding="utf-8"))
    except Exception:
        return None
    if not meta.get("owner"):
        return None
    try:
        pid = int(open(HOLD_PID).read().strip())
        os.kill(pid, 0)
    except Exception:
        return None
    if meta.get("holder_pid") not in (None, pid):
        return None
    meta["pid"] = pid
    return meta


def quota_stop(reset, code):
    """Targeted stop of the burners: dispatcher + LLM crons. NOT hermes pause.

    `hermes pause` is a global ESTOP: crons, the dispatcher and ALL new
    gateway turns, including the owner's chat and the health cron itself. There
    is no automatic return, the quota may come back earlier than the reset
    time, and nobody would be told. So only the burners are stopped.

    The dispatcher is stopped by a holder of the board dispatch locks
    (kanban-dispatch-hold.py): the gateway tick gets skipped_locked and spawns
    no workers. Verified on a live gateway: with a non-empty ready queue the
    tick reported "0 workers spawned. Last tick held back: skipped_locked=4",
    and after the release the next tick ran freely and spawned at the first
    free slot. The config is NOT touched: kanban.max_in_progress is read by the
    gateway once at start (the dispatcher settings are resolved outside the
    tick loop), and 0 for the dispatcher means "unset" with a memory-based
    default, so editing the config on a live gateway changes nothing. An older
    max_in_progress=0 stop did not work at all.
    """
    subprocess.Popen([sys.executable, HOLD, "start"],
                     start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    paused = []
    for job in cron_jobs():
        if job["name"] in CRON_KEEP or job["status"] != "active":
            continue
        if run([HERMES, "cron", "pause", job["id"]], timeout=60)[0] == 0:
            paused.append({"id": job["id"], "name": job["name"]})
    inc = {"code": code, "reset": reset, "paused": paused,
           "ts": int(time.time())}
    os.makedirs(os.path.dirname(quota_incident_path()), exist_ok=True)
    json.dump(inc, open(quota_incident_path(), "w", encoding="utf-8"),
              ensure_ascii=False)
    return inc


def quota_recover(inc):
    """The quota answered: bring back what was stopped, close the incident, one signal.

    The incident must be removed here, not at the moment of death: otherwise
    the next quota death with the same reset time would pass without a signal.

    First writes the recovery marker (quota-recovered.json): 429 lines older
    than the moment of recovery do not reopen the incident (otherwise two
    false stops came from a line written before the recovery).

    The lock holder dies on SIGTERM (the kernel releases the locks), the next
    gateway tick resumes spawning by itself. Legacy incidents with the old
    prev_mip key are validated: writing 0 back is forbidden (for the dispatcher
    0 means "unset", not a stop), an invalid value removes the key entirely.
    """
    try:
        os.makedirs(os.path.dirname(quota_recovered_path()), exist_ok=True)
        json.dump({"ts": int(time.time()), "code": inc.get("code", "?")},
                  open(quota_recovered_path(), "w", encoding="utf-8"),
                  ensure_ascii=False)
    except OSError:
        pass
    prev = inc.get("prev_mip") if "prev_mip" in inc else None
    if "prev_mip" in inc:
        if isinstance(prev, bool) or not isinstance(prev, int) or prev < 1:
            run([HERMES, "config", "unset", "kanban.max_in_progress"], timeout=60)
        else:
            run([HERMES, "config", "set", "kanban.max_in_progress", str(prev)],
                timeout=60)
    owner_stop = hold_owner_stop()
    if not owner_stop:
        run([sys.executable, HOLD, "release"], timeout=90)
    for job in inc.get("paused", []):
        run([HERMES, "cron", "resume", job["id"]], timeout=60)
    try:
        os.remove(quota_incident_path())
    except OSError:
        pass
    names = ", ".join(j["name"] for j in inc.get("paused", [])) or "-"
    if owner_stop:
        # Automation NEVER releases an owner stop.
        # Crons are resumed: the quota is alive, only the dispatcher stays held.
        note("quota", "the quota is back, but an owner stop (owner=%s, reason=%s, "
             "pid=%s) keeps the dispatcher stopped: auto-recovery did not touch it, "
             "crons resumed" % (owner_stop.get("owner"), owner_stop.get("reason"),
                                owner_stop.get("pid")),
             "release only by the owner by hand: kanban-dispatch-hold.py release --force")
        run([HERMES, "send", "-t", OWNER_CHAT,
             "QUOTA IS BACK (was 429 %s): crons resumed: %s. BUT the card "
             "dispatcher holds an OWNER STOP (owner=%s, reason=%s, since %s): "
             "automation did not release it and will not. The quota incident is closed."
             % (inc.get("code", "?"), names, owner_stop.get("owner"),
                owner_stop.get("reason"),
                time.strftime("%d.%m %H:%M", time.localtime(owner_stop.get("since", 0))))],
            timeout=60)
        return
    run([HERMES, "send", "-t", OWNER_CHAT,
         "QUOTA IS BACK (was 429 %s, waited for reset %s): the card dispatcher "
         "is unblocked (dispatch locks released, spawning returns on the next "
         "tick), crons resumed: %s. The incident is closed."
         % (inc.get("code", "?"), inc.get("reset", "?"), names)],
        timeout=60)


def check_dispatch_owner_hold():
    """Owner stop: alert, but never release.

    Two reasons to shout: the stop has stood for over 24 h (maybe it was
    forgotten) and the stop vanished together with its holder (label present,
    pid dead: the stop is gone).
    """
    try:
        meta = json.load(open(HOLD_META, encoding="utf-8"))
    except Exception:
        return
    if not meta.get("owner"):
        return
    age_h = (time.time() - meta.get("since", 0)) / 3600.0
    pid = meta.get("holder_pid")
    alive = False
    if isinstance(pid, int):
        try:
            os.kill(pid, 0)
            alive = True
        except OSError:
            alive = False
    if not alive:
        note("owner-stop", "label present, holder gone (pid=%s): the stop VANISHED, "
             "the dispatcher is probably spawning again (owner=%s, reason=%s)"
             % (pid, meta.get("owner"), meta.get("reason")),
             "find out why the holder died; do not silently restore an owner stop")
        return
    if age_h > 24:
        note("owner-stop", "standing for %.1f h (owner=%s, reason=%s): over a day, "
             "alert by rule, automation does not release"
             % (age_h, meta.get("owner"), meta.get("reason")),
             "owner: release by hand with release --force or plan the release")


def check_quota_death():
    """429 1310/1113: quota death, not a rate limit. Retries do not cure it.

    Looks at fresh gateway log lines, not only at a ping: a ping once passed in
    the middle of a dead quota (second attempt) and health stayed silent while
    the dispatcher burned dozens of empty coder sessions. While an incident is
    open, every health tick (10 min) makes a 5-token probe: 200 = the quota is
    back (even before the reset time), resume what was stopped and send one
    signal. While not 200: stay silent, the signal has already been sent.

    Lines older than the moment of the last quota recovery are not evidence:
    after a recovery a stale line used to reopen the incident for another 45
    minutes. Window floor = max(now - 45 min, quota_recovered_at()).

    The whole guard is opt-in: without the probe settings a stop could never
    be lifted automatically, so nothing is stopped and one line says why.
    """
    if not quota_probe_settings():
        print("hermes-health: quota guard skipped: set FACTORY_QUOTA_PROBE_BASE, "
              "FACTORY_QUOTA_PROBE_KEY_VAR and FACTORY_QUOTA_PROBE_MODEL "
              "(or a fallback model) to enable it", file=sys.stderr)
        return
    inc = read_incident()
    if inc:
        if quota_probe():
            quota_recover(inc)
            note("quota", "the quota is back: the dispatcher and LLM crons were "
                 "resumed automatically, the owner signal is sent, the incident is closed",
                 "review the cron history for missed runs")
        return
    cutoff = time.time() - 45 * 60
    recovered = quota_recovered_at()
    if recovered:
        cutoff = max(cutoff, recovered)
    ts_re = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
    hit, code = "", ""
    for name in ("gateway.log", "errors.log"):
        try:
            with open(os.path.join(qroot(), "logs", name), encoding="utf-8",
                      errors="replace") as f:
                lines = f.readlines()[-400:]
        except OSError:
            continue
        for line in lines:
            m = ts_re.match(line)
            if not m:
                continue
            try:
                ts = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
            except ValueError:
                continue
            if ts < cutoff:
                continue
            c = quota_hit(line)
            if c:
                hit, code = line, c
    if not hit:
        return
    m = QUOTA_RESET_RE.search(hit)
    reset = m.group(1) if m else "see the reset time in the provider dashboard"
    # write the incident before sending: a send failure must not cause a repeated stop
    inc = quota_stop(reset, code)
    names = ", ".join(j["name"] for j in inc["paused"]) or "-"
    run([HERMES, "send", "-t", OWNER_CHAT,
         "PROVIDER QUOTA: 429 %s (%s), reset %s. Only the burners were stopped: "
         "the card dispatcher (board dispatch locks, ticks without spawning) and "
         "LLM crons: %s. Chat and "
         "health stay alive. Recovery is automatic: a probe every 10 min, one "
         "\"quota is back\" signal. Do not resume anything by hand."
         % (code, hit.strip()[:120], reset, names)], timeout=60)
    note("quota", "429 %s quota death, reset %s: burners stopped, recovery is "
         "automatic by a probe every 10 min" % (code, reset),
         "do not retry by hand; the chat with Hermes works")


def check_provider_alive():
    """A live call. A single failure is not counted as a breakage.

    A provider may answer 429 routinely when parallel requests are exhausted:
    that is a working mode, not a failure. Shout only if the provider did not
    answer twice in a row or returned a configuration error (key, rights,
    nonexistent model) that will not go away by itself.
    """
    fatal = ("HTTP 401", "HTTP 403", "HTTP 404", "is not supported", "Non-retryable", "Limit Exhausted")
    for attempt in (1, 2):
        code, out = run([HERMES, "-z", "ping", "-t", "file", "--ignore-rules"], timeout=180)
        hard = [b for b in fatal if b in out]
        if hard:
            note("provider", "configuration error: %s" % hard[0],
                 "hermes config show | grep -A4 Model")
            return
        if code == 0:
            return
        if attempt == 1:
            time.sleep(30)
    first = next((l for l in out.splitlines() if l.strip()), "")
    note("provider", "no answer on two attempts in a row: %s" % first[:90],
         "hermes config show | grep -A4 Model")


def check_fallback_models():
    """Models of the fallback chain must exist in the provider catalog."""
    cfg_ = load_yaml(os.path.join(HOME, "config.yaml"))
    chain = cfg_.get("fallback_providers") or []
    wanted = [f.get("model") for f in chain if f.get("provider") == "openrouter"]
    aux = (cfg_.get("auxiliary") or {})
    # vision is checked against the OpenRouter catalog only if it lives there.
    # Once vision moved to another provider while the check kept looking for
    # its model in the OpenRouter catalog and printed a false alarm.
    vision = aux.get("vision") or {}
    extras = [aux.get("openrouter_model")]
    if vision.get("provider") == "openrouter":
        extras.append(vision.get("model"))
    for extra in extras:
        if extra:
            wanted.append(extra)
    if not wanted:
        return
    key = env_value("OPENROUTER_API_KEY")
    if not key:
        return
    try:
        req = urllib.request.Request("https://openrouter.ai/api/v1/models",
                                     headers={"Authorization": "Bearer " + key})
        ids = {m["id"] for m in json.load(urllib.request.urlopen(req, timeout=30))["data"]}
    except Exception:
        return
    for model in wanted:
        if model not in ids:
            note("model", "%s is missing from the OpenRouter catalog" % model,
                 "remove it from the fallback chain or replace it with a live one")


def check_openrouter_credit():
    """A model in the catalog and a paid model are different things.

    Workers once failed in batches with HTTP 402 from OpenRouter: the key limit
    was exhausted. The catalog check missed it because the model existed. 402 is
    dangerous because it is NOT retryable: hit from the fallback, the run is
    aborted instead of returning to retries on the primary provider. A dead
    fallback is worse than none.
    """
    cfg_ = load_yaml(os.path.join(HOME, "config.yaml"))
    chain = cfg_.get("fallback_providers") or []
    aux = (cfg_.get("auxiliary") or {})
    used = any(f.get("provider") == "openrouter" for f in chain)
    used = used or (aux.get("vision") or {}).get("provider") == "openrouter"
    if not used:
        return
    key = env_value("OPENROUTER_API_KEY")
    if not key:
        return
    try:
        req = urllib.request.Request("https://openrouter.ai/api/v1/key",
                                     headers={"Authorization": "Bearer " + key})
        d = json.load(urllib.request.urlopen(req, timeout=20)).get("data", {})
        left = d.get("limit_remaining")
    except Exception:
        return
    if left is not None and float(left) < 0.5:
        note("credit", "OpenRouter balance $%.4f, and it is in the chain" % float(left),
             "top up or remove it from the fallback chain: 402 aborts the run instead of retrying")


def check_balance():
    cfg_ = load_yaml(os.path.join(HOME, "config.yaml"))
    chain = cfg_.get("fallback_providers") or []
    primary = (cfg_.get("model") or {}).get("provider")
    if primary != "deepseek" and not any(f.get("provider") == "deepseek" for f in chain):
        return
    key = env_value("DEEPSEEK_API_KEY")
    if not key:
        return
    try:
        req = urllib.request.Request("https://api.deepseek.com/user/balance",
                                     headers={"Authorization": "Bearer " + key})
        info = json.load(urllib.request.urlopen(req, timeout=20))["balance_infos"][0]
        balance = float(info["total_balance"])
    except Exception:
        return
    if balance < 3:
        note("balance", "DeepSeek $%.2f" % balance, "top up at platform.deepseek.com")


def check_memory():
    try:
        fields = {}
        for line in open("/proc/meminfo"):
            key, _, rest = line.partition(":")
            fields[key] = int(rest.split()[0])
        available = fields.get("MemAvailable", 0) // 1024
    except Exception:
        return
    if available < 500:
        note("memory", "%d MB available" % available,
             "lower kanban.max_in_progress or close unneeded language servers")


def boards():
    if not os.path.isdir(BOARDS):
        return []
    return [b for b in sorted(os.listdir(BOARDS))
            if os.path.exists(os.path.join(BOARDS, b, "kanban.db"))]


def check_board(slug):
    db = os.path.join(BOARDS, slug, "kanban.db")
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=10)
    try:
        tasks = {r[0]: {"status": r[1], "title": r[2] or ""} for r in
                 con.execute("select id,status,title from tasks")}
        blockers = {}
        for parent, child in con.execute("select parent_id,child_id from task_links"):
            blockers.setdefault(child, []).append(parent)
        # workers record an explicit "Release condition:" line in a
        # comment; cards that have one are waiting on purpose, not stale.
        waited = {r[0] for r in con.execute("select task_id from task_comments where body like ?",
                                            ("%elease condition%",))}
        recent = [r[0] for r in con.execute(
            "select outcome from task_runs where started_at > ?", (time.time() - 6 * 3600,))]
    except sqlite3.Error as e:
        # an empty/broken database (an archived board, an interrupted copy):
        # this is a finding for the report, not a traceback
        con.close()
        note("board", "%s: the database is not readable (%s)" % (slug, e),
             "check the board directory in kanban/boards")
        return
    finally:
        con.close()

    queued = [i for i in tasks if tasks[i]["status"] in ("todo", "triage")]
    moving = [i for i in tasks if tasks[i]["status"] in ("ready", "running")]
    if queued and not moving:
        note("board", "%s: %d cards queued, none is moving" % (slug, len(queued)),
             "hermes kanban diag")
    stale = [i for i, t in tasks.items()
             if t["status"] == "blocked" and i not in waited and blockers.get(i)
             and all(tasks.get(p, {}).get("status") in ("done", "archived")
                     for p in blockers[i])]
    if stale:
        note("board", "%s: %d cards blocked with finished dependencies (%s)"
             % (slug, len(stale), ", ".join(sorted(stale)[:3])),
             "hermes kanban unblock <id>")
    bad = [o for o in recent if o in ("crashed", "gave_up", "timed_out")]
    if len(recent) >= 5 and len(bad) > 0.6 * len(recent):
        note("board", "%s: %d of %d runs failing in the last 6 hours" % (slug, len(bad), len(recent)),
             "check the environment before looking into cards")


def check_crons():
    code, out = run([HERMES, "cron", "list"], timeout=90)
    for name in ("board-watchdog", "pr-hygiene"):
        if name not in out:
            note("cron", "%s is not created" % name, "hermes cron create ...")
    for block in out.split("Name:")[1:]:
        name = block.split("\n")[0].strip()
        if "error:" in block and name in ("board-watchdog", "pr-hygiene"):
            note("cron", "%s last run ended with an error" % name, "hermes cron runs <id>")


# Statuses that mean "the cron does not work", as opposed to "works".
CRON_BAD_STATUSES = ("error", "blocked_config")


def cron_failure_alerts(paths=None):
    """Red crons of all profiles: streak >= 2 or last_status error/blocked_config.

    Reason: two accepted-lesson crons once silently did not work for a whole
    day. One failed 10 times in a row (a no_agent wrapper without argv), the
    other stood in blocked_config (a failure_deliver preflight), and nobody
    noticed: the blocked_config notification comes once, failure streaks do not
    come at all. The rule: a mechanism is not accepted until its cron has run
    at least once with last_status ok.
    Reads jobs.json directly (root + every profile): `hermes cron list` does
    not show failure_streak and does not see other profiles.
    Returns a list of lines "profile/name: streak, status, time, first line of
    the error".
    """
    # CRON_JOBS_ROOT: an injection point for test fixtures (like
    # ORPHAN_PROC_ROOT for orphans); unset in production, the root is the same.
    root = os.environ.get("CRON_JOBS_ROOT") or HOME
    if paths is None:
        paths = [os.path.join(root, "cron", "jobs.json")]
        proot = os.path.join(root, "profiles")
        if os.path.isdir(proot):
            paths += [os.path.join(proot, p, "cron", "jobs.json")
                      for p in sorted(os.listdir(proot))]
    alerts = []
    main_jobs = os.path.join(root, "cron", "jobs.json")
    for path in paths:
        profile = "MAIN" if path == main_jobs else path.split("/")[-3]
        try:
            jobs = json.load(open(path, encoding="utf-8")).get("jobs", [])
        except (OSError, ValueError):
            continue
        for job in jobs:
            streak = job.get("failure_streak") or 0
            status = (job.get("last_status") or "").strip()
            if streak < 2 and status not in CRON_BAD_STATUSES:
                continue
            lines = (job.get("last_error") or "").strip().splitlines()
            first = next((l.strip() for l in lines if l.strip()), "-")[:100]
            alerts.append("%s/%s: streak=%s last=%s at %s | %s"
                          % (profile, job.get("name") or job.get("id"),
                             streak, status or "-", str(job.get("last_run_at") or "")[:16],
                             first))
    return alerts


def check_cron_failures():
    for line in cron_failure_alerts():
        note("cron-failure", line,
             "hermes cron runs <id>; blocked_config: hermes cron doctor (delivery preflight)")


def check_scripts():
    # Scripts whose loss or breakage stops the factory without any other
    # signal. FACTORY_GUARDED_SCRIPTS lists them, comma-separated.
    for script in cfg_list("FACTORY_GUARDED_SCRIPTS",
                           ("board-watchdog.py", "pr-hygiene.py")):
        path = os.path.join(SCRIPTS, script)
        if not os.path.exists(path):
            note("script", "%s is missing" % script, "restore it from the pack or the session history")
            continue
        if script.endswith(".py"):
            code, out = run([sys.executable, "-c",
                             "import ast,sys;ast.parse(open(sys.argv[1]).read())", path], timeout=30)
            if code != 0:
                note("script", "%s does not parse" % script, out.strip()[:80])


def check_scripts_drift():
    """An edit of scripts/*.py,*.sh that bypassed teach-snapshot.sh.

    The snapshot gate catches drift only at the moment of a snapshot; a cron
    edit, a manual edit or a worker between snapshots is seen by nobody, and a
    RED snapshot is committed anyway. Hermes has no native mechanism for this
    (shell hooks fire on session events, the cron wakeAgent gate decides
    whether to wake an agent, git hooks see only commits).

    Here the digest of scripts/ is compared with the stamp of the last green
    run of run-tests.py --audit (the runner writes the stamp itself, including
    from the teach-snapshot gate). A mismatch triggers a full run; only RED
    reaches the report (file name and reason), green stays silent and refreshes
    the stamp. Each digest is checked and alerted exactly once: a repeated tick
    without changes is quiet and does not rerun the suite. The roots
    (directories and paths) are overridden by env for hermetic tests.
    """
    sdir = os.environ.get("SCRIPTS_DIR", SCRIPTS)
    stamp_path = os.environ.get("SCRIPTS_GREEN_STAMP", os.path.join(
        HOME, "cron", "scripts-tests.green.json"))
    state_path = os.environ.get("SCRIPTS_DRIFT_STATE", os.path.join(
        HOME, "cron", "scripts-drift.state.json"))
    runner = os.environ.get("SCRIPTS_RUN_TESTS",
                            os.path.join(sdir, "run-tests.py"))
    if not os.path.exists(runner):
        return
    digest = scripts_digest.scripts_digest(sdir)
    try:
        stamp = json.load(open(stamp_path))
    except Exception:
        stamp = {}
    if stamp.get("digest") == digest:
        return                      # no drift: quiet tick
    try:
        state = json.load(open(state_path))
    except Exception:
        state = {}
    if state.get("digest") == digest:
        return                      # this digest was already checked and alerted
    code, out = run([sys.executable, runner, "--audit"], timeout=1800)
    red = [l.strip() for l in (out or "").splitlines()
           if l.startswith("FAIL ") or l.startswith("RED:")]
    if code == 0 and not red:
        # A green run refreshed the stamp itself; if it could not (no
        # permission), the watchdog writes it: silence here and in the report.
        if stamp.get("digest") != digest:
            try:
                os.makedirs(os.path.dirname(stamp_path), exist_ok=True)
                json.dump({"digest": digest, "ts": time.time()},
                          open(stamp_path, "w"))
            except OSError:
                pass
    else:
        note("scripts-drift[%s]" % digest[:12],
             "scripts/ changed without a green run (digest %s), "
             "run-tests.py --audit RED: %s"
             % (digest[:12], "; ".join(red[:5]) or "summary unavailable"),
             "an edit bypassed teach-snapshot: fix the tests, then take a "
             "snapshot with scripts/teach-snapshot.sh \"what changed\"")
    try:
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        json.dump({"digest": digest,
                   "verdict": "red" if (code or red) else "green",
                   "ts": time.time()}, open(state_path, "w"))
    except OSError:
        pass


def used_provider_ids():
    """Provider ids from model/fallback/auxiliary of at least one profile.

    An "invalid API key" alert makes sense only for providers the system
    actually works with. doctor checks every key in .env, and the keys of two
    providers no longer in use dripped into the owner's chat for a week after
    everything moved to another provider.
    """
    ids = set()
    import glob as _glob
    paths = [os.path.join(HOME, "config.yaml")] + \
        sorted(_glob.glob(os.path.join(HOME, "profiles", "*", "config.yaml")))
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        for m in re.finditer(r"^\s{0,8}provider:\s*[\"']?([A-Za-z0-9_.-]+)", text, re.M):
            pid = m.group(1)
            if pid:
                ids.add(pid.lower())
    return ids


def doctor_bad_lines(out, used):
    """Failure lines of the doctor output that are fit for an owner alert.

    "invalid API key" is filtered by the providers in use: the key of a dead
    provider that is not attached anywhere is no alarm for the owner. The
    provider name in the doctor output may differ from the config id in
    punctuation ("Z.AI" versus "zai"), so the comparison uses the alphanumeric
    trace.
    """
    bad = []
    for line in (out or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("\u2717", "x ", "ERROR", "FAIL")) or " FAIL" in stripped:
            if "invalid API key" in stripped:
                flat = re.sub(r"[^a-z0-9]", "", stripped.lower())
                if not any(pid in flat for pid in used):
                    continue
            bad.append(stripped[:150])
    return bad


def check_doctor():
    """The built-in installation diagnostics. No point retelling them in our own
    words: take the output as is and show only the failure lines."""
    code, out = run([HERMES, "doctor"], timeout=300)
    for line in doctor_bad_lines(out, used_provider_ids())[:6]:
        note("doctor", line, "hermes doctor")


def check_orphan_stands():
    """Java/node/chromium processes older than 2 hours without a run-bounded registry entry.

    Reason: the stands of a session killed by an iteration limit once lived on
    for another 25 minutes and shared ports with a parallel worker (see
    run-bounded.sh). The watchdog runs every 10 minutes, so an orphan older than
    2 hours is caught without an agent.
    The run-bounded registry exempts live entries (a stand with an owner and a
    deadline is not an orphan); dead and expired entries are pruned from here.
    Kills with SIGTERM by default; ORPHAN_DRY_RUN=1 switches to report only.
    """
    proc_root = os.environ.get("ORPHAN_PROC_ROOT", "/proc")
    registry = os.environ.get(
        "ORPHAN_REGISTRY",
        os.path.join(HOME, "cache", "bounded-procs.jsonl"))
    min_age = int(os.environ.get("ORPHAN_MIN_AGE_SEC", "7200"))
    # Killing is on by default: the production hermes-health cron runs the
    # script without an environment, orphans must die without a manual run.
    # ORPHAN_DRY_RUN=1 brings back the "report only" mode (diagnostics).
    dry = os.environ.get("ORPHAN_DRY_RUN", "") == "1"
    apply_kill = os.environ.get("ORPHAN_APPLY", "") == "1" or not dry
    exempt = set()
    lines = []
    try:
        lines = [json.loads(l) for l in open(registry) if l.strip()]
    except Exception:
        pass
    now = time.time()
    kept = []
    for e in lines:
        pid = e.get("pid")
        cmd = e.get("cmd", "")
        alive = os.path.exists(os.path.join(proc_root, str(pid)))
        if alive and now < e.get("deadline", 0):
            exempt.add(pid)
            kept.append(e)
            continue
        if not alive:                       # the process died, the entry is not needed
            continue
        kept.append(e)                      # alive but expired: not exempt
    if lines != kept:
        try:
            os.makedirs(os.path.dirname(registry), exist_ok=True)
            with open(registry, "w") as fh:
                for e in kept:
                    fh.write(json.dumps(e) + "\n")
        except Exception:
            pass
    try:
        uptime = float(open(os.path.join(proc_root, "uptime")).read().split()[0])
    except Exception:
        return
    hz = os.sysconf("SC_CLK_TCK")
    cands = {}                            # pid -> (exe, cmd, age, ppid)
    for ent in os.listdir(proc_root):
        if not ent.isdigit():
            continue
        pid = int(ent)
        try:
            exe = os.path.basename(os.readlink(
                os.path.join(proc_root, ent, "exe")))
        except Exception:
            continue
        if exe not in ("java", "node", "chromium-browser", "chrome",
                       "headless_shell", "chromium"):
            continue
        try:
            with open(os.path.join(proc_root, ent, "cmdline"), "rb") as fh:
                cmd = fh.read().replace(b"\0", b" ").decode("utf8", "replace")
        except Exception:
            continue
        try:
            stat = open(os.path.join(proc_root, ent, "stat")).read()
            after = stat[stat.rfind(")") + 2:].split()
            starttime = float(after[19])
            ppid = int(after[1])
        except Exception:
            continue
        age = uptime - starttime / hz
        cands[pid] = (exe, cmd, age, ppid)

    # Hermes' own units: the gateway, kanban workers and owner sessions live in
    # them (the cgroup of an interactive session is app.slice/hermes-gateway.service).
    # Orphan stands from those MUST be caught, so these units are excluded from
    # the service exemption below.
    # A shared browser service (for example a CDP Chromium for the browser
    # toolset, systemd-managed with Restart=on-failure) does NOT belong here:
    # it was once listed as a Hermes unit, and the sweep killed the shared
    # browser every 2 hours as an orphan, breaking the live CDP sessions of
    # other workers. A worker's own chromium runs in the gateway's cgroup, not
    # in such a service, so the hunt loses nothing by leaving it alone.
    hermes_units = {"hermes-gateway.service", "hermes-dashboard.service"}

    def service_unit(pid):
        """Name of the systemd unit if the process lives in its own *.service
        in app.slice (for example demo-app.service). None: not a service."""
        try:
            cg = open(os.path.join(proc_root, str(pid), "cgroup")).read()
        except Exception:
            return None
        path = cg.strip().splitlines()[-1].split("::", 1)[-1]
        if "app.slice" not in path:
            return None
        name = path.rstrip("/").rsplit("/", 1)[-1]
        return name if name.endswith(".service") else None

    def whitelisted(pid):
        unit = service_unit(pid)
        if unit and unit not in hermes_units:
            return True                     # a legitimate foreign service (systemd
                                            # would bring it back anyway)
        exe, cmd, _age, _ppid = cands[pid]
        if ".hermes" in cmd or "hermes-agent" in cmd:
            return True                     # gateway, tools, Hermes installs
        if exe == "node" and ("_npx" in cmd or "-mcp" in cmd):
            return True                     # Hermes native MCP servers
        if exe in ("chromium-browser", "chrome", "chromium", "headless_shell") \
                and "--headless" not in cmd:
            # leave every non-headless chromium alone: it is the user's
            # graphical browser or one attached through /browser connect (its
            # children are non-headless in cmdline too); our orphans are
            # headless screenshot tools and java stands
            return True
        return False

    exempt.update(p for p in cands if whitelisted(p))
    # exempt extends to children: a live browser's renderer is not an orphan
    for _ in range(2):
        for pid, (_exe, _cmd, _age, ppid) in cands.items():
            if ppid in exempt:
                exempt.add(pid)

    for pid in sorted(cands):
        exe, cmd, age, _ppid = cands[pid]
        if age < min_age or pid in exempt:
            continue
        action = "report only"
        if apply_kill:
            import signal
            try:
                os.kill(pid, signal.SIGTERM)
                action = "SIGTERM sent"
            except Exception as exc:
                action = "SIGTERM failed: %s" % exc
        note("orphan-stand",
             "pid %d (%s), age %d min, nobody owns it; %s"
             % (pid, cmd.strip()[:90], age // 60, action),
             "this is a stand of a forgotten session; start stands through "
             "run-bounded.sh --owner <card|session>")


def main():
    check_doctor()
    check_services()
    check_provider_config()
    check_fallback_models()
    check_openrouter_credit()
    check_balance()
    check_memory()
    check_scripts()
    check_scripts_drift()
    check_crons()
    check_cron_failures()
    for slug in boards():
        check_board(slug)
    check_provider_alive()
    check_quota_death()
    check_dispatch_owner_hold()
    check_orphan_stands()

    if not problems:
        return 0
    state = {}
    if os.path.exists(STATE):
        try:
            state = json.load(open(STATE))
        except Exception:
            state = {}
    now = time.time()
    fresh = []
    for check, detail, fix in problems:
        key = "%s|%s" % (check, re.sub(r"\d+", "N", detail))
        if now - state.get(key, 0) < REPEAT_AFTER:
            continue
        state[key] = now
        fresh.append((check, detail, fix))
    if not fresh:
        # Empty output and code 0 is the documented quiet tick for cron
        # --no-agent. Returning 1 marked the job failed on EVERY run, so a real
        # failure would have been indistinguishable from the usual silence.
        return 0
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump(state, open(STATE, "w"))
    print("HERMES HEALTH: %d problem(s)" % len(fresh))
    for check, detail, fix in fresh:
        print("  [%s] %s" % (check, detail))
        print("        -> %s" % fix)
    # Found problems are NOT a script failure: the signal is the printed text,
    # which cron delivers as is. A non-zero code here would mean "the check
    # broke", while it ran fine.
    return 0


if __name__ == "__main__":
    sys.exit(main())
