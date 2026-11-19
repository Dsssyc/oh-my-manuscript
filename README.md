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

## `0.1` Workflow

`oh-my-manuscript` installs once into a shared global Agent Skill (`~/.agents/skills/oh-my-manuscript`, with automatic directory links for installed coding agents and a single-instance loopback workbench at `127.0.0.1:49638`), while keeping every paper's **Manuscript Workspace** self-contained inside its own Git repository:

- `uvx oh-my-manuscript@<version> install` — Materialize the isolated runtime and shared Agent Skill (`~/.agents/skills/oh-my-manuscript/scripts/omm`).
- `omm init` / `omm register` / `omm relocate` — Initialize or register a Git-backed **Manuscript Workspace** (`manuscript.toml`, sectioned **Structured Source**, `references.bib`, `.omm/`).
- `omm console` — Ensure the single-instance loopback workbench daemon is running and print its fixed local URL (five top-level views: Manuscript / Appendices / Review Items / Figures / References, three-tier **Checkpoint** diffing, and universal **Annotation** tools).
- `omm stamp` / `omm sync` — Stamp persistent `{#block_id}` anchors onto new blocks and publish a verified Git `revision` to switch the workbench view atomically without mid-edit flicker.
- `omm check` — Validate unexpanded **Outline Blocks**, open **Annotations**, broken `block_id` or citation references, and journal constraints.
- `omm build` — Run **Target Compilers** against the published tree (and optional **Submission Milestone** tag) to emit submission-ready `.docx` or `.tex` packages with resolved page/line numbers.

See [docs/GLOSSARY.md](./docs/GLOSSARY.md) for canonical bilingual domain terms, and [ADR-001](./docs/adr/ADR-001-core-representation-and-contracts.md) / [ADR-002](./docs/adr/ADR-002-global-skill-runtime-and-daemon-contract.md) for architectural contracts.

---

## Project Status & Verification Boundary

The `0.1` workflow is implemented in source and covered by headless automated checks. It includes Structured Source parsing, validation, and stamping, explicit `omm sync`, three levels of Checkpoint diffs, the five-view workbench, and a global shared Skill with a windowless loopback daemon. `omm build` generates Clean and Marked Word `.docx` and LaTeX `.tex` deliverables, with page and line numbers for `[#loc:...]` in response letters.

When a location reference has no supplied `--layout-pdf`, the Word target requires headless LibreOffice (`soffice`) and the LaTeX target requires a TeX engine. The end-to-end test uses a PDF with printed margin line numbers to exercise the same location resolver without those engines.

Word outputs are checked by the built-in strict OOXML validator. The acceptance fixture's Word XML parts were checked against the official ECMA-376 schemas, and automated tests compare the accepted and rejected revisions with their respective Clean documents. Review fonts, pagination, and revision behavior in desktop Microsoft Word before submission. [ECMA-376](https://ecma-international.org/publications-and-standards/standards/ecma-376/)

Legacy `.docx` ingestion (`ingest`) is planned for `0.2`.

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
