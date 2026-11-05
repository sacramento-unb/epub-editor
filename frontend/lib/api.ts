// Cliente da API (mesma origem via rewrite do Next). Nenhuma credencial de IA passa por aqui.
import type { BBox } from "./geometry";

export type ElementType =
  | "heading" | "paragraph" | "list_item" | "blockquote" | "image" | "figure" | "caption" | "table"
  | "formula" | "footnote" | "header" | "footer" | "page_number" | "toc" | "cover" | "decoration"
  | "watermark" | "region";

export interface Source { page: number; bbox: BBox; coord: string }
export interface Mark { start: number; end: number; kind: string; href?: string | null }
export interface Cell { text: string; header: boolean; rowspan: number; colspan: number }

export interface Element {
  id: string;
  type: ElementType;
  subtype?: string | null;
  text_original?: string | null;
  text?: string | null;
  marks: Mark[];
  sources: Source[];
  level?: number | null;
  chapter_break: boolean;
  parent_id?: string | null;
  depends_on: string[];
  included: boolean;
  excluded_explicit: boolean;
  locked: boolean;
  observed_style: { font?: string | null; size?: number | null; bold: boolean; italic: boolean; color?: string | null };
  output_style: Record<string, string>;
  asset?: string | null;
  caption_id?: string | null;
  alt?: string | null;
  note_id?: string | null;
  table?: Cell[][] | null;
  extraction: { method: string; confidence?: number | null; evidence: string[] };
  review: string[];
  provenance: string[];
}

export interface PageInfo {
  index: number; width: number; height: number; rotation: number; kind: string;
  ocr_confidence?: number | null; ocr_lang?: string | null;
}

export interface DocumentModel {
  schema_version: number;
  metadata: { title: string; authors: string[]; language: string };
  pages: PageInfo[];
  elements: Element[];
  global_style: Record<string, string | number>;
  outline: { title: string; level: number; page: number | null }[];
  doc_kind: string;
  warnings: string[];
}

export interface RevisionInfo {
  id: number; seq: number; kind: string; label?: string | null; doc_hash: string; can_undo: boolean; can_redo: boolean;
}

export interface DocPayload { revision: RevisionInfo; document: DocumentModel }

export interface Job {
  id: string; project_id: string; kind: string; status: string; params: Record<string, unknown>;
  steps: { name: string; status: string; message?: string | null; error?: string | null }[];
  progress: number; message?: string | null; error?: string | null; result: Record<string, any>;
  cancel_requested: boolean; attempts: number; created_at: string; finished_at?: string | null;
}

export interface Project {
  id: string; name: string; original_filename: string; status: string; error?: string | null;
  page_count: number; pdf_size: number; encrypted: boolean; settings: Record<string, any>;
  head_revision?: { id: number; seq: number; kind: string; doc_hash: string } | null;
  created_at: string; updated_at: string; jobs?: Job[];
}

export interface ExportInfo {
  id: string; mode: string; revision_id: number; size: number; sha256: string; created_at: string; stale: boolean;
  epubcheck: { available?: boolean; status?: string; version?: string; errors?: number; warnings?: number; detail?: string };
  epubcheck_messages: { severity: string; id: string; message: string; path?: string; line?: number }[];
  integrity: { ok?: boolean; missing?: any[]; partial?: any[]; lost_numbers?: string[]; unexpected_words?: string[];
    duplicated?: string[]; checked_elements?: number; limitations?: string; error?: string };
  warnings: string[]; spine: string[]; mapping?: Record<string, { file: string; anchor: string }>;
}

export interface Proposal {
  id: string; kind: string; unit: string; iteration: number; status: string; base_revision_id: number;
  result_revision_id?: number | null; diagnosis: string; metrics: Record<string, any>; job_id?: string | null;
  ops: { op?: any; ai?: any; status: string; error?: string | null; unit?: any; old_text?: string }[];
  created_at: string;
}

export class ApiError extends Error {
  constructor(public status: number, public detail: any) {
    super(typeof detail === "string" ? detail : detail?.message || JSON.stringify(detail));
  }
}

export async function api<T = any>(path: string, init: RequestInit & { json?: unknown } = {}): Promise<T> {
  const { json, ...rest } = init;
  const headers: Record<string, string> = { ...(rest.headers as Record<string, string>) };
  if (json !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(path, { ...rest, headers, body: json !== undefined ? JSON.stringify(json) : rest.body });
  if (!res.ok) {
    let detail: any = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch { /* corpo não JSON */ }
    throw new ApiError(res.status, detail);
  }
  return res.status === 204 ? (undefined as T) : res.json();
}

export const TYPE_LABEL: Record<string, string> = {
  heading: "Título", paragraph: "Parágrafo", list_item: "Item de lista", blockquote: "Citação", image: "Imagem",
  figure: "Figura", caption: "Legenda", table: "Tabela", formula: "Fórmula", footnote: "Nota", header: "Cabeçalho",
  footer: "Rodapé", page_number: "Nº de página", toc: "Sumário (PDF)", cover: "Capa", decoration: "Decorativo",
  watermark: "Marca d’água", region: "Recorte",
};

export const JOB_LABEL: Record<string, string> = {
  extract: "Análise do PDF", export: "Geração do EPUB", validate: "Validação", visual: "Comparação visual (IA)",
  editorial: "Reformatação editorial (IA)",
};

export const STATUS_LABEL: Record<string, string> = {
  queued: "na fila", running: "em execução", succeeded: "concluída", failed: "falhou", cancelled: "cancelada",
};

export function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 ** 2).toFixed(1)} MB`;
}

// Capítulos como no EPUB fluido: com includedOnly, replica a divisão da exportação (usada pela IA).
export function chaptersOf(doc: DocumentModel, includedOnly = false): { index: number; title: string; ids: string[] }[] {
  const out: { index: number; title: string; ids: string[] }[] = [];
  let cur: { index: number; title: string; ids: string[] } | null = null;
  for (const e of doc.elements) {
    if (includedOnly && (!e.included || e.type === "cover")) continue;
    if (!cur || (e.chapter_break && cur.ids.length)) {
      cur = { index: out.length, title: e.type === "heading" ? (e.text || "") : `Trecho ${out.length + 1}`, ids: [] };
      out.push(cur);
    }
    if (e.chapter_break && e.type === "heading") cur.title = e.text || cur.title;
    cur.ids.push(e.id);
  }
  return out;
}
