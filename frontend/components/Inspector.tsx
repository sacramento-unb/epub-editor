"use client";
import { useEffect, useRef, useState } from "react";
import { TYPE_LABEL, type DocumentModel, type Element } from "@/lib/api";

const TYPES = ["heading", "paragraph", "list_item", "blockquote", "caption", "footnote", "image", "figure", "formula",
  "table", "header", "footer", "page_number", "toc", "decoration", "watermark", "cover"];

const STYLE_LABEL: Record<string, string> = {
  align: "Alinhamento", emphasis: "Ênfase", size: "Tamanho", spacing_before: "Espaço antes", spacing_after: "Espaço depois",
  indent: "Recuo", width: "Largura (img %)", list_style: "Marcador", page_break_before: "Quebra antes",
};

interface Props {
  doc: DocumentModel;
  element: Element | null;
  selectedCount: number;
  styleTokens: Record<string, string[]>;
  onOps: (ops: any[], label?: string) => Promise<void>;
  onGroupSelected: () => void;
}

export function Inspector({ doc, element: e, selectedCount, styleTokens, onOps, onGroupSelected }: Props) {
  const [text, setText] = useState("");
  const [alt, setAlt] = useState("");
  const ta = useRef<HTMLTextAreaElement>(null);
  useEffect(() => { setText(e?.text || ""); setAlt(e?.alt || ""); }, [e?.id, e?.text, e?.alt]);

  if (!e) return <div className="inspector muted">Selecione um elemento na árvore, no PDF ou na prévia do EPUB.</div>;
  const idx = doc.elements.findIndex((x) => x.id === e.id);
  const next = doc.elements[idx + 1];
  const captions = doc.elements.filter((x) => x.type === "caption" && x.sources[0]?.page === e.sources[0]?.page);
  const isVisual = ["image", "figure", "formula", "region", "cover", "table"].includes(e.type);

  return (
    <div className="inspector" aria-label="Propriedades do elemento">
      <h3>{TYPE_LABEL[e.type]} <span className="muted" style={{ fontWeight: 400 }}>· {e.id}</span></h3>
      {e.review.length > 0 && (
        <div className="card" style={{ padding: 8, borderColor: "var(--warn)" }}>
          <strong>Pendências</strong>
          <ul style={{ margin: "4px 0", paddingLeft: 18 }}>{e.review.map((r, i) => <li key={i}>{r}</li>)}</ul>
        </div>
      )}
      <div className="kv">
        <span>Exportar</span>
        <label className="check"><input type="checkbox" checked={e.included}
          onChange={(ev) => onOps([{ op: "set_included", ids: [e.id], included: ev.target.checked, mode: "item" }])} />
          {e.included ? "incluído" : e.excluded_explicit ? "excluído (explícito)" : "excluído"}</label>
        <span>Bloqueio IA</span>
        <label className="check"><input type="checkbox" checked={e.locked}
          onChange={(ev) => onOps([{ op: "set_locked", ids: [e.id], locked: ev.target.checked }])} />
          {e.locked ? "bloqueado" : "livre"}</label>
        <span>Tipo</span>
        <select value={e.type} onChange={(ev) => onOps([{ op: "set_type", id: e.id, type: ev.target.value }])} aria-label="Tipo semântico">
          {TYPES.map((t) => <option key={t} value={t}>{TYPE_LABEL[t]}</option>)}
        </select>
        {e.type === "heading" && (<>
          <span>Nível</span>
          <select value={e.level || 2} onChange={(ev) => onOps([{ op: "set_heading_level", id: e.id, level: +ev.target.value }])} aria-label="Nível do título">
            {[1, 2, 3, 4, 5, 6].map((l) => <option key={l} value={l}>H{l}</option>)}
          </select>
        </>)}
        <span>Capítulo</span>
        <label className="check"><input type="checkbox" checked={e.chapter_break}
          onChange={(ev) => onOps([{ op: "set_chapter_break", id: e.id, value: ev.target.checked }])} /> inicia novo capítulo</label>
        {isVisual && (<>
          <span>Legenda</span>
          <select value={e.caption_id || ""} aria-label="Legenda vinculada"
            onChange={(ev) => onOps([{ op: "link_caption", figure_id: e.id, caption_id: ev.target.value || null }])}>
            <option value="">(nenhuma)</option>
            {captions.map((c) => <option key={c.id} value={c.id}>{(c.text || "").slice(0, 60)}</option>)}
          </select>
        </>)}
      </div>

      {e.text !== null && e.text !== undefined && (
        <label className="field">Texto
          <textarea ref={ta} rows={5} value={text} onChange={(ev) => setText(ev.target.value)}
            onBlur={() => text !== e.text && onOps([{ op: "set_text", id: e.id, text }], "Edição de texto")} />
        </label>
      )}
      {e.text_original && e.text_original !== e.text && (
        <details><summary className="muted">Texto original extraído</summary><p style={{ whiteSpace: "pre-wrap" }}>{e.text_original}</p></details>
      )}
      {isVisual && (
        <label className="field">Texto alternativo
          <textarea rows={2} value={alt} onChange={(ev) => setAlt(ev.target.value)}
            onBlur={() => alt !== (e.alt || "") && onOps([{ op: "set_alt", id: e.id, alt }])} />
        </label>
      )}

      <details>
        <summary>Estilo editorial de saída</summary>
        <div className="kv" style={{ marginTop: 6 }}>
          {Object.entries(styleTokens).map(([tok, vals]) => (
            <FragmentRow key={tok} label={STYLE_LABEL[tok] || tok}>
              <select value={e.output_style[tok] || ""} aria-label={STYLE_LABEL[tok] || tok}
                onChange={(ev) => onOps([{ op: "set_style", ids: [e.id], style: { [tok]: ev.target.value } }])}>
                <option value="">(padrão)</option>
                {vals.map((v) => <option key={v} value={v}>{v}</option>)}
              </select>
            </FragmentRow>
          ))}
        </div>
      </details>

      <div className="row" style={{ marginTop: 8 }}>
        {e.text && <button className="small" title="Divide no cursor do campo de texto"
          onClick={() => {
            const pos = ta.current?.selectionStart ?? 0;
            if (pos > 0 && pos < (e.text || "").length) onOps([{ op: "split", id: e.id, offset: pos }], "Dividir bloco");
            else alert("Posicione o cursor no texto onde o bloco deve ser dividido.");
          }}>Dividir no cursor</button>}
        {e.text && next?.text != null && <button className="small" onClick={() => onOps([{ op: "merge", ids: [e.id, next.id] }], "Unir blocos")}>Unir com o próximo</button>}
        {selectedCount >= 2 && <button className="small" onClick={onGroupSelected}>Agrupar seleção em figura</button>}
        {doc.elements.some((x) => x.parent_id === e.id) && <button className="small" onClick={() => onOps([{ op: "ungroup", id: e.id }])}>Desagrupar</button>}
        {e.caption_id && <button className="small" title="Inclui/exclui figura e legenda juntas"
          onClick={() => onOps([{ op: "set_included", ids: [e.id, e.caption_id!], included: !e.included, mode: "item" }])}>
          {e.included ? "Excluir" : "Incluir"} com a legenda</button>}
      </div>

      <details style={{ marginTop: 8 }}>
        <summary className="muted">Origem e evidências</summary>
        <ul style={{ paddingLeft: 18, fontSize: 12 }}>
          <li>Método: {e.extraction.method}{e.extraction.confidence != null ? ` · confiança heurística ${e.extraction.confidence.toFixed(2)} (não calibrada)` : ""}</li>
          {e.sources.map((s, i) => <li key={i}>p. {s.page + 1}: [{s.bbox.map((v) => v.toFixed(1)).join(", ")}] pt</li>)}
          {e.observed_style.font && <li>Fonte observada: {e.observed_style.font} {e.observed_style.size?.toFixed(1)}pt{e.observed_style.bold ? " negrito" : ""}{e.observed_style.italic ? " itálico" : ""}</li>}
          {e.extraction.evidence.map((v, i) => <li key={`ev${i}`}>{v}</li>)}
          {e.provenance.length > 0 && <li>Proveniência: {e.provenance.join(", ")}</li>}
        </ul>
      </details>
    </div>
  );
}

function FragmentRow({ label, children }: { label: string; children: React.ReactNode }) {
  return <><span>{label}</span>{children}</>;
}
