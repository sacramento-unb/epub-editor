"use client";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";

export default function Settings() {
  const [s, setS] = useState<any>(null);
  const [key, setKey] = useState("");
  const [models, setModels] = useState<Record<string, string[]>>({});
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  useEffect(() => { api("/api/ai/settings").then(setS).catch((e) => setErr(e.message)); }, []);
  if (!s) return <main className="page">{err ? <p className="error">{err}</p> : <p>Carregando…</p>}</main>;

  async function save(patch: any) {
    setErr(null); setMsg(null);
    try { setS(await api("/api/ai/settings", { method: "PUT", json: patch })); setMsg("Configurações salvas."); }
    catch (e: any) { setErr(e.message); }
  }
  async function list(provider: string) {
    setBusy(provider); setErr(null);
    try { const r = await api<{ models: string[] }>(`/api/ai/models?provider=${provider}`); setModels((m) => ({ ...m, [provider]: r.models })); }
    catch (e: any) { setErr(e.message); } finally { setBusy(null); }
  }
  async function detect(provider: string, model: string) {
    if (!model) return;
    setBusy(`${provider}:${model}`); setErr(null);
    try {
      await api("/api/ai/detect", { method: "POST", json: { provider, model } });
      setS(await api("/api/ai/settings"));
    } catch (e: any) { setErr(e.message); } finally { setBusy(null); }
  }
  const caps = (prov: string, model: string) => s.capabilities?.[`${prov}:${model}`];
  const Caps = ({ prov, model }: { prov: string; model: string }) => {
    const c = caps(prov, model);
    if (!model) return null;
    return (
      <span className="row" style={{ fontSize: 12 }}>
        {c ? <>
          <span className={`badge ${c.text ? "ok" : "err"}`}>texto {c.text ? "✓" : "✗"}</span>
          <span className={`badge ${c.vision ? "ok" : "err"}`}>visão {c.vision ? "✓" : "✗"}</span>
          <span className={`badge ${c.structured ? "ok" : "warn"}`}>JSON {c.structured ? "✓" : "?"}</span>
          {c.context_tokens && <span className="badge">contexto {c.context_tokens.toLocaleString("pt-BR")}</span>}
          <span className="muted" title={(c.source || []).join("\n")}>evidências</span>
        </> : <span className="muted">capacidades não detectadas</span>}
        <button className="small" disabled={busy === `${prov}:${model}`} onClick={() => detect(prov, model)}>
          {busy === `${prov}:${model}` ? "Testando…" : "Detectar capacidades"}</button>
      </span>
    );
  };
  const lim = s.limits;

  return (
    <main className="page">
      <h1>Configurações de IA</h1>
      <p className="muted">A conversão, a edição manual, a prévia e a exportação funcionam sem IA. Os fluxos de IA são opcionais.</p>
      {msg && <p role="status">{msg}</p>}
      {err && <p className="error" role="alert">{err}</p>}
      <section className="card">
        <h2>Modo</h2>
        {[["none", "Sem IA"], ["local", "IA local (Ollama) — nunca recorre à nuvem"], ["gemini", "Gemini (nuvem) — dados selecionados saem desta máquina"]].map(([v, l]) => (
          <label key={v} className="check"><input type="radio" name="mode" checked={s.mode === v} onChange={() => save({ mode: v })} /> {l}</label>
        ))}
      </section>
      <section className="card">
        <h2>Gemini</h2>
        <p>Chave: {s.gemini.has_key ? <span className="badge ok">configurada ({s.gemini.key_source})</span> : <span className="badge warn">não configurada</span>}
          {" "}<span className="muted">A chave fica apenas no servidor (cifrada com APP_SECRET_KEY) e nunca é exibida.</span></p>
        {!s.can_store_secrets && <p className="error">APP_SECRET_KEY não definida: não é possível salvar a chave pelo navegador (use GEMINI_API_KEY no ambiente).</p>}
        <div className="row">
          <input type="password" autoComplete="off" placeholder="Nova chave da API" value={key} onChange={(e) => setKey(e.target.value)} aria-label="Chave da API Gemini" />
          <button onClick={() => { save({ gemini_key: key }); setKey(""); }} disabled={!key}>Salvar chave</button>
          {s.gemini.key_source === "banco (cifrada)" && <button className="danger" onClick={() => save({ gemini_key: "" })}>Remover chave</button>}
        </div>
        <ModelRow label="Modelo textual" value={s.gemini.text_model} options={models.gemini} onSave={(v) => save({ gemini_text_model: v })} />
        <Caps prov="gemini" model={s.gemini.text_model} />
        <ModelRow label="Modelo multimodal (visão)" value={s.gemini.vision_model} options={models.gemini} onSave={(v) => save({ gemini_vision_model: v })} />
        <Caps prov="gemini" model={s.gemini.vision_model} />
        <button onClick={() => list("gemini")} disabled={busy === "gemini" || !s.gemini.has_key}>Listar modelos disponíveis</button>
      </section>
      <section className="card">
        <h2>Ollama (local)</h2>
        <label className="field">Endereço
          <span className="row"><input type="url" defaultValue={s.ollama.url} id="ollama-url" style={{ width: 320 }} />
            <button onClick={() => save({ ollama_url: (document.getElementById("ollama-url") as HTMLInputElement).value })}>Salvar</button></span>
        </label>
        <p className="muted" style={{ fontSize: 12 }}>Hosts permitidos (definidos pelo administrador em OLLAMA_ALLOWED_HOSTS): {s.ollama.allowed_hosts.join(", ")}.
          No Docker use http://host.docker.internal:11434 para o Ollama do host.</p>
        <ModelRow label="Modelo textual" value={s.ollama.text_model} options={models.ollama} onSave={(v) => save({ ollama_text_model: v })} />
        <Caps prov="ollama" model={s.ollama.text_model} />
        <ModelRow label="Modelo multimodal (visão)" value={s.ollama.vision_model} options={models.ollama} onSave={(v) => save({ ollama_vision_model: v })} />
        <Caps prov="ollama" model={s.ollama.vision_model} />
        <button onClick={() => list("ollama")} disabled={busy === "ollama"}>Listar modelos instalados</button>
        <p className="muted" style={{ fontSize: 12 }}>Listar um modelo não prova que ele aceita imagens: use “Detectar capacidades” (faz um teste mínimo com uma imagem).</p>
      </section>
      <section className="card">
        <h2>Limites por execução</h2>
        <form className="row" onSubmit={(e) => { e.preventDefault(); const f = new FormData(e.currentTarget);
          const out: any = {}; f.forEach((v, k) => { out[k] = v === "" ? null : Number(v); }); save({ limits: out }); }}>
          {[["max_pages", "Páginas"], ["render_dpi", "Resolução (dpi)"], ["max_requests", "Requisições"], ["max_output_tokens", "Tokens de saída"],
            ["max_input_chars", "Caracteres por unidade"], ["timeout_s", "Timeout (s)"], ["budget_usd", "Orçamento (US$)"]].map(([k, l]) => (
            <label key={k} className="field">{l}<input type="number" step="any" name={k} defaultValue={lim[k] ?? ""} style={{ width: 120 }} /></label>
          ))}
          <button type="submit">Salvar limites</button>
        </form>
        <p className="muted" style={{ fontSize: 12 }}>Concorrência: 1 chamada por vez (fixo nesta versão).</p>
      </section>
      <section className="card">
        <h2>Preços (para estimativas)</h2>
        <p className="muted">Sem preço configurado não há estimativa de custo. Informe valores da tabela oficial do provedor, por milhão de tokens.</p>
        <PriceForm prices={s.prices} models={[s.gemini.text_model, s.gemini.vision_model].filter(Boolean)} onSave={(p) => save({ prices: p })} />
      </section>
    </main>
  );
}

function ModelRow({ label, value, options, onSave }: { label: string; value: string; options?: string[]; onSave: (v: string) => void }) {
  const [v, setV] = useState(value || "");
  useEffect(() => setV(value || ""), [value]);
  const listId = `ml-${label.replace(/\W/g, "")}`;
  return (
    <label className="field">{label}
      <span className="row">
        <input type="text" list={listId} value={v} onChange={(e) => setV(e.target.value)} style={{ width: 320 }} />
        <datalist id={listId}>{(options || []).map((o) => <option key={o} value={o} />)}</datalist>
        <button onClick={() => onSave(v)} disabled={v === value}>Salvar</button>
      </span>
    </label>
  );
}

function PriceForm({ prices, models, onSave }: { prices: Record<string, any>; models: string[]; onSave: (p: any) => void }) {
  const [p, setP] = useState<Record<string, any>>(prices || {});
  const all = [...new Set([...models, ...Object.keys(p)])];
  return (
    <div>
      {all.map((m) => (
        <div key={m} className="row">
          <code style={{ width: 220 }}>{m}</code>
          <label className="row">entrada US$/Mtok <input type="number" step="any" value={p[m]?.input_per_mtok ?? ""} style={{ width: 90 }}
            onChange={(e) => setP({ ...p, [m]: { ...p[m], input_per_mtok: e.target.value } })} /></label>
          <label className="row">saída US$/Mtok <input type="number" step="any" value={p[m]?.output_per_mtok ?? ""} style={{ width: 90 }}
            onChange={(e) => setP({ ...p, [m]: { ...p[m], output_per_mtok: e.target.value } })} /></label>
        </div>
      ))}
      <button onClick={() => onSave(Object.fromEntries(Object.entries(p).filter(([, v]) => v?.input_per_mtok !== undefined && v?.input_per_mtok !== "")
        .map(([k, v]) => [k, { input_per_mtok: Number(v.input_per_mtok), output_per_mtok: Number(v.output_per_mtok || 0) }])))}>Salvar preços</button>
    </div>
  );
}
