import type { Selector } from "./api";
import { codePointsSlice, points } from "./manuscript";

export type TextSelector = Extract<Selector, { kind: "text" }>;

type Boundary = { node: Node; offset: number };

function compare(left: Boundary, right: Boundary) {
  if (left.node === right.node) return left.offset - right.offset;
  const lhs = document.createRange();
  const rhs = document.createRange();
  lhs.setStart(left.node, left.offset);
  rhs.setStart(right.node, right.offset);
  return lhs.compareBoundaryPoints(Range.START_TO_START, rhs);
}

function enclosingRegion(node: Node) {
  const element = node instanceof Element ? node : node.parentElement;
  return element?.closest<HTMLElement>("[data-text-region]") ?? undefined;
}

function intersectedLeaves(range: Range, region: HTMLElement) {
  const leaves = [...region.querySelectorAll<HTMLElement>("[data-span-start][data-span-end]")];
  return leaves.filter((leaf) => {
    if (leaf.closest("[data-text-region]") !== region || !range.intersectsNode(leaf)) return false;
    const boundaries = document.createRange();
    boundaries.selectNodeContents(leaf);
    return compare(
      { node: range.startContainer, offset: range.startOffset },
      { node: boundaries.endContainer, offset: boundaries.endOffset },
    ) < 0 && compare(
      { node: range.endContainer, offset: range.endOffset },
      { node: boundaries.startContainer, offset: boundaries.startOffset },
    ) > 0;
  });
}

function renderedOffset(leaf: HTMLElement, boundary: Boundary, end: boolean): number {
  const prefix = document.createRange();
  prefix.selectNodeContents(leaf);
  prefix.setEnd(boundary.node, boundary.offset);
  const length = prefix.toString().length;
  let units = 0;
  let offset = 0;
  for (const character of points(leaf.textContent ?? "")) {
    const next = units + character.length;
    if (length < next) return offset + Number(end && length > units);
    units = next;
    offset += 1;
  }
  return offset;
}

const entityDecoder = document.createElement("textarea");

function textPositions(raw: string, visible: string, code: boolean) {
  const source = points(raw);
  let cursor = 0;
  let limit = source.length;
  if (code) {
    const ticks = raw.match(/^`+/)?.[0].length ?? 0;
    cursor = ticks;
    limit -= ticks;
    if (ticks && raw[ticks] === " " && raw.at(-ticks - 1) === " " && /[^ `]/.test(visible)) {
      cursor += 1;
      limit -= 1;
    }
  }
  const positions: { start: number; end: number }[] = [];
  const characters = points(visible);
  for (let index = 0; index < characters.length; index += 1) {
    const character = characters[index];
    let start = cursor;
    if (!code && source[cursor] === "\\" && source[cursor + 1] === character) {
      cursor += 2;
    } else if (!code && source[cursor] === "&") {
      const entity = source.slice(cursor, cursor + 34).join("").match(/^&(?:#(?:x[\da-f]+|\d+)|[a-z]\w*);/i)?.[0];
      if (entity) {
        entityDecoder.innerHTML = entity;
        const decoded = points(entityDecoder.value);
        if (decoded.every((value, offset) => characters[index + offset] === value)) {
          cursor += entity.length;
          decoded.forEach(() => positions.push({ start, end: cursor }));
          index += decoded.length - 1;
          continue;
        }
      }
      if (character !== "&") {
        const match = source.indexOf(character, cursor);
        if (match !== -1 && match < limit) start = cursor = match;
      }
      cursor += 1;
    } else {
      if (source[cursor] !== character && !(code && source[cursor] === "\n" && character === " ")) {
        const match = source.indexOf(character, cursor);
        if (match !== -1 && match < limit) start = cursor = match;
      }
      cursor += 1;
    }
    positions.push({ start: Math.min(start, limit), end: Math.min(cursor, limit) });
  }
  return positions;
}

function sourceBoundary(leaf: HTMLElement, boundary: Boundary, source: string, end: boolean) {
  const start = Number(leaf.dataset.spanStart);
  const stop = Number(leaf.dataset.spanEnd);
  if (leaf.dataset.atomic === "true" || !leaf.contains(boundary.node)) return end ? stop : start;
  const visible = leaf.textContent ?? "";
  const raw = codePointsSlice(source, start, stop);
  const offset = renderedOffset(leaf, boundary, end);
  if (raw === visible) return Math.min(start + offset, stop);
  const positions = textPositions(raw, visible, leaf.dataset.code === "true");
  return start + (end ? positions[offset - 1]?.end ?? 0 : positions[offset]?.start ?? stop - start);
}

export function selectionRegion(selection = document.getSelection()): HTMLElement | undefined {
  return selection?.anchorNode && !selection.isCollapsed ? enclosingRegion(selection.anchorNode) : undefined;
}

export function textSelector(region: HTMLElement, source: string, selection: Selection): TextSelector | null {
  const regionRange = document.createRange();
  regionRange.selectNodeContents(region);
  const range = selection.getRangeAt(0).cloneRange();
  if (range.compareBoundaryPoints(Range.START_TO_START, regionRange) < 0) {
    range.setStart(regionRange.startContainer, regionRange.startOffset);
  }
  if (range.compareBoundaryPoints(Range.END_TO_END, regionRange) > 0) {
    range.setEnd(regionRange.endContainer, regionRange.endOffset);
  }
  if (range.collapsed) return null;
  const leaves = intersectedLeaves(range, region);
  if (!leaves.length) return null;
  const start = sourceBoundary(leaves[0], { node: range.startContainer, offset: range.startOffset }, source, false);
  const end = sourceBoundary(leaves[leaves.length - 1], { node: range.endContainer, offset: range.endOffset }, source, true);
  const quote = codePointsSlice(source, start, end);
  return start < end && quote.trim() ? { kind: "text", start, end, quote } : null;
}
