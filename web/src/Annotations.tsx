import { useEffect, useRef, useState } from "react";
import { api, type Annotation, type Draft } from "./api";
import { ResolutionHistory } from "./Blocks";
import { useReader } from "./Inline";
import { blockLabel } from "./manuscript";
import type { Translate } from "./i18n";

type Action = "resolve" | "reopen" | "delete";

interface DrawerProps {
  current?: string;
  busy: boolean;
  resolutionMode: boolean;
  close: () => void;
  act: (action: Action, annotation: Annotation) => void;
  compare: (annotation: Annotation) => void;
  onError: (error: unknown) => void;
}

function EarlierSource({ annotation, onError }: { annotation: Annotation; onError: DrawerProps["onError"] }) {
  const { workspaceId, t } = useReader();
  const [source, setSource] = useState(annotation.before_block_md);
  const [loading, setLoading] = useState(false);
  return <details onToggle={(event) => {
    if (!event.currentTarget.open || source || loading) return;
    setLoading(true);
    void api.block(workspaceId, annotation.block_id, annotation.base_rev).then((result) => {
      if (!result.found || result.clean_source === null) throw new Error(t("error.history"));
      setSource(result.clean_source);
    }).catch(onError).finally(() => setLoading(false));
  }}>
    <summary>{t("annotation.before")}</summary>
    {loading && <p role="status">{t("workspace.loading")}</p>}
    {source !== undefined && <pre>{source}</pre>}
  </details>;
}

function AnnotationCard({ annotation, busy, resolutionMode, act, compare, onError }: Omit<DrawerProps, "current" | "close"> & {
  annotation: Annotation;
}) {
  const { t, index, jump, selectedAnnotation } = useReader();
  const [confirming, setConfirming] = useState(false);
  const isSelected = selectedAnnotation === annotation.id;
  return <article className="annotation-card" data-annotation-id={annotation.id} data-selected={isSelected || undefined}>
    <header>
      <button type="button" className="link-button" onClick={() => jump(annotation.block_id)} disabled={annotation.anchor === "block_deleted"}>
        {blockLabel(t, index.locate(annotation.block_id), annotation.block_id)}
      </button>
      <span className="badge">{t(`annotation.${annotation.status}`)}</span>
    </header>
    <p className="annotation-comment">{annotation.comment}</p>
    {annotation.selector.kind === "text" && <blockquote className="annotation-quote">{annotation.selector.quote}</blockquote>}
    <div className="annotation-meta">
      {t(`annotation.${annotation.selector.kind}`)} · {t("annotation.created_at_revision", { revision: annotation.base_rev.slice(0, 7) })}
    </div>
    {annotation.anchor === "asset_changed" && <p className="asset-changed">{t("figure.asset_changed")}</p>}
    {(annotation.anchor === "block_deleted" || annotation.anchor === "block_modified" || annotation.anchor === "invalidated") && (
      <>
        <p className="note">{t(`annotation.${annotation.anchor}`)}</p>
        <EarlierSource annotation={annotation} onError={onError} />
      </>
    )}
    {annotation.resolutions.length > 0 && <button className="link-button resolution-link" type="button"
      aria-pressed={resolutionMode && isSelected} onClick={() => compare(annotation)}>
      {t("diff.resolution_snapshot")}
    </button>}
    {resolutionMode && isSelected && <ResolutionHistory annotation={annotation} />}
    <div className="card-actions">
      {confirming ? <>
        <span>{t("annotation.delete_prompt")}</span>
        <button type="button" disabled={busy} onClick={() => setConfirming(false)}>{t("action.cancel")}</button>
        <button type="button" className="danger" disabled={busy} onClick={() => act("delete", annotation)}>{t("annotation.delete")}</button>
      </> : <>
        <button type="button" disabled={busy} onClick={() => act(annotation.status === "open" ? "resolve" : "reopen", annotation)}>
          {t(annotation.status === "open" ? "annotation.resolve" : "annotation.reopen")}
        </button>
        <button type="button" className="quiet" disabled={busy} onClick={() => setConfirming(true)}>{t("annotation.delete")}</button>
      </>}
    </div>
  </article>;
}

export function AnnotationsDrawer({ current, close, ...props }: DrawerProps) {
  const { snapshot, selectedAnnotation, t } = useReader();
  const [filter, setFilter] = useState<"all" | "open" | "resolved" | "current">("all");
  const list = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (selectedAnnotation) setFilter("all");
  }, [selectedAnnotation]);
  useEffect(() => {
    list.current?.querySelector('[data-selected="true"]')?.scrollIntoView?.({ block: "nearest" });
  }, [selectedAnnotation, filter]);
  const annotations = snapshot.annotations.filter((annotation) => filter === "all" ||
    (filter === "current" ? annotation.block_id === current : annotation.status === filter));
  return <aside className="annotation-drawer" aria-label={t("annotation.title")}>
    <header className="drawer-heading">
      <h2>{t("annotation.title")} <span className="count">{snapshot.annotations.length}</span></h2>
      <button type="button" className="quiet icon-button" onClick={close} aria-label={t("annotation.close")}>×</button>
    </header>
    <div className="drawer-toolbar">
      <select aria-label={t("annotation.filter")} value={filter} onChange={(event) => setFilter(event.target.value as typeof filter)}>
        {(["all", "open", "resolved", "current"] as const).map((item) => <option key={item} value={item}>{t(`annotation.${item}`)}</option>)}
      </select>
    </div>
    <div className="annotation-list" ref={list}>
      {annotations.length ? annotations.map((annotation) => (
        <AnnotationCard key={annotation.id} annotation={annotation} {...props} />
      )) : <p className="note">{filter === "current" && !current ? t("annotation.select_block") : t("empty.annotations")}</p>}
    </div>
  </aside>;
}

export function AnnotationComposer({ draft, t, busy, cancel, submit }: {
  draft: Draft; t: Translate; busy: boolean; cancel: () => void; submit: (comment: string) => void;
}) {
  const [comment, setComment] = useState("");
  return <form className="annotation-composer" aria-label={t("annotation.new")} onSubmit={(event) => {
    event.preventDefault();
    if (comment.trim()) submit(comment.trim());
  }} onKeyDown={(event) => {
    if (event.key === "Escape" && !busy) cancel();
  }}>
    <header><h2>{t("annotation.new")}</h2><span className="badge">{t(`annotation.${draft.selector.kind}`)}</span></header>
    {draft.selector.kind === "text" && <blockquote className="annotation-quote">{draft.selector.quote}</blockquote>}
    <label htmlFor="annotation-comment" className="label">{t("annotation.comment")}</label>
    <textarea autoFocus id="annotation-comment" value={comment} onChange={(event) => setComment(event.target.value)} rows={3} />
    <div className="form-actions">
      <button type="button" disabled={busy} onClick={cancel}>{t("action.cancel")}</button>
      <button type="submit" className="primary" disabled={busy || !comment.trim()}>{t("action.submit")}</button>
    </div>
  </form>;
}
