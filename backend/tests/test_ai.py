"""Fluxos de IA com provedor simulado (CI). Integrações reais: ver docs/VERIFICACAO.md."""
import json
import logging
import zipfile
from pathlib import Path

import pytest

from app.ai import runtime
from app.ai.providers import MockProvider, ProviderError
from tests.conftest import upload

HAS_CHROMIUM = Path("/usr/bin/chromium").exists()


def _setup(client, pdfs, name="simple.pdf", vision=True, responder=None):
    mock = MockProvider(responder=responder, vision=vision)
    runtime.set_mock_provider(mock)
    r = client.put("/api/ai/settings", json={"mode": "local", "ollama_text_model": "mock-text",
                                             "ollama_vision_model": "mock-vision" if vision else "mock-text"})
    assert r.status_code == 200, r.text
    caps = client.post("/api/ai/detect", json={"provider": "ollama", "model": "mock-vision" if vision else "mock-text"}).json()
    assert caps["vision"] is vision
    pid = upload(client, pdfs[name]).json()["project"]["id"]
    return pid, mock


def _doc(client, pid):
    return client.get(f"/api/projects/{pid}/document").json()


def test_no_ai_mode_blocks_ai_but_conversion_works(client, pdfs):
    pid = upload(client, pdfs["simple.pdf"]).json()["project"]["id"]
    assert client.get("/api/ai/settings").json()["mode"] == "none"
    r = client.post(f"/api/projects/{pid}/jobs", json={"kind": "editorial", "params": {}})
    assert r.status_code == 400 and "desativada" in r.json()["detail"]
    j = client.post(f"/api/projects/{pid}/jobs", json={"kind": "export", "params": {}}).json()
    assert j["status"] == "succeeded"


def test_text_only_local_model_disables_visual(client, pdfs):
    pid, _ = _setup(client, pdfs, vision=False)
    r = client.post(f"/api/projects/{pid}/jobs", json={"kind": "visual", "params": {}})
    assert r.status_code == 400 and "imagem" in r.json()["detail"]
    prev = client.post(f"/api/projects/{pid}/ai/preview", json={"kind": "visual", "params": {}}).json()
    assert prev["available"] is False


def test_local_failure_never_falls_back_to_gemini(client, pdfs, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("Gemini não pode ser usado no modo local")

    monkeypatch.setattr("app.ai.runtime.GeminiProvider", boom)
    pid, mock = _setup(client, pdfs, responder=lambda c: ProviderError("oom", "memória insuficiente"))
    j = client.post(f"/api/projects/{pid}/jobs", json={"kind": "editorial", "params": {}}).json()
    assert j["status"] == "failed" and "guia" in j["error"]
    assert _doc(client, pid)["revision"]["kind"] == "extraction"  # trabalho preservado


def _editorial_responder(target_kind="style"):
    def resp(call):
        data = json.loads(call["text"])
        if "inventario" in data:
            return {"rules": ["títulos de capítulo centralizados"], "heading_policy": "h1 capítulos",
                    "global_props": [{"name": "line_height", "value": "1.6"}], "notes": ""}
        ops = []
        for el in data["elementos"]:
            if el["tipo"] == "heading":
                ops.append({"op": "set_style", "target_ids": [el["id"]],
                            "style": [{"name": "align", "value": "center"}], "reason": "padronizar"})
            if el["tipo"] == "paragraph" and target_kind == "text":
                ops.append({"op": "set_text", "target_ids": [el["id"]], "text": "TEXTO INVENTADO",
                            "reason": "x"})
        return {"summary": f"capítulo {data['capitulo']}", "operations": ops}
    return resp


def test_editorial_css_only_preserves_text_and_covers_chapters(client, pdfs):
    pid, mock = _setup(client, pdfs, responder=_editorial_responder())
    before = _doc(client, pid)["document"]
    j = client.post(f"/api/projects/{pid}/jobs", json={"kind": "editorial", "params": {
        "profile": "tecnico", "apply_mode": "css"}}).json()
    assert j["status"] == "succeeded", j
    res = j["result"]
    assert res["status"] == "applied" or "applied_revision_id" in res
    assert all(c["status"] == "revisado" for c in res["coverage"]), res["coverage"]
    after = _doc(client, pid)["document"]
    assert [e["text"] for e in before["elements"]] == [e["text"] for e in after["elements"]]
    assert after["global_style"]["line_height"] == 1.6
    assert all(e["output_style"].get("align") == "center" for e in after["elements"] if e["type"] == "heading")
    # undo volta ao estado anterior
    d = client.post(f"/api/projects/{pid}/undo").json()["document"]
    assert d["global_style"]["line_height"] == before["global_style"]["line_height"]


def test_editorial_text_changes_rejected_without_ocr_option(client, pdfs):
    pid, mock = _setup(client, pdfs, responder=_editorial_responder("text"))
    j = client.post(f"/api/projects/{pid}/jobs", json={"kind": "editorial", "params": {"apply_mode": "css_structure"}}).json()
    assert j["status"] == "succeeded"
    props = client.get(f"/api/projects/{pid}/ai/proposals").json()
    invalid = [o for o in props[0]["ops"] if o["status"] == "invalid"]
    assert invalid and all("set_text" in json.dumps(o["ai"]) for o in invalid)
    assert not any("TEXTO INVENTADO" in (e["text"] or "") for e in _doc(client, pid)["document"]["elements"])
    # com correção de OCR: vira proposta pendente (nunca aplicada automaticamente) com texto anterior
    j = client.post(f"/api/projects/{pid}/jobs", json={"kind": "editorial", "params": {
        "apply_mode": "css_structure", "ocr_correction": True}}).json()
    pr = client.get(f"/api/projects/{pid}/ai/proposals").json()[0]
    assert pr["status"] == "pending"
    st = [o for o in pr["ops"] if (o.get("op") or {}).get("op") == "set_text"]
    assert st and st[0]["old_text"]


def test_editorial_invalid_json_and_excluded_items(client, pdfs):
    calls = {"n": 0}

    def resp(call):
        calls["n"] += 1
        data = json.loads(call["text"])
        if "inventario" in data:
            return {"rules": [], "heading_policy": "", "global_props": []}
        return "{json inválido"

    pid, _ = _setup(client, pdfs, responder=resp)
    head_before = _doc(client, pid)["revision"]["id"]
    j = client.post(f"/api/projects/{pid}/jobs", json={"kind": "editorial", "params": {"apply_mode": "css"}}).json()
    assert j["status"] == "succeeded"
    assert all(c["status"] == "não revisado" for c in j["result"]["coverage"])
    assert _doc(client, pid)["revision"]["id"] == head_before


def test_ai_cannot_reinclude_excluded(client, pdfs):
    d0 = None

    def resp(call):
        data = json.loads(call["text"])
        if "inventario" in data:
            return {"rules": [], "heading_policy": "", "global_props": []}
        return {"summary": "", "operations": [
            {"op": "set_style", "target_ids": [d0], "style": [{"name": "align", "value": "center"}], "reason": "x"},
            {"op": "set_included", "target_ids": [d0], "reason": "x"}]}

    pid, _ = _setup(client, pdfs, responder=resp)
    doc = _doc(client, pid)
    d0 = next(e["id"] for e in doc["document"]["elements"] if e["type"] == "paragraph")
    client.post(f"/api/projects/{pid}/ops", json={"ops": [{"op": "set_included", "ids": [d0], "included": False}]})
    client.post(f"/api/projects/{pid}/jobs", json={"kind": "editorial", "params": {"apply_mode": "css_structure"}})
    e = next(x for x in _doc(client, pid)["document"]["elements"] if x["id"] == d0)
    assert e["included"] is False and e["output_style"] == {}


def test_gemini_key_never_exposed(client, pdfs, app_env, caplog):
    key = "test-key-placeholder"
    r = client.put("/api/ai/settings", json={"gemini_key": key, "mode": "gemini"})
    assert r.status_code == 200 and key not in r.text and r.json()["gemini"]["has_key"]
    assert key not in client.get("/api/ai/settings").text
    from app.db import AISettings, AuditLog, SessionLocal

    with SessionLocal() as db:
        s = db.get(AISettings, 1)
        assert s.gemini_key_enc and key not in s.gemini_key_enc
        assert all(key not in json.dumps(a.detail) for a in db.query(AuditLog).all())
    from app.secrets import install_log_redaction

    install_log_redaction()
    with caplog.at_level(logging.INFO):
        logging.getLogger("app").info("chamando com api_key=%s", key)
    assert key not in caplog.text
    pid = upload(client, pdfs["simple.pdf"]).json()["project"]["id"]
    j = client.post(f"/api/projects/{pid}/jobs", json={"kind": "export", "params": {}}).json()
    path = app_env / "data" / "files" / "projects" / pid / "exports" / f"{j['result']['export_id']}.epub"
    with zipfile.ZipFile(path) as z:
        assert all(key.encode() not in z.read(n) for n in z.namelist())
    # frontend: nenhum valor de chave, nenhuma variável pública e só API_URL (servidor) no código e no bundle
    import re

    front = Path(__file__).resolve().parents[2] / "frontend"
    if front.exists():
        for f in front.rglob("*"):
            if not f.is_file() or "node_modules" in f.parts or f.suffix not in (".ts", ".tsx", ".js", ".mjs"):
                continue
            src = f.read_text(errors="ignore")
            assert key not in src and not re.search(r"AIza[0-9A-Za-z_\-]{30,}", src), f
            assert "NEXT_PUBLIC_" not in src, f
            if ".next" not in f.parts:
                envs = set(re.findall(r"process\.env\.([A-Z_]+)", src))
                assert envs <= {"API_URL"}, (f, envs)


@pytest.mark.skipif(not HAS_CHROMIUM, reason="Chromium indisponível para capturar a prévia")
def test_visual_loop_accepts_improvement_and_respects_limits(client, pdfs):
    state = {"n": 0}

    def resp(call):
        state["n"] += 1
        data = json.loads(call["text"])
        it_score = min(9, 4 + 2 * (state["n"] - 1))
        heads = [e["id"] for e in data["elementos"] if e["tipo"] == "heading"]
        ops = [{"op": "set_style", "target_ids": heads[:1],
                "style": [{"name": "emphasis", "value": "bold" if state["n"] % 2 else "italic"}], "reason": "destaque"}]
        return {"diagnosis": "títulos sem destaque", "score": it_score, "operations": ops if heads else []}

    pid, mock = _setup(client, pdfs, responder=resp)
    j = client.post(f"/api/projects/{pid}/jobs", json={"kind": "visual", "params": {
        "pages": [0], "max_iterations": 2, "auto_apply": True}}).json()
    assert j["status"] == "succeeded", j
    res = j["result"]
    assert len(mock.calls) == 3  # 2 iterações + avaliação final
    assert all(c["images"] == 2 for c in mock.calls)
    assert res["stop_reason"] == "limite de iterações atingido"
    assert all(it["evaluation"]["accepted"] for it in res["iterations"] if "evaluation" in it)
    assert "applied_revision_id" in res
    assert _doc(client, pid)["revision"]["kind"] == "ai_visual"


@pytest.mark.skipif(not HAS_CHROMIUM, reason="Chromium indisponível para capturar a prévia")
def test_visual_regression_rolls_back(client, pdfs):
    state = {"n": 0}
    excluded = {}

    def resp(call):
        state["n"] += 1
        data = json.loads(call["text"])
        ids = [e["id"] for e in data["elementos"]]
        assert excluded["id"] not in ids, "item excluído não pode ser enviado como alvo"
        ops = [{"op": "set_style", "target_ids": [ids[0]], "style": [{"name": "size", "value": "xlarge"}], "reason": "x"},
               {"op": "set_style", "target_ids": [excluded["id"]], "style": [{"name": "size", "value": "small"}], "reason": "x"}]
        return {"diagnosis": "d", "score": 6 if state["n"] == 1 else 3, "operations": ops}

    pid, mock = _setup(client, pdfs, responder=resp)
    doc = _doc(client, pid)
    excluded["id"] = next(e["id"] for e in doc["document"]["elements"] if e["type"] == "paragraph")
    client.post(f"/api/projects/{pid}/ops", json={"ops": [{"op": "set_included", "ids": [excluded["id"]], "included": False}]})
    head = _doc(client, pid)["revision"]["id"]
    j = client.post(f"/api/projects/{pid}/jobs", json={"kind": "visual", "params": {
        "pages": [0], "max_iterations": 3, "auto_apply": True}}).json()
    assert j["status"] == "succeeded", j
    res = j["result"]
    ev = res["iterations"][0]["evaluation"]
    assert ev["accepted"] is False and "sem melhora" in ev["reason"]
    assert res["best_revision_id"] == res["base_revision_id"]
    assert _doc(client, pid)["revision"]["id"] == head  # nada aplicado
    ops = res["iterations"][0]["ops"]
    assert any(o["status"] == "invalid" and "excluído" in (o["error"] or "") for o in ops)
    props = client.get(f"/api/projects/{pid}/ai/proposals").json()
    assert any(p["status"] == "reverted" for p in props)
