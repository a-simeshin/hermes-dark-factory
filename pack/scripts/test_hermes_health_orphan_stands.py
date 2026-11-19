#!/usr/bin/env python3
"""Tests for check_orphan_stands in hermes-health.py (java/node/chromium orphans).

The watchdog must: find an old unregistered stand, leave young and
allowlisted (Hermes-owned) processes alone, exempt a live run-bounded registry
entry, prune dead entries, kill when ORPHAN_APPLY=1.
Mutants check that every protection really works.
"""
import atexit
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

# The script under test never touches the real Hermes home: a temp one is used.
_HOME = tempfile.mkdtemp(prefix="hh-home-")
atexit.register(shutil.rmtree, _HOME, ignore_errors=True)
os.environ["HERMES_HOME"] = _HOME
os.environ.setdefault("FACTORY_OWNER_CHAT", "telegram:000000000")

REAL = os.environ.get("HERMES_HEALTH_SCRIPT",
                      os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "hermes-health.py"))
HZ = os.sysconf("SC_CLK_TCK")


def load_health(path):
    spec = importlib.util.spec_from_file_location("hh_test_%d" % int(time.time()), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeProc:
    def __init__(self, uptime=1_000_000.0):
        self.root = tempfile.mkdtemp(prefix="fakeproc-")
        open(os.path.join(self.root, "uptime"), "w").write("%f 100000.0\n" % uptime)
        self.uptime = uptime

    def add(self, pid, exe, cmd, age_sec, state="S", cg=None):
        d = os.path.join(self.root, str(pid))
        os.makedirs(d)
        os.symlink("/fakebin/%s" % exe, os.path.join(d, "exe"))
        open(os.path.join(d, "cmdline"), "wb").write(cmd.encode() + b"\0")
        starttime = (self.uptime - age_sec) * HZ
        # fields after (comm): index 19 = starttime (field 22 of stat(5))
        rest = ["S", "1", "1", "1", "1", "1", "1", "1", "1", "1",
                "1", "1", "1", "1", "1", "1", "1", "1", "1",
                str(starttime)] + ["0"] * 20
        open(os.path.join(d, "stat"), "w").write(
            "%d (%s) %s\n" % (pid, exe, " ".join(rest)))
        if cg:
            open(os.path.join(d, "cgroup"), "w").write("0::%s\n" % cg)

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


def run_check(proc, registry_path=None, env_extra=None):
    mod = load_health(REAL)
    mod.problems.clear()
    env = dict(os.environ)
    env["ORPHAN_PROC_ROOT"] = proc.root
    if registry_path:
        env["ORPHAN_REGISTRY"] = registry_path
    env.update(env_extra or {})
    old = dict(os.environ)
    os.environ.clear(); os.environ.update(env)
    try:
        mod.check_orphan_stands()
    finally:
        os.environ.clear(); os.environ.update(old)
    return list(mod.problems)


class TestOrphanStands(unittest.TestCase):
    def setUp(self):
        self.proc = FakeProc()
        self.tmp = tempfile.mkdtemp(prefix="reg-")

    def tearDown(self):
        self.proc.cleanup()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_t1_old_unregistered_java_reported(self):
        self.proc.add(63145, "java", "java -Xmx512m -jar demo.jar --server.port=8199",
                      age_sec=3 * 3600)
        probs = run_check(self.proc, env_extra={"ORPHAN_DRY_RUN": "1"})
        self.assertEqual(len(probs), 1, "an old java without a registry entry must be reported")
        self.assertIn("63145", probs[0][1])
        self.assertIn("report only", probs[0][1], "dry-run only reports")

    def test_t11_kill_is_default(self):
        # kill by default: without any env the watchdog tries SIGTERM
        # (the fake pid is dead, so the attempt is recorded, not the success)
        self.proc.add(63146, "java", "java -jar orphan.jar", age_sec=3 * 3600)
        probs = run_check(self.proc)
        self.assertEqual(len(probs), 1)
        self.assertIn("SIGTERM", probs[0][1], "without env the watchdog must try to kill")

    def test_t2_hermes_owned_node_not_reported(self):
        self.proc.add(111, "node", "node /srv/hermes/.hermes/hermes-agent/gateway.js",
                      age_sec=10 * 3600)
        probs = run_check(self.proc)
        self.assertEqual(probs, [], "Hermes processes are not orphans")

    def test_t3_registered_with_deadline_exempt(self):
        # age ABOVE the threshold (3h): only a live registry entry saves it
        # (a ttl can exceed 2 hours), otherwise the test does not isolate exempt
        self.proc.add(222, "java", "java -jar stand.jar --server.port=8191",
                      age_sec=3 * 3600)
        reg = os.path.join(self.tmp, "reg.jsonl")
        open(reg, "w").write(json.dumps(
            {"pid": 222, "owner": "card:t_x", "started": time.time() - 3 * 3600,
             "deadline": time.time() + 3600, "cmd": "java -jar stand.jar"}) + "\n")
        probs = run_check(self.proc, reg)
        self.assertEqual(probs, [], "a live registry entry exempts the stand")

    def test_t4_dead_registry_entry_pruned(self):
        reg = os.path.join(self.tmp, "reg.jsonl")
        open(reg, "w").write(json.dumps(
            {"pid": 999999, "owner": "card:t_y", "started": 1,
             "deadline": 2, "cmd": "java"}) + "\n")
        run_check(self.proc, reg)
        left = open(reg).read().strip()
        self.assertEqual(left, "", "an entry with a dead pid must be pruned")

    def test_t5_young_process_ignored(self):
        self.proc.add(333, "java", "java -jar young.jar", age_sec=600)
        probs = run_check(self.proc)
        self.assertEqual(probs, [], "younger than the threshold: not an orphan")

    def test_t9_foreign_systemd_service_not_reported(self):
        # demo-app.service: a legitimate service, systemd would bring it back
        self.proc.add(700, "java", "java -jar demo-app.jar --server.port=8080",
                      age_sec=3 * 3600,
                      cg="/user.slice/user-1001.slice/user@1001.service/app.slice/demo-app.service")
        probs = run_check(self.proc)
        self.assertEqual(probs, [], "a foreign *.service in app.slice is not an orphan")

    def test_t10_hermes_unit_still_hunted(self):
        # a stand started from a session/worker: the gateway cgroup, MUST be hunted
        self.proc.add(701, "java", "java -jar stand.jar --server.port=8191",
                      age_sec=3 * 3600,
                      cg="/user.slice/user-1001.slice/user@1001.service/app.slice/hermes-gateway.service")
        probs = run_check(self.proc)
        self.assertEqual(len(probs), 1, "a stand in the hermes-gateway cgroup is not exempt")
        self.assertIn("701", probs[0][1])

    def test_t12_shared_browser_service_not_reported(self):
        # a shared CDP browser in its own systemd unit is not an abandoned
        # stand; the sweep must not kill it at the age of 2 hours
        self.proc.add(800, "chromium-browser",
                      "chromium-browser --headless=new --no-sandbox "
                      "--remote-debugging-port=9222 --user-data-dir=chromium-cdp",
                      age_sec=3 * 3600,
                      cg="/user.slice/user-1001.slice/user@1001.service/app.slice/"
                         "chromium-cdp.service")
        probs = run_check(self.proc)
        self.assertEqual(probs, [], "a shared browser service is not an orphan")

    def test_t6_apply_kills_real_process(self):
        victim = subprocess.Popen(["sleep", "300"])
        self.proc.add(victim.pid, "node", "node orphan-server.js",
                      age_sec=5 * 3600)
        self.addCleanup(lambda: victim.poll() or victim.kill())
        probs = run_check(self.proc, env_extra={"ORPHAN_APPLY": "1"})
        self.assertEqual(len(probs), 1)
        self.assertIn("SIGTERM sent", probs[0][1])
        victim.wait(timeout=10)
        self.assertNotEqual(victim.returncode, 0, "the victim must die from TERM")

    def test_t7_headless_chromium_orphan_but_desktop_browser_safe(self):
        # the user's non-headless browser and its renderer: not orphans;
        # an old headless screenshot tool: an orphan
        self.proc.add(500, "chromium-browser",
                      "chromium-browser --disable-features=LensOverlay",
                      age_sec=20 * 3600)
        self.proc.add(501, "chromium-browser",
                      "chromium-browser --type=renderer --crashpad-handler-pid=500",
                      age_sec=20 * 3600)
        self.proc.add(502, "chromium-browser",
                      "chromium-browser --headless=new --remote-debugging-port=9222",
                      age_sec=5 * 3600)
        probs = run_check(self.proc)
        self.assertEqual(len(probs), 1, "only the headless orphan is in the problems")
        self.assertIn("502", probs[0][1])


class TestMutants(unittest.TestCase):
    """Every mutant must turn at least one test above red."""

    def mutant(self, old, new, name):
        src = open(REAL).read()
        self.assertIn(old, src, "mutant pattern %s not found" % name)
        d = tempfile.mkdtemp(prefix="mut-")
        path = os.path.join(d, "hermes-health.py")
        open(path, "w").write(src.replace(old, new, 1))
        # the mutant lives outside the pack directory: it needs factory_config
        for _dep in ("factory_config.py", "scripts_digest.py"):
            shutil.copy(os.path.join(os.path.dirname(REAL), _dep), d)
        return path

    def run_with(self, script, test_name):
        r = subprocess.run(
            [sys.executable, os.path.abspath(__file__), test_name],
            env=dict(os.environ, HERMES_HEALTH_SCRIPT=script),
            capture_output=True, text=True, timeout=120)
        return r

    def test_m1_whitelist_removed(self):
        script = self.mutant('if ".hermes" in cmd or "hermes-agent" in cmd:',
                             'if False:', "whitelist")
        r = self.run_with(script, "TestOrphanStands.test_t2_hermes_owned_node_not_reported")
        self.assertNotEqual(r.returncode, 0, "the whitelist mutant must be red")

    def test_m2_registry_exempt_removed(self):
        script = self.mutant("if age < min_age or pid in exempt:",
                             "if age < min_age:", "registry-exempt")
        r = self.run_with(script, "TestOrphanStands.test_t3_registered_with_deadline_exempt")
        self.assertNotEqual(r.returncode, 0, "the exempt mutant must be red")

    def test_m3_age_threshold_never(self):
        script = self.mutant('min_age = int(os.environ.get("ORPHAN_MIN_AGE_SEC", "7200"))',
                             'min_age = 10**9', "age")
        r = self.run_with(script, "TestOrphanStands.test_t1_old_unregistered_java_reported")
        self.assertNotEqual(r.returncode, 0, "the age threshold mutant must be red")

    def test_m7_kill_optin_again(self):
        script = self.mutant(
            'apply_kill = os.environ.get("ORPHAN_APPLY", "") == "1" or not dry',
            'apply_kill = os.environ.get("ORPHAN_APPLY", "") == "1"', "kill-default")
        r = self.run_with(script, "TestOrphanStands.test_t11_kill_is_default")
        self.assertNotEqual(r.returncode, 0, "the kill rollback mutant must be red")

    def test_m5_service_exempt_removed(self):
        script = self.mutant('if unit and unit not in hermes_units:',
                             'if False:', "service-exempt")
        r = self.run_with(script, "TestOrphanStands.test_t9_foreign_systemd_service_not_reported")
        self.assertNotEqual(r.returncode, 0, "the service exemption mutant must be red")

    def test_m6_hermes_units_leak_into_exempt(self):
        script = self.mutant('if unit and unit not in hermes_units:',
                             'if unit:', "hermes-units")
        r = self.run_with(script, "TestOrphanStands.test_t10_hermes_unit_still_hunted")
        self.assertNotEqual(r.returncode, 0, "the hermes units mutant must be red")

    def test_m4_chromium_whitelist_removed(self):
        script = self.mutant(
            'if exe in ("chromium-browser", "chrome", "chromium", "headless_shell") \\\n                and "--headless" not in cmd:',
            'if exe == "never-matches" and "--headless" not in cmd:',
            "chromium-whitelist")
        r = self.run_with(script,
                          "TestOrphanStands.test_t7_headless_chromium_orphan_but_desktop_browser_safe")
        self.assertNotEqual(r.returncode, 0, "the chromium whitelist mutant must be red")


if __name__ == "__main__":
    unittest.main(verbosity=2)
