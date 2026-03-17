# Instruções do projeto

## Contexto
Agent Hub MCP — hub de orquestração multi-agente com SQLite.
Stack principal: Python, FastMCP e SQLite.
Repo: `~/agent-hub-mcp`. Estrutura: `hub/` (core), `tools/` (MCP tools), `server.py` (entry point).
Validação principal do projeto: `python3 tests/smoke_test.py`.

## Modo de trabalho
- Antes de editar, inspecione a stack, os scripts, os testes e as convenções.
- Prefira mudanças pequenas, reversíveis e fáceis de validar.
- Não reestruture módulos sem necessidade comprovada.
- Trabalhe por diff pequeno. Um commit por mudança lógica.
- Explicite trade-offs relevantes e mantenha rastreabilidade das decisões tomadas durante a mudança.
- Preserve o estilo existente: docstrings no topo, type hints, params opcionais por último.

## Regras arquiteturais
1. Aditivo, não destrutivo — nenhuma função existente muda de comportamento.
2. Advisory antes de enforced — checklist, domain, métricas informam; enforcement é opt-in por playbook (ex: `work/automation`).
3. SQLite-first — sem dependência externa nova.
4. Sem breaking change — retornos existentes inalterados, params novos são opcionais e últimos.
5. Separação de camadas — `hub/db.py` (estrutural) → `hub/bootstrap.py` (funcional) → `server.py` (aplicação).
6. `ensure_ready()` propaga erros — startup parcial é pior que crash.
7. `classify_domain` é interno — NÃO é MCP tool.
8. Falhas de métricas — warning via audit, nunca silencioso, nunca quebra fluxo principal.
9. Promoção de conhecimento é explícita — nenhum fluxo promove automaticamente.

## Fluxo obrigatório após mudanças relevantes
1. Resuma a intenção da mudança.
2. Identifique arquivos alterados e impacto esperado.
3. Rode a validação mínima aplicável.
   Se houver ferramenta de lint configurada no projeto, rode lint também.
   Se não houver, registre explicitamente essa ausência no fechamento.
4. Faça revisão técnica local.
5. Envie pacote mínimo para revisão crítica via `ask_gpt`.
6. Classifique o feedback em: bloqueante, importante, opcional.
7. Aplique apenas críticas tecnicamente relevantes.
8. Revalide.
9. Registre o fechamento da mudança.
10. Só então avance para o próximo módulo.

Considere "mudança relevante" qualquer alteração que envolva:
- código de produção
- comportamento observável
- contrato MCP
- persistência ou schema
- segurança
- concorrência
- integração externa
- tratamento de erro
- lógica de orquestração

`ask_gpt` pode ser pulado apenas em:
- typo
- formatação
- documentação
- testes sem impacto funcional
- configuração sem efeito comportamental

Quando pular, registre o motivo explicitamente.

## Uso do ask_gpt
- Use `ask_gpt` como crítico técnico, não como autor principal.
- Em mudanças relevantes, a consulta ao `ask_gpt` é parte padrão do fluxo.
- Envie apenas contexto mínimo necessário:
  - diff
  - trechos alterados
  - erro observado
  - saída de testes/lint
  - dúvida arquitetural específica
- Nunca envie o repositório inteiro sem necessidade.
- Limite o contexto para reduzir custo, ruído e chance de revisão genérica.
- Após cada consulta, registre note com convenção `[GPT-CONSULT]`:
  ```
  [GPT-CONSULT] role=<counterpoint|auditor> | purpose=<motivo> | result=<agreed|diverged|partial> | action=<adopted|discarded|noted>
  ```

## Critérios de qualidade
- Preservar compatibilidade com a base atual.
- Evitar duplicação.
- Evitar acoplamento desnecessário.
- Tratar erros explicitamente.
- Não esconder falhas com fallback silencioso.
- Preferir clareza operacional a abstração prematura.

## Checklist final de conclusão
Antes de encerrar uma mudança, registre:
- intenção da mudança
- arquivos alterados
- validações executadas
- consulta ao `ask_gpt` ou motivo explícito do skip
- críticas aceitas
- críticas rejeitadas com justificativa
- riscos remanescentes
- pendências ou follow-up recomendado

## Uso diário
- Mudança pequena e relevante: implementar, validar, consultar `ask_gpt`, aplicar o que fizer sentido, revalidar, registrar fechamento.
- Revisão mais cuidadosa: acionar o subagente `gpt-critic` e usar `ask_gpt` como contraponto externo.
- Fluxo repetível: invocar a skill `review-loop`.
