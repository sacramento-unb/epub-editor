import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SAMPLES = ROOT / "tests" / "_samples"


@pytest.fixture(scope="session")
def pdfs():
    from tests.fixtures import make_all

    return make_all(SAMPLES)


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    """Banco SQLite isolado, tarefas executadas na própria requisição, IA simulada habilitada."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JOB_INLINE", "1")
    monkeypatch.setenv("AI_ALLOW_MOCK", "1")
    monkeypatch.setenv("APP_SECRET_KEY", "chave-de-teste-com-32-caracteres!!")
    monkeypatch.setenv("OCR_LANGUAGES", os.environ.get("TEST_OCR_LANGS", "por+eng"))
    monkeypatch.setenv("GEMINI_API_KEY", "")
    if Path("/usr/bin/chromium").exists():
        monkeypatch.setenv("CHROMIUM_PATH", "/usr/bin/chromium")
    from app import config, db, storage
    from app.ai import runtime

    config.get_settings.cache_clear()
    db.reset_engine()
    storage.reset_storage()
    runtime.set_mock_provider(None)
    db.init_db()
    yield tmp_path
    runtime.set_mock_provider(None)
    db.reset_engine()
    config.get_settings.cache_clear()


@pytest.fixture()
def client(app_env):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


def upload(client, path, **form):
    with open(path, "rb") as f:
        r = client.post("/api/projects", files={"file": (Path(path).name, f, "application/pdf")}, data=form)
    return r
