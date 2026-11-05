"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, fmtBytes, type Project } from "@/lib/api";
import { completeUpload, sendChunks, startUpload } from "@/lib/upload";

export default function ProjectsPage() {
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [over, setOver] = useState(false);
  const [pending, setPending] = useState<string | null>(null); // upload_id aguardando senha
  const [progress, setProgress] = useState<number | null>(null);
  const [needPassword, setNeedPassword] = useState(false);
  const [password, setPassword] = useState("");
  const [ocr, setOcr] = useState("por+eng");
  const inputRef = useRef<HTMLInputElement>(null);

  const [limits, setLimits] = useState<{ max_upload_mb: number; max_pages: number } | null>(null);
  useEffect(() => { api("/api/health").then((h) => setLimits(h.limits)).catch(() => undefined); }, []);
  const load = useCallback(() => {
    api<Project[]>("/api/projects").then(setProjects).catch((e) => setError(String(e.message)));
  }, []);
  useEffect(load, [load]);

  async function finish(uploadId: string, pw?: string) {
    try {
      const r = await completeUpload(uploadId, { password: pw, ocr_languages: ocr });
      window.location.href = `/projects/${r.project.id}`;
    } catch (e) {
      if (e instanceof ApiError && e.detail?.code === "password") {
        setPending(uploadId);
        setNeedPassword(true);
      } else setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function send(file: File) {
    setBusy(true);
    setError(null);
    setProgress(0);
    try {
      const id = await startUpload(file);
      await sendChunks(id, file, setProgress);
      await finish(id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
      setProgress(null);
    }
  }

  function pick(files: FileList | null) {
    const f = files?.[0];
    if (!f) return;
    if (!f.name.toLowerCase().endsWith(".pdf") && f.type !== "application/pdf") {
      setError("Selecione um arquivo PDF.");
      return;
    }
    if (limits && f.size > limits.max_upload_mb * 1024 * 1024) {
      setError(`O arquivo tem ${fmtBytes(f.size)}; o limite configurado é ${limits.max_upload_mb} MB (MAX_UPLOAD_MB).`);
      return;
    }
    send(f);
  }

  async function remove(p: Project) {
    if (!confirm(`Excluir o projeto "${p.name}"? O PDF, versões e exportações serão apagados.`)) return;
    try {
      await api(`/api/projects/${p.id}?confirm=true`, { method: "DELETE" });
      load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <main className="page">
      <h1>Projetos</h1>
      <section className="card" aria-label="Importar PDF">
        <div
          className={`dropzone ${over ? "over" : ""}`}
          role="button"
          tabIndex={0}
          aria-label="Arraste um PDF ou pressione Enter para escolher"
          onClick={() => inputRef.current?.click()}
          onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && inputRef.current?.click()}
          onDragOver={(e) => { e.preventDefault(); setOver(true); }}
          onDragLeave={() => setOver(false)}
          onDrop={(e) => { e.preventDefault(); setOver(false); pick(e.dataTransfer.files); }}
        >
          {busy ? (
            <div>
              <p>{progress !== null && progress < 1 ? `Enviando o PDF… ${Math.round(progress * 100)}%` : "Validando o PDF…"}</p>
              <div className="progress" style={{ maxWidth: 320, margin: "0 auto" }}><div style={{ width: `${Math.round((progress ?? 1) * 100)}%` }} /></div>
            </div>
          ) : (
            <>
              <p><strong>Arraste um PDF aqui</strong> ou clique para escolher.</p>
              <p className="muted">{limits ? `Até ${limits.max_upload_mb} MB e ${limits.max_pages} páginas (envio em partes de 8 MB). ` : ""} O original fica imutável. A análise roda em segundo plano, sem IA.</p>
            </>
          )}
          <input ref={inputRef} type="file" accept="application/pdf,.pdf" hidden onChange={(e) => pick(e.target.files)} />
        </div>
        <div className="row" style={{ marginTop: 8 }}>
          <label className="row">Idiomas de OCR
            <select value={ocr} onChange={(e) => setOcr(e.target.value)} aria-label="Idiomas de OCR">
              <option value="por+eng">Português + Inglês</option>
              <option value="por">Português</option>
              <option value="eng">Inglês</option>
            </select>
          </label>
          <span className="muted">OCR só é usado em páginas sem camada de texto.</span>
        </div>
        {error && <p className="error" role="alert">{error}</p>}
      </section>

      {needPassword && pending && (
        <div className="overlay-modal" role="dialog" aria-modal="true" aria-labelledby="pw-title">
          <form className="modal" onSubmit={(e) => { e.preventDefault(); setNeedPassword(false); finish(pending, password); }}>
            <h2 id="pw-title">PDF protegido</h2>
            <p>Este PDF exige senha. Informe-a somente se você tiver autorização para abri-lo. Nenhuma proteção é contornada.</p>
            <label className="field">Senha
              <input type="password" autoFocus value={password} onChange={(e) => setPassword(e.target.value)} />
            </label>
            <div className="row">
              <button className="primary" type="submit">Abrir</button>
              <button type="button" onClick={() => setNeedPassword(false)}>Cancelar</button>
            </div>
          </form>
        </div>
      )}

      <section className="card">
        {projects === null ? <p>Carregando…</p> : projects.length === 0 ? <p className="muted">Nenhum projeto ainda.</p> : (
          <table className="list">
            <thead><tr><th>Nome</th><th>Páginas</th><th>Tamanho</th><th>Estado</th><th>Atualizado</th><th></th></tr></thead>
            <tbody>
              {projects.map((p) => (
                <tr key={p.id}>
                  <td><a href={`/projects/${p.id}`}>{p.name}</a><div className="muted" style={{ fontSize: 12 }}>{p.original_filename}</div></td>
                  <td>{p.page_count}</td>
                  <td>{fmtBytes(p.pdf_size)}</td>
                  <td>
                    <span className={`badge ${p.status === "ready" ? "ok" : p.status === "error" ? "err" : "warn"}`}>
                      {p.status === "ready" ? "pronto" : p.status === "error" ? "erro" : "analisando"}
                    </span>
                    {p.error && <div className="error" style={{ fontSize: 12 }}>{p.error}</div>}
                  </td>
                  <td>{new Date(p.updated_at).toLocaleString("pt-BR")}</td>
                  <td className="row">
                    <a href={`/projects/${p.id}/history`}>Histórico</a>
                    <button className="small danger" onClick={() => remove(p)} aria-label={`Excluir ${p.name}`}>Excluir</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </main>
  );
}
