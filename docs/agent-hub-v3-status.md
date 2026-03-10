# Agent Hub v3 — Checkpoint Consolidado

Data: 2026-03-10 | Versão: v3.2

---

## Estado Atual

**v3.0 + v3.1 + v3.2 concluídas. 150/150 testes passando. Zero regressão.**

27 tools MCP, 10 tabelas SQLite, 7 domínios classificados e propagados em todo o fluxo de orquestração.
Bootstrap funcional separado. Domain advisory (sem enforcement). Métricas passivas. Playbooks advisory.

---

## Rodadas Concluídas

### v3.0 — Memória + Playbooks + Métricas (5 etapas)

| Etapa | Escopo | Resultado |
|-------|--------|-----------|
| E1 — Schema e Migrações | +6 tabelas, +1 coluna (claimed_at), +6 índices | 38/38 testes |
| E2 — F1: Memória | 4 tools MCP (store_memory, recall_memory, record_decision, query_decisions) | 69/69 testes |
| E3 — F2: Playbooks | 2 tools MCP (get_playbook, validate_checklist) + seed interno | 93/93 testes |
| E4 — F3: Métricas | 1 tool MCP (get_metrics) + collect_task_metric interno | 117/117 testes |
| E5 — Validação Integrada | Smoke test completo, impact summary, docs, memória | 117/117 testes |

### v3.1 — Bootstrap Fix + Domain Router

| Componente | O que fez |
|------------|-----------|
| Bootstrap | `hub/bootstrap.py` com `ensure_ready()` — ponto único de startup funcional. Separa estrutural (db.py) de funcional (bootstrap.py) de aplicação (server.py). Propaga erros, nunca silencia. |
| Domain Router | `hub/domain.py` com `classify_domain(title, description)` — função interna, NÃO MCP tool. 7 domínios. Keyword-based, word boundary, title peso 2, desc peso 1. Empate por prioridade fixa. Fallback: "general". |
| create_task | +param `domain: str = ""` (último, opcional). Vazio → auto-classifica. Preenchido → override validado. Inválido → erro. |
| Métricas | `collect_task_metric` usa `task.get("domain") or "general"` em vez de NULL. |
| DB | +coluna `domain TEXT DEFAULT 'general'` em tasks via `_TASK_MIGRATIONS`. |
| **Testes** | **+25 testes → 142/142 passando** |

### v3.2 — Domain na Orquestração

| Componente | O que fez |
|------------|-----------|
| submit_request | Classifica domain uma vez a partir do request original. Propaga para root + todas as tasks do plano. |
| record_review | Followup herda domain da task original. Fallback chain: task.domain válido → reclassify(title, desc) → "general". |
| Consistência | Todas as tasks numa árvore têm domain consistente. Review tasks NÃO recebem "process" — herdam domain do request. |
| **Testes** | **+8 testes → 150/150 passando** |

---

## Arquivos Criados (5)

| Arquivo | Rodada | Conteúdo |
|---------|--------|----------|
| `tools/memory.py` | v3.0 E2 | 4 tools MCP (store_memory, recall_memory, record_decision, query_decisions) + helpers |
| `tools/playbooks.py` | v3.0 E3 | 2 tools MCP (get_playbook, validate_checklist) + seed_default_playbooks interno |
| `tools/metrics.py` | v3.0 E4 | 1 tool MCP (get_metrics) + collect_task_metric interno |
| `hub/bootstrap.py` | v3.1 | ensure_ready() — startup funcional idempotente |
| `hub/domain.py` | v3.1 | classify_domain, VALID_DOMAINS, DOMAIN_KEYWORDS |

## Arquivos Alterados (5)

| Arquivo | Rodadas | Alterações |
|---------|---------|------------|
| `hub/db.py` | v3.0 E1, v3.1 | +6 tabelas em _SCHEMA, +2 colunas em _TASK_MIGRATIONS (claimed_at, domain), +6 índices |
| `server.py` | v3.0 E2-E4, v3.1 | +3 imports de tools, +7 mcp.tool(), __main__ usa ensure_ready(), imports limpos, docstring atualizado |
| `tools/tasks.py` | v3.0 E4, v3.1 | +hooks de métricas em claim/complete/fail, +param domain em create_task, +import domain |
| `tools/orchestration.py` | v3.2 | +import classify_domain/VALID_DOMAINS, +domain= em submit_request e record_review |
| `tests/smoke_test.py` | v3.0-v3.2 | +112 testes (31+24+24+25+8), usa ensure_ready(), imports atualizados |

---

## Tools MCP Atuais (27)

| Grupo | Tools |
|-------|-------|
| Tasks (8) | create_task, get_task, claim_task, claim_next_task, heartbeat_task, complete_task, fail_task, list_tasks |
| Notes (2) | append_note, list_notes |
| Artifacts (2) | publish_artifact, read_artifact |
| Locks (2) | acquire_lock, release_lock |
| Delegation (2) | ask_gpt, delegate_task_to_gpt |
| Orchestration (4) | submit_request, record_review, list_task_tree, summarize_request |
| Memory (4) | store_memory, recall_memory, record_decision, query_decisions |
| Playbooks (2) | get_playbook, validate_checklist |
| Metrics (1) | get_metrics |

---

## Tabelas SQLite Atuais (10)

| Tabela | Rodada | Uso |
|--------|--------|-----|
| tasks | v1 (original) | Grafo de tarefas com dependências |
| notes | v1 | Notas por tarefa |
| artifacts | v1 | Artefatos publicados |
| locks | v1 | Locks nomeados por agente |
| audit_log | v1 | Log de auditoria |
| memory_entries | v3.0 E1 | Memória persistente por domínio |
| decisions | v3.0 E1 | Log de decisões arquiteturais |
| playbooks | v3.0 E1 | Playbooks advisory por kind/domain |
| task_metrics | v3.0 E1 | Métricas passivas de performance |
| retrospectives | v3.0 E1 | Preparação futura (vazia) |

---

## Regras Arquiteturais Vigentes

1. **Aditivo, não destrutivo** — nenhuma função existente muda de comportamento
2. **Advisory antes de enforced** — checklist, domain e métricas informam, nunca bloqueiam
3. **SQLite-first** — sem dependência externa nova
4. **Sem breaking change** — retornos existentes inalterados, params novos são opcionais e últimos
5. **Separação de camadas** — `hub/db.py` (estrutural) → `hub/bootstrap.py` (funcional) → `server.py` (aplicação)
6. **ensure_ready() propaga erros** — startup parcial é pior que crash
7. **classify_domain é interno** — NÃO é MCP tool, NÃO faz routing/enforcement
8. **Domain propagado na orquestração** — classify uma vez no submit_request, herda em record_review
9. **Falhas de métricas** — warning via audit, nunca silencioso, nunca quebra fluxo principal
10. **seed_default_playbooks e collect_task_metric são internos** — NÃO expostos como tools MCP

---

## Pendências e Riscos Remanescentes

| Item | Tipo | Detalhe |
|------|------|---------|
| retrospectives tabela vazia | By design | Criada para preparação futura, sem tool implementada |
| Tag filtering em Python | Performance | OK para <1000 entries. Se escalar, considerar FTS5 |
| Sem deduplicação automática de memória | By design | Agente deve chamar recall_memory antes de store_memory |
| classify_domain é keyword-based | Limitação | Pode classificar errado em edge cases. Advisory only, override manual disponível |
| delegate_task_to_gpt não propaga domain | Observação | Task já tem domain quando delegada. Sem impacto |
| Sem agent profiles | Próxima rodada | Agentes não têm capacidades declaradas. claim_next_task não faz matching por perfil |
| Sem domain-aware routing | Próxima rodada | Domain existe mas não influencia claim_next_task |

---

## Próximo Passo Recomendado

**Agent Profiles** — perfis de capacidade por agente, usados no claim_next_task para matching domain-aware.

Justificativa: domain já está classificado e propagado em todo o fluxo. O próximo ganho real é usar essa informação para direcionar tasks ao agente certo. Sem isso, domain é metadata inerte.

Dependências:
1. Agent Profiles (tabela + 2-3 tools MCP)
2. Domain-aware routing em claim_next_task
3. Testes de matching e fallback

Itens que NÃO devem entrar junto:
- Retrospective automática
- Checklist enforced
- GPT auditor bloqueante

---

## Handoff para Próxima Sessão

**Projeto**: agent-hub-mcp em `~/agent-hub-mcp`
**O que foi entregue**: v3.0 (memória + playbooks + métricas) + v3.1 (bootstrap fix + domain router) + v3.2 (domain propagação na orquestração).
**Estado**: 150/150 testes passando. 27 tools MCP, 10 tabelas, 7 domínios. Domain classificado e propagado em todo o fluxo.
**Checkpoint**: `docs/agent-hub-v3-status.md` (este arquivo)
**Memória persistente**: `~/.claude/projects/-home-rdios/memory/agent-hub.md`
**Próximo**: Agent Profiles + domain-aware routing em claim_next_task. Blueprint primeiro, implementar depois.
