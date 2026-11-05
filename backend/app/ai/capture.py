"""Captura da renderização real do arquivo EPUB exportado (Playwright/Chromium).

O EPUB gerado é descompactado com validação de caminhos num diretório
temporário isolado e cada documento da spine é aberto com viewport, escala,
tema e tamanho de fonte fixos — tornando a comparação reproduzível. Rede
externa é bloqueada. As caixas medidas formam um mapeamento de renderização
específico desta exportação/leitor/viewport (não são gravadas no documento).
"""
from __future__ import annotations

import io
import os
import re
import shutil
import tempfile
from pathlib import Path

import numpy as np
from lxml import etree
from PIL import Image

from ..config import get_settings
from ..engine.epub import safe_read_epub

READER_FLUID = {"viewport": [600, 900], "device_scale_factor": 1, "font_scale": 1.0, "theme": "light",
                "reader": "chromium-xhtml", "fonts": "sistema (sem fontes incorporadas)"}

READER_CSS = "html { font-size: %d%% !important; background: #fff !important; color: #000 !important; }"


class CaptureUnavailable(Exception):
    pass


def _chromium_path() -> str | None:
    p = get_settings().chromium_path
    if p and Path(p).exists():
        return p
    return None


def spine_files(files: dict[str, bytes]) -> tuple[str, list[str]]:
    cont = etree.fromstring(files["META-INF/container.xml"])
    opf_path = cont.find(".//{*}rootfile").get("full-path")
    opf = etree.fromstring(files[opf_path])
    base = str(Path(opf_path).parent)
    items = {i.get("id"): i.get("href") for i in opf.iter("{*}item")}
    spine = []
    for ref in opf.iter("{*}itemref"):
        if ref.get("linear") == "no":
            continue
        spine.append(f"{base}/{items[ref.get('idref')]}" if base != "." else items[ref.get("idref")])
    return base, spine


def capture_epub(epub_path: Path, work_dir: Path, *, mode: str = "fluid", reader: dict | None = None) -> dict:
    try:
        from playwright.sync_api import Error as PWError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover
        raise CaptureUnavailable("Playwright não instalado.") from exc
    reader = {**READER_FLUID, **(reader or {})}
    files = safe_read_epub(epub_path)
    _, spine = spine_files(files)
    tmp = Path(tempfile.mkdtemp(prefix="epubcap-", dir=work_dir))
    try:
        root = tmp / "book"
        for name, data in files.items():
            dest = (root / name).resolve()
            if root.resolve() not in dest.parents:
                raise ValueError(f"caminho inseguro: {name}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        shots_dir = work_dir / "shots"
        shots_dir.mkdir(parents=True, exist_ok=True)
        pages, boxes = [], {}
        launch = {"headless": True, "args": ["--disable-gpu", "--disable-extensions"]}
        if os.environ.get("CHROMIUM_NO_SANDBOX") == "1":
            launch["args"].append("--no-sandbox")
        exe = _chromium_path()
        if exe:
            launch["executable_path"] = exe
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(**launch)
                try:
                    for idx, name in enumerate(spine):
                        vw, vh = reader["viewport"]
                        if mode == "fixed":
                            m = re.search(rb'name="viewport" content="width=(\d+), height=(\d+)"', files[name])
                            if m:
                                vw, vh = int(m.group(1)), int(m.group(2))
                        ctx = browser.new_context(viewport={"width": vw, "height": vh},
                                                  device_scale_factor=reader["device_scale_factor"],
                                                  color_scheme="light", offline=True)
                        ctx.route(re.compile(r"^(https?|wss?|ftp):"), lambda r: r.abort())
                        page = ctx.new_page()
                        page.goto((root / name).as_uri(), wait_until="load", timeout=30000)
                        if mode == "fluid":
                            page.add_style_tag(content=READER_CSS % int(reader["font_scale"] * 100))
                        page.evaluate("document.fonts.ready.then(() => true)")
                        png = page.screenshot(full_page=(mode == "fluid"), type="png")
                        out = shots_dir / f"{idx:04d}.png"
                        out.write_bytes(png)
                        found = page.evaluate("""() => Array.from(document.querySelectorAll('[id^="e-"]')).map(el => {
                            const r = el.getBoundingClientRect();
                            const cs = getComputedStyle(el);
                            return [el.id.slice(2), r.left + scrollX, r.top + scrollY, r.width, r.height,
                                    cs.display === 'none' || cs.visibility === 'hidden'];
                        })""")
                        for eid, x, y, w, h, hidden in found:
                            boxes[eid] = {"file": name, "shot": idx, "x": x, "y": y, "w": w, "h": h, "hidden": hidden}
                        with Image.open(io.BytesIO(png)) as im:
                            pages.append({"file": name, "shot": str(out), "width": im.width, "height": im.height,
                                          "viewport": [vw, vh]})
                        ctx.close()
                finally:
                    browser.close()
        except PWError as exc:
            raise CaptureUnavailable(f"Falha ao abrir o Chromium para captura: {str(exc)[:300]}. "
                                     "Instale com 'playwright install chromium' ou defina CHROMIUM_PATH.") from None
        return {"reader": {**reader, "mode": mode}, "pages": pages, "boxes": boxes}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def crop_epub_region(capture: dict, element_ids: list[str], max_h: int = 2400) -> Image.Image | None:
    """Recorta, da captura do EPUB, a faixa vertical que contém os elementos da unidade."""
    bx = [capture["boxes"][e] for e in element_ids if e in capture["boxes"] and not capture["boxes"][e]["hidden"]]
    if not bx:
        return None
    shot = max({b["shot"] for b in bx}, key=lambda s: sum(1 for b in bx if b["shot"] == s))
    bx = [b for b in bx if b["shot"] == shot]
    pg = capture["pages"][shot]
    y0 = max(0, int(min(b["y"] for b in bx)) - 16)
    y1 = min(pg["height"], int(max(b["y"] + b["h"] for b in bx)) + 16)
    with Image.open(pg["shot"]) as im:
        crop = im.convert("RGB").crop((0, y0, pg["width"], max(y0 + 1, y1)))
    if crop.height > max_h:
        crop = crop.resize((int(crop.width * max_h / crop.height), max_h))
    return crop


def png_bytes(img: Image.Image, max_side: int = 1600) -> bytes:
    if max(img.size) > max_side:
        s = max_side / max(img.size)
        img = img.resize((int(img.width * s), int(img.height * s)))
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def ssim(a: Image.Image, b: Image.Image, block: int = 8) -> float:
    """SSIM médio por blocos (indicador auxiliar para layout fixo; não mede qualidade editorial)."""
    b = b.resize(a.size)
    x = np.asarray(a.convert("L"), dtype=np.float64)
    y = np.asarray(b.convert("L"), dtype=np.float64)
    h, w = (x.shape[0] // block) * block, (x.shape[1] // block) * block
    if h == 0 or w == 0:
        return 0.0
    x = x[:h, :w].reshape(h // block, block, w // block, block).swapaxes(1, 2).reshape(-1, block * block)
    y = y[:h, :w].reshape(h // block, block, w // block, block).swapaxes(1, 2).reshape(-1, block * block)
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    mx, my = x.mean(1), y.mean(1)
    vx, vy = x.var(1), y.var(1)
    cov = ((x - mx[:, None]) * (y - my[:, None])).mean(1)
    s = ((2 * mx * my + c1) * (2 * cov + c2)) / ((mx ** 2 + my ** 2 + c1) * (vx + vy + c2))
    return float(s.mean())
