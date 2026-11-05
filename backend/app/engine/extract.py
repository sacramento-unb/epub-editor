"""Importação e análise do PDF: validação, extração nativa, OCR e classificação.

Estratégia: extração nativa (pdfplumber/pdfminer.six) primeiro; OCR (Tesseract)
apenas em páginas sem camada de texto útil. Classificação por heurísticas
explícitas, sempre registrando evidências e pendências de revisão. A confiança
gravada é heurística — não é probabilidade calibrada.
"""
from __future__ import annotations

import os
import re
import statistics
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pdfplumber
import pypdfium2 as pdfium

from . import fastpdf, render

fastpdf.install()
# Tesseract com uma thread: chamadas pequenas ficam bem mais rápidas e as páginas já rodam em paralelo
os.environ.setdefault("OMP_THREAD_LIMIT", "1")
from .model import (Document, Element, Extraction, InlineMark, Metadata, ObservedStyle,
                    PageInfo, SourceRegion, TableCell)


class PdfError(Exception):
    """Erro de validação/abertura do PDF com mensagem para o usuário."""

    def __init__(self, message: str, code: str = "invalid"):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- validação

def validate_pdf(path: Path, password: str | None = None, max_pages: int = 2000) -> dict:
    with open(path, "rb") as f:
        head = f.read(1024)
    if b"%PDF-" not in head:
        raise PdfError("O arquivo não tem assinatura de PDF (%PDF-).", "signature")
    try:
        doc = pdfium.PdfDocument(str(path), password=password or None)
    except pdfium.PdfiumError as exc:
        msg = str(exc).lower()
        if "password" in msg:
            raise PdfError("PDF protegido por senha. Informe a senha se você tiver autorização.",
                           "password") from exc
        raise PdfError(f"PDF corrompido ou ilegível: {exc}", "corrupt") from exc
    try:
        n = len(doc)
        if n == 0:
            raise PdfError("O PDF não tem páginas.", "empty")
        if n > max_pages:
            raise PdfError(f"O PDF tem {n} páginas; o limite configurado é {max_pages}.", "too_many_pages")
        meta = doc.get_metadata_dict(skip_empty=True)
        return {"page_count": n, "metadata": meta, "encrypted": bool(password)}
    finally:
        doc.close()


# --------------------------------------------------------------------------- utilitários

BULLET_RE = re.compile(r"^\s*([•▪◦●○■□‣⁃∙·\-–—*]|\(?\d{1,3}[.)]|\(?[a-zA-Z][.)]|[ivxlcdm]{1,5}[.)])\s+")
CAPTION_RE = re.compile(r"^\s*(figura|fig\.|figure|tabela|table|quadro|gr[áa]fico|chart|"
                        r"ilustra[çc][ãa]o|imagem|foto|mapa|equa[çc][ãa]o)\s*[\dIVXivx]+", re.I)
PAGE_NUM_RE = re.compile(r"^\W*(p[áa]g(ina)?\.?\s*)?(\d{1,4}|[ivxlcdm]{1,7})(\s*(de|/|of)\s*\d{1,4})?\W*$", re.I)
TERMINAL_RE = re.compile(r"[.!?:;…»”\"')\]]\s*$")
MATH_CHARS = set("²³½=+−×÷∑∫√≤≥≈≠∞πσμλαβγδθΔΩ∂∈∉⊂⊆∪∩→⇒↔∀∃±^·′″")
MATH_FONT_RE = re.compile(r"(cmmi|cmsy|cmex|msam|msbm|math|symbol|stix|cambria ?math)", re.I)
TOC_HEAD_RE = re.compile(r"^\s*(sum[áa]rio|[íi]ndice|contents|table of contents)\s*$", re.I)
TOC_LINE_RE = re.compile(r".{3,}?[\s.·…_]{2,}\s*(\d{1,4}|[ivxlcdm]{1,6})\s*$", re.I)
CID_RE = re.compile(r"\(cid:(\d+)\)")
SUP_MARK_RE = re.compile(r"^[\d*†‡§¹²³⁴⁵⁶⁷⁸⁹⁰]{1,3}$")


def normalize_pattern(text: str) -> str:
    t = unicodedata.normalize("NFKC", text).lower().strip()
    t = re.sub(r"\d+", "#", t)
    t = re.sub(r"\b[ivxlcdm]{1,7}\b", "#", t)
    return re.sub(r"\s+", " ", t)


def _is_bold(font: str) -> bool:
    return bool(re.search(r"bold|black|heavy|semibold|demi", font or "", re.I))


def _is_italic(font: str) -> bool:
    return bool(re.search(r"italic|oblique|-it\b|it$", font or "", re.I))


def _clean_font(font: str) -> str:
    return re.sub(r"^[A-Z]{6}\+", "", font or "")


def _color(c) -> str | None:
    try:
        if c is None:
            return None
        if isinstance(c, (int, float)):
            c = (c,)
        c = tuple(float(x) for x in c)
        if len(c) == 1:
            v = int(255 * c[0])
            return f"#{v:02x}{v:02x}{v:02x}"
        if len(c) == 3:
            return "#" + "".join(f"{int(255 * x):02x}" for x in c)
        if len(c) == 4:  # CMYK aproximado
            cc, m, y, k = c
            return "#" + "".join(f"{int(255 * (1 - v) * (1 - k)):02x}" for v in (cc, m, y))
    except (TypeError, ValueError):
        return None
    return None


def _overlap(a, b) -> float:
    """Área de interseção / área de a."""
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    area = max(1e-6, (a[2] - a[0]) * (a[3] - a[1]))
    return (x1 - x0) * (y1 - y0) / area


def _union(boxes):
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


# --------------------------------------------------------------------------- estruturas

@dataclass
class Word:
    text: str
    x0: float
    top: float
    x1: float
    bottom: float
    size: float
    font: str = ""
    color: str | None = None
    upright: bool = True
    conf: float | None = None
    href: str | None = None
    bold: bool | None = None  # medido pela tinta quando a fonte não tem nome
    recovered: bool = False  # texto recuperado por OCR (glifos sem Unicode)

    @property
    def bbox(self):
        return (self.x0, self.top, self.x1, self.bottom)


def _wbold(w: "Word") -> bool:
    return w.bold if w.bold is not None else _is_bold(w.font)


@dataclass
class Line:
    words: list[Word]
    col: int = 0  # -1 = atravessa colunas

    @property
    def x0(self):
        return min(w.x0 for w in self.words)

    @property
    def x1(self):
        return max(w.x1 for w in self.words)

    @property
    def top(self):
        return min(w.top for w in self.words)

    @property
    def bottom(self):
        return max(w.bottom for w in self.words)

    @property
    def size(self):
        sizes = [w.size for w in self.words if len(w.text) > 1] or [w.size for w in self.words]
        return statistics.median(sizes)


@dataclass
class Block:
    page: int
    lines: list[Line]
    col: int
    method: str = "native"
    kind: str | None = None  # classificação
    extra: dict = field(default_factory=dict)

    @property
    def bbox(self):
        return _union([(l.x0, l.top, l.x1, l.bottom) for l in self.lines])

    @property
    def size(self):
        return statistics.median([l.size for l in self.lines])

    def words(self):
        return [w for l in self.lines for w in l.words]


# --------------------------------------------------------------------------- texto

def assemble_text(lines: list[Line], body_size: float, known_hyphenated: set[str]) -> tuple[str, list[InlineMark], list[str]]:
    """Une linhas em texto corrido, desfaz hifenização conservadora e gera marcas."""
    out = ""
    marks: list[InlineMark] = []
    evidence: list[str] = []
    for li, line in enumerate(lines):
        main = line.size
        baseline = statistics.median([w.bottom for w in line.words if w.size >= main * 0.9] or
                                     [w.bottom for w in line.words])
        for wi, w in enumerate(line.words):
            t = w.text
            is_sup = (w.size < main * 0.8 and w.bottom < baseline - main * 0.15
                      and SUP_MARK_RE.match(t) is not None)
            if out:
                if wi == 0:
                    prev = out
                    if prev.endswith("­"):
                        out = prev[:-1]
                        evidence.append("hífen discricionário removido")
                    elif prev.endswith("-") and re.match(r"^[a-zà-ÿ]", t):
                        left = re.findall(r"(\S+)-$", prev)
                        cand = (left[0] if left else "") + "-" + re.sub(r"\W+$", "", t)
                        if cand.lower() in known_hyphenated or (left and "-" in left[0]):
                            evidence.append(f"hífen mantido (composto): {cand}")
                        elif left and len(left[0]) >= 2 and left[0][-1].isalpha():
                            out = prev[:-1]
                            evidence.append(f"hifenização desfeita: {cand}")
                        else:
                            out += ""
                    elif not prev.endswith((" ", "\n")):
                        out += " "
                elif not is_sup:
                    out += " "
            start = len(out)
            out += t
            if is_sup:
                marks.append(InlineMark(start=start, end=len(out), kind="sup"))
            elif _wbold(w) and not _is_bold_line(line):
                _add_mark(marks, "bold", start, len(out))
            elif _is_italic(w.font) and not _is_italic_line(line):
                _add_mark(marks, "italic", start, len(out))
            if w.href:
                _add_mark(marks, "link", start, len(out), w.href)
    return out.strip(), marks, evidence


def _add_mark(marks, kind, start, end, href=None):
    for m in reversed(marks):
        if m.kind == kind and m.href == href and start - m.end <= 1:
            m.end = end
            return
        if m.kind == kind:
            break
    marks.append(InlineMark(start=start, end=end, kind=kind, href=href))


def _bold_frac(line: Line) -> float:
    return sum(1 for w in line.words if _wbold(w)) / max(1, len(line.words))


def _is_bold_line(line: Line) -> bool:
    return all(_wbold(w) for w in line.words)


def _is_italic_line(line: Line) -> bool:
    return all(_is_italic(w.font) for w in line.words)


# --------------------------------------------------------------------------- layout

def group_lines(words: list[Word]) -> list[Line]:
    words = sorted(words, key=lambda w: (round(w.top, 1), w.x0))
    lines: list[list[Word]] = []
    for w in words:
        placed = False
        for ln in reversed(lines[-4:]):
            ref = ln[0]
            mid_w = (w.top + w.bottom) / 2
            if ref.top - 1 <= mid_w <= ref.bottom + 1 or abs(w.bottom - ref.bottom) < max(1.5, ref.size * 0.3):
                ln.append(w)
                placed = True
                break
        if not placed:
            lines.append([w])
    out = []
    for ln in lines:
        ln.sort(key=lambda w: w.x0)
        out.append(Line(ln))
    out.sort(key=lambda l: (l.top, l.x0))
    return out


def find_gutters(lines: list[Line], page_w: float, top_limit: float, bottom_limit: float) -> list[float]:
    """Detecta calhas verticais entre colunas (lista de x)."""
    body = [l for l in lines if top_limit < l.top and l.bottom < bottom_limit]
    if len(body) < 6:
        return []
    res = 2.0
    nb = int(page_w / res) + 1
    cover = [0] * nb
    for l in body:
        for w in l.words:
            for i in range(int(w.x0 / res), min(nb, int(w.x1 / res) + 1)):
                cover[i] += 1
    gutters = []
    lo, hi = int(page_w * 0.2 / res), int(page_w * 0.8 / res)
    i = lo
    while i < hi:
        if cover[i] == 0:
            j = i
            while j < hi and cover[j] == 0:
                j += 1
            if (j - i) * res >= 8:
                x = (i + j) / 2 * res
                lrows = sum(1 for l in body if any(w.x1 <= x for w in l.words))
                rrows = sum(1 for l in body if any(w.x0 >= x for w in l.words))
                rwords = [w for l in body for w in l.words if w.x0 >= x]
                lwords = [w for l in body for w in l.words if w.x1 <= x]
                numeric = sum(1 for w in rwords if re.fullmatch(r"[\divxlcdm.]+", w.text, re.I)) / max(1, len(rwords))
                widths_ok = (max((w.x1 for w in rwords), default=x) - x >= page_w * 0.15
                             and x - min((w.x0 for w in lwords), default=x) >= page_w * 0.15)
                if (lrows >= 3 and rrows >= 3 and min(lrows, rrows) >= 0.25 * len(body)
                        and numeric < 0.6 and widths_ok):
                    gutters.append(x)
            i = j
        else:
            i += 1
    return gutters


def split_columns(lines: list[Line], gutters: list[float]) -> list[Line]:
    if not gutters:
        return lines
    out = []
    for l in lines:
        crossing = any(w.x0 < g < w.x1 for w in l.words for g in gutters)
        if crossing:
            l.col = -1
            out.append(l)
            continue
        parts = defaultdict(list)
        for w in l.words:
            col = sum(1 for g in gutters if (w.x0 + w.x1) / 2 > g)
            parts[col].append(w)
        for col, ws in parts.items():
            out.append(Line(ws, col))
    return out


def reading_order(lines: list[Line]) -> list[Line]:
    """Ordem: faixas separadas por linhas que atravessam colunas; dentro da faixa, coluna a coluna."""
    lines = sorted(lines, key=lambda l: (l.top, l.x0))
    out, band = [], []

    def flush():
        cols = sorted({l.col for l in band})
        for c in cols:
            out.extend(sorted([l for l in band if l.col == c], key=lambda l: l.top))
        band.clear()

    for l in lines:
        if l.col == -1:
            flush()
            out.append(l)
        else:
            band.append(l)
    flush()
    return out


def make_blocks(page: int, lines: list[Line], body_size: float) -> list[Block]:
    blocks: list[Block] = []
    cur: list[Line] = []

    col_left: dict[int, float] = {}
    for l in lines:
        col_left[l.col] = min(col_left.get(l.col, 1e9), l.x0)
    col_right: dict[int, float] = {}
    for l in lines:
        col_right[l.col] = max(col_right.get(l.col, 0), l.x1)

    def align_of(l: Line) -> str:
        left, right = col_left.get(l.col, l.x0), col_right.get(l.col, l.x1)
        width = max(1.0, right - left)
        if l.x0 > left + width * 0.25 and abs(right - l.x1) <= l.size:
            return "right"
        if l.x0 > left + l.size * 2 and abs((l.x0 - left) - (right - l.x1)) <= l.size * 1.5:
            return "center"
        return "left"

    def close():
        if cur:
            b = Block(page, list(cur), cur[0].col)
            al = {align_of(x) for x in cur}
            if len(al) == 1 and al != {"left"}:
                b.extra["align"] = al.pop()
            blocks.append(b)
            cur.clear()

    for l in lines:
        if not cur:
            cur.append(l)
            continue
        p = cur[-1]
        gap = l.top - p.bottom
        lh = max(p.size, l.size)
        new = False
        if l.col != p.col:
            new = True
        elif gap > lh * 0.9 or gap < -lh * 0.5:
            new = True
        elif abs(l.size - p.size) > max(0.6, p.size * 0.12):
            new = True
        elif abs(_bold_frac(l) - _bold_frac(p)) > 0.7 and (len(p.words) < 12 or len(l.words) < 12):
            new = True
        elif BULLET_RE.match(" ".join(w.text for w in l.words)):
            new = True
        elif CAPTION_RE.match(" ".join(w.text for w in l.words)):
            new = True
        elif align_of(l) != align_of(p) and "left" not in (align_of(l), align_of(p)) or \
                (align_of(l) != align_of(p) and (align_of(l) == "right" or align_of(p) == "right")):
            new = True  # ex.: referência alinhada à direita após o parágrafo
        else:
            left = col_left.get(l.col, l.x0)
            right = col_right.get(l.col, l.x1)
            prev_text = " ".join(w.text for w in p.words)
            short_prev = p.x1 < right - lh * 3
            indented = l.x0 > left + lh * 0.9 and p.x0 <= left + lh * 0.5
            if indented and (short_prev or TERMINAL_RE.search(prev_text)):
                new = True
            elif short_prev and TERMINAL_RE.search(prev_text) and l.x0 <= left + 1:
                # linha anterior curta terminada em pontuação: provável fim de parágrafo
                new = True
        if new:
            close()
        cur.append(l)
    close()
    return blocks


# --------------------------------------------------------------------------- extração

ProgressFn = Callable[[float, str], None]
CancelFn = Callable[[], bool]


@dataclass
class PageData:
    info: PageInfo
    words: list[Word]
    rotated_words: list[Word]
    images: list[tuple]  # bbox
    tables: list[dict]
    vector_regions: list[tuple]
    links: list[dict]


def _ocr_page(img, dpi: float, lang: str) -> tuple[list[Word], float | None]:
    import pytesseract

    data = pytesseract.image_to_data(img, lang=lang, output_type=pytesseract.Output.DICT)
    s = 72.0 / dpi
    words, confs = [], []
    for i, txt in enumerate(data["text"]):
        txt = (txt or "").strip()
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1
        if not txt or conf < 0:
            continue
        x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
        words.append(Word(txt, x * s, y * s, (x + w) * s, (y + h) * s, size=max(4.0, h * s * 1.1),
                          conf=conf / 100.0))
        confs.append(conf / 100.0)
    return words, (statistics.mean(confs) if confs else None)


def available_ocr_langs(requested: str) -> tuple[str, list[str]]:
    try:
        import pytesseract

        have = set(pytesseract.get_languages(config=""))
    except Exception:  # tesseract ausente
        return "", requested.split("+")
    req = [l for l in requested.split("+") if l]
    ok = [l for l in req if l in have]
    missing = [l for l in req if l not in have]
    return "+".join(ok), missing


def _resolve_dest(pdf, dest) -> int | None:
    """Resolve destino PDF explícito (lista com referência de página) para índice."""
    try:
        from pdfminer.pdftypes import resolve1

        d = resolve1(dest)
        if isinstance(d, dict):
            d = resolve1(d.get("D"))
        if isinstance(d, (bytes, str)):
            name = d.decode("latin-1") if isinstance(d, bytes) else d
            d = resolve1(pdf.doc.get_dest(name))
            if isinstance(d, dict):
                d = resolve1(d.get("D"))
        if isinstance(d, list) and d:
            ref = d[0]
            objid = getattr(ref, "objid", None)
            for i, p in enumerate(pdf.pages):
                if p.page_obj.pageid == objid:
                    return i
    except Exception:
        return None
    return None


def _is_unmapped(t: str) -> bool:
    return t == "" or t == "\ufffd" or (len(t) == 1 and ord(t) < 32 and t not in "\t\n\r ")


def _recover_unmapped_glyphs(page, tb, words: list["Word"], i: int, raster, ocr_lang: str,
                             warnings: list[str]) -> None:
    """Glifos sem mapeamento Unicode (texto vazio na camada do PDF) são lidos por OCR da região.

    O texto recuperado é unido à palavra vizinha quando encostado nela (ex.: ponto final) ou vira
    uma palavra própria (ex.: número de versículo "18."). Fica marcado como recuperado.
    """
    chars = [c for c in page.chars if _is_unmapped(c.get("text", "")) and c.get("upright", True)]
    if not chars:
        return
    boxes = []
    for c in chars:
        b = tb(c["x0"], c["top"], c["x1"], c["bottom"])
        boxes.append((b, float(c.get("size") or 10)))
    boxes.sort(key=lambda x: (round(x[0][1]), x[0][0]))
    runs: list[list] = []
    for b, sz in boxes:
        r = runs[-1] if runs else None
        if r and abs(b[1] - r[1]) < sz * 0.5 and 0 <= b[0] - r[2] < sz * 0.6:
            r[2], r[3] = max(r[2], b[2]), max(r[3], b[3])
        else:
            runs.append([b[0], b[1], b[2], b[3], sz])
    if not ocr_lang:
        warnings.append(f"Página {i + 1}: {len(runs)} trecho(s) com glifos sem Unicode; OCR indisponível para recuperar.")
        return
    img, dpi = raster()
    sc = dpi / 72.0
    recovered = lost = 0
    texts: list[str] = [""] * len(runs)
    pending: list[int] = []
    for k, (x0, y0, x1, y1, sz) in enumerate(runs):
        if (x1 - x0) < sz * 0.45:  # glifo único estreito: tenta pontuação pela forma (sem OCR)
            # a caixa do glifo pode invadir a letra anterior (kerning): começa após a palavra vizinha
            lx = max((wd.x1 for wd in words if wd.x0 < x0 and wd.x1 < x1
                      and min(wd.bottom, y1) - max(wd.top, y0) > 0), default=x0)
            texts[k] = _punct_by_shape(img, sc, max(x0, lx + 0.3), y0, x1, y1, sz)
        if not texts[k]:
            pending.append(k)
    if pending:
        crops = []
        for k in pending:
            x0, y0, x1, y1, sz = runs[k]
            pad = sz * 0.15
            crops.append(img.crop((max(0, int((x0 - pad) * sc)), max(0, int((y0 - pad) * sc)),
                                   min(img.width, int((x1 + pad) * sc) + 1), min(img.height, int((y1 + pad) * sc) + 1))))
        results = _ocr_mosaic(crops, ocr_lang)
        for n, k in enumerate(pending):  # reserva: OCR individual para o que o mosaico não leu
            if not results[n]:
                import pytesseract

                g = _normalize_crop(crops[n])
                if g.height < 48:
                    g = g.resize((max(1, int(g.width * 48 / max(1, g.height))), 48))
                results[n] = re.sub(r"\s+", "", pytesseract.image_to_string(g, lang=ocr_lang, config="--psm 7"))
        for k, txt in zip(pending, results):
            narrow = (runs[k][2] - runs[k][0]) < runs[k][4] * 0.45
            if narrow:
                txt = txt[:1] if txt[:1] in ".,;:!?)" else ""
            texts[k] = txt
    for (x0, y0, x1, y1, sz), txt in zip(runs, texts):
        narrow = (x1 - x0) < sz * 0.45
        if not txt:
            lost += 1
            continue
        recovered += 1
        line = [wd for wd in words if min(wd.bottom, y1) - max(wd.top, y0) > 0.5 * min(wd.bottom - wd.top, y1 - y0)]
        # glifos podem se sobrepor à palavra vizinha (kerning): compara pelos inícios/fins
        left = max((wd for wd in line if wd.x0 < x0 and wd.x1 <= x1), key=lambda wd: wd.x1, default=None)
        right = min((wd for wd in line if wd.x1 > x1 and wd.x0 >= x0), key=lambda wd: wd.x0, default=None)
        glue = sz * (0.35 if narrow else 0.12)
        if re.fullmatch(r"[.,;:!?)\]”’]+", txt):
            glue = sz * 1.0  # pontuação recuperada cola na palavra anterior
        if left is not None and x0 - left.x1 < glue:
            left.text += txt
            left.x1 = max(left.x1, x1)
            left.recovered = True
        elif right is not None and right.x0 - x1 < glue:
            right.text = txt + right.text
            right.x0 = min(right.x0, x0)
            right.recovered = True
        else:
            words.append(Word(txt, x0, y0, x1, y1, size=sz, font="unknown", recovered=True))
    if recovered:
        warnings.append(f"Página {i + 1}: {recovered} trecho(s) com glifos sem Unicode recuperados por OCR.")
    if lost:
        warnings.append(f"Página {i + 1}: {lost} trecho(s) com glifos sem Unicode não recuperados.")


def _normalize_crop(crop):
    """Texto escuro em fundo claro, contraste esticado (páginas de fundo escuro ficam invertidas)."""
    from PIL import ImageOps
    import numpy as np

    g = crop.convert("L")
    a = np.asarray(g)
    border = np.concatenate([a[0], a[-1], a[:, 0], a[:, -1]]) if a.size else np.array([255])
    if np.median(border) < 128:
        g = ImageOps.invert(g)
    return ImageOps.autocontrast(g)


def _ocr_mosaic(crops: list, lang: str, row_h: int = 48, gap: int = 28) -> list[str]:
    """OCR de vários recortes numa única chamada: um recorte por linha de um mosaico."""
    import pytesseract
    from PIL import Image

    rows = []
    for c in crops:
        g = _normalize_crop(c)
        f = row_h / max(1, g.height)
        rows.append(g.resize((max(1, int(g.width * f)), row_h)))
    width = max(r.width for r in rows) + 2 * gap
    canvas = Image.new("L", (width, gap + len(rows) * (row_h + gap)), 255)
    for k, r in enumerate(rows):
        canvas.paste(r, (gap, gap + k * (row_h + gap)))
    data = pytesseract.image_to_data(canvas, lang=lang, config="--psm 6", output_type=pytesseract.Output.DICT)
    out = [""] * len(rows)
    for t, top, h, conf in zip(data["text"], data["top"], data["height"], data["conf"]):
        t = (t or "").strip()
        if not t or float(conf) < 0:
            continue
        k = int((top + h / 2 - gap / 2) // (row_h + gap))
        if 0 <= k < len(out):
            out[k] += t
    return out


def _punct_by_shape(img, sc: float, x0, y0, x1, y1, sz) -> str:
    """Classifica pontuação isolada pela forma da tinta, usando a linha de base do texto à esquerda."""
    import numpy as np

    left = max(0.0, x0 - sz * 1.2)
    a = np.asarray(img.convert("L").crop((int(left * sc), int(y0 * sc), int(x1 * sc) + 1, int(y1 * sc) + 1)),
                   dtype=np.int16)
    if a.size == 0:
        return ""
    bg = np.median(np.concatenate([a[0], a[-1]]))
    m = np.abs(a - bg) > 60
    split = int((x0 - left) * sc)
    ref, glyph = m[:, :split], m[:, split:]
    rows_ref = ref.sum(1)
    if not glyph.any() or not rows_ref.any():
        return ""
    base = int(np.nonzero(rows_ref >= 0.3 * rows_ref.max())[0].max())  # linha de base aproximada
    rows = np.nonzero(glyph.any(1))[0]
    comps, start = [], rows[0]
    for r0, r1 in zip(rows, rows[1:]):
        if r1 - r0 > 1:
            comps.append((start, r0))
            start = r1
    comps.append((start, rows[-1]))
    tol = max(2, int(sz * sc * 0.06))
    descends = comps[-1][1] > base + tol
    height = rows[-1] - rows[0]
    if len(comps) == 1 and height < sz * sc * 0.35:
        return "," if descends else "."
    if len(comps) == 2 and comps[0][1] < base - tol:
        return ";" if descends else ":"
    return ""


def _smooth_bold(words: list["Word"]) -> None:
    """Palavras curtas entre/ao lado de palavras em negrito na mesma linha herdam o negrito."""
    rows: dict[int, list] = defaultdict(list)
    for wd in words:
        rows[round(wd.top / max(1.0, wd.size * 0.5))].append(wd)
    for ws in rows.values():
        ws.sort(key=lambda x: x.x0)
        for k, wd in enumerate(ws):
            if wd.bold or len(wd.text) > 3:
                continue
            prev_b = k > 0 and ws[k - 1].bold
            next_b = k + 1 < len(ws) and ws[k + 1].bold
            if (prev_b and next_b) or ((prev_b or next_b) and (k == 0 or k == len(ws) - 1)):
                wd.bold = True


def _detect_bold_by_ink(words: list["Word"], raster) -> None:
    """Sem nome de fonte, estima negrito pela espessura relativa do traço.

    Espessura ≈ pixels de tinta ÷ pixels de borda, normalizada pela altura da palavra: depende pouco
    do formato das letras (ao contrário da fração de tinta). Referência: mediana da página.
    """
    import numpy as np

    img, dpi = raster()
    sc = dpi / 72.0
    gray = img.convert("L")
    stroke: dict[int, float] = {}
    for k, wd in enumerate(words):
        if len(wd.text) < 2:
            continue
        a = np.asarray(gray.crop((int(wd.x0 * sc), int(wd.top * sc), max(int(wd.x0 * sc) + 2, int(wd.x1 * sc) + 1),
                                  max(int(wd.top * sc) + 2, int(wd.bottom * sc) + 1))), dtype=np.int16)
        if a.shape[0] < 6 or a.size < 40:
            continue
        bg = np.median(np.concatenate([a[0], a[-1], a[:, 0], a[:, -1]]))
        m = np.abs(a - bg) > 60
        inner = m.copy()
        inner[1:] &= m[:-1]
        inner[:-1] &= m[1:]
        inner[:, 1:] &= m[:, :-1]
        inner[:, :-1] &= m[:, 1:]
        edge = int((m & ~inner).sum())
        if edge:
            stroke[k] = float(m.sum()) / edge / a.shape[0]
    if len(stroke) >= 6:
        med = statistics.median(stroke.values())
        for k, v in stroke.items():
            words[k].bold = v > med * 1.25
    for wd in words:
        if wd.bold is None:
            wd.bold = False
    _smooth_bold(words)


def read_page(pdf, i: int, pdfium_doc, assets_dir: Path, ocr_lang: str, ocr_dpi: int,
              warnings: list[str]) -> PageData:
    page = pdf.pages[i]
    pp = pdfium_doc[i]
    rotation = pp.get_rotation()
    mediabox = tuple(pp.get_mediabox())
    cropbox = tuple(pp.get_cropbox())
    pp.close()
    fn, w, h = render.display_transform(mediabox, cropbox, rotation)

    def tb(x0, top, x1, bottom):
        a = fn(x0, top)
        b = fn(x1, bottom)
        return (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))

    words: list[Word] = []
    rotated: list[Word] = []
    unmapped = [0]
    try:
        raw = page.extract_words(extra_attrs=["fontname", "size", "upright", "non_stroking_color"],
                                 x_tolerance=1.5, y_tolerance=2, keep_blank_chars=False)
    except Exception as exc:  # pragma: no cover - PDFs malformados
        warnings.append(f"Página {i + 1}: falha na extração nativa ({exc}).")
        raw = []
    for r in raw:
        r["text"] = re.sub(r"\s+", " ", r["text"]).strip()
        if not r["text"]:
            continue
        if "(cid:" in r["text"]:
            r["text"] = CID_RE.sub(lambda m: "•" if m.group(1) in ("127", "149", "183") else "\ufffd", r["text"])
            if "\ufffd" in r["text"]:
                unmapped[0] += 1
        b = tb(r["x0"], r["top"], r["x1"], r["bottom"])
        wd = Word(r["text"], *b, size=float(r.get("size") or 10), font=_clean_font(r.get("fontname", "")),
                  color=_color(r.get("non_stroking_color")), upright=bool(r.get("upright", True)))
        if not (-2 <= b[0] <= w + 2 and -2 <= b[1] <= h + 2):
            continue  # fora da CropBox
        (words if wd.upright else rotated).append(wd)

    if unmapped[0]:
        warnings.append(f"Página {i + 1}: {unmapped[0]} palavra(s) com glifos sem mapeamento Unicode (\ufffd).")
    page_raster = {}

    def raster():
        if "img" not in page_raster:
            dpi = min(150.0, 3000 * 72.0 / max(w, h, 1))
            page_raster["dpi"] = dpi
            page_raster["img"] = render.render_page(None, i, dpi=dpi, doc=pdfium_doc)
        return page_raster["img"], page_raster["dpi"]

    try:
        _recover_unmapped_glyphs(page, tb, words, i, raster, ocr_lang, warnings)
    except Exception as exc:  # pragma: no cover - recuperação é melhor esforço
        warnings.append(f"Página {i + 1}: falha ao recuperar glifos sem Unicode ({type(exc).__name__}).")
    if words and sum(1 for x in words if x.font.lower() in ("", "unknown")) >= 0.5 * len(words):
        _detect_bold_by_ink(words, raster)
    images = []
    for im in page.images:
        b = tb(im["x0"], im["top"], im["x1"], im["bottom"])
        b = (max(0, b[0]), max(0, b[1]), min(w, b[2]), min(h, b[3]))
        if b[2] - b[0] > 1 and b[3] - b[1] > 1:
            images.append(b)

    # Tabelas por linhas desenhadas (estratégia padrão do pdfplumber)
    tables = []
    try:
        for t in page.find_tables():
            rows = t.extract()
            if not rows or len(rows) < 2 or max(len(r) for r in rows) < 2:
                continue
            tables.append({"bbox": tb(*t.bbox), "rows": rows})
    except Exception as exc:  # pragma: no cover
        warnings.append(f"Página {i + 1}: detecção de tabelas falhou ({exc}).")

    # Regiões vetoriais (diagramas/gráficos): agrupamento de curvas e retângulos
    vec = [tb(o["x0"], o["top"], o["x1"], o["bottom"]) for o in page.curves]
    vec += [tb(o["x0"], o["top"], o["x1"], o["bottom"]) for o in page.rects
            if (o["x1"] - o["x0"]) < w * 0.95]
    vector_regions = _cluster_boxes([v for v in vec if not any(_overlap(v, t["bbox"]) > 0.5 for t in tables)],
                                    gap=12)
    vector_regions = [r for r in vector_regions
                      if (r[2] - r[0]) * (r[3] - r[1]) > w * h * 0.02 and (r[2] - r[0]) > 40 and (r[3] - r[1]) > 30]

    links = []
    for hl in page.hyperlinks:
        links.append({"bbox": tb(hl["x0"], hl["top"], hl["x1"], hl["bottom"]), "href": hl["uri"]})
    for an in page.annots or []:
        data = an.get("data") or {}
        if an.get("uri"):
            continue
        dest = data.get("Dest")
        if dest is None:
            a = data.get("A")
            try:
                from pdfminer.pdftypes import resolve1

                a = resolve1(a)
                if isinstance(a, dict) and str(a.get("S")) in ("/'GoTo'", "/GoTo") or (
                        isinstance(a, dict) and a.get("D") is not None):
                    dest = a.get("D")
            except Exception:
                dest = None
        if dest is not None:
            idx = _resolve_dest(pdf, dest)
            if idx is not None:
                links.append({"bbox": tb(an["x0"], an["top"], an["x1"], an["bottom"]), "href": f"#page-{idx}"})

    for wd in words:
        cx, cy = (wd.x0 + wd.x1) / 2, (wd.top + wd.bottom) / 2
        for ln in links:
            b = ln["bbox"]
            if b[0] - 1 <= cx <= b[2] + 1 and b[1] - 1 <= cy <= b[3] + 1:
                wd.href = ln["href"]

    text_chars = sum(len(x.text) for x in words)
    big_image = any((b[2] - b[0]) * (b[3] - b[1]) > w * h * 0.5 for b in images)
    kind = "digital"
    ocr_conf = None
    used_lang = None
    if text_chars < 20 and (big_image or not words):
        if ocr_lang:
            img = render.render_page(None, i, dpi=ocr_dpi, doc=pdfium_doc)
            ocr_words, ocr_conf = _ocr_page(img, ocr_dpi, ocr_lang)
            used_lang = ocr_lang
            if ocr_words:
                words = ocr_words
                kind = "scanned"
                # a imagem da página digitalizada não é uma figura editorial
                images = [b for b in images if (b[2] - b[0]) * (b[3] - b[1]) < w * h * 0.5]
            else:
                kind = "empty" if not images else "scanned"
        else:
            kind = "scanned" if big_image else "empty"
            warnings.append(f"Página {i + 1}: sem texto nativo e OCR indisponível.")
    elif big_image and text_chars >= 20:
        kind = "mixed"

    info = PageInfo(index=i, width=round(w, 3), height=round(h, 3), rotation=rotation,
                    mediabox=mediabox, cropbox=cropbox, kind=kind,
                    ocr_confidence=round(ocr_conf, 3) if ocr_conf is not None else None,
                    ocr_lang=used_lang)
    return PageData(info, words, rotated, images, tables, vector_regions, links)


def _cluster_boxes(boxes, gap=10.0):
    clusters: list[list[float]] = []
    for b in sorted(boxes, key=lambda b: (b[1], b[0])):
        merged = False
        for c in clusters:
            if b[0] <= c[2] + gap and b[2] >= c[0] - gap and b[1] <= c[3] + gap and b[3] >= c[1] - gap:
                c[0], c[1], c[2], c[3] = min(c[0], b[0]), min(c[1], b[1]), max(c[2], b[2]), max(c[3], b[3])
                merged = True
                break
        if not merged:
            clusters.append(list(b))
    # segunda passada para fundir clusters que passaram a se tocar
    changed = True
    while changed:
        changed = False
        for a in clusters:
            for b in clusters:
                if a is not b and b[0] <= a[2] + gap and b[2] >= a[0] - gap and b[1] <= a[3] + gap and b[3] >= a[1] - gap:
                    a[0], a[1], a[2], a[3] = min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])
                    clusters.remove(b)
                    changed = True
                    break
            if changed:
                break
    return [tuple(c) for c in clusters]


def available_cpus() -> int:
    """CPUs disponíveis respeitando o limite do contêiner (cgroup v2 cpu.max)."""
    n = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else (os.cpu_count() or 1)
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()[:2]
        if quota != "max":
            n = min(n, max(1, int(int(quota) / int(period))))
    except (OSError, ValueError):
        pass
    return max(1, n)


def extract_workers(n_pages: int) -> int:
    env = os.environ.get("EXTRACT_WORKERS")
    want = int(env) if env and env.isdigit() else min(4, available_cpus())
    return max(1, min(want, n_pages // 4 or 1))


_W: dict = {}


def _worker_init(pdf_path: str, password: str | None):
    fastpdf.install()
    _W["pdf"] = pdfplumber.open(pdf_path, password=password or "")
    _W["pdfium"] = pdfium.PdfDocument(pdf_path, password=password or None)


def _worker_read(i: int, assets_dir: str, ocr_lang: str, ocr_dpi: int):
    w: list[str] = []
    pd = read_page(_W["pdf"], i, _W["pdfium"], Path(assets_dir), ocr_lang, ocr_dpi, w)
    _W["pdf"].pages[i].close()  # libera o cache de objetos da página
    return i, pd, w


def _read_pages_parallel(pdf_path, password, indices, n, nworkers, assets_dir, ocr_lang, ocr_dpi, warnings,
                         progress, cancelled) -> list[PageData]:
    """Lê páginas em processos separados (o interpretador do pdfminer é Python puro e usa 1 CPU)."""
    import multiprocessing as mp
    from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait

    results: dict[int, tuple] = {}
    with ProcessPoolExecutor(nworkers, mp_context=mp.get_context("spawn"), initializer=_worker_init,
                             initargs=(str(pdf_path), password)) as ex:
        futs = {ex.submit(_worker_read, i, str(assets_dir), ocr_lang, ocr_dpi) for i in indices}
        while futs:
            done, futs = wait(futs, timeout=1.0, return_when=FIRST_COMPLETED)
            for f in done:
                i, pd, w = f.result()
                results[i] = (pd, w)
            if cancelled and cancelled():
                for f in futs:
                    f.cancel()
                ex.shutdown(wait=False, cancel_futures=True)
                raise InterruptedError("cancelado")
            if progress and done:
                progress(0.7 * len(results) / len(indices), f"{len(results)}/{len(indices)} páginas lidas "
                                                              f"({nworkers} processos)")
    pages = []
    for i in indices:
        pd, w = results[i]
        warnings.extend(w)
        pages.append(pd)
    return pages


def extract_document(pdf_path: Path, assets_dir: Path, *, password: str | None = None,
                     ocr_languages: str = "por+eng", ocr_dpi: int = 300,
                     progress: ProgressFn | None = None, cancelled: CancelFn | None = None,
                     page_range: tuple[int, int] | None = None) -> Document:
    assets_dir.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    ocr_lang, missing = available_ocr_langs(ocr_languages)
    if missing:
        warnings.append("Idiomas de OCR indisponíveis no Tesseract: " + ", ".join(missing))

    pdfium_doc = pdfium.PdfDocument(str(pdf_path), password=password or None)
    pdf = pdfplumber.open(str(pdf_path), password=password or "")
    try:
        n = len(pdf.pages)
        rng = range(*(page_range or (0, n)))
        pages: list[PageData] = []
        nworkers = extract_workers(len(rng))
        if nworkers > 1:
            pages = _read_pages_parallel(pdf_path, password, list(rng), n, nworkers, assets_dir, ocr_lang,
                                         ocr_dpi, warnings, progress, cancelled)
        else:
            for k, i in enumerate(rng):
                if cancelled and cancelled():
                    raise InterruptedError("cancelado")
                pages.append(read_page(pdf, i, pdfium_doc, assets_dir, ocr_lang, ocr_dpi, warnings))
                if progress:
                    progress(0.7 * (k + 1) / len(rng), f"Página {i + 1}/{n} lida")
        outline = []
        try:
            for it in pdfium_doc.get_toc():
                dest = it.get_dest()
                idx = dest.get_index() if dest is not None else None
                outline.append({"title": it.get_title(), "level": it.level + 1, "page": idx})
        except Exception:
            warnings.append("Não foi possível ler os marcadores (outline) do PDF.")
        meta = pdfium_doc.get_metadata_dict(skip_empty=True)
        doc = build_document(pages, outline, meta, pdf_path, pdfium_doc, assets_dir, warnings,
                             progress=progress)
        return doc
    finally:
        pdf.close()
        pdfium_doc.close()


# --------------------------------------------------------------------------- classificação

def build_document(pages: list[PageData], outline, meta, pdf_path, pdfium_doc, assets_dir: Path,
                   warnings: list[str], progress: ProgressFn | None = None) -> Document:
    size_counter: Counter = Counter()
    for p in pages:
        for w in p.words:
            size_counter[round(w.size * 2) / 2] += len(w.text)
    body_size = size_counter.most_common(1)[0][0] if size_counter else 10.0

    # Blocos por página
    page_blocks: dict[int, list[Block]] = {}
    for p in pages:
        info = p.info
        table_boxes = [t["bbox"] for t in p.tables]
        words = [w for w in p.words if not any(_overlap(w.bbox, tbx) > 0.6 for tbx in table_boxes)]
        lines = group_lines(words)
        gutters = find_gutters(lines, info.width, info.height * 0.08, info.height * 0.92)
        lines = split_columns(lines, gutters)
        lines = reading_order(lines)
        blocks = make_blocks(info.index, lines, body_size)
        method = "ocr" if info.kind == "scanned" else "native"
        for b in blocks:
            b.method = method
        if gutters:
            for b in blocks:
                b.extra["columns"] = len(gutters) + 1
        page_blocks[info.index] = blocks

    # Cabeçalhos/rodapés recorrentes: posição + padrão (não apenas repetição)
    n_pages = len(pages)
    pattern_pages: dict[tuple, set[int]] = defaultdict(set)
    for p in pages:
        h = p.info.height
        for b in page_blocks[p.info.index]:
            bb = b.bbox
            zone = "top" if bb[3] < h * 0.12 else "bottom" if bb[1] > h * 0.88 else None
            if zone:
                txt = " ".join(w.text for w in b.words())
                pattern_pages[(zone, normalize_pattern(txt))].add(p.info.index)
                b.extra["zone"] = zone
    min_repeat = max(2, int(n_pages * 0.25)) if n_pages >= 3 else 99

    heading_sizes = sorted({round(b.size * 2) / 2 for bl in page_blocks.values() for b in bl
                            if b.size >= body_size * 1.15}, reverse=True)
    outline_titles = {normalize_pattern(o["title"]): o for o in outline if o.get("title")}
    known_hyph = set()
    for p in pages:
        for w in p.words:
            if "-" in w.text.strip("-") and not w.text.endswith("-"):
                known_hyph.add(re.sub(r"\W+$", "", w.text.lower()))

    elements: list[Element] = []
    seq = Counter()

    def new_id(page):
        seq[page] += 1
        return f"p{page + 1:04d}e{seq[page]:03d}"

    toc_pages = set()
    for p in pages:
        bl = page_blocks[p.info.index]
        def is_margin(b):
            zone = b.extra.get("zone")
            if not zone:
                return False
            txt = " ".join(w.text for w in b.words())
            return bool(PAGE_NUM_RE.match(txt)) or len(pattern_pages.get((zone, normalize_pattern(txt)), ())) >= min_repeat

        entries = detect_toc_entries(bl, p.info.width, is_margin)
        lines_txt = [" ".join(w.text for w in l.words) for b in bl for l in b.lines]
        if entries is not None:
            page_blocks[p.info.index] = entries
            toc_pages.add(p.info.index)
        elif any(TOC_HEAD_RE.match(t) for t in lines_txt[:5]) and \
                sum(1 for t in lines_txt if TOC_LINE_RE.match(t)) >= 3:
            toc_pages.add(p.info.index)

    page_els: dict[int, list[Element]] = defaultdict(list)
    para_stats = {"n": 0, "indent": 0, "justified": 0, "multi": 0}
    for p in pages:
        info = p.info
        i = info.index
        h, w = info.height, info.width
        img_dpi = crop_dpi(w, h)
        page_img = None

        def get_img():
            nonlocal page_img
            if page_img is None:
                page_img = render.render_page(pdf_path, i, dpi=img_dpi, doc=pdfium_doc)
            return page_img

        # Imagens
        visual: list[Element] = []
        covered_by_visual: list[tuple] = []
        for k, bb in enumerate(_cluster_boxes(p.images, gap=2)):
            area = (bb[2] - bb[0]) * (bb[3] - bb[1])
            eid = new_id(i)
            if (bb[2] - bb[0]) < 16 or (bb[3] - bb[1]) < 16:
                el = Element(id=eid, type="decoration", included=False,
                             sources=[SourceRegion(page=i, bbox=bb)],
                             extraction=Extraction(confidence=0.6, evidence=["imagem muito pequena"]))
            elif i == 0 and area > w * h * 0.55:
                el = Element(id=eid, type="cover", sources=[SourceRegion(page=i, bbox=bb)],
                             extraction=Extraction(confidence=0.6, evidence=["imagem grande na primeira página"]),
                             review=["capa detectada por heurística"])
            else:
                el = Element(id=eid, type="image", sources=[SourceRegion(page=i, bbox=bb)],
                             extraction=Extraction(confidence=0.8, evidence=["objeto de imagem do PDF"]),
                             review=["texto alternativo ausente"])
                if area > w * h * 0.5 and info.kind == "mixed":
                    el.review.append("imagem de página inteira em página com texto: verifique sobreposição")
            visual.append(el)
            covered_by_visual.append(bb)
        for bb in p.vector_regions:
            if any(_overlap(bb, c) > 0.5 for c in covered_by_visual):
                continue
            el = Element(id=new_id(i), type="figure", subtype="vector",
                         sources=[SourceRegion(page=i, bbox=bb)],
                         extraction=Extraction(confidence=0.4, evidence=["agrupamento de objetos vetoriais"]),
                         review=["figura vetorial detectada por heurística: confirme o recorte",
                                 "texto alternativo ausente"])
            visual.append(el)
            covered_by_visual.append(bb)

        # Tabelas
        tables_els: list[Element] = []
        for t in p.tables:
            rows = t["rows"]
            cells: list[list[TableCell]] = []
            ncols = max(len(r) for r in rows)
            for ri, r in enumerate(rows):
                row: list[TableCell] = []
                ci = 0
                while ci < len(r):
                    txt = r[ci]
                    span = 1
                    while ci + span < len(r) and r[ci + span] is None:
                        span += 1
                    if txt is not None:
                        row.append(TableCell(text=re.sub(r"\s+", " ", txt).strip(), header=(ri == 0), colspan=span))
                    ci += span
                cells.append(row)
            review = []
            if any(c is None for r in rows for c in r):
                review.append("tabela com células mescladas: confira a estrutura")
            el = Element(id=new_id(i), type="table", table=cells, sources=[SourceRegion(page=i, bbox=t["bbox"])],
                         extraction=Extraction(confidence=0.7, evidence=[f"tabela por linhas ({len(rows)}x{ncols})"]),
                         review=review + ["cabeçalho da tabela assumido na primeira linha"])
            tables_els.append(el)

        # Blocos de texto
        text_els: list[Element] = []
        for b in page_blocks[i]:
            bb = b.bbox
            # texto dentro de figura vetorial → rótulo da figura (fica no recorte)
            inside = [v for v in visual if v.type in ("figure",) and _overlap(bb, v.sources[0].bbox) > 0.8]
            text, marks, evid = assemble_text(b.lines, body_size, known_hyph)
            if not text:
                continue
            fonts = Counter(w.font for w in b.words())
            font = fonts.most_common(1)[0][0] if fonts else None
            bold = sum(1 for wd in b.words() if _wbold(wd)) > 0.7 * len(b.words())
            italic = sum(1 for wd in b.words() if _is_italic(wd.font)) > 0.7 * len(b.words())
            colors = Counter(wd.color for wd in b.words())
            el = Element(id=new_id(i), type="paragraph", text=text, text_original=text, marks=marks,
                         sources=[SourceRegion(page=i, bbox=bb)],
                         observed_style=ObservedStyle(font=font, size=round(b.size, 2), bold=bold, italic=italic,
                                                      color=colors.most_common(1)[0][0] if colors else None),
                         extraction=Extraction(method=b.method, evidence=evid))
            if b.method == "ocr":
                confs = [wd.conf for wd in b.words() if wd.conf is not None]
                el.extraction.confidence = round(statistics.mean(confs), 3) if confs else None
                if confs and statistics.mean(confs) < 0.75:
                    el.review.append("OCR com baixa confiança: revise o texto")
            if b.extra.get("columns"):
                el.extraction.evidence.append(f"página em {b.extra['columns']} colunas")
            if inside:
                el.parent_id = inside[0].id
                el.subtype = "figure-label"
                el.included = False
                el.extraction.evidence.append("texto dentro de figura (preservado no recorte)")
            _classify(el, b, info, body_size, heading_sizes, outline_titles, pattern_pages,
                      min_repeat, i in toc_pages)
            n_rec = sum(1 for wd in b.words() if wd.recovered)
            if n_rec:
                el.extraction.evidence.append(f"{n_rec} palavra(s) com glifos sem Unicode recuperadas por OCR")
                el.review.append("texto parcialmente recuperado por OCR (glifos sem Unicode no PDF): confira")
            # aparência observada no PDF vira estilo de saída inicial (editável)
            if b.extra.get("align"):
                el.observed_style.align = b.extra["align"]
                if el.type in ("paragraph", "caption", "blockquote", "heading"):
                    el.output_style["align"] = b.extra["align"]
            if el.observed_style.bold:
                el.marks = [m for m in el.marks if m.kind != "bold"]
            if el.observed_style.italic:
                el.marks = [m for m in el.marks if m.kind != "italic"]
            if el.type in ("paragraph", "list_item", "blockquote", "caption"):
                if el.observed_style.bold and el.observed_style.italic:
                    el.output_style["emphasis"] = "bold-italic"
                elif el.observed_style.bold:
                    el.output_style["emphasis"] = "bold"
                elif el.observed_style.italic:
                    el.output_style["emphasis"] = "italic"
            if el.type == "paragraph" and len(b.lines) >= 2:
                _para_stats(b, para_stats)
            text_els.append(el)

        # Rótulos de figuras vetoriais com muito texto: a figura provavelmente é texto
        for v in visual:
            labels = [t for t in text_els if t.parent_id == v.id]
            if v.subtype == "vector" and sum(len(t.text or "") for t in labels) > 600:
                for t in labels:
                    t.parent_id, t.included, t.subtype = None, True, None
                v.included = False
                v.review.append("região vetorial contém muito texto; mantida como texto (fundo/caixa)")
                v.type = "decoration"

        # Texto rotacionado (marca d'água ou rótulos laterais)
        if p.rotated_words:
            txt = " ".join(wd.text for wd in sorted(p.rotated_words, key=lambda x: (x.x0, x.top)))
            bb = _union([wd.bbox for wd in p.rotated_words])
            big = statistics.median([wd.size for wd in p.rotated_words]) > body_size * 2
            el = Element(id=new_id(i), type="watermark" if big else "paragraph", text=txt, text_original=txt,
                         sources=[SourceRegion(page=i, bbox=bb)], included=not big,
                         extraction=Extraction(confidence=0.3, evidence=["texto não horizontal"]),
                         review=["texto rotacionado: confirme ordem e inclusão"])
            text_els.append(el)

        # Legendas: vincula à figura/tabela mais próxima verticalmente
        targets = visual + tables_els
        for c in [t for t in text_els if t.type == "caption"]:
            cb = c.sources[0].bbox
            best, bestd = None, 1e9
            for tg in targets:
                tbx = tg.sources[0].bbox
                horiz = min(cb[2], tbx[2]) - max(cb[0], tbx[0])
                if horiz < -20:
                    continue
                d = min(abs(cb[1] - tbx[3]), abs(tbx[1] - cb[3]))
                if d < bestd and not tg.caption_id:
                    best, bestd = tg, d
            if best is not None and bestd < 80:
                best.caption_id = c.id
                c.depends_on.append(best.id)
            else:
                c.review.append("legenda sem figura/tabela associada")

        # Ordem de leitura da página: visuais/tabelas inseridos pela posição vertical na coluna
        ordered = list(text_els)
        for v in targets:
            vb = v.sources[0].bbox
            pos = len(ordered)
            for k, t in enumerate(ordered):
                tb_ = t.sources[0].bbox
                if t.type in ("header", "page_number") and tb_[3] < h * 0.12:
                    continue
                same_col = not (tb_[2] < vb[0] or tb_[0] > vb[2])
                if same_col and tb_[1] >= vb[1] - 2:
                    pos = k
                    break
                if t.type in ("footer", "footnote", "page_number") and tb_[1] > vb[3]:
                    pos = k
                    break
            ordered.insert(pos, v)
        # legenda logo após a figura quando abaixo dela; rótulos logo após a figura
        for v in targets:
            if v.caption_id:
                cap = next((t for t in ordered if t.id == v.caption_id), None)
                if cap:
                    ordered.remove(cap)
                    vi = ordered.index(v)
                    above = cap.sources[0].bbox[3] <= v.sources[0].bbox[1] + 2
                    ordered.insert(vi if above else vi + 1, cap)
            kids = [t for t in ordered if t.parent_id == v.id]
            for kid in kids:
                ordered.remove(kid)
            vi = ordered.index(v)
            ordered[vi + 1:vi + 1] = kids

        # Assets dos visuais (recorte da página renderizada, sem máscaras na extração)
        for v in visual + [t for t in ordered if t.type == "formula"]:
            if v.type in ("decoration",):
                continue
            img = render.crop_region(get_img(), v.sources[0].bbox, img_dpi)
            v.asset = render.save_crop(img, assets_dir / v.id).name
        page_els[i] = ordered
        if progress:
            progress(0.7 + 0.25 * (i + 1) / max(1, len(pages)), f"Página {i + 1} classificada")

    # Notas de rodapé: liga chamadas sobrescritas às notas da mesma página
    for i, els in page_els.items():
        notes = {e.note_id: e for e in els if e.type == "footnote" and e.note_id}
        for e in els:
            if e.type in ("footnote",) or e.text is None:
                continue
            for m in e.marks:
                if m.kind == "sup":
                    label = e.text[m.start:m.end].strip()
                    if label in notes:
                        m.kind = "noteref"
                        m.href = notes[label].id
                        notes[label].depends_on.append(e.id)
        for nt in notes.values():
            if not nt.depends_on:
                nt.review.append("nota sem chamada identificada no texto")

    for i in sorted(page_els):
        elements.extend(page_els[i])

    _merge_across_pages(elements)
    _assign_heading_levels(elements, heading_sizes, outline_titles)
    link_toc_entries(elements)

    infos = [p.info for p in pages]
    kinds = {p.kind for p in infos}
    doc_kind = "mixed" if len(kinds - {"empty"}) > 1 or "mixed" in kinds else (next(iter(kinds - {"empty"}), "empty"))
    meta_title = ((meta or {}).get("Title") or "").strip()
    if meta_title.lower() in ("untitled", "sem título", "document"):
        meta_title = ""
    title = meta_title or next((e.text for e in elements if e.type == "heading"), None) \
        or Path(pdf_path).stem
    authors = [(meta or {}).get("Author")] if (meta or {}).get("Author") else []
    lang = "pt-BR"
    sample = " ".join((e.text or "") for e in elements[:200]).lower()
    if sample and sum(sample.count(x) for x in (" the ", " and ", " of ")) > \
            sum(sample.count(x) for x in (" de ", " que ", " e ", " do ", " da ")):
        lang = "en"
    doc = Document(metadata=Metadata(title=title[:300], authors=authors, language=lang),
                   pages=infos, elements=elements, outline=outline, doc_kind=doc_kind, warnings=warnings)
    gstyle, gnote = infer_global_style(para_stats)
    doc.global_style.update(gstyle)
    if gnote:
        doc.warnings.append(gnote)
    if not any(e.chapter_break for e in doc.elements):
        first = next((e for e in doc.elements if e.type == "heading" and e.included), None)
        if first:
            first.chapter_break = True
    return doc


def _classify(el: Element, b: Block, info: PageInfo, body: float, heading_sizes, outline_titles,
              pattern_pages, min_repeat, is_toc_page):
    text = el.text or ""
    h = info.height
    bb = b.bbox
    ev = el.extraction.evidence
    zone = b.extra.get("zone")
    pat = normalize_pattern(text)
    if zone and PAGE_NUM_RE.match(text):
        el.type, el.included = "page_number", False
        el.extraction.confidence = 0.9
        ev.append(f"número na margem {'superior' if zone == 'top' else 'inferior'}")
        return
    if zone and len(text) < 160 and b.size <= body * 1.15:
        reps = len(pattern_pages.get((zone, pat), ()))
        if reps >= min_repeat:
            el.type = "header" if zone == "top" else "footer"
            el.included = False
            el.extraction.confidence = min(0.95, 0.5 + reps / 20)
            ev.append(f"padrão repetido em {reps} páginas na margem")
            return
        ev.append("na margem, mas sem repetição suficiente")
    if is_toc_page:
        el.type, el.included = "toc", False
        el.extraction.confidence = 0.6
        ev.append("página de sumário do PDF")
        label = b.extra.get("toc_page")
        if label:
            # entrada reconhecida: mantida como lista de links para os títulos (sem nº de página)
            el.subtype, el.included = "toc-entry", True
            ev.append(f"entrada de sumário → página {label} do PDF")
            if b.extra.get("toc_first"):
                el.review.append("sumário do PDF mantido como lista de links (números de página removidos); "
                                 "o EPUB também tem sumário navegável próprio")
        else:
            el.review.append("linha de sumário não reconhecida: excluída (o EPUB gera sumário navegável)")
        return
    # Notas: fonte menor, terço inferior, começa com marcador
    m = re.match(r"^\s*([\d*†‡§¹²³⁴⁵⁶⁷⁸⁹]{1,3})[.)]?\s+\S", text)
    first_sup = el.marks and el.marks[0].kind == "sup" and el.marks[0].start == 0
    if (m or first_sup) and bb[1] > h * 0.6 and b.size < body * 0.92:
        el.type = "footnote"
        label = text[el.marks[0].start:el.marks[0].end] if first_sup else m.group(1)
        el.note_id = label.strip()
        el.extraction.confidence = 0.6
        ev.append("fonte menor no terço inferior iniciando com marcador")
        return
    if CAPTION_RE.match(text) and len(text) < 600:
        el.type = "caption"
        el.extraction.confidence = 0.7
        ev.append("padrão de legenda")
        return
    math_ratio = sum(1 for ch in text if ch in MATH_CHARS) / max(1, len(text))
    math_font = sum(1 for wd in b.words() if MATH_FONT_RE.search(wd.font)) / max(1, len(b.words()))
    n_math = sum(1 for ch in text if ch in MATH_CHARS)
    if len(text.strip()) >= 3 and ((math_ratio > 0.12 and n_math >= 2 and len(text) < 300) or math_font > 0.5):
        el.type = "formula"
        el.extraction.confidence = 0.4
        ev.append(f"símbolos matemáticos {math_ratio:.0%}, fontes matemáticas {math_font:.0%}")
        el.review.append("fórmula preservada como recorte de imagem; MathML não gerado (transcrição não verificada)")
        return
    if "\ufffd" in text:
        el.review.append("texto com glifos sem mapeamento Unicode: revise")
    out = outline_titles.get(pat)
    words_n = len(text.split())
    alnum = sum(1 for ch in text if ch.isalnum())
    if alnum == 0 and len(text.strip()) <= 3:
        el.type, el.included = "decoration", False
        el.extraction.confidence = 0.5
        ev.append("símbolo isolado, sem letras ou números")
        el.review.append("símbolo isolado excluído: inclua se fizer parte do conteúdo")
        return
    max_lines = 8 if b.size >= body * 1.5 else 4
    if out or (b.size >= body * 1.15 and len(text) < 300 and len(b.lines) <= max_lines and alnum >= 2):
        el.type = "heading"
        el.extraction.confidence = 0.85 if out else 0.65
        ev.append("corresponde a marcador do PDF" if out else f"fonte {b.size:.1f}pt > corpo {body:.1f}pt")
        if out:
            el.level = min(6, out["level"])
            el.subtype = "outline"
        return
    if el.observed_style.bold and words_n <= 14 and not TERMINAL_RE.search(text.rstrip(":")) and len(b.lines) <= 2:
        el.type = "heading"
        el.extraction.confidence = 0.45
        ev.append("bloco curto em negrito")
        el.review.append("classificação incerta: título inferido apenas por negrito")
        return
    if BULLET_RE.match(text):
        el.type = "list_item"
        el.level = 1
        el.extraction.confidence = 0.7
        ev.append("inicia com marcador de lista")
        return
    el.extraction.confidence = 0.7
    ev.append("bloco de texto corrido")


def _merge_across_pages(elements: list[Element]) -> None:
    """Une parágrafos quebrados entre páginas/colunas (mantendo proveniência)."""
    skip = {"header", "footer", "page_number", "footnote", "decoration", "watermark"}
    i = 0
    while i < len(elements):
        a = elements[i]
        if a.type != "paragraph" or not a.text or not a.included:
            i += 1
            continue
        j = i + 1
        while j < len(elements) and (elements[j].type in skip or not elements[j].included):
            j += 1
        if j >= len(elements):
            break
        b = elements[j]
        cross = b.sources and a.sources and b.sources[0].page != a.sources[-1].page
        if (b.type == "paragraph" and b.text and cross and not TERMINAL_RE.search(a.text)
                and re.match(r"^[a-zà-ÿ(“\"]", b.text)):
            joiner = ""
            text = a.text
            if text.endswith("-") and re.match(r"^[a-zà-ÿ]", b.text):
                text = text[:-1]
                a.extraction.evidence.append("hifenização desfeita entre páginas")
            else:
                joiner = " "
            base = len(text) + len(joiner)
            a.marks += [InlineMark(start=m.start + base, end=m.end + base, kind=m.kind, href=m.href) for m in b.marks]
            a.text = text + joiner + b.text
            a.text_original = (a.text_original or "") + " " + (b.text_original or "")
            a.sources += b.sources
            a.provenance.append(b.id)
            a.extraction.evidence.append(f"parágrafo continua na página {b.sources[0].page + 1}")
            for x in elements:
                if b.id in x.depends_on:
                    x.depends_on = [a.id if d == b.id else d for d in x.depends_on]
            elements.pop(j)
            continue
        i += 1


def _para_stats(b: "Block", st: dict) -> None:
    lines = b.lines
    st["n"] += 1
    rest_x0 = min(l.x0 for l in lines[1:])
    if lines[0].x0 - rest_x0 > lines[0].size * 0.8:
        st["indent"] += 1
    if len(lines) >= 3:
        st["multi"] += 1
        right = max(l.x1 for l in lines)
        body = lines[:-1]
        if sum(1 for l in body if right - l.x1 <= l.size * 0.5) >= 0.8 * len(body):
            st["justified"] += 1


def infer_global_style(st: dict) -> tuple[dict, str | None]:
    """Estilo global inicial a partir dos parágrafos do PDF (recuo, alinhamento, espaçamento)."""
    if st["n"] < 3:
        return {}, None
    indent = st["indent"] / st["n"] > 0.5
    justified = st["multi"] >= 2 and st["justified"] / st["multi"] > 0.6
    style = {"text_indent_em": 1.2 if indent else 0.0, "text_align": "justify" if justified else "left",
             "paragraph_spacing_em": 0.0 if indent else 0.8}
    note = (f"Estilo inferido do PDF: parágrafos {'com' if indent else 'sem'} recuo, "
            f"{'justificados' if justified else 'alinhados à esquerda'} "
            f"({st['n']} parágrafos analisados).")
    return style, note


def cluster_sizes(sizes: list[float], ratio: float = 1.12) -> list[list[float]]:
    """Agrupa tamanhos de fonte próximos (títulos com ajuste automático de tamanho)."""
    out: list[list[float]] = []
    for sz in sorted(set(sizes), reverse=True):
        if out and out[-1][-1] / max(sz, 0.1) < ratio:
            out[-1].append(sz)
        else:
            out.append([sz])
    return out


def _sz(e) -> float:
    return round((e.observed_style.size or 0) * 2) / 2


def _assign_heading_levels(elements, heading_sizes, outline_titles):
    """Níveis de título.

    1) Abertura de capítulo: rótulo curto (≤ 40 caracteres) logo acima de um título maior na mesma
       página, com padrão que se repete (ex.: "01ª Parte.", "Capítulo 2"). Rótulo e título → nível 1,
       e o rótulo inicia o capítulo. Funciona mesmo com títulos de tamanho ajustado ao texto.
    2) Demais títulos: grupos de tamanhos próximos (razão < 1,12), do maior para o menor.
    """
    def next_content(i):
        j = i + 1
        while j < len(elements) and (elements[j].type in ("header", "footer", "page_number", "decoration")
                                     or (elements[j].type in ("image", "figure") and elements[j].sources
                                         and elements[i].sources
                                         and elements[j].sources[0].page == elements[i].sources[0].page)):
            j += 1
        return elements[j] if j < len(elements) else None

    pairs, patterns = [], Counter()
    for i, k in enumerate(elements):
        if k.type != "heading" or len(k.text or "") > 40 or not k.sources:
            continue
        t = next_content(i)
        if (t is not None and t.type in ("heading", "paragraph") and t.sources
                and t.sources[0].page == k.sources[0].page and _sz(t) > _sz(k) * 1.1
                and k.sources[0].bbox[3] <= t.sources[0].bbox[1] + 2):
            pairs.append((k, t))
            patterns[normalize_pattern(k.text or "")] += 1
    openers = {}
    for k, t in pairs:
        if patterns[normalize_pattern(k.text or "")] >= 2:
            k.level, k.chapter_break, k.subtype = 1, True, k.subtype or "kicker"
            k.extraction.evidence.append("rótulo de abertura de capítulo (padrão repetido)")
            if t.type == "paragraph":
                t.type = "heading"
                t.review = [r for r in t.review if not r.startswith("classificação")]
            t.level, t.subtype = 1, t.subtype or "chapter-title"
            t.extraction.evidence.append("título de capítulo (após rótulo)")
            openers[k.id] = openers[t.id] = True

    rest = [e for e in elements if e.type == "heading" and e.id not in openers and not e.level]
    clusters = cluster_sizes([_sz(e) for e in elements if e.type == "heading" and e.id not in openers])
    offset = 1 if openers else 0
    level_of = {sz: min(6, k + 1 + offset) for k, cl in enumerate(clusters) for sz in cl}
    for e in rest:
        e.level = level_of.get(_sz(e), 2)
    if not openers:
        for e in elements:
            if e.type == "heading" and e.level == 1:
                e.chapter_break = True
    # ilustração acima do início do capítulo, na mesma página, abre o capítulo junto
    for i, e in enumerate(elements):
        if not e.chapter_break or not e.sources:
            continue
        j, first = i - 1, None
        while j >= 0:
            x = elements[j]
            if x.type in ("header", "footer", "page_number", "decoration") or not x.included:
                j -= 1
                continue
            if (x.type in ("image", "figure") and x.sources and x.sources[0].page == e.sources[0].page
                    and x.sources[0].bbox[1] <= e.sources[0].bbox[1]):
                first = x
                j -= 1
                continue
            break
        if first is not None:
            first.chapter_break, e.chapter_break = True, False
            first.extraction.evidence.append("ilustração de abertura de capítulo")


def link_toc_entries(elements: list[Element]) -> None:
    """Liga entradas do sumário original aos títulos correspondentes (link interno)."""
    def key(t):
        return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", t or "").lower())

    heads = {}
    for e in elements:
        if e.type == "heading":
            heads.setdefault(key(e.text), e)
    for e in elements:
        if e.type == "toc" and e.subtype == "toc-entry" and e.text:
            h = heads.get(key(e.text))
            if h is not None:
                e.marks = [m for m in e.marks if m.kind != "link"]
                e.marks.append(InlineMark(start=0, end=len(e.text), kind="link", href=f"#e-{h.id}"))
                e.depends_on.append(h.id)
            else:
                e.review.append("entrada de sumário sem título correspondente")


def crop_dpi(w: float, h: float, base: float = 150, max_px: int = 2000) -> float:
    """Resolução dos recortes: até 150 dpi, limitada a ~2000 px no maior lado da página."""
    return max(36.0, min(base, max_px * 72.0 / max(w, h, 1)))


def detect_toc_entries(blocks: list[Block], page_w: float, is_margin=None) -> list[Block] | None:
    """Detecta página de sumário por números de página alinhados à direita e agrupa entradas.

    Suporta o número na primeira linha da entrada (texto continua abaixo) ou na última.
    Retorna os novos blocos (um por entrada) ou None se a página não parece um sumário.
    """
    is_margin = is_margin or (lambda b: bool(b.extra.get("zone")))
    margin = [b for b in blocks if is_margin(b)]  # cabeçalho/rodapé recorrentes ficam fora do sumário
    blocks = [b for b in blocks if not is_margin(b)]
    lines = [l for b in blocks for l in b.lines]
    if len(lines) < 5:
        return None
    num_re = re.compile(r"^(\d{1,4}|[ivxlcdm]{1,6})$", re.I)
    right = max(l.x1 for l in lines)
    numbered = []
    for l in lines:
        last = l.words[-1]
        gap = last.x0 - l.words[-2].x1 if len(l.words) > 1 else 0
        numbered.append(bool(num_re.match(last.text)) and len(l.words) > 1 and right - last.x1 <= page_w * 0.03
                        and gap >= last.size * 0.6)
    nums = [int(l.words[-1].text) for l, n in zip(lines, numbered) if n and l.words[-1].text.isdigit()]
    if sum(numbered) < 5 or sum(numbered) < 0.4 * len(lines):
        return None
    if len(nums) >= 3 and sum(1 for a, b in zip(nums, nums[1:]) if b >= a) < 0.8 * (len(nums) - 1):
        return None
    first = next(i for i, n in enumerate(numbered) if n)
    body_sz = statistics.median([l.size for l in lines])
    # acima da primeira entrada numerada: título ("Sumário", fonte maior) fica separado;
    # linhas comuns sem número indicam que o número vem na ÚLTIMA linha da entrada
    head_n = 0
    while head_n < first and (TOC_HEAD_RE.match(" ".join(w.text for w in lines[head_n].words))
                              or lines[head_n].size > body_sz * 1.15):
        head_n += 1
    head = lines[:head_n]
    # votos: linha sem número começando em minúscula continua a entrada anterior (número na 1ª linha);
    # linha numerada começando em minúscula continua a anterior (número na última linha)
    def starts_lower(l):
        return bool(re.match(r"^[\W\d]*[a-zà-ÿ]", " ".join(w.text for w in l.words)))

    v_first = v_last = 0
    for i in range(head_n + 1, len(lines)):
        if starts_lower(lines[i]):
            if numbered[i - 1] and not numbered[i]:
                v_first += 1
            elif numbered[i] and not numbered[i - 1]:
                v_last += 1
    if v_first != v_last:
        number_first = v_first > v_last
    elif head_n < first:
        number_first = False
    else:
        number_first = not numbered[-1]
    first = head_n
    out: list[Block] = list(margin)
    for l in head:  # título ("Sumário") ou cabeçalho acima das entradas
        out.append(Block(blocks[0].page, [l], l.col, method=blocks[0].method))
    cur: list[Line] = []
    label = None

    def close():
        nonlocal cur, label
        if cur:
            b = Block(blocks[0].page, cur, cur[0].col, method=blocks[0].method)
            b.extra["toc_page"] = label
            out.append(b)
        cur, label = [], None

    for l, n in zip(lines[first:], numbered[first:]):
        if n:
            num_word = l.words[-1]
            l = Line(l.words[:-1], l.col)
            if number_first:
                close()
                cur, label = [l], num_word.text
            else:
                cur.append(l)
                label = num_word.text
                close()
        else:
            cur.append(l)
    close()
    for b in out:
        if b not in margin:
            b.extra["zone"] = None
    entries = [b for b in out if b.extra.get("toc_page")]
    if entries:
        entries[0].extra["toc_first"] = True
    return out
