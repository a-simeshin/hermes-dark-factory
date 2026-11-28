# serena-lsp: recipes

This document collects the less frequent Serena calls that the main skill
only names: finding implementations and overrides, text search when there is
no symbol, replacing a whole method body, inserting a method next to an
existing one, and bulk regex edits inside a file. `<file>` stands for a source file of your
repository; stack skills under `pack/stacks/` show the same calls with real
paths of their language.

### Who implements and overrides it

```
find_implementations(name_path="Transport", relative_path="<file>")
```

For an interface it returns the implementations, for an abstract method the
overrides. `relative_path` here must be a FILE; a directory is not accepted.
Typical case: you change an interface contract and want to know how many
classes need edits. Grep by the interface name returns imports and mentions,
not implementations.

### Text search when there is no symbol

```
search_for_pattern(substring_pattern="destructiveHint\\s*=\\s*true",
                   paths_include_glob="**/*.<ext>",
                   restrict_search_to_code_files=True,
                   context_lines_before=2, context_lines_after=2)
```

The replacement for `grep -rn`. Why it is worth it: a glob restriction,
separate context before and after, and the "code files only" flag (it does
not wander into build output, dependency folders or binaries).

### Replace a whole method body

```
replace_symbol_body(name_path="ProxyHandler/relayAndAwait[0]", relative_path="<file>", body="...")
```

Edits the symbol within the bounds the language server knows. `sed` by line
numbers breaks on any shift higher up in the file; this class of error does
not exist here.

### Add a method next to an existing one

```
insert_after_symbol(name_path="ProxyHandler/relayAndAwait", relative_path="<file>", body="...")
insert_before_symbol(name_path="ProxyHandler/close", relative_path="<file>", body="...")
```

Insertion by symbol, not by line: no counting indentation or looking for the
closing brace.

### Bulk edit inside a file

```
replace_content(relative_path="<file>",
                needle="connectButton\\(\\).*?click\\(\\);",
                repl="connectViaProfile();",
                mode="regex",
                allow_multiple_occurrences=True)
```

For places without symbol bounds: edits inside a method body, repeated
constructs in tests. In `regex` mode use a pattern like `start.*?end` to
avoid quoting large blocks. Without `allow_multiple_occurrences`, several
matches give an error instead of a silent edit in the wrong place: that is a
safeguard, not an obstacle.
