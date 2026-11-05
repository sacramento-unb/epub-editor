"""Modo de IA para reformatação editorial (independente da composição do PDF).

Lê a estrutura do EPUB exportado (XHTML, CSS, sumário, metadados), reconcilia
com o modelo intermediário pelos IDs/âncoras e processa o livro em unidades que
cabem na janela de contexto, com inventário global, guia editorial persistido e
contexto entre unidades. A cobertura é controlada por hash/IDs por capítulo:
nada é declarado revisado sem ter sido processado com sucesso.

Por padrão é formatação, não reescrita: operações de texto só existem com a
opção de correção de OCR (desativada por padrão) e sempre exigem aprovação.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter

from lxml import etree

from ..db import AIProposal, Project, Revision, SessionLocal, audit
from ..engine.epub import XHTML_NS, safe_read_epub
from ..engine.integrity import norm
from ..engine.model import Document, element_text_for_integrity
from ..engine.ops import OperationList, OpError, apply_ops, check_ai_op
from ..jobs import JobContext, JobFailed, handler
from ..services import commit_revision, export_revision, unpack
from ..storage import get_storage
from .capture import spine_files
from .providers import ProviderError
from .runtime import (DEFAULT_LIMITS, SYSTEM_GUARD, AIUnavailable, BudgetExceeded, CallBudget, call_structured,
                      load_settings, select)
from .schemas import (ConversionError, EditorialGuide, EditorialResponse, editorial_schema, guide_schema,
                      to_engine_op)

PROFILES = {
    "tecnico": "Livro técnico: títulos claros e consistentes, listas e tabelas legíveis, figuras com legenda.",
    "academico": "Livro acadêmico: hierarquia rigorosa, notas e referências organizadas, citações em bloco.",
    "narrativa": "Narrativa: parágrafos com recuo, capítulos com quebra, poucos estilos, leitura contínua.",
    "tela_pequena": "Leitura em tela pequena: margens pequenas, entrelinha confortável, imagens até 100%.",
}
CSS_OPS = {"set_style", "set_global_style"}


def read_epub_structure(path, mapping: dict) -> dict:
    """Extrai a estrutura do EPUB e reconcilia com o modelo (por âncora e-<id>)."""
    files = safe_read_epub(path)
    _, spine = spine_files(files)
    chapters = []
    for name in spine:
        root = etree.fromstring(files[name], parser=etree.XMLParser(resolve_entities=False, no_network=True))
        ids, tags = [], []
        for el in root.iter():
            if not isinstance(el.tag, str):
                continue
            i = el.get("id") or ""
            if i.startswith("e-"):
                ids.append(i[2:])
                tags.append(etree.QName(el).localname)
        title = root.find(f".//{{{XHTML_NS}}}title")
        chapters.append({"file": name, "title": title.text if title is not None else "", "ids": ids,
                         "tags": Counter(tags)})
    css = {n: d.decode("utf-8", "replace") for n, d in files.items() if n.endswith(".css")}
    nav = next((d for n, d in files.items() if n.endswith("nav.xhtml")), b"")
    unknown = [i for ch in chapters for i in ch["ids"] if i not in mapping]
    return {"chapters": chapters, "css_chars": sum(len(c) for c in css.values()), "nav_bytes": len(nav),
            "unreconciled_ids": unknown}


def inventory(doc: Document, structure: dict) -> dict:
    by = doc.by_id()
    types = Counter(e.type for e in doc.elements if e.included)
    levels = Counter(e.level for e in doc.elements if e.included and e.type == "heading")
    styles = Counter(f"{k}={v}" for e in doc.elements if e.included for k, v in e.output_style.items())
    chapters = []
    for i, ch in enumerate(structure["chapters"]):
        heads = [by[x] for x in ch["ids"] if x in by and by[x].type == "heading"]
        chapters.append({"n": i, "file": ch["file"], "title": ch["title"], "elements": len(ch["ids"]),
                         "headings": [(h.level, (h.text or "")[:80]) for h in heads[:12]]})
    return {"types": dict(types), "heading_levels": {str(k): v for k, v in levels.items()},
            "styles_in_use": dict(styles), "global_style": doc.global_style, "chapters": chapters,
            "metadata": {"title": doc.metadata.title, "language": doc.metadata.language}}


def _el_payload(e, full_text: bool) -> dict:
    t = e.text or ""
    excerpt = t if full_text or len(t) <= 400 else t[:300] + " […] " + t[-100:]
    return {"id": e.id, "tipo": e.type, "nivel": e.level, "bloqueado": e.locked, "quebra_capitulo": e.chapter_break,
            "texto": excerpt, "estilo": e.output_style, "legenda": e.caption_id, "alt": e.alt,
            "observado": {"pt": e.observed_style.size, "negrito": e.observed_style.bold}}


def make_units(doc: Document, structure: dict, chapters_sel: list[int] | None, max_chars: int,
               full_text: bool) -> list[dict]:
    by = doc.by_id()
    units = []
    for i, ch in enumerate(structure["chapters"]):
        if chapters_sel is not None and i not in chapters_sel:
            continue
        els = [by[x] for x in ch["ids"] if x in by and by[x].included]
        chunk, size = [], 0
        part = 0
        for e in els:
            p = _el_payload(e, full_text)
            n = len(json.dumps(p, ensure_ascii=False))
            if chunk and size + n > max_chars:
                units.append({"chapter": i, "part": part, "ids": [x["id"] for x in chunk], "payload": chunk})
                part += 1
                chunk, size = [], 0
            chunk.append(p)
            size += n
        if chunk:
            units.append({"chapter": i, "part": part, "ids": [x["id"] for x in chunk], "payload": chunk})
    return units


def chapter_hash(doc: Document, ids: list[str]) -> str:
    by = doc.by_id()
    h = hashlib.sha256()
    for i in ids:
        e = by.get(i)
        if e:
            h.update(f"{e.id}|{e.type}|{e.level}|{e.text or ''}|{sorted(e.output_style.items())}".encode())
    return h.hexdigest()[:16]


def text_preserved(before: Document, after: Document, allowed_text_ids: set[str]) -> list[str]:
    """IDs cujo texto mudou sem autorização (dividir/unir é tolerado se o texto total é preservado)."""
    a = {e.id: norm(element_text_for_integrity(e)) for e in before.elements if e.included}
    b = {e.id: norm(element_text_for_integrity(e)) for e in after.elements if e.included}
    total_a = " ".join(v for k, v in a.items() if k not in allowed_text_ids).replace(" ", "")
    total_b = " ".join(v for k, v in b.items() if k not in allowed_text_ids).replace(" ", "")
    if total_a == total_b:
        return []
    return [k for k in a if k not in allowed_text_ids and a[k] != b.get(k)] or ["(texto total diverge)"]


@handler("editorial")
def run_editorial(ctx: JobContext) -> dict:
    p = ctx.params
    profile = p.get("profile", "tecnico")
    instructions = (p.get("instructions") or "")[:2000]
    apply_mode = p.get("apply_mode", "selected")  # css|css_structure|selected
    ocr_fix = bool(p.get("ocr_correction", False))
    chapters_sel = p.get("chapters")
    with SessionLocal() as db:
        project = db.get(Project, ctx.project_id)
        s = load_settings(db)
        try:
            sel = select(s, "text")
        except AIUnavailable as exc:
            raise JobFailed(str(exc)) from None
        limits = {**DEFAULT_LIMITS, **(s.limits or {}), **(p.get("limits") or {})}
        prices = dict(s.prices or {})
        base_id = ctx.result("snapshot") or project.head_revision_id
        ctx.run_step("snapshot", lambda: base_id, "Snapshot da versão base")
        base_rev = db.get(Revision, base_id)
        doc = unpack(base_rev.doc)
        if ctx.done("export"):
            exp_id = ctx.result("export")
        else:
            exp = export_revision(db, project, base_rev, "fluid", purpose="candidate")
            db.commit()
            exp_id = exp.id
            ctx.run_step("export", lambda: exp_id, "Exportando EPUB para leitura")
        from ..db import Export

        exp = db.get(Export, exp_id)
        epub_path = get_storage().local_path(exp.storage_key)
        mapping = (exp.build or {}).get("mapping", {})
        base_ec_errors = (exp.epubcheck or {}).get("errors") or 0

    structure = read_epub_structure(epub_path, mapping)
    inv = inventory(doc, structure)
    max_chars = int(limits.get("max_input_chars") or 24000)
    caps_ctx = (sel.caps or {}).get("context_tokens")
    if caps_ctx:
        max_chars = min(max_chars, int(caps_ctx * 4 * 0.4))
    units = make_units(doc, structure, chapters_sel, max_chars, ocr_fix)
    budget = CallBudget(limits=limits, prices=prices)
    system = (f"{SYSTEM_GUARD}\nVocê é um editor de livros digitais. Objetivo: melhorar a experiência de leitura no "
              f"EPUB sem buscar reprodução visual do PDF. Perfil: {PROFILES.get(profile, profile)} "
              "Formatação, não reescrita: preserve integralmente conteúdo, idioma, significado, sequência, valores "
              "e referências. Não resuma, traduza, complete lacunas nem acrescente fatos.")

    # 1) Guia editorial global (persistido)
    if ctx.done("guide"):
        guide = EditorialGuide.model_validate(ctx.result("guide"))
    else:
        def mk_guide():
            g = call_structured(sel, budget, purpose="editorial-guide", system=system,
                                text=json.dumps({"inventario": inv, "instrucoes_usuario": instructions},
                                                ensure_ascii=False),
                                images=[], schema=guide_schema(), response_model=EditorialGuide,
                                project_id=ctx.project_id, job_id=ctx.job_id,
                                summary={"chapters": len(inv["chapters"])})
            return g.model_dump()
        try:
            guide = EditorialGuide.model_validate(ctx.run_step("guide", mk_guide, "Guia editorial"))
        except (ProviderError, BudgetExceeded) as exc:
            raise JobFailed(f"Falha ao gerar o guia editorial: {exc}") from None

    # 2) Unidades
    records, summaries, unit_status = [], [], {}
    global_ops = []
    for kv in guide.global_props:
        global_ops.append({"op": "set_global_style", "target_ids": [], "global_props": [kv.model_dump()],
                           "reason": "guia editorial"})
    for k, u in enumerate(units):
        key = f"unit-{u['chapter']}-{u['part']}"
        ctx.check_cancel()
        if ctx.done(key):
            res = ctx.result(key)
        else:
            prev = summaries[-1] if summaries else ""
            text = json.dumps({"guia": guide.model_dump(), "capitulo": u["chapter"], "parte": u["part"],
                               "contexto_anterior": prev, "elementos": u["payload"],
                               "correcao_ocr_permitida": ocr_fix}, ensure_ascii=False)
            try:
                resp = call_structured(sel, budget, purpose="editorial-unit", system=system, text=text, images=[],
                                       schema=editorial_schema(ocr_fix), response_model=EditorialResponse,
                                       project_id=ctx.project_id, job_id=ctx.job_id,
                                       summary={"chapter": u["chapter"], "part": u["part"], "elements": len(u["ids"])})
                res = {"ok": True, "summary": resp.summary, "ops": [o.model_dump(exclude_none=True) for o in resp.operations]}
            except BudgetExceeded as exc:
                res = {"ok": False, "error": str(exc)}
            except ProviderError as exc:
                res = {"ok": False, "error": f"{exc.kind}: {exc}"}
            ctx.run_step(key, lambda r=res: r, f"Capítulo {u['chapter'] + 1}, parte {u['part'] + 1}")
        unit_status[key] = res.get("ok", False)
        if res.get("ok"):
            summaries.append(res.get("summary", "")[:600])
            for o in res["ops"]:
                records.append({"unit": key, "scope": u["ids"], "ai": o})
        ctx.progress((k + 1) / max(1, len(units)) * 0.85, f"Unidade {k + 1}/{len(units)} processada")

    # 3) Validação das operações
    from .schemas import AIOperation

    allowed_kinds = CSS_OPS if apply_mode == "css" else None
    valid, op_records = [], []
    for rec in [{"unit": "guia", "scope": None, "ai": o} for o in global_ops] + records:
        r = {"unit": rec["unit"], "ai": rec["ai"], "status": "invalid", "error": None}
        try:
            d = to_engine_op(AIOperation.model_validate(rec["ai"]))
            if allowed_kinds and d["op"] not in allowed_kinds:
                raise OpError("fora do modo 'somente CSS'")
            scope = set(rec["scope"]) if rec["scope"] and d["op"] != "set_global_style" else None
            check_ai_op(doc, OperationList.validate_python([d])[0], allow_text=ocr_fix, scope_ids=scope)
            r.update(op=d, status="valid")
            valid.append(d)
        except (ConversionError, OpError, ValueError) as exc:
            r["error"] = str(exc)
        op_records.append(r)

    # 4) Candidata: aplica, verifica preservação e EPUBCheck
    cand, applied = doc, []
    for r in op_records:
        if r["status"] != "valid":
            continue
        try:
            nxt = apply_ops(cand, [r["op"]], actor="ai", allow_text=ocr_fix)
            if nxt.content_hash() == cand.content_hash():
                r["status"] = "no_effect"  # valor já em vigor: não vira proposta
                continue
            cand = nxt
            r["status"] = "pending"
            applied.append(r["op"])
        except OpError as exc:
            r.update(status="invalid", error=str(exc))
    text_ids = {o["id"] for o in applied if o["op"] == "set_text"}
    changed = text_preserved(doc, cand, text_ids)
    coverage = []
    for i, ch in enumerate(structure["chapters"]):
        if chapters_sel is not None and i not in chapters_sel:
            continue
        keys = [f"unit-{u['chapter']}-{u['part']}" for u in units if u["chapter"] == i]
        done = sum(1 for k in keys if unit_status.get(k))
        coverage.append({"chapter": i, "file": ch["file"], "title": ch["title"], "elements": len(ch["ids"]),
                         "hash": chapter_hash(doc, ch["ids"]), "units": len(keys), "units_done": done,
                         "status": "revisado" if keys and done == len(keys) else
                         ("parcial" if done else ("vazio" if not keys else "não revisado"))})
    metrics = {"coverage": coverage, "unreconciled_ids": structure["unreconciled_ids"][:50],
               "text_changed_without_permission": changed, "usage": {
                   "requests": budget.requests, "input_tokens": budget.input_tokens,
                   "output_tokens": budget.output_tokens,
                   "cost_estimate": round(budget.cost, 4) if budget.cost else None}}
    status = "pending"
    note = ""
    with SessionLocal() as db:
        project = db.get(Project, ctx.project_id)
        if changed:
            status, note = "rejected", "Rejeitada: o texto mudaria sem autorização."
        cand_rev = None
        if applied and status == "pending":
            cand_rev = commit_revision(db, project, cand, kind="ai_editorial", ops=applied, actor="ai",
                                       label="Candidata editorial", parent_id=base_id, set_head=False)
            cexp = export_revision(db, project, cand_rev, "fluid", purpose="candidate")
            metrics["candidate_epubcheck_errors"] = (cexp.epubcheck or {}).get("errors")
            metrics["candidate_integrity_ok"] = (cexp.integrity or {}).get("ok")
            if (metrics["candidate_epubcheck_errors"] or 0) > base_ec_errors:
                status, note = "rejected", "Rejeitada: EPUBCheck piorou na versão candidata."
        prop = AIProposal(project_id=project.id, job_id=ctx.job_id, kind="editorial",
                          unit=",".join(str(c["chapter"]) for c in coverage), iteration=0,
                          base_revision_id=base_id, result_revision_id=cand_rev.id if cand_rev else None,
                          diagnosis=(note + " " + " | ".join(summaries)).strip()[:4000],
                          ops=op_records, metrics={**metrics, "guide": guide.model_dump(), "profile": profile,
                                                   "apply_mode": apply_mode}, status=status)
        db.add(prop)
        db.flush()
        auto = apply_mode in ("css", "css_structure") and not ocr_fix
        result = {"proposal_id": prop.id, "coverage": coverage, "ops_valid": len(applied),
                  "ops_invalid": sum(1 for r in op_records if r["status"] == "invalid"), "status": status,
                  "usage": metrics["usage"]}
        if auto and status == "pending" and cand_rev is not None:
            if project.head_revision_id == base_id:
                newrev = commit_revision(db, project, cand, kind="ai_editorial", ops=applied, actor="ai",
                                         label=f"Reformatação editorial ({profile})")
                prop.status = "applied"
                prop.ops = [{**r, "status": "applied"} if r["status"] == "pending" else r for r in op_records]
                result["applied_revision_id"] = newrev.id
            else:
                prop.diagnosis = "Documento mudou durante a execução; aprovação manual necessária. " + prop.diagnosis
        audit(db, project.id, "ai_editorial", status=prop.status, ops=len(applied))
        db.commit()
    return result
