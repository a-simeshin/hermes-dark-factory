#!/usr/bin/env python3
"""Tests of the "invalid API key" filter in check_doctor of hermes-health.py.

Reason: hermes doctor checks every key in .env, and check_doctor forwarded
every failure line to the owner. The keys of providers that were no longer
used anywhere kept dripping alerts for a week. Rule: a key alert only for
providers named in model/fallback/auxiliary of at least one profile.

Hermetic: the providers in use are read from a temp HOME fixture, the live
Hermes home is never opened.
"""
import atexit
import importlib.util
import os
import shutil
import tempfile
import time
import unittest

_HOME = tempfile.mkdtemp(prefix="hh-home-")
atexit.register(shutil.rmtree, _HOME, ignore_errors=True)
os.environ["HERMES_HOME"] = _HOME
os.environ.setdefault("FACTORY_OWNER_CHAT", "telegram:000000000")

REAL = os.environ.get("HERMES_HEALTH_SCRIPT",
                      os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "hermes-health.py"))


def load_health(home):
    spec = importlib.util.spec_from_file_location(
        "hh_dockey_%d" % int(time.time()), REAL)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.HOME = home
    return mod


def fixture_home():
    root = tempfile.mkdtemp(prefix="hh-dockey-")
    atexit.register(shutil.rmtree, root, ignore_errors=True)
    os.makedirs(os.path.join(root, "profiles", "coder"))
    with open(os.path.join(root, "config.yaml"), "w") as f:
        f.write("model:\n  provider: zai\n"
                "fallback_providers:\n  - provider: zai\n")
    with open(os.path.join(root, "profiles", "coder", "config.yaml"), "w") as f:
        f.write("model:\n  provider: zai\n")
    return root


DEAD = [
    "✗ Alibaba/DashScope    (invalid API key)",
    "✗ OpenCode Zen         (invalid API key)",
    "✗ Alibaba Cloud DashScope (China) (invalid API key)",
]
LIVE_KEY = "✗ Z.AI (invalid API key)"


class DoctorKeyFilter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hh = load_health(fixture_home())

    def test_used_providers_from_profiles(self):
        self.assertEqual(self.hh.used_provider_ids(), {"zai"})

    def test_dead_providers_filtered(self):
        self.assertEqual(self.hh.doctor_bad_lines("\n".join(DEAD), {"zai"}), [])

    def test_used_provider_passes_with_dot_in_name(self):
        self.assertEqual(self.hh.doctor_bad_lines(LIVE_KEY, {"zai"}), [LIVE_KEY])

    def test_other_failures_untouched(self):
        other = ["✗ npm audit: 4 vulnerabilities", "FAIL unit tests"]
        self.assertEqual(self.hh.doctor_bad_lines("\n".join(other), {"zai"}), other)

    def test_mixed_output_keeps_only_live(self):
        mixed = DEAD + [LIVE_KEY, "✗ git hooks missing"]
        self.assertEqual(self.hh.doctor_bad_lines("\n".join(mixed), {"zai"}),
                         [LIVE_KEY, "✗ git hooks missing"])


if __name__ == "__main__":
    unittest.main()
