---
name: oh-my-manuscript
description: Draft, revise, annotate, check, and compile Git-backed scientific manuscripts (Structured Source `.md` + BibTeX + figures) with persistent `{#block_id}` addressing, explicit revision sync, and Word/LaTeX submission builds. Use when creating or editing academic papers, outlines, figures, three-line tables, author-reviewer response letters, workspace annotations, or `.docx`/`.tex` exports in a `manuscript.toml` workspace.
metadata:
  version: "0.1.0"
---

# Oh My Manuscript (`omm`)

`oh-my-manuscript` keeps Structured Source (`.md`), `references.bib`, `figures/`, and `.omm/annotations.json` inside the paper's own Git repository while running one shared loopback workbench daemon (`http://127.0.0.1:49638/`).

## Launcher & Authority

Use the installed launcher (`~/.agents/skills/oh-my-manuscript/scripts/omm`; Windows `%USERPROFILE%\.agents\skills\oh-my-manuscript\scripts\omm.cmd`) with `--cwd <workspace>` (and `--json` when structured output is needed). `omm --help` and `omm <command> --help` list exact flags.

Installing (`omm install`), restarting (`omm restart`), and stopping (`omm stop`) the shared runtime or background daemon are user-owned operations: ask for explicit user authorization before running them. Never open browser windows automatically; `omm console` ensures the loopback daemon is ready and prints its URL (`http://127.0.0.1:49638/#workspaces/<workspace_id>`).

## Core Workflow

1. **Workspace registration**: `omm init --title "..."` initializes and registers a new Manuscript Workspace; `omm register` registers an existing checkout; `omm relocate` updates a moved path. Running `check`, `stamp`, `sync`, `checkpoint`, `annotate`, or `build` in a temporary Git worktree never mutates the registered main workspace path.
2. **Write-time `{#block_id}`**: Every addressable block (heading, paragraph, list, display math, directive block, and multi-panel subfigure) carries a workspace-unique `{#<block_id>}` (`^[A-Za-z][A-Za-z0-9_-]{1,63}$`; never reuse a deleted block's ID for unrelated content):
   - Headings, paragraphs, lists, and `$$...$$` display math place `{#id}` at the end of the block (`$$...$$ {#eq-id unnumbered}` for unnumbered equations; never use `\tag`).
   - Directive blocks place `{#id}` on the opening fence: `:::{outline} {#out-id}`, `:::{figure} {#fig-id cols=2}`, `:::{table} {#tbl-id}`, `:::{review-item} {#R1-C1 reviewer="Reviewer 1"}`.
   - Assign `{#block_id}` directly when writing or splitting blocks, or run `omm stamp` once after editing to stamp unlabelled blocks. `omm sync` and `omm check` never rewrite `.md` files.
3. **Explicit revision gate**: Edit `.md`, `references.bib`, or `figures/` freely on disk — the workbench stays pinned to the last published revision without mid-edit flicker. When a coherent edit round is ready and `omm check` has no errors, run `omm sync --json` to publish a new Git revision (`refs/omm/published`) without touching `HEAD` or `.git/index`. At stage milestones, commit normally on the Git branch (`git commit`) and run `omm sync` to align `refs/omm/published` with `HEAD`.
4. **Annotations & Checkpoints**:
   - Inspect open feedback with `omm annotate list --status open --json`.
   - Address each open Annotation in Structured Source, run `omm sync --json`, then record its frozen Resolution Snapshot with `omm annotate resolve <id> --json`.
   - Advance the review baseline with `omm checkpoint --json` when requested by the author.
5. **Submission check & build**: Run `omm check` (must exit `0` with zero unexpanded `:::{outline}` blocks and zero open Annotations) before `omm build`.

## Structured Source Invariants

- **Strict single-meaning references** (rendered and exported in `style.link_color`, default `#0070C0`):
  - `#` is exclusively a workspace block reference: `[#sec-method]`, `[#fig-domain]`, `[#fig-panels-a]`, `[#tbl-metrics]`, `[#eq-boussinesq]`. Never reference `:::{outline}` blocks.
  - `[#loc:p-start]` or `[#loc:p-start..p-end]` is exclusively for response letters (`response_letter` deliverable) to backfill compiled page and line numbers of paragraphs/headings/equations in a single target deliverable (never floating figures or tables).
  - `@` is exclusively a BibTeX citation: parenthetical `[@beven1979; @kirchner2009]` or narrative `@beven1979`.
- **No manual `## References` section**: Never hand-write a bibliography section in `.md`; `references.bib` is the single source of truth, formatted automatically in the References tab and by `omm build`.
- **Canonical three-line tables (`:::{table}`)**: One caption paragraph before the pipe grid, optional note paragraphs after; equal logical columns per row; rectangular cell merges use `>` across the top row to the right of the top-left primary cell and `^` across the full width of every covered row below (`---` in all cells emits a midrule without consuming a logical row index).
- **Review Items (`:::{review-item}`)**: Must start with a `:::{comment} ... :::` block quoting the reviewer, followed by stamped author reply blocks. Reverse target indexing reads `[#...]` and `[#loc:...]` strictly from the author reply body.
