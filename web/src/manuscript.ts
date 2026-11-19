import type { Block, BlockDiff, Deliverable, Figure, PublishedSnapshot, SerializedInline } from "./api";
import type { Translate } from "./i18n";

export const TABS = ["manuscript", "appendices", "reviews", "figures", "references"] as const;
export type Tab = typeof TABS[number];

function layout(kind: Deliverable["kind"]): Tab {
  if (kind === "supplementary") return "appendices";
  if (kind === "response_letter") return "reviews";
  return "manuscript";
}

export interface Publication {
  id: string;
  tab: Tab;
  blocks: Block[];
  ordered: string[];
}

export interface BlockLocation {
  block: Block;
  publication: Publication;
}

export interface TargetRange {
  start: string;
  end?: string | null;
  deliverable?: string | null;
}

export const points = (text: string) => Array.from(text);
export const codePointsSlice = (text: string, start: number, end?: number) =>
  points(text).slice(start, end).join("");

export function groupLines(block: { clean_source: string; inlines: SerializedInline[] }): SerializedInline[][] {
  const groups: SerializedInline[][] = [];
  const source = points(block.clean_source);
  let previous = 0;
  for (const node of block.inlines) {
    const gap = source.slice(previous, node.span[0]).join("");
    if (!groups.length || /\n[\t ]*(?:(?:[-+*]|\d+[.)])[\t ]+|\n)/.test(gap)) {
      groups.push([]);
    }
    groups[groups.length - 1].push(node);
    previous = node.span[1];
  }
  return groups;
}

export function indexSnapshot(snapshot: PublishedSnapshot) {
  const publications: Publication[] = [];
  const locations = new Map<string, BlockLocation[]>();
  const figures = new Map<string, Figure>();
  const bySource = new Map<string, Block[]>();
  for (const block of snapshot.blocks) {
    const source = bySource.get(block.source);
    if (source) source.push(block);
    else bySource.set(block.source, [block]);
  }

  for (const deliverable of snapshot.deliverables) {
    const publication: Publication = {
      id: deliverable.name,
      tab: layout(deliverable.kind),
      blocks: deliverable.sources.flatMap((source) => bySource.get(source) ?? []),
      ordered: [],
    };
    publications.push(publication);

    function visit(block: Block) {
      const id = block.block_id;
      if (id) {
        const location = { block, publication };
        const paths = locations.get(id);
        if (paths) paths.push(location);
        else locations.set(id, [location]);
        publication.ordered.push(id);
      }

      if (block.kind === "figure") {
        if (id && !figures.has(id)) figures.set(id, block);
        block.subfigures.forEach(visit);
      } else if (block.kind === "review_item") {
        block.reply.forEach(visit);
      } else if (block.kind === "markdown") {
        block.children.forEach(visit);
      }
    }

    publication.blocks.forEach(visit);
  }

  function locate(id: string, preferred?: Publication, end?: string | null): BlockLocation | undefined {
    const candidates = locations.get(id) ?? [];
    const available = end ? candidates.filter(({ publication }) => {
      const ordered = publication.ordered;
      return ordered.indexOf(end) >= ordered.indexOf(id);
    }) : candidates;
    return available.find(({ publication }) => publication === preferred) ??
      available.find(({ publication }) => publication.tab === "manuscript" || publication.tab === "appendices") ??
      available[0];
  }

  function locateRange(target: TargetRange) {
    const preferred = publications.find(({ id }) => id === target.deliverable);
    return locate(target.start, preferred, target.end);
  }

  function rangeIds(target: TargetRange): string[] {
    const location = locateRange(target);
    if (!location) return [];
    if (!target.end) return [target.start];
    const ordered = location.publication.ordered;
    return ordered.slice(ordered.indexOf(target.start), ordered.indexOf(target.end) + 1);
  }

  const changes = snapshot.diff?.blocks;
  const deleted: BlockDiff[] = Object.values(changes ?? {}).filter((change) => change.status === "deleted");

  return { publications, figures, locate, locateRange, rangeIds, changes, deleted };
}

export type ManuscriptIndex = ReturnType<typeof indexSnapshot>;

export function blockLabel(t: Translate, target: BlockLocation | undefined, id: string) {
  if (!target) return id;
  const { block } = target;
  if (block.label) return block.label;
  const number = block.number;
  if (!number) return id;
  switch (block.kind) {
    case "heading": return t("block.section", { number });
    case "figure":
    case "subfigure": return t("block.figure", { number });
    case "table": return t("block.table", { number });
    case "display_math": return t("block.equation", { number: number.replace(/^\(|\)$/g, "") });
    default: return id;
  }
}
