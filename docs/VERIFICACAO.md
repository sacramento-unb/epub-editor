# Verificação: o que foi testado de verdade

Ambiente de 2026-10-23: Arch Linux (Omarchy), AMD Ryzen 5 PRO 6650H (12 threads), 12 GB de RAM,
Python 3.12.15, Node 26.7, OpenJDK 27, Tesseract 5.5.3 (só `eng` no host), Chromium 154,
Docker 29.9 com Compose 5.6.

Legenda: ✅ executado e aprovado · 🧪 coberto só com provedor simulado (mock) · ⛔ não executado (motivo indicado)

## Suítes automatizadas

| Suíte | Comando | Resultado |
|---|---|---|
| Backend completo (unitários, integração, API, IA mock, e2e) | `cd backend && E2E_BASE_URL=http://localhost:3100 .venv/bin/python -m pytest -q` | ✅ **52 aprovados** em 128 s (e2e contra a pilha Docker, com importação pela interface) |
| Frontend: geometria (zoom × rotação × ida e volta) | `cd frontend && npm test` | ✅ 3 aprovados |
| Frontend: tipos | `npm run typecheck` | ✅ |
| Frontend: build de produção | `npm run build` | ✅ |
| Migrações | `alembic upgrade head` + `alembic check` (SQLite) e na partida da API (PostgreSQL 17) | ✅ sem divergências |
| Imagens Docker | `docker compose build` + `up` | ✅ api, worker, web e db saudáveis |

## Critérios de aceitação da especificação (seção 14)

| # | Critério | Como foi verificado | Estado |
|---|---|---|---|
| 1 | Conversão completa sem IA; EPUBCheck sem erros | `test_epub_valid_and_complete`: 7 PDFs × fluido/fixo, **0 erros e 0 avisos** no EPUBCheck 5.4.0; também na imagem Docker | ✅ |
| 2 | Seleção persiste ao reabrir e reflete na exportação | `test_selection_persists_and_reflects_in_export` (reconecta ao banco, baixa o EPUB e procura o texto) e o e2e (recarrega a página) | ✅ |
| 3 | Exclusão em lote não remove parágrafos legítimos nem é desfeita pela IA | `test_headers_footers_vs_repeated_body` (frase de corpo repetida em 5 páginas continua incluída); `test_batch_include_respects_explicit_exclusion`; `test_ai_cannot_reinclude_excluded` | ✅ |
| 4 | Overlays alinhados em zoom e rotação | `geometry.test.ts` (4 rotações × 4 escalas, ida e volta); `test_coordinates_match_rendering` (bbox do modelo × raster do pdfium com CropBox e /Rotate 90); inspeção visual no navegador | ✅ |
| 5 | Duas colunas na ordem certa; sem duplicação de OCR nem perda silenciosa | `test_two_columns_reading_order`; `test_scanned_and_mixed_ocr` (página digital não duplicada); benchmark de 120 páginas: 480/480 parágrafos | ✅ |
| 6 | Tabelas, links, notas e sumário preservam relações; incertezas viram pendências | `test_tables_figures_notes_formula`, `test_paragraph_across_pages_and_hyphenation` (link externo, outline), `test_exclusions_reflected_and_links_not_broken` | ✅ |
| 7 | Layout fixo e fluido com metadados adequados | Verificação de `rendition:layout pre-paginated` e viewport no fixo; nav reflowable | ✅ |
| 8 | Comparação visual usa o EPUB exportado, respeita exclusões e limites, detecta regressão e faz rollback | Testes com captura real e modelo simulado (incluindo o rollback) **e execução real com Gemini** (ver abaixo) | ✅ (o rollback só foi exercitado com mock) |
| 9 | Reformatação percorre os capítulos e preserva o conteúdo; só estilo não altera o texto | Testes com mock **e execução real com Gemini**: 2/2 capítulos revisados, texto inalterado | ✅ |
| 10 | Modelo local só textual não habilita visão; falha local não vai para o Gemini | `test_text_only_local_model_disables_visual`, `test_local_failure_never_falls_back_to_gemini`; detecção real no Ollama 0.40.6 com modelo só de embeddings (texto ✗, visão ✗) | ✅ / 🧪 |
| 11 | Chave Gemini fora de logs, frontend e exportações | `test_gemini_key_never_exposed` (API, banco cifrado, auditoria, logs com redação, EPUB, código e bundle do frontend) | ✅ |
| 12 | Cancelamento, reinício do worker, falha de API e JSON inválido preservam a última versão válida | `test_cancel_retry_and_worker_restart` (heartbeat antigo → retomada; cancelamento na fila e cooperativo; repetição), `test_local_failure_never_falls_back_to_gemini`, `test_editorial_invalid_json_and_excluded_items` | ✅ / 🧪 |

## Teste de interface (ponta a ponta)

`tests/e2e/test_ui.py` (Playwright) rodou contra o dev local e contra a pilha Docker. Ele cobre:
importação, seleção na árvore com destaque no PDF, exclusão com diálogo de impacto, persistência após
recarregar, geração do EPUB pela interface, conteúdo da prévia real (sem a nota excluída), clique na
prévia selecionando o elemento e desfazer com Ctrl+Z. ✅ nos dois ambientes.

## Medições reais (host acima, sem Docker)

PDF sintético de 120 páginas (texto, cabeçalhos e rodapés, 12 capítulos, 12 imagens, 12 tabelas):

| Etapa | Tempo |
|---|---|
| Extração + classificação (sem OCR) | 5,1 s (≈43 ms/página) |
| Geração do EPUB | 0,01 s |
| Verificação de integridade | 0,12 s (ok) |
| EPUBCheck 5.4.0 | 3,2 s (0 erros, 0 avisos) |

Classificação obtida: 120 cabeçalhos e 120 números de página excluídos, 12 títulos, 480 parágrafos,
12 imagens com 12 legendas vinculadas e 12 tabelas. São documentos sintéticos e simples: **os números
não representam PDFs reais complexos**.

OCR (página A4 digitalizada a 200 dpi): na imagem Docker com `por+eng`, o texto saiu correto
("confiança", "125 kg", "2024"), com confiança média de 0,65 a 0,70. No host, só com `eng`, saiu
"confianga".

## Integrações externas

| Integração | Estado | Detalhe |
|---|---|---|
| EPUBCheck 5.4.0 | ✅ real | host e imagem Docker |
| Tesseract | ✅ real | `eng` no host; `por+eng` no Docker |
| Chromium / Playwright (captura) | ✅ real | host (Chromium 154) e contêiner (`capture_epub` executado no worker) |
| PostgreSQL 17 | ✅ real | pilha Docker |
| **Ollama** | ✅ parcial real | listagem, `/api/show`, detecção (modelo de embeddings → sem texto e sem visão), erros 400/404 e validação de host contra o Ollama 0.40.6 do host. **⛔ Geração com modelo de chat ou visão não executada:** nenhum modelo desse tipo instalado; baixar vários GB não foi feito sem autorização. |
| **Gemini** (`gemini-3.8-flash`, `google-genai` 2.33) | ✅ real (sessão 1, chave do `.env`) | listagem de modelos; detecção de capacidades (texto ✓, visão ✓, JSON ✓, contexto 1.048.576); fluxos editorial e visual completos na pilha Docker (detalhes abaixo) |
| Leitor EPUB independente | ⛔ indisponível | nenhum (Calibre, Foliate, MuPDF, KOReader) instalado. A compatibilidade foi avaliada só no leitor web (Chromium) e com o EPUBCheck. |

### Como rodar os testes reais de IA quando houver provedor

1. Gemini: defina a chave em *Configurações de IA*, escolha os modelos e use **Detectar
   capacidades**. Rode **Reformatar com IA** no modo "Revisar e escolher" sobre `simple.pdf`
   e depois **Comparar visualmente** com 1 página e 1 iteração. Confira em *Histórico → Uso de IA*
   os tokens informados pelo provedor.
2. Ollama: `ollama pull` de um modelo textual e de um multimodal, depois o mesmo procedimento no modo
   **IA local**.

## Execução real com Gemini (2026-10-23, pilha Docker, `simple.pdf` de 3 páginas)

- **Detecção:** o primeiro teste ativo falhou porque o limite de 64 tokens de saída foi consumido pelos
  **tokens de raciocínio** do modelo (`finish_reason=MAX_TOKENS`, 57 tokens de raciocínio). Corrigido:
  o teste usa 1024 tokens; uma resposta truncada vira um erro explicativo; o uso registrado soma os
  tokens de raciocínio (que são cobrados como saída); o limite padrão de saída subiu para 8192.
- **Editorial** (perfil técnico, modo revisar): 3 chamadas, 2.894 tokens de entrada e 4.626 de saída;
  2/2 capítulos "revisado"; 11 operações válidas e 0 inválidas; texto inalterado; candidata com 0 erros no
  EPUBCheck e integridade ok. Observação: o guia repetiu valores globais já em vigor. Agora essas
  operações são marcadas `no_effect` e não viram proposta.
- **Visual** (página 1, fluido, até 2 iterações): 2 chamadas, 6.682 tokens de entrada e 3.033 de saída.
  Diagnóstico correto: o PDF usa parágrafos em bloco, sem recuo e alinhados à esquerda, e o EPUB estava
  justificado e com recuo. Operação proposta: `set_global_style {text_indent_em: 0, paragraph_spacing_em: 0.8, text_align: left}`.
  Pontuação do modelo de 8,5 para 9,5, aceita; parada por convergência. A proposta foi aplicada pela
  API e a nova exportação passou no EPUBCheck (0 erros) e na integridade.
- **Não exercitado com o modelo real:** rollback por regressão, correção de OCR e layout fixo.

## Limites de tamanho de arquivo (verificados)

| Limite | Valor | Origem | Tratamento |
|---|---|---|---|
| Corpo de requisição pelo frontend | **10 MiB** | `proxyClientMaxBodySize` padrão do Next.js 16 (proxy `/api`) | Antes, um PDF de 90 MB enviado pela interface **travava** (timeout de 300 s); pela API direta, entrava em 0,3 s. Agora a interface envia em **partes de 8 MiB** (`/api/uploads`) com retomada e novas tentativas. Teste real: 90,1 MB em 11 partes pelo proxy em 0,4 s; análise de 60 páginas em 8 s. |
| Tamanho do PDF | 200 MB (`MAX_UPLOAD_MB`) | aplicação | verificado antes do envio (na interface) e em cada etapa (na API) |
| Páginas | 2000 (`MAX_PAGES`) | aplicação | validado na importação |
| Requisição inline do Gemini | 20 MB | documentação da API | o PDF **nunca** é enviado inteiro: cada chamada visual leva 2 PNGs (lado ≤ 1600 px) e trechos de texto; o editorial usa unidades de ≤ 24.000 caracteres. Uma guarda recusa requisições acima de ~19 MB com mensagem clara. |

## Desempenho da análise (livro real de 135 páginas, 124 MB, protegido por senha de proprietário)

Perfil inicial (20 páginas, 70 s): descriptografia RC4 do pdfminer em Python puro (36%), interpretação
do conteúdo das páginas (~1.700 curvas por página) e 106 chamadas ao Tesseract (~0,14 s cada).

| Otimização | Efeito |
|---|---|
| RC4 pela implementação nativa da `cryptography` (`engine/fastpdf.py`; byte a byte idêntico, com teste para chaves de 5 a 16 bytes) | remove ~36% do tempo |
| Pontuação isolada pela forma da tinta antes do OCR; OCR dos demais glifos de uma página num único mosaico; Tesseract com `OMP_THREAD_LIMIT=1` | OCR de glifos: 15 s → 1,5 s (20 páginas) |
| Páginas em paralelo (processos `spawn`; `EXTRACT_WORKERS`, padrão min(4, CPUs do contêiner via cgroup)) | quase linear até 4 processos |

| Medição | Tempo |
|---|---|
| Antes (contêiner, 2 CPUs) | 371 s |
| Host, 1 processo | 108 s (0,80 s/página) |
| Host, 2 processos | 60 s |
| Host, 4 processos | 39 s (0,29 s/página) |
| **Contêiner, 4 CPUs (novo padrão `WORKER_CPUS=4`)** | **46 s** |

O modelo gerado com 1, 2 e 4 processos é **idêntico** (comparação do JSON completo; também há teste
automatizado). O que sobra é a interpretação de conteúdo do pdfminer (Python puro), proporcional à
quantidade de objetos gráficos da página.
