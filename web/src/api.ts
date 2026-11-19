export type Span = [number, number];

interface InlineBase {
  span: Span;
}

export type LocationReference = InlineBase & {
  kind: "loc_ref";
  atomic: true;
  start_id: string;
  end_id: string | null;
  target_deliverable: string | null;
  label: string;
};

export type SerializedInline = InlineBase & (
  | { kind: "text" | "code_span"; value: string }
  | { kind: "line_break"; hard: boolean }
  | { kind: "strong" | "emphasis" | "subscript" | "superscript"; children: SerializedInline[] }
  | { kind: "link"; children: SerializedInline[]; destination: string; title: string | null }
  | { kind: "image"; alt: string; destination: string; title: string | null }
  | { kind: "inline_math"; atomic: true; latex: string }
  | { kind: "block_ref"; atomic: true; target_id: string; target_kind: Block["kind"] | null; target_deliverable: string | null; label: string }
  | LocationReference
  | { kind: "citation"; atomic: true; keys: string[]; parenthetical: boolean; label: string }
);

export interface Content {
  inlines: SerializedInline[];
  span: Span;
}

export interface TableCell extends Content {
  row_span: number;
  col_span: number;
  covered: boolean;
}

interface BlockBase {
  block_id: string | null;
  source: string;
  deliverables: string[];
  line: number;
  clean_source: string;
  number: string | null;
  label: string | null;
  asset_sha256: string | null;
}

export type Subfigure = BlockBase & { kind: "subfigure"; alt: string; asset_path: string; sublabel: string | null };
export type Figure = BlockBase & {
  kind: "figure";
  subfigures: Subfigure[];
  caption: Content;
  cols: number | null;
};
export type Table = BlockBase & {
  kind: "table";
  caption: Content;
  alignment: ("left" | "center" | "right" | null)[];
  header: TableCell[][];
  rows: TableCell[][];
  midrule_after: number[];
  notes: Content[];
};
export type ReviewItem = BlockBase & {
  kind: "review_item";
  reviewer: string;
  comment: Content;
  reply: Block[];
  referenced_block_ids: string[];
  location_refs: LocationReference[];
};
export type Outline = BlockBase & { kind: "outline"; body_span: Span; body_markdown: string; inlines: SerializedInline[] };

export type Block = BlockBase & (
  | { kind: "heading"; level: number; inlines: SerializedInline[] }
  | { kind: "paragraph"; inlines: SerializedInline[] }
  | { kind: "list"; ordered: boolean; inlines: SerializedInline[] }
  | { kind: "display_math"; latex: string; unnumbered: boolean }
  | { kind: "markdown"; markdown_kind: string; children: Block[] }
) | Outline | Subfigure | Figure | Table | ReviewItem;

export type Selector =
  | { kind: "block" }
  | { kind: "text"; start: number; end: number; quote: string }
  | { kind: "rect"; x: number; y: number; w: number; h: number; asset_sha256?: string }
  | { kind: "cell"; row: number; col: number };

export interface DiffChunk {
  op: "equal" | "insert" | "delete";
  text: string;
}

export interface BlockDiff {
  block_id: string;
  status: "unchanged" | "added" | "modified" | "moved" | "modified_and_moved" | "deleted";
  before_source: string | null;
  after_source: string | null;
  before_deliverables: string[];
  after_deliverables: string[];
  asset_changed: boolean;
  before_md: string | null;
  after_md: string | null;
  chunks: DiffChunk[];
}

export interface Resolution {
  after_rev: string;
  after_block_md: string | null;
  resolved_at: string;
  reopened_at: string | null;
  diff_chunks: DiffChunk[];
}

export interface Annotation {
  id: string;
  block_id: string;
  base_rev: string;
  selector: Selector;
  comment: string;
  created_at: string;
  status: "open" | "resolved";
  anchor: "exact" | "block_deleted" | "block_modified" | "asset_changed" | "invalidated";
  before_block_md: string;
  resolutions: Resolution[];
}

export interface Milestone {
  name: string;
  revision: string;
}

interface RevisionState {
  workspace_id: string;
  title: string;
  published_rev: string | null;
  checkpoint_rev: string | null;
  has_unsynced_changes: boolean;
  milestones: Milestone[];
}

export interface WorkspaceSummary extends RevisionState {
  path: string;
  exists: boolean;
}

export interface Deliverable {
  name: string;
  kind: "manuscript" | "supplementary" | "response_letter";
  sources: string[];
}

export interface ReferenceEntry {
  citekey: string;
  entry_type: string;
  number: number | null;
  short_label: string;
  formatted_text: string;
  fields: Record<string, string>;
  cited: boolean;
  deliverable_counts: Record<string, number>;
  citing_blocks: { block_id: string | null; deliverable: string; source: string }[];
}

export interface PublishedSnapshot extends RevisionState {
  published: true;
  revision: string;
  authors: string | { name: string; affiliation: string | null; email: string | null; orcid: string | null }[];
  style: {
    journal: string;
    link_color: string;
    citation_style: "numeric" | "author-year";
    bibliography_placement: "per-deliverable" | "merged-in-main";
  };
  deliverables: Deliverable[];
  blocks: Block[];
  references: { entries: ReferenceEntry[] };
  annotations: Annotation[];
  diff: { base_revision: string; target_revision: string; blocks: Record<string, BlockDiff> } | null;
  review_target_index: Record<string, string[]>;
}

export type WorkspaceSnapshot = PublishedSnapshot | (RevisionState & {
  published: false;
  revision: null;
  published_rev: null;
  checkpoint_rev: null;
});

interface HistoricalBlock {
  revision: string;
  block_id: string;
  found: boolean;
  clean_source: string | null;
  kind: Block["kind"] | null;
  source: string | null;
}

export interface Draft {
  blockId: string;
  baseRev: string;
  selector: Selector;
}

declare global {
  interface Window {
    __OMM_BOOTSTRAP__?: { csrfToken?: string };
  }
}

export class ApiError extends Error {
  constructor(readonly code: string, message: string) {
    super(message);
  }
}

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => null);
  if (!response.ok || data === null) {
    const error = data?.error;
    throw new ApiError(error?.code ?? String(response.status), error?.message ?? "");
  }
  return data as T;
}

let bootstrap: Promise<string> | undefined;

async function csrfToken(): Promise<string> {
  const injected = window.__OMM_BOOTSTRAP__?.csrfToken;
  if (injected) return injected;
  bootstrap ??= request<{ csrf_token: string; contract_version: string }>("/api/bootstrap")
    .then((data) => {
      if (!data.csrf_token) throw new ApiError("ERR_CSRF_TOKEN_MISSING", "");
      return data.csrf_token;
    })
    .catch((error: unknown) => {
      bootstrap = undefined;
      throw error;
    });
  return bootstrap;
}

async function write<T>(method: "POST" | "PUT" | "DELETE", url: string, body?: object) {
  return request<T>(url, {
    method,
    headers: { "X-OMM-CSRF": await csrfToken(), "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

const workspaceURL = (id: string) => `/api/workspaces/${encodeURIComponent(id)}`;
const blockURL = (id: string, blockId: string) => `${workspaceURL(id)}/blocks/${encodeURIComponent(blockId)}`;
const annotationURL = (id: string, annotationId: string) =>
  `${workspaceURL(id)}/annotations/${encodeURIComponent(annotationId)}`;

export const PUBLISHED = "refs/omm/published";
export const LAST_CHECKPOINT = "refs/omm/checkpoints/latest";

export const api = {
  workspaces: (signal?: AbortSignal) => request<{ workspaces: WorkspaceSummary[] }>("/api/workspaces", { signal }),
  snapshot(id: string, revision: string, diffBase?: string, signal?: AbortSignal) {
    const query = new URLSearchParams();
    if (revision !== PUBLISHED) query.set("rev", revision);
    if (diffBase) query.set("diff_base", diffBase);
    const suffix = query.size ? `?${query}` : "";
    return request<WorkspaceSnapshot>(`${workspaceURL(id)}/snapshot${suffix}`, { signal });
  },
  asset: (id: string, revision: string, path: string) =>
    `${workspaceURL(id)}/assets?rev=${encodeURIComponent(revision)}&path=${encodeURIComponent(path)}`,
  block: (id: string, blockId: string, revision: string) =>
    request<HistoricalBlock>(`${blockURL(id, blockId)}?rev=${encodeURIComponent(revision)}`),
  sync: (id: string, revision: string | null) =>
    write<{ revision: string; changed: boolean }>("POST", `${workspaceURL(id)}/sync`, { expected_rev: revision }),
  checkpoint: (id: string, revision: string) =>
    write<{ checkpoint: string }>("POST", `${workspaceURL(id)}/checkpoint`, { revision }),
  annotate: (id: string, draft: Draft, comment: string) =>
    write<Annotation>("POST", `${workspaceURL(id)}/annotations`, {
      block_id: draft.blockId, base_rev: draft.baseRev, selector: draft.selector, comment,
    }),
  resolve: (id: string, annotationId: string, revision: string) =>
    write<Annotation>("POST", `${annotationURL(id, annotationId)}/resolve`, { after_rev: revision }),
  reopen: (id: string, annotationId: string) =>
    write<Annotation>("POST", `${annotationURL(id, annotationId)}/reopen`, {}),
  delete: (id: string, annotationId: string) =>
    write<{ id: string }>("DELETE", annotationURL(id, annotationId)),
  outline: (id: string, blockId: string, revision: string, body: string) =>
    write<{ revision: string; changed: boolean }>("PUT",
      `${workspaceURL(id)}/outlines/${encodeURIComponent(blockId)}`, { expected_rev: revision, body_markdown: body }),
};

export type WorkspaceEvent =
  | { type: "reconnected" }
  | { type: "workspaces_changed"; workspace_id?: string }
  | { type: "unsynced_status"; workspace_id: string; has_unsynced_changes: boolean; published_rev: string | null }
  | { type: "revision_published"; workspace_id: string; revision: string }
  | { type: "checkpoint_updated"; workspace_id: string; checkpoint: string }
  | { type: "annotations_changed"; workspace_id: string };

export function subscribe(onEvent: (event: WorkspaceEvent) => void) {
  const source = new EventSource("/api/events");
  let opened = false;
  source.onopen = () => {
    if (opened) onEvent({ type: "reconnected" });
    opened = true;
  };
  source.onmessage = (event: MessageEvent<string>) => {
    let data: Exclude<WorkspaceEvent, { type: "reconnected" }>;
    try {
      data = JSON.parse(event.data);
    } catch {
      return;
    }
    switch (data?.type) {
      case "workspaces_changed":
      case "unsynced_status":
      case "revision_published":
      case "checkpoint_updated":
      case "annotations_changed":
        onEvent(data);
    }
  };
  return () => source.close();
}
