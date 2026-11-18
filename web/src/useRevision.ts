import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, ApiError, PUBLISHED, type PublishedSnapshot, type WorkspaceEvent, type WorkspaceSnapshot } from "./api";

interface Loaded {
  snapshot: PublishedSnapshot;
  baseline?: string;
}

interface RevisionRequest {
  revision: string;
}

interface Candidate {
  loaded: Loaded;
  request: RevisionRequest;
  requestedBaseline?: string;
}

export function useRevision(id: string, baseline: string | undefined, editing: boolean,
  beforeSwap: () => void, onError: (error: unknown) => void) {
  const [displayed, setDisplayed] = useState<Loaded>();
  const [candidate, setCandidate] = useState<Candidate>();
  const [requested, setRequested] = useState<RevisionRequest>({ revision: PUBLISHED });
  const [loading, setLoading] = useState(true);
  const [unpublished, setUnpublished] = useState(false);
  const [unsynced, setUnsynced] = useState<boolean>();
  const lastStatus = useRef(0);
  const lastPublication = useRef(0);

  const refresh = useCallback(() => setRequested(({ revision }) => ({ revision })), []);
  const publish = useCallback((revision: string, force = false) => {
    setRequested((current) => force || current.revision !== revision ? { revision } : current);
  }, []);

  function receive(event: WorkspaceEvent) {
    if (event.type === "reconnected") {
      // Events missed while disconnected are reconciled against the published ref.
      publish(PUBLISHED, true);
      return;
    }
    if (!("workspace_id" in event) || event.workspace_id !== id) return;
    switch (event.type) {
      case "unsynced_status":
        lastStatus.current += 1;
        setUnsynced(event.has_unsynced_changes);
        break;
      case "revision_published":
        lastPublication.current += 1;
        publish(event.revision);
        break;
      case "checkpoint_updated":
      case "annotations_changed":
        refresh();
        break;
    }
  }

  useEffect(() => {
    const controller = new AbortController();
    const statusVersion = lastStatus.current;
    const { signal } = controller;
    setLoading(true);

    async function load() {
      let diffBase = baseline;
      let snapshot: WorkspaceSnapshot;
      try {
        snapshot = await api.snapshot(id, requested.revision, baseline, signal);
      } catch (error) {
        if (!baseline || !(error instanceof ApiError) ||
            error.code !== "ERR_REVISION_NOT_FOUND") throw error;
        snapshot = await api.snapshot(id, requested.revision, undefined, signal);
        diffBase = undefined;
        if (!signal.aborted) onError(error);
      }
      if (signal.aborted) return;
      if (statusVersion === lastStatus.current) setUnsynced(snapshot.has_unsynced_changes);
      setUnpublished(!snapshot.published);
      if (!snapshot.published) {
        setCandidate(undefined);
        return;
      }
      setCandidate({ loaded: { snapshot, baseline: diffBase }, request: requested, requestedBaseline: baseline });
    }

    void load().catch((error: unknown) => {
      if (signal.aborted) return;
      onError(error);
    }).finally(() => {
      if (!signal.aborted) setLoading(false);
    });
    return () => controller.abort();
  }, [id, baseline, requested, onError]);

  // A refresh can request the same revision with different annotation state.
  const ready = candidate?.request === requested && candidate.requestedBaseline === baseline
    ? candidate.loaded
    : undefined;
  const pending = Boolean(editing && displayed && candidate?.request.revision === requested.revision &&
    candidate.requestedBaseline === baseline && candidate.loaded.snapshot.revision !== displayed.snapshot.revision);

  useLayoutEffect(() => {
    if (editing || !ready || ready === displayed) return;
    if (displayed) beforeSwap();
    setDisplayed(ready);
  }, [ready, displayed, editing, beforeSwap]);

  return {
    snapshot: displayed?.snapshot,
    activeBaseline: displayed?.baseline,
    pending,
    loading,
    unpublished,
    unsynced,
    receive,
    refresh,
    publish,
    lastPublication,
    lastStatus,
    clearUnsynced: () => setUnsynced(false),
  };
}
