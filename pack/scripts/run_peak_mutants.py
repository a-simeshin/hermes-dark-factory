#!/usr/bin/env python3
"""Mutant run of test_peak_pause.py: every mutant must turn the test red.

The pristine module must stay green first; then each hand-written mutant of
kanban-dispatch-peak-pause.py is substituted through the PEAK_MODULE env that
the test reads. The test and the script are taken from this directory.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
SRC = HERE / "kanban-dispatch-peak-pause.py"
HOLD_SRC = HERE / "kanban-dispatch-hold.py"
CONFIG_SRC = HERE / "factory_config.py"
TEST = HERE / "test_peak_pause.py"
SCRATCH = Path(tempfile.mkdtemp(prefix="peak-mutants-"))
BASE = SRC.read_text(encoding="utf-8")

MUTANTS = {
    "M1-window-inverted": (
        "return PEAK_START <= hm < PEAK_END",
        "return not (PEAK_START <= hm < PEAK_END)"),
    "M2-weekdays-removed": (
        'if dt.weekday() not in PEAK_DAYS:\n        return False\n    ',
        ""),
    "M3-lifts-foreign-stop": (
        'if not (meta and meta.get("owner") == PEAK_OWNER):',
        "if False:"),
    "M4-overwrites-owner": (
        'if meta and own_meta and meta.get("owner"):',
        "if False:"),
    "M5-annotate-disabled": (
        'code = _hold("annotate", "--owner", PEAK_OWNER, "--reason", PEAK_REASON)',
        "code = 1"),
}


def run_case(name: str, source: str | None, expect_green: bool = False):
    case_dir = SCRATCH / f"peakmut-{name}"
    if case_dir.exists():
        shutil.rmtree(case_dir)
    (case_dir / "scripts").mkdir(parents=True)
    # The module under test resolves its siblings (the hold script, the
    # settings helper) relative to its own directory.
    shutil.copy(HOLD_SRC, case_dir / "scripts" / "kanban-dispatch-hold.py")
    shutil.copy(CONFIG_SRC, case_dir / "scripts" / "factory_config.py")
    (case_dir / "logs").mkdir()
    (case_dir / "cache").mkdir()
    module = case_dir / "scripts" / "peak_pause.py"
    module.write_text(source if source is not None else BASE, encoding="utf-8")
    env = dict(os.environ, HERMES_HOME=str(case_dir), PEAK_MODULE=str(module))
    r = subprocess.run([sys.executable, str(TEST)], env=env,
                       capture_output=True, text=True, timeout=600)
    green = r.returncode == 0
    killed = not green
    tag = "GREEN" if green else "RED"
    print(f"{name}: rc={r.returncode} {tag}")
    tail = [l for l in r.stdout.splitlines() if l.startswith("  [FAIL]") or "RED" in l]
    for l in tail[:6]:
        print(f"    {l}")
    if r.returncode not in (0, 1):
        print(f"    STDERR: {r.stderr[-500:]}")
    return green, killed


def main() -> int:
    try:
        rc_pristine, _ = run_case("pristine", None, expect_green=True)
        if not rc_pristine:
            print("PRISTINE MODULE IS RED: stop")
            return 1
        results = {name: run_case(name, BASE.replace(old, new), expect_green=False)[1]
                   for name, (old, new) in MUTANTS.items()}
        for name, (old, _new) in MUTANTS.items():
            if BASE.count(old) != 1:
                print(f"SETUP ERROR: {name}: substituted string count={BASE.count(old)}")
                results[name] = False
        dead = sum(results.values())
        print(f"\ntotal: mutants killed {dead}/{len(MUTANTS)}")
        return 0 if dead == len(MUTANTS) else 1
    finally:
        shutil.rmtree(SCRATCH, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
