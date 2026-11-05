"""Gera PDFs de teste localmente (reportlab, BSD). Nenhum conteúdo de terceiros.

Uso: python -m tests.fixtures <diretório>
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

from reportlab.lib.colors import Color
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

W, H = A4
LOREM = ("A conversão de documentos exige atenção à estrutura do texto, à ordem de leitura e "
         "à preservação dos valores numéricos como 3,14 m² e 42 kg. ")


def _para(c, x, y, text, width=80, size=11, font="Helvetica", leading=None):
    """Escreve texto quebrando por número de caracteres; retorna y final."""
    leading = leading or size * 1.35
    c.setFont(font, size)
    words, line = text.split(), ""
    for w in words:
        if len(line) + len(w) + 1 > width:
            c.drawString(x, y, line)
            y -= leading
            line = w
        else:
            line = (line + " " + w).strip()
    if line:
        c.drawString(x, y, line)
        y -= leading
    return y


def simple(path):
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setTitle("Livro de Teste")
    c.setAuthor("Equipe de Testes")
    c.bookmarkPage("cap1")
    c.addOutlineEntry("Capítulo 1 — Introdução", "cap1", 0)
    c.setFont("Helvetica-Bold", 22)
    c.drawString(72, H - 100, "Capítulo 1 — Introdução")
    y = H - 140
    y = _para(c, 72, y, "Este é o primeiro parágrafo do livro. " + LOREM * 2) - 8
    # hifenização de fim de linha
    c.setFont("Helvetica", 11)
    c.drawString(72, y, "A palavra seguinte foi dividida no fim da li-")
    y -= 15
    c.drawString(72, y, "nha pelo diagramador. O guarda-chuva continua com hífen.")
    y -= 23
    c.setFont("Helvetica-Bold", 15)
    c.drawString(72, y, "1.1 Seção com lista")
    y -= 24
    for item in ("• Primeiro item da lista", "• Segundo item da lista", "• Terceiro item da lista"):
        c.setFont("Helvetica", 11)
        c.drawString(80, y, item)
        y -= 18
    y -= 8
    c.setFont("Helvetica", 11)
    c.drawString(72, y, "Visite o site do projeto para mais informações.")
    c.linkURL("https://example.org/projeto", (72, y - 3, 330, y + 11))
    y -= 30
    c.bookmarkPage("cap2")
    c.showPage()
    c.addOutlineEntry("Capítulo 2 — Desenvolvimento", "cap2", 0)
    c.setFont("Helvetica-Bold", 22)
    c.drawString(72, H - 100, "Capítulo 2 — Desenvolvimento")
    y = _para(c, 72, H - 140, "Segundo capítulo com texto corrido. " + LOREM * 3)
    # parágrafo que atravessa a página
    y = _para(c, 72, y - 8, "Este parágrafo começa nesta página e continua na próxima página sem "
                            "ponto final antes da quebra porque o texto é longo e")
    c.showPage()
    y = _para(c, 72, H - 72, "termina aqui, na página seguinte, com ponto final. " + LOREM)
    c.showPage()
    c.save()


def two_columns(path):
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(72, H - 80, "Artigo em Duas Colunas")
    colw = 36
    left = ["Coluna esquerda parágrafo um. " + LOREM, "Coluna esquerda parágrafo dois. " + LOREM]
    right = ["Coluna direita parágrafo um. " + LOREM, "Coluna direita parágrafo dois. " + LOREM]
    y = H - 120
    for t in left:
        y = _para(c, 56, y, t, width=colw, size=10) - 10
    y = H - 120
    for t in right:
        y = _para(c, 310, y, t, width=colw, size=10) - 10
    c.showPage()
    c.save()


def _page_image(draw_fn, dpi=200):
    """Desenha uma página com reportlab e devolve imagem PIL (para simular digitalização)."""
    import pypdfium2 as pdfium

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    draw_fn(c)
    c.showPage()
    c.save()
    doc = pdfium.PdfDocument(buf.getvalue())
    img = doc[0].render(scale=dpi / 72).to_pil().convert("L")
    doc.close()
    return img


def _scanned_page(c):
    def draw(cc):
        cc.setFont("Helvetica-Bold", 20)
        cc.drawString(72, H - 100, "Documento Digitalizado")
        _para(cc, 72, H - 140, "Texto reconhecido por OCR com confiança registrada. "
                               "O valor medido foi 125 kg em 2024.", size=13)
    img = _page_image(draw)
    from reportlab.lib.utils import ImageReader

    c.drawImage(ImageReader(img), 0, 0, W, H)


def scanned(path):
    c = canvas.Canvas(str(path), pagesize=A4)
    _scanned_page(c)
    c.showPage()
    c.save()


def mixed(path):
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(72, H - 100, "Página Digital")
    _para(c, 72, H - 140, "Esta página tem camada de texto nativa. " + LOREM)
    c.showPage()
    _scanned_page(c)
    c.showPage()
    c.save()


def tables_figures_notes(path):
    from PIL import Image, ImageDraw
    from reportlab.lib.utils import ImageReader

    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(72, H - 80, "Dados e Figuras")
    y = _para(c, 72, H - 115, "O resultado principal aparece na tabela abaixo.")
    # tabela com linhas
    rows = [["Material", "Massa (kg)", "Volume (m³)"], ["Aço", "7850", "1,0"], ["Água", "1000", "1,0"]]
    x0, top, cw, rh = 72, y - 10, 140, 22
    for r, row in enumerate(rows):
        for k, cell in enumerate(row):
            c.rect(x0 + k * cw, top - (r + 1) * rh, cw, rh)
            c.setFont("Helvetica-Bold" if r == 0 else "Helvetica", 10)
            c.drawString(x0 + k * cw + 5, top - (r + 1) * rh + 7, cell)
    y = top - len(rows) * rh - 14
    c.setFont("Helvetica", 10)
    c.drawString(72, y, "Tabela 1 – Densidades de referência.")
    y -= 30
    img = Image.new("RGB", (400, 240), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([20, 20, 380, 220], outline="black", width=4)
    d.ellipse([120, 60, 280, 200], fill=(200, 60, 60))
    c.drawImage(ImageReader(img), 72, y - 180, 300, 180)
    y -= 196
    c.setFont("Helvetica", 10)
    c.drawString(72, y, "Figura 1 – Diagrama esquemático do ensaio.")
    y -= 30
    c.setFont("Helvetica", 11)
    c.drawString(72, y, "A densidade foi medida conforme a norma vigente")
    c.setFont("Helvetica", 7)
    c.drawString(72 + c.stringWidth("A densidade foi medida conforme a norma vigente", "Helvetica", 11) + 1,
                 y + 5, "1")
    c.setFont("Helvetica", 11)
    c.drawString(72 + c.stringWidth("A densidade foi medida conforme a norma vigente", "Helvetica", 11) + 6,
                 y, "e registrada.")
    y -= 30
    c.setFont("Times-Italic", 12)
    c.drawString(140, y, "x² + y² = z²")
    c.setFont("Symbol", 12)
    c.drawString(220, y, "a + b = g")
    c.setFont("Helvetica", 8)
    c.drawString(72, 70, "1 Norma técnica de referência, edição de 2020.")
    c.showPage()
    c.save()


def headers(path, pages=5):
    c = canvas.Canvas(str(path), pagesize=A4)
    for p in range(pages):
        c.setFont("Helvetica", 9)
        c.drawString(72, H - 40, "Manual Técnico — Edição 2025")
        c.drawString(W / 2 - 5, 30, str(p + 1))
        c.setFont("Helvetica-Bold", 13)
        c.drawString(72, H - 90, f"Seção {p + 1}")
        y = _para(c, 72, H - 115, f"Conteúdo da página {p + 1}. " + LOREM)
        # frase legítima que se repete no corpo de todas as páginas
        _para(c, 72, y - 10, "Observação: verifique as unidades antes de prosseguir.")
        c.showPage()
    c.save()


def rotated(path):
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(72, H - 90, "Página Retrato")
    _para(c, 72, H - 120, "Texto normal na primeira página. " + LOREM)
    c.showPage()
    # página paisagem: conteúdo desenhado rotacionado + /Rotate 90 => aparece em pé
    c.setPageRotation(90)
    c.saveState()
    c.translate(W, 0)
    c.rotate(90)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(72, W - 90, "Página Rotacionada")
    c.setFont("Helvetica", 11)
    c.drawString(72, W - 120, "Texto que deve ser lido na horizontal após a rotação.")
    c.restoreState()
    c.showPage()
    c.save()


def cropped(path):
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setCropBox((36, 36, W - 36, H - 36))
    c.setFont("Helvetica-Bold", 18)
    c.drawString(72, H - 90, "Página com CropBox")
    _para(c, 72, H - 120, "Coordenadas relativas à área visível. " + LOREM)
    c.showPage()
    c.save()


def encrypted(path, password="segredo"):
    from reportlab.lib import pdfencrypt

    enc = pdfencrypt.StandardEncryption(password, canPrint=1)
    c = canvas.Canvas(str(path), pagesize=A4, encrypt=enc)
    c.setFont("Helvetica", 12)
    c.drawString(72, H - 90, "Conteúdo protegido.")
    c.showPage()
    c.save()


def book(path):
    """Livro em formato de tela (1080×1920 pt), fundo escuro, capa em imagem, sumário sem título
    com número na primeira linha da entrada e espaçamento largo, rótulo de capítulo acima do título,
    títulos com tamanhos ajustados ao texto e marca de licença repetida no topo."""
    import random

    from PIL import Image
    from reportlab.lib.utils import ImageReader

    BW, BH = 1080, 1920
    c = canvas.Canvas(str(path), pagesize=(BW, BH))
    claims = ["“Primeira afirmação de teste!”", "“Segunda afirmação um pouco mais longa de teste!”",
              "“Terceira afirmação de teste com um texto bem mais comprido que quebra em duas linhas no sumário”"]
    sizes = [100, 121, 85]
    rnd = random.Random(7)

    def bg():
        c.setFillColorRGB(0.05, 0.05, 0.05)
        c.rect(0, 0, BW, BH, stroke=0, fill=1)
        c.setFillColorRGB(0.6, 0.6, 0.6)
        c.setFont("Helvetica", 11)
        c.drawString(2, BH - 14, "Licenciado para Leitor de Teste - leitor@example.org")

    # capa: imagem "fotográfica" ocupando a página
    img = Image.frombytes("RGB", (540, 960), bytes(rnd.randrange(256) for _ in range(540 * 960 * 3)))
    img = img.resize((270, 480)).resize((540, 960))  # textura "fotográfica" (muitas cores)
    bg()
    c.drawImage(ImageReader(img), 0, 0, BW, BH)
    c.showPage()
    # sumário sem título, número na primeira linha, linhas espaçadas (60 pt para fonte 30)
    bg()
    c.setFillColorRGB(0.9, 0.4, 0.2)
    entries = [(claims[0], 4), (claims[1], 7), (claims[2], 9)]
    y = BH - 140
    for text, page in entries:
        words, line, lines = text.split(), "", []
        for w in words:
            if c.stringWidth(line + " " + w, "Helvetica-Bold", 30) > 820:
                lines.append(line)
                line = w
            else:
                line = (line + " " + w).strip()
        lines.append(line)
        for k, ln in enumerate(lines):
            c.setFont("Helvetica-Bold", 30)
            c.drawString(94, y, ln)
            if k == 0:
                c.drawRightString(999, y, str(page))
            y -= 60
    # mais entradas para formar um sumário plausível
    for k in range(4):
        c.drawString(94, y, f"“Entrada extra de sumário número {k + 1}”")
        c.drawRightString(999, y, str(12 + k))
        y -= 60
    c.showPage()
    # capítulos: rótulo "Nª Parte." (50 pt) acima do título (tamanho variável), provas (64 pt), corpo (30 pt)
    for n, (claim, sz) in enumerate(zip(claims, sizes)):
        bg()
        if n == 0:  # ilustração de abertura acima do rótulo
            c.drawImage(ImageReader(img.resize((200, 100))), 138, BH - 500, 600, 300)
        c.setFillColorRGB(0.9, 0.4, 0.2)
        c.setFont("Helvetica-Bold", 50)
        c.drawString(138, BH - 594, f"{n + 1:02d}ª Parte.")
        c.setFont("Helvetica-Bold", sz)
        yy = BH - 760
        line = ""
        for w in claim.split():
            if c.stringWidth(line + " " + w, "Helvetica-Bold", sz) > 800:
                c.drawString(138, yy, line)
                yy -= sz * 1.1
                line = w
            else:
                line = (line + " " + w).strip()
        c.drawString(138, yy, line)
        c.showPage()
        bg()
        c.setFillColorRGB(0.85, 0.85, 0.85)
        c.setFont("Helvetica-Bold", 64)
        c.drawString(94, BH - 350, "Prova 1")
        y = _para(c, 94, BH - 450, "Texto do corpo do capítulo de teste com conteúdo suficiente. " * 4, width=48,
                  size=30, leading=42)
        c.setFont("Helvetica", 40)
        c.drawString(94, y - 40, "+")
        c.setFont("Helvetica-Bold", 64)
        c.drawString(94, y - 160, "Prova 2")
        _para(c, 94, y - 260, "Segundo trecho do corpo do capítulo de teste. " * 3, width=48, size=30, leading=42)
        c.showPage()
    c.save()


ALL = {
    "simple.pdf": simple, "two_columns.pdf": two_columns, "scanned.pdf": scanned, "mixed.pdf": mixed,
    "tables_figures_notes.pdf": tables_figures_notes, "headers.pdf": headers, "rotated.pdf": rotated,
    "cropped.pdf": cropped, "encrypted.pdf": encrypted, "book.pdf": book,
}


def make_all(out: Path) -> dict[str, Path]:
    out.mkdir(parents=True, exist_ok=True)
    res = {}
    for name, fn in ALL.items():
        p = out / name
        if not p.exists():
            fn(p)
        res[name] = p
    return res


if __name__ == "__main__":
    print(make_all(Path(sys.argv[1] if len(sys.argv) > 1 else "samples")))
