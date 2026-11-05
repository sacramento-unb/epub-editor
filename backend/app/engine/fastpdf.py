"""Acelerações para o pdfminer.six (usado pelo pdfplumber).

O pdfminer descriptografa RC4 em Python puro, o que dominava o tempo de PDFs protegidos por senha de
proprietário (sem senha de abertura). Aqui o RC4 passa a usar a implementação nativa da biblioteca
`cryptography`, com o mesmo resultado byte a byte. Nenhuma proteção é contornada: só PDFs que
abrem normalmente (sem senha ou com a senha informada) chegam a ser lidos.
"""
from __future__ import annotations

_installed = False


_NATIVE_KEY_SIZES = {5, 7, 8, 10, 16, 24, 32}  # bytes aceitos pela implementação nativa


class FastArcfour:
    def __init__(self, key: bytes):
        key = bytes(key)
        if len(key) in _NATIVE_KEY_SIZES:
            from cryptography.hazmat.decrepit.ciphers.algorithms import ARC4
            from cryptography.hazmat.primitives.ciphers import Cipher

            self._enc = Cipher(ARC4(key), mode=None).encryptor()
            self._py = None
        else:
            from pdfminer.arcfour import Arcfour

            self._enc = None
            self._py = Arcfour(key)

    def process(self, data: bytes) -> bytes:
        if self._py is not None:
            return self._py.process(bytes(data))
        return self._enc.update(bytes(data))

    encrypt = decrypt = process


def install() -> None:
    global _installed
    if _installed:
        return
    try:
        from pdfminer import pdfdocument

        FastArcfour(b"teste")  # garante disponibilidade antes de trocar
        pdfdocument.Arcfour = FastArcfour
        _installed = True
    except Exception:  # sem cryptography/ARC4: mantém a implementação original
        pass
