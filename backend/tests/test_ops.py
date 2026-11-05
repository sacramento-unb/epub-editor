"""Testes unitários das regras do modelo documental e das operações."""
import pytest

from app.engine.model import Document, Element, PageInfo, SourceRegion
from app.engine.ops import OpError, apply_ops, check_ai_op, exclusion_impact, OperationList


def mkdoc():
    pg = PageInfo(index=0, width=600, height=800, mediabox=(0, 0, 600, 800), cropbox=(0, 0, 600, 800))
    els = [
        Element(id="h1", type="heading", level=1, text="Título", sources=[SourceRegion(page=0, bbox=(10, 10, 200, 30))]),
        Element(id="p1", type="paragraph", text="Primeiro parágrafo.", sources=[SourceRegion(page=0, bbox=(10, 40, 500, 60))]),
        Element(id="p2", type="paragraph", text="Segundo parágrafo.", sources=[SourceRegion(page=0, bbox=(10, 70, 500, 90))]),
        Element(id="fig", type="image", asset="x.png", sources=[SourceRegion(page=0, bbox=(10, 100, 300, 300))],
                caption_id="cap"),
        Element(id="cap", type="caption", text="Figura 1 – X", sources=[SourceRegion(page=0, bbox=(10, 305, 300, 320))]),
        Element(id="hdr", type="header", text="Cabeçalho", included=False, sources=[SourceRegion(page=0, bbox=(10, 0, 100, 8))]),
    ]
    return Document(pages=[pg], elements=els)


def test_batch_include_respects_explicit_exclusion():
    d = mkdoc()
    d = apply_ops(d, [{"op": "set_included", "ids": ["p1"], "included": False, "mode": "item"}])
    assert d.get("p1").excluded_explicit
    d = apply_ops(d, [{"op": "set_included", "ids": ["p1", "p2", "hdr"], "included": True, "mode": "batch"}])
    assert not d.get("p1").included, "lote não pode reincluir exclusão explícita"
    assert d.get("hdr").included
    d = apply_ops(d, [{"op": "set_included", "ids": ["p1"], "included": True, "mode": "item"}])
    assert d.get("p1").included and not d.get("p1").excluded_explicit


def test_group_exclusion_propagates_and_children_keep_explicit():
    d = mkdoc()
    d = apply_ops(d, [{"op": "group", "ids": ["fig", "cap"], "type": "figure", "new_id": "g1"}])
    assert d.get("fig").parent_id == "g1"
    d = apply_ops(d, [{"op": "set_included", "ids": ["cap"], "included": False}])
    d = apply_ops(d, [{"op": "set_included", "ids": ["g1"], "included": False, "mode": "batch"}])
    assert not d.get("fig").included
    d = apply_ops(d, [{"op": "set_included", "ids": ["g1"], "included": True, "mode": "batch"}])
    assert d.get("fig").included and not d.get("cap").included


def test_ai_cannot_touch_excluded_or_locked_or_reinclude():
    d = mkdoc()
    d = apply_ops(d, [{"op": "set_included", "ids": ["p2"], "included": False},
                      {"op": "set_locked", "ids": ["p1"], "locked": True}])
    for op in ({"op": "set_included", "ids": ["p2"], "included": True},
               {"op": "set_style", "ids": ["p2"], "style": {"align": "center"}},
               {"op": "set_style", "ids": ["p1"], "style": {"align": "center"}},
               {"op": "set_text", "id": "h1", "text": "Outro"},
               {"op": "set_style", "ids": ["inexistente"], "style": {"align": "center"}}):
        with pytest.raises(OpError):
            apply_ops(d, [op], actor="ai")
    # permitido
    d2 = apply_ops(d, [{"op": "set_style", "ids": ["h1"], "style": {"align": "center"}}], actor="ai")
    assert d2.get("h1").output_style["align"] == "center"
    # texto só com correção de OCR habilitada
    d3 = apply_ops(d, [{"op": "set_text", "id": "h1", "text": "Título."}], actor="ai", allow_text=True)
    assert d3.get("h1").text == "Título."


def test_ai_scope_restriction():
    d = mkdoc()
    op = OperationList.validate_python([{"op": "set_style", "ids": ["p1"], "style": {"align": "left"}}])[0]
    with pytest.raises(OpError):
        check_ai_op(d, op, scope_ids={"h1"})


def test_style_tokens_validated():
    d = mkdoc()
    with pytest.raises(OpError):
        apply_ops(d, [{"op": "set_style", "ids": ["p1"], "style": {"position": "absolute"}}])
    with pytest.raises(OpError):
        apply_ops(d, [{"op": "set_style", "ids": ["p1"], "style": {"align": "javascript:alert(1)"}}])
    with pytest.raises(OpError):
        apply_ops(d, [{"op": "set_global_style", "props": {"line_height": 9}}])


def test_split_merge_keep_provenance():
    d = mkdoc()
    d = apply_ops(d, [{"op": "split", "id": "p1", "offset": 9, "new_id": "p1b"}])
    assert d.get("p1").text == "Primeiro" and d.get("p1b").text == "parágrafo."
    assert "p1" in d.get("p1b").provenance
    d = apply_ops(d, [{"op": "merge", "ids": ["p1", "p1b"]}])
    assert d.get("p1").text == "Primeiro parágrafo." and d.get("p1b") is None
    assert "p1b" in d.get("p1").provenance
    with pytest.raises(OpError):
        apply_ops(d, [{"op": "merge", "ids": ["p1", "fig"]}])  # não adjacentes/sem texto


def test_atomicity():
    d = mkdoc()
    with pytest.raises(OpError):
        apply_ops(d, [{"op": "set_style", "ids": ["p1"], "style": {"align": "center"}},
                      {"op": "set_heading_level", "id": "p1", "level": 2}])
    assert d.get("p1").output_style == {}


def test_move_and_impact():
    d = mkdoc()
    d = apply_ops(d, [{"op": "move", "ids": ["p2"], "before_id": "p1"}])
    assert [e.id for e in d.elements][:3] == ["h1", "p2", "p1"]
    imp = exclusion_impact(d, ["cap"])
    assert any(i["kind"] == "legenda" for i in imp)
    imp = exclusion_impact(d, ["fig"])
    assert any(i["kind"] == "legenda órfã" for i in imp)


def test_create_region_clamped():
    d = mkdoc()
    d = apply_ops(d, [{"op": "create_region", "page": 0, "bbox": (-10, 50, 700, 120), "type": "figure", "new_id": "r1"}])
    assert d.get("r1").sources[0].bbox == (0, 50, 600, 120)


def test_impact_noteref():
    d = mkdoc()
    from app.engine.model import Element, InlineMark, SourceRegion

    d.elements.append(Element(id="n1", type="footnote", note_id="1", text="1 Nota.",
                              sources=[SourceRegion(page=0, bbox=(10, 700, 300, 710))]))
    d.get("p1").marks.append(InlineMark(start=0, end=1, kind="noteref", href="n1"))
    assert any(i["kind"] == "nota" for i in exclusion_impact(d, ["n1"]))
