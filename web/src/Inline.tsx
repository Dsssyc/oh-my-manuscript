import { createContext, lazy, Suspense, useContext } from "react";
import { api, type Block, type Content, type DiffChunk, type Outline, type PublishedSnapshot, type Selector, type SerializedInline } from "./api";
import { type ManuscriptIndex, type Publication, type TargetRange } from "./manuscript";
import type { Translate } from "./i18n";

export interface ReadingContext {
  workspaceId: string;
  snapshot: PublishedSnapshot;
  index: ManuscriptIndex;
  t: Translate;
  jump: (id: string, preferred?: Publication) => void;
  openRange: (target: TargetRange) => void;
  showReference: (key: string, related?: string[]) => void;
  annotate: (id: string, selector: Selector) => void;
  focusBlock: (id: string) => void;
  editOutline: (block: Outline, publicationId?: string) => void;
  drawing: (active: boolean) => void;
  outlineEditor: { id: string; publicationId?: string; body: string; busy: boolean; save: (body: string) => void; cancel: () => void } | null;
  selectAnnotation: (id: string) => void;
  highlighted: ReadonlySet<string>;
  selectedAnnotation?: string;
  resolutionAnnotation?: string;
  showDiff: boolean;
}

export const Reader = createContext<ReadingContext>(null!);
export const useReader = () => useContext(Reader);
export const PublicationContext = createContext<Publication | undefined>(undefined);

const KatexMarkup = lazy(() => import("./KatexMarkup"));

export function MathMarkup({ latex, display = false }: { latex: string; display?: boolean }) {
  return <Suspense fallback={<span>{latex}</span>}><KatexMarkup latex={latex} display={display} /></Suspense>;
}

function activate(event: React.MouseEvent<HTMLAnchorElement>, action: () => void) {
  event.preventDefault();
  if (document.getSelection()?.isCollapsed === false) return;
  action();
}

function safeLink(destination: string) {
  try {
    const url = new URL(destination, window.location.origin);
    return ["http:", "https:", "mailto:"].includes(url.protocol) ? destination : undefined;
  } catch {
    return undefined;
  }
}

function InlineNode({ node }: { node: SerializedInline }) {
  const { index, snapshot, workspaceId, jump, openRange, showReference } = useReader();
  const publication = useContext(PublicationContext);
  const span = {
    "data-span-start": node.span[0],
    "data-span-end": node.span[1],
    "data-atomic": "atomic" in node ? String(node.atomic) : undefined,
  };
  switch (node.kind) {
    case "text": return <span {...span}>{node.value}</span>;
    case "code_span": return <code {...span} data-code="true">{node.value}</code>;
    case "line_break": return node.hard ? <br {...span} /> : <span {...span}>{"\n"}</span>;
    case "strong": return <strong><Inlines nodes={node.children} /></strong>;
    case "emphasis": return <em><Inlines nodes={node.children} /></em>;
    case "subscript": return <sub><Inlines nodes={node.children} /></sub>;
    case "superscript": return <sup><Inlines nodes={node.children} /></sup>;
    case "link":
      // Structured references own their navigation, even inside Markdown links.
      return node.children.some(hasStructuredLink)
        ? <Inlines nodes={node.children} />
        : <a href={safeLink(node.destination)} title={node.title ?? undefined}><Inlines nodes={node.children} /></a>;
    case "image":
      return <img {...span} src={api.asset(workspaceId, snapshot.revision, node.destination)} alt={node.alt} title={node.title ?? undefined} />;
    case "inline_math":
      return <span {...span}><MathMarkup latex={node.latex} /></span>;
    case "block_ref": {
      const preferred = index.publications.find(({ id }) => id === node.target_deliverable) ?? publication;
      return <a {...span} href={`#${node.target_id}`}
        onClick={(event) => activate(event, () => jump(node.target_id, preferred))}>{node.label}</a>;
    }
    case "loc_ref": {
      const target = { start: node.start_id, end: node.end_id, deliverable: node.target_deliverable };
      return <a {...span} href={`#${target.start}`} onClick={(event) => activate(event, () => openRange(target))}>
        {node.label}
      </a>;
    }
    case "citation": {
      const numeric = snapshot.style.citation_style === "numeric";
      const separator = numeric ? ", " : "; ";
      const wrapped = numeric || node.parenthetical;
      const entries = (wrapped ? node.label.slice(1, -1) : node.label).split(separator);
      const first = node.keys[0];
      if (!first) return <span {...span}>{node.label}</span>;
      if (node.keys.length === 1 || entries.length !== node.keys.length) {
        return <a {...span} href={`#${first}`} onClick={(event) => activate(event, () => showReference(first, node.keys))}>
          {node.label}
        </a>;
      }
      return <span {...span} className="citation-group">{wrapped && node.label[0]}{node.keys.map((key, i) => (
        <span key={`${key}-${i}`}>{i > 0 && separator}
          <a href={`#${key}`} onClick={(event) => activate(event, () => showReference(key))}>{entries[i]}</a>
        </span>
      ))}{wrapped && node.label.at(-1)}</span>;
    }
  }
}

function hasStructuredLink(node: SerializedInline): boolean {
  if (node.kind === "block_ref" || node.kind === "loc_ref" || node.kind === "citation") return true;
  return "children" in node && node.children.some(hasStructuredLink);
}

export function Inlines({ nodes }: { nodes: SerializedInline[] }) {
  return <>{nodes.map((node, index) => <InlineNode key={`${node.span[0]}-${index}`} node={node} />)}</>;
}

export function TextContent({ content, block }: { content: Pick<Content, "inlines">; block: Block }) {
  return <span data-text-region={block.block_id ?? undefined}><Inlines nodes={content.inlines} /></span>;
}

export function DiffText({ chunks }: { chunks: DiffChunk[] }) {
  return <div className="diff-text">{chunks.map(({ op, text }, index) => {
    if (op === "insert") return <ins key={index}>{text}</ins>;
    if (op === "delete") return <del key={index}>{text}</del>;
    return <span key={index}>{text}</span>;
  })}</div>;
}
