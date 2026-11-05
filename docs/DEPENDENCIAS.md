# Dependências, versões e licenças

Versões consultadas no PyPI, no npm e no GitHub em 2026-10-23 e efetivamente instaladas e testadas.

## Backend (Python 3.12)

| Pacote | Versão | Licença | Uso |
|---|---|---|---|
| fastapi | 0.161.1 | MIT | API HTTP |
| uvicorn | 0.54.0 | BSD-3 | servidor ASGI |
| pydantic | 2.14.1 | MIT | modelo documental e contratos |
| SQLAlchemy | 2.1.6 | MIT | persistência |
| alembic | 1.20.1 | MIT | migrações |
| psycopg[binary] | 3.3.6 | LGPL-3.0 | driver PostgreSQL (uso como biblioteca dinâmica, sem modificação) |
| pdfplumber | 0.11.10 | MIT | extração de texto, fontes, tabelas e links (sobre pdfminer.six, MIT) |
| pypdfium2 | 5.14.0 (PDFium) | BSD-3 / Apache-2.0 | renderização e metadados |
| pytesseract | 0.3.13 | Apache-2.0 | ponte para o Tesseract |
| Pillow | 13.0.0 | MIT-CMU | imagens |
| numpy | 2.5.x | BSD-3 | SSIM auxiliar |
| lxml | 6.1.3 | BSD-3 | XHTML/OPF/NCX |
| httpx | 0.28.1 | BSD-3 | cliente do Ollama |
| google-genai | 2.33.0 | Apache-2.0 | SDK oficial do Gemini |
| cryptography | 50.x | Apache-2.0/BSD | Fernet (segredos) |
| playwright | 1.64.1 | Apache-2.0 | captura da prévia (Chromium) |
| reportlab (dev) | 5.1.0 | BSD | geração dos PDFs de teste |
| pytest (dev) | 9.1.1 | MIT | testes |

## Ferramentas externas

| Ferramenta | Versão testada | Licença | Observação |
|---|---|---|---|
| EPUBCheck | 5.4.0 | BSD-3 (W3C) | baixado do release oficial; requer Java (testado com OpenJDK 27 local, `default-jre-headless` na imagem) |
| Tesseract | 5.5.3 (local) | Apache-2.0 | local só tinha `eng`; a imagem Docker instala `por` e `eng` |
| Chromium | 154 (local, Arch) / Playwright 1.64 (imagem) | BSD | só renderiza XHTML gerado pela aplicação, sem rede |
| PostgreSQL | 17 (imagem `postgres:17-alpine`) | PostgreSQL | |

## Frontend

| Pacote | Versão | Licença |
|---|---|---|
| next | 16.4.3 | MIT |
| react / react-dom | 19.3.1 | MIT |
| pdfjs-dist | 6.4.299 | Apache-2.0 |
| typescript | 5.9.3 | Apache-2.0 |

## Alternativas consideradas

- **PyMuPDF:** tem boa extração, mas a licença AGPL-3.0 (ou comercial) impõe obrigações na
  distribuição. Não foi usado. Se a qualidade justificar, pode virar um adaptador opcional, desde que a
  licença seja aceita.
- **epub.js:** a última versão (0.3.93) está sem manutenção ativa. Usamos uma prévia própria que serve
  o próprio arquivo exportado.
- **Celery + Redis:** substituídos pela fila no PostgreSQL (ver [ARQUITETURA.md](ARQUITETURA.md)).
- **ebooklib:** preferimos geração própria para controlar a validade do XHTML e do OCF.
