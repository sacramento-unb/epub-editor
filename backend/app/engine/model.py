"""Modelo documental intermediário (IR) — fonte de verdade da exportação.

Sistema de coordenadas (``coord = "page_display_pt"``): pontos PDF (1/72 pol.),
origem no canto superior esquerdo da página *como exibida* (após aplicar /Rotate),
relativa à CropBox. É o mesmo espaço do viewport do PDF.js com escala 1 e a
rotação intrínseca da página. Rotação adicional/zoom da interface são
transformações de visualização, nunca gravadas no documento.
"""
from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, Field

SCHEMA_VERSION = 1

ElementType = Literal[
    "heading", "paragraph", "list_item", "blockquote", "image", "figure", "caption",
    "table", "formula", "footnote", "header", "footer", "page_number", "toc",
    "cover", "decoration", "watermark", "region",
]

# Tipos que não entram por padrão na exportação.
NON_CONTENT_TYPES = {"header", "footer", "page_number", "decoration", "watermark"}
TEXT_TYPES = {"heading", "paragraph", "list_item", "blockquote", "caption", "footnote",
              "header", "footer", "page_number", "toc"}
VISUAL_TYPES = {"image", "figure", "formula", "region", "cover"}

# Tokens de estilo editorial permitidos (nunca CSS arbitrário).
STYLE_TOKENS: dict[str, list[str]] = {
    "align": ["left", "center", "right", "justify"],
    "emphasis": ["none", "bold", "italic", "bold-italic", "small-caps"],
    "size": ["xsmall", "small", "normal", "large", "xlarge"],
    "spacing_before": ["none", "small", "normal", "large"],
    "spacing_after": ["none", "small", "normal", "large"],
    "indent": ["none", "first-line", "block"],
    "width": ["25", "50", "75", "100"],  # largura de imagens (%)
    "list_style": ["disc", "decimal", "none"],
    "page_break_before": ["auto", "always"],
}

# Propriedades globais permitidas (valores validados por faixa/enumeração).
GLOBAL_STYLE_RULES: dict[str, dict] = {
    "body_font": {"enum": ["serif", "sans-serif", "monospace"]},
    "heading_font": {"enum": ["serif", "sans-serif", "monospace"]},
    "line_height": {"min": 1.0, "max": 2.2},
    "paragraph_spacing_em": {"min": 0.0, "max": 2.0},
    "text_indent_em": {"min": 0.0, "max": 3.0},
    "text_align": {"enum": ["left", "justify"]},
    "margin_em": {"min": 0.0, "max": 4.0},
    "heading_scale": {"min": 1.0, "max": 1.6},
    "image_max_width_pct": {"min": 30, "max": 100},
    "hyphens": {"enum": ["auto", "manual", "none"]},
}

DEFAULT_GLOBAL_STYLE = {
    "body_font": "serif", "heading_font": "sans-serif", "line_height": 1.5,
    "paragraph_spacing_em": 0.0, "text_indent_em": 1.2, "text_align": "justify",
    "margin_em": 1.0, "heading_scale": 1.25, "image_max_width_pct": 100, "hyphens": "manual",
}


class SourceRegion(BaseModel):
    page: int  # índice 0-based
    bbox: tuple[float, float, float, float]  # x0, top, x1, bottom
    coord: Literal["page_display_pt"] = "page_display_pt"


class ObservedStyle(BaseModel):
    font: str | None = None
    size: float | None = None
    bold: bool = False
    italic: bool = False
    color: str | None = None
    align: str | None = None


class InlineMark(BaseModel):
    """Marca inline sobre ``text`` (offsets de caractere)."""
    start: int
    end: int
    kind: Literal["bold", "italic", "sup", "sub", "link", "noteref"]
    href: str | None = None  # URI externa, "#elem-id" interno ou id da nota


class TableCell(BaseModel):
    text: str = ""
    header: bool = False
    rowspan: int = 1
    colspan: int = 1


class Extraction(BaseModel):
    method: Literal["native", "ocr", "manual", "derived", "ai"] = "native"
    # Confiança heurística (0–1) ou do OCR; NÃO é probabilidade calibrada.
    confidence: float | None = None
    evidence: list[str] = Field(default_factory=list)


class Element(BaseModel):
    id: str
    type: ElementType
    subtype: str | None = None
    text_original: str | None = None
    text: str | None = None
    marks: list[InlineMark] = Field(default_factory=list)
    sources: list[SourceRegion] = Field(default_factory=list)
    level: int | None = None  # nível de título (1–6) ou de lista
    chapter_break: bool = False  # inicia novo arquivo/capítulo no EPUB fluido
    parent_id: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    included: bool = True
    excluded_explicit: bool = False  # exclusão individual explícita pelo usuário
    locked: bool = False  # bloqueado contra alterações de IA
    observed_style: ObservedStyle = Field(default_factory=ObservedStyle)
    output_style: dict[str, str] = Field(default_factory=dict)  # tokens STYLE_TOKENS
    asset: str | None = None  # caminho relativo do recorte/imagem
    caption_id: str | None = None
    alt: str | None = None
    note_id: str | None = None  # para footnote: rótulo (ex.: "1")
    table: list[list[TableCell]] | None = None
    mathml: str | None = None  # somente quando transcrito de forma confiável
    extraction: Extraction = Field(default_factory=Extraction)
    review: list[str] = Field(default_factory=list)  # pendências de revisão
    provenance: list[str] = Field(default_factory=list)  # ids de elementos de origem

    @property
    def anchor(self) -> str:
        return f"e-{self.id}"


class PageInfo(BaseModel):
    index: int
    width: float  # largura exibida (após rotação), pontos
    height: float
    rotation: int = 0
    mediabox: tuple[float, float, float, float]
    cropbox: tuple[float, float, float, float]
    kind: Literal["digital", "scanned", "mixed", "empty"] = "digital"
    ocr_confidence: float | None = None
    ocr_lang: str | None = None


class Metadata(BaseModel):
    title: str = "Sem título"
    authors: list[str] = Field(default_factory=list)
    language: str = "pt-BR"
    publisher: str | None = None
    description: str | None = None
    identifier: str = ""
    direction: Literal["ltr", "rtl"] = "ltr"


class Document(BaseModel):
    schema_version: int = SCHEMA_VERSION
    metadata: Metadata = Field(default_factory=Metadata)
    pages: list[PageInfo] = Field(default_factory=list)
    elements: list[Element] = Field(default_factory=list)  # ordem = ordem de leitura
    global_style: dict = Field(default_factory=lambda: dict(DEFAULT_GLOBAL_STYLE))
    outline: list[dict] = Field(default_factory=list)  # marcadores do PDF {title, level, page}
    doc_kind: Literal["digital", "scanned", "mixed", "empty"] = "digital"
    warnings: list[str] = Field(default_factory=list)

    def by_id(self) -> dict[str, Element]:
        return {e.id: e for e in self.elements}

    def get(self, eid: str) -> Element | None:
        for e in self.elements:
            if e.id == eid:
                return e
        return None

    def children(self, eid: str) -> list[Element]:
        return [e for e in self.elements if e.parent_id == eid]

    def content_hash(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()

    def selected(self) -> list[Element]:
        """Elementos que entram na exportação, em ordem de leitura."""
        return [e for e in self.elements if e.included]


def element_text_for_integrity(e: Element) -> str:
    if e.table:
        return " ".join(c.text for row in e.table for c in row)
    return e.text or ""


def doc_to_json(doc: Document) -> str:
    return doc.model_dump_json()


def doc_from_json(data: str | bytes | dict) -> Document:
    if isinstance(data, dict):
        return Document.model_validate(data)
    return Document.model_validate(json.loads(data))
