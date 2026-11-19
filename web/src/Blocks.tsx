import { useContext, useState, type ReactNode } from "react";
import type { Annotation, Block, ReviewItem, Table, TableCell } from "./api";
import { FigureView } from "./Figure";
import { DiffText, Inlines, MathMarkup, PublicationContext, TextContent, useReader } from "./Inline";
import { blockLabel, groupLines, points } from "./manuscript";

function TableView({ block, number }: { block: Table; number: string | null }) {
  const { t, annotate } = useReader();

  function cells(row: TableCell[], rowIndex: number, header: boolean) {
    const Tag = header ? "th" : "td";
    return row.map((cell, col) => cell.covered ? null : (
      <Tag key={col} rowSpan={cell.row_span} colSpan={cell.col_span}
        scope={header ? (cell.col_span > 1 ? "colgroup" : "col") : undefined}
        className={header && cell.col_span > 1 && rowIndex + cell.row_span < block.header.length ? "column-rule" : undefined}
        style={{ textAlign: block.alignment[col] ?? undefined }}>
        <TextContent block={block} content={cell} />
        {block.block_id && (
          <button type="button" className="cell-annotation" title={t("annotation.add_cell")} aria-label={t("annotation.add_cell")}
            onClick={() => annotate(block.block_id!, { kind: "cell", row: rowIndex, col })}>+</button>
        )}
      </Tag>
    ));
  }

  return (
    <div className="table-block">
      <div className="table-scroll">
        <table className="three-line">
          <caption>
            {number && <strong>{t("block.table", { number })}. </strong>}
            <TextContent block={block} content={block.caption} />
          </caption>
          <thead>{block.header.map((row, r) => <tr key={r}>{cells(row, r, true)}</tr>)}</thead>
          <tbody>{block.rows.map((row, r) => (
            <tr key={r} className={block.midrule_after.includes(r) ? "row-rule" : undefined}>
              {cells(row, r + block.header.length, false)}
            </tr>
          ))}</tbody>
        </table>
      </div>
      {block.notes.map((note, index) => (
        <p className="table-note" key={index}><TextContent block={block} content={note} /></p>
      ))}
    </div>
  );
}

function ReviewCard({ block }: { block: ReviewItem }) {
  const { t, index, openRange } = useReader();
  const locationStarts = new Set(block.location_refs.map((loc) => loc.start_id));
  return (
    <div className="review-card">
      <header className="review-header">
        <h2>{block.block_id}</h2>
        <span className="badge reviewer">{block.reviewer}</span>
      </header>
      <div className="review-comment">
        <h3>{t("review.comment")}</h3>
        <blockquote><TextContent block={block} content={block.comment} /></blockquote>
      </div>
      {block.reply.length > 0 && <div className="review-reply">
        <h3>{t("review.reply")}</h3>
        <Blocks blocks={block.reply} />
      </div>}
      <footer className="review-targets">
        <h3>{t("review.targets")}</h3>
        <div className="chip-row">
          {block.referenced_block_ids.filter((id) => !locationStarts.has(id)).map((id) => (
            <button className="chip" key={id} type="button" onClick={() => openRange({ start: id })}>
              {blockLabel(t, index.locate(id), id)}
            </button>
          ))}
          {block.location_refs.map(({ start_id, end_id, target_deliverable, label }, i) => {
            const target = { start: start_id, end: end_id, deliverable: target_deliverable };
            return <button className="chip" key={i} type="button" onClick={() => openRange(target)}>
              {label}
            </button>;
          })}
          {!block.referenced_block_ids.length && !block.location_refs.length && (
            <span className="note">{t("empty.backlinks")}</span>
          )}
        </div>
      </footer>
    </div>
  );
}

function BlockBody({ block }: { block: Block }): ReactNode {
  const number = block.number;
  const { editOutline, outlineEditor, t } = useReader();
  const publicationId = useContext(PublicationContext)?.id;
  switch (block.kind) {
    case "heading": {
      const levels = ["h1", "h2", "h3", "h4", "h5", "h6"] as const;
      const Heading = levels[Math.max(0, Math.min(5, block.level - 1))];
      return <Heading className="section-heading">
        {number && <span className="section-number">{number}</span>}
        <TextContent block={block} content={block} />
      </Heading>;
    }
    case "paragraph": return <p className="paragraph"><TextContent block={block} content={block} /></p>;
    case "list": {
      const Tag = block.ordered ? "ol" : "ul";
      const first = block.ordered ? /^\s*(\d+)[.)]/.exec(block.clean_source)?.[1] : undefined;
      return <Tag className="source-list" start={first === undefined ? undefined : Number(first)}>
        {groupLines(block).map((inlines, i) => (
          <li key={i}><TextContent block={block} content={{ inlines }} /></li>
        ))}
      </Tag>;
    }
    case "display_math": return <div className="equation">
      <div className="equation-body" data-span-start={0} data-span-end={points(block.clean_source).length} data-atomic="true">
        <MathMarkup latex={block.latex} display />
      </div>
      {number && !block.unnumbered && <span className="equation-number">{number}</span>}
    </div>;
    case "outline": {
      if (outlineEditor?.id === block.block_id && outlineEditor.publicationId === publicationId) {
        return <OutlineForm editor={outlineEditor} />;
      }
      return <div className="outline">
        <header>
          <span className="label">{t("block.outline")}</span>
          <button type="button" className="quiet" onClick={() => editOutline(block, publicationId)}>{t("block.edit_outline")}</button>
        </header>
        <div className="outline-body" data-text-region={block.block_id ?? undefined}>
          {block.inlines.length ? groupLines(block).map((inlines, i) => (
            <p key={i}><Inlines nodes={inlines} /></p>
          )) : (
            <p data-span-start={block.body_span[0]} data-span-end={block.body_span[1]}>{block.body_markdown}</p>
          )}
        </div>
      </div>;
    }
    case "figure": return <FigureView figure={block} />;
    case "table": return <TableView block={block} number={number} />;
    case "review_item": return <ReviewCard block={block} />;
    case "subfigure": return null;
    case "markdown":
      switch (block.markdown_kind) {
        case "blockquote": return <blockquote><Blocks blocks={block.children} /></blockquote>;
        case "hr": return <hr />;
        case "fence": {
          const lines = block.clean_source.split("\n");
          return <pre className="source-code"><code>{lines.slice(1, /^\s*(`{3,}|~{3,})\s*$/.test(lines.at(-1) ?? "") ? -1 : undefined).join("\n")}</code></pre>;
        }
        case "code_block": return <pre className="source-code"><code>{block.clean_source}</code></pre>;
        default: return block.children.length
          ? <Blocks blocks={block.children} />
          : <pre className="source-code">{block.clean_source}</pre>;
      }
  }
}

function OutlineForm({ editor }: { editor: NonNullable<ReturnType<typeof useReader>["outlineEditor"]> }) {
  const { t } = useReader();
  const [body, setBody] = useState(editor.body);
  return <form className="outline outline-editor" onSubmit={(event) => {
    event.preventDefault();
    editor.save(body);
  }}>
    <label className="label" htmlFor="outline-body">{t("block.outline_body")}</label>
    <textarea id="outline-body" autoFocus value={body} onChange={(event) => setBody(event.target.value)} rows={7} />
    <div className="form-actions">
      <button type="button" disabled={editor.busy} onClick={editor.cancel}>{t("action.cancel")}</button>
      <button type="submit" className="primary" disabled={editor.busy || body === editor.body}>{t("action.save")}</button>
    </div>
  </form>;
}

export function BlockView({ block }: { block: Block }) {
  const { index, t, showDiff, highlighted, focusBlock, annotate, jump, resolutionAnnotation, snapshot } = useReader();
  const id = block.block_id;
  const diff = showDiff && id ? index.changes?.[id] : undefined;
  const reviewLinks = id ? snapshot.review_target_index[id] : undefined;
  const resolution = resolutionAnnotation && snapshot.annotations.find((item) => item.id === resolutionAnnotation && item.block_id === id);
  return (
    <section className={`block block-${block.kind}`} data-block-id={id ?? undefined}
      data-highlight={id && highlighted.has(id) || undefined}
      onClick={(event) => {
        const target = event.target as Element;
        if (id && !target.closest("button, a, input, textarea") && target.closest("[data-block-id]") === event.currentTarget) {
          focusBlock(id);
        }
      }}>
      {id && <div className="block-tools">
        <button className="quiet annotate-block" type="button" onClick={() => annotate(id, { kind: "block" })}
          title={t("annotation.add_block")} aria-label={t("annotation.add_block")}>+</button>
      </div>}
      {diff && diff.status !== "unchanged" && <span className="badge diff-badge" data-status={diff.status}>{t(`diff.${diff.status}`)}</span>}
      <BlockBody block={block} />
      {reviewLinks?.length ? <div className="chip-row review-backlinks" aria-label={t("block.linked")}>
        {reviewLinks.map((review) => <button type="button" className="chip" key={review} onClick={() => jump(review)}>{review}</button>)}
      </div> : null}
      {diff?.chunks?.some((chunk) => chunk.op !== "equal") && <DiffText chunks={diff.chunks} />}
      {resolution && (
        <div className="block-resolution">
          <ResolutionHistory annotation={resolution} />
        </div>
      )}
    </section>
  );
}

export function ResolutionHistory({ annotation }: { annotation: Annotation }) {
  const { t } = useReader();
  return <div className="resolution-history">{annotation.resolutions.slice().reverse().map((record, i) => (
    <div key={`${record.after_rev}-${i}`} className="resolution-record">
      <div className="label">{t("diff.resolve_label", { revision: record.after_rev.slice(0, 7) })}</div>
      <DiffText chunks={record.diff_chunks} />
      {record.after_block_md === null && <span className="badge">{t("diff.deleted")}</span>}
    </div>
  ))}</div>;
}

export function Blocks({ blocks }: { blocks: Block[] }) {
  return <>{blocks.map((block, i) => <BlockView key={block.block_id ?? i} block={block} />)}</>;
}
