import os

from pdfminer.arcfour import Arcfour

from app.engine.fastpdf import FastArcfour


def test_rc4_identical_to_pdfminer():
    for n in range(5, 17):  # tamanhos de chave possíveis em PDF RC4
        k, d = os.urandom(n), os.urandom(50_000)
        assert Arcfour(k).decrypt(d) == FastArcfour(k).decrypt(d)
