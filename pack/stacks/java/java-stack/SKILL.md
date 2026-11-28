---
name: java-stack
description: Concrete Java and Maven recipes for the language-neutral pack skills.
version: 1.0.0
author: maintainer, Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [java, maven, stack]
    related_skills: [serena-lsp, dependency-bump, tester-verdict-checklist, debugging-discipline, decomposition-contract]
---

# Java stack recipes

This skill holds the Java and Maven commands that the core pack skills state
only as principles. Add `pack/stacks/java` to `skills.external_dirs` only for
projects on this stack. The commands the project actually uses still belong
in project conventions § Build and test commands; these are the defaults to
start from.

## Build and verify (tester-verdict-checklist, debugging-discipline)

- Compile the affected modules with their in-repo dependencies:
  `./mvnw -pl <modules> -am compile` (`-am` builds what they depend on).
- "Green locally" is the phase CI runs: `./mvnw -pl <modules> -am verify`
  runs static analysis, coverage and style checks; a build with
  `-DskipTests` runs none of them. Gradle: `./gradlew :<module>:check`.

## Dependency versions (dependency-bump)

Latest version from Maven Central metadata, not from a search site:

```bash
curl -s https://repo1.maven.org/maven2/<group/path>/<artifactId>/maven-metadata.xml \
  | grep -o '<latest>[^<]*'
```

The path is the groupId with dots replaced by slashes, plus the artifactId.
Pinned versions on the integration branch in one command:
`git show origin/<integration-branch>:pom.xml | grep -oE "<[a-z-]+\\.version>[^<]+"`.

## Proving a third-party API exists (decomposition-contract)

`javap -cp <jar> <class>` lists the members of a class in the pinned jar
(from `~/.m2/repository/...`); cite the class and version as the source.

## Serena calls with Java paths (serena-lsp)

```
get_symbols_overview(relative_path="server/src/main/java/com/example/ProxyHandler.java", depth=1)
find_referencing_symbols(name_path="ErrorDto/redactUrl", relative_path="core/src/main/java/com/example/ErrorDto.java")
find_declaration(name_path="relayWithSessionHeader", relative_path="server/src/main/java/com/example/ProxyController.java")
rename_symbol(name_path="ErrorDto/redactUrl", relative_path="core/src/main/java/com/example/ErrorDto.java", new_name="redactTargetUrl")
safe_delete_symbol(name_path="ProxyHandler/legacyRelay", relative_path="server/src/main/java/com/example/ProxyHandler.java")
get_diagnostics_for_file(relative_path="server/src/main/java/com/example/ProxyHandler.java", min_severity=1)
find_implementations(name_path="Transport", relative_path="core/src/main/java/com/example/Transport.java")
search_for_pattern(substring_pattern="destructiveHint\\s*=\\s*true", paths_include_glob="**/*.java",
                   restrict_search_to_code_files=True)
replace_symbol_body(name_path="ProxyHandler/relayAndAwait[0]", relative_path="server/src/main/java/com/example/ProxyHandler.java", body="...")
replace_content(relative_path="ui/src/test/java/com/example/UiIT.java",
                needle="connectButton\\(\\).*?click\\(\\);", repl="connectViaProfile();",
                mode="regex", allow_multiple_occurrences=True)
```

Java has overloading: a body edit or rename on an overloaded method needs
the index form `Class/method[0]`. Point the language server at the Maven or
Gradle build so it resolves the module classpath.
