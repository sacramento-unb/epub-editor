"use client";
import { useEffect, useMemo, useRef, useState } from "react";
import type { DocumentModel, Element } from "@/lib/api";
import { contains, fromView, toView, viewSize, type BBox, type Rotation } from "@/lib/geometry";

type PdfDoc = { numPages: number; getPage: (n: number) => Promise<any>; destroy: () => void };

interface Props {
  projectId: string;
  doc: DocumentModel;
  current: string | null;
  selected: Set<string>;
  showOverlays: boolean;
  onPick: (id: string, additive: boolean) => void;
  onRegion: (page: number, bbox: BBox) => void;
}

export function PdfView({ projectId, doc, current, selected, showOverlays, onPick, onRegion }: Props) {
  const [pdf, setPdf] = useState<PdfDoc | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [zoom, setZoom] = useState(1);
  const [rot, setRot] = useState<Rotation>(0);
  const container = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    let loaded: PdfDoc | null = null;
    (async () => {
      const pdfjs: any = await import("pdfjs-dist");
      pdfjs.GlobalWorkerOptions.workerSrc = "/pdf.worker.min.mjs";
      // Scripts embutidos no PDF ficam desativados (isEvalSupported=false; sem XFA/JS).
      const task = pdfjs.getDocument({ url: `/api/projects/${projectId}/pdf`, isEvalSupported: false, enableXfa: false });
      task.onPassword = (update: (pw: string) => void) => {
        const pw = window.prompt("Este PDF é protegido. Informe a senha (somente se tiver autorização):");
        if (pw) update(pw);
        else task.destroy();
      };
      try {
        loaded = await task.promise;
        if (!cancelled) setPdf(loaded);
      } catch (e: any) {
        if (!cancelled) setError(`Não foi possível abrir o PDF: ${e?.message || e}`);
      }
    })();
    return () => { cancelled = true; loaded?.destroy(); };
  }, [projectId]);

  const byPage = useMemo(() => {
    const m = new Map<number, { e: Element; bbox: BBox }[]>();
    for (const e of doc.elements) for (const s of e.sources) {
      if (!m.has(s.page)) m.set(s.page, []);
      m.get(s.page)!.push({ e, bbox: s.bbox });
    }
    return m;
  }, [doc]);

  // navega até o elemento corrente
  useEffect(() => {
    if (!current || !container.current) return;
    const el = container.current.querySelector(`[data-ov="${current}"]`) as HTMLElement | null;
    if (el) el.scrollIntoView({ block: "center", behavior: "smooth" });
    else {
      const e = doc.elements.find((x) => x.id === current);
      const pg = e?.sources[0]?.page;
      if (pg !== undefined) container.current.querySelector(`[data-page="${pg}"]`)?.scrollIntoView({ block: "start" });
    }
  }, [current, doc]);

  return (
    <div className="pane" style={{ flex: 1 }}>
      <div className="pane-head">
        <strong>PDF original</strong>
        <span className="grow" />
        <button className="small" onClick={() => setZoom((z) => Math.max(0.25, +(z - 0.25).toFixed(2)))} aria-label="Diminuir zoom">−</button>
        <span aria-live="polite">{Math.round(zoom * 100)}%</span>
        <button className="small" onClick={() => setZoom((z) => Math.min(4, +(z + 0.25).toFixed(2)))} aria-label="Aumentar zoom">+</button>
        <button className="small" onClick={() => setRot((r) => ((r + 90) % 360) as Rotation)} aria-label="Girar 90 graus">⟳ {rot}°</button>
      </div>
      <div className="pane-body" ref={container}>
        {error && <p className="error" style={{ padding: 12 }}>{error}</p>}
        {!pdf && !error && <p style={{ padding: 12 }}>Carregando PDF…</p>}
        {pdf && doc.pages.map((p) => (
          <PdfPage key={p.index} pdf={pdf} index={p.index} pageW={p.width} pageH={p.height} zoom={zoom} rot={rot}
            items={byPage.get(p.index) || []} current={current} selected={selected} showOverlays={showOverlays}
            onPick={onPick} onRegion={onRegion} />
        ))}
      </div>
    </div>
  );
}

function PdfPage({ pdf, index, pageW, pageH, zoom, rot, items, current, selected, showOverlays, onPick, onRegion }: {
  pdf: PdfDoc; index: number; pageW: number; pageH: number; zoom: number; rot: Rotation;
  items: { e: Element; bbox: BBox }[]; current: string | null; selected: Set<string>; showOverlays: boolean;
  onPick: (id: string, additive: boolean) => void; onRegion: (page: number, bbox: BBox) => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const [visible, setVisible] = useState(false);
  const [drag, setDrag] = useState<{ x0: number; y0: number; x1: number; y1: number } | null>(null);
  const [vw, vh] = viewSize(pageW, pageH, zoom, rot);

  useEffect(() => {
    const io = new IntersectionObserver((es) => es.forEach((e) => e.isIntersecting && setVisible(true)), { rootMargin: "600px" });
    if (ref.current) io.observe(ref.current);
    return () => io.disconnect();
  }, []);

  useEffect(() => {
    if (!visible || !canvas.current) return;
    let task: any = null;
    let stop = false;
    pdf.getPage(index + 1).then((page: any) => {
      if (stop || !canvas.current) return;
      const dpr = window.devicePixelRatio || 1;
      // rotação intrínseca (/Rotate) + rotação da interface; escala 1 = pontos exibidos
      const viewport = page.getViewport({ scale: zoom * dpr, rotation: (page.rotate + rot) % 360 });
      const c = canvas.current;
      c.width = Math.floor(viewport.width);
      c.height = Math.floor(viewport.height);
      c.style.width = `${viewport.width / dpr}px`;
      c.style.height = `${viewport.height / dpr}px`;
      task = page.render({ canvas: c, viewport });
      task.promise.catch(() => undefined);
    });
    return () => { stop = true; task?.cancel?.(); };
  }, [visible, pdf, index, zoom, rot]);

  function local(ev: React.MouseEvent) {
    const r = ref.current!.getBoundingClientRect();
    return [ev.clientX - r.left, ev.clientY - r.top] as const;
  }

  function onMouseDown(ev: React.MouseEvent) {
    if (!ev.shiftKey) return;
    const [x, y] = local(ev);
    setDrag({ x0: x, y0: y, x1: x, y1: y });
    ev.preventDefault();
  }
  function onMouseMove(ev: React.MouseEvent) {
    if (!drag) return;
    const [x, y] = local(ev);
    setDrag({ ...drag, x1: x, y1: y });
  }
  function onMouseUp(ev: React.MouseEvent) {
    if (drag) {
      const a = fromView(drag.x0, drag.y0, pageW, pageH, zoom, rot);
      const b = fromView(drag.x1, drag.y1, pageW, pageH, zoom, rot);
      setDrag(null);
      const bb: BBox = [Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.max(a[0], b[0]), Math.max(a[1], b[1])];
      if (bb[2] - bb[0] > 4 && bb[3] - bb[1] > 4) onRegion(index, bb);
      return;
    }
    const [x, y] = local(ev);
    const [px, py] = fromView(x, y, pageW, pageH, zoom, rot);
    const hits = items.filter((it) => contains(it.bbox, px, py))
      .sort((a, b) => (a.bbox[2] - a.bbox[0]) * (a.bbox[3] - a.bbox[1]) - (b.bbox[2] - b.bbox[0]) * (b.bbox[3] - b.bbox[1]));
    if (hits.length) onPick(hits[0].e.id, ev.ctrlKey || ev.metaKey);
  }

  return (
    <div ref={ref} className="pdfpage" data-page={index} style={{ width: vw, height: vh }}
      onMouseDown={onMouseDown} onMouseMove={onMouseMove} onMouseUp={onMouseUp}
      aria-label={`Página ${index + 1}`} title="Clique para selecionar; Shift+arrastar cria um recorte manual">
      <canvas ref={canvas} />
      {items.map(({ e, bbox }, k) => {
        const isCur = e.id === current;
        const isSel = selected.has(e.id);
        if (!showOverlays && !isCur && !isSel) return null;
        const v = toView(bbox, pageW, pageH, zoom, rot);
        const cls = ["ov", e.included ? "in" : "out", e.review.length ? "review" : "", isCur ? "cur" : "", isSel ? "selected" : ""].join(" ");
        return <div key={`${e.id}-${k}`} data-ov={e.id} className={cls}
          style={{ left: v[0], top: v[1], width: v[2] - v[0], height: v[3] - v[1] }} />;
      })}
      {drag && (
        <div className="ov drawing" style={{ left: Math.min(drag.x0, drag.x1), top: Math.min(drag.y0, drag.y1),
          width: Math.abs(drag.x1 - drag.x0), height: Math.abs(drag.y1 - drag.y0) }} />
      )}
      <span className="badge" style={{ position: "absolute", left: 4, top: 4 }}>p. {index + 1}</span>
    </div>
  );
}
