import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { api, LAST_CHECKPOINT, type Annotation, type Milestone, type Outline, type Draft, type Selector, type WorkspaceEvent } from "./api";
import { AnnotationComposer, AnnotationsDrawer } from "./Annotations";
import { ErrorNotice } from "./ErrorNotice";
import { Reader, type ReadingContext } from "./Inline";
import type { Translate } from "./i18n";
import { DeletedBlocks, FiguresLayout, ReadingLayout, ReferencesLayout } from "./Layouts";
import { indexSnapshot, TABS, type Publication, type Tab, type TargetRange } from "./manuscript";
import { selectionRegion, textSelector } from "./selection";
import { useRevision } from "./useRevision";

interface Props {
  workspaceId: string;
  summaryUnsynced?: boolean;
  t: Translate;
  listen: (callback: (event: WorkspaceEvent) => void) => () => void;
}

type ScrollAnchor = { pane: HTMLElement; id: string; publication?: string; offset: number };
type Destination = { id: string; publication?: string; pane: "main" | "target"; citation?: boolean };
type Baseline = "off" | "checkpoint" | "resolution" | "milestone";

function publicationOf(element: HTMLElement) {
  return element.closest<HTMLElement>("[data-publication]")?.dataset.publication;
}

function blockElements(pane: HTMLElement) {
  return [...pane.querySelectorAll<HTMLElement>("[data-block-id]")];
}

function locateElement(pane: HTMLElement, id: string, publication?: string) {
  return blockElements(pane).find((element) => element.dataset.blockId === id &&
    (!publication || publicationOf(element) === publication));
}

export function Workbench({ workspaceId, summaryUnsynced, t, listen }: Props) {
  const [tab, setTab] = useState<Tab>("manuscript");
  const [baseline, setBaseline] = useState<Baseline>("off");
  const [milestone, setMilestone] = useState<Milestone>();
  const [split, setSplit] = useState<TargetRange>();
  const [destination, setDestination] = useState<Destination>();
  const [activeRefs, setActiveRefs] = useState<ReadonlySet<string>>(new Set());
  const [highlighted, setHighlighted] = useState<ReadonlySet<string>>(new Set());
  const [activeBlock, setActiveBlock] = useState<string>();
  const [drawer, setDrawer] = useState(false);
  const [selectedAnnotation, setSelectedAnnotation] = useState<string>();
  const [draft, setDraft] = useState<Draft>();
  const [outline, setOutline] = useState<{ id: string; publicationId?: string; body: string; base: string }>();
  const [selecting, setSelecting] = useState(false);
  const [drawing, setDrawing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const [checkpoint, setCheckpoint] = useState<string>();
  const mainPane = useRef<HTMLDivElement>(null);
  const targetPane = useRef<HTMLDivElement>(null);
  const working = useRef(false);
  const interaction = useRef(false);
  const anchors = useRef<ScrollAnchor[]>([]);
  const handleError = useCallback((value: unknown) => setError(value), []);

  const capture = useCallback(() => {
    anchors.current = [mainPane.current, targetPane.current].flatMap((pane) => {
      if (!pane) return [];
      const bounds = pane.getBoundingClientRect();
      const visible = blockElements(pane).filter((element) => {
        const rect = element.getBoundingClientRect();
        return rect.bottom > bounds.top && rect.top < bounds.bottom;
      });
      const first = visible.filter((element) => element.getBoundingClientRect().top <= bounds.top).at(-1) ?? visible[0];
      return first?.dataset.blockId ? [{
        pane,
        id: first.dataset.blockId,
        publication: publicationOf(first),
        offset: first.getBoundingClientRect().top - bounds.top,
      }] : [];
    });
  }, []);

  const diffBase = baseline === "checkpoint" ? LAST_CHECKPOINT : baseline === "milestone" ? milestone?.revision : undefined;
  const revision = useRevision(workspaceId, diffBase, Boolean(draft || outline || selecting || drawing), capture, handleError);
  const { snapshot } = revision;
  const index = useMemo(() => snapshot && indexSnapshot(snapshot), [snapshot]);
  const eventHandler = useRef(revision.receive);
  eventHandler.current = revision.receive;
  useEffect(() => listen((event) => eventHandler.current(event)), [listen]);

  useLayoutEffect(() => {
    const saved = anchors.current;
    anchors.current = [];
    for (const { pane, id, offset, publication } of saved) {
      if (!pane.isConnected) continue;
      const block = locateElement(pane, id, publication) ?? locateElement(pane, id);
      if (block) pane.scrollTop += block.getBoundingClientRect().top - pane.getBoundingClientRect().top - offset;
    }
  }, [snapshot]);

  useLayoutEffect(() => {
    if (!destination) return;
    const pane = destination.pane === "main" ? mainPane.current : targetPane.current;
    if (!pane) return;
    const element = destination.citation
      ? [...pane.querySelectorAll<HTMLElement>("[data-citekey]")].find((node) => node.dataset.citekey === destination.id)
      : locateElement(pane, destination.id, destination.publication);
    if (!element) return;
    const behavior = window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
    pane.scrollTo({ top: pane.scrollTop + element.getBoundingClientRect().top - pane.getBoundingClientRect().top - 22, behavior });
  }, [destination]);

  function openAnnotation(id: string, selector: Selector) {
    if (!snapshot || outline || draft) return;
    setActiveBlock(id);
    setDraft({ blockId: id, baseRev: snapshot.revision, selector });
  }

  const selectionHandler = useRef<(commit: boolean) => void>(() => undefined);
  selectionHandler.current = (commit) => {
    const selection = document.getSelection();
    const region = selectionRegion(selection);
    const id = region?.dataset.textRegion;
    const belongs = region && Boolean(mainPane.current?.contains(region) || targetPane.current?.contains(region));
    if (!id || !belongs || !selection) {
      setSelecting(interaction.current);
      return;
    }
    if (!commit) {
      setSelecting(true);
      return;
    }
    const block = index?.locate(id)?.block;
    const selector = block && textSelector(region, block.clean_source, selection);
    if (selector && !draft && !outline) {
      openAnnotation(id, selector);
      selection.removeAllRanges();
    }
    setSelecting(false);
  };

  useEffect(() => {
    const begin = (event: PointerEvent) => {
      const region = (event.target as Element | null)?.closest?.("[data-text-region]");
      if (event.button === 0 && region && (mainPane.current?.contains(region) || targetPane.current?.contains(region))) {
        interaction.current = true;
        setSelecting(true);
      }
    };
    const end = () => {
      interaction.current = false;
      selectionHandler.current(true);
    };
    const change = () => selectionHandler.current(false);
    const key = (event: KeyboardEvent) => {
      if (event.key === "Shift" || (event.shiftKey && (event.key.startsWith("Arrow") || ["End", "Home"].includes(event.key)))) end();
    };
    window.addEventListener("pointerdown", begin, true);
    window.addEventListener("pointerup", end);
    window.addEventListener("mouseup", end);
    window.addEventListener("pointercancel", end);
    window.addEventListener("keyup", key);
    document.addEventListener("selectionchange", change);
    return () => {
      window.removeEventListener("pointerdown", begin, true);
      window.removeEventListener("pointerup", end);
      window.removeEventListener("mouseup", end);
      window.removeEventListener("pointercancel", end);
      window.removeEventListener("keyup", key);
      document.removeEventListener("selectionchange", change);
    };
  }, []);

  async function run(action: () => Promise<unknown>) {
    if (working.current) return;
    working.current = true;
    setBusy(true);
    setError(undefined);
    try {
      await action();
    } catch (reason) {
      handleError(reason);
    } finally {
      working.current = false;
      setBusy(false);
    }
  }

  function sync() {
    void run(async () => {
      const signal = revision.lastPublication.current;
      const status = revision.lastStatus.current;
      const result = await api.sync(workspaceId, snapshot?.revision ?? null);
      if (status === revision.lastStatus.current) revision.clearUnsynced();
      if (signal === revision.lastPublication.current) revision.publish(result.revision);
    });
  }

  function saveOutline(body: string) {
    if (!outline) return;
    void run(async () => {
      const signal = revision.lastPublication.current;
      const result = await api.outline(workspaceId, outline.id, outline.base, body);
      if (signal === revision.lastPublication.current) revision.publish(result.revision);
      setOutline(undefined);
    });
  }

  function changeTab(next: Tab) {
    if (outline || draft || tab === next) return;
    setTab(next);
    setSplit(undefined);
    setActiveRefs(new Set());
    setHighlighted(new Set());
    mainPane.current?.scrollTo({ top: 0, behavior: "instant" });
  }

  function jump(id: string, preferred?: Publication) {
    if (outline || draft) return;
    const found = index?.locate(id, preferred);
    if (!found) {
      handleError(new Error(t("error.target")));
      return;
    }
    setTab(found.publication.tab);
    setSplit(undefined);
    setHighlighted(new Set([id]));
    setActiveBlock(id);
    setActiveRefs(new Set());
    setDestination({ id, publication: found.publication.id, pane: "main" });
  }

  function openRange(target: TargetRange) {
    if (outline || draft) return;
    const publication = index?.locateRange(target)?.publication;
    const ids = index?.rangeIds(target) ?? [];
    if (!publication || publication.tab === "reviews" || !ids.length) {
      handleError(new Error(t("error.target")));
      return;
    }
    setTab("reviews");
    setSplit(target);
    setHighlighted(new Set(ids));
    setActiveBlock(target.start);
    setDestination({ id: target.start, publication: publication.id, pane: "target" });
  }

  function showReference(id: string, related = [id]) {
    if (outline || draft) return;
    setTab("references");
    setSplit(undefined);
    setActiveRefs(new Set(related));
    setDestination({ id, pane: "main", citation: true });
  }

  function selectAnnotation(id: string) {
    setDrawer(true);
    setSelectedAnnotation(id);
  }

  const target = split && index?.locateRange(split)?.publication;
  const resolution = baseline === "resolution" &&
    snapshot?.annotations.some((annotation) => annotation.id === selectedAnnotation && annotation.resolutions.length)
    ? selectedAnnotation
    : undefined;
  const currentHighlights = split && index ? new Set(index.rangeIds(split)) : highlighted;

  const context: ReadingContext | undefined = snapshot && index ? {
    workspaceId, snapshot, index, t, jump, openRange, showReference,
    annotate: openAnnotation, focusBlock: setActiveBlock, drawing: setDrawing, highlighted: currentHighlights,
    selectAnnotation, selectedAnnotation, resolutionAnnotation: resolution, showDiff: Boolean(diffBase && revision.activeBaseline === diffBase),
    editOutline: (block: Outline, publicationId) => {
      if (block.block_id && !draft && !outline) {
        setOutline({ id: block.block_id, publicationId, body: block.body_markdown, base: snapshot.revision });
        setActiveBlock(block.block_id);
      }
    },
    outlineEditor: outline ? { ...outline, busy, save: saveOutline, cancel: () => setOutline(undefined) } : null,
  } : undefined;

  const milestones = snapshot?.milestones ?? [];
  const firstMilestone = milestones[0];
  return <div className="workbench" style={{ "--academic-link": snapshot?.style.link_color || "#0070C0" } as CSSProperties}>
    <div className="command-bar">
      {snapshot && <span className="revision" title={snapshot.revision}>
        {t("workspace.revision", { revision: snapshot.revision.slice(0, 7) })}
      </span>}
      {(revision.unsynced ?? summaryUnsynced) && <span className="badge unsynced">{t("workspace.unsynced")}</span>}
      <div className="publication-actions">
        <button type="button" disabled={busy || (!snapshot && !revision.unpublished)} onClick={sync}>{t("workspace.sync")}</button>
        <button type="button" disabled={busy || !snapshot} onClick={() => {
          if (!snapshot) return;
          void run(async () => {
            const result = await api.checkpoint(workspaceId, snapshot.revision);
            setCheckpoint(result.checkpoint);
            revision.refresh();
          });
        }}>{t("workspace.checkpoint")}</button>
      </div>
      <div className="comparison-controls">
        {checkpoint && <span className="note" role="status">{t("workspace.checkpoint_set", { revision: checkpoint.slice(0, 7) })}</span>}
        <label className="control">{t("diff.label")}
          <select value={baseline} disabled={!snapshot} onChange={(event) => {
            const next = event.target.value as Baseline;
            setBaseline(next);
            if (next === "resolution") setDrawer(true);
            if (next === "milestone" && !milestone) setMilestone(firstMilestone);
          }}>
            <option value="off">{t("diff.off")}</option>
            <option value="checkpoint" disabled={!snapshot?.checkpoint_rev}>{t("diff.last_checkpoint")}</option>
            <option value="resolution">{t("diff.resolution_snapshot")}</option>
            <option value="milestone" disabled={!milestones.length}>{t("diff.submission_milestone")}</option>
          </select>
        </label>
        {baseline === "milestone" && <select aria-label={t("diff.submission_milestone")} value={milestone?.name ?? ""}
          onChange={(event) => setMilestone(milestones.find(({ name }) => name === event.target.value))}>
          {milestones.map(({ name }) => <option key={name} value={name}>{name}</option>)}
        </select>}
        <button type="button" className="annotation-toggle" disabled={!snapshot} aria-expanded={drawer} onClick={() => setDrawer(!drawer)}>
          {t("annotation.title")} {snapshot && <span className="count">{snapshot.annotations.length}</span>}
        </button>
      </div>
    </div>
    <ErrorNotice error={error} t={t} dismiss={() => setError(undefined)} />
    {revision.pending && <div role="status" className="notice queued">{t("workspace.queued")}</div>}
    {baseline === "resolution" && !resolution && <div role="status" className="notice">{t("diff.select_resolution")}</div>}
    <nav className="tabs" aria-label={t("workspace.label")}>
      {TABS.map((value) => <button key={value} type="button" aria-current={tab === value ? "page" : undefined}
        disabled={Boolean(outline || draft) && tab !== value} onClick={() => changeTab(value)}>{t(`tab.${value}`)}</button>)}
    </nav>
    {context ? <Reader.Provider value={context}>
      <div className="work-area">
        <main className={`stage ${target ? "split" : ""}`}>
          <div className="read-pane" ref={mainPane}>
            {tab === "figures" ? <FiguresLayout /> : tab === "references" ? <ReferencesLayout activeKeys={activeRefs} clearActive={() => setActiveRefs(new Set())} /> :
              <ReadingLayout tab={tab} />}
            <DeletedBlocks />
          </div>
          {target && <section className="target-pane" aria-label={t("review.target")}>
            <header><strong>{t(`tab.${target.tab}`)}</strong>
              <button type="button" className="quiet" disabled={Boolean(outline)} onClick={() => {
                setSplit(undefined);
                setHighlighted(new Set());
              }}>{t("review.close_split")}</button>
            </header>
            <div className="read-pane" ref={targetPane}><ReadingLayout tab={target.tab} publication={target} /></div>
          </section>}
        </main>
        {drawer && <AnnotationsDrawer current={activeBlock} busy={busy} resolutionMode={baseline === "resolution"}
          onError={handleError} close={() => setDrawer(false)} compare={(annotation) => {
            setBaseline("resolution");
            setSelectedAnnotation(annotation.id);
          }} act={(action, annotation: Annotation) => void run(async () => {
            if (action === "resolve") await api.resolve(workspaceId, annotation.id, context.snapshot.revision);
            if (action === "reopen") await api.reopen(workspaceId, annotation.id);
            if (action === "delete") {
              await api.delete(workspaceId, annotation.id);
              if (annotation.id === selectedAnnotation) setSelectedAnnotation(undefined);
            }
            revision.refresh();
          })} />}
      </div>
    </Reader.Provider> : <main className="empty">
      {revision.loading && <p role="status">{t("workspace.loading")}</p>}
      {revision.unpublished && <p>{t("workspace.unpublished")}</p>}
      {!revision.loading && !revision.unpublished && <button type="button" onClick={() => {
        setError(undefined);
        revision.refresh();
      }}>{t("workspace.retry")}</button>}
    </main>}
    {draft && <AnnotationComposer draft={draft} t={t} busy={busy} cancel={() => setDraft(undefined)}
      submit={(comment) => void run(async () => {
        await api.annotate(workspaceId, draft, comment);
        setDrawer(true);
        setDraft(undefined);
        revision.refresh();
      })} />}
  </div>;
}
