"""API: importação, persistência de seleção, exportação, undo/redo, versões, tarefas."""
import zipfile
from pathlib import Path

from tests.conftest import upload


def _ready(client, pdfs, name="simple.pdf"):
    r = upload(client, pdfs[name])
    assert r.status_code == 200, r.text
    pid = r.json()["project"]["id"]
    assert r.json()["job"]["status"] == "succeeded", r.json()["job"]
    return pid


def test_import_validation(client, tmp_path, pdfs):
    bad = tmp_path / "x.pdf"
    bad.write_bytes(b"not a pdf at all")
    r = upload(client, bad)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "signature"
    r = upload(client, pdfs["encrypted.pdf"])
    assert r.status_code == 422 and r.json()["detail"]["code"] == "password"
    r = upload(client, pdfs["encrypted.pdf"], password="segredo")
    assert r.status_code == 200 and r.json()["job"]["status"] == "succeeded"
    doc = client.get(f"/api/projects/{r.json()['project']['id']}/document").json()["document"]
    assert any("Conteúdo protegido" in (e["text"] or "") for e in doc["elements"])


def test_selection_persists_and_reflects_in_export(client, pdfs, app_env):
    pid = _ready(client, pdfs)
    d = client.get(f"/api/projects/{pid}/document").json()
    rev = d["revision"]["id"]
    p1 = next(e for e in d["document"]["elements"] if e["text"] and e["text"].startswith("Este é o primeiro"))
    r = client.post(f"/api/projects/{pid}/ops", json={"base_revision_id": rev, "ops": [
        {"op": "set_included", "ids": [p1["id"]], "included": False}]})
    assert r.status_code == 200
    # versão desatualizada é rejeitada
    r2 = client.post(f"/api/projects/{pid}/ops", json={"base_revision_id": rev, "ops": [
        {"op": "set_locked", "ids": [p1["id"]], "locked": True}]})
    assert r2.status_code == 409
    # "reabrir": nova conexão lê o estado persistido
    from app import db as dbm

    dbm.reset_engine()
    d2 = client.get(f"/api/projects/{pid}/document").json()
    e = next(x for x in d2["document"]["elements"] if x["id"] == p1["id"])
    assert e["included"] is False and e["excluded_explicit"] is True
    j = client.post(f"/api/projects/{pid}/jobs", json={"kind": "export", "params": {"mode": "fluid"}}).json()
    assert j["status"] == "succeeded", j
    exps = client.get(f"/api/projects/{pid}/exports").json()
    assert exps[0]["stale"] is False and exps[0]["integrity"]["ok"]
    dl = client.get(f"/api/exports/{exps[0]['id']}/download")
    epub = app_env / "dl.epub"
    epub.write_bytes(dl.content)
    with zipfile.ZipFile(epub) as z:
        allx = "".join(z.read(n).decode() for n in z.namelist() if n.endswith(".xhtml"))
    assert "Este é o primeiro parágrafo" not in allx
    assert "Segundo capítulo" in allx
    # mudança de seleção invalida a exportação anterior
    client.post(f"/api/projects/{pid}/ops", json={"ops": [{"op": "set_included", "ids": [p1["id"]], "included": True}]})
    assert client.get(f"/api/projects/{pid}/exports").json()[0]["stale"] is True


def test_undo_redo_restore_versions(client, pdfs):
    pid = _ready(client, pdfs)
    d = client.get(f"/api/projects/{pid}/document").json()
    h = next(e for e in d["document"]["elements"] if e["type"] == "heading")
    client.post(f"/api/projects/{pid}/versions", json={"label": "Antes"})
    d = client.post(f"/api/projects/{pid}/ops", json={"ops": [{"op": "set_text", "id": h["id"], "text": "Novo"}]}).json()
    assert next(e for e in d["document"]["elements"] if e["id"] == h["id"])["text"] == "Novo"
    d = client.post(f"/api/projects/{pid}/undo").json()
    assert next(e for e in d["document"]["elements"] if e["id"] == h["id"])["text"] == h["text"]
    assert d["revision"]["can_redo"]
    d = client.post(f"/api/projects/{pid}/redo").json()
    assert next(e for e in d["document"]["elements"] if e["id"] == h["id"])["text"] == "Novo"
    revs = client.get(f"/api/projects/{pid}/revisions").json()
    named = next(r for r in revs if r["named"])
    d = client.post(f"/api/projects/{pid}/revisions/{named['id']}/restore").json()
    assert next(e for e in d["document"]["elements"] if e["id"] == h["id"])["text"] == h["text"]


def test_preview_serves_real_epub_with_csp(client, pdfs):
    pid = _ready(client, pdfs, "tables_figures_notes.pdf")
    j = client.post(f"/api/projects/{pid}/jobs", json={"kind": "export", "params": {"mode": "fluid"}}).json()
    eid = j["result"]["export_id"]
    r = client.get(f"/api/exports/{eid}/files/OEBPS/text/ch001.xhtml?font=120&paged=true&hl=abc")
    assert r.status_code == 200
    assert "script-src" not in r.headers["content-security-policy"]
    assert "default-src 'none'" in r.headers["content-security-policy"]
    assert b"font-size:120%" in r.content
    assert client.get(f"/api/exports/{eid}/files/../../etc/passwd").status_code == 404
    m = client.get(f"/api/exports/{eid}").json()["mapping"]
    assert all(v["file"].startswith("text/") or v["file"] == "cover.xhtml" for v in m.values())


def test_cancel_retry_and_worker_restart(client, pdfs, monkeypatch):
    from app import jobs
    from app.db import Job, SessionLocal

    pid = _ready(client, pdfs)
    # tarefa interrompida (worker morto): heartbeat antigo volta para a fila e retoma
    monkeypatch.setenv("JOB_INLINE", "0")
    from app import config

    config.get_settings.cache_clear()
    j = client.post(f"/api/projects/{pid}/jobs", json={"kind": "export", "params": {"mode": "fluid"}}).json()
    assert j["status"] == "queued"
    jid = jobs.claim_next()
    assert jid == j["id"]
    with SessionLocal() as db:
        job = db.get(Job, jid)
        job.heartbeat_at = None  # simula worker que morreu
        db.commit()
    assert jobs.requeue_stale(0) == 1
    jid2 = jobs.claim_next()
    jobs.run_job(jid2)
    assert client.get(f"/api/jobs/{jid}").json()["status"] == "succeeded"
    # cancelamento de tarefa na fila
    j = client.post(f"/api/projects/{pid}/jobs", json={"kind": "export", "params": {"mode": "fixed"}}).json()
    c = client.post(f"/api/jobs/{j['id']}/cancel").json()
    assert c["status"] == "cancelled"
    # cancelamento cooperativo durante execução
    j = client.post(f"/api/projects/{pid}/jobs", json={"kind": "extract", "params": {}}).json()
    jid = jobs.claim_next()
    client.post(f"/api/jobs/{jid}/cancel")
    jobs.run_job(jid)
    assert client.get(f"/api/jobs/{jid}").json()["status"] == "cancelled"
    # repetição
    r = client.post(f"/api/jobs/{jid}/retry").json()
    assert r["status"] == "queued"
    jobs.run_job(jobs.claim_next())
    assert client.get(f"/api/jobs/{jid}").json()["status"] == "succeeded"
    # a última versão válida foi preservada durante o cancelamento
    assert client.get(f"/api/projects/{pid}/document").status_code == 200


def test_delete_project_removes_files(client, pdfs, app_env):
    pid = _ready(client, pdfs)
    client.post(f"/api/projects/{pid}/jobs", json={"kind": "export", "params": {}})  # cria subpastas
    folder = Path(app_env) / "data" / "files" / "projects" / pid
    assert folder.exists()
    assert client.delete(f"/api/projects/{pid}").status_code == 400
    assert client.delete(f"/api/projects/{pid}?confirm=true").status_code == 200
    assert not folder.exists()
    assert client.get(f"/api/projects/{pid}").status_code == 404


def _chunked(client, path, chunk=1024 * 1024, skip=(), **complete):
    data = Path(path).read_bytes()
    r = client.post("/api/uploads", json={"filename": Path(path).name, "size": len(data), "chunk_size": chunk})
    assert r.status_code == 200, r.text
    uid, n = r.json()["upload_id"], r.json()["chunks"]
    for i in range(n):
        if i in skip:
            continue
        r = client.put(f"/api/uploads/{uid}/chunks/{i}", content=data[i * chunk:(i + 1) * chunk])
        assert r.status_code == 200, r.text
    return uid, n, data


def test_chunked_upload_resume_and_password(client, pdfs, app_env):
    from tests.fixtures import tables_figures_notes  # noqa: F401

    big = Path(app_env) / "big.pdf"
    import os

    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    from PIL import Image

    c = canvas.Canvas(str(big))
    for p in range(4):
        c.drawString(72, 750, f"Página {p + 1}")
        c.drawImage(ImageReader(Image.frombytes("RGB", (600, 600), os.urandom(600 * 600 * 3))), 72, 100, 400, 400)
        c.showPage()
    c.save()
    assert big.stat().st_size > 3 * 1024 * 1024
    # parte faltando → 409 com a lista; retomada pelo status
    uid, n, data = _chunked(client, big, skip={1})
    r = client.post(f"/api/uploads/{uid}/complete", json={})
    assert r.status_code == 409 and r.json()["detail"]["missing"] == [1]
    assert client.get(f"/api/uploads/{uid}").json()["received"] == [i for i in range(n) if i != 1]
    client.put(f"/api/uploads/{uid}/chunks/1", content=data[1024 * 1024:2 * 1024 * 1024])
    r = client.post(f"/api/uploads/{uid}/complete", json={"name": "Grande"})
    assert r.status_code == 200, r.text
    proj = r.json()["project"]
    assert proj["pdf_size"] == len(data) and proj["page_count"] == 4 and proj["name"] == "Grande"
    import hashlib

    assert proj["pdf_sha256"] == hashlib.sha256(data).hexdigest()
    # parte com tamanho errado e limites
    uid2, _, _ = _chunked(client, pdfs["simple.pdf"], chunk=4096, skip=set(range(1000)))
    assert client.put(f"/api/uploads/{uid2}/chunks/0", content=b"x" * 10).status_code == 400
    assert client.post("/api/uploads", json={"filename": "x.pdf", "size": 10 ** 12, "chunk_size": 1024}).status_code == 413
    assert client.post("/api/uploads", json={"filename": "x.pdf", "size": 100, "chunk_size": 64 * 1024 * 1024}).status_code == 422
    assert client.get("/api/uploads/../../etc").status_code in (400, 404)
    # PDF protegido: senha pedida sem reenviar as partes
    uid3, _, _ = _chunked(client, pdfs["encrypted.pdf"], chunk=2048)
    r = client.post(f"/api/uploads/{uid3}/complete", json={})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "password"
    r = client.post(f"/api/uploads/{uid3}/complete", json={"password": "segredo"})
    assert r.status_code == 200 and r.json()["job"]["status"] == "succeeded"
