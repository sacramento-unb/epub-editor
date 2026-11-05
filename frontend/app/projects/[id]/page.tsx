"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "next/navigation";
import { api, ApiError, chaptersOf, type DocPayload, type ExportInfo, type Job, type Project } from "@/lib/api";
import { parsePageRanges, type BBox } from "@/lib/geometry";
import { PdfView } from "@/components/PdfView";
import { Tree } from "@/components/Tree";
import { Inspector } from "@/components/Inspector";
import { EpubPreview } from "@/components/EpubPreview";
import { EditorialDialog, ExportDialog, JobsPanel, Modal, ProposalsDrawer, VisualDialog } from "@/components/Panels";

export default function Workspace() {
  const { id } = useParams<{ id: string }>();
  const [project, setProject] = useState<Project | null>(null);
  const [payload, setPayload] = useState<DocPayload | null>(null);
  const [exports, setExports] = useState<ExportInfo[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [current, setCurrent] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [overlays, setOverlays] = useState(true);
  const [saving, setSaving] = useState<"idle" | "saving" | "saved" | "error">("idle");
  const [message, setMessage] = useState<string | null>(null);
  const [leftW, setLeftW] = useState(360);
  const [leftCollapsed, setLeftCollapsed] = useState(false);
  const [splitPct, setSplitPct] = useState(50);
  const [dialog, setDialog] = useState<null | "export" | "visual" | "editorial" | "proposals" | "region" | "impact">(null);
  const [region, setRegion] = useState<{ page: number; bbox: BBox; type: string } | null>(null);
  const [impact, setImpact] = useState<{ items: any[]; run: () => void } | null>(null);
  const [tokens, setTokens] = useState<Record<string, string[]>>({});
  const anchorSel = useRef<string | null>(null);

  const doc = payload?.document;
  const rev = payload?.revision;
  const latestExport = exports[0] || null;

  const loadDoc = useCallback(async () => {
    try { setPayload(await api<DocPayload>(`/api/projects/${id}/document`)); } catch (e) { if (!(e instanceof ApiError && e.status === 409)) setMessage(String((e as Error).message)); }
  }, [id]);
  const loadProject = useCallback(async () => {
    const p = await api<Project>(`/api/projects/${id}`);
    setProject(p);
    setJobs(p.jobs || []);
  }, [id]);
  const loadExports = useCallback(async () => setExports(await api<ExportInfo[]>(`/api/projects/${id}/exports`)), [id]);

  useEffect(() => {
    loadProject(); loadDoc(); loadExports();
    api("/api/health").then((h) => setTokens(h.style_tokens || {})).catch(() => undefined);
  }, [loadProject, loadDoc, loadExports]);

  // progresso por SSE (reconexão automática do EventSource) + consulta periódica de reserva
  const known = useRef(new Map<string, string>());
  const onJob = useCallback((j: Job) => {
    setJobs((js) => [j, ...js.filter((x) => x.id !== j.id)].sort((a, b) => b.created_at.localeCompare(a.created_at)));
    const prev = known.current.get(j.id);
    known.current.set(j.id, j.status);
    if (prev && prev !== j.status && j.status === "succeeded") {
      if (j.kind === "extract") { loadDoc(); loadProject(); }
      if (j.kind === "export" || j.kind === "validate") loadExports();
      if (j.kind === "visual" || j.kind === "editorial") { loadDoc(); setMessage("Tarefa de IA concluída — veja as propostas."); }
    }
  }, [loadDoc, loadExports, loadProject]);
  useEffect(() => {
    const es = new EventSource(`/api/projects/${id}/events`);
    es.addEventListener("job", (ev) => onJob(JSON.parse((ev as MessageEvent).data) as Job));
    es.addEventListener("head", () => loadDoc());
    return () => es.close();
  }, [id, loadDoc, onJob]);
  const active = jobs.some((j) => j.status === "queued" || j.status === "running");
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => api<Job[]>(`/api/projects/${id}/jobs`).then((js) => js.forEach(onJob)).catch(() => undefined), 3000);
    return () => clearInterval(t);
  }, [active, id, onJob]);

  const applyOps = useCallback(async (ops: any[], label?: string) => {
    if (!rev) return;
    setSaving("saving");
    try {
      const r = await api<DocPayload>(`/api/projects/${id}/ops`, { method: "POST", json: { base_revision_id: rev.id, ops, label } });
      setPayload(r);
      setSaving("saved");
      loadExports(); // a seleção mudou: exportações anteriores ficam desatualizadas
    } catch (e) {
      setSaving("error");
      if (e instanceof ApiError && e.status === 409) { setMessage("O documento mudou em outro lugar; recarregado."); loadDoc(); }
      else setMessage((e as Error).message);
    }
  }, [id, rev, loadDoc, loadExports]);

  const setIncluded = useCallback(async (ids: string[], included: boolean, mode: "item" | "batch") => {
    const run = () => applyOps([{ op: "set_included", ids, included, mode }]);
    if (included) return run();
    const r = await api<{ impact: any[] }>(`/api/projects/${id}/impact`, { method: "POST", json: { ids } });
    if (r.impact.length) { setImpact({ items: r.impact, run }); setDialog("impact"); }
    else run();
  }, [applyOps, id]);

  const select = useCallback((eid: string, mode: "single" | "toggle" | "range") => {
    setCurrent(eid);
    setSelected((s) => {
      if (mode === "single") { anchorSel.current = eid; return new Set([eid]); }
      if (mode === "toggle") { const n = new Set(s); if (n.has(eid)) n.delete(eid); else n.add(eid); anchorSel.current = eid; return n; }
      const ids = doc?.elements.map((e) => e.id) || [];
      const a = ids.indexOf(anchorSel.current || eid), b = ids.indexOf(eid);
      return new Set(ids.slice(Math.min(a, b), Math.max(a, b) + 1));
    });
  }, [doc]);

  // atalhos de teclado (fora de campos de texto)
  useEffect(() => {
    const h = (ev: KeyboardEvent) => {
      const t = ev.target as HTMLElement;
      if (["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName)) return;
      const mod = ev.ctrlKey || ev.metaKey;
      if (mod && ev.key.toLowerCase() === "z" && !ev.shiftKey) { ev.preventDefault(); undo(); }
      else if (mod && (ev.key.toLowerCase() === "y" || (ev.key.toLowerCase() === "z" && ev.shiftKey))) { ev.preventDefault(); redo(); }
      else if (ev.key === "Delete" && selected.size) { ev.preventDefault(); setIncluded([...selected], false, "batch"); }
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  });

  async function undo() { try { setPayload(await api(`/api/projects/${id}/undo`, { method: "POST" })); loadExports(); } catch (e) { setMessage((e as Error).message); } }
  async function redo() { try { setPayload(await api(`/api/projects/${id}/redo`, { method: "POST" })); loadExports(); } catch (e) { setMessage((e as Error).message); } }
  async function startJob(kind: string, params: any = {}) {
    try {
      const j = await api<Job>(`/api/projects/${id}/jobs`, { method: "POST", json: { kind, params } });
      onJob(j);
    } catch (e) { setMessage((e as Error).message); }
  }

  const currentEl = useMemo(() => doc?.elements.find((e) => e.id === current) || null, [doc, current]);
  const chapters = useMemo(() => (doc ? chaptersOf(doc) : []), [doc]);

  function batch(scope: "selection" | "page" | "chapter" | "pages" | "equivalent", included: boolean) {
    if (!doc) return;
    let ids: string[] = [];
    if (scope === "selection") ids = [...selected];
    if (scope === "page" && currentEl) ids = doc.elements.filter((e) => e.sources[0]?.page === currentEl.sources[0]?.page).map((e) => e.id);
    if (scope === "chapter" && current) ids = chapters.find((c) => c.ids.includes(current))?.ids || [];
    if (scope === "pages") {
      const r = window.prompt("Páginas (ex.: 1-3, 7):");
      if (!r) return;
      const pages = new Set(parsePageRanges(r, doc.pages.length));
      const t = currentEl?.type;
      const onlyType = t && window.confirm(`Aplicar somente a elementos do tipo atual (${t})? Cancelar = todos os tipos.`);
      ids = doc.elements.filter((e) => pages.has(e.sources[0]?.page ?? -1) && (!onlyType || e.type === t)).map((e) => e.id);
    }
    if (scope === "equivalent" && current) {
      api<{ ids: string[] }>(`/api/projects/${id}/equivalents/${current}`).then((r) => {
        if (window.confirm(`${r.ids.length} elementos equivalentes (mesmo tipo e padrão de texto). ${included ? "Incluir" : "Excluir"} todos?`))
          setIncluded(r.ids, included, "batch");
      });
      return;
    }
    if (ids.length) setIncluded(ids, included, "batch");
  }

  function startResize(kind: "left" | "split", ev: React.MouseEvent) {
    ev.preventDefault();
    const startX = ev.clientX, startW = leftW, startPct = splitPct;
    const main = (ev.currentTarget.parentElement as HTMLElement).getBoundingClientRect();
    const move = (e: MouseEvent) => {
      if (kind === "left") setLeftW(Math.min(700, Math.max(220, startW + e.clientX - startX)));
      else setSplitPct(Math.min(85, Math.max(15, startPct + ((e.clientX - startX) / main.width) * 100)));
    };
    const up = () => { window.removeEventListener("mousemove", move); window.removeEventListener("mouseup", up); };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  }

  if (!project) return <main className="page"><p>Carregando projeto…</p></main>;
  const extracting = jobs.some((j) => j.kind === "extract" && (j.status === "running" || j.status === "queued"));

  return (
    <div className="ws">
      {leftCollapsed ? (
        <div className="collapsed-bar" role="button" tabIndex={0} onClick={() => setLeftCollapsed(false)}
          onKeyDown={(e) => e.key === "Enter" && setLeftCollapsed(false)} aria-label="Mostrar painel de elementos">Elementos ▸</div>
      ) : (
        <>
          <aside className="ws-left" style={{ width: leftW }} aria-label="Elementos">
            <div className="row" style={{ padding: "6px 8px", borderBottom: "1px solid var(--border)" }}>
              <strong className="grow" title={project.original_filename}>{project.name}</strong>
              <button className="small" onClick={() => setLeftCollapsed(true)} aria-label="Recolher painel">◂</button>
            </div>
            {doc ? (
              <>
                <Tree projectId={id} doc={doc} current={current} selected={selected} onSelect={select}
                  onSetIncluded={setIncluded} onMove={(ids, before) => applyOps([{ op: "move", ids, before_id: before }], "Reordenar")} />
                <Inspector doc={doc} element={currentEl} selectedCount={selected.size} styleTokens={tokens} onOps={applyOps}
                  onGroupSelected={() => applyOps([{ op: "group", ids: doc.elements.filter((e) => selected.has(e.id)).map((e) => e.id), type: "figure" }], "Agrupar")} />
              </>
            ) : <p style={{ padding: 12 }}>{extracting ? "Analisando o PDF… acompanhe o progresso no canto inferior." : project.error || "Sem documento."}</p>}
          </aside>
          <div className="resizer" onMouseDown={(e) => startResize("left", e)} role="separator" aria-label="Redimensionar painel" />
        </>
      )}
      <main className="ws-main">
        <div className="ws-toolbar" role="toolbar" aria-label="Ações">
          <button onClick={() => window.confirm("Reanalisar o PDF cria uma nova versão a partir da extração (edições atuais ficam no histórico). Continuar?") && startJob("extract")} disabled={extracting}>Analisar PDF</button>
          <button className="primary" onClick={() => setDialog("export")} disabled={!doc}>Gerar EPUB</button>
          <button onClick={() => setDialog("visual")} disabled={!doc}>Comparar visualmente com IA</button>
          <button onClick={() => setDialog("editorial")} disabled={!doc}>Reformatar com IA</button>
          <button onClick={() => latestExport && startJob("validate", { export_id: latestExport.id })} disabled={!latestExport}>Validar</button>
          <a href={latestExport ? `/api/exports/${latestExport.id}/download` : undefined} aria-disabled={!latestExport}>
            <button disabled={!latestExport}>Baixar EPUB</button></a>
          <span style={{ width: 8 }} />
          <button onClick={undo} disabled={!rev?.can_undo} title="Desfazer (Ctrl+Z)">↶</button>
          <button onClick={redo} disabled={!rev?.can_redo} title="Refazer (Ctrl+Shift+Z)">↷</button>
          <button onClick={() => { const l = window.prompt("Nome da versão:"); if (l) api(`/api/projects/${id}/versions`, { method: "POST", json: { label: l } }).then(() => setMessage("Versão salva.")); }} disabled={!rev}>Salvar versão</button>
          <button onClick={() => setDialog("proposals")}>Propostas de IA</button>
          <a href={`/projects/${id}/history`}>Histórico</a>
          <span className="grow" />
          <label className="check" title="Oculta apenas os destaques; não altera o que é exportado">
            <input type="checkbox" checked={overlays} onChange={(e) => setOverlays(e.target.checked)} /> Mostrar detecções</label>
          <span className="muted" aria-live="polite" style={{ fontSize: 12 }}>
            {saving === "saving" ? "Salvando…" : saving === "saved" ? `Salvo · rev. ${rev?.seq}` : saving === "error" ? "Erro ao salvar" : rev ? `rev. ${rev.seq}` : ""}
          </span>
        </div>
        {selected.size > 0 && doc && (
          <div className="ws-toolbar" style={{ fontSize: 12 }} aria-label="Ações em lote">
            <span>{selected.size} selecionado(s)</span>
            <button className="small" onClick={() => batch("selection", true)}>Incluir</button>
            <button className="small" onClick={() => batch("selection", false)}>Excluir</button>
            <span className="muted">Aplicar a:</span>
            <button className="small" onClick={() => batch("page", false)} disabled={!currentEl}>Excluir página</button>
            <button className="small" onClick={() => batch("page", true)} disabled={!currentEl}>Incluir página</button>
            <button className="small" onClick={() => batch("chapter", false)}>Excluir capítulo</button>
            <button className="small" onClick={() => batch("chapter", true)}>Incluir capítulo</button>
            <button className="small" onClick={() => batch("pages", false)}>Excluir em páginas…</button>
            <button className="small" onClick={() => batch("equivalent", false)} disabled={!current}>Excluir equivalentes</button>
            <button className="small" onClick={() => batch("equivalent", true)} disabled={!current}>Incluir equivalentes</button>
            <button className="small" onClick={() => setSelected(new Set())}>Limpar seleção</button>
          </div>
        )}
        {message && <div className="ws-toolbar" role="status"><span className="grow">{message}</span><button className="small" onClick={() => setMessage(null)}>OK</button></div>}
        {doc?.warnings?.length ? <details className="ws-toolbar" style={{ fontSize: 12 }}><summary>{doc.warnings.length} aviso(s) da análise</summary><ul>{doc.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul></details> : null}
        <div className="ws-split">
          <div style={{ width: `${splitPct}%`, display: "flex", minWidth: 0 }}>
            {doc && <PdfView projectId={id} doc={doc} current={current} selected={selected} showOverlays={overlays}
              onPick={(eid, add) => select(eid, add ? "toggle" : "single")}
              onRegion={(page, bbox) => { setRegion({ page, bbox, type: "figure" }); setDialog("region"); }} />}
          </div>
          <div className="resizer" onMouseDown={(e) => startResize("split", e)} role="separator" aria-label="Redimensionar divisão" />
          <div style={{ flex: 1, display: "flex", minWidth: 0 }}>
            <EpubPreview exp={latestExport} current={current} onPick={(eid) => select(eid, "single")} />
          </div>
        </div>
      </main>

      <JobsPanel jobs={jobs} onChanged={loadProject} />
      {dialog === "export" && <ExportDialog mode={project.settings.export_mode || "fluid"} onClose={() => setDialog(null)}
        onMode={(m) => api(`/api/projects/${id}`, { method: "PATCH", json: { export_mode: m } }).then(loadProject)}
        onExport={() => startJob("export", { mode: project.settings.export_mode || "fluid" })} />}
      {dialog === "visual" && doc && <VisualDialog projectId={id} doc={doc} onClose={() => setDialog(null)} onStarted={loadProject} />}
      {dialog === "editorial" && doc && <EditorialDialog projectId={id} doc={doc} onClose={() => setDialog(null)} onStarted={loadProject} />}
      {dialog === "proposals" && <ProposalsDrawer projectId={id} onClose={() => setDialog(null)} onApplied={loadDoc} />}
      {dialog === "region" && region && (
        <Modal title="Criar recorte manual" onClose={() => setDialog(null)}>
          <p>Página {region.page + 1}, região [{region.bbox.map((v) => v.toFixed(0)).join(", ")}] pt. O recorte vira imagem identificada como tal; texto excluído sob ele é mascarado.</p>
          <label className="field">Tipo
            <select value={region.type} onChange={(e) => setRegion({ ...region, type: e.target.value })}>
              <option value="figure">Figura</option><option value="formula">Fórmula</option><option value="table">Tabela (como imagem)</option><option value="region">Região</option>
            </select>
          </label>
          <div className="row">
            <button className="primary" onClick={() => { applyOps([{ op: "create_region", page: region.page, bbox: region.bbox, type: region.type }], "Recorte manual"); setDialog(null); }}>Criar</button>
            <button onClick={() => setDialog(null)}>Cancelar</button>
          </div>
        </Modal>
      )}
      {dialog === "impact" && impact && (
        <Modal title="Impacto da exclusão" onClose={() => setDialog(null)}>
          <p>Excluir estes elementos afeta referências. Links e chamadas sem destino viram texto simples na exportação (sem links quebrados).</p>
          <ul>{impact.items.slice(0, 30).map((i, k) => <li key={k}><code>{i.id}</code> — {i.kind}: {i.detail}</li>)}</ul>
          <div className="row">
            <button className="primary" onClick={() => { impact.run(); setDialog(null); }}>Excluir mesmo assim</button>
            <button onClick={() => setDialog(null)}>Cancelar</button>
          </div>
        </Modal>
      )}
    </div>
  );
}
