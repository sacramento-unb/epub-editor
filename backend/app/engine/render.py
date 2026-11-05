"""Renderização de páginas/regiões do PDF com pypdfium2 (BSD/Apache).

O pdfium renderiza a CropBox com a rotação intrínseca aplicada, que é exatamente
o espaço ``page_display_pt`` do modelo documental.
"""
from __future__ import annotations

import threading
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageDraw

# O pdfium não é thread-safe; serializamos o acesso.
_LOCK = threading.Lock()


def display_transform(mediabox, cropbox, rotation: int):
    """Retorna (fn, width, height): fn mapeia (x, top) do pdfplumber para o espaço exibido.

    O pdfplumber entrega coordenadas no espaço da MediaBox rotacionada (origem no
    topo-esquerdo). Convertemos para coordenadas relativas à CropBox exibida.
    """
    mx0, my0, mx1, my1 = mediabox
    rotation %= 360

    def user_to_plumber(x, y):
        if rotation == 90:
            return y - my0, x - mx0
        if rotation == 180:
            return mx1 - x, y - my0
        if rotation == 270:
            return my1 - y, mx1 - x
        return x - mx0, my1 - y

    cx0, cy0, cx1, cy1 = cropbox
    pts = [user_to_plumber(x, y) for x, y in ((cx0, cy0), (cx0, cy1), (cx1, cy0), (cx1, cy1))]
    ox = min(p[0] for p in pts)
    oy = min(p[1] for p in pts)
    w = max(p[0] for p in pts) - ox
    h = max(p[1] for p in pts) - oy

    def fn(x, top):
        return x - ox, top - oy

    return fn, w, h


def open_pdf(path: str | Path, password: str | None = None) -> pdfium.PdfDocument:
    return pdfium.PdfDocument(str(path), password=password or None)


def render_page(path: str | Path, index: int, dpi: float = 110, password: str | None = None,
                doc: pdfium.PdfDocument | None = None) -> Image.Image:
    with _LOCK:
        own = doc is None
        d = doc or open_pdf(path, password)
        try:
            page = d[index]
            img = page.render(scale=dpi / 72.0, may_draw_forms=True).to_pil().convert("RGB")
            page.close()
            return img
        finally:
            if own:
                d.close()


def crop_region(page_img: Image.Image, bbox, dpi: float, pad: float = 2.0,
                masks: list | None = None) -> Image.Image:
    """Recorta ``bbox`` (pontos exibidos) de uma página renderizada a ``dpi``.

    ``masks``: caixas (em pontos) a apagar com branco — usado para impedir que
    texto excluído reapareça dentro de recortes e para referências filtradas.
    """
    s = dpi / 72.0
    img = page_img
    if masks:
        img = page_img.copy()
        draw = ImageDraw.Draw(img)
        for m in masks:
            draw.rectangle([m[0] * s, m[1] * s, m[2] * s, m[3] * s], fill="white")
    x0, y0, x1, y1 = bbox
    box = (max(0, int((x0 - pad) * s)), max(0, int((y0 - pad) * s)),
           min(img.width, int((x1 + pad) * s + 0.999)), min(img.height, int((y1 + pad) * s + 0.999)))
    if box[2] <= box[0] or box[3] <= box[1]:
        return Image.new("RGB", (1, 1), "white")
    return img.crop(box)


def mask_page(page_img: Image.Image, boxes: list, dpi: float, fill="white") -> Image.Image:
    img = page_img.copy()
    s = dpi / 72.0
    draw = ImageDraw.Draw(img)
    for b in boxes:
        draw.rectangle([b[0] * s, b[1] * s, b[2] * s, b[3] * s], fill=fill)
    return img


def save_crop(img: Image.Image, base: Path) -> Path:
    """Salva recorte: PNG para desenhos/texto (poucas cores), JPEG para imagens fotográficas grandes."""
    img = img.convert("RGB")
    photographic = img.getcolors(maxcolors=4096) is None and img.width * img.height > 250_000
    path = base.with_suffix(".jpg" if photographic else ".png")
    if photographic:
        img.save(path, "JPEG", quality=85, optimize=True, progressive=True)
    else:
        img.save(path, "PNG", optimize=True)
    return path
