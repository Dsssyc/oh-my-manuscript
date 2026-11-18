import { useState } from "react";
import { Blocks, BlockView } from "./Blocks";
import { DiffText, PublicationContext, useReader } from "./Inline";
import { blockLabel, type Publication, type Tab } from "./manuscript";

export function ReadingLayout({ tab, publication }: { tab: Tab; publication?: Publication }) {
  const { index, t, jump } = useReader();
  const publications = publication ? [publication] : index.publications.filter((entry) => entry.tab === tab);
  const headings = publications.flatMap((entry) => entry.blocks.flatMap((block) => {
    if (block.kind !== "heading" || !block.block_id) return [];
    return [{ block, id: block.block_id, publication: entry }];
  }));

  if (!publications.some((entry) => entry.blocks.length)) return <p className="empty">{t(`empty.${tab}`)}</p>;
  return (
    <div className="reading-layout" data-layout={tab}>
      {!publication && headings.length > 1 && (
        <nav className="contents" aria-label={t("block.navigate")}>
          <h2 className="label">{t("block.navigate")}</h2>
          {headings.map(({ block, id, publication: entry }) => (
            <PublicationContext.Provider key={`${entry.id}-${id}`} value={entry}>
              <button type="button" className="contents-link" onClick={() => jump(id, entry)}
                data-level={block.level}>
                <span>{block.number}</span>
                {block.inlines.map((node) => node.kind === "text" ? node.value : "").join("") || id}
              </button>
            </PublicationContext.Provider>
          ))}
        </nav>
      )}
      <div className="paper-stack">
        {publications.map((entry) => (
          <PublicationContext.Provider key={entry.id} value={entry}>
            <article className="paper" data-publication={entry.id} aria-label={publications.length > 1 ? entry.id : t(`tab.${tab}`)}>
              {publications.length > 1 && <div className="publication-name">{entry.id}</div>}
              <Blocks blocks={entry.blocks} />
            </article>
          </PublicationContext.Provider>
        ))}
      </div>
    </div>
  );
}

export function FiguresLayout() {
  const { index, t } = useReader();
  if (!index.figures.size) return <p className="empty">{t("empty.figures")}</p>;
  return (
    <article className="paper gallery">
      {[...index.figures].map(([id, figure]) => (
        <PublicationContext.Provider key={id} value={index.locate(id)?.publication}>
          <BlockView block={figure} />
        </PublicationContext.Provider>
      ))}
    </article>
  );
}

export function ReferencesLayout({ activeKeys, clearActive }: { activeKeys: ReadonlySet<string>; clearActive: () => void }) {
  const { snapshot, index, t, jump } = useReader();
  const [uncitedOnly, setUncitedOnly] = useState(false);
  const entries = snapshot.references.entries;
  const visible = !activeKeys.size && uncitedOnly;
  return (
    <article className="paper references">
      <header className="references-header">
        <h1>{t("tab.references")}</h1>
        <select value={visible ? "uncited" : "all"} aria-label={t("references.filter")}
          onChange={(event) => {
            clearActive();
            setUncitedOnly(event.target.value === "uncited");
          }}>
          <option value="all">{t("references.all")}</option>
          <option value="uncited">{t("references.uncited_only")}</option>
        </select>
      </header>
      {entries.length ? <ul className="reference-list">
        {entries.map((entry) => {
          const counts = Object.entries(entry.deliverable_counts);
          const uncited = !entry.cited;
          if (visible && !uncited) return null;
          const number = entry.number === null ? "" : `[${entry.number}]`;
          return <li key={entry.citekey} data-citekey={entry.citekey} data-highlight={activeKeys.has(entry.citekey) || undefined}
            data-uncited={uncited || undefined}>
            <div className="reference-text">{number && !entry.formatted_text.startsWith(number) && <span className="reference-number">{number}</span>}
              {entry.formatted_text}
            </div>
            <div className="chip-row">
              {counts.map(([deliverable, count]) => <span className="badge" key={deliverable}>
                {t("references.cited_in", { deliverable, count })}
              </span>)}
              {uncited && <span className="badge uncited">{t("references.uncited")}</span>}
              {entry.citing_blocks.map(({ block_id, deliverable, source }) => {
                if (!block_id) return null;
                const publication = index.publications.find((item) => item.id === deliverable);
                return <button className="chip" type="button" key={`${deliverable}-${source}-${block_id}`}
                  onClick={() => jump(block_id, publication)}>
                  {blockLabel(t, index.locate(block_id, publication), block_id)}
                </button>;
              })}
            </div>
          </li>;
        })}
      </ul> : <p className="note">{t("empty.references")}</p>}
    </article>
  );
}

export function DeletedBlocks() {
  const { index, t, showDiff } = useReader();
  if (!showDiff || !index.deleted.length) return null;
  return <details className="deleted-blocks">
    <summary>{t("diff.deleted_blocks", { count: index.deleted.length })}</summary>
    {index.deleted.map((block) => (
      <details key={block.block_id}>
        <summary><span className="badge diff-badge" data-status="deleted">{t("diff.deleted")}</span> {block.block_id}</summary>
        <DiffText chunks={block.chunks} />
      </details>
    ))}
  </details>;
}
