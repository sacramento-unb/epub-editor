"""Serviços de domínio: revisões (desfazer/refazer/versões), exportação e recortes."""
from __future__ import annotations

import hashlib
import json
import zlib
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import Export, Project, Revision, audit
from .engine import render
from .engine.epub import build_epub, sha256_file
from .engine.extract import _overlap
from .engine.integrity import check_integrity, run_epubcheck
from .engine.model import TEXT_TYPES, Document, Element, doc_from_json
from .engine.ops import apply_ops, descendants
from .secrets import decrypt
from .storage import get_storage, project_key


class Conflict(Exception):
    pass


def pack(doc: Document) -> tuple[bytes, str]:
    raw = doc.model_dump_json().encode()
    return zlib.compress(raw, 6), hashlib.sha256(raw).hexdigest()


def unpack(blob: bytes) -> Document:
    return doc_from_json(zlib.decompress(blob))


def get_project(db: Session, pid: str) -> Project | None:
    return db.get(Project, pid)


def head_revision(db: Session, project: Project) -> Revision | None:
    return db.get(Revision, project.head_revision_id) if project.head_revision_id else None


def load_doc(db: Session, project: Project) -> tuple[Document, Revision]:
    rev = head_revision(db, project)
    if rev is None:
        raise LookupError("projeto ainda não tem documento extraído")
    return unpack(rev.doc), rev


def commit_revision(db: Session, project: Project, doc: Document, *, kind: str, ops: list | None = None,
                    actor: str = "user", label: str | None = None, parent_id: int | None = None,
                    set_head: bool = True) -> Revision:
    blob, h = pack(doc)
    seq = (db.scalar(select(func.max(Revision.seq)).where(Revision.project_id == project.id)) or 0) + 1
    rev = Revision(project_id=project.id, seq=seq, parent_id=parent_id if parent_id is not None else project.head_revision_id,
                   kind=kind, actor=actor, label=label, ops=ops or [], doc=blob, doc_hash=h)
    db.add(rev)
    db.flush()
    if set_head:
        project.head_revision_id = rev.id
    return rev


def apply_user_ops(db: Session, project: Project, base_revision_id: int | None, ops: list[dict],
                   label: str | None = None) -> Revision:
    doc, head = load_doc(db, project)
    if base_revision_id is not None and base_revision_id != head.id:
        raise Conflict("o documento mudou desde a última leitura; recarregue")
    new = apply_ops(doc, ops, actor="user")
    rev = commit_revision(db, project, new, kind="edit", ops=ops, label=label)
    audit(db, project.id, "ops", count=len(ops), kinds=sorted({o.get("op") for o in ops}), revision=rev.seq)
    return rev


def undo(db: Session, project: Project) -> Revision:
    head = head_revision(db, project)
    if head is None or head.parent_id is None:
        raise Conflict("nada para desfazer")
    parent = db.get(Revision, head.parent_id)
    if parent is None:
        raise Conflict("revisão anterior indisponível")
    project.head_revision_id = parent.id
    audit(db, project.id, "undo", from_seq=head.seq, to_seq=parent.seq)
    return parent


def redo(db: Session, project: Project) -> Revision:
    child = db.scalar(select(Revision).where(Revision.project_id == project.id,
                                             Revision.parent_id == project.head_revision_id)
                      .order_by(Revision.id.desc()).limit(1))
    if child is None:
        raise Conflict("nada para refazer")
    project.head_revision_id = child.id
    audit(db, project.id, "redo", to_seq=child.seq)
    return child


def can_redo(db: Session, project: Project) -> bool:
    return db.scalar(select(func.count()).select_from(Revision).where(
        Revision.project_id == project.id, Revision.parent_id == project.head_revision_id)) > 0


def restore(db: Session, project: Project, revision_id: int) -> Revision:
    src = db.get(Revision, revision_id)
    if src is None or src.project_id != project.id:
        raise LookupError("revisão inexistente")
    rev = commit_revision(db, project, unpack(src.doc), kind="restore", label=f"Restaurada da revisão {src.seq}")
    audit(db, project.id, "restore", from_seq=src.seq, new_seq=rev.seq)
    return rev


def project_password(project: Project) -> str | None:
    return decrypt(project.password_enc) if project.password_enc else None


# --------------------------------------------------------------------------- recortes

def make_asset_fn(project: Project, doc: Document):
    """Gera recortes sob demanda, mascarando texto excluído que cairia dentro do recorte."""
    st = get_storage()
    pdf_path = st.local_path(project_key(project.id, "original.pdf"))
    assets = st.local_path(project_key(project.id, "assets"))
    assets.mkdir(parents=True, exist_ok=True)
    password = project_password(project)
    cache: dict[int, object] = {}

    from .engine.extract import crop_dpi

    def dpi_of(i):
        pg = doc.pages[i]
        return crop_dpi(pg.width, pg.height)

    def page_img(i):
        if i not in cache:
            cache[i] = render.render_page(pdf_path, i, dpi=dpi_of(i), password=password)
        return cache[i]

    def fn(e: Element) -> Path | None:
        if not e.sources:
            return assets / e.asset if e.asset else None
        src = e.sources[0]
        own = {d.id for d in descendants(doc, e.id)}
        masks = []
        for o in doc.elements:
            if o.included or o.id in own or o.id == e.id or o.type not in TEXT_TYPES:
                continue
            for s in o.sources:
                if s.page == src.page and _overlap(s.bbox, src.bbox) > 0 and _overlap(s.bbox, src.bbox) > 0.3:
                    masks.append(s.bbox)
        if e.asset and not masks and (assets / e.asset).is_file():
            return assets / e.asset
        key = hashlib.sha1(json.dumps([src.page, src.bbox, masks]).encode()).hexdigest()[:10]
        for ext in (".png", ".jpg"):
            if (assets / f"{e.id}-{key}{ext}").is_file():
                return assets / f"{e.id}-{key}{ext}"
        img = render.crop_region(page_img(src.page), src.bbox, dpi_of(src.page), masks=masks)
        return render.save_crop(img, assets / f"{e.id}-{key}")

    return fn


# --------------------------------------------------------------------------- exportação

def export_revision(db: Session, project: Project, rev: Revision, mode: str, *, purpose: str = "user",
                    validate: bool = True) -> Export:
    doc = unpack(rev.doc)
    exp = Export(project_id=project.id, revision_id=rev.id, doc_hash=rev.doc_hash, mode=mode,
                 storage_key="", purpose=purpose)
    db.add(exp)
    db.flush()
    key = project_key(project.id, "exports", f"{exp.id}.epub")
    st = get_storage()
    out = st.local_path(key)
    res = build_epub(doc, out, st.local_path(project_key(project.id, "assets")), mode=mode,
                     asset_fn=make_asset_fn(project, doc), identifier=f"urn:uuid:{project.id[:8]}-{project.id[8:12]}-"
                     f"{project.id[12:16]}-{project.id[16:20]}-{project.id[20:32]}")
    exp.storage_key = key
    exp.size = out.stat().st_size
    exp.sha256 = sha256_file(out)
    exp.build = {"mapping": res.mapping, "warnings": res.warnings, "spine": res.spine, "files": res.files}
    exp.integrity = check_integrity(doc, out)
    if validate:
        exp.epubcheck = run_epubcheck(out)
    return exp


def export_summary(exp: Export, head_hash: str | None) -> dict:
    return {
        "id": exp.id, "mode": exp.mode, "revision_id": exp.revision_id, "size": exp.size, "sha256": exp.sha256,
        "purpose": exp.purpose, "created_at": exp.created_at.isoformat() if exp.created_at else None,
        "stale": head_hash is not None and exp.doc_hash != head_hash,
        "epubcheck": {k: v for k, v in (exp.epubcheck or {}).items() if k != "messages"},
        "epubcheck_messages": (exp.epubcheck or {}).get("messages", [])[:100],
        "integrity": exp.integrity, "warnings": (exp.build or {}).get("warnings", []),
        "spine": (exp.build or {}).get("spine", []),
    }
