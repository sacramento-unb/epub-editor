"""Tarefas assíncronas: análise do PDF e exportação/validação do EPUB."""
from __future__ import annotations

from .config import get_settings
from .db import Export, Project, Revision, SessionLocal, audit
from .engine.extract import PdfError, extract_document
from .jobs import JobContext, JobFailed, handler
from .services import commit_revision, export_revision, project_password, unpack
from .storage import get_storage, project_key

# registra handlers de IA
from .ai import editorial as _editorial  # noqa: F401,E402
from .ai import visual as _visual  # noqa: F401,E402


@handler("extract")
def run_extract(ctx: JobContext) -> dict:
    st = get_storage()
    with SessionLocal() as db:
        project = db.get(Project, ctx.project_id)
        pdf = st.local_path(project_key(project.id, "original.pdf"))
        assets = st.local_path(project_key(project.id, "assets"))
        password = project_password(project)
        langs = ctx.params.get("ocr_languages") or (project.settings or {}).get("ocr_languages") \
            or get_settings().ocr_languages

    def extract():
        try:
            doc = extract_document(pdf, assets, password=password, ocr_languages=langs,
                                   ocr_dpi=get_settings().ocr_dpi,
                                   progress=lambda f, m: ctx.progress(f * 0.95, m),
                                   cancelled=lambda: _cancel_check(ctx))
        except PdfError as exc:
            raise JobFailed(str(exc)) from None
        except InterruptedError:
            from .jobs import JobCancelled

            raise JobCancelled() from None
        with SessionLocal() as db:
            project = db.get(Project, ctx.project_id)
            rev = commit_revision(db, project, doc, kind="extraction", actor="system", label="Extração inicial",
                                  parent_id=None)
            project.status = "ready"
            project.page_count = len(doc.pages)
            project.error = None
            audit(db, project.id, "extracted", pages=len(doc.pages), elements=len(doc.elements),
                  kind=doc.doc_kind, warnings=len(doc.warnings))
            db.commit()
            return {"revision_id": rev.id, "elements": len(doc.elements), "pages": len(doc.pages),
                    "doc_kind": doc.doc_kind, "warnings": doc.warnings}

    try:
        return ctx.run_step("extract", extract, "Analisando PDF")
    except JobFailed as exc:
        with SessionLocal() as db:
            project = db.get(Project, ctx.project_id)
            project.status = "error"
            project.error = str(exc)
            db.commit()
        raise


def _cancel_check(ctx: JobContext) -> bool:
    from .jobs import JobCancelled

    try:
        ctx.check_cancel()
        return False
    except JobCancelled:
        return True


@handler("export")
def run_export(ctx: JobContext) -> dict:
    mode = ctx.params.get("mode", "fluid")
    validate = ctx.params.get("validate", True)

    def build():
        with SessionLocal() as db:
            project = db.get(Project, ctx.project_id)
            rev_id = ctx.params.get("revision_id") or project.head_revision_id
            rev = db.get(Revision, rev_id)
            if rev is None:
                raise JobFailed("Projeto sem documento extraído.")
            ctx.progress(0.2, "Gerando EPUB")
            exp = export_revision(db, project, rev, mode, validate=validate)
            audit(db, project.id, "export", mode=mode, export=exp.id, epubcheck=(exp.epubcheck or {}).get("status"))
            db.commit()
            return {"export_id": exp.id, "epubcheck": (exp.epubcheck or {}).get("status"),
                    "errors": (exp.epubcheck or {}).get("errors"), "integrity_ok": (exp.integrity or {}).get("ok")}

    return ctx.run_step("build+validate", build, "Gerando e validando EPUB")


@handler("validate")
def run_validate(ctx: JobContext) -> dict:
    from .engine.integrity import check_integrity, run_epubcheck

    def go():
        with SessionLocal() as db:
            exp = db.get(Export, ctx.params["export_id"])
            path = get_storage().local_path(exp.storage_key)
            exp.epubcheck = run_epubcheck(path)
            exp.integrity = check_integrity(unpack(db.get(Revision, exp.revision_id).doc), path)
            db.commit()
            return {"epubcheck": exp.epubcheck.get("status"), "integrity_ok": exp.integrity.get("ok")}

    return ctx.run_step("validate", go, "Validando EPUB")
