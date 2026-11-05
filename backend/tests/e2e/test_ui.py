"""Teste de interface de ponta a ponta contra uma pilha em execução.

Executar com a aplicação no ar (Docker ou dev):
    E2E_BASE_URL=http://localhost:3000 .venv/bin/python -m pytest -q tests/e2e
Pulado quando E2E_BASE_URL não está definido.
"""
import os
import re
import time

import httpx
import pytest

BASE = os.environ.get("E2E_BASE_URL")
pytestmark = pytest.mark.skipif(not BASE, reason="E2E_BASE_URL não definido")


def _wait(fn, timeout=90, step=1.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = fn()
        if v:
            return v
        time.sleep(step)
    raise AssertionError("tempo esgotado")


def test_fluxo_principal_sem_ia(pdfs):
    from playwright.sync_api import expect, sync_playwright

    launch = {"headless": True}
    if os.environ.get("CHROMIUM_PATH"):
        launch["executable_path"] = os.environ["CHROMIUM_PATH"]
    pid = None
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch(**launch)
            page = b.new_page(viewport={"width": 1500, "height": 950})
            # importação pela interface (envio em partes pelo navegador)
            page.goto(BASE)
            page.locator("input[type=file]").set_input_files(str(pdfs["tables_figures_notes.pdf"]))
            page.wait_for_url(re.compile(r"/projects/[0-9a-f]{32}$"), timeout=60000)
            pid = page.url.rsplit("/", 1)[-1]
            _wait(lambda: httpx.get(f"{BASE}/api/projects/{pid}").json()["status"] == "ready")
            page.reload()
            tree = page.get_by_role("tree")
            expect(tree).to_be_visible(timeout=20000)
            # árvore → destaque no PDF
            page.locator(".tree .item", has_text="Figura 1 – Diagrama").first.click()
            expect(page.locator(".pdfpage .ov.cur").first).to_be_visible(timeout=20000)
            # excluir a nota (diálogo de impacto) e verificar persistência após recarregar
            page.get_by_label(re.compile(r"Incluir Nota ")).click()
            page.get_by_role("button", name="Excluir mesmo assim").click()
            expect(page.get_by_text(re.compile(r"Salvo · rev\. 2"))).to_be_visible(timeout=10000)
            page.reload()
            expect(page.get_by_label(re.compile(r"Incluir Nota "))).not_to_be_checked(timeout=20000)
            # gerar EPUB pela interface e conferir a prévia real
            page.get_by_role("button", name="Gerar EPUB").click()
            page.get_by_role("button", name="Gerar e validar").click()
            frame = page.frame_locator("iframe[title='Prévia do EPUB']")
            expect(frame.locator("h1")).to_have_text("Dados e Figuras", timeout=90000)
            body = frame.locator("body").inner_text()
            assert "Norma técnica de referência" not in body
            assert "Densidades de referência" in body
            # clique na prévia seleciona o elemento na árvore
            frame.locator("table").click()
            expect(page.locator(".tree .item.cur")).to_contain_text("Tabela", timeout=10000)
            # desfazer pelo teclado
            page.locator(".tree").click()
            page.keyboard.press("Control+z")
            expect(page.get_by_label(re.compile(r"Incluir Nota "))).to_be_checked(timeout=10000)
            b.close()
    finally:
        if pid:
            httpx.delete(f"{BASE}/api/projects/{pid}?confirm=true")
