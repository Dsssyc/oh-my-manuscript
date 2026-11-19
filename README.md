# oh-my-manuscript (`omm`)

English | [简体中文](./README.zh-CN.md)

**An author-first, AI-collaborative workbench and deterministic multi-target compiler for scientific manuscripts.**

`oh-my-manuscript` (`omm`) is **not** an automated paper-mill generator, nor is it another rich-text word processor. The human researcher remains the first-person accountable author—owning every scientific claim, experimental fact, and scholarly judgment—while AI agents help eliminate mechanical friction across outlining, multi-round prose refinement, cross-checking, and journal submission or peer-review revision packaging.

---

## Philosophy

1. **Author Accountability & Academic Integrity**: AI assists with drafting from author-authored outlines, polishing, cross-checking, and formatting—never fabricating research conclusions or generating synthetic scientific figures.
2. **Workbench, Not Another Word Processor**: Built to run in a local browser or embedded web view alongside your coding agent (CLI, desktop app, or IDE). Authors steer via **Outline Blocks** and **Annotations** (across text spans, figure regions, equations, and table cells); plain-text **Structured Source** and Git remain the single source of truth on disk.
3. **One Structured Source, Deterministic Multi-Target Compilation**: Write clean Markdown prose + native LaTeX math + declarative figure/table blocks once; compile deterministically to the live five-view workbench, journal-ready Word (`.docx` Clean + strict ECMA-376 native Track Changes + line-numbered response letters), and standard `.tex` (Clean + AST-native `latexdiff`-compatible Marked `.tex`).

---

## Installation (`0.1.0.dev0`)

`oh-my-manuscript` is currently at `0.1.0.dev0` and is **not yet published to PyPI**. Install from source (requires Python `>= 3.12` with [`uv`](https://docs.astral.sh/uv/), Node.js `>= 22`, and Git):

```sh
git clone https://github.com/Dsssyc/oh-my-manuscript.git
cd oh-my-manuscript

# 1. Build the local React workbench bundle
npm --prefix web install
npm --prefix web run build

# 2. Sync Python dependencies and materialize the isolated runtime & global Skill
uv sync
uv run omm install
```

`omm install` materializes a content-addressed, self-contained runtime and installs the shared global Agent Skill once:

- **Canonical Skill & launcher**:
  - macOS / Linux: `~/.agents/skills/oh-my-manuscript/scripts/omm`
  - Windows: `%USERPROFILE%\.agents\skills\oh-my-manuscript\scripts\omm.cmd`
- **Automatic coding-agent links** (symlinks on POSIX, NTFS junctions via `mklink /J` on Windows): linked into `~/.claude/skills/oh-my-manuscript` and `~/.gemini/config/skills/oh-my-manuscript` when those host directories exist, without overwriting existing conflicting assets.

Once `0.1` is published to PyPI, `uvx oh-my-manuscript@<version> install` will provide the same one-command setup.

---

## `0.1` Workflow & CLI

Each paper's **Manuscript Workspace** stays self-contained inside its own Git repository, while all workspaces share the single-instance loopback workbench daemon at `127.0.0.1:49638`. All commands accept `--lang en|zh-CN`; workspace commands accept `--cwd PATH` and `--json` where applicable.

```sh
# Initialize a new Manuscript Workspace and open the five-view workbench
omm init --title "My Paper"
omm stamp
omm check
omm sync
omm console

# Tag a Submission Milestone, revise, sync, and build Clean + Marked deliverables
git tag v1-submitted refs/omm/published
# ... edit Structured Source and response letter ...
omm sync
omm build
```

### Workspace Commands

| Command | Purpose |
| :--- | :--- |
| `omm init [--title TITLE]` | Create and register a Git-backed **Manuscript Workspace** (`manuscript.toml`, `sections/`, `references.bib`, `.omm/`). |
| `omm register` / `omm relocate [--replace]` | Register an existing checkout or update a moved workspace's registered path. |
| `omm stamp` | Assign persistent `{#block_id}` anchors to newly written addressable blocks. |
| `omm check` | Validate **Structured Source**, unexpanded **Outline Blocks**, **Annotations**, broken `block_id`/citation references, and figure assets. |
| `omm sync [--expected-rev REV] [--json]` | Validate and publish a new Git snapshot to `refs/omm/published`, switching the workbench view atomically without mid-edit flicker. |
| `omm checkpoint [--revision REV] [--json]` | Advance the `Last Checkpoint` reference for three-tier diff review. |
| `omm annotate <list\|create\|resolve\|reopen\|delete>` | Manage localized **Annotations** (whole block, text span, figure region, or table cell) and freeze **Resolution Snapshots** on resolve. |
| `omm build [--target docx\|latex\|all] ...` | Compile deterministic Clean and Marked `.docx` / `.tex` submission packages and resolve `[#loc:...]` page/line references. |

### Runtime & Workbench Commands

| Command | Purpose |
| :--- | :--- |
| `omm install` | Materialize the isolated runtime, launcher scripts, and shared Agent Skill links. |
| `omm console [--cwd PATH]` | Ensure the windowless single-instance workbench daemon (`127.0.0.1:49638`) is running and print its local URL (five views: Manuscript, Appendices, Review Items, Figures, References). |
| `omm health` / `omm runtime` / `omm paths` | Inspect daemon health, active runtime isolation (`source_leak: false`), and global paths. |
| `omm restart` / `omm stop` | Restart the workbench onto the active runtime or cleanly stop the daemon. |

---

## Manuscript Workspace & Structured Source

A workspace contains `manuscript.toml`, UTF-8 Markdown **Structured Source** files, `references.bib`, figure assets, and `.omm/` (for local annotations and default `.omm/build/` outputs).

| Construct | Syntax |
| :--- | :--- |
| Addressable block | Append `{#block-id}` to headings, paragraphs, lists, or display equations (`$$ ... $$ {#eq-id}` or `{#eq-id unnumbered}`). |
| Directive blocks | `:::{outline} {#out-id}`, `:::{figure} {#fig-id cols=2}`, `:::{table} {#tbl-id}` (canonical three-line table with `>` / `^` spans and `---` midrules), closed with `:::`. |
| **Review Item** | `:::{review-item} {#R1-C1 reviewer="Reviewer 1"}` starting with `:::{comment} ... :::` followed by stamped reply blocks. |
| Cross-references & citations | Logical block ref `[#block-id]`, response-letter physical location `[#loc:p-id]` or `[#loc:p-start..p-end]`, BibTeX `[@key1; @key2]` or narrative `@key`. |

See [src/omm/skill_bundle/references/syntax-and-cli.md](./src/omm/skill_bundle/references/syntax-and-cli.md) for full CLI flags and error codes, [docs/GLOSSARY.md](./docs/GLOSSARY.md) for canonical bilingual terms, and [ADR-001](./docs/adr/ADR-001-core-representation-and-contracts.md) / [ADR-002](./docs/adr/ADR-002-global-skill-runtime-and-daemon-contract.md) for architectural contracts.

---

## Submission Builds (`omm build`)

- **Clean & Marked deliverables**: Defaults to `--target all` (`docx` + `latex`) writing into `.omm/build/` (custom inside-workspace `--out-dir` paths must live under `.omm/<subdir>` to protect source files). By default, `omm build` reads `refs/omm/published` (requiring a clean sync, or an explicit `--revision REV`) and automatically compares against the latest ancestral `v*-submitted` **Submission Milestone** tag (override with `--milestone REV`) to emit `*.clean.*` and `*.marked.*` files.
- **Physical page/line backfill (`[#loc:...]`)**:
  - When a response letter uses `[#loc:...]`, supply line-numbered PDFs exported from the corresponding Clean deliverables. With `--target all`, qualify each PDF by target format:
    ```sh
    omm build \
      --layout-pdf docx:manuscript=word-numbered.pdf \
      --layout-pdf latex:manuscript=tex-numbered.pdf
    ```
    With a single target (`--target docx` or `--target latex`), the prefix is optional (`--layout-pdf manuscript=numbered.pdf`).
  - If `--layout-pdf` is omitted for a referenced deliverable, Word targets invoke headless LibreOffice (`soffice`) and LaTeX targets run a headless TeX engine (`lineno` `.aux` labels). If neither a layout PDF nor a layout engine is available—or if a target block has no independent line number—compilation fails explicitly at the boundary rather than estimating line numbers.
  - Workspaces without `[#loc:...]` references require neither LibreOffice nor TeX.

---

## Project Status & Verification Boundary

All four stages of `0.1` are implemented on `main` (`0.1.0.dev0`): Structured Source parsing/validation/stamping, explicit `omm sync`, three-tier Checkpoint diffing, localized Annotations, the five-view React workbench & windowless loopback daemon, global shared Skill installation, and Clean/Marked Word `.docx` + LaTeX `.tex` compilation with `[#loc:...]` resolution.

**Verified in automated & isolated runs:**

- **macOS (Python 3.13, Node.js 22)**: `uv run ruff check` clean; `uv run pytest -q` (`505 passed, 1 skipped`—only the Windows-specific NTFS junction test is skipped); `npm --prefix web run typecheck` and production `vite build`; isolated empty-`HOME` offline wheel `omm install` and `scripts/omm build` end-to-end.
- **Windows 11 x64 NTFS (Python 3.12, default `LongPathsEnabled=0`)**: Native run of `uv run ruff check` and `uv run pytest -q` (`0 failed, 0 skipped`, covering `>260`-character runtime installation and native extension import via `_omm.pth`), plus offline non-ASCII/space path `omm.cmd install` (`chcp 1252`, `PYTHONUTF8=1`) and `omm.cmd build` E2E with NTFS junctions.
- **Word OOXML & Track Changes**: Every generated `.docx` passes the built-in strict OPC/WML package validator (`verify_docx_package`), acceptance fixture XML parts were validated against official [ECMA-376](https://ecma-international.org/publications-and-standards/standards/ecma-376/) XSD schemas, and automated tests verify bidirectional Accept-All / Reject-All projection equivalence against Clean builds.

**Explicit boundaries & upcoming work:**

- **Packaging**: Not yet published to PyPI; install from source for now.
- **OS coverage**: Native Linux test runs have not yet been executed.
- **External typesetting engines**: Location resolution is tested with positioned PDF byte fixtures and mock launchers; automated checks do not run desktop Microsoft Word, LibreOffice, or TeX Live. Review final fonts, pagination, and tracked revisions in desktop Word or your TeX toolchain before journal submission.
- **Planned for `0.2`**: Legacy `.docx` manuscript ingestion (`omm ingest`).

---

## Development

Requirements: Python `>= 3.12` (`uv`), Node.js `>= 22`, and Git.

```sh
# Install dependencies and run Python lint & test suite
uv sync
uv run ruff check
uv run pytest

# Run CLI directly from checkout without dispatching to an installed runtime
OMM_DEV_SOURCE=1 uv run omm --help

# Typecheck & build Web workbench (outputs to src/omm/_web_dist/)
npm --prefix web install
npm --prefix web run typecheck
npm --prefix web run build
```
