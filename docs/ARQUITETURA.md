# Arquitetura

```
navegador ──► web (Next.js 16, React 19, TS) ──/api──► api (FastAPI) ──► PostgreSQL
                    pdf.js (PDF)                          │   ▲            (projetos, revisões, tarefas,
                    iframe sandbox (EPUB real)            │   │             exportações, IA, auditoria)
                                                          ▼   │
                                                   tabela jobs ◄── worker (Python)
                                                                    ├ pdfplumber/pdfminer + pypdfium2
                                                                    ├ Tesseract (OCR por página)
                                                                    ├ gerador EPUB (lxml) + EPUBCheck
                                                                    ├ Playwright/Chromium (captura)
                                                                    └ provedores: Gemini | Ollama
                                       arquivos: Storage (LocalStorage em /data; interface p/ S3)
```

## Decisões

| Tema | Decisão | Justificativa |
|---|---|---|
| Fila | Tabela `jobs` no PostgreSQL com `FOR UPDATE SKIP LOCKED`, no lugar de Celery+Redis | O volume é baixo e o estado das etapas já precisa ser durável. Um serviço a menos, com retomada por etapa e cancelamento cooperativo pelo banco. |
| Progresso | SSE (`/api/projects/{id}/events`) com consulta de reserva no cliente | Nenhuma operação longa depende de uma requisição aberta. O SSE só observa o banco. |
| Extração | pdfplumber 0.11.10 (pdfminer.six) para texto, fontes, tabelas e links; pypdfium2 5.14 para renderização | Licenças MIT, BSD e Apache. **PyMuPDF não foi usado**: é AGPL ou comercial, o que afetaria a distribuição. |
| OCR | Tesseract 5 via pytesseract, por página e só sem camada de texto | Evita duplicar texto nativo. A confiança média por bloco fica registrada. |
| EPUB | Gerador próprio com lxml e zipfile | Controle total de XHTML válido, OCF (`mimetype` primeiro, sem compressão), nav, NCX, page-list e metadados de layout fixo. |
| Validação | EPUBCheck 5.4.0 (W3C), via `java -jar … --json` | Tudo é registrado por exportação. |
| Prévia | O backend serve os arquivos **de dentro do EPUB exportado** com CSP `default-src 'none'`, sem scripts. O iframe usa `sandbox="allow-same-origin"`, sem `allow-scripts`. | A prévia é o arquivo real. O pai escuta cliques sem que o conteúdo execute código. |
| Captura | Playwright 1.64 e Chromium abrem o EPUB descompactado com caminhos validados. Viewport 600×900, escala 1, tema claro, fonte 100%, rede bloqueada. | A comparação é reproduzível. As caixas medidas vão para `render_maps`, nunca para o documento. |
| Leitor de prévia | Leitor próprio em iframe, no lugar do epub.js | O epub.js (0.3.93) está sem manutenção ativa. A camada de leitura (fonte, paginação por colunas CSS, tema) é injetada só na resposta da prévia e não faz parte do EPUB. |
| Segredos | Fernet com chave derivada de `APP_SECRET_KEY` (fora do banco). Filtro de logs remove padrões de chave. | A chave nunca vai ao frontend, aos logs ou às exportações (há teste). |

## Modelo documental (fonte de verdade)

Definido em `backend/app/engine/model.py`, com JSON Schema em `docs/schemas/document.schema.json`.

- `Document`: metadados, páginas (dimensões exibidas, rotação, MediaBox, CropBox, tipo
  digital/digitalizada/mista, confiança de OCR), elementos **em ordem de leitura**, estilo global,
  outline do PDF e avisos.
- `Element`: id estável (`p0001e003`), tipo/subtipo, `text_original` e `text`, marcas inline (negrito,
  itálico, sobrescrito, link, chamada de nota), `sources` (uma ou mais regiões
  página+bbox+sistema de coordenadas), nível, `chapter_break`, `parent_id`, `depends_on`, `included`,
  `excluded_explicit`, `locked`, `observed_style` (do PDF) separado de `output_style` (tokens
  editoriais), recurso/recorte, legenda, texto alternativo, tabela (células com cabeçalho e span),
  método e confiança da extração com evidências, pendências de revisão e proveniência.
- Âncora no XHTML: `id="e-<id>"`. O mapeamento elemento → arquivo e âncora é gravado em cada
  exportação (`exports.build.mapping`).

**Sistema de coordenadas `page_display_pt`:** pontos PDF, com origem no canto superior esquerdo da
página como exibida (com /Rotate aplicado) e relativa à CropBox. Equivale ao viewport do PDF.js na
escala 1. Zoom e rotação da interface são transformações de visualização em `frontend/lib/geometry.ts`
(testadas em todas as rotações e escalas). O extrator converte as coordenadas do pdfplumber
(MediaBox rotacionada) para esse espaço em `render.display_transform`. Os testes verificam o
alinhamento com o raster em páginas com CropBox e com rotação.

**Versionamento:** cada conjunto de operações gera uma `Revision` com o documento completo
(comprimido) e o hash. O projeto aponta para a revisão corrente. Desfazer volta ao pai, refazer
segue o filho mais recente e restaurar cria uma nova revisão (nada é apagado). "Salvar versão"
nomeia a revisão corrente. Exportações guardam a revisão e o hash; quando o hash corrente muda, a
interface mostra a exportação como desatualizada.

**Toda alteração** passa por `engine/ops.py`, seja manual ou de IA: operações tipadas, validadas e
aplicadas atomicamente sobre uma cópia, com verificação de integridade referencial. Dividir e unir
mantêm a proveniência; um parágrafo pode ter várias origens (ex.: quebra entre páginas) e uma página
pode originar vários trechos.

## Fluxos

1. **Importação:** validação (assinatura `%PDF-`, abertura via pdfium, senha, limites), o original é
   gravado como somente leitura e a tarefa `extract` é criada.
2. **Extração:** palavras com fonte e tamanho → linhas → detecção de calhas (colunas) → ordem de
   leitura por faixas → blocos → classificação com evidências → tabelas por linhas desenhadas →
   imagens e regiões vetoriais → legendas vinculadas por proximidade → notas e chamadas sobrescritas
   → parágrafos unidos entre páginas → níveis de título por tamanho de fonte e outline.
   Cabeçalhos e rodapés combinam **posição** (margem), **padrão** (texto normalizado, dígitos → #),
   **repetição** (≥25% das páginas) e **tamanho de fonte** (≤ corpo×1,15). Por isso, uma frase de
   corpo que se repete não é excluída.
3. **Exportação:** `output_elements` (incluídos, sem filhos de visuais incluídos) → capítulos (fluido)
   ou páginas (fixo) → XHTML → nav, NCX, OPF, OCF → integridade → EPUBCheck.
4. **IA visual** (`ai/visual.py`): snapshot → exporta a candidata → captura → referência do PDF com
   máscara nos excluídos → modelo → operações validadas → nova candidata → nova medição → aceite ou
   rollback. Para em convergência, sem melhora, regressão, limite de iterações, tempo ou orçamento.
5. **IA editorial** (`ai/editorial.py`): exporta → lê a estrutura do EPUB e reconcilia pelas âncoras →
   inventário → guia editorial (persistido como resultado de etapa) → unidades por capítulo dentro da
   janela de contexto, com o resumo da unidade anterior → validação → candidata → preservação de
   texto e EPUBCheck → proposta ou aplicação automática (somente CSS ou CSS+estrutura). A cobertura
   por capítulo usa hash e IDs.

## Segurança

- Serviços publicados só em `127.0.0.1`. Modo individual sem autenticação: **não exponha na rede**.
- Worker com CPU, memória e PIDs limitados, `no-new-privileges` e usuário não root.
- PDF.js com `isEvalSupported: false` e XFA desativado. A prévia não executa scripts e tem CSP.
  Leitura de EPUB com proteção contra travessia de caminho, número de entradas, tamanho expandido e
  razão de compressão.
- O endereço do Ollama é validado contra `OLLAMA_ALLOWED_HOSTS` (definido pelo administrador),
  só aceita http/https e não aceita credenciais nem caminho.
- O texto do PDF e do EPUB é tratado como dado. O prompt de sistema manda ignorar instruções contidas
  no documento, e as respostas só podem conter operações da lista permitida, validadas no backend.
  HTML, CSS ou scripts arbitrários nunca são aceitos.
- A auditoria de IA registra modelo, finalidade, tokens, custo estimado e um resumo do pedido
  (página e contagens), nunca o conteúdo integral nem chaves.
