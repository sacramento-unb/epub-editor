# Contratos das alterações propostas por IA

- Versão do contrato: `ai-ops/1` (`backend/app/ai/schemas.py`)
- Versão dos prompts: `2026-10-23.1`. Ela entra na chave de cache, então mudar o prompt invalida o cache.
- Schemas gerados a partir do código em [schemas/](schemas/):
  - `ai-visual-response.schema.json`: resposta da comparação visual (`diagnosis`, `score` 0–10, `operations`).
  - `ai-editorial-guide.schema.json`: guia editorial global.
  - `ai-editorial-response.schema.json` e `-ocr`: resposta por unidade (o `set_text` só aparece com correção de OCR).
  - `operations.schema.json`: operações tipadas do motor (as mesmas usadas pela edição manual).
  - `document.schema.json`: o modelo documental.

## Formato enviado ao modelo

Para funcionar com as saídas estruturadas de provedores diferentes, cada operação é um objeto **achatado**,
sem uniões:

```json
{"op": "set_style", "target_ids": ["p0003e002"], "style": [{"name": "align", "value": "center"}],
 "reason": "títulos de capítulo centralizados", "evidence": "imagem 1, topo", "risk": ""}
```

O backend converte cada item em uma operação tipada (`to_engine_op`) e a valida
(`OperationList` do pydantic). Depois verifica as invariantes (`check_ai_op`).

## Operações permitidas para IA

`set_style`, `set_global_style`, `set_heading_level`, `set_type` (apenas entre título, parágrafo,
item de lista, citação e legenda), `link_caption`, `move`, `set_chapter_break`, `split`, `merge`
(mesmo tipo e adjacentes) e `set_alt`.
`set_text` só existe com a opção **correção de OCR**, que vem desativada. Nesse caso a alteração vira
proposta pendente com comparação de palavras e nunca é aplicada automaticamente.

Ficam **fora** do alcance da IA: inclusão e exclusão (`set_included`), bloqueio, recortes,
agrupamento e metadados.

Estilos são **tokens enumerados**, como `align=center`, `emphasis=bold`, `size=large` e
`spacing_before=small`. Propriedades globais têm faixas fixas, como `line_height` entre 1,0 e 2,2.
CSS, HTML e JavaScript arbitrários não são aceitos.

## Rejeição

Uma operação é rejeitada, com o motivo registrado na proposta, quando:

- o ID de destino não existe;
- o destino está **bloqueado** ou **excluído** (a IA nunca reinclui nem altera itens excluídos);
- o destino está fora do escopo da unidade (página ou capítulo enviados);
- a operação não está na lista permitida ou não é permitida no modo escolhido (ex.: "somente CSS");
- os valores são inválidos ou a operação está malformada;
- a versão-base mudou: ao aprovar uma proposta, cada operação é validada de novo sobre a versão corrente.

As operações são aplicadas atomicamente numa versão candidata e as dependências são recalculadas.
Depois disso:

- **Preservação de texto:** sem permissão de texto, o texto normalizado de todos os elementos tem de
  ser idêntico. Dividir e unir são aceitos se o texto total se mantém.
- **EPUBCheck:** a candidata não pode ter mais erros que a base.
- **Integridade da exportação:** elementos ausentes, números perdidos, texto inesperado ou duplicado.
- **Visual:** a pontuação do modelo precisa subir pelo menos 0,25 sem regressão das verificações
  acima; caso contrário, a versão anterior é restaurada e o motivo fica registrado.

Respostas estruturadas garantem a **forma** quando o provedor as suporta, não a verdade nem a correção
semântica. Por isso tudo é validado de novo no backend. Uma resposta fora do schema ou com JSON
inválido é registrada como `invalid` e a unidade fica "não revisada"; a última versão válida é
preservada.
