"use client";
import { useEffect, useMemo, useState } from "react";
import { api, chaptersOf, JOB_LABEL, STATUS_LABEL, type DocumentModel, type Job, type Proposal } from "@/lib/api";
import { parsePageRanges } from "@/lib/geometry";

export function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: React.ReactNode }) {
  useEffect(() => {
    const h = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [onClose]);
  return (
    <div className="overlay-modal" role="dialog" aria-modal="true" aria-label={title} onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal"><h2>{title}</h2>{children}</div>
    </div>
  );
}

function ScopePreview({ projectId, kind, params }: { projectId: string; kind: string; params: any }) {
  const [p, setP] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    setErr(null);
    api(`/api/projects/${projectId}/ai/preview`, { method: "POST", json: { kind, params } }).then(setP).catch((e) => setErr(e.message));
  }, [projectId, kind, JSON.stringify(params)]);
  if (err) return <p className="error">{err}</p>;
  if (!p) return <p className="muted">Calculando escopo…</p>;
  return (
    <div className="card" style={{ padding: 10 }} aria-live="polite">
      <strong>O que será enviado</strong>
      {p.available ? (
        <ul style={{ margin: "6px 0", paddingLeft: 18 }}>
          <li>Destino: <strong>{p.destination}</strong> · modelo <code>{p.model}</code></li>
          {p.pages && <li>Páginas: {p.pages.join(", ")} · {p.images_per_iteration} imagens por iteração · até {p.max_calls} chamadas · {p.render_dpi} dpi</li>}
          {p.text_chars != null && <li>Texto: ~{p.text_chars.toLocaleString("pt-BR")} caracteres (~{p.estimated_input_tokens?.toLocaleString("pt-BR")} tokens estimados)</li>}
          {p.text_chars_per_iteration != null && <li>Trechos de texto por iteração: ~{p.text_chars_per_iteration.toLocaleString("pt-BR")} caracteres</li>}
          <li>PDF completo: {p.sends_full_pdf ? "sim" : "não"}</li>
          {p.estimated_cost_usd != null && <li>Custo estimado: US$ {p.estimated_cost_usd} ({p.price_source})</li>}
          <li className="muted">{p.note}</li>
        </ul>
      ) : <p className="error">{p.reason}</p>}
    </div>
  );
}

export function VisualDialog({ projectId, doc, onClose, onStarted }: { projectId: string; doc: DocumentModel; onClose: () => void; onStarted: () => void }) {
  const [pages, setPages] = useState("1");
  const [iters, setIters] = useState(3);
  const [intensity, setIntensity] = useState("moderada");
  const [auto, setAuto] = useState(false);
  const [mode, setMode] = useState("fluid");
  const [time, setTime] = useState(20);
  const [err, setErr] = useState<string | null>(null);
  const params = useMemo(() => ({ pages: parsePageRanges(pages, doc.pages.length), max_iterations: iters, intensity,
    auto_apply: auto, mode, time_limit_s: time * 60 }), [pages, iters, intensity, auto, mode, time, doc.pages.length]);
  async function start() {
    try {
      await api(`/api/projects/${projectId}/jobs`, { method: "POST", json: { kind: "visual", params } });
      onStarted(); onClose();
    } catch (e: any) { setErr(e.message); }
  }
  return (
    <Modal title="Comparar visualmente com IA" onClose={onClose}>
      <p className="muted">Compara a renderização real do EPUB exportado com o PDF (itens excluídos ficam mascarados) e propõe ajustes reversíveis.</p>
      <label className="field">Páginas (ex.: 1-3, 5)
        <input type="text" value={pages} onChange={(e) => setPages(e.target.value)} />
      </label>
      <div className="row">
        <label className="field">Saída
          <select value={mode} onChange={(e) => setMode(e.target.value)}><option value="fluid">Fluido</option><option value="fixed">Layout fixo</option></select>
        </label>
        <label className="field">Iterações máx.
          <input type="number" min={1} max={5} value={iters} onChange={(e) => setIters(+e.target.value)} />
        </label>
        <label className="field">Intensidade
          <select value={intensity} onChange={(e) => setIntensity(e.target.value)}>
            <option value="leve">Leve (≤3 ops)</option><option value="moderada">Moderada (≤8)</option><option value="forte">Forte (≤15)</option>
          </select>
        </label>
        <label className="field">Tempo máx. (min)
          <input type="number" min={1} max={120} value={time} onChange={(e) => setTime(+e.target.value)} />
        </label>
      </div>
      <label className="check"><input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} />
        Aplicar automaticamente as melhorias aceitas (sempre com histórico e desfazer)</label>
      <ScopePreview projectId={projectId} kind="visual" params={params} />
      {err && <p className="error">{err}</p>}
      <div className="row"><button className="primary" onClick={start} disabled={!params.pages.length}>Iniciar</button><button onClick={onClose}>Cancelar</button></div>
    </Modal>
  );
}

export function EditorialDialog({ projectId, doc, onClose, onStarted }: { projectId: string; doc: DocumentModel; onClose: () => void; onStarted: () => void }) {
  const chapters = useMemo(() => chaptersOf(doc, true), [doc]);
  const [sel, setSel] = useState<Set<number>>(new Set(chapters.map((c) => c.index)));
  const [profile, setProfile] = useState("tecnico");
  const [instructions, setInstructions] = useState("");
  const [applyMode, setApplyMode] = useState("selected");
  const [ocr, setOcr] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const params = useMemo(() => ({ profile, instructions, apply_mode: applyMode, ocr_correction: ocr,
    chapters: sel.size === chapters.length ? null : [...sel] }), [profile, instructions, applyMode, ocr, sel, chapters.length]);
  async function start() {
    try {
      await api(`/api/projects/${projectId}/jobs`, { method: "POST", json: { kind: "editorial", params } });
      onStarted(); onClose();
    } catch (e: any) { setErr(e.message); }
  }
  return (
    <Modal title="Reformatar com IA" onClose={onClose}>
      <p className="muted">Lê XHTML, CSS e sumário do EPUB e propõe melhorias de apresentação. Por padrão é formatação, não reescrita.</p>
      <label className="field">Perfil
        <select value={profile} onChange={(e) => setProfile(e.target.value)}>
          <option value="tecnico">Livro técnico</option><option value="academico">Acadêmico</option>
          <option value="narrativa">Narrativa</option><option value="tela_pequena">Leitura em tela pequena</option>
        </select>
      </label>
      <label className="field">Instruções adicionais
        <textarea rows={3} maxLength={2000} value={instructions} onChange={(e) => setInstructions(e.target.value)} />
      </label>
      <fieldset>
        <legend>Capítulos</legend>
        <div style={{ maxHeight: 140, overflow: "auto" }}>
          {chapters.map((c) => (
            <label key={c.index} className="check"><input type="checkbox" checked={sel.has(c.index)}
              onChange={(e) => setSel((s) => { const n = new Set(s); if (e.target.checked) n.add(c.index); else n.delete(c.index); return n; })} />
              {c.index + 1}. {c.title.slice(0, 70)}</label>
          ))}
        </div>
      </fieldset>
      <fieldset>
        <legend>Aplicação</legend>
        <label className="check"><input type="radio" name="am" checked={applyMode === "selected"} onChange={() => setApplyMode("selected")} /> Revisar e escolher alterações</label>
        <label className="check"><input type="radio" name="am" checked={applyMode === "css"} onChange={() => setApplyMode("css")} /> Aplicar somente CSS (estilos)</label>
        <label className="check"><input type="radio" name="am" checked={applyMode === "css_structure"} onChange={() => setApplyMode("css_structure")} /> Aplicar CSS e estrutura</label>
      </fieldset>
      <label className="check"><input type="checkbox" checked={ocr} onChange={(e) => setOcr(e.target.checked)} />
        Permitir correção de texto de OCR (sempre revisável com comparação; nunca aplicada automaticamente)</label>
      <ScopePreview projectId={projectId} kind="editorial" params={params} />
      {err && <p className="error">{err}</p>}
      <div className="row"><button className="primary" onClick={start} disabled={!sel.size}>Iniciar</button><button onClick={onClose}>Cancelar</button></div>
    </Modal>
  );
}

export function ExportDialog({ mode, onMode, onClose, onExport }: { mode: string; onMode: (m: string) => void; onClose: () => void; onExport: () => void }) {
  return (
    <Modal title="Gerar EPUB" onClose={onClose}>
      <label className="check"><input type="radio" name="m" checked={mode === "fluid"} onChange={() => onMode("fluid")} />
        <span><strong>Fluido (recomendado)</strong> — o texto se adapta à tela, à fonte e às preferências do leitor. Paginação não reproduz o PDF.</span></label>
      <label className="check"><input type="radio" name="m" checked={mode === "fixed"} onChange={() => onMode("fixed")} />
        <span><strong>Layout fixo</strong> — preserva posição e escala de cada página. Em telas pequenas exige zoom, o leitor não pode
          ajustar fonte/margens, acessibilidade é reduzida e o suporte varia entre leitores. Gráficos vetoriais de fundo não são reproduzidos.</span></label>
      <div className="row"><button className="primary" onClick={() => { onExport(); onClose(); }}>Gerar e validar</button><button onClick={onClose}>Cancelar</button></div>
    </Modal>
  );
}

function wordDiff(a: string, b: string) {
  const x = a.split(/(\s+)/), y = b.split(/(\s+)/);
  const n = x.length, m = y.length;
  if (n * m > 400_000) return <><del>{a}</del> <ins>{b}</ins></>;
  const dp = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--)
    dp[i][j] = x[i] === y[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  const out: React.ReactNode[] = [];
  let i = 0, j = 0, k = 0;
  while (i < n && j < m) {
    if (x[i] === y[j]) { out.push(x[i]); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) out.push(<del key={k++}>{x[i++]}</del>);
    else out.push(<ins key={k++}>{y[j++]}</ins>);
  }
  while (i < n) out.push(<del key={k++}>{x[i++]}</del>);
  while (j < m) out.push(<ins key={k++}>{y[j++]}</ins>);
  return <span className="diff">{out}</span>;
}

function describeOp(op: any): string {
  if (!op) return "";
  const t = op.ids ? op.ids.join(", ") : op.id || op.figure_id || "";
  switch (op.op) {
    case "set_style": return `estilo ${JSON.stringify(op.style)} em ${t}`;
    case "set_global_style": return `estilo global ${JSON.stringify(op.props)}`;
    case "set_heading_level": return `título ${t} → H${op.level}`;
    case "set_type": return `tipo de ${t} → ${op.type}`;
    case "link_caption": return `legenda de ${t} → ${op.caption_id}`;
    case "move": return `mover ${t} ${op.before_id ? `antes de ${op.before_id}` : `depois de ${op.after_id}`}`;
    case "set_chapter_break": return `${op.value ? "iniciar" : "remover"} capítulo em ${t}`;
    case "split": return `dividir ${t} na posição ${op.offset}`;
    case "merge": return `unir ${t}`;
    case "set_alt": return `texto alternativo de ${t}`;
    case "set_text": return `corrigir texto de ${t}`;
    default: return op.op;
  }
}

export function ProposalsDrawer({ projectId, onClose, onApplied }: { projectId: string; onClose: () => void; onApplied: () => void }) {
  const [items, setItems] = useState<Proposal[] | null>(null);
  const [checked, setChecked] = useState<Record<string, Set<number>>>({});
  const [err, setErr] = useState<string | null>(null);
  const load = () => api<Proposal[]>(`/api/projects/${projectId}/ai/proposals`).then(setItems).catch((e) => setErr(e.message));
  useEffect(() => { load(); }, [projectId]);

  async function apply(p: Proposal, all: boolean) {
    setErr(null);
    try {
      const idx = all ? null : [...(checked[p.id] || new Set())];
      await api(`/api/proposals/${p.id}/apply`, { method: "POST", json: { indexes: idx } });
      onApplied(); load();
    } catch (e: any) { setErr(e.message); }
  }
  async function reject(p: Proposal) { await api(`/api/proposals/${p.id}/reject`, { method: "POST" }); load(); }

  return (
    <aside className="drawer" aria-label="Propostas de IA">
      <div className="row"><h2 className="grow" style={{ margin: 0 }}>Propostas de IA</h2><button onClick={onClose} aria-label="Fechar">✕</button></div>
      {err && <p className="error">{err}</p>}
      {!items ? <p>Carregando…</p> : items.length === 0 ? <p className="muted">Nenhuma proposta.</p> : items.map((p) => (
        <div key={p.id} className="card" style={{ padding: 10 }}>
          <div className="row">
            <strong>{p.kind === "visual" ? "Fidelidade visual" : "Editorial"}</strong>
            <span className="badge">{p.unit === "final" ? "resultado final" : `iteração ${p.iteration + 1}`}</span>
            <span className={`badge ${p.status === "applied" ? "ok" : p.status === "rejected" || p.status === "reverted" ? "err" : "warn"}`}>{p.status}</span>
            <span className="muted grow" style={{ textAlign: "right", fontSize: 12 }}>{new Date(p.created_at).toLocaleString("pt-BR")}</span>
          </div>
          {p.diagnosis && <p style={{ whiteSpace: "pre-wrap" }}>{p.diagnosis}</p>}
          {p.metrics?.scores && <p className="muted" style={{ fontSize: 12 }}>
            Pontuação do modelo por página (indicador, não garantia): {Object.entries(p.metrics.scores).map(([k, v]) => `p.${+k + 1}: ${v}`).join(" · ")}
            {p.metrics.ssim && Object.keys(p.metrics.ssim).length > 0 && <> · SSIM (auxiliar): {Object.entries(p.metrics.ssim).map(([k, v]) => `p.${+k + 1}: ${v}`).join(" · ")}</>}
            {" "}· EPUBCheck erros: {String(p.metrics.epubcheck_errors ?? "—")} · integridade: {p.metrics.integrity_ok ? "ok" : "falhou"}
            {p.metrics.reason && <> · {p.metrics.reason}</>}
          </p>}
          {p.metrics?.coverage && (
            <table className="list" style={{ fontSize: 12 }}><thead><tr><th>Capítulo</th><th>Unidades</th><th>Cobertura</th></tr></thead>
              <tbody>{p.metrics.coverage.map((c: any) => (
                <tr key={c.chapter}><td>{c.chapter + 1}. {c.title}</td><td>{c.units_done}/{c.units}</td>
                  <td><span className={`badge ${c.status === "revisado" ? "ok" : "warn"}`}>{c.status}</span></td></tr>))}</tbody></table>
          )}
          {p.metrics?.text_changed_without_permission?.length > 0 && <p className="error">Texto alterado sem autorização: proposta rejeitada.</p>}
          <ul style={{ paddingLeft: 0, listStyle: "none", fontSize: 12 }}>
            {p.ops.map((o, i) => (
              <li key={i} style={{ padding: "3px 0", borderBottom: "1px solid var(--border)" }}>
                <label className="check" style={{ margin: 0, alignItems: "flex-start" }}>
                  {(p.status === "pending" || p.status === "partial") && (o.status === "pending" || o.status === "valid") && (
                    <input type="checkbox" checked={checked[p.id]?.has(i) || false}
                      onChange={(e) => setChecked((c) => { const s = new Set(c[p.id] || []); if (e.target.checked) s.add(i); else s.delete(i); return { ...c, [p.id]: s }; })} />
                  )}
                  <span>
                    <span className={`badge ${o.status === "applied" ? "ok" : o.status === "invalid" ? "err" : ""}`}>{o.status}</span>{" "}
                    {o.op ? describeOp(o.op) : o.ai?.op} {o.op?.reason || o.ai?.reason ? <span className="muted">— {o.op?.reason || o.ai?.reason}</span> : null}
                    {o.error && <div className="error">{o.error}</div>}
                    {o.op?.op === "set_text" && o.old_text != null && <div style={{ marginTop: 4 }}>{wordDiff(o.old_text, o.op.text)}</div>}
                  </span>
                </label>
              </li>
            ))}
          </ul>
          {(p.status === "pending" || p.status === "partial") && (
            <div className="row">
              <button className="primary small" onClick={() => apply(p, true)}>Aplicar todas válidas</button>
              <button className="small" onClick={() => apply(p, false)} disabled={!checked[p.id]?.size}>Aplicar selecionadas</button>
              <button className="small danger" onClick={() => reject(p)}>Rejeitar</button>
            </div>
          )}
        </div>
      ))}
    </aside>
  );
}

export function JobsPanel({ jobs, onChanged }: { jobs: Job[]; onChanged: () => void }) {
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const visible = jobs.filter((j) => !hidden.has(j.id) && (j.status === "running" || j.status === "queued" ||
    (j.finished_at && Date.now() - new Date(j.finished_at).getTime() < 120_000)));
  if (!visible.length) return null;
  return (
    <div className="jobs" aria-live="polite" aria-label="Tarefas">
      {visible.slice(0, 4).map((j) => (
        <div key={j.id} className="jobcard">
          <div className="row">
            <strong className="grow">{JOB_LABEL[j.kind] || j.kind}</strong>
            <span className={`badge ${j.status === "succeeded" ? "ok" : j.status === "failed" ? "err" : "warn"}`}>{STATUS_LABEL[j.status] || j.status}</span>
            <button className="small" aria-label="Ocultar" onClick={() => setHidden((s) => new Set(s).add(j.id))}>✕</button>
          </div>
          {(j.status === "running" || j.status === "queued") && <div className="progress" style={{ margin: "6px 0" }}><div style={{ width: `${Math.round(j.progress * 100)}%` }} /></div>}
          <div className="muted">{j.message}</div>
          {j.steps.filter((s) => s.status === "error").map((s) => <div key={s.name} className="error">Etapa “{s.name}”: {s.error}</div>)}
          {j.error && <div className="error">{j.error}</div>}
          <div className="row" style={{ marginTop: 4 }}>
            {(j.status === "running" || j.status === "queued") && !j.cancel_requested &&
              <button className="small" onClick={() => api(`/api/jobs/${j.id}/cancel`, { method: "POST" }).then(onChanged)}>Cancelar</button>}
            {j.cancel_requested && j.status === "running" && <span className="muted">cancelando…</span>}
            {(j.status === "failed" || j.status === "cancelled") &&
              <button className="small" onClick={() => api(`/api/jobs/${j.id}/retry`, { method: "POST" }).then(onChanged)}>Repetir etapas pendentes</button>}
          </div>
        </div>
      ))}
    </div>
  );
}
