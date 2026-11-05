# Conversor e editor de PDF para EPUB (com IA opcional)

Aplicação web que transforma PDFs em EPUB 3 editáveis. Você revisa os elementos detectados (títulos,
parágrafos, listas, tabelas, figuras, notas, cabeçalhos, números de página…), marca o que entra no
livro, edita a estrutura e exporta em **modo fluido** (padrão) ou **layout fixo**. O EPUB é validado
com o **EPUBCheck**.

Há dois fluxos de IA **opcionais e independentes**:

- **Fidelidade visual:** compara a renderização real do EPUB exportado com o PDF e propõe ajustes reversíveis.
- **Reformatação editorial:** lê XHTML, CSS e sumário do EPUB e melhora a apresentação, sem reescrever o conteúdo.

A conversão, a edição manual, a prévia e a exportação funcionam **sem chave de API e sem modelo instalado**.

> Não há conversão perfeita de qualquer PDF. A extração é heurística, e as classificações incertas
> aparecem como pendências editáveis. Veja [docs/LIMITACOES.md](docs/LIMITACOES.md).

## Executar com Docker (recomendado)

Pré-requisitos: Docker com Compose v2.

```bash
cp .env.example .env
```

Edite o `.env` e defina `APP_SECRET_KEY`. Ela é necessária para salvar a chave do Gemini pela interface
e senhas de PDFs protegidos. Para gerar uma:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Depois suba os serviços:

```bash
docker compose up -d --build
```

Abra <http://localhost:3000>. Os serviços:

| Serviço | Função | Porta (apenas 127.0.0.1) |
|---|---|---|
| `web` | Interface Next.js/React/TypeScript | 3000 |
| `api` | FastAPI, que aplica as migrações Alembic na partida | 8000 |
| `worker` | Tarefas longas: análise, exportação, validação, IA (CPU/memória limitadas) | — |
| `db` | PostgreSQL 17 | — |
| `ollama` | Opcional, perfil `ollama` | 11434 |

Logs e parada:

```bash
docker compose logs -f worker
```

```bash
docker compose down
```

Os dados ficam nos volumes `pgdata` e `appdata`.

## Desenvolvimento local (sem Docker)

Pré-requisitos: Python 3.12, Node 24+, Java 17+ (EPUBCheck), Tesseract com `por` e `eng`, Chromium
(para a comparação visual).

```bash
cd backend && uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements-dev.txt
```

EPUBCheck: baixe o release oficial em `tools/` (a pasta é ignorada pelo git):

```bash
mkdir -p tools && curl -L -o tools/epubcheck.zip https://github.com/w3c/epubcheck/releases/download/v5.4.0/epubcheck-5.4.0.zip && unzip -o tools/epubcheck.zip -d tools
```

API (SQLite local em `backend/data/`):

```bash
cd backend && DATABASE_URL=sqlite:///./data/dev.db APP_SECRET_KEY=troque-por-um-segredo-longo CHROMIUM_PATH=/usr/bin/chromium .venv/bin/uvicorn app.main:app --port 8000
```

Worker (em outro terminal):

```bash
cd backend && DATABASE_URL=sqlite:///./data/dev.db APP_SECRET_KEY=troque-por-um-segredo-longo CHROMIUM_PATH=/usr/bin/chromium .venv/bin/python -m app.worker
```

Frontend:

```bash
cd frontend && npm install && API_URL=http://127.0.0.1:8000 npm run dev
```

## Uso

1. **Projetos → Importar:** arraste um PDF (até `MAX_UPLOAD_MB`, padrão 200 MB, e `MAX_PAGES`, padrão
   2000). O navegador envia o arquivo em partes de 8 MiB, porque o proxy do Next aceita no máximo
   10 MiB por requisição; o envio é retomável. A assinatura, a integridade, o tamanho e o número de
   páginas são validados. Para PDF protegido, a senha é pedida e nenhuma proteção é contornada. A análise roda
   em segundo plano: extração nativa primeiro, OCR (Tesseract) só nas páginas sem texto.
2. **Área de trabalho:**
   - **Painel esquerdo:** árvore por capítulo, página ou tipo. Tem caixas de inclusão (grupos com estado
     parcial), busca, filtros (tipo, excluídos, pendências, bloqueados) e arrastar e soltar para
     reordenar elementos e capítulos. Ctrl/Shift+clique seleciona vários.
   - **Ações em lote:** incluir ou excluir a seleção, a página, o capítulo, um intervalo de páginas
     ("1-3, 7") ou "equivalentes" (mesmo tipo e mesmo padrão de texto, como cabeçalhos repetidos).
     Exclusões individuais explícitas são respeitadas pelas ações em grupo.
   - **Inspetor:** tipo semântico, nível de título, texto, legenda, texto alternativo e estilos editoriais.
     Também permite bloquear contra IA, dividir no cursor, unir com o próximo, agrupar e desagrupar.
   - **PDF:** zoom e rotação com overlays alinhados. Clique seleciona o elemento; **Shift+arrastar** cria
     um recorte manual. "Mostrar detecções" só oculta os destaques e não muda a exportação.
   - **EPUB:** prévia do arquivo exportado de verdade, com presets de tela, tamanho de fonte, leitura
     contínua ou paginada e tema. Clicar no texto seleciona o elemento.
   - Toda edição é salva automaticamente como revisão. Use **Ctrl+Z** e **Ctrl+Shift+Z** para
     desfazer e refazer, **Salvar versão** para marcar um ponto e **Histórico** para restaurar.
3. **Gerar EPUB:** escolha fluido ou fixo. O EPUB passa pelo EPUBCheck e pela verificação de integridade
   do conteúdo. Depois use **Baixar EPUB**.
4. **IA (opcional):** configure em *Configurações de IA* e use **Comparar visualmente com IA** ou
   **Reformatar com IA**. Antes de iniciar, o diálogo mostra o que será enviado e para onde.
   As propostas aparecem em **Propostas de IA**, onde você aprova tudo ou alteração por alteração.

### Executar sem IA

É o padrão (modo "Sem IA"). Nada é enviado a serviços externos.

### Configurar o Gemini

1. Em *Configurações de IA*, escolha **Gemini** e salve a chave. Ela fica só no servidor, cifrada com
   `APP_SECRET_KEY`, e nunca volta ao navegador. Alternativa: `GEMINI_API_KEY` no `.env`.
2. Informe os modelos textual e multimodal ("Listar modelos disponíveis" ajuda) e clique em
   **Detectar capacidades**. Isso faz uma chamada mínima com uma imagem e um schema JSON.
3. Para estimar custos, cadastre os preços por milhão de tokens. Sem preço configurado, nenhuma
   estimativa é exibida.

O SDK usado é o `google-genai` (2.33.0), com `models.generate_content` e `response_json_schema`.
O modelo padrão `gemini-3.8-flash` vem do `.env.example` e dos exemplos da documentação oficial.
Confira sempre a lista atual de modelos.

### Configurar IA local (Ollama)

1. Instale o [Ollama](https://ollama.com) no host ou use `docker compose --profile ollama up -d`.
2. Baixe um modelo textual e um multimodal. Os nomes abaixo são exemplos; escolha conforme a sua
   memória (veja a tabela em [docs/LIMITACOES.md](docs/LIMITACOES.md)):

   ```bash
   ollama pull qwen3:8b
   ```

   ```bash
   ollama pull gemma3:4b
   ```

3. Em *Configurações de IA*, escolha **IA local** e informe o endereço:
   - Docker → Ollama no host: `http://host.docker.internal:11434`. No Linux, o Ollama precisa escutar
     em `0.0.0.0`: use `OLLAMA_HOST=0.0.0.0 ollama serve` ou edite o serviço do systemd.
   - Ollama no perfil do compose: `http://ollama:11434`.
   - Desenvolvimento sem Docker: `http://localhost:11434`.

   Os hosts aceitos são definidos pelo administrador em `OLLAMA_ALLOWED_HOSTS`.
4. Selecione os modelos e clique em **Detectar capacidades**. A detecção combina `/api/show` com um
   teste ativo com imagem. Sem visão confirmada, a comparação visual fica desabilitada.
5. O modo local **nunca** recorre à nuvem. Falta de memória, timeout ou capacidade incompatível geram
   um erro claro e o trabalho é preservado.

**GPU:** no Ollama do host, use o driver NVIDIA/ROCm do sistema. No contêiner, descomente `deploy`
no serviço `ollama`, o que requer o NVIDIA Container Toolkit. Sem GPU, os modelos rodam em CPU,
mais devagar.

### Validar e exportar

Cada exportação registra a versão do EPUBCheck, os erros e avisos com localização, a verificação de
integridade (elementos ausentes, números perdidos, texto inesperado ou duplicado) e os avisos do
gerador, como links removidos para evitar links quebrados. Tudo aparece no **Histórico**.
O botão **Validar** roda as verificações de novo sobre a última exportação.

### Backup e restauração

Backup:

```bash
docker compose exec db pg_dump -U epub epub > backup.sql
```

```bash
docker run --rm -v pdf-epub_appdata:/data -v "$PWD":/b alpine tar czf /b/appdata.tgz -C /data .
```

Restauração, com os serviços `api`, `worker` e `web` parados:

```bash
docker compose exec -T db psql -U epub epub < backup.sql
```

```bash
docker run --rm -v pdf-epub_appdata:/data -v "$PWD":/b alpine tar xzf /b/appdata.tgz -C /data
```

Use o mesmo `APP_SECRET_KEY`. Sem ele, chaves e senhas cifradas não podem ser lidas e precisam ser
cadastradas de novo.

## Testes

Backend (unitários, integração com PDFs gerados localmente, API e IA com provedor simulado):

```bash
cd backend && .venv/bin/python -m pytest -q
```

Frontend (transformações de coordenadas) e checagem de tipos:

```bash
cd frontend && npm test && npm run typecheck
```

Para gerar os PDFs de exemplo usados nos testes:

```bash
cd backend && .venv/bin/python -m tests.fixtures ../samples
```

Os resultados medidos e o que foi ou não verificado de verdade estão em
[docs/VERIFICACAO.md](docs/VERIFICACAO.md).

## Documentação

- [Arquitetura e modelo documental](docs/ARQUITETURA.md)
- [Contratos das alterações de IA](docs/SCHEMAS_IA.md) e [JSON Schemas](docs/schemas/)
- [Dependências, versões e licenças](docs/DEPENDENCIAS.md)
- [Verificação e testes executados](docs/VERIFICACAO.md)
- [Limitações](docs/LIMITACOES.md)
- [Registro de progresso](docs/PROGRESSO.md)
