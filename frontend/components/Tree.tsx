"use client";
import { useEffect, useMemo, useRef, useState } from "react";
import { chaptersOf, TYPE_LABEL, type DocumentModel, type Element } from "@/lib/api";

type GroupBy = "chapter" | "page" | "type";

interface Props {
  projectId: string;
  doc: DocumentModel;
  current: string | null;
  selected: Set<string>;
  onSelect: (id: string, mode: "single" | "toggle" | "range") => void;
  onSetIncluded: (ids: string[], included: boolean, mode: "item" | "batch") => void;
  onMove: (ids: string[], beforeId: string) => void;
}

export function Tree({ projectId, doc, current, selected, onSelect, onSetIncluded, onMove }: Props) {
  const [groupBy, setGroupBy] = useState<GroupBy>("chapter");
  const [q, setQ] = useState("");
  const [typeFilter, setTypeFilter] = useState<string>("");
  const [stateFilter, setStateFilter] = useState<string>("");
  const [dropOver, setDropOver] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const listRef = useRef<HTMLDivElement>(null);

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return doc.elements.filter((e) => {
      if (typeFilter && e.type !== typeFilter) return false;
      if (stateFilter === "included" && !e.included) return false;
      if (stateFilter === "excluded" && e.included) return false;
      if (stateFilter === "review" && !e.review.length) return false;
      if (stateFilter === "locked" && !e.locked) return false;
      if (needle && !(e.text || e.alt || "").toLowerCase().includes(needle) && !e.id.includes(needle)) return false;
      return true;
    });
  }, [doc, q, typeFilter, stateFilter]);

  const groups = useMemo(() => {
    const visible = new Set(filtered.map((e) => e.id));
    const out: { key: string; title: string; all: Element[]; shown: Element[]; chapterHead?: string }[] = [];
    const byId = new Map(doc.elements.map((e) => [e.id, e]));
    if (groupBy === "chapter") {
      for (const ch of chaptersOf(doc)) {
        const all = ch.ids.map((i) => byId.get(i)!);
        out.push({ key: `c${ch.index}`, title: ch.title || `Trecho ${ch.index + 1}`, all, shown: all.filter((e) => visible.has(e.id)),
          chapterHead: ch.ids[0] });
      }
    } else if (groupBy === "page") {
      const m = new Map<number, Element[]>();
      for (const e of doc.elements) {
        const p = e.sources[0]?.page ?? -1;
        if (!m.has(p)) m.set(p, []);
        m.get(p)!.push(e);
      }
      for (const [p, all] of [...m.entries()].sort((a, b) => a[0] - b[0]))
        out.push({ key: `p${p}`, title: p < 0 ? "Sem origem" : `Página ${p + 1}`, all, shown: all.filter((e) => visible.has(e.id)) });
    } else {
      const m = new Map<string, Element[]>();
      for (const e of doc.elements) {
        if (!m.has(e.type)) m.set(e.type, []);
        m.get(e.type)!.push(e);
      }
      for (const [t, all] of m) out.push({ key: `t${t}`, title: TYPE_LABEL[t] || t, all, shown: all.filter((e) => visible.has(e.id)) });
    }
    return out.filter((g) => g.shown.length);
  }, [doc, filtered, groupBy]);

  // navegação por teclado: ↑/↓ movem o elemento corrente; Espaço alterna inclusão
  useEffect(() => {
    if (!current || !listRef.current) return;
    listRef.current.querySelector(`[data-id="${current}"]`)?.scrollIntoView({ block: "nearest" });
  }, [current]);

  function onKey(ev: React.KeyboardEvent) {
    const ids = groups.flatMap((g) => (collapsed.has(g.key) ? [] : g.shown.map((e) => e.id)));
    const i = current ? ids.indexOf(current) : -1;
    if (ev.key === "ArrowDown" && i < ids.length - 1) { onSelect(ids[i + 1], ev.shiftKey ? "range" : "single"); ev.preventDefault(); }
    if (ev.key === "ArrowUp" && i > 0) { onSelect(ids[i - 1], ev.shiftKey ? "range" : "single"); ev.preventDefault(); }
    if (ev.key === " " && current) {
      const e = doc.elements.find((x) => x.id === current)!;
      onSetIncluded([current], !e.included, "item");
      ev.preventDefault();
    }
  }

  const types = useMemo(() => [...new Set(doc.elements.map((e) => e.type))].sort(), [doc]);

  return (
    <>
      <div style={{ padding: 8, display: "flex", flexDirection: "column", gap: 6, borderBottom: "1px solid var(--border)" }}>
        <input type="text" placeholder="Buscar texto ou ID…" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Buscar elementos" />
        <div className="row">
          <select value={groupBy} onChange={(e) => setGroupBy(e.target.value as GroupBy)} aria-label="Agrupar por">
            <option value="chapter">Por capítulo</option><option value="page">Por página</option><option value="type">Por tipo</option>
          </select>
          <select value={typeFilter} onChange={(e) => setTypeFilter(e.target.value)} aria-label="Filtrar tipo">
            <option value="">Todos os tipos</option>
            {types.map((t) => <option key={t} value={t}>{TYPE_LABEL[t] || t}</option>)}
          </select>
          <select value={stateFilter} onChange={(e) => setStateFilter(e.target.value)} aria-label="Filtrar estado">
            <option value="">Todos</option><option value="included">Incluídos</option><option value="excluded">Excluídos</option>
            <option value="review">Com pendência</option><option value="locked">Bloqueados</option>
          </select>
        </div>
        <div className="muted" style={{ fontSize: 12 }}>
          {filtered.length} de {doc.elements.length} elementos · {doc.elements.filter((e) => e.review.length).length} pendências ·
          Ctrl/Shift+clique seleciona vários
        </div>
      </div>
      <div className="tree" ref={listRef} tabIndex={0} onKeyDown={onKey} role="tree" aria-label="Elementos do documento">
        {groups.map((g) => {
          const inc = g.all.filter((e) => e.included).length;
          const state = inc === 0 ? "none" : inc === g.all.length ? "all" : "some";
          const isCol = collapsed.has(g.key);
          return (
            <div key={g.key} role="group">
              <div className="group"
                draggable={!!g.chapterHead}
                onDragStart={(ev) => g.chapterHead && ev.dataTransfer.setData("text/ids", JSON.stringify(g.all.map((e) => e.id)))}
                onDragOver={(ev) => { if (g.chapterHead) { ev.preventDefault(); setDropOver(g.key); } }}
                onDragLeave={() => setDropOver(null)}
                onDrop={(ev) => {
                  ev.preventDefault(); setDropOver(null);
                  const ids = JSON.parse(ev.dataTransfer.getData("text/ids") || "[]") as string[];
                  if (g.chapterHead && ids.length && !ids.includes(g.chapterHead)) onMove(ids, g.chapterHead);
                }}
                style={dropOver === g.key ? { boxShadow: "inset 0 2px 0 var(--accent)" } : undefined}>
                <button className="small" aria-label={isCol ? "Expandir grupo" : "Recolher grupo"} aria-expanded={!isCol}
                  onClick={() => setCollapsed((s) => { const n = new Set(s); if (n.has(g.key)) n.delete(g.key); else n.add(g.key); return n; })}>
                  {isCol ? "▸" : "▾"}
                </button>
                <TriCheckbox state={state} label={`Incluir grupo ${g.title}`}
                  onChange={(v) => onSetIncluded(g.all.map((e) => e.id), v, "batch")} />
                <span className="grow" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{g.title}</span>
                <span className="meta">{inc}/{g.all.length}</span>
              </div>
              {!isCol && g.shown.map((e) => (
                <div key={e.id} data-id={e.id} role="treeitem" aria-selected={selected.has(e.id)}
                  className={`item ${selected.has(e.id) ? "sel" : ""} ${current === e.id ? "cur" : ""} ${e.included ? "" : "excluded"} ${dropOver === e.id ? "dropover" : ""}`}
                  style={e.parent_id ? { paddingLeft: 30 } : undefined}
                  onClick={(ev) => onSelect(e.id, ev.shiftKey ? "range" : ev.ctrlKey || ev.metaKey ? "toggle" : "single")}
                  draggable
                  onDragStart={(ev) => {
                    const ids = selected.has(e.id) ? doc.elements.filter((x) => selected.has(x.id)).map((x) => x.id) : [e.id];
                    ev.dataTransfer.setData("text/ids", JSON.stringify(ids));
                  }}
                  onDragOver={(ev) => { ev.preventDefault(); setDropOver(e.id); }}
                  onDragLeave={() => setDropOver(null)}
                  onDrop={(ev) => {
                    ev.preventDefault(); setDropOver(null);
                    const ids = JSON.parse(ev.dataTransfer.getData("text/ids") || "[]") as string[];
                    if (ids.length && !ids.includes(e.id)) onMove(ids, e.id);
                  }}>
                  <input type="checkbox" checked={e.included} aria-label={`Incluir ${TYPE_LABEL[e.type]} ${e.id}`}
                    onClick={(ev) => ev.stopPropagation()}
                    onChange={(ev) => onSetIncluded([e.id], ev.target.checked, "item")} />
                  {e.asset ? <img className="thumb" alt="" src={`/api/projects/${projectId}/assets/${e.asset}`} loading="lazy" /> : null}
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div className="row" style={{ gap: 4 }}>
                      <span className="badge">{TYPE_LABEL[e.type] || e.type}{e.type === "heading" && e.level ? ` ${e.level}` : ""}</span>
                      {e.locked && <span className="badge" title="Bloqueado contra alterações de IA">🔒</span>}
                      {e.review.length > 0 && <span className="badge warn" title={e.review.join("\n")}>⚠ {e.review.length}</span>}
                      {e.extraction.method === "ocr" && <span className="badge" title="Texto obtido por OCR">OCR</span>}
                      <span className="meta">p. {e.sources.map((s) => s.page + 1).filter((v, i, a) => a.indexOf(v) === i).join(",") || "—"}</span>
                    </div>
                    <div className="txt">{e.text || e.alt || (e.table ? `Tabela ${e.table.length}×${e.table[0]?.length || 0}` : "")}</div>
                  </div>
                </div>
              ))}
            </div>
          );
        })}
      </div>
    </>
  );
}

function TriCheckbox({ state, label, onChange }: { state: "all" | "some" | "none"; label: string; onChange: (v: boolean) => void }) {
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => { if (ref.current) ref.current.indeterminate = state === "some"; }, [state]);
  return <input ref={ref} type="checkbox" checked={state === "all"} aria-label={label}
    aria-checked={state === "some" ? "mixed" : state === "all"} onChange={() => onChange(state !== "all")} />;
}
