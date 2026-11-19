import { act, useLayoutEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { api, PUBLISHED, type Annotation, type PublishedSnapshot, type WorkspaceSnapshot } from "./api";
import { useRevision } from "./useRevision";

function snapshot(revision: string, annotations: Annotation[] = []): PublishedSnapshot {
  return {
    workspace_id: "workspace", title: "Manuscript", authors: "", published: true,
    published_rev: revision, revision, checkpoint_rev: null, has_unsynced_changes: false, milestones: [],
    style: { journal: "academic-default", link_color: "#0070C0", citation_style: "author-year", bibliography_placement: "per-deliverable" },
    deliverables: [], blocks: [], references: { entries: [] }, annotations, diff: null, review_target_index: {},
  };
}

function expect(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

// Run in a DOM test environment with React act enabled.
export async function testRefreshInvalidatesPrefetch() {
  const r2 = snapshot("2".repeat(40));
  const r3 = snapshot("3".repeat(40));
  const newAnnotation: Annotation = {
    id: "annotation", block_id: "p-first", base_rev: r2.revision, selector: { kind: "block" },
    comment: "Check this passage", status: "open", anchor: "exact",
    created_at: "2026-11-18T00:00:00Z", before_block_md: "Passage", resolutions: [],
  };
  const refreshed = { ...r3, annotations: [newAnnotation] };
  const originalSnapshot = api.snapshot;
  let refreshing = false;
  let respond: ((data: WorkspaceSnapshot) => void) | undefined;
  api.snapshot = async (_, revision) => {
    if (revision === PUBLISHED) return r2;
    if (!refreshing) return r3;
    return new Promise((resolve) => { respond = resolve; });
  };

  let readState: () => ReturnType<typeof useRevision>;
  let openDraft: () => void;
  let endDraftAndRefresh: () => void;
  const noop = () => undefined;
  const rethrow = (error: unknown) => { throw error; };

  function Harness() {
    const [editing, setEditing] = useState(false);
    const state = useRevision(r2.workspace_id, undefined, editing, noop, rethrow);
    useLayoutEffect(() => {
      readState = () => state;
      openDraft = () => setEditing(true);
      endDraftAndRefresh = () => {
        setEditing(false);
        state.refresh();
      };
    });
    return null;
  }

  const root = createRoot(document.createElement("div"));
  try {
    await act(async () => root.render(<Harness />));
    expect(readState!().snapshot === r2, "Expected R2 to be displayed initially");

    await act(async () => openDraft!());
    await act(async () => readState!().receive({
      type: "revision_published", workspace_id: r2.workspace_id, revision: r3.revision,
    }));
    expect(readState!().pending && readState!().snapshot === r2, "Expected R3 to wait for the draft");

    refreshing = true;
    await act(async () => endDraftAndRefresh!());
    expect(respond, "Expected a fresh R3 request after saving the annotation");
    expect(readState!().snapshot === r2, "Displayed stale R3 while the post-submit request was still pending");

    await act(async () => respond!(refreshed));
    expect(readState!().snapshot === refreshed, "Expected R3 with the new annotation to be displayed");
  } finally {
    await act(async () => root.unmount());
    api.snapshot = originalSnapshot;
  }
}
