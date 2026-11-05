"""API HTTP (FastAPI). Operações demoradas viram tarefas; progresso por SSE."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import re
import shutil
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import jobs, tasks  # noqa: F401  (registra handlers)
from .ai import runtime as airt
from .ai.providers import ProviderError, validate_ollama_url
from .config import find_epubcheck, get_settings
from .db import (AICall, AIProposal, AISettings, Export, Job, Project, Revision, SessionLocal, audit, init_db)
from .engine.epub import EPUB_PROFILE, safe_read_epub
from .engine.extract import PdfError, available_ocr_langs, validate_pdf
from .engine.model import GLOBAL_STYLE_RULES, STYLE_TOKENS
from .engine.ops import OPS_SCHEMA_VERSION, OpError, apply_ops, check_ai_op, equivalent_ids, exclusion_impact, OperationList
from .secrets import SecretUnavailable, encrypt, install_log_redaction
from .services import (Conflict, can_redo, commit_revision, export_summary, load_doc, redo, restore, undo, unpack,
                       apply_user_ops)
from .storage import get_storage, project_key

log = logging.getLogger("app")


@asynccontextmanager
async def lifespan(_app):
    install_log_redaction()
    init_db()
    yield


app = FastAPI(title="Conversor PDF → EPUB", version="0.1.0", lifespan=lifespan)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _project(db: Session, pid: str) -> Project:
    p = db.get(Project, pid)
    if p is None:
        raise HTTPException(404, "Projeto não encontrado.")
    return p


def _project_dict(db: Session, p: Project) -> dict:
    head = db.get(Revision, p.head_revision_id) if p.head_revision_id else None
    return {"id": p.id, "name": p.name, "original_filename": p.original_filename, "status": p.status,
            "error": p.error, "page_count": p.page_count, "pdf_size": p.pdf_size, "pdf_sha256": p.pdf_sha256,
            "encrypted": bool(p.password_enc), "settings": p.settings or {},
            "head_revision": {"id": head.id, "seq": head.seq, "kind": head.kind, "doc_hash": head.doc_hash} if head else None,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "updated_at": p.updated_at.isoformat() if p.updated_at else None}


# --------------------------------------------------------------------------- saúde

@app.get("/api/health")
def health(db: Session = Depends(get_db)):
    langs, missing = available_ocr_langs(get_settings().ocr_languages)
    from .ai.capture import _chromium_path

    return {"ok": True, "db": db.bind.dialect.name, "epub_profile": EPUB_PROFILE,
            "limits": {"max_upload_mb": get_settings().max_upload_mb, "max_pages": get_settings().max_pages,
                       "upload_chunk_max": CHUNK_MAX},
            "epubcheck": {"jar": bool(find_epubcheck())}, "ocr": {"languages": langs, "missing": missing},
            "chromium": {"configured_path": _chromium_path()}, "ops_schema_version": OPS_SCHEMA_VERSION,
            "style_tokens": STYLE_TOKENS, "global_style_rules": GLOBAL_STYLE_RULES}


# --------------------------------------------------------------------------- projetos

@app.get("/api/projects")
def list_projects(db: Session = Depends(get_db)):
    ps = db.scalars(select(Project).order_by(Project.updated_at.desc())).all()
    return [_project_dict(db, p) for p in ps]


@app.post("/api/projects")
async def create_project(file: UploadFile = File(...), name: str = Form(""), password: str = Form(""),
                         ocr_languages: str = Form(""), db: Session = Depends(get_db)):
    """Envio em uma única requisição (arquivos pequenos). A interface usa /api/uploads (em partes)."""
    cfg = get_settings()
    limit = cfg.max_upload_mb * 1024 * 1024
    h = hashlib.sha256()
    size = 0
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf", dir=cfg.data_dir) as tmp:
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            if size > limit:
                tmp.close()
                Path(tmp.name).unlink(missing_ok=True)
                raise HTTPException(413, f"Arquivo maior que o limite de {cfg.max_upload_mb} MB.")
            h.update(chunk)
            tmp.write(chunk)
        tmp_path = Path(tmp.name)
    return _create_project_from_file(db, tmp_path, size, h.hexdigest(), file.filename or "documento.pdf",
                                     name, password, ocr_languages)


def _create_project_from_file(db: Session, tmp_path: Path, size: int, sha: str, filename: str, name: str,
                              password: str, ocr_languages: str) -> dict:
    cfg = get_settings()
    try:
        info = validate_pdf(tmp_path, password or None, cfg.max_pages)
    except PdfError as exc:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(422, {"code": exc.code, "message": str(exc)}) from None
    pw_enc = None
    if password:
        try:
            pw_enc = encrypt(password)
        except SecretUnavailable as exc:
            tmp_path.unlink(missing_ok=True)
            raise HTTPException(400, {"code": "secret", "message": str(exc)}) from None
    meta_title = (info["metadata"].get("Title") or "").strip()
    if meta_title.lower() in ("untitled", "sem título", "sem titulo", "document", "microsoft word"):
        meta_title = ""
    title = name.strip() or meta_title or Path(filename).stem
    p = Project(name=title[:300], original_filename=filename[:500], pdf_sha256=sha,
                pdf_size=size, page_count=info["page_count"], password_enc=pw_enc, status="importing",
                settings={"export_mode": "fluid", "ocr_languages": ocr_languages or cfg.ocr_languages})
    db.add(p)
    db.flush()
    dest = get_storage().local_path(project_key(p.id, "original.pdf"))
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(tmp_path), dest)
    dest.chmod(0o444)  # original imutável
    audit(db, p.id, "import", size=size, pages=info["page_count"])
    db.commit()
    job = jobs.enqueue(db, p.id, "extract", {})
    db.refresh(p)
    return {"project": _project_dict(db, p), "job": jobs.job_dict(job)}


# --------------------------------------------------------------------------- envio em partes
# O proxy do frontend (Next.js) limita cada corpo de requisição a 10 MiB (proxyClientMaxBodySize).
# A interface divide o PDF em partes de até CHUNK_MAX e as envia em sequência, com retomada.

CHUNK_MAX = 9 * 1024 * 1024
_UPLOAD_ID = re.compile(r"^[0-9a-f]{32}$")


def _upload_dir(uid: str) -> Path:
    if not _UPLOAD_ID.match(uid):
        raise HTTPException(400, "Identificador de envio inválido.")
    return get_settings().data_dir / "uploads" / uid


class UploadInit(BaseModel):
    filename: str = Field(min_length=1, max_length=500)
    size: int = Field(gt=0)
    chunk_size: int = Field(gt=0, le=CHUNK_MAX)


@app.post("/api/uploads")
def upload_init(body: UploadInit):
    cfg = get_settings()
    if body.size > cfg.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, f"Arquivo maior que o limite de {cfg.max_upload_mb} MB.")
    root = cfg.data_dir / "uploads"
    root.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for old in root.iterdir():  # limpa envios abandonados (> 24 h)
        if old.is_dir() and now - old.stat().st_mtime > 86400:
            shutil.rmtree(old, ignore_errors=True)
    uid = uuid.uuid4().hex
    d = root / uid
    d.mkdir()
    total = (body.size + body.chunk_size - 1) // body.chunk_size
    (d / "meta.json").write_text(json.dumps({"filename": body.filename, "size": body.size,
                                             "chunk_size": body.chunk_size, "chunks": total}))
    return {"upload_id": uid, "chunks": total, "chunk_size": body.chunk_size, "max_chunk": CHUNK_MAX}


@app.get("/api/uploads/{uid}")
def upload_status(uid: str):
    d = _upload_dir(uid)
    if not d.is_dir():
        raise HTTPException(404, "Envio não encontrado ou expirado.")
    meta = json.loads((d / "meta.json").read_text())
    got = sorted(int(f.stem) for f in d.glob("*.part"))
    return {**meta, "received": got}


@app.put("/api/uploads/{uid}/chunks/{index}")
async def upload_chunk(uid: str, index: int, request: Request):
    d = _upload_dir(uid)
    if not d.is_dir():
        raise HTTPException(404, "Envio não encontrado ou expirado.")
    meta = json.loads((d / "meta.json").read_text())
    if not 0 <= index < meta["chunks"]:
        raise HTTPException(400, "Índice de parte inválido.")
    expected = meta["chunk_size"] if index < meta["chunks"] - 1 else meta["size"] - meta["chunk_size"] * index
    data = bytearray()
    async for part in request.stream():
        data += part
        if len(data) > CHUNK_MAX:
            raise HTTPException(413, "Parte maior que o permitido.")
    if len(data) != expected:
        raise HTTPException(400, f"Parte {index} com {len(data)} bytes; esperados {expected}.")
    tmp = d / f"{index:06d}.tmp"
    tmp.write_bytes(data)
    tmp.replace(d / f"{index:06d}.part")
    return {"index": index, "size": len(data)}


class UploadComplete(BaseModel):
    name: str = ""
    password: str = ""
    ocr_languages: str = ""


@app.post("/api/uploads/{uid}/complete")
def upload_complete(uid: str, body: UploadComplete, db: Session = Depends(get_db)):
    d = _upload_dir(uid)
    if not d.is_dir():
        raise HTTPException(404, "Envio não encontrado ou expirado.")
    meta = json.loads((d / "meta.json").read_text())
    missing = [i for i in range(meta["chunks"]) if not (d / f"{i:06d}.part").is_file()]
    if missing:
        raise HTTPException(409, {"code": "incomplete", "message": f"Faltam {len(missing)} parte(s).",
                                  "missing": missing[:50]})
    h = hashlib.sha256()
    out = d / "assembled.pdf"
    with open(out, "wb") as f:
        for i in range(meta["chunks"]):
            chunk = (d / f"{i:06d}.part").read_bytes()
            h.update(chunk)
            f.write(chunk)
    size = out.stat().st_size
    if size != meta["size"]:
        raise HTTPException(400, "Tamanho final diverge do informado.")
    try:
        res = _create_project_from_file(db, out, size, h.hexdigest(), meta["filename"], body.name, body.password,
                                        body.ocr_languages)
    except HTTPException as exc:
        if isinstance(exc.detail, dict) and exc.detail.get("code") == "password":
            # mantém as partes para nova tentativa com senha
            out.unlink(missing_ok=True)
            raise
        shutil.rmtree(d, ignore_errors=True)
        raise
    shutil.rmtree(d, ignore_errors=True)
    return res


@app.get("/api/projects/{pid}")
def get_project(pid: str, db: Session = Depends(get_db)):
    p = _project(db, pid)
    d = _project_dict(db, p)
    js = db.scalars(select(Job).where(Job.project_id == pid).order_by(Job.created_at.desc()).limit(20)).all()
    d["jobs"] = [jobs.job_dict(j) for j in js]
    return d


class ProjectPatch(BaseModel):
    name: str | None = None
    export_mode: str | None = Field(default=None, pattern="^(fluid|fixed)$")
    ocr_languages: str | None = Field(default=None, pattern=r"^[a-z_]{3,10}(\+[a-z_]{3,10})*$")


@app.patch("/api/projects/{pid}")
def patch_project(pid: str, body: ProjectPatch, db: Session = Depends(get_db)):
    p = _project(db, pid)
    if body.name:
        p.name = body.name[:300]
    st = dict(p.settings or {})
    if body.export_mode:
        st["export_mode"] = body.export_mode
    if body.ocr_languages:
        st["ocr_languages"] = body.ocr_languages
    p.settings = st
    db.commit()
    return _project_dict(db, p)


@app.delete("/api/projects/{pid}")
def delete_project(pid: str, confirm: bool = Query(False), db: Session = Depends(get_db)):
    p = _project(db, pid)
    if not confirm:
        raise HTTPException(400, "Confirme a exclusão (confirm=true).")
    running = db.scalars(select(Job).where(Job.project_id == pid, Job.status == "running")).first()
    if running:
        raise HTTPException(409, "Há tarefa em execução; cancele antes de excluir.")
    # arquivos primeiro (o original é somente leitura): se falhar, o banco continua consistente
    st = get_storage()
    path = st.local_path(project_key(pid))
    if path.exists():
        for f in path.rglob("*"):
            try:
                f.chmod(0o755 if f.is_dir() else 0o644)
            except OSError:
                pass
    try:
        st.delete_prefix(project_key(pid))
    except OSError as exc:
        raise HTTPException(500, f"Não foi possível apagar os arquivos do projeto: {exc}") from None
    for model in (AIProposal, Export, Revision, Job):
        for row in db.scalars(select(model).where(model.project_id == pid)).all():
            db.delete(row)
    for row in db.scalars(select(AICall).where(AICall.project_id == pid)).all():
        db.delete(row)
    from .db import AuditLog, RenderMap

    for model in (RenderMap, AuditLog):
        for row in db.scalars(select(model).where(model.project_id == pid)).all():
            db.delete(row)
    db.delete(p)
    db.commit()
    return {"deleted": pid}


@app.get("/api/projects/{pid}/pdf")
def get_pdf(pid: str, db: Session = Depends(get_db)):
    _project(db, pid)
    path = get_storage().local_path(project_key(pid, "original.pdf"))
    return FileResponse(path, media_type="application/pdf", headers={"Cache-Control": "private, max-age=3600"})


@app.get("/api/projects/{pid}/assets/{name}")
def get_asset(pid: str, name: str, db: Session = Depends(get_db)):
    if not re.fullmatch(r"[A-Za-z0-9_\-]+\.(png|jpg|jpeg)", name):
        raise HTTPException(400, "Nome inválido.")
    path = get_storage().local_path(project_key(pid, "assets", name))
    if not path.is_file():
        raise HTTPException(404, "Recurso não encontrado.")
    return FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})


# --------------------------------------------------------------------------- documento e edição

def _doc_payload(db, p: Project) -> dict:
    try:
        doc, rev = load_doc(db, p)
    except LookupError:
        raise HTTPException(409, "Documento ainda não extraído.") from None
    return {"revision": {"id": rev.id, "seq": rev.seq, "kind": rev.kind, "label": rev.label, "doc_hash": rev.doc_hash,
                         "can_undo": rev.parent_id is not None, "can_redo": can_redo(db, p)},
            "document": json.loads(doc.model_dump_json())}


@app.get("/api/projects/{pid}/document")
def get_document(pid: str, db: Session = Depends(get_db)):
    return _doc_payload(db, _project(db, pid))


class OpsBody(BaseModel):
    base_revision_id: int | None = None
    ops: list[dict]
    label: str | None = None


@app.post("/api/projects/{pid}/ops")
def post_ops(pid: str, body: OpsBody, db: Session = Depends(get_db)):
    p = _project(db, pid)
    try:
        apply_user_ops(db, p, body.base_revision_id, body.ops, body.label)
    except Conflict as exc:
        raise HTTPException(409, str(exc)) from None
    except (OpError, ValueError) as exc:
        db.rollback()
        raise HTTPException(422, f"Operação inválida: {exc}") from None
    db.commit()
    return _doc_payload(db, p)


@app.post("/api/projects/{pid}/impact")
def impact(pid: str, ids: list[str] = Body(..., embed=True), db: Session = Depends(get_db)):
    doc, _ = load_doc(db, _project(db, pid))
    return {"impact": exclusion_impact(doc, ids)}


@app.get("/api/projects/{pid}/equivalents/{eid}")
def equivalents(pid: str, eid: str, db: Session = Depends(get_db)):
    doc, _ = load_doc(db, _project(db, pid))
    try:
        return {"ids": equivalent_ids(doc, eid)}
    except OpError as exc:
        raise HTTPException(404, str(exc)) from None


@app.post("/api/projects/{pid}/undo")
def post_undo(pid: str, db: Session = Depends(get_db)):
    p = _project(db, pid)
    try:
        undo(db, p)
    except Conflict as exc:
        raise HTTPException(409, str(exc)) from None
    db.commit()
    return _doc_payload(db, p)


@app.post("/api/projects/{pid}/redo")
def post_redo(pid: str, db: Session = Depends(get_db)):
    p = _project(db, pid)
    try:
        redo(db, p)
    except Conflict as exc:
        raise HTTPException(409, str(exc)) from None
    db.commit()
    return _doc_payload(db, p)


@app.get("/api/projects/{pid}/revisions")
def list_revisions(pid: str, db: Session = Depends(get_db)):
    p = _project(db, pid)
    revs = db.scalars(select(Revision).where(Revision.project_id == pid).order_by(Revision.seq.desc()).limit(300)).all()
    return [{"id": r.id, "seq": r.seq, "kind": r.kind, "actor": r.actor, "label": r.label, "named": r.named,
             "parent_id": r.parent_id, "ops": len(r.ops or []), "op_kinds": sorted({o.get("op", "?") for o in r.ops or []}),
             "is_head": r.id == p.head_revision_id, "doc_hash": r.doc_hash,
             "created_at": r.created_at.isoformat() if r.created_at else None} for r in revs]


@app.post("/api/projects/{pid}/revisions/{rid}/restore")
def post_restore(pid: str, rid: int, db: Session = Depends(get_db)):
    p = _project(db, pid)
    try:
        restore(db, p, rid)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    db.commit()
    return _doc_payload(db, p)


@app.post("/api/projects/{pid}/versions")
def save_version(pid: str, label: str = Body(..., embed=True), db: Session = Depends(get_db)):
    p = _project(db, pid)
    rev = db.get(Revision, p.head_revision_id)
    rev.named = True
    rev.label = label[:300]
    audit(db, pid, "version", seq=rev.seq, label=label[:300])
    db.commit()
    return {"id": rev.id, "seq": rev.seq, "label": rev.label}


# --------------------------------------------------------------------------- tarefas

class JobBody(BaseModel):
    kind: str = Field(pattern="^(extract|export|validate|visual|editorial)$")
    params: dict[str, Any] = Field(default_factory=dict)


@app.post("/api/projects/{pid}/jobs")
def create_job(pid: str, body: JobBody, db: Session = Depends(get_db)):
    p = _project(db, pid)
    active = db.scalars(select(Job).where(Job.project_id == pid, Job.kind == body.kind,
                                          Job.status.in_(["queued", "running"]))).first()
    if active:
        raise HTTPException(409, f"Já existe uma tarefa '{body.kind}' em andamento.")
    if body.kind in ("visual", "editorial"):
        s = airt.load_settings(db)
        try:
            airt.select(s, "vision" if body.kind == "visual" else "text")
        except airt.AIUnavailable as exc:
            raise HTTPException(400, str(exc)) from None
    if body.kind == "export":
        body.params.setdefault("mode", (p.settings or {}).get("export_mode", "fluid"))
    job = jobs.enqueue(db, pid, body.kind, body.params)
    return jobs.job_dict(job)


@app.get("/api/projects/{pid}/jobs")
def list_jobs(pid: str, db: Session = Depends(get_db)):
    js = db.scalars(select(Job).where(Job.project_id == pid).order_by(Job.created_at.desc()).limit(50)).all()
    return [jobs.job_dict(j) for j in js]


@app.get("/api/jobs/{jid}")
def get_job(jid: str, db: Session = Depends(get_db)):
    j = db.get(Job, jid)
    if j is None:
        raise HTTPException(404, "Tarefa não encontrada.")
    return jobs.job_dict(j)


@app.post("/api/jobs/{jid}/cancel")
def cancel_job(jid: str, db: Session = Depends(get_db)):
    j = db.get(Job, jid)
    if j is None:
        raise HTTPException(404, "Tarefa não encontrada.")
    jobs.cancel(db, j)
    return jobs.job_dict(j)


@app.post("/api/jobs/{jid}/retry")
def retry_job(jid: str, db: Session = Depends(get_db)):
    j = db.get(Job, jid)
    if j is None:
        raise HTTPException(404, "Tarefa não encontrada.")
    try:
        jobs.retry(db, j)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    db.refresh(j)
    return jobs.job_dict(j)


@app.get("/api/projects/{pid}/events")
async def events(pid: str, request: Request):
    """SSE: emite mudanças de tarefas e da revisão corrente do projeto."""

    async def gen():
        seen: dict[str, str] = {}
        head = None
        while True:
            if await request.is_disconnected():
                break
            with SessionLocal() as db:
                p = db.get(Project, pid)
                if p is None:
                    yield "event: deleted\ndata: {}\n\n"
                    break
                js = db.scalars(select(Job).where(Job.project_id == pid).order_by(Job.created_at.desc()).limit(20)).all()
                for j in js:
                    stamp = f"{j.status}|{j.progress}|{j.message}|{j.updated_at}"
                    if seen.get(j.id) != stamp:
                        seen[j.id] = stamp
                        yield f"event: job\ndata: {json.dumps(jobs.job_dict(j), ensure_ascii=False)}\n\n"
                if p.head_revision_id != head:
                    head = p.head_revision_id
                    yield f"event: head\ndata: {json.dumps({'revision_id': head, 'status': p.status})}\n\n"
            yield ": ping\n\n"
            await asyncio.sleep(1.0)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})


# --------------------------------------------------------------------------- exportações e prévia

@app.get("/api/projects/{pid}/exports")
def list_exports(pid: str, purpose: str = "user", db: Session = Depends(get_db)):
    p = _project(db, pid)
    head = db.get(Revision, p.head_revision_id) if p.head_revision_id else None
    q = select(Export).where(Export.project_id == pid).order_by(Export.created_at.desc()).limit(50)
    if purpose != "all":
        q = q.where(Export.purpose == purpose)
    return [export_summary(e, head.doc_hash if head else None) for e in db.scalars(q).all()]


def _export(db, eid) -> Export:
    e = db.get(Export, eid)
    if e is None:
        raise HTTPException(404, "Exportação não encontrada.")
    return e


@app.get("/api/exports/{eid}")
def get_export(eid: str, db: Session = Depends(get_db)):
    e = _export(db, eid)
    p = db.get(Project, e.project_id)
    head = db.get(Revision, p.head_revision_id)
    d = export_summary(e, head.doc_hash if head else None)
    d["mapping"] = (e.build or {}).get("mapping", {})
    return d


@app.get("/api/exports/{eid}/download")
def download_export(eid: str, db: Session = Depends(get_db)):
    e = _export(db, eid)
    p = db.get(Project, e.project_id)
    safe = re.sub(r"[^\w\-. ]+", "_", p.name).strip() or "livro"
    return FileResponse(get_storage().local_path(e.storage_key), media_type="application/epub+zip",
                        filename=f"{safe}{'-fixo' if e.mode == 'fixed' else ''}.epub")


PREVIEW_CSP = ("default-src 'none'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; font-src 'self'; "
               "frame-ancestors 'self'; base-uri 'none'; form-action 'none'")
_epub_cache: dict[str, dict[str, bytes]] = {}


@app.get("/api/exports/{eid}/files/{path:path}")
def preview_file(eid: str, path: str, font: int = Query(100, ge=50, le=300), paged: bool = False,
                 theme: str = Query("light", pattern="^(light|dark|sepia)$"), hl: str | None = Query(None, pattern=r"^[A-Za-z0-9]{1,40}$"),
                 db: Session = Depends(get_db)):
    """Serve arquivos do EPUB exportado (o arquivo real) para a prévia, com CSP restritiva e sem scripts."""
    e = _export(db, eid)
    files = _epub_cache.get(eid)
    if files is None:
        files = safe_read_epub(get_storage().local_path(e.storage_key))
        if len(_epub_cache) > 8:
            _epub_cache.clear()
        _epub_cache[eid] = files
    data = files.get(path)
    if data is None:
        raise HTTPException(404, "Arquivo não existe no EPUB.")
    mt = mimetypes.guess_type(path)[0] or "application/octet-stream"
    if path.endswith(".xhtml"):
        mt = "application/xhtml+xml"
        reader = [f"html{{font-size:{font}%;}}"]
        if theme == "dark":
            reader.append("html,body{background:#1e1e1e!important;color:#e6e6e6!important;} a{color:#8ab4f8}")
        elif theme == "sepia":
            reader.append("html,body{background:#f4ecd8!important;color:#3b2f20!important;}")
        if paged and e.mode == "fluid":
            reader.append("html{height:100vh;overflow-x:auto;overflow-y:hidden;}"
                          "body{height:calc(100vh - 2em);column-width:calc(100vw - 2em);column-gap:2em;"
                          "column-fill:auto;margin:1em!important;}")
        if hl:
            reader.append(f"#e-{hl}{{outline:3px solid #f59e0b;outline-offset:2px;background:rgba(245,158,11,.12);}}")
        reader.append("[id^='e-']{cursor:pointer;}")
        style = ("<style>/* camada do leitor de prévia (não faz parte do EPUB) */" + "".join(reader) + "</style>").encode()
        data = data.replace(b"</head>", style + b"</head>", 1)
    elif mt in ("text/html",):
        mt = "text/plain"
    return Response(data, media_type=mt, headers={"Content-Security-Policy": PREVIEW_CSP,
                                                   "X-Content-Type-Options": "nosniff",
                                                   "Cache-Control": "no-store"})


# --------------------------------------------------------------------------- IA: configurações

class AISettingsBody(BaseModel):
    mode: str | None = Field(default=None, pattern="^(none|local|gemini)$")
    gemini_key: str | None = Field(default=None, max_length=300)  # somente escrita; "" remove
    gemini_text_model: str | None = Field(default=None, max_length=100)
    gemini_vision_model: str | None = Field(default=None, max_length=100)
    ollama_url: str | None = Field(default=None, max_length=300)
    ollama_text_model: str | None = Field(default=None, max_length=100)
    ollama_vision_model: str | None = Field(default=None, max_length=100)
    limits: dict[str, Any] | None = None
    prices: dict[str, dict[str, float]] | None = None


@app.get("/api/ai/settings")
def get_ai_settings(db: Session = Depends(get_db)):
    return airt.settings_public(airt.load_settings(db))


@app.put("/api/ai/settings")
def put_ai_settings(body: AISettingsBody, db: Session = Depends(get_db)):
    s = airt.load_settings(db)
    if body.gemini_key is not None:
        if body.gemini_key == "":
            s.gemini_key_enc = None
        else:
            try:
                s.gemini_key_enc = encrypt(body.gemini_key.strip())
            except SecretUnavailable as exc:
                raise HTTPException(400, str(exc)) from None
    if body.ollama_url is not None:
        try:
            s.ollama_url = validate_ollama_url(body.ollama_url, get_settings().ollama_allowed_hosts.split(","))
        except ProviderError as exc:
            raise HTTPException(400, str(exc)) from None
    for f in ("mode", "gemini_text_model", "gemini_vision_model", "ollama_text_model", "ollama_vision_model"):
        v = getattr(body, f)
        if v is not None:
            setattr(s, f, v.strip())
    if body.limits is not None:
        allowed = set(airt.DEFAULT_LIMITS)
        clean = {k: v for k, v in body.limits.items() if k in allowed}
        s.limits = {**(s.limits or {}), **clean}
    if body.prices is not None:
        s.prices = {m: {k: float(v) for k, v in pr.items() if k in ("input_per_mtok", "output_per_mtok")}
                    for m, pr in body.prices.items()}
    audit(db, None, "ai_settings", mode=s.mode)  # nunca registra a chave
    db.commit()
    return airt.settings_public(s)


@app.get("/api/ai/models")
def ai_models(provider: str = Query(..., pattern="^(gemini|ollama)$"), db: Session = Depends(get_db)):
    s = airt.load_settings(db)
    try:
        return {"models": airt.make_provider(s, provider).list_models()}
    except ProviderError as exc:
        raise HTTPException(502, str(exc)) from None


class DetectBody(BaseModel):
    provider: str = Field(pattern="^(gemini|ollama)$")
    model: str = Field(min_length=1, max_length=100)
    probe: bool = True


@app.post("/api/ai/detect")
def ai_detect(body: DetectBody, db: Session = Depends(get_db)):
    s = airt.load_settings(db)
    try:
        caps = airt.make_provider(s, body.provider).detect(body.model, probe=body.probe)
    except ProviderError as exc:
        raise HTTPException(502, str(exc)) from None
    allc = dict(s.capabilities or {})
    allc[f"{body.provider}:{body.model}"] = caps.as_dict()
    s.capabilities = allc
    db.commit()
    return caps.as_dict()


@app.post("/api/projects/{pid}/ai/preview")
def ai_scope_preview(pid: str, body: JobBody, db: Session = Depends(get_db)):
    """Mostra previamente o que será enviado ao provedor (destino, páginas, imagens, volume de texto)."""
    p = _project(db, pid)
    s = airt.load_settings(db)
    doc, _ = load_doc(db, p)
    limits = {**airt.DEFAULT_LIMITS, **(s.limits or {})}
    need = "vision" if body.kind == "visual" else "text"
    out: dict[str, Any] = {"mode": s.mode, "kind": body.kind, "sends_full_pdf": False}
    try:
        sel = airt.select(s, need)
        out.update(provider=sel.provider.name, model=sel.model,
                   destination=("nuvem — Google Gemini API (dados saem desta máquina)" if sel.provider.is_cloud
                                else f"local — Ollama em {s.ollama_url}"), available=True)
    except airt.AIUnavailable as exc:
        out.update(available=False, reason=str(exc))
    if body.kind == "visual":
        pages = body.params.get("pages") or list(range(len(doc.pages)))
        pages = pages[: int(limits["max_pages"])]
        it = int(body.params.get("max_iterations", 3))
        chars = sum(len((e.text or "")[:160]) + 120 for e in doc.elements
                    if e.included and e.sources and e.sources[0].page in pages)
        out.update(pages=[x + 1 for x in pages], images_per_iteration=2 * len(pages),
                   max_calls=len(pages) * (it + 1), text_chars_per_iteration=chars,
                   render_dpi=limits["render_dpi"],
                   note="Somente regiões renderizadas das páginas escolhidas e trechos (até 160 caracteres) dos "
                        "elementos incluídos. Itens excluídos são mascarados.")
    else:
        full = bool(body.params.get("ocr_correction"))
        chars = sum(min(len(e.text or ""), 10_000 if full else 420) + 150 for e in doc.elements if e.included)
        out.update(text_chars=chars, estimated_input_tokens=chars // 4, images=0,
                   note=("Texto integral dos elementos (correção de OCR ativada)." if full else
                         "Trechos de até ~400 caracteres por elemento, estrutura e estilos; sem imagens."))
    price = (s.prices or {}).get(out.get("model", ""))
    if price and out.get("estimated_input_tokens"):
        out["estimated_cost_usd"] = round(out["estimated_input_tokens"] * price.get("input_per_mtok", 0) / 1e6, 4)
        out["price_source"] = "preço configurado pelo usuário"
    return out


# --------------------------------------------------------------------------- IA: propostas

def _proposal_dict(db, pr: AIProposal) -> dict:
    ops = pr.ops or []
    base = db.get(Revision, pr.base_revision_id)
    by = unpack(base.doc).by_id() if base and any((o.get("op") or {}).get("op") == "set_text" for o in ops) else {}
    out_ops = []
    for o in ops:
        d = dict(o)
        op = o.get("op") or {}
        if op.get("op") == "set_text" and op.get("id") in by:
            d["old_text"] = by[op["id"]].text
        out_ops.append(d)
    return {"id": pr.id, "kind": pr.kind, "unit": pr.unit, "iteration": pr.iteration, "status": pr.status,
            "base_revision_id": pr.base_revision_id, "result_revision_id": pr.result_revision_id,
            "diagnosis": pr.diagnosis, "ops": out_ops, "metrics": pr.metrics, "job_id": pr.job_id,
            "created_at": pr.created_at.isoformat() if pr.created_at else None}


@app.get("/api/projects/{pid}/ai/proposals")
def list_proposals(pid: str, db: Session = Depends(get_db)):
    _project(db, pid)
    prs = db.scalars(select(AIProposal).where(AIProposal.project_id == pid)
                     .order_by(AIProposal.created_at.desc()).limit(100)).all()
    return [_proposal_dict(db, x) for x in prs]


@app.post("/api/proposals/{prid}/apply")
def apply_proposal(prid: str, indexes: list[int] | None = Body(None, embed=True), db: Session = Depends(get_db)):
    pr = db.get(AIProposal, prid)
    if pr is None:
        raise HTTPException(404, "Proposta não encontrada.")
    if pr.status not in ("pending", "partial"):
        raise HTTPException(409, f"Proposta em estado '{pr.status}' não pode ser aplicada.")
    p = db.get(Project, pr.project_id)
    doc, head = load_doc(db, p)
    allow_text = any((o.get("op") or {}).get("op") == "set_text" for o in pr.ops)
    new, applied, results = doc, [], []
    ops = list(pr.ops)
    for i, o in enumerate(ops):
        op = o.get("op")
        if not op or o.get("status") not in ("pending", "valid"):
            continue
        if indexes is not None and i not in indexes:
            continue
        try:
            check_ai_op(new, OperationList.validate_python([op])[0], allow_text=allow_text)
            new = apply_ops(new, [op], actor="ai", allow_text=allow_text)
            ops[i] = {**o, "status": "applied"}
            applied.append(op)
        except (OpError, ValueError) as exc:
            ops[i] = {**o, "status": "invalid", "error": f"inválida na versão atual: {exc}"}
        results.append(ops[i]["status"])
    if not applied:
        pr.ops = ops
        db.commit()
        raise HTTPException(422, "Nenhuma operação pôde ser aplicada na versão atual.")
    rev = commit_revision(db, p, new, kind=f"ai_{pr.kind}", ops=applied, actor="ai",
                          label=f"Proposta de IA ({pr.kind}) aprovada")
    remaining = any(o.get("status") in ("pending", "valid") for o in ops)
    pr.ops = ops
    pr.status = "partial" if remaining else "applied"
    audit(db, p.id, "proposal_apply", proposal=prid, applied=len(applied), revision=rev.seq)
    db.commit()
    return {"proposal": _proposal_dict(db, pr), **_doc_payload(db, p)}


@app.post("/api/proposals/{prid}/reject")
def reject_proposal(prid: str, db: Session = Depends(get_db)):
    pr = db.get(AIProposal, prid)
    if pr is None:
        raise HTTPException(404, "Proposta não encontrada.")
    pr.status = "rejected"
    db.commit()
    return _proposal_dict(db, pr)


@app.get("/api/projects/{pid}/ai/calls")
def list_calls(pid: str, db: Session = Depends(get_db)):
    calls = db.scalars(select(AICall).where(AICall.project_id == pid).order_by(AICall.id.desc()).limit(200)).all()
    return [{"id": c.id, "provider": c.provider, "model": c.model, "purpose": c.purpose, "cached": c.cached,
             "input_tokens": c.input_tokens, "output_tokens": c.output_tokens, "cost_estimate": c.cost_estimate,
             "status": c.status, "error": c.error, "summary": c.request_summary,
             "created_at": c.created_at.isoformat() if c.created_at else None} for c in calls]


@app.get("/api/projects/{pid}/audit")
def list_audit(pid: str, db: Session = Depends(get_db)):
    from .db import AuditLog

    rows = db.scalars(select(AuditLog).where(AuditLog.project_id == pid).order_by(AuditLog.id.desc()).limit(200)).all()
    return [{"action": r.action, "detail": r.detail, "created_at": r.created_at.isoformat()} for r in rows]


@app.exception_handler(SecretUnavailable)
def _secret_exc(_, exc):
    return JSONResponse(status_code=400, content={"detail": str(exc)})
