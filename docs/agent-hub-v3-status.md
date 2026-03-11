# Agent Hub v3 — Checkpoint Consolidado

Data: 2026-03-11 | Versão: v3.3 (validada)

---

## Estado Atual

**v3.0 + v3.1 + v3.2 + v3.3 concluídas. 186/186 testes passando. Validação real concluída. Zero regressão.**

30 tools MCP, 11 tabelas SQLite, 7 domínios classificados e propagados em todo o fluxo de orquestração.
Bootstrap funcional separado. Domain advisory com matching preferencial via agent profiles.
Métricas passivas. Playbooks advisory. Agent profiles com tuple ranking em claim_next_task.

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

### v3.3 — Agent Profiles + Domain-Aware Matching

| Componente | O que fez |
|------------|-----------|
| Schema | +tabela `agent_profiles` (agent_name UNIQUE, domains JSON, task_kinds JSON, max_concurrent, active). +1 índice. |
| Tools MCP | 3 novos: `register_agent` (upsert com active param), `get_agent_profile`, `list_agents` (com validação de domain). |
| Matching | `claim_next_task` usa tuple ranking `(domain_match, kind_match, priority, -created_at)` quando agente tem perfil ativo. Sem perfil = legacy inalterado. |
| Safety cap | `_MAX_CANDIDATES = 1000` — query sem LIMIT semântico quando perfil ativo, para ranking completo. Cap documentado no código. |
| Sem perfil vs generalista | `None` = legacy puro (priority only). `domains=[]` = generalista registrado (ranking por kind + priority, sem domain bonus). |
| Validações | domains ⊂ VALID_DOMAINS, task_kinds ⊂ VALID_TASK_KINDS, active ∈ {0,1}, max_concurrent ≥ 1, list_agents(domain) valida domain inválido → erro. |
| **Testes** | **+36 testes → 186/186 passando** |

#### Impacto prático da v3.3

Antes: `claim_next_task` pegava a task de maior prioridade disponível, ignorando domain. Um agente backend podia receber task frontend porque tinha prioridade maior. Domain era metadata inerte.

Depois: agentes com perfil registrado preferem tasks do seu domain mesmo que existam tasks de maior prioridade em outros domains. Se não houver task no domain do agente, faz fallback para a melhor disponível. Agentes sem perfil mantêm comportamento idêntico ao v3.2. Matching nunca bloqueia — só reordena.

---

## Arquivos Criados (6)

| Arquivo | Rodada | Conteúdo |
|---------|--------|----------|
| `tools/memory.py` | v3.0 E2 | 4 tools MCP (store_memory, recall_memory, record_decision, query_decisions) + helpers |
| `tools/playbooks.py` | v3.0 E3 | 2 tools MCP (get_playbook, validate_checklist) + seed_default_playbooks interno |
| `tools/metrics.py` | v3.0 E4 | 1 tool MCP (get_metrics) + collect_task_metric interno |
| `hub/bootstrap.py` | v3.1 | ensure_ready() — startup funcional idempotente |
| `hub/domain.py` | v3.1 | classify_domain, VALID_DOMAINS, DOMAIN_KEYWORDS |
| `tools/agents.py` | v3.3 | 3 tools MCP (register_agent, get_agent_profile, list_agents) + VALID_TASK_KINDS + helpers |

## Arquivos Alterados (5)

| Arquivo | Rodadas | Alterações |
|---------|---------|------------|
| `hub/db.py` | v3.0 E1, v3.1, v3.3 | +7 tabelas em _SCHEMA, +2 colunas em _TASK_MIGRATIONS (claimed_at, domain), +7 índices |
| `server.py` | v3.0 E2-E4, v3.1, v3.3 | +4 imports de tools, +10 mcp.tool(), __main__ usa ensure_ready(), docstring atualizado |
| `tools/tasks.py` | v3.0 E4, v3.1, v3.3 | +hooks de métricas, +param domain, +_get_active_profile helper, +tuple ranking em claim_next_task, +_MAX_CANDIDATES |
| `tools/orchestration.py` | v3.2 | +import classify_domain/VALID_DOMAINS, +domain= em submit_request e record_review |
| `tests/smoke_test.py` | v3.0-v3.3 | +148 testes (31+24+24+25+8+36), usa ensure_ready(), imports atualizados |

---

## Tools MCP Atuais (30)

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
| Agents (3) | register_agent, get_agent_profile, list_agents |

---

## Tabelas SQLite Atuais (11)

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
| agent_profiles | v3.3 | Perfis de capacidade por agente |

---

## Regras Arquiteturais Vigentes

1. **Aditivo, não destrutivo** — nenhuma função existente muda de comportamento
2. **Advisory antes de enforced** — checklist, domain, métricas e agent profiles informam, nunca bloqueiam
3. **SQLite-first** — sem dependência externa nova
4. **Sem breaking change** — retornos existentes inalterados, params novos são opcionais e últimos
5. **Separação de camadas** — `hub/db.py` (estrutural) → `hub/bootstrap.py` (funcional) → `server.py` (aplicação)
6. **ensure_ready() propaga erros** — startup parcial é pior que crash
7. **classify_domain é interno** — NÃO é MCP tool, NÃO faz routing/enforcement
8. **Domain propagado na orquestração** — classify uma vez no submit_request, herda em record_review
9. **Falhas de métricas** — warning via audit, nunca silencioso, nunca quebra fluxo principal
10. **seed_default_playbooks, collect_task_metric, _get_active_profile são internos** — NÃO expostos como tools MCP
11. **Agent profile matching é preferencial** — reordena candidatas, nunca bloqueia. Sem perfil = legacy puro.
12. **Sem perfil ≠ perfil generalista** — None (legacy, priority only) vs domains=[] (registrado, ranking por kind + priority)

---

## Pendências e Riscos Remanescentes

| Item | Tipo | Detalhe |
|------|------|---------|
| retrospectives tabela vazia | By design | Criada para preparação futura, sem tool implementada |
| Tag filtering em Python | Performance | OK para <1000 entries. Se escalar, considerar FTS5 |
| Sem deduplicação automática de memória | By design | Agente deve chamar recall_memory antes de store_memory |
| classify_domain é keyword-based | Limitação | Pode classificar errado em edge cases. Advisory only, override manual disponível |
| delegate_task_to_gpt não propaga domain | Observação | Task já tem domain quando delegada. Sem impacto |
| _MAX_CANDIDATES = 1000 | Safety cap | Limita candidatas na query quando perfil ativo. Se >1000 tasks ativas, otimizar com índice composto |
| max_concurrent não enforced | By design | Registrado no perfil, NÃO usado no matching desta rodada |
| list_agents(domain) exclui generalistas | By design | Generalistas (domains=[]) só aparecem na listagem sem filtro |
| Matching depende de perfil registrado | Operacional | Se agente não chamar register_agent antes de claim_next_task, matching não se aplica |
| classify_domain + agent profile desalinhados | Risco operacional | Se keywords de domain classificam "backend" mas agente registra domains=["api"], não vai casar. Domínios devem ser os 7 de VALID_DOMAINS |

---

## Validação Real Concluída — 2026-03-11

Objetivo executado: provar em uso real o fluxo `submit_request → claim_next_task → complete_task → record_review → summarize_request`, além de domain propagation, agent profiles, métricas, fallback e retrocompatibilidade.

### Evidências consolidadas

| Item | Resultado |
|------|-----------|
| Request de teste | `Fix API endpoint for user auth` |
| Domain esperado | `backend` |
| Domain real do root | `backend` |
| Domain real das subtasks | `backend` em `work`, `review` e `synthesize` |
| Agente que claimou work | `claude-backend` |
| Domain da work task | `backend` |
| Métrica gerada | Sim |
| Domain na métrica | `backend` |
| Review aprovada | Sim |
| Domain da review | `backend` |
| summarize_request | Estado coerente: `work` + `review` done, `synthesize` única ready task |
| Fechamento operacional completo | `synthesize` concluída, `ready_tasks=0` |
| Fallback funcionou | Sim — especialista backend claimou task frontend válida quando não havia match backend |
| Agente sem perfil manteve legacy | Sim — prioridade pura |
| Agente com perfil inativo manteve legacy | Sim — `active=0` ignorado no matching |
| audit_log | 0 entradas com `result_status='error'` durante a rodada |

### Conclusão objetiva

- Happy path aprovado
- Fallback aprovado
- Retrocompatibilidade aprovada
- Decisão: pode avançar

---

## Decisões Operacionais Vigentes

1. **Root task é envelope, não task operacional** — `task_kind="request"` organiza a árvore, mas não entra no fluxo normal de claim/complete.
2. **Request operacionalmente concluído** = todas as tasks operacionais em `done` + `ready_tasks=0` + `synthesize` concluída.
3. **Root pending por design** — `root_task_id` pode permanecer `pending` sem indicar erro ou incompletude.
4. **Validação real concluída antes da próxima rodada** — a próxima decisão técnica depende de uso disciplinado, não de mais feature adiantada.

---

## Operação Recomendada Antes da Próxima Rodada

### Sessões reais mínimas

1. **Rodar 3 a 5 sessões reais curtas** com o hub, sem roteiro guiado.
2. **Registrar perfis reais** para pelo menos um especialista backend, um generalista e, se fizer sentido, um agente focado em review.
3. **Capturar evidências mínimas por sessão**:
   - request enviado
   - domain esperado
   - domain real
   - agente que claimou
   - se houve fallback
   - se gerou `task_metrics`
   - se review fechou
   - se `synthesize` fechou
   - se apareceu `general` onde não devia

### Sinais a observar em produção

| Sinal | O que indica | Como medir |
|-------|-------------|------------|
| task_metrics.domain == agent_profiles.domains | Matching está funcionando | `get_metrics(agent="X")` → verificar se domain das métricas bate com o perfil |
| Rework rate por agente | Agente especialista erra menos no seu domain | `get_metrics(agent="X")` → aggregates.rework_rate |
| Tasks com domain "general" | classify_domain não reconheceu keywords | `get_metrics(domain="general")` → se for maioria, keywords precisam de ajuste |
| Tempo entre criação e claim | Matching não está atrasando claim | `get_metrics` → avg time_to_claim_ms antes e depois de registrar perfis |
| Fallback frequency | Com que frequência agente pega task fora do seu domain | Comparar task.domain vs profile.domains no audit_log |

### Edge cases do domain router que merecem atenção

1. **Requests bilíngues**: classify_domain usa keywords em inglês. Requests em português (ex: "corrigir endpoint de autenticação") podem cair em "general" porque "endpoint" casa mas "corrigir" não. Monitorar taxa de "general" em requests reais.
2. **Requests ambíguos**: "refactor API component" → backend (API) ou architecture (refactor)? A prioridade fixa resolve (backend > architecture), mas o resultado pode não ser o esperado pelo humano.
3. **Requests compostos**: "deploy the new React dashboard" → infra (deploy) ou frontend (React, dashboard)? Peso do title resolve, mas requests longos com keywords de vários domains são imprevisíveis.
4. **Keywords ausentes**: qualquer tecnologia ou framework novo que não esteja em DOMAIN_KEYWORDS vai para "general". A lista precisa de revisão periódica.
5. **Domain override esquecido**: se submit_request classifica errado, todas as tasks da árvore herdam o erro. O override manual via `create_task(domain="X")` só funciona para tasks individuais, não para árvores inteiras criadas via submit_request.

### Dados mínimos para decidir a próxima rodada

1. **Volume real**: quantas tasks ativas simultaneamente? (valida se _MAX_CANDIDATES=1000 é suficiente)
2. **Distribuição de domains**: `get_metrics()` → agrupar por domain. Se >50% é "general", o domain router precisa de mais keywords antes de investir em retrospective
3. **Match rate**: para agentes com perfil, em quantas claims o task.domain casou com o profile.domains? Se <60%, o valor real dos profiles é baixo
4. **Rework rate antes vs depois**: se agent profiles não reduziu rework, o matching está correto mas o agente não é realmente especialista
5. **Número de perfis registrados**: se após N sessões de uso real só 1 agente tem perfil, o tool register_agent não está sendo usado — revisar UX ou documentação antes de avançar

**Critério objetivo**: se após 3-5 sessões de uso real (a) distribuição de domains for saudável (<40% "general"), (b) match rate for >60%, e (c) rework rate de especialistas for menor que de generalistas — a fundação está sólida para avançar para retrospective automática. Caso contrário, priorizar ajustes em classify_domain ou documentação de profiles.

---

## Próximo Passo Recomendado

**Retrospective Automática** — blueprint primeiro, implementação depois.

Justificativa: com agent profiles e domain-aware matching, o sistema já direciona tasks ao agente certo. O próximo ganho é visibilidade: quando um request é finalizado, gerar automaticamente um resumo de performance (tempo total, rework rate, bottlenecks) usando os dados de task_metrics. Isso preenche a tabela `retrospectives` (criada em v3.0, ainda vazia) e fecha o ciclo de feedback.

Alternativas:
1. **Checklist enforced (opt-in)** — validate_checklist pode bloquear complete_task se review_policy exigir
2. **max_concurrent enforcement** — usar o campo já registrado em agent_profiles para limitar tasks simultâneas
3. **GPT auditor bloqueante** — integrar ask_gpt como gate em record_review

**Pré-condição**: concluir 3-5 sessões reais curtas e confirmar que domain, profiles e fluxo ponta a ponta continuam saudáveis sem intervenção manual estranha.

---

## Handoff para Próxima Sessão

**Projeto**: agent-hub-mcp em `~/agent-hub-mcp`
**O que foi entregue**: v3.0 (memória + playbooks + métricas) + v3.1 (bootstrap fix + domain router) + v3.2 (domain propagação na orquestração) + v3.3 (agent profiles + domain-aware matching).
**Estado**: 186/186 testes passando. 30 tools MCP, 11 tabelas. Agent profiles com tuple ranking em claim_next_task. v3.3 validada em uso real.
**Checkpoint**: `docs/agent-hub-v3-status.md` (este arquivo — canônico, arquivo único)
**Memória persistente**: `~/.claude/projects/-home-rdios/memory/agent-hub.md`
**Repo**: github.com/rdioscaio/agent-hub (branch main)
**Antes de avançar**: operar 3-5 sessões reais curtas conforme seção "Operação Recomendada Antes da Próxima Rodada"
**Próximo**: blueprint de Retrospective Automática, ou ajuste de domain/profiles se os sinais operacionais piorarem.
