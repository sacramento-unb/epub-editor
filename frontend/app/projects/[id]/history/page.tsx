"use client";
import { useCallback, useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { api, fmtBytes, JOB_LABEL, STATUS_LABEL, type ExportInfo, type Job } from "@/lib/api";

const KIND: Record<string, string> = { extraction: "extração", edit: "edição", ai_visual: "IA visual", ai_editorial: "IA editorial", restore: "restauração" };

export default function History() {
  const { id } = useParams<{ id: string }>();
  const [revs, setRevs] = useState<any[]>([]);
  const [exps, setExps] = useState<ExportInfo[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [calls, setCalls] = useState<any[]>([]);
  const [audit, setAudit] = useState<any[]>([]);
  const [msg, setMsg] = useState<string | null>(null);
  const load = useCallback(() => {
    api(`/api/projects/${id}/revisions`).then(setRevs);
    api(`/api/projects/${id}/exports`).then(setExps);
    api(`/api/projects/${id}/jobs`).then(setJobs);
    api(`/api/projects/${id}/ai/calls`).then(setCalls);
    api(`/api/projects/${id}/audit`).then(setAudit);
  }, [id]);
  useEffect(load, [load]);

  async function restore(r: any) {
    if (!confirm(`Restaurar a revisão ${r.seq}? Uma nova revisão será criada; nada é apagado.`)) return;
    await api(`/api/projects/${id}/revisions/${r.id}/restore`, { method: "POST" });
    setMsg(`Revisão ${r.seq} restaurada.`);
    load();
  }

  const used = calls.filter((c) => !c.cached);
  return (
    <main className="page">
      <p><a href={`/projects/${id}`}>← Área de trabalho</a></p>
      <h1>Histórico e exportações</h1>
      {msg && <p role="status">{msg}</p>}
      <section className="card">
        <h2>Exportações</h2>
        {exps.length === 0 ? <p className="muted">Nenhuma exportação.</p> : exps.map((e) => (
          <details key={e.id} className="card" style={{ padding: 10 }}>
            <summary className="row">
              <strong>{e.mode === "fixed" ? "Layout fixo" : "Fluido"}</strong>
              <span className="muted">{new Date(e.created_at).toLocaleString("pt-BR")} · rev. {e.revision_id} · {fmtBytes(e.size)}</span>
              {e.stale && <span className="badge warn">desatualizada</span>}
              <span className={`badge ${e.epubcheck.status === "ok" ? "ok" : e.epubcheck.available === false ? "warn" : "err"}`}>
                EPUBCheck {e.epubcheck.version || ""}: {e.epubcheck.status}{e.epubcheck.available !== false ? ` (${e.epubcheck.errors} erros, ${e.epubcheck.warnings} avisos)` : ""}</span>
              <span className={`badge ${e.integrity.ok ? "ok" : "err"}`}>integridade {e.integrity.ok ? "ok" : "com alertas"}</span>
              <a href={`/api/exports/${e.id}/download`}>Baixar</a>
            </summary>
            {e.epubcheck.detail && <p className="muted">{e.epubcheck.detail}</p>}
            {e.epubcheck_messages.length > 0 && <table className="list" style={{ fontSize: 12 }}><tbody>
              {e.epubcheck_messages.map((m, i) => <tr key={i}><td>{m.severity}</td><td>{m.id}</td><td>{m.message}</td><td>{m.path}:{m.line}</td></tr>)}
            </tbody></table>}
            <p style={{ fontSize: 12 }}>Integridade: {e.integrity.checked_elements} elementos verificados.
              {e.integrity.missing?.length ? ` Ausentes: ${e.integrity.missing.map((m: any) => m.id).join(", ")}.` : ""}
              {e.integrity.lost_numbers?.length ? ` Números ausentes: ${e.integrity.lost_numbers.join(", ")}.` : ""}
              {e.integrity.unexpected_words?.length ? ` Texto inesperado: ${e.integrity.unexpected_words.join(", ")}.` : ""}
              {e.integrity.duplicated?.length ? ` Duplicado: ${e.integrity.duplicated.length} trecho(s).` : ""}
              <span className="muted"> {e.integrity.limitations}</span></p>
            {e.warnings.length > 0 && <ul style={{ fontSize: 12 }}>{e.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul>}
          </details>
        ))}
      </section>
      <section className="card">
        <h2>Revisões</h2>
        <table className="list"><thead><tr><th>#</th><th>Tipo</th><th>Rótulo</th><th>Operações</th><th>Quando</th><th></th></tr></thead>
          <tbody>{revs.map((r) => (
            <tr key={r.id}><td>{r.seq}{r.is_head && <span className="badge ok" style={{ marginLeft: 4 }}>atual</span>}</td>
              <td>{KIND[r.kind] || r.kind} · {r.actor}</td><td>{r.named && <span className="badge">versão</span>} {r.label}</td>
              <td className="muted">{r.ops} {r.op_kinds.join(", ")}</td><td>{new Date(r.created_at).toLocaleString("pt-BR")}</td>
              <td>{!r.is_head && <button className="small" onClick={() => restore(r)}>Restaurar</button>}</td></tr>
          ))}</tbody></table>
      </section>
      <section className="card">
        <h2>Tarefas</h2>
        <table className="list"><tbody>{jobs.map((j) => (
          <tr key={j.id}><td>{JOB_LABEL[j.kind] || j.kind}</td><td>{STATUS_LABEL[j.status]}</td>
            <td className="muted">{j.steps.map((s) => `${s.name}: ${s.status}`).join(" · ")}</td>
            <td className="error">{j.error}</td><td>{new Date(j.created_at).toLocaleString("pt-BR")}</td></tr>
        ))}</tbody></table>
      </section>
      <section className="card">
        <h2>Uso de IA</h2>
        <p className="muted">{used.length} chamadas reais · {calls.length - used.length} respostas de cache ·
          tokens de entrada {used.reduce((a, c) => a + (c.input_tokens || 0), 0).toLocaleString("pt-BR")} ·
          saída {used.reduce((a, c) => a + (c.output_tokens || 0), 0).toLocaleString("pt-BR")} (conforme informado pelo provedor) ·
          custo estimado {used.some((c) => c.cost_estimate != null) ? `US$ ${used.reduce((a, c) => a + (c.cost_estimate || 0), 0).toFixed(4)}` : "— (sem preços configurados)"}</p>
        <table className="list" style={{ fontSize: 12 }}><tbody>{calls.slice(0, 50).map((c) => (
          <tr key={c.id}><td>{c.provider}/{c.model}</td><td>{c.purpose}</td><td>{c.cached ? "cache" : c.status}</td>
            <td>{c.input_tokens ?? "—"}/{c.output_tokens ?? "—"}</td><td className="error">{c.error}</td><td>{new Date(c.created_at).toLocaleString("pt-BR")}</td></tr>
        ))}</tbody></table>
      </section>
      <section className="card">
        <h2>Auditoria</h2>
        <table className="list" style={{ fontSize: 12 }}><tbody>{audit.map((a, i) => (
          <tr key={i}><td>{a.action}</td><td className="muted">{JSON.stringify(a.detail)}</td><td>{new Date(a.created_at).toLocaleString("pt-BR")}</td></tr>
        ))}</tbody></table>
      </section>
    </main>
  );
}
