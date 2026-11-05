# Limitações conhecidas

## Extração e classificação
- **Heurísticas sem garantia:** títulos, listas, legendas, notas, fórmulas, cabeçalhos e colunas são
  inferidos por regras explícitas (tamanho e peso de fonte, posição, padrão, repetição). A
  "confiança" exibida é heurística e **não é probabilidade calibrada**. Os casos incertos viram pendências.
- **Ordem de leitura:** detecta calhas verticais entre colunas e lê por faixas. Layouts irregulares
  (revistas, boxes, texto ao redor de figuras) podem exigir reordenação manual.
- **Tabelas:** só são detectadas quando têm **linhas desenhadas** (estratégia de linhas do pdfplumber).
  Tabelas sem bordas viram parágrafos; nesse caso use um recorte manual do tipo "Tabela (como imagem)".
  O cabeçalho é assumido na primeira linha (pendência), e células mescladas detectadas viram
  `colspan` com pendência de revisão.
- **Fórmulas:** detectadas por densidade de símbolos e fontes matemáticas. São **preservadas como
  recorte de imagem** com o texto extraído no `alt` e num equivalente oculto. **MathML não é gerado**,
  porque não há transcrição confiável. Nenhuma fórmula é inventada.
- **Figuras vetoriais:** agrupadas por proximidade de curvas e retângulos. O recorte pode precisar de
  ajuste manual (agrupar, Shift+arrastar). Rótulos dentro da figura ficam no recorte e não viram texto.
- **Hifenização:** só é desfeita no fim de linha quando a parte seguinte começa com minúscula e a
  palavra composta não aparece com hífen em outro lugar do documento. Cada caso fica nas evidências
  do elemento. Compostos que só aparecem quebrados no fim de linha podem perder o hífen; confira as evidências.
- **Glifos sem Unicode** (texto vazio ou `(cid:N)` na camada do PDF): a região é lida por OCR e o
  texto fica marcado como "recuperado por OCR", com pendência. Pontuação isolada usa OCR de caractere
  único ou a forma da tinta (`.`, `,`, `:`, `;`). Quando nada é reconhecido, há aviso na análise.
- **Negrito sem nome de fonte:** estimado pela espessura do traço na página renderizada. Funciona bem
  em texto com fundo uniforme, mas pode errar em palavras muito curtas, em fundos texturizados ou em
  páginas quase todas em negrito. Itálico só é detectado pelo nome da fonte.
- **OCR:** só em páginas sem camada de texto (menos de 20 caracteres e imagem grande). Páginas mistas
  com texto dentro de imagens não passam por OCR por região nesta versão. A qualidade depende do
  idioma instalado: no host de teste só havia `eng`, então "confiança" saiu como "confianga" no
  português; a imagem Docker instala `por`.
- **Links internos do PDF:** resolvidos quando o destino é explícito ou nomeado. Se o destino for
  excluído, o link vira texto simples (aviso na exportação).
- **Marcas d’água:** detectadas só como texto rotacionado grande. Marcas em imagem ou com transparência não são detectadas.

## EPUB
- **Fluido:** a paginação não reproduz o PDF. A capa só é detectada como imagem grande na primeira página.
- **Layout fixo:** posiciona texto e imagens nas coordenadas originais com fontes substitutas, então
  o texto pode transbordar ou ficar mais curto que a caixa original. **Fundos, fios e gráficos
  vetoriais da página não são reproduzidos** (não rasterizamos a página inteira para não transformar
  o livro em imagens). Leitores variam bastante no suporte a layout fixo.
- **Modo misto** (páginas fixas dentro de livro fluido) não está implementado.
- **Fontes:** nenhuma fonte é incorporada, porque não há verificação de licença de incorporação.
  Usamos pilhas de fontes do sistema.
- **Acessibilidade:** usamos marcação semântica (`epub:type`, papéis ARIA de notas, page-list, `alt`),
  mas **nenhuma conformidade de acessibilidade é declarada**: os metadados `schema:accessibility*`
  não foram avaliados nem incluídos. Imagens sem texto alternativo saem com `alt=""` e uma pendência.

## IA
- **Gemini:** testado de verdade só com `gemini-3.8-flash`, num PDF sintético pequeno; rollback,
  correção de OCR e layout fixo foram exercitados só com mock.
- **Ollama:** a geração real não foi testada (só há um modelo de embeddings no host). Listagem,
  detecção e erros foram testados contra o Ollama 0.40.6. Os modelos citados no README são exemplos,
  **não testados**.
- **Modelos que "pensam":** os tokens de raciocínio contam no limite de saída e são cobrados como saída.
  Limites muito baixos truncam a resposta; o erro é reportado com orientação.
- **Pontuação visual:** é a avaliação do próprio modelo (0–10), um indicador e não uma medida objetiva.
  O SSIM (só no layout fixo) é auxiliar e não mede qualidade editorial. Uma melhora só é aceita se
  integridade e EPUBCheck não piorarem.
- **Comparação no modo fluido:** compara o recorte vertical do EPUB que contém os elementos da página
  com a página do PDF. Quando os elementos se espalham por arquivos diferentes, usa o arquivo com mais
  elementos da página.
- **Concorrência de IA:** uma chamada por vez. O campo de concorrência não é configurável nesta versão.
- **Custos:** só há estimativa com preços cadastrados pelo usuário. O consumo de tokens vem do provedor
  quando ele o informa.
- **Respostas estruturadas** garantem forma, não correção. Tudo é validado de novo, mas uma sugestão
  válida pode ser editorialmente ruim; por isso o padrão é revisar antes de aplicar.

## Requisitos de hardware (orientação, não medição)
Nenhum modelo local foi executado nesta sessão; os números abaixo são referências gerais de
quantização de 4 bits, **não medidas aqui**:

| Tamanho do modelo | Memória aproximada (RAM/VRAM) |
|---|---|
| 3–4 B parâmetros | 3–4 GB |
| 7–8 B | 5–6 GB |
| 12–14 B | 9–10 GB |
| 27–32 B | 18–22 GB |

Modelos multimodais precisam de memória adicional para as imagens. A janela de contexto efetiva
reduz o tamanho das unidades de texto, e o sistema se ajusta a ela quando a capacidade é detectada.
O host de desenvolvimento tinha 12 GB de RAM e GPU NVIDIA sem driver ativo.

## Operação
- **Modo individual sem autenticação:** as portas ficam em `127.0.0.1`. Não exponha na rede sem um
  proxy autenticado.
- **Cancelamento cooperativo:** a interrupção acontece entre páginas e unidades. Uma etapa longa e
  indivisível (ex.: uma chamada de IA) termina antes de o cancelamento valer.
- **Reanálise do PDF** cria uma nova revisão a partir da extração. As edições anteriores continuam no
  histórico, mas não são reaplicadas automaticamente.
- **Reabrir após reinício:** tarefas interrompidas são retomadas pela etapa pendente. Etapas
  concluídas não são refeitas.
