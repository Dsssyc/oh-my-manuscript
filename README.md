# oh-my-manuscript (`omm`)

English | [简体中文](./README.zh-CN.md)

**An author-first, AI-collaborative workbench and multi-target compiler for scientific manuscripts.**

`oh-my-manuscript` (`omm`) is **not** an automated paper-mill generator, nor is it another rich-text word processor. The human researcher remains the first-person accountable author—owning every scientific claim, experimental fact, and scholarly judgment—while AI agents help eliminate mechanical friction across outlining, multi-round prose refinement, deterministic figure/table layout, and journal submission or peer-review revision packaging.

---

## Philosophy

1. **Author Accountability & Academic Integrity**: AI assists with drafting from author-authored outlines, polishing, cross-checking, and formatting—never fabricating research conclusions or generating synthetic scientific figures.
2. **Workbench, Not Another Word Processor**: Designed to sit side-by-side in an AI IDE's embedded web view or local browser. Authors steer via **Outline Blocks** and **Annotations** (across text spans, figure regions, equations, and table cells); plain-text **Structured Source** and Git remain the single source of truth on disk.
3. **One Structured Source, Deterministic Multi-Target Compilation**: Write clean Markdown prose + native LaTeX math + declarative figure/table blocks once; compile deterministically to live HTML preview, journal-ready Word (`.docx` Clean + strict ECMA-376 native Track Changes + auto-line-numbered response letters), and standard `.tex` (`+ latexdiff`).

---

## Planned `0.1` Workflow

- `omm init` — Initialize a Git-backed **Manuscript Workspace** (`manuscript.toml`, sectioned **Structured Source**, `references.bib`, `.omm/`).
- `omm serve` — Launch the local live-reloading workbench (multi-tab navigation for Manuscript / Appendices / Review Responses / Figures, three-tier **Checkpoint** diffing, and universal **Annotation** tools).
- `omm check` — Validate unresolved **Outline Blocks**, open **Annotations**, broken `block_id` or citation references, terminology consistency, and journal constraints.
- `omm build` — Run **Target Compilers** against the current tree (and optional **Submission Milestone** tag) to emit submission-ready `.docx` or `.tex` packages with resolved page/line numbers.

See [docs/GLOSSARY.md](./docs/GLOSSARY.md) for canonical bilingual domain terms.

---

## Project Status & Verification Boundary

> **Status (`0.1.0-dev` — Initial Scaffold)**:
> - **Implemented & Verified**: Python package & `omm` CLI entry point (`uv` + `pytest`), bilingual (`en` / `zh-CN`) i18n catalog, and Vite + React + TypeScript workbench shell (`tsc` + build).
> - **Not Yet Implemented (`0.1` Roadmap)**: Structured Source parser & `block_id` indexer, SSE/WebSocket live workbench server & annotation store, Git Checkpoint diff engine, and Word (`.docx` OOXML/OMML/Track Changes) & LaTeX (`.tex`) Target Compilers. Legacy external `.docx` ingestion (`ingest`) is deferred to `0.2`.

---

## Development Quickstart

Requirements: Python `>= 3.12` (managed with [`uv`](https://docs.astral.sh/uv/)) and Node.js `>= 22`.

```sh
# Run Python CLI & unit tests
uv run omm --help
uv run pytest

# Typecheck & build Web workbench
npm --prefix web install
npm --prefix web run typecheck
npm --prefix web run build
```
