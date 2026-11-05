"""Cifragem de segredos persistidos (chaves de API, senhas de PDF).

Usa Fernet com chave derivada de APP_SECRET_KEY (variável de ambiente, externa ao
banco). Sem APP_SECRET_KEY, segredos não são persistidos.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import re

from cryptography.fernet import Fernet, InvalidToken

from .config import get_settings


class SecretUnavailable(Exception):
    pass


def _fernet() -> Fernet:
    key = get_settings().app_secret_key
    if not key or len(key) < 16:
        raise SecretUnavailable("APP_SECRET_KEY ausente ou curta (mín. 16 caracteres); segredos não podem ser salvos.")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest()))


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(token: str | None) -> str | None:
    if not token:
        return None
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, SecretUnavailable):
        return None


class RedactFilter(logging.Filter):
    """Remove padrões de chave de API de mensagens de log."""
    PAT = re.compile(r"(AIza[0-9A-Za-z_\-]{20,}|(?i:api[_-]?key)\s*[=:]\s*\S+|(?i:x-goog-api-key)\S*)")

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        red = self.PAT.sub("[REDIGIDO]", msg)
        for s in _known_secrets():
            if s and s in red:
                red = red.replace(s, "[REDIGIDO]")
        if red != msg:
            record.msg, record.args = red, ()
        return True


def _known_secrets() -> list[str]:
    s = get_settings()
    return [s.gemini_api_key] if s.gemini_api_key else []


def install_log_redaction():
    f = RedactFilter()
    root = logging.getLogger()
    root.addFilter(f)
    for h in root.handlers:
        h.addFilter(f)
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error", "httpx", "google_genai", "app"):
        logging.getLogger(name).addFilter(f)
