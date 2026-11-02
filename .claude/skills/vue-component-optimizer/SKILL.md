---
name: vue-component-optimizer
description: Analyze Vue 3 component structure and recommend performance and code-reuse optimizations, as a ranked report with file:line evidence and before/after snippets, then offer to apply the fixes. Use this whenever someone asks to review, audit, profile, clean up, refactor, or speed up Vue components or views. That includes "why is this page slow", "find duplicated code in the frontend", "which components are too big", "suggest composables to extract", "optimize re-renders", "lazy-load routes", or a request to analyze client/src or a specific .vue file, even if they never say "optimize".
---

# Vue Component Optimizer

Find the changes that actually matter in a Vue 3 codebase: work done on every render, unstable list identity, oversized components, and logic copy-pasted across files. Present them so a developer can act on them in minutes.

The output is a **report first**. Edit code only after the user picks which findings to fix. Optimizations change behaviour in subtle ways (reactivity, timing, bundle splits), so the user should choose.

## Workflow

### 1. Scope and conventions

Default scope is the whole frontend source directory (e.g. `client/src`), or the files the user named. Read the project's `CLAUDE.md` files (root and client) before judging anything. House conventions override generic advice. Examples: "Composition API with `setup()`", "never use index as v-for key", or "delegate .vue edits to a specific subagent".

### 2. Run the scanner

```bash
python3 <skill-dir>/scripts/scan_vue.py <src-dir>          # markdown
python3 <skill-dir>/scripts/scan_vue.py <src-dir> --json   # for further processing
```

It gives you the size of every component and a list of **signals**:

| Signal | What it suggests |
|---|---|
| `large-component` / `large-template` / `large-script` | The component likely holds several responsibilities; split it |
| `index-key`, `v-for-no-key` | Unstable list identity: wrong DOM reuse and lost component state |
| `v-for-with-v-if` | Filtering in the template on every render |
| `template-method-calls` | A function evaluated on every render, often several times with the same arguments |
| `composable-in-function` | `useX()` called inside a nested function instead of at setup's top level |
| `deep-watch` | Deep traversal of a possibly large object on every change |
| `eager-heavy-import`, `eager-routes` | Code that could be split into lazy chunks |
| `unused-scoped-css`, `unused-setup-return` | Dead code: delete it before splitting so children don't inherit it |
| `unregistered-component` | A tag that renders as an unknown element. Almost always a user-visible bug |
| duplicated helpers | Extraction candidates for a composable or util |

The scanner is regex-based and **heuristic**. Treat each signal as a lead. Open the file at that line and confirm it before it goes in the report. For example, a template method that returns a constant class name is fine. One that filters a 250-item array three times per render is not.

### 3. Verify and dig deeper

For every signal you keep, read enough surrounding code to state the actual cost. For large files, read the flagged regions rather than the whole file; it keeps the review fast. Then look for what the scanner can't see. `references/patterns.md` has the catalogue with before/after code. Read the sections relevant to what you found. The most common misses:

- Derived data recomputed in methods instead of `computed`
- Global, uncached formatters (`Intl.NumberFormat`, `toLocaleDateString` with options) created per call
- Watchers that reload everything when only one filter changed
- Several components each doing the same fetch, loading state and error state
- Large static lookup tables declared inside `setup()` instead of at module level
- Long lists rendered without virtualization or pagination

Report **correctness bugs** you come across (a missing component, a state that never resets, a formatter that shows the wrong unit, a race between overlapping requests) in their own section ahead of the optimizations. A refactor that preserves a bug spreads it into new files.

Before recommending a new `:key`, check the actual data that the field is unique per list. A stable-looking id that repeats is worse than an index.

For reuse, check whether duplicated helpers really are duplicates. Diff their bodies. Two `formatDate` functions with different formats are a parameterization opportunity, not a straight extraction. Check what already exists in `composables/` and `utils/` before proposing a new file.

### 4. Rank by impact

Rank each finding on two axes and sort by impact, then by effort:

- **Impact**: High = runs per render or per list item on a hot view, causes visible bugs (state lost on reorder), or the same logic exists in 3+ places. Medium = a measurable but local cost, or 2 copies. Low = hygiene.
- **Effort**: S (under 15 minutes, one file), M (a few files, or a new composable), L (a component split or an architectural change).

Leave out findings you can't tie to a concrete cost. A short, credible report beats a long, speculative one; around 10 findings is usually plenty.

### 5. Write the report

Use this structure:

```markdown
# Vue component review: <scope>

## Summary
<2-3 sentences: overall health, the single biggest win, rough count of findings by impact.>

## Component inventory
<Table of the largest components from the scan: file, lines, template/script/style split, finding count. Up to 10 rows.>

## Bugs found along the way
<Only if any: what breaks for the user, where, the fix. Fix these before optimizing.>

## Findings

### 1. <Short imperative title>  ·  Impact: High  ·  Effort: S
**Where:** `views/Orders.vue:14-26`
**Why it matters:** <the concrete cost, e.g. "getOrdersByStatus() filters all orders 4x on every render, including on every filter change">
**Before:**
```vue
<minimal snippet>
```
**After:**
```vue
<minimal snippet>
```

<...one section per finding...>

## Reuse opportunities
<Table: helper or pattern, files, proposed home (composable/util), notes on differences between copies.>

## Not worth changing
<Optional: signals you checked and dismissed, one line each, so the reader knows they were considered.>

## Suggested order
<Numbered list of finding numbers to do first, grouped into safe batches.>
```

Keep snippets minimal: only the lines that change, plus enough context to locate them. Cite real line numbers from the files, not from the scanner, if they differ.

### 6. Offer fixes, then apply on approval

End by asking which findings to apply: by number, "all High", or "none". When applying:

- Follow project conventions from step 1, including any rule about which agent edits `.vue` files.
- Make one logical change at a time, so each can be reviewed or reverted on its own.
- After the edits, build (`npm run build` or `npx vite build`) and run any tests. If a dev server is running, reload the affected views and check the console for errors.
- Report what changed with file:line references, and anything you skipped and why.

If the user only asked for analysis, stop after the report and the question.
