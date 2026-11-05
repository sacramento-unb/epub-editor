"""Modo de IA para fidelidade visual (PDF × EPUB exportado).

Pipeline por iteração (até ``max_iterations``, padrão 3):
 1. snapshot = revisão candidata imutável; 2. exporta o EPUB candidato e valida;
 3. captura a renderização real do arquivo exportado com leitor fixo;
 4. renderiza a referência do PDF com máscara sobre itens excluídos;
 5. envia imagens + estrutura + restrições; 6. valida operações tipadas;
 7. aplica numa nova candidata, exporta, valida e mede;
 8. aceita só se a pontuação melhora sem degradar integridade/EPUBCheck; senão reverte.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from ..db import AIProposal, Export, Project, RenderMap, Revision, SessionLocal, audit, utcnow
from ..engine import render
from ..engine.ops import OpError, apply_ops, check_ai_op
from ..engine.model import Document
from ..jobs import JobContext, JobFailed, handler
from ..services import commit_revision, export_revision, project_password, unpack
from ..storage import get_storage, project_key
from .capture import CaptureUnavailable, capture_epub, crop_epub_region, png_bytes, ssim
from .providers import ProviderError
from .runtime import (DEFAULT_LIMITS, SYSTEM_GUARD, AIUnavailable, BudgetExceeded, CallBudget, call_structured,
                      load_settings, select)
from .schemas import ConversionError, VisualResponse, to_engine_op, visual_schema

INTENSITY_MAX_OPS = {"leve": 3, "moderada": 8, "forte": 15}
MIN_IMPROVEMENT = 0.25


def _unit_elements(doc: Document, page: int) -> list:
    return [e for e in doc.elements if e.sources and e.sources[0].page == page and e.included]


def _excluded_boxes(doc: Document, page: int) -> list:
    out = []
    for e in doc.elements:
        if not e.included and e.subtype != "figure-label":
            out += [s.bbox for s in e.sources if s.page == page]
    return out


def _unit_payload(doc: Document, els: list, mode: str) -> str:
    items = []
    for e in els:
        items.append({
            "id": e.id, "tipo": e.type, "nivel": e.level, "bloqueado": e.locked,
            "trecho": (e.text or e.alt or "")[:160], "estilo_saida": e.output_style,
            "observado_pdf": {"tamanho_pt": e.observed_style.size, "negrito": e.observed_style.bold,
                              "italico": e.observed_style.italic},
            "legenda": e.caption_id,
        })
    return json.dumps({"modo_epub": mode, "estilo_global": doc.global_style, "elementos": items},
                      ensure_ascii=False)


def _prompt(mode: str, max_ops: int) -> str:
    focus = ("Modo FLUIDO: avalie hierarquia de títulos, sequência, destaque (negrito/itálico), proporção de imagens, "
             "alinhamento e legibilidade. NÃO avalie paginação, quebras de linha ou posição exata; o texto reflui."
             if mode == "fluid" else
             "Modo LAYOUT FIXO: compare posição, escala e composição da página em coordenadas normalizadas.")
    return (f"{SYSTEM_GUARD}\nVocê avalia a fidelidade visual de um trecho de livro convertido de PDF para EPUB. "
            "Imagem 1: referência do PDF; áreas em branco retangulares foram excluídas deliberadamente pelo usuário "
            "e NÃO devem ser reinseridas nem penalizadas. Imagem 2: renderização real do EPUB gerado. "
            f"{focus} Dê 'score' de 0 a 10 para a semelhança editorial atual e proponha no máximo {max_ops} "
            "operações da lista permitida, apenas sobre os IDs listados e não bloqueados. 'diagnosis' deve ser "
            "breve e objetivo (sem raciocínio interno). Se estiver bom, devolva operations vazia.")


@handler("visual")
def run_visual(ctx: JobContext) -> dict:
    p = ctx.params
    max_iter = max(1, min(5, int(p.get("max_iterations", 3))))
    intensity = p.get("intensity", "moderada")
    max_ops = INTENSITY_MAX_OPS.get(intensity, 8)
    auto_apply = bool(p.get("auto_apply", False))
    time_limit = float(p.get("time_limit_s", 1200))
    t0 = time.monotonic()
    with SessionLocal() as db:
        project = db.get(Project, ctx.project_id)
        s = load_settings(db)
        try:
            sel = select(s, "vision")
        except AIUnavailable as exc:
            raise JobFailed(str(exc)) from None
        limits = {**DEFAULT_LIMITS, **(s.limits or {}), **(p.get("limits") or {})}
        prices = dict(s.prices or {})
        mode = p.get("mode") or (project.settings or {}).get("export_mode", "fluid")
        base_rev_id = ctx.result("snapshot") or project.head_revision_id
        if not ctx.done("snapshot"):
            ctx.run_step("snapshot", lambda: base_rev_id, "Snapshot da versão base")
        base_doc = unpack(db.get(Revision, base_rev_id).doc)
        pdf_path = get_storage().local_path(project_key(project.id, "original.pdf"))
        password = project_password(project)
    pages = p.get("pages")
    if not pages:
        chapters = p.get("chapters")
        if chapters:
            pages = sorted({e.sources[0].page for i, e in _chapter_iter(base_doc) if i in chapters and e.sources})
        else:
            pages = list(range(len(base_doc.pages)))
    pages = [int(x) for x in pages if 0 <= int(x) < len(base_doc.pages)]
    warn = []
    if len(pages) > int(limits["max_pages"]):
        warn.append(f"Escopo reduzido de {len(pages)} para {limits['max_pages']} páginas (limite configurado).")
        pages = pages[: int(limits["max_pages"])]
    pages = [pg for pg in pages if _unit_elements(base_doc, pg)]
    if not pages:
        raise JobFailed("Nenhuma página do escopo tem elementos incluídos para comparar.")

    budget = CallBudget(limits=limits, prices=prices)
    dpi = float(limits.get("render_dpi") or 110)
    work = get_storage().local_path(project_key(ctx.project_id, "work", ctx.job_id))
    work.mkdir(parents=True, exist_ok=True)

    cand_id = ctx.result("state") and ctx.result("state")["cand_id"] or base_rev_id
    iterations: list[dict] = (ctx.result("state") or {}).get("iterations", [])
    units = (ctx.result("state") or {}).get("units") or {str(pg): {"status": "ativa", "scores": [], "errors": []}
                                                           for pg in pages}
    prev_metrics = (ctx.result("state") or {}).get("prev_metrics")
    pending_ops: list[dict] = (ctx.result("state") or {}).get("pending_ops", [])
    accepted_ops: list[dict] = (ctx.result("state") or {}).get("accepted_ops", [])
    start_it = len(iterations)
    stop_reason = None

    for it in range(start_it, max_iter + 1):
        ctx.check_cancel()
        if time.monotonic() - t0 > time_limit:
            stop_reason = "limite de tempo atingido"
            break
        ctx.progress(it / (max_iter + 1), f"Iteração {it + 1}: exportando candidata")
        with SessionLocal() as db:
            project = db.get(Project, ctx.project_id)
            rev = db.get(Revision, cand_id)
            exp = export_revision(db, project, rev, mode, purpose="candidate")
            db.commit()
            exp_id, exp_path = exp.id, get_storage().local_path(exp.storage_key)
            ec_errors = (exp.epubcheck or {}).get("errors")
            integ_ok = (exp.integrity or {}).get("ok", False)
            cand_doc = unpack(rev.doc)
        ctx.progress((it + 0.3) / (max_iter + 1), f"Iteração {it + 1}: capturando prévia do EPUB exportado")
        try:
            cap = capture_epub(exp_path, work, mode=mode)
        except CaptureUnavailable as exc:
            raise JobFailed(str(exc)) from None
        with SessionLocal() as db:
            db.add(RenderMap(project_id=ctx.project_id, export_id=exp_id, reader=cap["reader"],
                             boxes=cap["boxes"]))
            db.commit()

        scores, ssims, unit_results = {}, {}, {}
        for k, pg in enumerate(pages):
            u = units[str(pg)]
            if u["status"] != "ativa":
                continue
            ctx.check_cancel()
            els = _unit_elements(cand_doc, pg)
            ref = render.render_page(pdf_path, pg, dpi=dpi if mode == "fluid" else 96, password=password)
            ref = render.mask_page(ref, _excluded_boxes(cand_doc, pg), dpi if mode == "fluid" else 96)
            if mode == "fixed":
                shot = next((c for c in cap["pages"] if c["file"].endswith(f"page{pg + 1:04d}.xhtml")), None)
                if shot is None:
                    u["status"] = "sem prévia"
                    continue
                from PIL import Image

                with Image.open(shot["shot"]) as im:
                    epub_img = im.convert("RGB")
                ssims[str(pg)] = round(ssim(ref, epub_img), 4)
            else:
                epub_img = crop_epub_region(cap, [e.id for e in els])
                if epub_img is None:
                    u["status"] = "sem prévia"
                    continue
            try:
                resp = call_structured(
                    sel, budget, purpose="visual", system=_prompt(mode, max_ops),
                    text=_unit_payload(cand_doc, els, mode), images=[png_bytes(ref), png_bytes(epub_img)],
                    schema=visual_schema(), response_model=VisualResponse, project_id=ctx.project_id,
                    job_id=ctx.job_id, summary={"page": pg, "elements": len(els), "images": 2, "iteration": it})
            except BudgetExceeded as exc:
                stop_reason = str(exc)
                break
            except ProviderError as exc:
                u["errors"].append(f"{exc.kind}: {exc}")
                u["status"] = "erro"
                continue
            scores[str(pg)] = resp.score
            u["scores"].append(resp.score)
            unit_results[str(pg)] = resp
            ctx.progress((it + 0.3 + 0.6 * (k + 1) / len(pages)) / (max_iter + 1),
                         f"Iteração {it + 1}: página {pg + 1} avaliada")
        if stop_reason:
            break

        metrics = {"scores": scores, "ssim": ssims, "epubcheck_errors": ec_errors, "integrity_ok": integ_ok,
                   "export_id": exp_id}
        # Avaliação da iteração anterior: aceita ou reverte
        if it > 0 and prev_metrics is not None and pending_ops:
            common = [k for k in scores if k in prev_metrics["scores"]]
            before = sum(prev_metrics["scores"][k] for k in common) / max(1, len(common))
            after = sum(scores[k] for k in common) / max(1, len(common))
            regress = []
            if not integ_ok:
                regress.append("integridade de conteúdo falhou")
            if (ec_errors or 0) > (prev_metrics.get("epubcheck_errors") or 0):
                regress.append("EPUBCheck piorou")
            if ssims and prev_metrics.get("ssim"):
                s0 = sum(prev_metrics["ssim"].get(k, 0) for k in ssims) / len(ssims)
                s1 = sum(ssims.values()) / len(ssims)
                metrics["ssim_delta"] = round(s1 - s0, 4)
            accepted = not regress and after >= before + MIN_IMPROVEMENT
            it_log = iterations[-1]
            it_log["evaluation"] = {"score_before": round(before, 2), "score_after": round(after, 2),
                                    "accepted": accepted, "regressions": regress}
            if accepted:
                accepted_ops += pending_ops
            else:
                cand_id = it_log["base_revision_id"]  # rollback
                it_log["evaluation"]["reason"] = ", ".join(regress) or \
                    f"sem melhora suficiente ({before:.2f} → {after:.2f})"
                with SessionLocal() as db:
                    prop = db.get(AIProposal, it_log["proposal_id"])
                    prop.status = "reverted"
                    prop.metrics = {**prop.metrics, **it_log["evaluation"]}
                    db.commit()
                stop_reason = "regressão/sem melhora: versão anterior restaurada"
                pending_ops = []
                break
            pending_ops = []
        prev_metrics = metrics
        if it == max_iter:
            stop_reason = stop_reason or "limite de iterações atingido"
            break

        # Coleta e valida operações propostas
        op_records, valid_ops = [], []
        for pg_key, resp in unit_results.items():
            scope = {e.id for e in _unit_elements(cand_doc, int(pg_key))}
            for a in resp.operations[:max_ops]:
                rec = {"unit": int(pg_key), "ai": a.model_dump(exclude_none=True), "status": "invalid", "error": None}
                try:
                    d = to_engine_op(a)
                    check_ai_op(cand_doc, _parse(d), scope_ids=scope if d["op"] != "set_global_style" else None)
                    rec.update(op=d, status="valid")
                    valid_ops.append(d)
                except (ConversionError, OpError) as exc:
                    rec["error"] = str(exc)
                op_records.append(rec)
            if not resp.operations:
                units[pg_key]["status"] = "convergiu"
        if not valid_ops:
            iterations.append({"iteration": it, "metrics": metrics, "ops": op_records, "applied": 0,
                               "base_revision_id": cand_id})
            stop_reason = "convergência (nenhuma operação válida proposta)"
            break
        new_doc, applied = cand_doc, []
        for d, rec in zip(valid_ops, [r for r in op_records if r["status"] == "valid"]):
            try:
                nxt = apply_ops(new_doc, [d], actor="ai")
                if nxt.content_hash() == new_doc.content_hash():
                    rec["status"] = "no_effect"
                    continue
                new_doc = nxt
                rec["status"] = "applied"
                applied.append(d)
            except OpError as exc:
                rec.update(status="invalid", error=str(exc))
        if not applied:
            iterations.append({"iteration": it, "metrics": metrics, "ops": op_records, "applied": 0,
                               "base_revision_id": cand_id})
            stop_reason = "convergência (operações propostas não alteram o documento)"
            break
        with SessionLocal() as db:
            project = db.get(Project, ctx.project_id)
            new_rev = commit_revision(db, project, new_doc, kind="ai_visual", ops=applied, actor="ai",
                                      label=f"Candidata visual (iteração {it + 1})", parent_id=cand_id, set_head=False)
            prop = AIProposal(project_id=project.id, job_id=ctx.job_id, kind="visual", unit=",".join(map(str, pages)),
                              iteration=it, base_revision_id=cand_id, result_revision_id=new_rev.id,
                              diagnosis="\n".join(f"Página {int(k) + 1}: {r.diagnosis}" for k, r in unit_results.items()),
                              ops=op_records, metrics=metrics, status="candidate")
            db.add(prop)
            db.commit()
            iterations.append({"iteration": it, "metrics": metrics, "ops": op_records, "applied": len(applied),
                               "base_revision_id": cand_id, "candidate_revision_id": new_rev.id,
                               "proposal_id": prop.id})
            cand_id = new_rev.id
        pending_ops = applied
        ctx.run_step(f"iter-{it}", lambda: True)
        ctx._step("state")["result"] = {"cand_id": cand_id, "iterations": iterations, "units": units,
                                        "prev_metrics": prev_metrics, "pending_ops": pending_ops,
                                        "accepted_ops": accepted_ops}
        ctx._step("state")["status"] = "running"
        ctx._save(steps=[dict(x) for x in ctx.steps])

    # Finalização
    final = {"stop_reason": stop_reason, "iterations": iterations, "units": units, "warnings": warn,
             "usage": {"requests": budget.requests, "input_tokens": budget.input_tokens,
                       "output_tokens": budget.output_tokens,
                       "cost_estimate": round(budget.cost, 4) if budget.cost else None},
             "base_revision_id": base_rev_id, "best_revision_id": cand_id}
    with SessionLocal() as db:
        project = db.get(Project, ctx.project_id)
        if cand_id != base_rev_id and accepted_ops:
            summary = AIProposal(project_id=project.id, job_id=ctx.job_id, kind="visual", unit="final",
                                 iteration=len(iterations), base_revision_id=base_rev_id, result_revision_id=cand_id,
                                 diagnosis=f"{len(accepted_ops)} operação(ões) aceitas em {len(iterations)} iteração(ões).",
                                 ops=[{"op": o, "status": "pending"} for o in accepted_ops],
                                 metrics={"stop_reason": stop_reason}, status="pending")
            db.add(summary)
            db.flush()
            final["proposal_id"] = summary.id
            if auto_apply:
                if project.head_revision_id == base_rev_id:
                    rev = db.get(Revision, cand_id)
                    newrev = commit_revision(db, project, unpack(rev.doc), kind="ai_visual", ops=accepted_ops,
                                             actor="ai", label="Fidelidade visual (aplicada automaticamente)")
                    summary.status = "applied"
                    summary.ops = [{"op": o, "status": "applied"} for o in accepted_ops]
                    final["applied_revision_id"] = newrev.id
                else:
                    summary.diagnosis += " Documento mudou durante a execução: aprovação manual necessária."
        audit(db, project.id, "ai_visual", stop_reason=stop_reason, requests=budget.requests)
        db.commit()
    return final


def _parse(d: dict):
    from ..engine.ops import OperationList

    return OperationList.validate_python([d])[0]


def _chapter_iter(doc: Document):
    """(índice do capítulo, elemento) para elementos incluídos, como no EPUB fluido."""
    ch = 0
    started = False
    for e in doc.elements:
        if not e.included:
            continue
        if e.chapter_break and started:
            ch += 1
        started = True
        yield ch, e
