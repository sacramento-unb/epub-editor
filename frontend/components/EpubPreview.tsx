"use client";
import { useEffect, useMemo, useRef, useState } from "react";
import { api, type ExportInfo } from "@/lib/api";

const PRESETS: Record<string, [number, number] | null> = {
  "Livre": null, "Celular (360×640)": [360, 640], "E-reader (600×800)": [600, 800], "Tablet (768×1024)": [768, 1024],
};

interface Props {
  exp: ExportInfo | null;
  current: string | null;
  onPick: (id: string) => void;
}

export function EpubPreview({ exp, current, onPick }: Props) {
  const [detail, setDetail] = useState<ExportInfo | null>(null);
  const [file, setFile] = useState<string | null>(null);
  const [font, setFont] = useState(100);
  const [paged, setPaged] = useState(false);
  const [theme, setTheme] = useState("light");
  const [preset, setPreset] = useState("Livre");
  const frame = useRef<HTMLIFrameElement>(null);

  useEffect(() => {
    if (!exp) { setDetail(null); return; }
    api<ExportInfo>(`/api/exports/${exp.id}`).then((d) => { setDetail(d); setFile((f) => f && d.spine.includes(f) ? f : d.spine[0] || null); });
  }, [exp?.id]);

  const target = useMemo(() => (current && detail?.mapping?.[current]) || null, [current, detail]);
  const shownFile = target?.file || file;

  const src = useMemo(() => {
    if (!detail || !shownFile) return null;
    const qs = new URLSearchParams({ font: String(font), paged: String(paged), theme });
    if (current && target) qs.set("hl", current);
    return `/api/exports/${detail.id}/files/OEBPS/${shownFile}?${qs}${target ? `#${target.anchor}` : ""}`;
  }, [detail, shownFile, font, paged, theme, current, target]);

  function onLoad() {
    // iframe sem scripts (sandbox sem allow-scripts); o pai escuta cliques para sincronizar a seleção
    const d = frame.current?.contentDocument;
    if (!d) return;
    d.addEventListener("click", (ev) => {
      const el = (ev.target as HTMLElement | null)?.closest?.("[id^='e-']");
      if (el) { ev.preventDefault(); onPick(el.id.slice(2)); }
    });
  }

  const size = PRESETS[preset];
  return (
    <div className="pane" style={{ flex: 1 }}>
      <div className="pane-head">
        <strong>EPUB exportado</strong>
        {exp ? (
          <>
            <span className={`badge ${exp.stale ? "warn" : "ok"}`} title={exp.stale ? "A seleção mudou desde esta exportação" : "Corresponde à versão atual"}>
              {exp.mode === "fixed" ? "fixo" : "fluido"} · {exp.stale ? "desatualizada" : "atual"}
            </span>
            <select value={shownFile || ""} onChange={(e) => setFile(e.target.value)} aria-label="Documento do EPUB">
              {(detail?.spine || []).map((f) => <option key={f} value={f}>{f.replace("text/", "")}</option>)}
            </select>
          </>
        ) : <span className="muted">gere o EPUB para ver a prévia</span>}
        <span className="grow" />
        <select value={preset} onChange={(e) => setPreset(e.target.value)} aria-label="Tamanho de tela">
          {Object.keys(PRESETS).map((k) => <option key={k}>{k}</option>)}
        </select>
        {exp?.mode !== "fixed" && (<>
          <label className="row" title="Tamanho da fonte do leitor">A
            <input type="range" min={60} max={200} step={10} value={font} onChange={(e) => setFont(+e.target.value)} aria-label="Tamanho da fonte" style={{ width: 70 }} />
          </label>
          <select value={paged ? "p" : "c"} onChange={(e) => setPaged(e.target.value === "p")} aria-label="Modo de leitura">
            <option value="c">Contínua</option><option value="p">Paginada</option>
          </select>
        </>)}
        <select value={theme} onChange={(e) => setTheme(e.target.value)} aria-label="Tema do leitor">
          <option value="light">Claro</option><option value="sepia">Sépia</option><option value="dark">Escuro</option>
        </select>
      </div>
      <div className="pane-body" style={{ display: "flex", justifyContent: "center", alignItems: size ? "flex-start" : "stretch" }}>
        {src ? (
          <iframe ref={frame} key={src.split("#")[0]} src={src} onLoad={onLoad} title="Prévia do EPUB"
            sandbox="allow-same-origin"
            style={{ border: 0, background: "#fff", width: size ? size[0] : "100%", height: size ? size[1] : "100%",
              margin: size ? 12 : 0, boxShadow: size ? "0 1px 6px rgba(0,0,0,.3)" : undefined }} />
        ) : <p className="muted" style={{ padding: 12 }}>Sem exportação.</p>}
      </div>
    </div>
  );
}
