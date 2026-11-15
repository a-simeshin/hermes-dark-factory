#!/usr/bin/env python3
"""Tests for the deterministic PR risk class (pr-risk.py).

The pure functions of the classifier and the mini YAML parser are covered by
synthetic diffs; gh is never called. Mutants (all must fail):
- M1 a yaml copy without the ui_state_plus_view rule -> test_ui_state_plus_view_rule
  fails (a one-layer diff becomes indistinguishable from a two-layer one);
- M2 a yaml copy with max_lines 5000 -> test_prod_churn_over_limit fails
  (a 490-line diff stops being critical);
- M3 a yaml copy with package.json removed from ci_build ->
  test_ci_build_package_json fails (a build dependency stops being critical);
- M4 a script copy where the test category is not excluded from churn ->
  test_churn_ignores_tests_and_docs fails (tests start to count as
  production lines);
- M5 a yaml copy with the core/recorder glob removed ->
  test_recorder_is_traffic_path fails;
- M6 a script copy where known.get(...) is replaced with None (the
  known/new distinction is knocked out) ->
  test_shadow_known_misses_are_not_new fails (the known exceptions become
  new misses and the shadow run is red forever).
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
# The script reads the repository from FACTORY_REPO and the Hermes home at
# import time: give it a fixture repository and an empty temp home so the test
# never sees a real installation.
os.environ["FACTORY_REPO"] = "acme/widget"
HH = tempfile.mkdtemp(prefix="hermes-home-")
os.environ["HERMES_HOME"] = HH
SCRIPT = os.environ.get("PR_RISK_SCRIPT") or os.path.join(HERE, "pr-risk.py")
YAML_PATH = os.environ.get("PR_RISK_YAML") or os.path.join(HERE, "pr-risk.yaml")

spec = importlib.util.spec_from_file_location("pr_risk", SCRIPT)
if spec is None or spec.loader is None:
    print("SKIP: cannot load pr-risk.py")
    sys.exit(0)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
CFG = mod.load_yaml_subset(YAML_PATH)


def f(name, add=1, dele=0, status="modified"):
    return {"filename": name, "additions": add, "deletions": dele,
            "status": status}


def ui(path):
    return "widget-ui/web-client/" + path


# ------------------------------------------------------------ mini YAML parser
def test_yaml_parses_all_rules():
    assert CFG["version"] == 1
    for rule in mod.RULE_ORDER:
        assert rule in CFG["critical_rules"], rule
    assert CFG["critical_rules"]["prod_churn"]["max_lines"] == 400
    assert CFG["critical_rules"]["test_deletion"]["partial_min_deletions"] == 20
    assert isinstance(CFG["trivial_files"], list) and CFG["trivial_files"]
    assert set(CFG["categories"]) == {"test", "docs", "lock", "build"}
    print("OK test_yaml_parses_all_rules")


def test_yaml_rejects_unknown_construct():
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write("version: 1\n  bad indent: x\n")
        path = fh.name
    try:
        try:
            mod.load_yaml_subset(path)
            raise AssertionError("a bad indent must be an error")
        except ValueError:
            pass
    finally:
        os.unlink(path)
    print("OK test_yaml_rejects_unknown_construct")


# ------------------------------------------------------------- file categories
def test_file_categories():
    cat = lambda p: mod.file_category(p, CFG)
    assert cat("widget-core/src/test/java/x/CoreTests.java") == "test"
    assert cat(ui("src/components/__tests__/ToolsTab.test.tsx")) == "test"
    assert cat(ui("docs/param-validation.txt")) == "docs"
    assert cat("README.md") == "docs"
    assert cat(ui("package-lock.json")) == "lock"
    assert cat(ui("package.json")) == "build"
    assert cat("pom.xml") == "build"
    assert cat(ui("src/App.tsx")) == "prod"
    assert cat("widget-core/src/main/java/x/A.java") == "prod"
    print("OK test_file_categories")


# ---------------------------------------------------------------- critical
def test_proxy_paths():
    klass, why = mod.classify([f("widget-core/src/main/"
                                 "java/com/acme/widget/core/gateway/"
                                 "RouteFactory.java", 5, 5)], CFG)
    assert klass == "critical"
    assert any(r["rule"] == "proxy_transport" for r in why)
    # starters are on the live path too; a demo-app test directory does not count
    klass2, _ = mod.classify([f("widget-starter-reactive/src/"
                                "main/java/com/acme/widget/reactive/gateway/"
                                "RouteHandler.java", 2, 2)], CFG)
    assert klass2 == "critical"
    klass3, _ = mod.classify([f("widget-demo-app/src/test/"
                                "java/com/acme/widget/demo/gateway/"
                                "RouteProtocolIT.java", 18, 9)], CFG)
    assert klass3 == "normal", "a demo-app test is not a live-path surface"
    print("OK test_proxy_paths")


def test_auth_paths():
    klass, why = mod.classify([f("core/src/main/java/com/acme/widget/core/"
                                 "auth/TokenValidator.java", 3, 1)], CFG)
    assert klass == "critical"
    assert any(r["rule"] == "auth_headers" for r in why)
    klass2, _ = mod.classify([f("core/src/main/java/x/OAuthFlow.java", 1, 1)], CFG)
    assert klass2 == "critical"
    print("OK test_auth_paths")


def test_ci_build_package_json():
    # M3: package.json removed from ci_build -> this test is red
    klass, why = mod.classify([f(ui("package.json"), 1, 0),
                               f(ui("src/App.tsx"), 1, 1)], CFG)
    assert klass == "critical"
    assert any(r["rule"] == "ci_build" for r in why), (
        "a build dependency must be critical")
    klass2, _ = mod.classify([f(".github/workflows/ci.yml", 1, 0)], CFG)
    assert klass2 == "critical"
    klass3, _ = mod.classify([f("pom.xml", 2, 0)], CFG)
    assert klass3 == "critical"
    print("OK test_ci_build_package_json")


def test_migrations_default_config():
    klass, why = mod.classify([f("core/src/main/resources/"
                                 "application.yml", 2, 0)], CFG)
    assert klass == "critical"
    assert any(r["rule"] == "migrations_default_config" for r in why)
    klass2, _ = mod.classify([f("db/migration/V2__init.sql", 5, 0)], CFG)
    assert klass2 == "critical"
    print("OK test_migrations_default_config")


def test_test_deletion():
    removed = f("core/src/test/java/x/RouteHandlerAuthProfileTests.java",
                0, 120, status="removed")
    klass, why = mod.classify([removed], CFG)
    assert klass == "critical"
    assert any(r["rule"] == "test_deletion" for r in why)
    carved = f("core/src/test/java/x/RouteFactoryTests.java",
               10, 78)
    klass2, why2 = mod.classify([carved], CFG)
    assert klass2 == "critical", "carving out 78 lines of tests is a test deletion"
    # +94/-78, more additions than deletions -> not a deletion
    honest = f("core/src/test/java/x/RouteFactoryTests.java",
               94, 78)
    klass3, why3 = mod.classify([honest], CFG)
    assert not any(r["rule"] == "test_deletion" for r in why3)
    print("OK test_test_deletion")


def test_prod_churn_over_limit():
    # M2: limit 5000 -> this test is red (a 490-line diff)
    files = [f("core/src/main/java/com/acme/widget/core/client/"
               "ExternalStdioClientFactory.java", 17, 1),
             f("core/src/main/java/com/acme/widget/core/client/"
               "InitializeResultCapturingTransport.java", 159, 0),
             f("core/src/main/java/com/acme/widget/core/client/"
               "LoopbackClientFactory.java", 71, 7),
             f("core/src/main/java/com/acme/widget/core/dto/"
               "ExtensionSupport.java", 125, 0),
             f("starter-reactive/src/main/java/com/acme/widget/reactive/"
               "router/WidgetHandler.java", 35, 8),
             f("starter-reactive/src/main/java/com/acme/widget/reactive/"
               "router/SessionContext.java", 15, 0),
             f("starter-webmvc/src/main/java/com/acme/widget/webmvc/"
               "controller/WidgetRestController.java", 31, 6),
             f("starter-webmvc/src/main/java/com/acme/widget/webmvc/"
               "controller/SessionState.java", 15, 0)]
    total = sum(x["additions"] + x["deletions"] for x in files)
    assert total > 400, total
    klass, why = mod.classify(files, CFG)
    assert klass == "critical"
    assert any(r["rule"] == "prod_churn" for r in why)
    print("OK test_prod_churn_over_limit")


def test_churn_boundary_exactly_400_is_normal():
    files = [f("core/src/main/java/x/Big.java", 400, 0)]
    klass, why = mod.classify(files, CFG)
    assert klass == "normal", "\"above 400\" is strict: exactly 400 is not critical"
    files2 = [f("core/src/main/java/x/Big.java", 401, 0)]
    klass2, _ = mod.classify(files2, CFG)
    assert klass2 == "critical"
    print("OK test_churn_boundary_exactly_400_is_normal")


def test_churn_ignores_tests_and_docs():
    # M4: tests counted in churn -> this test is red
    files = [f("core/src/test/java/x/OnlyTests.java", 500, 0),
             f(ui("docs/note.txt"), 100, 0),
             f(ui("package-lock.json"), 300, 0),
             f(ui("src/App.tsx"), 10, 10)]
    klass, why = mod.classify(files, CFG)
    assert klass == "normal", "texts, locks and tests are not production code"
    assert not any(r["rule"] == "prod_churn" for r in why)
    print("OK test_churn_ignores_tests_and_docs")


def test_ui_state_plus_view_rule():
    # M1: the rule is knocked out -> this test is red (two layers vs one)
    both = [f(ui("src/hooks/useConnection.ts"), 10, 1),
            f(ui("src/components/HistoryAndNotifications.tsx"), 5, 3)]
    klass, why = mod.classify(both, CFG)
    assert klass == "critical"
    assert any(r["rule"] == "ui_state_plus_view" for r in why), (
        "a state hook plus a component is a two-layer diff, critical")
    only_hook = [f(ui("src/hooks/useConnection.ts"), 26, 1),
                 f(ui("src/hooks/__tests__/useConnection.test.tsx"), 472, 1)]
    klass2, why2 = mod.classify(only_hook, CFG)
    assert klass2 == "normal", "one state layer is normal"
    assert not any(r["rule"] == "ui_state_plus_view" for r in why2)
    print("OK test_ui_state_plus_view_rule")


# ------------------------------------------------------------------- trivial
def test_trivial_docs_only():
    klass, _ = mod.classify([f("README.md", 10, 2),
                             f(ui("docs/history.txt"), 5, 0)], CFG)
    assert klass == "trivial"
    klass2, _ = mod.classify([f("README.md", 1, 0),
                              f(ui("src/App.tsx"), 1, 1)], CFG)
    assert klass2 == "normal", "docs plus code is no longer trivial"
    print("OK test_trivial_docs_only")


def test_normal_small_ui_fix():
    klass, why = mod.classify([f(ui("src/utils/schemaUtils.ts"), 11, 2),
                               f(ui("src/utils/__tests__/schemaUtils.test.ts"),
                                 65, 0),
                               f(ui("docs/schema-ref-warnings.txt"), 5, 2)],
                              CFG)
    assert klass == "normal"
    assert not why
    print("OK test_normal_small_ui_fix")


# --------------------------------------- live traffic: core/recorder
def test_recorder_is_traffic_path():
    # A recorder of live traffic is on the live path (an owner decision after
    # a calibration run); M5: the glob is knocked out -> the case is red
    files = [f("core/src/main/java/com/acme/widget/core/recorder/"
               "TrafficRecorder.java", 75, 5),
             f("src/components/recorder/RecorderTab.tsx", 93, 2)]
    klass, reasons = mod.classify(files, CFG)
    assert klass == "critical", f"recorder = live traffic: {klass}"
    assert any(r["rule"] == "proxy_transport" for r in reasons)
    print("OK test_recorder_is_traffic_path")


# --------------------------------------------------------- owner's severity
def test_owner_max_severity():
    pr = {"owner_reviews": [
        {"body": "## Review verdict\nCHANGES REQUESTED: minor (owner, date)\n"
                 "- [minor] nit"},
        {"body": "## Review verdict\nAPPROVED: normal (owner, date)"},
    ], "owner_comments": []}
    assert mod.owner_max_severity(pr)[0] == "normal"
    pr2 = {"owner_reviews": [
        {"body": "CHANGES REQUESTED: critical (owner, 2026-01-01, head x)\n"
                 "- [BLOCK] one"},
    ], "owner_comments": []}
    assert mod.owner_max_severity(pr2)[0] == "critical"
    # BLOCK without a severity word -> not below major
    pr3 = {"owner_reviews": [{"body": "- [BLOCK] something blocking"}],
           "owner_comments": []}
    assert mod.owner_max_severity(pr3)[0] == "major"
    pr4 = {"owner_reviews": [], "owner_comments": [{"body": "unrelated"}]}
    assert mod.owner_max_severity(pr4)[0] is None
    # the old verdict format: the word without the (owner, ...) parenthesis is
    # read, and the BLOCK floor must not raise the severity
    pr5 = {"owner_reviews": [], "owner_comments": [
        {"body": "## Review verdict\nCHANGES REQUESTED: normal\n\nOwner "
                 "review, 2026-01-01.\n- [BLOCK] item"}]}
    assert mod.owner_max_severity(pr5)[0] == "normal", (
        "a severity word beats the BLOCK floor when it is present")
    pr6 = {"owner_reviews": [], "owner_comments": [
        {"body": "CHANGES REQUESTED: major (core code on the live "
                 "traffic path)"}]}
    assert mod.owner_max_severity(pr6)[0] == "major", (
        "the parenthesis after the word is not always (owner")
    pr7 = {"owner_reviews": [], "owner_comments": [
        {"body": "CHANGES REQUESTED: minor (docs)"}]}
    assert mod.owner_max_severity(pr7)[0] == "minor"
    print("OK test_owner_max_severity")


# ------------------------------------- known exceptions: known_misses
def test_yaml_known_misses_machine_readable():
    # the exceptions must be data, not a comment: otherwise the gate cannot
    # tell them from new misses
    assert isinstance(CFG["known_misses"], list), CFG.get("known_misses")
    assert len(CFG["known_misses"]) == 2, CFG["known_misses"]
    entry = {e["pr"]: e for e in CFG["known_misses"]}
    assert entry[101]["owner"] == "major" and entry[101]["script"] == "normal"
    assert entry[102]["owner"] == "major" and entry[102]["script"] == "normal"
    assert all(e.get("reason") for e in CFG["known_misses"])
    print("OK test_yaml_known_misses_machine_readable")


def _shadow_data():
    """A deferred sample in miniature: both PRs are owner major, by the facts
    of the diff the script says normal (live divergences, owner decisions)."""
    body = "CHANGES REQUESTED: major (owner, 2026-01-01)"
    return [
        {"number": 101,
         "files": [f("core/src/test/java/x/PortPartTests.java", 30, 1)],
         "owner_reviews": [{"body": body}], "owner_comments": []},
        {"number": 102,
         "files": [f(ui("src/components/Color.tsx"), 12, 2),
                   f(ui("src/components/__tests__/Color.test.tsx"), 60, 0)],
         "owner_reviews": [{"body": body}], "owner_comments": []},
    ]


def test_shadow_known_misses_are_not_new():
    # M6: the known/new distinction is knocked out -> the known exceptions
    # become new misses, the test is red
    res = mod.shadow_compare(_shadow_data(), CFG)
    assert res["new_misses"] == [], res["new_misses"]
    assert {h[0] for h in res["known_hits"]} == {101, 102}, res["known_hits"]
    assert res["stale"] == [], res["stale"]
    print("OK test_shadow_known_misses_are_not_new")


def test_shadow_new_miss_still_new():
    data = _shadow_data() + [{
        "number": 999, "files": [f("core/src/main/java/x/Small.java", 10, 1)],
        "owner_reviews": [{"body": "CHANGES REQUESTED: major "
                                   "(owner, 2026-01-02)"}],
        "owner_comments": []}]
    res = mod.shadow_compare(data, CFG)
    assert res["new_misses"] == [(999, "major", "normal")], res["new_misses"]
    assert 999 not in {h[0] for h in res["known_hits"]}, res["known_hits"]
    print("OK test_shadow_new_miss_still_new")


def test_shadow_stale_known_flagged():
    # a known entry whose divergence disappeared, or whose PR fell out of the
    # sample -> stale: a signal to clean the list, the gate is not red
    data = [{
        "number": 101,
        "files": [f("core/src/main/java/com/acme/widget/core/gateway/"
                    "P.java", 3, 1)],
        "owner_reviews": [{"body": "CHANGES REQUESTED: major "
                                   "(owner, 2026-01-01)"}],
        "owner_comments": []}]
    res = mod.shadow_compare(data, CFG)
    assert res["new_misses"] == [] and res["known_hits"] == [], res
    assert {s[0] for s in res["stale"]} == {101, 102}, res["stale"]
    print("OK test_shadow_stale_known_flagged")


def main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {t.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} green")
    shutil.rmtree(HH, ignore_errors=True)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
