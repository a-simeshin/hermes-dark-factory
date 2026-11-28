#!/usr/bin/env python3
"""factory-doctor.py must reject a project conventions file that breaks the
pack contract.

Cases:
  complete : every slot filled, a known override id: no problems, exit 0;
  missing  : one slot heading removed: a "missing slot" problem, exit 1;
  empty    : a slot with only its TODO placeholder: an "empty slot" problem;
  unknown  : an override of an id the pack does not tag: a problem, exit 1.
Red before green: the missing and unknown cases also run against a copy of
the script with the check they cover mutated away, and must fail there.

Sandbox: a fake pack root and conventions files in a temp dir, a temp
HERMES_HOME. No network, no live home.
"""
import importlib.util
import os
import shutil
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "factory-doctor.py")
TMP = tempfile.mkdtemp(prefix="factory-doctor-")
os.environ["HERMES_HOME"] = os.path.join(TMP, "hermes")


def load(src, name):
    path = os.path.join(TMP, name + ".py")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(src)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


with open(SCRIPT, encoding="utf-8") as fh:
    SRC = fh.read()
doctor = load(SRC, "doctor_real")

PACK = os.path.join(TMP, "pack")
os.makedirs(os.path.join(PACK, "skills", "demo"))
with open(os.path.join(PACK, "skills", "demo", "SKILL.md"), "w") as fh:
    fh.write("- **[PR-01]** the visual delta lives in the issue.\n")


def conventions(name, skip=None, empty=None, override="PR-01"):
    parts = ["---\nname: project-conventions\n---\n", "# Project conventions\n"]
    for slot in doctor.SLOTS:
        if slot == skip:
            continue
        body = "<!-- what goes here -->\nTODO: e.g. something\n"
        if slot != empty:
            body += ("overrides %s because test, 2026-01-01\n" % override
                     if slot == "Pack overrides" else "a real value\n")
        parts.append("## %s\n\n%s\n" % (slot, body))
    path = os.path.join(TMP, name + ".md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("".join(parts))
    return path


def run(mod, path):
    problems, _ = mod.check(path, PACK)
    return problems


try:
    complete = conventions("complete")
    assert run(doctor, complete) == [], run(doctor, complete)
    assert doctor.main(["x", complete, PACK]) == 0

    missing = conventions("missing", skip="Release mechanics")
    assert run(doctor, missing) == ["missing slot: ## Release mechanics"]
    assert doctor.main(["x", missing, PACK]) == 1

    empty = conventions("empty", empty="Persona addendum")
    assert run(doctor, empty) == ["empty slot (placeholder only): ## Persona addendum"]

    unknown = conventions("unknown", override="ZZ-99")
    assert run(doctor, unknown) == ["override of unknown rule id: ZZ-99"]
    assert doctor.main(["x", unknown, PACK]) == 1

    # Mutants: each check removed must let its bad file through.
    m1 = load(SRC.replace('problems.append("missing slot', 'pass  # ("missing slot'),
              "doctor_m1")
    assert run(m1, missing) == [], "mutant without the slot check still fails"
    m2 = load(SRC.replace("if rid not in known:", "if False:"), "doctor_m2")
    assert run(m2, unknown) == [], "mutant without the id check still fails"
    print("test_factory_doctor: 6 cases green")
finally:
    shutil.rmtree(TMP, ignore_errors=True)
