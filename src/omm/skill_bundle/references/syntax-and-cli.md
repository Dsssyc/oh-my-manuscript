# Structured Source and CLI reference

Use the installed `omm` launcher with `--cwd PATH` on Workspace commands or run from the directory containing `manuscript.toml`. Both the main command and its subcommands accept `--lang en|zh-CN`. Use `omm --help` and `omm COMMAND --help` for exact usage.

## Structured Source

| Construct | Syntax |
| --- | --- |
| Addressable Markdown block | Place `{#block-id}` at the end of a heading, paragraph, list, or display equation. Each ID is unique within the Manuscript Workspace. |
| Display equation | Place `{#eq-id}` after the closing `$$`, or use `{#eq-id unnumbered}`. |
| Directive | Put the ID on the opening fence, for example `:::{outline} {#out-method}`, `:::{figure} {#fig-domain cols=2}`, or `:::{table} {#tbl-results}`. Close with `:::`. |
| Three-line table | Write a caption, a pipe grid with equal logical columns, then any notes. Use `>` across the top of a merged rectangle and `^` across its rows below. A row of `---` cells emits a midrule. |
| Review Item | Open with `:::{review-item} {#R1-C1 reviewer="Reviewer 1"}`. Start with the reviewer's `:::{comment}` block, followed by the author's stamped reply blocks. |
| Block reference | Use `[#block-id]`. Outline Blocks cannot be referenced. |
| Location reference | Use `[#loc:p-start]` or `[#loc:p-start..p-end]` in a response letter for page and line backfill from a single target deliverable. |
| Citation | Use `[@key1; @key2]` or narrative `@key`. Define cited works in `references.bib`. |

## Workspace commands

| Command | Effect or JSON output |
| --- | --- |
| `init [--title TITLE]` | Create and register a Manuscript Workspace. |
| `register` | Register the current checkout. |
| `relocate [--replace]` | Update the registered path. `--replace` permits replacement while the previous directory still exists. |
| `stamp` | Assign missing Block IDs. |
| `check` | Check Structured Source and Annotations. Diagnostics go to stderr. |
| `sync [--expected-rev REV] [--json]` | Publish a revision. JSON includes `revision` (Git object ID) and `changed` (boolean). |
| `checkpoint [--revision REV] [--json]` | Advance the Last Checkpoint. JSON includes `checkpoint` (Git object ID). |
| `annotate list [--status open\|resolved] [--block-id ID] [--json]` | Return an array of matching Annotations in JSON mode. |
| `annotate create --block-id ID --comment TEXT [--base-rev REV] [--selector-json JSON] [--id UUID] [--json]` | Create an Annotation. The default selector addresses the whole block. |
| `annotate resolve ID [--after-rev REV] [--json]` | Record a Resolution Snapshot against a published revision. |
| `annotate reopen ID [--json]` | Reopen an Annotation. |
| `annotate delete ID [--json]` | Delete an Annotation. JSON includes its `id`. |
| `build [--target docx\|latex\|all] [--out-dir PATH] [--revision REV] [--milestone REV] [--layout-pdf [TARGET:]DELIVERABLE=PATH]… [--json]` | Compile Clean and, when a Submission Milestone applies, Marked deliverables. |

Annotation JSON includes `id`, `block_id`, `selector`, `comment`, `base_rev`, `created_at`, `before_block_md`, `resolutions`, and derived `status`. See subcommand help for the revision and selector options.

### Submission builds

`omm build` defaults to both formats (`--target all`) and writes to the Workspace's `.omm/build/`. Relative `--out-dir` and layout paths resolve from the directory where you invoke the command. `--cwd` selects the Workspace. Builds from temporary Git worktrees preserve the global Workspace registration.

The default input is `refs/omm/published`. Run `omm sync` before building a changed Workspace, or use `--revision REV` to explicitly build a Git snapshot while editing. `--milestone REV` selects the comparison revision for Marked manuscript and supplementary files. By default, the compiler selects the newest ancestral `v*-submitted` tag strictly before the requested revision. It emits Clean files when there is no eligible tag and requires an explicit milestone when the tags have no unique latest ancestor.

For reply locations, supply a PDF exported from the corresponding Clean deliverable with printed margin line numbers. With `--target all`, qualify each PDF with its format, such as `--layout-pdf docx:manuscript=word-numbered.pdf --layout-pdf latex:manuscript=tex-numbered.pdf`. With a single target, the prefix is optional, so `--target docx --layout-pdf manuscript=word-numbered.pdf` also works. For each referenced deliverable without a supplied PDF for its format, the Word target uses headless LibreOffice (`soffice`) and the LaTeX target uses a headless TeX engine's `.aux` labels. Workspaces without location references in compiled content need neither engine.

The default output directory is `.omm/build`. A custom output directory inside the workspace must be a subdirectory of `.omm`. The workspace root and its ancestors are invalid output directories.

`--json` reports the resolved Git IDs (`revision`, `milestone`), the absolute `out_dir`, and the requested `targets`. `deliverables` is keyed by target and deliverable name, with absolute `clean`, optional `marked` (or `null`), and `assets` paths. `resolved_locations` is keyed by target and then by block ID or `start..end` range.

## Runtime and workbench commands

Each command accepts `--json`. Installing, restarting, and stopping require the author's explicit authorization.

| Command | JSON output |
| --- | --- |
| `install` | `schema_version`, `runtime_id`, `version`, `contract_version`, `python`, `web_dist`, `created` (new materialization), `skill_root`, and `links`. |
| `console [--cwd PATH]` | `pid`, `port`, `runtime_id`, `contract_version`, `url`, `started`, and `workspace_id`. The ID is a UUID for a valid Manuscript Workspace and `null` otherwise. The URL includes the Workspace fragment when the ID is present. |
| `health` | `status` (`ok` or `unavailable`), `port`, `runtime_id`, `contract_version`, `dev_source`, and `source_leak`. A healthy daemon also reports `pid` and `url`. Invalid instance metadata or a persistently unresponsive daemon holding the lock causes an error. |
| `runtime` | `runtime_id`, `python`, `omm_file`, `version`, `contract_version`, `dev_source`, and `source_leak`. |
| `paths` | `omm_home`, `state_dir`, `runtime_root`, `active_runtime`, `skill_root`, and `launcher`. |
| `restart` | `pid`, `port`, `runtime_id`, `contract_version`, `url`, `started`, and `stopped`. The last field indicates whether a previous daemon was stopped. |
| `stop` | `stopped` indicates whether a daemon was stopped. |

Runtime command failures exit nonzero and report a diagnostic on stderr. With `--json`, stdout also contains `code`, `message`, and any diagnostic parameters.

## Troubleshooting

| Error | Action |
| --- | --- |
| `ERR_RUNTIME_NOT_INSTALLED`, `ERR_RUNTIME_INVALID` | Ask the author to authorize installation or repair. Use the installed launcher after installation. |
| `ERR_RUNTIME_SOURCE_LEAK` | Check `paths` and the interpreter's import environment. Use the installed launcher with the active runtime. |
| `ERR_SKILL_TARGET_CONFLICT` | Preserve the existing host asset. Ask its owner to resolve the destination before retrying installation. |
| `ERR_PORT_INVALID`, `ERR_CONSOLE_SETTINGS_INVALID` | Check the port and the schema, locale, and theme in `console-settings.json`. |
| `ERR_PORT_IN_USE` | Identify the listener occupying the configured port. |
| `ERR_DAEMON_START_FAILED` | Check `runtime`, `paths`, and `health` for the configured workbench. |
| `ERR_DAEMON_RUNTIME_MISMATCH` | Ask the author to authorize `restart` to switch the daemon to the active runtime. |
| `ERR_DAEMON_STOP_UNCONFIRMED` | Preserve the instance record for diagnosis and ask the author to check the daemon process and its state directory. |
| `ERR_SYNC_CONFLICT` | Read the current published revision, reconcile changes, and retry `sync`. |
| `ERR_NO_PUBLISHED_REVISION` | Run `omm sync` to publish a revision for the build, Checkpoint, or Annotation. |
| `ERR_BUILD_UNSYNCED_CHANGES` | Publish the disk changes with `omm sync`, or use `--revision REV` to explicitly build a Git snapshot. |
| `ERR_LOCATION_LAYOUT_REQUIRED` | Supply a numbered Clean PDF with `--layout-pdf` or make the target's headless layout engine available. |
| `ERR_LAYOUT_PDF_INVALID` | Check the deliverable name, the PDF's match to the requested Clean revision, and the printed margin line numbers. |
| `ERR_MILESTONE_AMBIGUOUS` | Select the intended Submission Milestone with `--milestone REV`. |
| `ERR_FIGURE_FORMAT_UNSUPPORTED` | Supply a supported raster figure (PNG, JPEG, GIF, BMP, or TIFF) for the Word target. |
| `ERR_DOCX_PACKAGE_INVALID` | Use the failing OOXML validation detail to identify the affected part. Report reproducible compiler failures with the relevant Structured Source and assets. |
