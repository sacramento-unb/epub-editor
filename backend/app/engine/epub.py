"""Geração de EPUB 3 (perfil EPUB 3.3) a partir do modelo documental.

- XHTML gerado com lxml (serialização XML real, nunca HTML tolerado).
- OCF: ``mimetype`` primeiro, sem compressão; demais entradas com deflate.
- Modo fluido (padrão) e layout fixo (``rendition:layout pre-paginated``).
"""
from __future__ import annotations

import hashlib
import html
import re
import shutil
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from lxml import etree

from .extract import BULLET_RE
from .model import Document, Element, InlineMark

XHTML_NS = "http://www.w3.org/1999/xhtml"
EPUB_NS = "http://www.idpf.org/2007/ops"
NSMAP = {None: XHTML_NS, "epub": EPUB_NS}
_INVALID_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
VISUAL = {"image", "figure", "formula", "region", "cover"}
EPUB_PROFILE = "EPUB 3.3 (package version 3.0)"


@dataclass
class BuildResult:
    path: Path
    mode: str
    mapping: dict[str, dict] = field(default_factory=dict)  # element id -> {file, anchor}
    files: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    spine: list[str] = field(default_factory=list)


AssetFn = Callable[[Element], Path | None]


def _clean(t: str | None) -> str:
    return _INVALID_XML.sub("", t or "")


def _E(tag, attrib=None, text=None):
    el = etree.Element(f"{{{XHTML_NS}}}{tag}", nsmap=None)
    for k, v in (attrib or {}).items():
        if k.startswith("epub:"):
            el.set(f"{{{EPUB_NS}}}{k[5:]}", v)
        else:
            el.set(k, v)
    if text:
        el.text = _clean(text)
    return el


def _sub(parent, tag, attrib=None, text=None):
    el = _E(tag, attrib, text)
    parent.append(el)
    return el


def _new_doc(title: str, lang: str, css_href: str, viewport: str | None = None):
    root = etree.Element(f"{{{XHTML_NS}}}html", nsmap=NSMAP)
    root.set("lang", lang)
    root.set("{http://www.w3.org/XML/1998/namespace}lang", lang)
    head = _sub(root, "head")
    _sub(head, "meta", {"charset": "utf-8"})
    if viewport:
        _sub(head, "meta", {"name": "viewport", "content": viewport})
    _sub(head, "title", text=title)
    _sub(head, "link", {"rel": "stylesheet", "type": "text/css", "href": css_href})
    body = _sub(root, "body")
    return root, body


def _serialize(root) -> bytes:
    return (b'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE html>\n' +
            etree.tostring(root, encoding="utf-8", pretty_print=True))


def _style_classes(e: Element) -> str:
    cls = [f"t-{e.type}"]
    for k, v in sorted(e.output_style.items()):
        cls.append(f"s-{k.replace('_', '-')}-{v}")
    if e.subtype and re.fullmatch(r"[a-z0-9-]+", e.subtype):
        cls.append(f"st-{e.subtype}")
    return " ".join(cls)


def strip_list_marker(text: str) -> tuple[str, str]:
    """Remove o marcador visual de item de lista. Retorna (texto, tipo de lista)."""
    m = BULLET_RE.match(text)
    if not m:
        return text, "ul"
    marker = m.group(1)
    kind = "ol" if re.match(r"\(?\d", marker) else "ul"
    return text[m.end():], kind


def list_marker_number(text: str) -> int | None:
    m = re.match(r"^\s*\(?(\d{1,4})[.)]", text or "")
    return int(m.group(1)) if m else None


def render_inline(parent, text: str, marks: list[InlineMark], resolve_href: Callable[[InlineMark], str | None],
                  offset: int = 0, noteref_ids: dict | None = None):
    """Insere ``text`` com marcas inline em ``parent`` (segmentando por fronteiras)."""
    text = _clean(text)
    bounds = {0, len(text)}
    ms = [InlineMark(start=m.start - offset, end=m.end - offset, kind=m.kind, href=m.href)
          for m in marks if m.end - offset > 0 and m.start - offset < len(text)]
    for m in ms:
        bounds.add(max(0, m.start))
        bounds.add(min(len(text), m.end))
    pts = sorted(bounds)
    last = None
    for a, b in zip(pts, pts[1:]):
        seg = text[a:b]
        if not seg:
            continue
        active = [m for m in ms if m.start <= a and m.end >= b]
        node = None
        outer = None
        for m in sorted(active, key=lambda m: ["link", "noteref", "bold", "italic", "sup", "sub"].index(m.kind)):
            tag, attrib = None, {}
            if m.kind in ("link", "noteref"):
                href = resolve_href(m)
                if not href:
                    continue
                tag = "a"
                attrib = {"href": href}
                if m.kind == "noteref":
                    attrib["epub:type"] = "noteref"
                    attrib["role"] = "doc-noteref"
                    attrib["class"] = "noteref"
                    if noteref_ids is not None:
                        rid = f"ref-{m.href}-{len(noteref_ids)}"
                        noteref_ids.setdefault(m.href, rid)
                        if noteref_ids[m.href] == rid:
                            attrib["id"] = rid
            elif m.kind == "bold":
                tag = "strong"
            elif m.kind == "italic":
                tag = "em"
            elif m.kind in ("sup", "sub"):
                tag = m.kind
            if tag is None:
                continue
            child = _E(tag, attrib)
            if node is None:
                outer = child
            else:
                node.append(child)
            node = child
        if node is None:
            if last is None:
                parent.text = (parent.text or "") + seg
            else:
                last.tail = (last.tail or "") + seg
        else:
            node.text = seg
            parent.append(outer)
            last = outer


DEFAULT_CSS = """@charset "UTF-8";
/* Gerado pelo Conversor PDF→EPUB. Estilos editoriais derivados do modelo documental. */
html { -epub-hyphens: VAR_HYPHENS; hyphens: VAR_HYPHENS; }
body { font-family: VAR_BODY_FONT; line-height: VAR_LINE_HEIGHT; margin: 0 VAR_MARGINem; }
p { margin: 0 0 VAR_PSPACEem 0; text-indent: VAR_INDENTem; text-align: VAR_ALIGN; orphans: 2; widows: 2; }
h1, h2, h3, h4, h5, h6 { font-family: VAR_HEAD_FONT; line-height: 1.2; text-align: left; page-break-after: avoid; break-after: avoid; hyphens: none; -epub-hyphens: none; }
h1 { font-size: calc(1em * VAR_SCALE * VAR_SCALE * VAR_SCALE); margin: 1.2em 0 0.8em; }
h2 { font-size: calc(1em * VAR_SCALE * VAR_SCALE); margin: 1.1em 0 0.6em; }
h3 { font-size: calc(1em * VAR_SCALE); margin: 1em 0 0.5em; }
h4, h5, h6 { font-size: 1em; margin: 1em 0 0.4em; }
h1 + p, h2 + p, h3 + p, h4 + p, h5 + p, h6 + p, figure + p, ul + p, ol + p, table + p { text-indent: 0; }
ul, ol { margin: 0.5em 0 0.8em 1.5em; padding: 0; }
li { margin: 0.2em 0; text-align: left; }
blockquote { margin: 1em 1.5em; font-style: italic; }
figure { margin: 1em 0; text-align: center; page-break-inside: avoid; break-inside: avoid; }
figure img { max-width: VAR_IMGMAX%; height: auto; }
figcaption, p.t-caption { font-size: 0.9em; text-align: center; text-indent: 0; margin-top: 0.4em; }
figure.t-formula img { max-width: 100%; }
table { border-collapse: collapse; margin: 1em auto; font-size: 0.9em; max-width: 100%; }
caption { caption-side: bottom; font-size: 0.9em; padding-top: 0.4em; }
th, td { border: 1px solid #888; padding: 0.25em 0.5em; text-align: left; vertical-align: top; }
th { font-weight: bold; }
aside.footnote, aside.endnote { font-size: 0.85em; }
section.notes { margin-top: 2em; border-top: 1px solid #999; padding-top: 0.5em; }
section.notes p { text-indent: 0; }
a.backlink { text-decoration: none; }
a.noteref { vertical-align: super; font-size: 0.75em; line-height: 0; text-decoration: none; }
p.t-running, p.t-toc { text-indent: 0; font-size: 0.9em; }
nav.toc-inline ol { list-style: none; margin: 1em 0; padding: 0; }
nav.toc-inline li { margin: 0.5em 0; text-align: left; }
nav.toc-inline a { text-decoration: none; }
span.pagebreak { display: none; }
.s-align-left { text-align: left; } .s-align-center { text-align: center; text-indent: 0; }
.s-align-right { text-align: right; } .s-align-justify { text-align: justify; }
.s-emphasis-bold { font-weight: bold; } .s-emphasis-italic { font-style: italic; }
.s-emphasis-bold-italic { font-weight: bold; font-style: italic; } .s-emphasis-small-caps { font-variant: small-caps; }
.s-emphasis-none { font-weight: normal; font-style: normal; }
.s-size-xsmall { font-size: 0.75em; } .s-size-small { font-size: 0.875em; } .s-size-normal { font-size: 1em; }
.s-size-large { font-size: 1.25em; } .s-size-xlarge { font-size: 1.5em; }
.s-spacing-before-none { margin-top: 0; } .s-spacing-before-small { margin-top: 0.4em; }
.s-spacing-before-normal { margin-top: 1em; } .s-spacing-before-large { margin-top: 2em; }
.s-spacing-after-none { margin-bottom: 0; } .s-spacing-after-small { margin-bottom: 0.4em; }
.s-spacing-after-normal { margin-bottom: 1em; } .s-spacing-after-large { margin-bottom: 2em; }
.s-indent-none { text-indent: 0; } .s-indent-first-line { text-indent: 1.5em; } .s-indent-block { margin-left: 2em; text-indent: 0; }
.s-width-25 img { width: 25%; } .s-width-50 img { width: 50%; } .s-width-75 img { width: 75%; } .s-width-100 img { width: 100%; }
.s-list-style-disc { list-style-type: disc; } .s-list-style-decimal { list-style-type: decimal; } .s-list-style-none { list-style-type: none; }
.s-page-break-before-always { page-break-before: always; break-before: page; }
"""

FIXED_CSS = """@charset "UTF-8";
html, body { margin: 0; padding: 0; }
body { position: relative; overflow: hidden; }
.fx { position: absolute; margin: 0; padding: 0; line-height: 1.15; white-space: normal; overflow: visible; }
.fx img { width: 100%; height: 100%; display: block; }
.fx table { border-collapse: collapse; width: 100%; height: 100%; font-size: inherit; }
.fx td, .fx th { border: 1px solid #888; padding: 1px 3px; text-align: left; }
.fx h1, .fx h2, .fx h3, .fx h4, .fx h5, .fx h6, .fx p { margin: 0; font-size: inherit; }
"""

FIXED_CSS += "\n".join(l for l in DEFAULT_CSS.splitlines() if l.startswith(".s-")) + "\n"

FONT_STACK = {"serif": 'Georgia, "Times New Roman", serif', "sans-serif": '"Helvetica Neue", Arial, sans-serif',
              "monospace": '"Courier New", monospace'}


def build_css(doc: Document) -> str:
    g = doc.global_style
    rep = {
        "VAR_HYPHENS": g.get("hyphens", "manual"), "VAR_BODY_FONT": FONT_STACK[g.get("body_font", "serif")],
        "VAR_HEAD_FONT": FONT_STACK[g.get("heading_font", "sans-serif")],
        "VAR_LINE_HEIGHT": f"{float(g.get('line_height', 1.5)):.2f}", "VAR_MARGIN": f"{float(g.get('margin_em', 1)):.2f}",
        "VAR_PSPACE": f"{float(g.get('paragraph_spacing_em', 0)):.2f}", "VAR_INDENT": f"{float(g.get('text_indent_em', 1.2)):.2f}",
        "VAR_ALIGN": g.get("text_align", "justify"), "VAR_SCALE": f"{float(g.get('heading_scale', 1.25)):.3f}",
        "VAR_IMGMAX": f"{int(float(g.get('image_max_width_pct', 100)))}",
    }
    css = DEFAULT_CSS
    for k, v in rep.items():
        css = css.replace(k, v)
    return css


def output_elements(doc: Document) -> list[Element]:
    """Elementos que entram na saída, excluindo filhos cobertos por um visual incluído."""
    by = doc.by_id()
    out = []
    for e in doc.elements:
        if not e.included:
            continue
        p = by.get(e.parent_id) if e.parent_id else None
        skip = False
        while p is not None:
            if not p.included:
                skip = True  # pai excluído exclui o grupo inteiro
                break
            if p.type in VISUAL:
                skip = True  # coberto pelo recorte do pai
                break
            p = by.get(p.parent_id) if p.parent_id else None
        if not skip:
            out.append(e)
    return out


def build_epub(doc: Document, out_path: Path, assets_dir: Path, *, mode: str = "fluid",
               asset_fn: AssetFn | None = None, identifier: str | None = None,
               modified: datetime | None = None) -> BuildResult:
    if mode not in ("fluid", "fixed"):
        raise ValueError("modo deve ser 'fluid' ou 'fixed'")
    res = BuildResult(path=out_path, mode=mode)
    files: dict[str, bytes] = {}
    manifest: list[dict] = []
    lang = doc.metadata.language or "pt-BR"
    els = output_elements(doc)
    by = doc.by_id()
    included_ids = {e.id for e in els}

    # Recursos de imagem
    images: dict[str, str] = {}  # element id -> href
    for e in els:
        if e.type in VISUAL:
            src = asset_fn(e) if asset_fn else (assets_dir / e.asset if e.asset else None)
            if src is None or not Path(src).is_file():
                res.warnings.append(f"Elemento {e.id} ({e.type}) sem imagem disponível; omitido.")
                continue
            data = Path(src).read_bytes()
            ext = Path(src).suffix.lower().lstrip(".") or "png"
            name = f"images/{e.id}.{ext}"
            files[f"OEBPS/{name}"] = data
            mt = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif",
                  "webp": "image/webp", "svg": "image/svg+xml"}.get(ext, "image/png")
            item = {"id": f"img-{e.id}", "href": name, "media-type": mt}
            if e.type == "cover":
                item["properties"] = "cover-image"
            manifest.append(item)
            images[e.id] = name

    if mode == "fluid":
        chapters = _fluid_chapters(doc, els, images, res)
    else:
        chapters = _fixed_pages(doc, els, images, res)

    # Mapeamento elemento -> arquivo (para links internos e notas)
    for fname, members in chapters:
        for e in members:
            res.mapping[e.id] = {"file": fname, "anchor": e.anchor}

    first_on_page: dict[int, Element] = {}
    for fname, members in chapters:
        for e in members:
            if e.sources and e.type not in ("footnote",):
                first_on_page.setdefault(e.sources[0].page, e)

    def rel(target: str, cur_file: str) -> str:
        import posixpath

        if target == cur_file:
            return ""
        return posixpath.relpath(target, posixpath.dirname(cur_file) or ".")

    def resolve(m: InlineMark, cur_file: str) -> str | None:
        href = m.href or ""
        if m.kind == "noteref":
            tgt = res.mapping.get(href)
            if not tgt:
                return None
            return rel(tgt["file"], cur_file) + "#" + tgt["anchor"]
        if href.startswith("#page-"):
            try:
                page = int(href[6:])
            except ValueError:
                return None
            tgt_el = first_on_page.get(page)
            if not tgt_el:
                res.warnings.append(f"Link interno para a página {page + 1} sem conteúdo exportado; removido.")
                return None
            t = res.mapping[tgt_el.id]
            return rel(t["file"], cur_file) + "#" + t["anchor"]
        if href.startswith("#e-"):
            t = res.mapping.get(href[3:])
            if not t:
                res.warnings.append(f"Link interno para elemento excluído ({href[3:]}); removido.")
                return None
            return rel(t["file"], cur_file) + "#" + t["anchor"]
        if re.match(r"^(https?|mailto):", href, re.I):
            return href
        return None

    css = build_css(doc) if mode == "fluid" else FIXED_CSS
    files["OEBPS/styles/book.css"] = css.encode()
    manifest.append({"id": "css", "href": "styles/book.css", "media-type": "text/css"})

    cover = next((e for e in els if e.type == "cover" and e.id in images), None)
    spine: list[dict] = []
    if cover and mode == "fluid":
        root, body = _new_doc(doc.metadata.title, lang, "styles/book.css")
        body.set(f"{{{EPUB_NS}}}type", "cover")
        sec = _sub(body, "section", {"epub:type": "cover"})
        # role="doc-cover" só é válido na imagem (DPUB-ARIA)
        _sub(_sub(sec, "figure"), "img", {"src": images[cover.id], "role": "doc-cover",
                                          "alt": _clean(cover.alt or f"Capa: {doc.metadata.title}")})
        files["OEBPS/cover.xhtml"] = _serialize(root)
        manifest.append({"id": "cover", "href": "cover.xhtml", "media-type": "application/xhtml+xml"})
        spine.append({"idref": "cover", "linear": "yes"})
        res.mapping[cover.id] = {"file": "cover.xhtml", "anchor": cover.anchor}

    toc_entries: list[tuple[int, str, str]] = []  # level, title, href
    page_list: list[tuple[str, str]] = []
    for idx, (fname, members) in enumerate(chapters):
        if mode == "fluid":
            root = _render_fluid_chapter(doc, fname, members, images, by, included_ids, resolve, res,
                                         toc_entries, page_list, lang)
            props = None
        else:
            root, props = _render_fixed_page(doc, fname, members, images, resolve, res, toc_entries, lang)
        files[f"OEBPS/{fname}"] = _serialize(root)
        item = {"id": f"x{idx:04d}", "href": fname, "media-type": "application/xhtml+xml"}
        if props:
            item["properties"] = props
        manifest.append(item)
        spine.append({"idref": item["id"], "linear": "yes"})

    if not chapters:
        root, body = _new_doc(doc.metadata.title, lang, "styles/book.css")
        _sub(body, "p", text="(Nenhum elemento selecionado para exportação.)")
        files["OEBPS/empty.xhtml"] = _serialize(root)
        manifest.append({"id": "empty", "href": "empty.xhtml", "media-type": "application/xhtml+xml"})
        spine.append({"idref": "empty", "linear": "yes"})
        res.warnings.append("Nenhum elemento selecionado: EPUB gerado com página vazia.")
        first_file = "empty.xhtml"
    else:
        first_file = chapters[0][0]

    # Navegação
    nav_root, nav_body = _new_doc("Sumário", lang, "styles/book.css")
    nav = _sub(nav_body, "nav", {"epub:type": "toc", "id": "toc", "role": "doc-toc"})
    _sub(nav, "h1", text="Sumário")
    if not toc_entries:
        toc_entries.append((1, doc.metadata.title, first_file))
    _nested_list(nav, toc_entries)
    lm = _sub(nav_body, "nav", {"epub:type": "landmarks", "id": "landmarks", "hidden": "hidden"})
    _sub(lm, "h2", text="Pontos de referência")
    lol = _sub(lm, "ol")
    _sub(_sub(lol, "li"), "a", {"epub:type": "toc", "href": "nav.xhtml#toc"}, "Sumário")
    _sub(_sub(lol, "li"), "a", {"epub:type": "bodymatter", "href": first_file}, "Início")
    if page_list:
        pl = _sub(nav_body, "nav", {"epub:type": "page-list", "id": "page-list", "hidden": "hidden"})
        _sub(pl, "h2", text="Páginas do PDF original")
        pol = _sub(pl, "ol")
        for label, href in page_list:
            _sub(_sub(pol, "li"), "a", {"href": href}, label)
    files["OEBPS/nav.xhtml"] = _serialize(nav_root)
    manifest.append({"id": "nav", "href": "nav.xhtml", "media-type": "application/xhtml+xml", "properties": "nav"})
    nav_ref = {"idref": "nav", "linear": "no"}
    if mode == "fixed":
        nav_ref["properties"] = "rendition:layout-reflowable"
    spine.append(nav_ref)

    ident = identifier or f"urn:uuid:{uuid.uuid4()}"
    files["OEBPS/toc.ncx"] = _ncx(doc, ident, toc_entries)
    manifest.append({"id": "ncx", "href": "toc.ncx", "media-type": "application/x-dtbncx+xml"})

    files["OEBPS/content.opf"] = _opf(doc, ident, manifest, spine, mode, modified)
    files["META-INF/container.xml"] = (
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        b'<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
        b'  <rootfiles>\n    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>\n'
        b'  </rootfiles>\n</container>\n')

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".tmp")
    with zipfile.ZipFile(tmp, "w") as z:
        zi = zipfile.ZipInfo("mimetype", date_time=(2020, 1, 1, 0, 0, 0))
        zi.compress_type = zipfile.ZIP_STORED
        z.writestr(zi, b"application/epub+zip")
        for name in sorted(files, key=lambda n: (not n.startswith("META-INF"), n)):
            zi = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, files[name])
    shutil.move(tmp, out_path)
    res.files = sorted(files)
    res.spine = (["cover.xhtml"] if cover and mode == "fluid" else []) + [c[0] for c in chapters]
    return res


def _nested_list(parent, entries):
    root_ol = _sub(parent, "ol")
    stack = [(entries[0][0] if entries else 1, root_ol)]
    last_li = None
    for level, title, href in entries:
        while len(stack) > 1 and level < stack[-1][0]:
            stack.pop()
        if level > stack[-1][0] and last_li is not None:
            stack.append((level, _sub(last_li, "ol")))
        li = _sub(stack[-1][1], "li")
        _sub(li, "a", {"href": href}, title or "(sem título)")
        last_li = li


def _fluid_chapters(doc, els, images, res) -> list[tuple[str, list[Element]]]:
    chapters: list[tuple[str, list[Element]]] = []
    cur: list[Element] = []
    for e in els:
        if e.type == "cover":
            continue
        if e.chapter_break and cur:
            chapters.append((f"text/ch{len(chapters) + 1:03d}.xhtml", cur))
            cur = []
        cur.append(e)
    if cur:
        chapters.append((f"text/ch{len(chapters) + 1:03d}.xhtml", cur))
    return chapters


def _fixed_pages(doc, els, images, res) -> list[tuple[str, list[Element]]]:
    pages: dict[int, list[Element]] = {}
    for e in els:
        if not e.sources:
            continue
        pages.setdefault(e.sources[0].page, []).append(e)
        for s in e.sources[1:]:
            if s.page != e.sources[0].page:
                res.warnings.append(f"Elemento {e.id} atravessa páginas; no layout fixo fica na página "
                                    f"{e.sources[0].page + 1}.")
    return [(f"text/page{p + 1:04d}.xhtml", pages[p]) for p in sorted(pages)]


def _render_fluid_chapter(doc, fname, members, images, by, included_ids, resolve, res, toc_entries,
                          page_list, lang):
    title = next((e.text for e in members if e.type == "heading"), None) or doc.metadata.title
    root, body = _new_doc(_clean(title)[:200], lang, "../styles/book.css")
    section = _sub(body, "section", {"epub:type": "chapter", "role": "doc-chapter"})
    notes: list[Element] = []
    noteref_ids: dict[str, str] = {}
    captions_used = {e.caption_id for e in members if e.caption_id and e.type in VISUAL | {"table"}}
    seen_pages: set[int] = set()
    list_el = None
    list_kind = None
    toc_el = None
    rel = lambda m: (lambda h: h)(resolve(m, fname))  # noqa: E731
    for e in members:
        if e.sources and doc.pages:
            for s in e.sources[:1]:
                if s.page not in seen_pages and e.type not in ("footnote",):
                    seen_pages.add(s.page)
                    pid = f"pg-{s.page + 1}"
                    if not any(pid == x[1].split("#")[-1] for x in page_list):
                        span = _E("span", {"id": pid, "epub:type": "pagebreak", "role": "doc-pagebreak",
                                           "aria-label": str(s.page + 1), "class": "pagebreak"})
                        section.append(span)
                        page_list.append((str(s.page + 1), f"{fname}#{pid}"))
        if e.type != "list_item":
            list_el = None
        if not (e.type == "toc" and e.subtype == "toc-entry"):
            toc_el = None
        if e.type == "footnote":
            notes.append(e)
            continue
        if e.type == "caption" and e.id in captions_used:
            continue
        cls = _style_classes(e)
        if e.type == "heading":
            lvl = max(1, min(6, e.level or 2))
            h = _sub(section, f"h{lvl}", {"id": e.anchor, "class": cls})
            render_inline(h, e.text or "", e.marks, rel, noteref_ids=noteref_ids)
            toc_entries.append((lvl, _clean(e.text or ""), f"{fname}#{e.anchor}"))
        elif e.type == "list_item":
            text, kind = strip_list_marker(e.text or "")
            offset = len(e.text or "") - len(text)
            if list_el is None or kind != list_kind:
                list_el = _sub(section, kind, {"class": "t-list"})
                list_kind = kind
            attrs = {"id": e.anchor, "class": cls}
            num = list_marker_number(e.text or "") if kind == "ol" else None
            if num is not None:
                attrs["value"] = str(num)  # preserva a numeração original (ex.: versículos 18–22)
            li = _sub(list_el, "li", attrs)
            render_inline(li, text, e.marks, rel, offset=offset, noteref_ids=noteref_ids)
        elif e.type == "toc" and e.subtype == "toc-entry":
            if toc_el is None:
                nav = _sub(section, "nav", {"class": "toc-inline", "aria-label": "Sumário"})
                toc_el = _sub(nav, "ol", {"class": "toc-list"})
            li = _sub(toc_el, "li", {"id": e.anchor, "class": cls})
            render_inline(li, e.text or "", e.marks, rel, noteref_ids=noteref_ids)
        elif e.type == "blockquote":
            bq = _sub(section, "blockquote", {"id": e.anchor, "class": cls})
            render_inline(_sub(bq, "p"), e.text or "", e.marks, rel, noteref_ids=noteref_ids)
        elif e.type in VISUAL:
            if e.id not in images:
                continue
            fig = _sub(section, "figure", {"id": e.anchor, "class": cls})
            alt = e.alt or ""
            if e.type == "formula" and not alt:
                alt = f"Fórmula (imagem): {e.text or ''}".strip()
            _sub(fig, "img", {"src": "../" + images[e.id], "alt": _clean(alt)})
            if e.type == "formula" and e.text:
                # equivalente textual pesquisável, sinalizado como extração não verificada
                _sub(fig, "span", {"class": "formula-text", "hidden": "hidden"}, e.text)
            cap = by.get(e.caption_id) if e.caption_id else None
            if cap is not None and cap.id in included_ids:
                fc = _sub(fig, "figcaption", {"id": cap.anchor, "class": _style_classes(cap)})
                render_inline(fc, cap.text or "", cap.marks, rel, noteref_ids=noteref_ids)
                res.mapping[cap.id] = {"file": fname, "anchor": cap.anchor}
        elif e.type == "table":
            tbl = _sub(section, "table", {"id": e.anchor, "class": cls})
            cap = by.get(e.caption_id) if e.caption_id else None
            if cap is not None and cap.id in included_ids:
                c = _sub(tbl, "caption", {"id": cap.anchor})
                render_inline(c, cap.text or "", cap.marks, rel)
                res.mapping[cap.id] = {"file": fname, "anchor": cap.anchor}
            rows = e.table or []
            head = [r for r in rows if r and all(c.header for c in r)]
            bodyrows = [r for r in rows if r not in head]
            if head:
                thead = _sub(tbl, "thead")
                for r in head:
                    tr = _sub(thead, "tr")
                    for c in r:
                        a = {"scope": "col"}
                        if c.colspan > 1:
                            a["colspan"] = str(c.colspan)
                        if c.rowspan > 1:
                            a["rowspan"] = str(c.rowspan)
                        _sub(tr, "th", a, c.text)
            tb = _sub(tbl, "tbody")
            for r in bodyrows or [[]]:
                tr = _sub(tb, "tr")
                for c in r:
                    a = {}
                    if c.colspan > 1:
                        a["colspan"] = str(c.colspan)
                    if c.rowspan > 1:
                        a["rowspan"] = str(c.rowspan)
                    _sub(tr, "th" if c.header else "td", a, c.text)
                if not r:
                    _sub(tr, "td")
        else:
            tag_cls = cls
            if e.type in ("header", "footer", "page_number"):
                tag_cls += " t-running"
            p = _sub(section, "p", {"id": e.anchor, "class": tag_cls})
            render_inline(p, e.text or "", e.marks, rel, noteref_ids=noteref_ids)
    if notes:
        ns = _sub(section, "section", {"epub:type": "footnotes", "class": "notes", "role": "doc-endnotes"})
        _sub(ns, "h2", {"class": "notes-title"}, "Notas")
        for n in notes:
            aside = _sub(ns, "aside", {"id": n.anchor, "epub:type": "footnote", "role": "doc-footnote",
                                       "class": "footnote"})
            p = _sub(aside, "p")
            render_inline(p, n.text or "", n.marks, rel)
            back = noteref_ids.get(n.id)
            if back:
                p.append(_E("a", {"href": f"#{back}", "class": "backlink", "role": "doc-backlink"}, " ↩"))
    return root


def _render_fixed_page(doc, fname, members, images, resolve, res, toc_entries, lang):
    page_idx = members[0].sources[0].page
    pg = doc.pages[page_idx]
    scale = 96.0 / 72.0
    W, H = int(round(pg.width * scale)), int(round(pg.height * scale))
    root, body = _new_doc(f"{doc.metadata.title} — página {page_idx + 1}", lang, "../styles/book.css",
                          viewport=f"width={W}, height={H}")
    body.set("style", f"width:{W}px;height:{H}px;")
    rel = lambda m: resolve(m, fname)  # noqa: E731
    for e in members:
        s = e.sources[0]
        x0, y0, x1, y1 = s.bbox
        style = (f"left:{x0 * scale:.1f}px;top:{y0 * scale:.1f}px;width:{max(1, x1 - x0) * scale:.1f}px;"
                 f"height:{max(1, y1 - y0) * scale:.1f}px;")
        fs = (e.observed_style.size or 10) * scale
        if e.type in VISUAL:
            if e.id not in images:
                continue
            d = _sub(body, "div", {"id": e.anchor, "class": f"fx {_style_classes(e)}", "style": style})
            _sub(d, "img", {"src": "../" + images[e.id], "alt": _clean(e.alt or (e.text or ""))})
            if e.type == "formula" and e.text:
                _sub(d, "span", {"class": "formula-text", "hidden": "hidden"}, e.text)
        elif e.type == "table":
            d = _sub(body, "div", {"id": e.anchor, "class": "fx t-table", "style": style + f"font-size:{fs * 0.9:.1f}px;"})
            tbl = _sub(d, "table")
            for r in e.table or []:
                tr = _sub(tbl, "tr")
                for c in r:
                    a = {"colspan": str(c.colspan)} if c.colspan > 1 else {}
                    _sub(tr, "th" if c.header else "td", a, c.text)
        else:
            weight = "font-weight:bold;" if e.observed_style.bold else ""
            ital = "font-style:italic;" if e.observed_style.italic else ""
            color = f"color:{e.observed_style.color};" if e.observed_style.color and \
                re.fullmatch(r"#[0-9a-f]{6}", e.observed_style.color) else ""
            tag = f"h{max(1, min(6, e.level or 2))}" if e.type == "heading" else "p"
            d = _sub(body, "div", {"class": f"fx {_style_classes(e)}",
                                   "style": style + f"font-size:{fs:.1f}px;{weight}{ital}{color}"})
            p = _sub(d, tag, {"id": e.anchor})
            text = e.text or ""
            render_inline(p, text, e.marks, rel)
            if e.type == "heading":
                toc_entries.append((max(1, min(6, e.level or 2)), _clean(text), f"{fname}#{e.anchor}"))
    if not any(t[2].startswith(fname) for t in toc_entries) and page_idx == 0:
        toc_entries.append((1, doc.metadata.title, fname))
    return root, None


def _opf(doc: Document, ident: str, manifest, spine, mode, modified) -> bytes:
    OPF = "http://www.idpf.org/2007/opf"
    DC = "http://purl.org/dc/elements/1.1/"
    pkg = etree.Element(f"{{{OPF}}}package", nsmap={None: OPF, "dc": DC})
    pkg.set("version", "3.0")
    pkg.set("unique-identifier", "bookid")
    pkg.set("{http://www.w3.org/XML/1998/namespace}lang", doc.metadata.language or "pt-BR")
    if mode == "fixed":
        pkg.set("prefix", "rendition: http://www.idpf.org/vocab/rendition/#")
    md = etree.SubElement(pkg, f"{{{OPF}}}metadata")
    e = etree.SubElement(md, f"{{{DC}}}identifier", id="bookid")
    e.text = ident
    etree.SubElement(md, f"{{{DC}}}title").text = _clean(doc.metadata.title) or "Sem título"
    etree.SubElement(md, f"{{{DC}}}language").text = doc.metadata.language or "pt-BR"
    for i, a in enumerate(doc.metadata.authors):
        etree.SubElement(md, f"{{{DC}}}creator", id=f"creator{i}").text = _clean(a)
    if doc.metadata.publisher:
        etree.SubElement(md, f"{{{DC}}}publisher").text = _clean(doc.metadata.publisher)
    if doc.metadata.description:
        etree.SubElement(md, f"{{{DC}}}description").text = _clean(doc.metadata.description)
    mod = (modified or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    etree.SubElement(md, f"{{{OPF}}}meta", property="dcterms:modified").text = mod
    if mode == "fixed":
        etree.SubElement(md, f"{{{OPF}}}meta", property="rendition:layout").text = "pre-paginated"
        etree.SubElement(md, f"{{{OPF}}}meta", property="rendition:orientation").text = "auto"
        etree.SubElement(md, f"{{{OPF}}}meta", property="rendition:spread").text = "none"
    cov = next((m for m in manifest if m.get("properties") == "cover-image"), None)
    if cov:
        etree.SubElement(md, f"{{{OPF}}}meta", name="cover", content=cov["id"])
    man = etree.SubElement(pkg, f"{{{OPF}}}manifest")
    for m in manifest:
        etree.SubElement(man, f"{{{OPF}}}item", **m)
    sp = etree.SubElement(pkg, f"{{{OPF}}}spine", toc="ncx")
    if doc.metadata.direction == "rtl":
        sp.set("page-progression-direction", "rtl")
    for s in spine:
        etree.SubElement(sp, f"{{{OPF}}}itemref", **s)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + etree.tostring(pkg, encoding="utf-8", pretty_print=True)


def _ncx(doc: Document, ident: str, entries) -> bytes:
    NCX = "http://www.daisy.org/z3986/2005/ncx/"
    root = etree.Element(f"{{{NCX}}}ncx", nsmap={None: NCX}, version="2005-1")
    head = etree.SubElement(root, f"{{{NCX}}}head")
    etree.SubElement(head, f"{{{NCX}}}meta", name="dtb:uid", content=ident)
    t = etree.SubElement(root, f"{{{NCX}}}docTitle")
    etree.SubElement(t, f"{{{NCX}}}text").text = _clean(doc.metadata.title)
    nm = etree.SubElement(root, f"{{{NCX}}}navMap")
    for i, (lvl, title, href) in enumerate(entries):
        np_ = etree.SubElement(nm, f"{{{NCX}}}navPoint", id=f"np{i + 1}", playOrder=str(i + 1))
        lbl = etree.SubElement(np_, f"{{{NCX}}}navLabel")
        etree.SubElement(lbl, f"{{{NCX}}}text").text = _clean(title) or "(sem título)"
        etree.SubElement(np_, f"{{{NCX}}}content", src=href)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + etree.tostring(root, encoding="utf-8", pretty_print=True)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------- leitura segura

MAX_UNCOMPRESSED = 500 * 1024 * 1024
MAX_ENTRIES = 5000


def safe_read_epub(path: Path) -> dict[str, bytes]:
    """Lê o EPUB validando caminhos (sem travessia) e limitando expansão."""
    out: dict[str, bytes] = {}
    total = 0
    with zipfile.ZipFile(path) as z:
        infos = z.infolist()
        if len(infos) > MAX_ENTRIES:
            raise ValueError("EPUB com entradas demais")
        for zi in infos:
            name = zi.filename
            if name.startswith("/") or ".." in Path(name).parts or "\\" in name or ":" in name.split("/")[0]:
                raise ValueError(f"caminho inseguro no EPUB: {name}")
            if zi.is_dir():
                continue
            total += zi.file_size
            if total > MAX_UNCOMPRESSED:
                raise ValueError("EPUB expande além do limite permitido")
            if zi.compress_size and zi.file_size / max(1, zi.compress_size) > 200:
                raise ValueError(f"razão de compressão suspeita: {name}")
            out[name] = z.read(zi)
    return out


BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "figcaption", "caption", "td", "th", "aside",
              "section", "div", "figure", "blockquote", "table", "tr", "ul", "ol", "body", "span"}


def xhtml_text(data: bytes) -> str:
    """Texto visível de um XHTML. Elementos de bloco viram fronteiras de espaço."""
    root = etree.fromstring(data, parser=etree.XMLParser(resolve_entities=False, no_network=True))
    parts: list[str] = []

    def walk(el):
        tag = etree.QName(el).localname if isinstance(el.tag, str) else ""
        if tag in ("head", "script", "style"):
            return
        hidden = el.get("hidden") is not None and el.get("class") != "formula-text"
        if not hidden:
            block = tag in BLOCK_TAGS
            if block:
                parts.append(" ")
            if el.text:
                parts.append(el.text)
            for c in el:
                walk(c)
                if c.tail:
                    parts.append(c.tail)
            if block:
                parts.append(" ")

    walk(root)
    return html.unescape("".join(parts))
