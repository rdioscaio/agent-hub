---
name: review-loop
description: Executa um ciclo de revisão técnica com crítica externa via ask_gpt e consolidação pragmática.
---

Use esta skill quando houver arquivos alterados e a mudança não for trivial.
Objetivo: fechar um ciclo completo de implementação + crítica técnica + revalidação antes de seguir.

Quando esta skill for invocada, siga este fluxo:

## 1. Identificar a mudança atual
- Qual o objetivo da mudança?
- Quais arquivos foram alterados?
- Qual o impacto esperado?

Use `git diff` e/ou `git status` para identificar automaticamente.

## 2. Montar pacote mínimo para revisão
Colete apenas o necessário:
- diff dos arquivos alterados
- trechos relevantes do código
- saída de testes
- saída de lint, quando existir no projeto
- dúvida arquitetural específica, se houver

Não envie o repositório inteiro.

## 3. Fazer revisão crítica
- Use o subagente `gpt-critic` para análise técnica inicial.
- Em mudanças relevantes, chame `ask_gpt` como contraponto crítico externo com pacote mínimo:
  - `purpose`: descreva o que está sendo revisado
  - `question`: inclua diff + contexto mínimo + pergunta específica
  - `data_policy`: "summary_only"
  - `max_tokens`: 1500
- Só pule `ask_gpt` em casos triviais e registre o motivo.

## 4. Classificar feedback
Organize cada ponto retornado em:
- **bloqueante** — deve ser resolvido agora
- **importante** — deve ser resolvido antes de seguir
- **opcional** — pode ser adiado

## 5. Aplicar somente feedback relevante
- Aplique bloqueantes e importantes.
- Registre opcionais como pendência.
- Rejeite com justificativa o que não fizer sentido no contexto do projeto.

## 6. Revalidar
- Rode novamente a validação mínima aplicável.
- Reexecute `python3 tests/smoke_test.py` quando a mudança afetar comportamento relevante.
- Rode lint também, quando houver ferramenta configurada no projeto.
- Se não houver lint configurado, registre essa ausência no fechamento.
- Confirme que nada quebrou.

## 7. Entregar resumo final

Produza um resumo com este formato:

```
### Resumo de revisão

**Mudanças feitas:** [lista]
**Críticas aceitas:** [lista com origem]
**Críticas rejeitadas:** [lista com justificativa]
**Validações executadas:** [testes, lint, smoke, etc.]
**Riscos remanescentes:** [lista ou "Nenhum identificado."]
**Pendências:** [lista ou "Nenhuma."]
```

Registre note com convenção `[GPT-CONSULT]` para cada chamada ao `ask_gpt`:
```
[GPT-CONSULT] role=<counterpoint|auditor> | purpose=<motivo> | result=<agreed|diverged|partial> | action=<adopted|discarded|noted>
```
