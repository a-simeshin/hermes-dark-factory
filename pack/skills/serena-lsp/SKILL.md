---
name: serena-lsp
description: Searching, editing and renaming code through LSP.
version: 1.0.0
author: maintainer, Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [serena, lsp, refactor, search]
    related_skills: [fix-at-the-root, no-duplicate-work]
---

# Serena: working with code through LSP

This skill tells agents to work with source code through the Serena MCP
server (language-server navigation and edits) instead of grep, cat and sed:
what a `name_path` is, a budget for reading code, a replacement table for
terminal habits, the call for each common task, and the order of calls that
saves the most context. Tool names and parameters below match Serena's tools;
check them against the Serena version you install, and make sure language
servers are configured for the languages of your repository. More recipes are
in references/recipes.md.

## Key concept: name_path

The path of a symbol INSIDE a file, not the file path. Method `myMethod` in
class `MyClass` has `name_path` = `MyClass/myMethod`. Forms:

- `myMethod`: any symbol with this name;
- `MyClass/myMethod`: suffix match of the path;
- `/MyClass/myMethod`: exact match of the whole path in the file;
- `MyClass/myMethod[1]`: a specific overload (zero-based index, for languages
  with overloading).

Overloads without an index are ambiguous: `rename_symbol` and body edits on
an ambiguous path may hit the wrong method.

## Budget for reading code

Reading files in chunks with `sed -n` and reading whole large files were
once the largest share of a coder's tool output. Rules:

- read code by symbols: `get_symbols_overview` for the map of a file,
  `find_symbol` with the body for the method or class you need;
- `sed -n` only with a narrow range when you need lines as such (a log, a
  config);
- `read_file` of a whole file only for an edit across the whole file;
  otherwise offset/limit around the place you need;
- when you need a large output in full (Hermes truncates tool output at
  `tool_output.max_bytes`), write it to a file and read the piece you need;
- assemble multi-step exploration (file map, symbol bodies,
  `search_for_pattern` over the tree) into ONE `execute_code` call: it makes
  many tool calls itself and prints a digest, while each separate call
  resends the whole cached context.

## Replacement table: what not to do in the terminal

| instead of | use | why |
|---|---|---|
| `grep -n "class Foo"` | `find_symbol` | returns the declaration with body and bounds, not a line |
| `grep -rn "foo("` | `find_referencing_symbols` | returns real callers, not matches in strings and comments |
| `grep -rn "text"` over the tree | `search_for_pattern` | structure-aware, can restrict the scope |
| `cat file`, `head -100` | `get_symbols_overview` | a 20-line map instead of 800 lines in context |
| editing a method body with sed | `replace_symbol_body` | edits the whole symbol, does not break neighbouring lines |

`terminal` remains the right tool for builds, tests, git and gh: there is
nothing to search by symbol there. It is also right for searching NON-code:
logs, CI output, configs outside the source tree.

## What to use for what

### Understand an unfamiliar file

```
get_symbols_overview(relative_path="src/main/java/com/example/ProxyHandler.java", depth=1)
```

A symbol map instead of hundreds of lines in context. `depth=1` adds class
members. Only then ask for the body of the symbol you need.

### Find a declaration

```
find_symbol(name_path_pattern="ProxyHandler/relayAndAwait", relative_path="server", include_body=True)
```

`relative_path` may be a directory: the search narrows and speeds up. Use
`include_body` only when you need the body. `substring_matching=True` helps
when the exact name is unknown.

### Who uses it

```
find_referencing_symbols(name_path="ErrorDto/redactUrl", relative_path="core/src/main/java/com/example/ErrorDto.java")
```

Returns the symbols that reference this one, with a code snippet around each
reference. This is NOT the same as grep by name: comments, strings and
same-named methods of other classes are not included. Mandatory before any
signature change and before deletion: the list of callers is the answer to
"what will I break".

### Where is what I see in a call declared

```
find_declaration(name_path="relayWithSessionHeader", relative_path="server/src/main/java/com/example/ProxyController.java")
```

The reverse of reference search: from a use to the declaration, including
jumps into dependencies.

## Edits

### Rename across the project

```
rename_symbol(name_path="ErrorDto/redactUrl", relative_path="core/src/main/java/com/example/ErrorDto.java", new_name="redactTargetUrl")
```

A refactoring done by the language server: it edits the declaration and ALL
references, including other modules. The only safe rename: textual search and
replace also hits same-named methods of other classes and string literals.
With overloads, the path must point at one method unambiguously.

### Delete a symbol safely

```
safe_delete_symbol(name_path="ProxyHandler/legacyRelay", relative_path="server/src/main/java/com/example/ProxyHandler.java")
```

Checks references before deleting. Cutting lines leaves calls the compiler
finds later, and in tests sometimes not at all.

## Check after an edit

```
get_diagnostics_for_file(relative_path="server/src/main/java/com/example/ProxyHandler.java", min_severity=1)
```

The language server's errors and warnings for the file, without a build. A
type typo shows in a second instead of after a full build.
`get_diagnostics_for_symbol` narrows it to one symbol.

## When Serena is NOT needed

- builds, tests, git, gh: nothing to search by symbol;
- logs, CI output, configs outside the source tree: plain search fits better;
- a file you already read in full in this session: rereading it with any tool
  wastes context.

## The order that saves the most

1. `get_symbols_overview` on the file to understand its structure;
2. `find_symbol` with `include_body=True` only for the symbol you need;
3. `find_referencing_symbols` before changing a signature or deleting;
4. edit with `replace_symbol_body` or `insert_after_symbol`;
5. `get_diagnostics_for_file` right after the edit, before running tests.

## References (read on demand)

- references/recipes.md: implementations and overrides; text search when
  there is no symbol; replacing a whole method body; adding a method next to
  an existing one; bulk edits inside a file
