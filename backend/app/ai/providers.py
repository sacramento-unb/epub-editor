"""Adaptadores de provedores de IA: Gemini (nuvem), Ollama (local) e simulado (somente testes).

Todos expõem a mesma interface: geração de JSON estruturado a partir de texto e
imagens PNG, listagem de modelos e detecção de capacidades efetivas.
"""
from __future__ import annotations

import base64
import io
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse

import httpx


class ProviderError(Exception):
    """Erro de provedor com categoria e indicação se é transitório."""

    def __init__(self, kind: str, message: str, transient: bool = False):
        super().__init__(message)
        self.kind = kind  # auth|rate_limit|timeout|oom|capability|invalid_response|unavailable|bad_request
        self.transient = transient


@dataclass
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass
class Capabilities:
    text: bool = False
    vision: bool = False
    structured: bool = False
    context_tokens: int | None = None
    source: list[str] = field(default_factory=list)  # evidências da detecção

    def as_dict(self):
        return {"text": self.text, "vision": self.vision, "structured": self.structured,
                "context_tokens": self.context_tokens, "source": self.source}


def _probe_image() -> bytes:
    from PIL import Image

    img = Image.new("RGB", (64, 64), (220, 20, 20))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


PROBE_SCHEMA = {"type": "object", "properties": {"color": {"type": "string"}}, "required": ["color"]}


class Provider:
    name = "base"
    is_cloud = False

    def generate_json(self, *, model: str, system: str, text: str, images: list[bytes], schema: dict,
                      max_output_tokens: int = 4096, timeout: float = 120) -> tuple[dict, Usage]:
        raise NotImplementedError

    def list_models(self) -> list[str]:
        raise NotImplementedError

    def detect(self, model: str, probe: bool = True) -> Capabilities:
        raise NotImplementedError

    def _probe(self, model: str, caps: Capabilities) -> None:
        """Teste ativo: uma chamada mínima com imagem e schema confirma visão e saída estruturada."""
        try:
            data, _ = self.generate_json(model=model, system="Responda apenas com JSON.",
                                         text="Qual é a cor predominante da imagem? Responda em português.",
                                         images=[_probe_image()], schema=PROBE_SCHEMA, max_output_tokens=1024,
                                         timeout=90)
            caps.structured = isinstance(data, dict) and "color" in data
            color = str(data.get("color", "")).lower() if isinstance(data, dict) else ""
            caps.vision = caps.structured and ("verm" in color or "red" in color)
            caps.text = True
            caps.source.append(f"teste ativo: resposta {data!r:.80}")
        except ProviderError as exc:
            caps.source.append(f"teste ativo falhou ({exc.kind}): {str(exc)[:200]}")


# --------------------------------------------------------------------------- Gemini

GEMINI_INLINE_LIMIT = 19 * 1024 * 1024


class GeminiProvider(Provider):
    name = "gemini"
    is_cloud = True

    def __init__(self, api_key: str):
        if not api_key:
            raise ProviderError("auth", "Chave da API Gemini não configurada.")
        from google import genai

        self._client = genai.Client(api_key=api_key)

    def generate_json(self, *, model, system, text, images, schema, max_output_tokens=4096, timeout=120):
        from google.genai import errors, types

        # Limite de requisição com dados inline da API Gemini: 20 MB (base64 ≈ +37%).
        approx = len(text.encode()) + len(system.encode()) + sum(len(i) for i in images) * 4 // 3
        if approx > GEMINI_INLINE_LIMIT:
            raise ProviderError("bad_request", f"Requisição de ~{approx / 1e6:.1f} MB excede o limite inline do Gemini "
                                "(20 MB). Reduza 'Resolução (dpi)' ou 'Caracteres por unidade'.")
        parts: list[Any] = [types.Part.from_text(text=text)]
        for img in images:
            parts.append(types.Part.from_bytes(data=img, mime_type="image/png"))
        cfg = types.GenerateContentConfig(
            system_instruction=system, response_mime_type="application/json", response_json_schema=schema,
            max_output_tokens=max_output_tokens, temperature=0.1,
            http_options=types.HttpOptions(timeout=int(timeout * 1000)))
        try:
            resp = self._client.models.generate_content(model=model, contents=[types.Content(role="user", parts=parts)],
                                                        config=cfg)
        except errors.APIError as exc:
            code = getattr(exc, "code", 0) or 0
            msg = getattr(exc, "message", None) or str(exc)
            if code in (401, 403):
                raise ProviderError("auth", f"Gemini recusou a credencial ({code}).") from None
            if code == 429:
                raise ProviderError("rate_limit", "Limite de requisições do Gemini atingido (429).", True) from None
            if code in (500, 502, 503, 504):
                raise ProviderError("unavailable", f"Gemini indisponível ({code}).", True) from None
            if code == 400:
                raise ProviderError("bad_request", f"Requisição rejeitada pelo Gemini: {msg[:300]}") from None
            raise ProviderError("unavailable", f"Erro do Gemini ({code}): {msg[:300]}", code >= 500) from None
        except httpx.TimeoutException:
            raise ProviderError("timeout", "Tempo limite excedido na chamada ao Gemini.", True) from None
        except httpx.HTTPError as exc:
            raise ProviderError("unavailable", f"Falha de rede ao chamar o Gemini: {type(exc).__name__}", True) from None
        um = getattr(resp, "usage_metadata", None)
        out_tok = getattr(um, "candidates_token_count", None)
        thoughts = getattr(um, "thoughts_token_count", None)
        if out_tok is not None or thoughts is not None:
            out_tok = (out_tok or 0) + (thoughts or 0)  # tokens de raciocínio são cobrados como saída
        usage = Usage(getattr(um, "prompt_token_count", None), out_tok)
        cands = getattr(resp, "candidates", None) or []
        finish = str(getattr(cands[0], "finish_reason", "")) if cands else ""
        if "MAX_TOKENS" in finish:
            raise ProviderError("invalid_response", "Resposta do Gemini truncada pelo limite de tokens de saída "
                                "(inclui tokens de raciocínio do modelo); aumente 'Tokens de saída' nos limites.")
        if "SAFETY" in finish or "PROHIBITED" in finish or "RECITATION" in finish:
            raise ProviderError("bad_request", f"Gemini bloqueou a resposta ({finish}).")
        raw = resp.text or ""
        try:
            return json.loads(raw), usage
        except json.JSONDecodeError:
            raise ProviderError("invalid_response", "Gemini retornou JSON inválido.") from None

    def list_models(self) -> list[str]:
        try:
            out = []
            for m in self._client.models.list():
                actions = getattr(m, "supported_actions", None) or []
                if not actions or "generateContent" in actions:
                    out.append((m.name or "").removeprefix("models/"))
            return sorted(out)
        except Exception as exc:
            raise ProviderError("unavailable", f"Não foi possível listar modelos do Gemini: {type(exc).__name__}") from None

    def detect(self, model: str, probe: bool = True) -> Capabilities:
        caps = Capabilities()
        try:
            info = self._client.models.get(model=model)
            caps.context_tokens = getattr(info, "input_token_limit", None)
            caps.text = True
            caps.source.append("metadados do modelo (models.get)")
        except Exception as exc:
            raise ProviderError("unavailable", f"Modelo Gemini '{model}' não encontrado: {type(exc).__name__}") from None
        if probe:
            self._probe(model, caps)
        return caps


# --------------------------------------------------------------------------- Ollama

def validate_ollama_url(url: str, allowed_hosts: list[str]) -> str:
    u = urlparse(url.strip())
    if u.scheme not in ("http", "https"):
        raise ProviderError("bad_request", "Endereço do Ollama deve usar http ou https.")
    if u.username or u.password:
        raise ProviderError("bad_request", "Endereço do Ollama não pode conter credenciais.")
    host = (u.hostname or "").lower()
    if host not in [h.strip().lower() for h in allowed_hosts if h.strip()]:
        raise ProviderError("bad_request", f"Host '{host}' não está em OLLAMA_ALLOWED_HOSTS (administrado no servidor).")
    if u.path not in ("", "/"):
        raise ProviderError("bad_request", "Informe apenas esquema, host e porta do Ollama.")
    return f"{u.scheme}://{u.netloc}"


class OllamaProvider(Provider):
    name = "ollama"
    is_cloud = False

    def __init__(self, base_url: str, allowed_hosts: list[str]):
        self.base = validate_ollama_url(base_url, allowed_hosts)

    def _req(self, method, path, timeout=30, **kw):
        try:
            r = httpx.request(method, self.base + path, timeout=timeout, **kw)
        except httpx.TimeoutException:
            raise ProviderError("timeout", "Tempo limite excedido no Ollama (modelo carregando ou lento demais).", True) from None
        except httpx.HTTPError as exc:
            raise ProviderError("unavailable", f"Ollama inacessível em {self.base}: {type(exc).__name__}") from None
        if r.status_code >= 400:
            msg = ""
            try:
                msg = r.json().get("error", "")
            except Exception:
                msg = r.text[:300]
            low = msg.lower()
            if "memory" in low or "out of memory" in low or "cuda" in low:
                raise ProviderError("oom", f"Memória insuficiente para o modelo no Ollama: {msg[:300]}")
            if r.status_code == 404:
                raise ProviderError("unavailable", f"Modelo não encontrado no Ollama: {msg[:200]}")
            if "does not support" in low or "image" in low or "vision" in low:
                raise ProviderError("capability", f"O modelo não aceita imagens: {msg[:200]}")
            raise ProviderError("unavailable", f"Ollama respondeu {r.status_code}: {msg[:300]}", r.status_code >= 500)
        return r.json()

    def generate_json(self, *, model, system, text, images, schema, max_output_tokens=4096, timeout=180):
        msg: dict = {"role": "user", "content": text}
        if images:
            msg["images"] = [base64.b64encode(i).decode() for i in images]
        body = {"model": model, "stream": False, "format": schema,
                "messages": [{"role": "system", "content": system}, msg],
                "options": {"temperature": 0.1, "num_predict": max_output_tokens}}
        data = self._req("POST", "/api/chat", timeout=timeout, json=body)
        content = (data.get("message") or {}).get("content", "")
        usage = Usage(data.get("prompt_eval_count"), data.get("eval_count"))
        try:
            return json.loads(content), usage
        except json.JSONDecodeError:
            raise ProviderError("invalid_response", "Ollama retornou JSON inválido.") from None

    def list_models(self) -> list[str]:
        data = self._req("GET", "/api/tags")
        return sorted(m.get("name", "") for m in data.get("models", []))

    def detect(self, model: str, probe: bool = True) -> Capabilities:
        caps = Capabilities()
        info = self._req("POST", "/api/show", json={"model": model})
        declared = info.get("capabilities")
        if isinstance(declared, list):
            caps.text = "completion" in declared
            caps.vision = "vision" in declared
            caps.source.append(f"/api/show capabilities={declared}")
        for k, v in (info.get("model_info") or {}).items():
            if k.endswith(".context_length") and isinstance(v, int):
                caps.context_tokens = v
        if probe and caps.text:
            declared_vision = caps.vision
            self._probe(model, caps)
            if declared_vision and not caps.vision:
                caps.source.append("modelo declara visão, mas o teste ativo não confirmou")
        return caps


# --------------------------------------------------------------------------- Simulado (testes)

class MockProvider(Provider):
    """Provedor determinístico para testes automatizados. Nunca apresentado como integração real."""
    name = "mock"
    is_cloud = False

    def __init__(self, responder: Callable[[dict], Any] | None = None, vision: bool = True):
        self.responder = responder
        self.vision = vision
        self.calls: list[dict] = []

    def generate_json(self, *, model, system, text, images, schema, max_output_tokens=4096, timeout=120):
        call = {"model": model, "system": system, "text": text, "images": len(images), "schema": schema}
        self.calls.append(call)
        if images and not self.vision:
            raise ProviderError("capability", "modelo simulado sem visão")
        out = self.responder(call) if self.responder else {}
        if isinstance(out, Exception):
            raise out
        if isinstance(out, str):
            try:
                return json.loads(out), Usage(len(text) // 4, 10)
            except json.JSONDecodeError:
                raise ProviderError("invalid_response", "JSON inválido (simulado)") from None
        return out, Usage(len(text) // 4, 10)

    def list_models(self):
        return ["mock-text", "mock-vision"]

    def detect(self, model, probe=True):
        return Capabilities(text=True, vision=self.vision and "vision" in model, structured=True,
                            context_tokens=32000, source=["simulado"])


def with_backoff(fn: Callable[[], Any], attempts: int = 4, base: float = 2.0, sleep=time.sleep):
    """Repete chamadas com erro transitório (429/5xx/timeout) com backoff exponencial."""
    for i in range(attempts):
        try:
            return fn()
        except ProviderError as exc:
            if not exc.transient or i == attempts - 1:
                raise
            sleep(base * (2 ** i))
