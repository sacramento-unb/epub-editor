"""Abstração de armazenamento de arquivos.

Implementação inicial: diretório local persistente. A interface permite trocar por
armazenamento compatível com S3 (bibliotecas de PDF exigem caminho local; um
backend S3 deve materializar o objeto em cache local em ``local_path``).
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from .config import get_settings

_KEY_RE = re.compile(r"^[A-Za-z0-9._\-/]+$")


class Storage:
    def local_path(self, key: str) -> Path: ...
    def put_bytes(self, key: str, data: bytes) -> None: ...
    def get_bytes(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def delete_prefix(self, prefix: str) -> None: ...


class LocalStorage(Storage):
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def local_path(self, key: str) -> Path:
        if not _KEY_RE.match(key) or ".." in key.split("/") or key.startswith("/"):
            raise ValueError(f"chave de armazenamento inválida: {key!r}")
        p = (self.root / key).resolve()
        if self.root not in p.parents and p != self.root:
            raise ValueError("caminho fora do armazenamento")
        return p

    def put_bytes(self, key: str, data: bytes) -> None:
        p = self.local_path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".part")
        tmp.write_bytes(data)
        tmp.replace(p)

    def get_bytes(self, key: str) -> bytes:
        return self.local_path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self.local_path(key).exists()

    def delete_prefix(self, prefix: str) -> None:
        p = self.local_path(prefix.rstrip("/"))
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()


_storage: Storage | None = None


def get_storage() -> Storage:
    global _storage
    if _storage is None:
        _storage = LocalStorage(get_settings().data_dir / "files")
    return _storage


def reset_storage():
    global _storage
    _storage = None


def project_key(pid: str, *parts: str) -> str:
    return "/".join(["projects", pid, *parts])
