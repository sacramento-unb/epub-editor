"""Integração: extração e exportação com PDFs gerados localmente."""
import zipfile

import pytest
from lxml import etree

from app.config import find_epubcheck
from app.engine import render
from app.engine.epub import build_epub
from app.engine.extract import available_ocr_langs, extract_document
from app.engine.integrity import check_integrity, run_epubcheck
from app.engine.ops import apply_ops

HAS_EPUBCHECK = find_epubcheck() is not None
OCR_LANG, _ = available_ocr_langs("por+eng")


def ext(pdfs, name, tmp_path):
    return extract_document(pdfs[name], tmp_path / "assets", ocr_languages="por+eng")


def texts(doc, t=None):
    return [e.text for e in doc.elements if (t is None or e.type == t) and e.text]


def test_two_columns_reading_order(pdfs, tmp_path):
    d = ext(pdfs, "two_columns.pdf", tmp_path)
    body = [t for t in texts(d, "paragraph")]
    order = [next(i for i, t in enumerate(body) if t.startswith(p)) for p in
             ("Coluna esquerda parágrafo um", "Coluna esquerda parágrafo dois",
              "Coluna direita parágrafo um", "Coluna direita parágrafo dois")]
    assert order == sorted(order)
    # nenhum parágrafo perdido nem duplicado
    assert len(body) == 4


def test_headers_footers_vs_repeated_body(pdfs, tmp_path):
    d = ext(pdfs, "headers.pdf", tmp_path)
    hdr = [e for e in d.elements if e.type == "header"]
    nums = [e for e in d.elements if e.type == "page_number"]
    assert len(hdr) == 5 and all(not e.included for e in hdr)
    assert len(nums) == 5
    obs = [e for e in d.elements if (e.text or "").startswith("Observação")]
    assert len(obs) == 5 and all(e.included and e.type == "paragraph" for e in obs), \
        "texto de corpo repetido não pode ser excluído só por se repetir"
    secs = [e for e in d.elements if (e.text or "").startswith("Seção")]
    assert all(e.type == "heading" for e in secs)


def test_paragraph_across_pages_and_hyphenation(pdfs, tmp_path):
    d = ext(pdfs, "simple.pdf", tmp_path)
    p = next(e for e in d.elements if (e.text or "").startswith("Este parágrafo começa"))
    assert {s.page for s in p.sources} == {1, 2}
    assert "termina aqui" in p.text
    hy = next(e for e in d.elements if "dividida no fim" in (e.text or ""))
    assert "linha pelo diagramador" in hy.text
    assert "guarda-chuva" in hy.text, "palavra composta deve manter hífen"
    assert [e.level for e in d.elements if e.type == "heading"] == [1, 2, 1]
    assert len([e for e in d.elements if e.type == "list_item"]) == 3
    link = next(e for e in d.elements if "Visite o site" in (e.text or ""))
    assert any(m.kind == "link" and m.href == "https://example.org/projeto" for m in link.marks)
    assert d.outline and d.outline[0]["title"].startswith("Capítulo 1")


def test_tables_figures_notes_formula(pdfs, tmp_path):
    d = ext(pdfs, "tables_figures_notes.pdf", tmp_path)
    tbl = next(e for e in d.elements if e.type == "table")
    assert [c.text for c in tbl.table[0]] == ["Material", "Massa (kg)", "Volume (m³)"]
    assert tbl.caption_id and d.get(tbl.caption_id).text.startswith("Tabela 1")
    img = next(e for e in d.elements if e.type == "image")
    assert img.caption_id and d.get(img.caption_id).text.startswith("Figura 1")
    assert "texto alternativo ausente" in img.review
    note = next(e for e in d.elements if e.type == "footnote")
    ref = next(e for e in d.elements if "norma vigente" in (e.text or ""))
    assert any(m.kind == "noteref" and m.href == note.id for m in ref.marks)
    f = next(e for e in d.elements if e.type == "formula")
    assert f.asset and any("MathML não gerado" in r for r in f.review)


@pytest.mark.skipif(not OCR_LANG, reason="Tesseract indisponível")
def test_scanned_and_mixed_ocr(pdfs, tmp_path):
    d = ext(pdfs, "mixed.pdf", tmp_path)
    assert [p.kind for p in d.pages] == ["digital", "scanned"]
    assert d.doc_kind == "mixed"
    ocr = [e for e in d.elements if e.extraction.method == "ocr"]
    assert ocr and all(e.extraction.confidence is not None for e in ocr)
    joined = " ".join(e.text for e in ocr)
    assert "125 kg" in joined and "2024" in joined
    # a página digital não é duplicada por OCR
    assert sum(1 for e in d.elements if "Página Digital" in (e.text or "")) == 1


def _dark_ratio(img, bbox, dpi):
    s = dpi / 72
    crop = img.convert("L").crop((int(bbox[0] * s), int(bbox[1] * s), int(bbox[2] * s), int(bbox[3] * s)))
    import numpy as np

    a = np.asarray(crop)
    return float((a < 128).mean()) if a.size else 0.0


@pytest.mark.parametrize("name,page", [("cropped.pdf", 0), ("rotated.pdf", 1), ("simple.pdf", 0)])
def test_coordinates_match_rendering(pdfs, tmp_path, name, page):
    """As caixas do modelo coincidem com o raster (CropBox e rotação aplicadas)."""
    d = ext(pdfs, name, tmp_path)
    h = next(e for e in d.elements if e.type == "heading" and e.sources[0].page == page)
    img = render.render_page(pdfs[name], page, dpi=144)
    pg = d.pages[page]
    assert abs(img.width - pg.width * 2) <= 2 and abs(img.height - pg.height * 2) <= 2
    assert _dark_ratio(img, h.sources[0].bbox, 144) > 0.08
    x0, y0, x1, y1 = h.sources[0].bbox
    assert _dark_ratio(img, (x0, max(0, y0 - 30), x1, max(1, y0 - 15)), 144) < 0.02


def _epubcheck_ok(path):
    r = run_epubcheck(path)
    assert r["available"]
    assert r["errors"] == 0, r["messages"]
    return r


@pytest.mark.skipif(not HAS_EPUBCHECK, reason="EPUBCheck não encontrado (tools/ ou EPUBCHECK_JAR)")
@pytest.mark.parametrize("name", ["simple.pdf", "tables_figures_notes.pdf", "headers.pdf", "two_columns.pdf",
                                  "rotated.pdf", "cropped.pdf", "mixed.pdf"])
@pytest.mark.parametrize("mode", ["fluid", "fixed"])
def test_epub_valid_and_complete(pdfs, tmp_path, name, mode):
    d = ext(pdfs, name, tmp_path)
    out = tmp_path / f"{mode}.epub"
    build_epub(d, out, tmp_path / "assets", mode=mode)
    r = _epubcheck_ok(out)
    assert r["warnings"] == 0, r["messages"]
    ig = check_integrity(d, out)
    assert ig["ok"], ig
    with zipfile.ZipFile(out) as z:
        first = z.infolist()[0]
        assert first.filename == "mimetype" and first.compress_type == zipfile.ZIP_STORED
        assert z.read("mimetype") == b"application/epub+zip"
        opf = z.read("OEBPS/content.opf").decode()
    if mode == "fixed":
        assert "rendition:layout\">pre-paginated" in opf
    else:
        assert "pre-paginated" not in opf


def test_exclusions_reflected_and_links_not_broken(pdfs, tmp_path):
    d = ext(pdfs, "tables_figures_notes.pdf", tmp_path)
    note = next(e for e in d.elements if e.type == "footnote")
    img = next(e for e in d.elements if e.type == "image")
    d = apply_ops(d, [{"op": "set_included", "ids": [note.id], "included": False}])
    out = tmp_path / "x.epub"
    build_epub(d, out, tmp_path / "assets")
    with zipfile.ZipFile(out) as z:
        xhtml = z.read("OEBPS/text/ch001.xhtml").decode()
    assert "Norma técnica de referência" not in xhtml
    assert "noteref" not in xhtml, "chamada para nota excluída deve virar texto simples"
    assert img.anchor in xhtml
    if HAS_EPUBCHECK:
        _epubcheck_ok(out)


def test_excluded_text_masked_inside_crops(pdfs, tmp_path):
    """Recorte manual sobre texto excluído não deve reexibir o texto (máscara)."""
    from app.engine.extract import _overlap  # noqa: F401
    from app.engine.model import TEXT_TYPES

    d = ext(pdfs, "simple.pdf", tmp_path)
    p = next(e for e in d.elements if e.type == "paragraph")
    d = apply_ops(d, [{"op": "set_included", "ids": [p.id], "included": False},
                      {"op": "create_region", "page": 0, "bbox": p.sources[0].bbox, "type": "figure", "new_id": "reg1"}])
    img = render.render_page(pdfs["simple.pdf"], 0, dpi=100)
    masks = [s.bbox for e in d.elements if not e.included and e.type in TEXT_TYPES for s in e.sources
             if s.page == 0]
    crop = render.crop_region(img, d.get("reg1").sources[0].bbox, 100, masks=masks)
    import numpy as np

    assert float((np.asarray(crop.convert("L")) < 128).mean()) < 0.01


def test_book_cover_toc_and_heading_levels(pdfs, tmp_path):
    """Regressão do livro real: capa, sumário sem título com entradas quebradas, níveis de título."""
    d = ext(pdfs, "book.pdf", tmp_path)
    cover = [e for e in d.elements if e.type == "cover"]
    assert len(cover) == 1 and cover[0].included and cover[0].asset.endswith(".jpg")
    toc = [e for e in d.elements if e.type == "toc"]
    texts = [e.text for e in toc]
    assert len(toc) == 7, texts
    assert all(e.included and e.subtype == "toc-entry" for e in toc), "sumário reconhecido é mantido"
    long_entry = next(t for t in texts if t.startswith("“Terceira"))
    assert long_entry.endswith("sumário”") and "9" not in long_entry, "entrada em 2 linhas unida e sem o número"
    linked = [e for e in toc if any(m.kind == "link" for m in e.marks)]
    assert len(linked) == 3, "entradas ligadas aos títulos correspondentes"
    heads = [e for e in d.elements if e.type == "heading"]
    claims = [e for e in heads if (e.text or "").startswith("“")]
    provas = [e for e in heads if (e.text or "").startswith("Prova")]
    kickers = [e for e in heads if "ª Parte" in (e.text or "")]
    assert {e.level for e in claims} == {1}, [(e.text, e.level, e.observed_style.size) for e in claims]
    assert {e.level for e in provas} == {2}
    assert all(k.level == 1 for k in kickers) and not any(c.chapter_break for c in claims)
    opener_img = next(e for e in d.elements if e.type == "image" and e.sources[0].page == 2)
    assert opener_img.chapter_break and not kickers[0].chapter_break, "ilustração acima do rótulo abre o capítulo"
    assert kickers[1].chapter_break and kickers[2].chapter_break
    assert not any(e.type == "formula" for e in d.elements), "'+' isolado não é fórmula"
    assert not any("\n" in (e.text or "") for e in d.elements)


@pytest.mark.skipif(not HAS_EPUBCHECK, reason="EPUBCheck não encontrado")
def test_book_epub_cover_valid(pdfs, tmp_path):
    d = ext(pdfs, "book.pdf", tmp_path)
    out = tmp_path / "book.epub"
    r = build_epub(d, out, tmp_path / "assets")
    # capa + capítulo inicial com o sumário do PDF + 3 capítulos
    assert r.spine[0] == "cover.xhtml" and len([s for s in r.spine if s.startswith("text/")]) == 4
    ec = _epubcheck_ok(out)
    assert ec["warnings"] == 0, ec["messages"]
    ig = check_integrity(d, out)
    assert ig["ok"], ig
    with zipfile.ZipFile(out) as z:
        assert 'role="doc-cover"' in z.read("OEBPS/cover.xhtml").decode()
        assert any(n.endswith(".jpg") for n in z.namelist())
        front = z.read("OEBPS/text/ch001.xhtml").decode()
        assert 'class="toc-inline"' in front and front.count('href="ch00') == 3, "entradas viram links relativos"
        assert "4</a>" not in front and "9</a>" not in front, "números de página do PDF removidos"


def test_parallel_extraction_identical(pdfs, tmp_path, monkeypatch):
    """Leitura em vários processos produz exatamente o mesmo modelo que a sequencial."""
    monkeypatch.setenv("EXTRACT_WORKERS", "1")
    a = extract_document(pdfs["book.pdf"], tmp_path / "a", ocr_languages="por+eng")
    monkeypatch.setenv("EXTRACT_WORKERS", "2")
    b = extract_document(pdfs["book.pdf"], tmp_path / "b", ocr_languages="por+eng")
    assert a.model_dump() == b.model_dump()


def test_parallel_extraction_cancel(pdfs, tmp_path, monkeypatch):
    monkeypatch.setenv("EXTRACT_WORKERS", "2")
    with pytest.raises(InterruptedError):
        extract_document(pdfs["book.pdf"], tmp_path / "c", ocr_languages="por+eng", cancelled=lambda: True)
