"""Operações tipadas sobre o modelo documental.

Toda alteração — manual ou de IA — passa por aqui. As operações são validadas e
aplicadas atomicamente sobre uma cópia; se qualquer uma falhar, nada é alterado.
O ator ``ai`` tem uma lista explícita e menor de operações permitidas e
restrições adicionais (itens bloqueados/excluídos, texto, escopo).
"""
from __future__ import annotations

import uuid
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter

from .model import (GLOBAL_STYLE_RULES, STYLE_TOKENS, Document, Element, Extraction,
                    InlineMark, SourceRegion)

OPS_SCHEMA_VERSION = 1


class OpBase(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class SetIncluded(OpBase):
    op: Literal["set_included"] = "set_included"
    ids: list[str]
    included: bool
    # "item": decisão individual (marca exclusão explícita);
    # "batch": decisão em lote (respeita exclusões explícitas dos filhos).
    mode: Literal["item", "batch"] = "item"


class SetLocked(OpBase):
    op: Literal["set_locked"] = "set_locked"
    ids: list[str]
    locked: bool


class SetType(OpBase):
    op: Literal["set_type"] = "set_type"
    id: str
    type: Literal["heading", "paragraph", "list_item", "blockquote", "caption", "footnote",
                  "header", "footer", "page_number", "toc", "image", "figure", "formula",
                  "decoration", "watermark", "table", "cover"]
    subtype: str | None = None


class SetHeadingLevel(OpBase):
    op: Literal["set_heading_level"] = "set_heading_level"
    id: str
    level: int = Field(ge=1, le=6)


class SetText(OpBase):
    op: Literal["set_text"] = "set_text"
    id: str
    text: str = Field(max_length=200_000)


class SetStyle(OpBase):
    op: Literal["set_style"] = "set_style"
    ids: list[str]
    style: dict[str, str]  # token -> valor; "" remove o token


class SetGlobalStyle(OpBase):
    op: Literal["set_global_style"] = "set_global_style"
    props: dict[str, float | str]


class LinkCaption(OpBase):
    op: Literal["link_caption"] = "link_caption"
    figure_id: str
    caption_id: str | None


class SetAlt(OpBase):
    op: Literal["set_alt"] = "set_alt"
    id: str
    alt: str = Field(max_length=2000)


class Move(OpBase):
    op: Literal["move"] = "move"
    ids: list[str]
    before_id: str | None = None
    after_id: str | None = None


class SetChapterBreak(OpBase):
    op: Literal["set_chapter_break"] = "set_chapter_break"
    id: str
    value: bool


class Split(OpBase):
    op: Literal["split"] = "split"
    id: str
    offset: int = Field(ge=1)
    new_id: str | None = None


class Merge(OpBase):
    op: Literal["merge"] = "merge"
    ids: list[str] = Field(min_length=2)
    separator: Literal[" ", "", "\n"] = " "


class Group(OpBase):
    op: Literal["group"] = "group"
    ids: list[str] = Field(min_length=1)
    type: Literal["figure", "region", "formula"] = "figure"
    new_id: str | None = None


class Ungroup(OpBase):
    op: Literal["ungroup"] = "ungroup"
    id: str


class CreateRegion(OpBase):
    op: Literal["create_region"] = "create_region"
    page: int = Field(ge=0)
    bbox: tuple[float, float, float, float]
    type: Literal["figure", "image", "formula", "region", "table"] = "region"
    alt: str | None = None
    new_id: str | None = None


class SetMetadata(OpBase):
    op: Literal["set_metadata"] = "set_metadata"
    title: str | None = None
    authors: list[str] | None = None
    language: str | None = None


Operation = Annotated[Union[
    SetIncluded, SetLocked, SetType, SetHeadingLevel, SetText, SetStyle, SetGlobalStyle,
    LinkCaption, SetAlt, Move, SetChapterBreak, Split, Merge, Group, Ungroup,
    CreateRegion, SetMetadata,
], Field(discriminator="op")]

OperationList = TypeAdapter(list[Operation])

# Operações que a IA pode propor. Inclusão/exclusão, bloqueio, recortes e
# metadados ficam fora. set_text só com correção de OCR habilitada.
AI_ALLOWED_OPS = {"set_style", "set_global_style", "set_heading_level", "set_type",
                  "link_caption", "move", "set_chapter_break", "split", "merge", "set_alt"}
AI_TEXT_OPS = {"set_text"}
AI_SAFE_TYPES = {"heading", "paragraph", "list_item", "blockquote", "caption"}


class OpError(ValueError):
    pass


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _targets(op) -> list[str]:
    for attr in ("ids",):
        if hasattr(op, attr):
            return list(getattr(op, attr))
    out = []
    for attr in ("id", "figure_id", "caption_id", "before_id", "after_id"):
        v = getattr(op, attr, None)
        if v:
            out.append(v)
    return out


def descendants(doc: Document, eid: str) -> list[Element]:
    out, stack = [], [eid]
    while stack:
        cur = stack.pop()
        for c in doc.children(cur):
            out.append(c)
            stack.append(c.id)
    return out


def check_ai_op(doc: Document, op, *, allow_text: bool = False,
                scope_ids: set[str] | None = None) -> None:
    """Invariantes para operações propostas por IA."""
    allowed = AI_ALLOWED_OPS | (AI_TEXT_OPS if allow_text else set())
    if op.op not in allowed:
        raise OpError(f"operação '{op.op}' não é permitida para IA")
    by = doc.by_id()
    for t in _targets(op):
        e = by.get(t)
        if e is None:
            raise OpError(f"alvo inexistente: {t}")
        if e.locked:
            raise OpError(f"elemento bloqueado: {t}")
        if not e.included:
            raise OpError(f"elemento excluído não pode ser alterado pela IA: {t}")
        if scope_ids is not None and t not in scope_ids:
            raise OpError(f"alvo fora do escopo: {t}")
    if op.op == "set_type" and op.type not in AI_SAFE_TYPES:
        raise OpError(f"tipo '{op.type}' não permitido para IA")
    if op.op == "set_type":
        e = by[op.id]
        if e.type not in AI_SAFE_TYPES:
            raise OpError(f"IA não pode reclassificar elemento do tipo '{e.type}'")
    if op.op == "merge":
        types = {by[i].type for i in op.ids}
        if len(types) != 1:
            raise OpError("IA só pode unir elementos do mesmo tipo")


def apply_ops(doc: Document, ops: list, *, actor: Literal["user", "ai"] = "user",
              allow_text: bool = False, scope_ids: set[str] | None = None) -> Document:
    """Aplica operações atomicamente; retorna um novo Document."""
    new = doc.model_copy(deep=True)
    parsed = [OperationList.validate_python([o])[0] if isinstance(o, dict) else o for o in ops]
    for op in parsed:
        if actor == "ai":
            check_ai_op(new, op, allow_text=allow_text, scope_ids=scope_ids)
        _apply_one(new, op, actor)
    _check_integrity(new)
    return new


def _require(doc: Document, eid: str) -> Element:
    e = doc.get(eid)
    if e is None:
        raise OpError(f"elemento inexistente: {eid}")
    return e


def _apply_one(doc: Document, op, actor: str) -> None:
    k = op.op
    if k == "set_included":
        for eid in op.ids:
            e = _require(doc, eid)
            family = [e] + descendants(doc, eid)
            for i, x in enumerate(family):
                if op.included:
                    if op.mode == "batch" and x.excluded_explicit:
                        continue  # respeita exclusão explícita
                    if op.mode == "batch" and i > 0 and x.excluded_explicit:
                        continue
                    x.included = True
                    if op.mode == "item" and i == 0:
                        x.excluded_explicit = False
                else:
                    x.included = False
                    if op.mode == "item" and i == 0:
                        x.excluded_explicit = True
    elif k == "set_locked":
        for eid in op.ids:
            _require(doc, eid).locked = op.locked
    elif k == "set_type":
        e = _require(doc, op.id)
        e.type = op.type
        e.subtype = op.subtype
        if op.type == "heading" and not e.level:
            e.level = 2
        if op.type != "heading" and op.type != "list_item":
            e.level = None
        e.review = [r for r in e.review if not r.startswith("classificação")]
    elif k == "set_heading_level":
        e = _require(doc, op.id)
        if e.type != "heading":
            raise OpError("nível de título só se aplica a títulos")
        e.level = op.level
    elif k == "set_text":
        e = _require(doc, op.id)
        if e.text is None and e.type not in ("heading", "paragraph", "list_item", "blockquote",
                                              "caption", "footnote", "toc"):
            raise OpError("elemento sem texto editável")
        old = e.text or ""
        e.text = op.text
        e.marks = _remap_marks(old, op.text, e.marks)
    elif k == "set_style":
        for t, v in op.style.items():
            if t not in STYLE_TOKENS:
                raise OpError(f"token de estilo desconhecido: {t}")
            if v and v not in STYLE_TOKENS[t]:
                raise OpError(f"valor inválido para {t}: {v}")
        for eid in op.ids:
            e = _require(doc, eid)
            for t, v in op.style.items():
                if v:
                    e.output_style[t] = v
                else:
                    e.output_style.pop(t, None)
    elif k == "set_global_style":
        for p, v in op.props.items():
            rule = GLOBAL_STYLE_RULES.get(p)
            if rule is None:
                raise OpError(f"propriedade global desconhecida: {p}")
            if "enum" in rule:
                if v not in rule["enum"]:
                    raise OpError(f"valor inválido para {p}: {v}")
            else:
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    raise OpError(f"valor numérico esperado para {p}") from None
                if not rule["min"] <= fv <= rule["max"]:
                    raise OpError(f"{p} fora da faixa [{rule['min']}, {rule['max']}]")
                v = fv
            doc.global_style[p] = v
    elif k == "link_caption":
        fig = _require(doc, op.figure_id)
        if fig.type not in ("image", "figure", "table", "formula", "region"):
            raise OpError("legenda só pode ser vinculada a figura, imagem, tabela ou fórmula")
        if op.caption_id:
            cap = _require(doc, op.caption_id)
            if cap.type != "caption":
                raise OpError("alvo da legenda deve ser do tipo caption")
            for other in doc.elements:  # uma legenda pertence a uma figura
                if other.caption_id == op.caption_id:
                    other.caption_id = None
        fig.caption_id = op.caption_id
    elif k == "set_alt":
        _require(doc, op.id).alt = op.alt
    elif k == "move":
        if bool(op.before_id) == bool(op.after_id):
            raise OpError("informe before_id ou after_id")
        anchor_id = op.before_id or op.after_id
        if anchor_id in op.ids:
            raise OpError("âncora não pode estar entre os elementos movidos")
        moving = []
        for eid in op.ids:
            e = _require(doc, eid)
            moving.append(e)
            moving.extend(d for d in descendants(doc, eid) if d not in moving)
        mv = {e.id for e in moving}
        rest = [e for e in doc.elements if e.id not in mv]
        idx = next((i for i, e in enumerate(rest) if e.id == anchor_id), None)
        if idx is None:
            raise OpError(f"âncora inexistente: {anchor_id}")
        if op.after_id:
            idx += 1
            # depois da âncora e de seus descendentes
            anc_desc = {d.id for d in descendants(doc, anchor_id)}
            while idx < len(rest) and rest[idx].id in anc_desc:
                idx += 1
        doc.elements = rest[:idx] + moving + rest[idx:]
    elif k == "set_chapter_break":
        _require(doc, op.id).chapter_break = op.value
    elif k == "split":
        e = _require(doc, op.id)
        text = e.text or ""
        if not 0 < op.offset < len(text):
            raise OpError("posição de divisão inválida")
        left, right = text[:op.offset].rstrip(), text[op.offset:].lstrip()
        if not left or not right:
            raise OpError("divisão geraria bloco vazio")
        ne = e.model_copy(deep=True)
        ne.id = op.new_id or _new_id()
        ne.text, ne.text_original = right, None
        ne.provenance = [e.id] + e.provenance
        ne.extraction = Extraction(method="derived", evidence=[f"dividido de {e.id}"])
        ne.chapter_break = False
        cut = len(text[:op.offset]) + (len(text[op.offset:]) - len(right))
        ne.marks = [InlineMark(start=m.start - cut, end=m.end - cut, kind=m.kind, href=m.href)
                    for m in e.marks if m.start >= cut]
        e.marks = [m for m in e.marks if m.end <= len(left)]
        e.text = left
        if doc.get(ne.id):
            raise OpError("id já existente")
        i = doc.elements.index(e)
        doc.elements.insert(i + 1, ne)
    elif k == "merge":
        els = [_require(doc, i) for i in op.ids]
        pos = [doc.elements.index(e) for e in els]
        if pos != list(range(pos[0], pos[0] + len(pos))):
            raise OpError("só é possível unir elementos adjacentes, em ordem")
        if any(e.text is None for e in els):
            raise OpError("só é possível unir elementos de texto")
        first = els[0]
        for other in els[1:]:
            base = len(first.text or "") + len(op.separator)
            first.marks += [InlineMark(start=m.start + base, end=m.end + base, kind=m.kind,
                                       href=m.href) for m in other.marks]
            first.text = (first.text or "") + op.separator + (other.text or "")
            first.sources += other.sources
            first.provenance += [other.id] + other.provenance
            first.review = sorted(set(first.review + other.review))
            first.included = first.included or other.included
            for x in doc.elements:  # redireciona referências
                if x.caption_id == other.id:
                    x.caption_id = first.id
                if x.parent_id == other.id:
                    x.parent_id = first.id
                x.depends_on = [first.id if d == other.id else d for d in x.depends_on]
            doc.elements.remove(other)
    elif k == "group":
        els = [_require(doc, i) for i in op.ids]
        gid = op.new_id or _new_id()
        if doc.get(gid):
            raise OpError("id já existente")
        sources: dict[int, list[float]] = {}
        for e in els:
            for s in e.sources:
                b = sources.setdefault(s.page, list(s.bbox))
                b[0], b[1] = min(b[0], s.bbox[0]), min(b[1], s.bbox[1])
                b[2], b[3] = max(b[2], s.bbox[2]), max(b[3], s.bbox[3])
        g = Element(id=gid, type=op.type,
                    sources=[SourceRegion(page=p, bbox=tuple(b)) for p, b in sorted(sources.items())],
                    provenance=[e.id for e in els],
                    extraction=Extraction(method="manual", evidence=["agrupamento manual"]))
        first_idx = min(doc.elements.index(e) for e in els)
        for e in els:
            e.parent_id = gid
        doc.elements.insert(first_idx, g)
    elif k == "ungroup":
        g = _require(doc, op.id)
        for c in doc.children(g.id):
            c.parent_id = g.parent_id
        doc.elements.remove(g)
        for x in doc.elements:
            if x.caption_id == g.id:
                x.caption_id = None
    elif k == "create_region":
        if op.page >= len(doc.pages):
            raise OpError("página inexistente")
        pg = doc.pages[op.page]
        x0, y0, x1, y1 = op.bbox
        x0, x1 = sorted((max(0, x0), min(pg.width, x1)))
        y0, y1 = sorted((max(0, y0), min(pg.height, y1)))
        if x1 - x0 < 2 or y1 - y0 < 2:
            raise OpError("região muito pequena")
        rid = op.new_id or _new_id()
        r = Element(id=rid, type=op.type, sources=[SourceRegion(page=op.page, bbox=(x0, y0, x1, y1))],
                    alt=op.alt, extraction=Extraction(method="manual", evidence=["recorte manual"]),
                    review=["recorte manual: revise texto alternativo"] if not op.alt else [])
        if op.type == "table":
            r.type = "figure"
            r.subtype = "table-crop"
        # insere após o último elemento da mesma página que está acima da região
        idx = len(doc.elements)
        for i, e in enumerate(doc.elements):
            s = e.sources[0] if e.sources else None
            if s and (s.page > op.page or (s.page == op.page and s.bbox[1] > y0)):
                idx = i
                break
        doc.elements.insert(idx, r)
    elif k == "set_metadata":
        if op.title is not None:
            doc.metadata.title = op.title.strip() or doc.metadata.title
        if op.authors is not None:
            doc.metadata.authors = [a.strip() for a in op.authors if a.strip()]
        if op.language is not None:
            doc.metadata.language = op.language
    else:  # pragma: no cover
        raise OpError(f"operação desconhecida: {k}")


def _remap_marks(old: str, new: str, marks: list[InlineMark]) -> list[InlineMark]:
    """Mantém marcas cujo trecho ainda existe no novo texto."""
    out = []
    used = 0
    for m in sorted(marks, key=lambda m: m.start):
        frag = old[m.start:m.end]
        if not frag:
            continue
        pos = new.find(frag, used if m.start >= used else 0)
        if pos >= 0:
            out.append(InlineMark(start=pos, end=pos + len(frag), kind=m.kind, href=m.href))
            used = pos + len(frag)
    return out


def _check_integrity(doc: Document) -> None:
    ids = [e.id for e in doc.elements]
    if len(ids) != len(set(ids)):
        raise OpError("ids duplicados após a operação")
    idset = set(ids)
    for e in doc.elements:
        if e.parent_id and e.parent_id not in idset:
            e.parent_id = None
        if e.caption_id and e.caption_id not in idset:
            e.caption_id = None
        e.depends_on = [d for d in e.depends_on if d in idset]
        if e.text is not None:
            e.marks = [m for m in e.marks if 0 <= m.start < m.end <= len(e.text)]


def exclusion_impact(doc: Document, ids: list[str]) -> list[dict]:
    """Referências que seriam afetadas ao excluir ``ids`` (para confirmar na interface)."""
    target = set(ids)
    for i in ids:
        target |= {d.id for d in descendants(doc, i)}
    by = doc.by_id()
    notes = {by[i].note_id for i in target if i in by and by[i].type == "footnote" and by[i].note_id}
    impact = []
    for e in doc.elements:
        if e.id in target or not e.included:
            continue
        if e.caption_id in target:
            impact.append({"id": e.id, "kind": "legenda", "detail": "figura perde a legenda vinculada"})
        for m in e.marks:
            if m.kind == "link" and m.href and m.href.startswith("#e-") and m.href[3:] in target:
                impact.append({"id": e.id, "kind": "link", "detail": "link interno ficará sem destino"})
            if m.kind == "noteref" and (m.href in target or m.href in notes):
                impact.append({"id": e.id, "kind": "nota", "detail": f"chamada de nota {m.href} ficará sem nota"})
        if set(e.depends_on) & target:
            impact.append({"id": e.id, "kind": "dependência", "detail": "depende de elemento excluído"})
    for i in target:
        e = by.get(i)
        if e and e.caption_id and e.caption_id not in target:
            impact.append({"id": e.caption_id, "kind": "legenda órfã",
                           "detail": "legenda continuará incluída sem a figura"})
    return impact


def equivalent_ids(doc: Document, eid: str) -> list[str]:
    """Elementos equivalentes (mesmo tipo e mesmo padrão normalizado, ex. cabeçalhos)."""
    from .extract import normalize_pattern

    e = _require(doc, eid)
    pat = normalize_pattern(e.text or "")
    return [x.id for x in doc.elements
            if x.type == e.type and normalize_pattern(x.text or "") == pat]
