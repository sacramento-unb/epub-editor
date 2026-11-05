"""Verificação de preservação de conteúdo e validação EPUBCheck.

Compara o texto dos elementos selecionados com o texto efetivamente presente no
EPUB gerado. Normalizações toleradas (documentadas): Unicode NFKC, espaços,
hífen discricionário, marcador visual de item de lista. A verificação só é tão
boa quanto a extração inicial: se a extração perdeu texto, isso não é detectado.
"""
from __future__ import annotations

import json
import re
import subprocess
import unicodedata
from collections import Counter
from pathlib import Path

from ..config import find_epubcheck
from .epub import output_elements, safe_read_epub, strip_list_marker, xhtml_text
from .model import Document, element_text_for_integrity

NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")
GENERATED = {"notas", "↩", "sumário", "pontos", "de", "referência", "início", "páginas", "do", "pdf", "original"}


def norm(t: str) -> str:
    t = unicodedata.normalize("NFKC", t or "").replace("­", "")
    return re.sub(r"\s+", " ", t).strip().lower()


def check_integrity(doc: Document, epub_path: Path) -> dict:
    files = safe_read_epub(epub_path)
    out_text = []
    for name, data in sorted(files.items()):
        if name.endswith(".xhtml") and not name.endswith("nav.xhtml"):
            try:
                out_text.append(xhtml_text(data))
            except Exception as exc:  # XHTML inválido é falha de integridade
                return {"ok": False, "error": f"XHTML inválido em {name}: {exc}"}
    output = norm(" ".join(out_text))
    output_nospace = output.replace(" ", "")
    missing, partial = [], []
    in_numbers: Counter = Counter()
    checked = 0
    for e in output_elements(doc):
        t = element_text_for_integrity(e)
        if e.type == "list_item":
            t = strip_list_marker(t)[0]
        if e.type in ("image", "figure", "region", "cover"):
            continue
        n = norm(t)
        if not n:
            continue
        checked += 1
        in_numbers.update(NUM_RE.findall(n))
        if n not in output and n.replace(" ", "") not in output_nospace:
            words = n.split()
            found = sum(1 for w in words if w in output)
            (partial if found >= len(words) * 0.8 else missing).append(
                {"id": e.id, "type": e.type, "excerpt": t[:120]})
    # duplicação: texto de elemento aparecendo mais vezes que na seleção
    # (o esperado conta ocorrências do trecho em todos os elementos selecionados, inclusive como parte de outro)
    def visible(e):  # o marcador numérico da lista vai no atributo value, não no texto
        t = element_text_for_integrity(e)
        return strip_list_marker(t)[0] if e.type == "list_item" else t

    texts = [norm(visible(e)) for e in output_elements(doc) if e.type not in ("image", "figure", "region", "cover")]
    joined_in = " ".join(texts)  # mesmo critério da saída: trechos podem atravessar elementos
    duplicated = [t[:120] for t in set(texts) if len(t) >= 25 and output.count(t) > joined_in.count(t)]
    out_numbers = Counter(NUM_RE.findall(output))
    lost_numbers = sorted(k for k, v in in_numbers.items() if out_numbers.get(k, 0) < v)
    # texto inesperado: palavras da saída que não existem em nenhum elemento selecionado
    vocab = set()
    for e in output_elements(doc):
        vocab.update(norm(element_text_for_integrity(e)).split())
    vocab.update(norm(doc.metadata.title).split())
    unexpected = sorted({w for w in output.split() if w not in vocab and w not in GENERATED
                         and not re.fullmatch(r"[\W\d]+", w)})[:50]
    ok = not missing and not lost_numbers and not unexpected and not duplicated
    return {
        "ok": ok, "checked_elements": checked, "missing": missing, "partial": partial,
        "lost_numbers": lost_numbers, "unexpected_words": unexpected, "duplicated": duplicated,
        "limitations": "Compara com o texto extraído do PDF; erros da extração/OCR não são detectáveis aqui.",
    }


def run_epubcheck(epub_path: Path, timeout: int = 300) -> dict:
    jar = find_epubcheck()
    if not jar:
        return {"available": False, "status": "indisponível",
                "detail": "EPUBCheck não encontrado (defina EPUBCHECK_JAR ou use a imagem Docker)."}
    out_json = epub_path.with_suffix(".epubcheck.json")
    try:
        proc = subprocess.run(["java", "-jar", jar, str(epub_path), "--json", str(out_json)],
                              capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return {"available": False, "status": "indisponível", "detail": "Java não encontrado."}
    except subprocess.TimeoutExpired:
        return {"available": True, "status": "erro", "detail": "EPUBCheck excedeu o tempo limite."}
    try:
        data = json.loads(out_json.read_text())
    except Exception:
        return {"available": True, "status": "erro", "detail": proc.stderr[-2000:]}
    msgs = []
    for m in data.get("messages", []):
        locs = m.get("locations") or [{}]
        msgs.append({"severity": m.get("severity"), "id": m.get("ID"), "message": m.get("message"),
                     "path": locs[0].get("path"), "line": locs[0].get("line")})
    counts = Counter(m["severity"] for m in msgs)
    checker = data.get("checker", {})
    return {
        "available": True,
        "status": "ok" if counts.get("ERROR", 0) == 0 and counts.get("FATAL", 0) == 0 else "erros",
        "version": checker.get("checkerVersion"), "errors": counts.get("ERROR", 0) + counts.get("FATAL", 0),
        "warnings": counts.get("WARNING", 0), "usage": counts.get("USAGE", 0), "messages": msgs[:500],
    }
