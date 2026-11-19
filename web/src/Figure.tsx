import { useRef, useState, type PointerEvent } from "react";
import { api, type Annotation, type Figure, type Selector, type Subfigure } from "./api";
import { TextContent, useReader } from "./Inline";

type Rectangle = Extract<Selector, { kind: "rect" }>;
type Point = { x: number; y: number };

function coordinates(event: PointerEvent<HTMLElement>): Point {
  const image = event.currentTarget.getBoundingClientRect();
  const clamp = (value: number) => Math.max(0, Math.min(1, value));
  return { x: clamp((event.clientX - image.left) / image.width), y: clamp((event.clientY - image.top) / image.height) };
}

function rectangle(first: Point, last: Point): Rectangle {
  const x = Math.min(first.x, last.x);
  const y = Math.min(first.y, last.y);
  const w = Math.min(Math.abs(last.x - first.x), 1 - x);
  const h = Math.min(Math.abs(last.y - first.y), 1 - y);
  return { kind: "rect", x, y, w, h };
}

function style(rect: Rectangle) {
  return { left: `${rect.x * 100}%`, top: `${rect.y * 100}%`, width: `${rect.w * 100}%`, height: `${rect.h * 100}%` };
}

function ImagePanel({ image, figure }: { image: Subfigure; figure: Figure }) {
  const reader = useReader();
  const { snapshot, workspaceId, t } = reader;
  const id = image.block_id ?? figure.block_id;
  const reviewLinks = image.block_id ? snapshot.review_target_index[image.block_id] : undefined;
  const [loaded, setLoaded] = useState(false);
  const [failed, setFailed] = useState(false);
  const [preview, setPreview] = useState<Rectangle | null>(null);
  const start = useRef<{ point: Point; pointer: number; x: number; y: number; annotationId?: string }>(null);
  const drawLabel = t("figure.draw");
  const annotations = snapshot.annotations.filter((annotation): annotation is Annotation & { selector: Rectangle } =>
    annotation.selector.kind === "rect" &&
    (annotation.block_id === id || (figure.subfigures.length === 1 && annotation.block_id === figure.block_id))
  );

  function handleStart(event: PointerEvent<HTMLDivElement>) {
    if (!loaded || failed || event.button !== 0 || !event.isPrimary || !id) return;
    const overlay = (event.target as Element).closest<HTMLElement>("[data-rectangle]");
    if (!overlay) event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    start.current = {
      point: coordinates(event), pointer: event.pointerId, x: event.clientX, y: event.clientY,
      annotationId: overlay?.dataset.rectangle,
    };
    reader.focusBlock(id);
    reader.drawing(true);
  }

  function stop(event: PointerEvent<HTMLDivElement>, save: boolean) {
    const first = start.current;
    if (!first || first.pointer !== event.pointerId) return;
    start.current = null;
    reader.drawing(false);
    setPreview(null);
    if (!save || !id) return;
    if (Math.hypot(event.clientX - first.x, event.clientY - first.y) < 4) {
      if (first.annotationId) reader.selectAnnotation(first.annotationId);
      return;
    }
    const rect = rectangle(first.point, coordinates(event));
    if (rect.w > 0 && rect.h > 0) {
      const hash = image.asset_sha256 ?? figure.asset_sha256;
      reader.annotate(id, hash ? { ...rect, asset_sha256: hash } : rect);
    }
  }

  return (
    <div className="figure-panel" data-block-id={image.block_id ?? undefined}
      data-highlight={image.block_id && reader.highlighted.has(image.block_id) || undefined}>
      <div className="image-plane" title={drawLabel} onPointerDown={handleStart}
        onPointerMove={(event) => {
          if (start.current?.pointer === event.pointerId) setPreview(rectangle(start.current.point, coordinates(event)));
        }}
        onPointerUp={(event) => stop(event, true)} onPointerCancel={(event) => stop(event, false)}
        onLostPointerCapture={(event) => stop(event, false)}>
        <img src={api.asset(workspaceId, snapshot.revision, image.asset_path)} draggable={false}
          alt={image.alt || t("block.alt", { number: image.number ?? "" })}
          onLoad={() => setLoaded(true)} onError={() => setFailed(true)} />
        {loaded && annotations.map((annotation) => (
          <button className="rectangle" key={annotation.id} type="button"
            aria-label={annotation.comment} title={annotation.comment} data-rectangle={annotation.id} style={style(annotation.selector)}
            data-changed={annotation.anchor === "asset_changed" || undefined}
            data-selected={reader.selectedAnnotation === annotation.id || undefined}
            onClick={() => reader.selectAnnotation(annotation.id)} />
        ))}
        {preview && <span className="rectangle drawing" style={style(preview)} />}
      </div>
      {failed && <p role="status" className="note">{t("error.asset")}</p>}
      <div className="panel-caption">{image.sublabel && <strong>{image.sublabel}</strong>}{image.alt && <span>{image.alt}</span>}</div>
      {annotations.some((annotation) => annotation.anchor === "asset_changed") && (
        <p className="asset-changed">{t("figure.asset_changed")}</p>
      )}
      {Boolean(reviewLinks?.length) && (
        <div className="chip-row" aria-label={t("block.linked")}>
          {reviewLinks?.map((review) => (
            <button type="button" className="chip" key={review} onClick={() => reader.jump(review)}>{review}</button>
          ))}
        </div>
      )}
    </div>
  );
}

export function FigureView({ figure }: { figure: Figure }) {
  const { snapshot, t } = useReader();
  return (
    <figure className="figure">
      <div className="figure-grid" style={{ gridTemplateColumns: `repeat(${Math.max(1, Math.min(figure.cols ?? figure.subfigures.length, figure.subfigures.length))}, minmax(0, 1fr))` }}>
        {figure.subfigures.map((image, i) => (
          <ImagePanel key={`${snapshot.revision}-${image.block_id ?? i}`} image={image} figure={figure} />
        ))}
      </div>
      <figcaption>
        {figure.number && <strong>{t("block.figure", { number: figure.number })}. </strong>}
        <TextContent block={figure} content={figure.caption} />
      </figcaption>
    </figure>
  );
}
