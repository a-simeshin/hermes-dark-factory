# Visual delta: the Before / After section, first in the issue body (not the PR)

This document defines the visual delta: a Before / After section at the top
of a UI-facing issue that shows the owner, in two pictures or two small diagrams,
what changes. It covers where the section lives (the issue, at the prototype
stage, never the PR), its format for UI and non-UI work, how frames are
captured with one command, how mermaid diagrams are validated, and the
recurring traps of automated screenshot capture.

## Why it exists and where it lives

The owner does not want a wall of text; they want two visual prototypes,
before and after. When the change is not visible on screen, they want a
conceptual diagram of how the process, code or components change.

Where it lives is a project decision; follow the project conventions. The
reference setup keeps it in the ISSUE, at the prototype stage: the PR has no
Before/After section and no separate visual-delta cards, and a final
screenshot from the E2E test is enough when a UI test runs. The owner moved
it there for cost (separate visual-delta work on PRs became the single most
expensive item of the token spend) and because the pictures are needed before
implementation starts, not after. A project that keeps before/after in the
PR applies the same format there.

The visual delta is required only for UI-facing issues. A delta the owner
cancelled for an issue is not requested or recreated again.

## Format

- The body of a UI-facing issue starts with `## Before / After`, FIRST,
  before any other text. The section heading must be the first line of the body
  (machines allow only whitespace before it), so any `Refs #N` line goes
  after the details block.
- **UI** (a real visible change on screen): a two-column table, Before and
  After, one image each. One screen in one state: Before is the current
  screen, After is the prototype of the desired one (a DOM edit of the live UI
  or a render of a mock-up). Mark the important places with outlines or
  captions. PNG files on the assets branch, raw links.
- **Non-UI** (a bug, documentation, refactoring, a protocol change): no
  section is required. When the author adds one, it is a mermaid diagram of
  the delta only (flowchart for a process, sequenceDiagram for a
  protocol or calls, classes or components for structure). House style: TWO
  mermaid blocks, the first is the Before state, the second the After state;
  `classDef delta` on the changed nodes of the After diagram; 5 to 10 nodes,
  the delta and not the whole system. For a bug: where it breaks and what the
  fix changes.
- After the section, at most 5 lines in the user's words ("For you: ...");
  everything else (details, verification, hygiene) is folded into
  `<details><summary>Details</summary>`.
- The AUTHOR of the issue writes the section at creation time.

## What is not a UI change

- File paths in the diff do not make an issue a UI issue: edits to component
  or style files without a visible change on screen are non-UI and get a
  diagram.
- Rendering text into a PNG to satisfy "two images differ" is forbidden. In
  one case an agent drew test output into a PNG and compared hashes to pass
  the check: that is fitting the evidence to a formal gate. No visible change
  means no images (a diagram, if a delta is written at all).
- A UI change that does not alter the runtime state of the screen (a link
  added to an existing note) needs no new shoot: the existing section stays
  the proof and the link is listed in the behaviour list.
- If the owner explicitly cancels screenshots or diagrams for an issue, use
  exactly the format the owner asked for and record that decision in the
  card, so the next worker does not "fix" the machine's signal against it.

## Prototype method for feature and ux issues

After is drawn ON TOP of the real UI of the integration branch's demo stand:
open the stand with Playwright or CDP, insert the proposed elements using the
product's own classes, and capture the frame. A separate HTML page is
forbidden: a mock built from scratch reads as a different product (fonts,
tables, spacing), and the owner ends up judging a parody instead of their own
interface. Before is the same stand without edits. After the table: a short
description of the behaviour and a list of open questions.

## Capture with one command

The pack's capture script (in the reference setup
`scripts/visual-delta-capture.py`) takes a label, a before ref and an after
ref (branch, SHA or an HTML file), a route, a viewport and named shots
(`name=selector`), optionally a steps file and annotations. It resolves the
refs, builds or reuses the artifact by SHA, starts bounded stands, waits for
readiness by an HTTP condition (not a sleep), captures both sides, pushes the
PNGs to the assets branch and prints a ready markdown table. It is idempotent
and fails with "FAIL <phase>: reason". Do not do the manual steps (worktrees,
dependency installs, builds, ports) by hand; use the lower-level shot script
only when the stands do not come from a git ref or are already running.

Adapt the build and stand-start steps of the script to your stack; the
readiness, capture and push logic is generic.

## Validating mermaid

GitHub's diagram enrichment can hang forever in an automated browser even on
valid diagrams, so a spinner is not a syntax verdict. Validate on
`about:blank` with mermaid 11 loaded from a CDN: `mermaid.parse(text)` must
succeed, `mermaid.render` is used to count nodes and edges, all inside the
page script, returning only the numbers. In v11 `render` resolves to an
OBJECT `{svg, bindFunctions}`, not a string: assigning the result itself to
`innerHTML` yields zero nodes. Selectors in v11: nodes `.node`, clusters
`.cluster`, edges `path.flowchart-link` (`.edgePath` returns 0 and looks like
"a diagram without edges"); a thick edge `==>` shows as
`.edge-thickness-thick`; the highlight is checked by the node class
(`node default delta`).

## Capture traps

Each of these cost a wasted run at least once.

- **Readiness** is URL plus `document.readyState == "complete"`; a bare
  readyState check also matches `about:blank`.
- **Tabs in headless UI libraries:** an inactive tab trigger may have
  `tabindex=-1` so a bare `focus()` does nothing; activate it with real
  pointer events and check the tab's active state attribute. The active panel
  is `[role=tabpanel]:not([hidden])` (or the library's active state
  attribute): a bare `[role=tabpanel]` can match hidden zero-width panels and
  the screenshot fails with "0 width".
- **Buttons by exact text** (`trim() === "Connect"`): substring matching
  catches an earlier element with a longer label.
- **A selector must exist on BOTH sides:** the base branch may lack the test
  id the head adds; exact text or a structural anchor works on both.
- **Results below the fold:** scroll the result heading into view before the
  frame; otherwise the frame is wrong while every step is green.
- **Fixed-height scroll containers** cut element shots: scrollIntoView does
  not help and the frame stays byte-identical even when the height option
  changes. Use a side-safe hook that sets `overflow: visible` and
  `height/max-height: auto/none` on the scroll ancestors, and say so in the
  caption (camera, not montage).
- **Presets in the URL may not survive:** an app that fetches its config
  after mount overwrites form defaults; set field values in the steps AFTER
  the config settles, using the native value setter plus an `input` event for
  framework-controlled fields.
- **Text shown capitalised may be lowercase in the DOM** (CSS
  `text-transform`): compare lowercased.
- **Ports are part of the origin:** local storage (saved items, profiles)
  survives a stand restart on the same port and leaks into frames. Use fresh
  ports for each capture.
- **Debug logging flags pollute frames** when the UI shows logs: capture on a
  default stand, collect wire evidence in a separate debug run and quote the
  log, not the frame.
- **Counters accumulate across sessions:** restart both stands before the
  final frame.
- **A control case with no visual change** produces byte-identical PNGs; do
  not put such a pair in the table (machines require a difference); give
  text links to the raw files in the caption instead, or prove "only
  timestamps changed" with a pixel diff.
- **The raw CDN caches** the assets branch for minutes: after a push compare
  bytes in a local checkout of the assets branch, not via the raw URL.
- **A stale branch's After can show the reverse of upstream changes** when
  the integration branch reworked the same surface. Do not mask it; caption
  it honestly ("sections below X reflect the branch's old base; the PR's delta
  is field Y").
- **A workaround needed to capture a frame is a finding.** If you had to
  bypass a real bug of the head (for example nudge a field so a memoised
  callback is recreated), post a PR comment with a reproduction and create a
  fix card (after checking that no live card already covers it); disclose the workaround in the caption. Never bake it silently
  into the steps.
- **The head's stand needs the backend of its own tree;** a donor artifact
  from another branch of the same era does not guarantee the contract.
- **Scratch directories are cleaned under you:** keep worktrees and built
  artifacts in the card's task workspace or the repository's `.worktrees/`,
  not in a profile cache.
