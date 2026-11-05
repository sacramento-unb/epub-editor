"""Contratos versionados das alterações propostas por IA.

O schema enviado ao modelo é "achatado" (sem uniões) para compatibilidade com
saídas estruturadas de diferentes provedores. O backend converte cada item para
uma operação tipada de ``engine.ops`` e valida tudo antes de aplicar.
Respostas estruturadas garantem forma quando suportadas — não garantem verdade.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from ..engine.model import GLOBAL_STYLE_RULES, STYLE_TOKENS
from ..engine.ops import AI_ALLOWED_OPS, AI_TEXT_OPS, OperationList

AI_SCHEMA_VERSION = "ai-ops/1"
PROMPT_VERSION = "2026-10-23.1"


class KV(BaseModel):
    name: str = Field(max_length=40)
    value: str = Field(max_length=40)


class AIOperation(BaseModel):
    op: str = Field(max_length=40)
    target_ids: list[str] = Field(default_factory=list, max_length=200)
    level: int | None = None
    type: str | None = None
    style: list[KV] = Field(default_factory=list, max_length=12)
    global_props: list[KV] = Field(default_factory=list, max_length=12)
    caption_id: str | None = None
    before_id: str | None = None
    after_id: str | None = None
    offset: int | None = None
    alt: str | None = Field(default=None, max_length=1000)
    value: bool | None = None
    text: str | None = Field(default=None, max_length=20000)
    reason: str = Field(default="", max_length=400)
    evidence: str = Field(default="", max_length=400)
    risk: str = Field(default="", max_length=200)


class VisualResponse(BaseModel):
    schema_version: str = AI_SCHEMA_VERSION
    diagnosis: str = Field(default="", max_length=1500)
    score: float = Field(ge=0, le=10)
    operations: list[AIOperation] = Field(default_factory=list, max_length=40)


class EditorialGuide(BaseModel):
    schema_version: str = AI_SCHEMA_VERSION
    rules: list[str] = Field(default_factory=list, max_length=30)
    heading_policy: str = Field(default="", max_length=800)
    global_props: list[KV] = Field(default_factory=list, max_length=12)
    notes: str = Field(default="", max_length=1500)


class EditorialResponse(BaseModel):
    schema_version: str = AI_SCHEMA_VERSION
    summary: str = Field(default="", max_length=1500)
    operations: list[AIOperation] = Field(default_factory=list, max_length=80)


def _op_json_schema(allow_text: bool) -> dict:
    ops = sorted(AI_ALLOWED_OPS | (AI_TEXT_OPS if allow_text else set()))
    kv = {"type": "object", "properties": {"name": {"type": "string"}, "value": {"type": "string"}},
          "required": ["name", "value"]}
    props = {
        "op": {"type": "string", "enum": ops},
        "target_ids": {"type": "array", "items": {"type": "string"}},
        "level": {"type": "integer", "minimum": 1, "maximum": 6},
        "type": {"type": "string", "enum": ["heading", "paragraph", "list_item", "blockquote", "caption"]},
        "style": {"type": "array", "items": kv, "description": "tokens: " + "; ".join(
            f"{k}={'|'.join(v)}" for k, v in STYLE_TOKENS.items())},
        "global_props": {"type": "array", "items": kv, "description": "; ".join(
            f"{k}: {'|'.join(v['enum']) if 'enum' in v else str(v['min']) + '..' + str(v['max'])}"
            for k, v in GLOBAL_STYLE_RULES.items())},
        "caption_id": {"type": "string"}, "before_id": {"type": "string"}, "after_id": {"type": "string"},
        "offset": {"type": "integer"}, "alt": {"type": "string"}, "value": {"type": "boolean"},
        "reason": {"type": "string"}, "evidence": {"type": "string"}, "risk": {"type": "string"},
    }
    if allow_text:
        props["text"] = {"type": "string"}
    return {"type": "object", "properties": props, "required": ["op", "target_ids", "reason"]}


def visual_schema() -> dict:
    return {"type": "object", "properties": {
        "schema_version": {"type": "string"}, "diagnosis": {"type": "string"},
        "score": {"type": "number", "minimum": 0, "maximum": 10},
        "operations": {"type": "array", "items": _op_json_schema(False)}},
        "required": ["diagnosis", "score", "operations"]}


def guide_schema() -> dict:
    kv = {"type": "object", "properties": {"name": {"type": "string"}, "value": {"type": "string"}},
          "required": ["name", "value"]}
    return {"type": "object", "properties": {
        "rules": {"type": "array", "items": {"type": "string"}}, "heading_policy": {"type": "string"},
        "global_props": {"type": "array", "items": kv}, "notes": {"type": "string"}},
        "required": ["rules", "heading_policy", "global_props"]}


def editorial_schema(allow_text: bool) -> dict:
    return {"type": "object", "properties": {
        "summary": {"type": "string"}, "operations": {"type": "array", "items": _op_json_schema(allow_text)}},
        "required": ["summary", "operations"]}


class ConversionError(ValueError):
    pass


def to_engine_op(a: AIOperation) -> dict:
    """Converte a operação achatada da IA em operação tipada do motor (validada)."""
    t = a.target_ids
    base = {"reason": (a.reason or "")[:400]}

    def one():
        if len(t) != 1:
            raise ConversionError(f"'{a.op}' exige exatamente um alvo")
        return t[0]

    if a.op == "set_style":
        d = {"op": "set_style", "ids": t, "style": {kv.name: kv.value for kv in a.style}}
    elif a.op == "set_global_style":
        props = {}
        for kv in a.global_props:
            rule = GLOBAL_STYLE_RULES.get(kv.name)
            if rule and "enum" not in rule:
                try:
                    props[kv.name] = float(kv.value)
                except ValueError:
                    raise ConversionError(f"valor numérico inválido para {kv.name}") from None
            else:
                props[kv.name] = kv.value
        d = {"op": "set_global_style", "props": props}
    elif a.op == "set_heading_level":
        d = {"op": "set_heading_level", "id": one(), "level": a.level}
    elif a.op == "set_type":
        d = {"op": "set_type", "id": one(), "type": a.type}
    elif a.op == "link_caption":
        d = {"op": "link_caption", "figure_id": one(), "caption_id": a.caption_id}
    elif a.op == "move":
        d = {"op": "move", "ids": t, "before_id": a.before_id, "after_id": a.after_id}
    elif a.op == "set_chapter_break":
        d = {"op": "set_chapter_break", "id": one(), "value": bool(a.value)}
    elif a.op == "split":
        d = {"op": "split", "id": one(), "offset": a.offset}
    elif a.op == "merge":
        d = {"op": "merge", "ids": t}
    elif a.op == "set_alt":
        d = {"op": "set_alt", "id": one(), "alt": a.alt or ""}
    elif a.op == "set_text":
        d = {"op": "set_text", "id": one(), "text": a.text or ""}
    else:
        raise ConversionError(f"operação não permitida: {a.op}")
    d.update(base)
    try:
        OperationList.validate_python([d])
    except ValidationError as exc:
        raise ConversionError(f"operação malformada: {exc.errors()[0].get('msg')}") from None
    return d
