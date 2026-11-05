"""Configuração da aplicação (variáveis de ambiente; ver .env.example)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./data/app.db"
    data_dir: Path = Path("./data")
    # Segredo externo ao banco usado para cifrar chaves de API persistidas (Fernet).
    app_secret_key: str = ""

    max_upload_mb: int = 200
    max_pages: int = 2000
    ocr_languages: str = "por+eng"
    ocr_dpi: int = 300

    epubcheck_jar: str = ""  # caminho para epubcheck.jar; vazio = procurar em tools/
    chromium_path: str = ""  # vazio = Chromium do Playwright

    # Gemini (opcional). A chave também pode ser cadastrada na interface.
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.8-flash"
    gemini_pages_per_call: int = 40
    gemini_max_calls: int = 20

    # Ollama (opcional)
    ollama_url: str = "http://localhost:11434"
    # Hosts permitidos para o endereço do Ollama (lista separada por vírgulas).
    ollama_allowed_hosts: str = "localhost,127.0.0.1,host.docker.internal,ollama"

    # Somente testes: habilita o provedor simulado. Nunca exposto como integração real.
    ai_allow_mock: bool = False

    job_inline: bool = False  # executa tarefas na própria requisição (apenas testes)
    worker_poll_seconds: float = 1.0
    job_stale_seconds: int = 120


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    return s


def find_epubcheck() -> str | None:
    s = get_settings()
    cands = [s.epubcheck_jar] if s.epubcheck_jar else []
    root = Path(__file__).resolve().parents[2]
    cands += [str(p) for p in sorted(root.glob("tools/epubcheck-*/epubcheck.jar"))]
    cands += ["/opt/epubcheck/epubcheck.jar"]
    for c in cands:
        if c and Path(c).is_file():
            return c
    return None
