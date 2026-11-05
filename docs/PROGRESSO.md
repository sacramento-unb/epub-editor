# Registro de progresso (retomada entre sessões)

Especificação: `PROMPT_CONVERSOR_PDF_EPUB_IA.md` (raiz).
`PROMPT_EXECUCAO_PDF_EPUB_IA.md` **não existe** no repositório nem no disco (verificado em
2026-10-23), então seguimos só o primeiro.

O código anterior em `app/` (conversor MIT baseado em PyMuPDF) já estava removido no working tree
antes desta implementação. A remoção foi preservada e nada foi restaurado. O container antigo
`epub_editor-ebook-formatter-1` (porta 8001) estava rodando e não foi tocado.

Legenda: ✅ implementado e verificado · 🧪 implementado e verificado só com IA simulada ·
⛔ bloqueado por dependência externa · ⬜ pendente

## Estado por etapa da especificação (§15)

| Etapa | Itens | Estado |
|---|---|---|
| 1 | Importação (validação, senha, limites), extração nativa + OCR, modelo intermediário, visualização do PDF, seleção, exportação básica sem IA | ✅ |
| 2 | Edição semântica (tipo, nível, texto, estilos, legenda, alt, dividir, unir, agrupar, recorte manual, reordenar), persistência, revisões, desfazer/refazer, versões, prévia do EPUB real, fluido e fixo, EPUBCheck, integridade | ✅ |
| 3 | Provedores Gemini e Ollama, detecção de capacidades, limites, orçamento, cache, backoff, auditoria; reformatação editorial com operações validadas, guia persistido, unidades e cobertura | ✅ Gemini real (`gemini-3.8-flash`); ⛔ geração real no Ollama (sem modelo de chat) |
| 4 | Comparação visual: captura real do EPUB exportado, referência mascarada, iterações, indicadores (pontuação do modelo e SSIM no fixo), aceite e rollback | ✅ Gemini real (aceite e convergência); 🧪 rollback |
| 5 | Robustez (retomada, cancelamento, repetição), acessibilidade da interface (teclado, ARIA, tema), e2e, documentação, Docker Compose | ✅ |

Detalhes do que foi testado: [VERIFICACAO.md](VERIFICACAO.md). Limitações: [LIMITACOES.md](LIMITACOES.md).

## Mapa do código

- `backend/app/engine/`: `model.py` (modelo intermediário), `ops.py` (operações e invariantes de IA),
  `extract.py` (análise), `render.py` (pdfium e coordenadas), `epub.py` (gerador e leitura segura),
  `integrity.py` (preservação e EPUBCheck)
- `backend/app/ai/`: `providers.py`, `runtime.py`, `schemas.py`, `capture.py`, `visual.py`, `editorial.py`
- `backend/app/`: `main.py` (API), `jobs.py` (fila), `tasks.py`, `worker.py`, `services.py`, `db.py`,
  `storage.py`, `secrets.py`, `config.py`. Migrações em `backend/alembic/`.
- `backend/tests/`: `fixtures.py` (gera os PDFs de teste), `test_ops.py`, `test_engine.py`,
  `test_api.py`, `test_ai.py`, `e2e/test_ui.py`
- `frontend/`: `app/` (projetos, área de trabalho, histórico, configurações), `components/`,
  `lib/geometry.ts` (+ teste), `lib/api.ts`
- `docker-compose.yml`, `backend/Dockerfile`, `frontend/Dockerfile`, `.env.example`

## Próximos passos sugeridos
1. Testar a geração real no Ollama quando houver modelos de chat e de visão; com o Gemini, exercitar
   rollback, correção de OCR e layout fixo em PDFs reais.
2. Testar com PDFs reais representativos (livros técnicos, artigos acadêmicos, digitalizações de baixa
   qualidade) e ajustar as heurísticas: tabelas sem bordas, OCR por região em páginas mistas.
3. Avaliar em um leitor independente (Calibre, Apple Books, Thorium) e registrar os resultados.
4. Modo misto de layout (páginas fixas em livro fluido), MathML para fórmulas transcritas e verificadas.

## Sessões
### 2026-10-23 (sessão 1)
- Ambiente: Python 3.12 (uv) em `backend/.venv`, Node 26, Java 27, Tesseract 5.5.3 (só `eng` no
  host), Chromium 154, Docker 29.
- EPUBCheck 5.4.0 baixado em `tools/` (ignorado pelo git).
- Ollama 0.40.6 no host, só com `nomic-embed-text` e com o serviço parado. Foi iniciado
  temporariamente na porta 11435 para testar o adaptador e depois encerrado.
- Implementadas as etapas 1–5. Resultado: 51 testes de backend (incluindo e2e na pilha Docker),
  3 de frontend, typecheck e build ok.
- Pilha Docker validada nas portas 3100/8100 (`WEB_PORT=3100 API_PORT=8100 docker compose up -d`).
- Dev local: `.claude/launch.json` (api:8000, web:3000), com worker iniciado à parte.
- Continuação (mesma data): o `.env` tinha `GEMINI_API_KEY`, e os fluxos editorial e visual foram
  executados de verdade (ver VERIFICACAO.md). Corrigidos: truncamento por tokens de raciocínio,
  operações sem efeito, exclusão de projeto (permissões de diretório e ordem banco/arquivos;
  5 pastas órfãs removidas do volume).
- **Limite de tamanho:** o proxy do Next limita requisições a 10 MiB. Implementado envio em partes de
  8 MiB (`/api/uploads`, com retomada, senha sem reenviar o arquivo e limite exibido na interface).
  Validado com 90 MB pelo proxy. Resultado: 52 testes de backend, 3 de frontend, typecheck e build ok.
- **PDF real (135 páginas, livro em formato de tela 1080×1920 pt):** a capa era exportada, mas não
  aparecia na lista da prévia, e tinha `role="doc-cover"` inválido na `<section>` (erro no EPUBCheck).
  O sumário sem título não era reconhecido: os números alinhados à direita viravam uma "coluna", e o
  espaçamento grande quebrava cada linha em um parágrafo. Os níveis saíam errados, porque os títulos
  têm tamanho ajustado ao texto. Correções:
  - sumário detectado por números de página alinhados à direita, com entradas de várias linhas unidas,
    número removido, links para os títulos e exclusão por padrão;
  - calhas de coluna exigem largura e não podem conter só números;
  - abertura de capítulo por rótulo repetido acima do título ("01ª Mentira."), com a ilustração
    acima incluída no capítulo; demais níveis por grupos de tamanho;
  - um símbolo isolado não vira título nem fórmula;
  - quebras de linha dentro de palavras são normalizadas;
  - recortes com resolução limitada (~2000 px) e JPEG para imagens fotográficas (EPUB de 22 MB para 4,8 MB);
  - a verificação de duplicação considera trechos contidos em outros elementos.

  Novo PDF de teste: `book.pdf`. No PDF real: 32 entradas de sumário ligadas, 32 capítulos e EPUBCheck
  com 0 erros e 0 avisos. Observação: a extração desse PDF levou 371 s no contêiner (2 CPUs);
  vale investigar o desempenho em páginas grandes com muitas imagens.
- **PDF real, 2ª rodada:** a página de sumário "sumia" porque era excluída por padrão. Agora as entradas
  reconhecidas são mantidas como lista de links (`nav.toc-inline`). Havia ainda um bug de link
  relativo entre capítulos (`text/text/…`), corrigido. O texto estava "desformatado" porque o PDF
  tem **glifos sem Unicode** (números de versículo, pontuação) e **fontes sem nome** (sem como saber
  o que é negrito). Correções:
  - OCR por região dos glifos sem Unicode (psm 7; pontuação isolada por psm 10 restrito ou pela forma
    da tinta), unindo o resultado à palavra vizinha e marcando o trecho como recuperado (676 trechos
    neste livro, 6 não recuperados);
  - negrito pela espessura relativa do traço (tinta ÷ borda ÷ altura), com suavização de palavras curtas;
  - blocos separados por alinhamento (direita/centro), com o alinhamento levado ao EPUB; bloco todo em
    negrito ou itálico vira ênfase do bloco;
  - listas numeradas mantêm a numeração original (`<li value>`);
  - estilo global inferido dos parágrafos do PDF (recuo, alinhamento, espaçamento);
  - a verificação de duplicação compara o texto visível (sem marcadores de lista), atravessando elementos.

  Livro reanalisado no projeto do usuário: EPUBCheck com 0 erros e integridade ok. A extração completa
  levava ~380 s; depois da otimização de desempenho (abaixo), 46 s.
- **Desempenho:** RC4 nativo, OCR de glifos em mosaico e pontuação pela forma, e páginas em paralelo.
  O livro real passou de 371 s para 46 s no contêiner, com resultado idêntico. Detalhes em
  VERIFICACAO.md. 56 testes no backend.
