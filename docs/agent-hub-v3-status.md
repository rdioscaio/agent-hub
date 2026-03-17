# Agent Hub v3 — Checkpoint Consolidado

Data: 2026-03-13 | Versão: v3.4 + Fase B validada + Auto-routing validado + Checklist Enforced (opt-in) implementado + Retrospective On-Demand implementada + Trilha Front-end/UI validada operacionalmente + Trilha Architecture validada + Policy Claude vs Ask GPT implementada e validada + Trilha Documentation/Planning implementada e validada + CLI para knowledge layer implementada

---

## Estado Atual

**v3.0 + v3.1 + v3.2 + v3.3 + v3.4 concluídas. Fase B validada. Auto-routing implementado e validado em 3 sessões reais. Default de synthesize alinhado com o generalista ativo. Checklist Enforced (opt-in) implementado para work/automation. Retrospective On-Demand (etapa 1) implementada com leitura MCP e persistência imutável por root. Trilha Front-end/UI implementada com playbooks específicos advisory e validada operacionalmente em 2 sessões reais. Trilha Architecture implementada com playbooks específicos advisory e integração explícita com `record_decision`. Policy Claude vs Ask GPT implementada e validada operacionalmente. Trilha Documentation/Planning implementada com playbooks `process` específicos advisory e validada operacionalmente. CLI para knowledge layer implementada no `hub_cli.py` com comandos de consulta, promoção, aprovação, supersede e depreciação. 347/347 testes passando. Zero regressão.**

37 tools MCP, 12 tabelas SQLite, 8 domínios classificados e propagados em todo o fluxo de orquestração.
Bootstrap funcional separado. Domain advisory com matching preferencial via agent profiles.
Métricas passivas. Playbooks advisory. Agent profiles com tuple ranking em claim_next_task.
Conhecimento curado com ciclo de vida explícito (`draft → active → superseded|deprecated`) e consulta default apenas em `active`.

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
| Domain Router | `hub/domain.py` com `classify_domain(title, description)` — função interna, NÃO MCP tool. 8 domínios. Keyword-based, word boundary, title peso 2, desc peso 1. Empate por prioridade fixa. Fallback: "general". |
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

### v3.4 — Knowledge Layer + Domain Drift Fix

| Componente | O que fez |
|------------|-----------|
| P0 — Domain drift | `tools/memory.py` agora normaliza `arch -> architecture`, valida `domain` contra `VALID_DOMAINS` em `store_memory` e `record_decision`, e corrige docstrings desalinhadas. |
| Schema | +tabela `knowledge_entries` com `UNIQUE(slug, version)`, `deprecation_reason` e partial unique index por `slug` para no máximo 1 entry aberta (`draft` ou `active`). |
| Tools MCP | 5 novas: `promote_knowledge`, `approve_knowledge`, `supersede_knowledge`, `deprecate_knowledge`, `query_knowledge`. |
| Semântica | Promoção explícita e manual. Query default retorna só `status='active'`. Busca por `slug` sem status explícito retorna só a versão ativa; se não houver ativa, retorna vazio. |
| Curadoria | `approve_knowledge` registra `same_author_warning` sem bloquear. `supersede_knowledge` cria nova versão ativa e preserva histórico. `deprecate_knowledge` registra `deprecation_reason` no banco. |
| **Testes** | **+38 testes → 224/224 passando** |

#### Impacto prático da v3.3

Antes: `claim_next_task` pegava a task de maior prioridade disponível, ignorando domain. Um agente backend podia receber task frontend porque tinha prioridade maior. Domain era metadata inerte.

Depois: agentes com perfil registrado preferem tasks do seu domain mesmo que existam tasks de maior prioridade em outros domains. Se não houver task no domain do agente, faz fallback para a melhor disponível. Agentes sem perfil mantêm comportamento idêntico ao v3.2. Matching nunca bloqueia — só reordena.

---

## Arquivos Criados (7)

| Arquivo | Rodada | Conteúdo |
|---------|--------|----------|
| `tools/memory.py` | v3.0 E2 | 4 tools MCP (store_memory, recall_memory, record_decision, query_decisions) + helpers |
| `tools/playbooks.py` | v3.0 E3 | 2 tools MCP (get_playbook, validate_checklist) + seed_default_playbooks interno |
| `tools/metrics.py` | v3.0 E4 | 1 tool MCP (get_metrics) + collect_task_metric interno |
| `hub/bootstrap.py` | v3.1 | ensure_ready() — startup funcional idempotente |
| `hub/domain.py` | v3.1 | classify_domain, VALID_DOMAINS, DOMAIN_KEYWORDS |
| `tools/agents.py` | v3.3 | 3 tools MCP (register_agent, get_agent_profile, list_agents) + VALID_TASK_KINDS + helpers |
| `tools/knowledge.py` | v3.4 | 5 tools MCP da camada curada + constantes + helpers internos |

## Arquivos Alterados (6)

| Arquivo | Rodadas | Alterações |
|---------|---------|------------|
| `hub/db.py` | v3.0 E1, v3.1, v3.3, v3.4 | +8 tabelas em _SCHEMA, +2 colunas em _TASK_MIGRATIONS (claimed_at, domain), +12 índices |
| `server.py` | v3.0 E2-E4, v3.1, v3.3, v3.4 | +5 imports/registrations de knowledge tools, __main__ usa ensure_ready(), docstring atualizado |
| `tools/memory.py` | v3.0 E2, v3.4 | +normalização `arch -> architecture`, +validação de domain, docstrings corrigidas |
| `tools/tasks.py` | v3.0 E4, v3.1, v3.3 | +hooks de métricas, +param domain, +_get_active_profile helper, +tuple ranking em claim_next_task, +_MAX_CANDIDATES |
| `tools/orchestration.py` | v3.2, auto-routing | +import classify_domain/VALID_DOMAINS, +domain= em submit_request e record_review, +_find_specialist, +_resolve_agents, +auto-routing em submit_request |
| `tests/smoke_test.py` | v3.0-v3.4, auto-routing | +210 testes acumulados, usa ensure_ready(), imports atualizados, +25 testes auto-routing/synthesizer |

---

## Tools MCP Atuais (37)

| Grupo | Tools |
|-------|-------|
| Tasks (8) | create_task, get_task, claim_task, claim_next_task, heartbeat_task, complete_task, fail_task, list_tasks |
| Notes (2) | append_note, list_notes |
| Artifacts (2) | publish_artifact, read_artifact |
| Locks (2) | acquire_lock, release_lock |
| Delegation (2) | ask_gpt, delegate_task_to_gpt |
| Orchestration (4) | submit_request, record_review, list_task_tree, summarize_request |
| Memory (4) | store_memory, recall_memory, record_decision, query_decisions |
| Knowledge (5) | promote_knowledge, approve_knowledge, supersede_knowledge, deprecate_knowledge, query_knowledge |
| Playbooks (2) | get_playbook, validate_checklist |
| Metrics (1) | get_metrics |
| Agents (3) | register_agent, get_agent_profile, list_agents |
| Retrospectives (2) | generate_retrospective, get_retrospective |

---

## Tabelas SQLite Atuais (12)

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
| retrospectives | v3.0 E1 + etapa on-demand | Retrospectives imutáveis por `root_task_id` |
| agent_profiles | v3.3 | Perfis de capacidade por agente |
| knowledge_entries | v3.4 | Conhecimento curado, versionado e consultável |

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
13. **Promoção de conhecimento é explícita** — nenhum fluxo promove memória/decisão automaticamente
14. **Consulta curada default = active only** — histórico só aparece com `status` explícito
15. **slug é chave lógica; version identifica revisão** — no máximo 1 entry aberta (`draft` ou `active`) por slug

---

## Pendências e Riscos Remanescentes

| Item | Tipo | Detalhe |
|------|------|---------|
| Retrospective automática (gatilho) ainda adiada | By design | A etapa atual é on-demand via MCP; geração automática continua futura |
| Tag filtering em Python | Performance | OK para <1000 entries. Se escalar, considerar FTS5 |
| Sem deduplicação automática de memória | By design | Agente deve chamar recall_memory antes de store_memory |
| classify_domain é keyword-based | Limitação | Pode classificar errado em edge cases. Advisory only, override manual disponível |
| delegate_task_to_gpt não propaga domain | Observação | Task já tem domain quando delegada. Sem impacto |
| _MAX_CANDIDATES = 1000 | Safety cap | Limita candidatas na query quando perfil ativo. Se >1000 tasks ativas, otimizar com índice composto |
| max_concurrent não enforced | By design | Registrado no perfil, NÃO usado no matching desta rodada |
| list_agents(domain) exclui generalistas | By design | Generalistas (domains=[]) só aparecem na listagem sem filtro |
| Matching depende de perfil registrado | Operacional | Se agente não chamar register_agent antes de claim_next_task, matching não se aplica |
| classify_domain + agent profile desalinhados | Risco operacional | Se keywords de domain classificam "backend" mas agente registra domains=["api"], não vai casar. Domínios devem ser os 8 de `VALID_DOMAINS` |
| Tag filtering em knowledge também é Python-side | Performance | OK para baixo volume. Se escalar, considerar FTS5 depois |
| CLI de knowledge ainda sem operações batch/listagem avançada | Ergonomia | O `hub_cli.py` agora cobre o lifecycle principal (query/promote/approve/supersede/deprecate). Busca mais rica ou operações em lote podem vir depois se realmente fizerem falta |
| ~~Validação operacional da policy Claude vs Ask GPT~~ | Resolvido | Validada em 2026-03-13 com `OPENAI_API_KEY` configurada, 2 sessões reais, 6/6 PASS |

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
2. **Distribuição de domains**: `get_metrics()` → agrupar por domain. Se >50% é "general", o domain router precisa de mais keywords antes de investir na próxima especialização
3. **Match rate**: para agentes com perfil, em quantas claims o task.domain casou com o profile.domains? Se <60%, o valor real dos profiles é baixo
4. **Rework rate antes vs depois**: se agent profiles não reduziu rework, o matching está correto mas o agente não é realmente especialista
5. **Número de perfis registrados**: se após N sessões de uso real só 1 agente tem perfil, o tool register_agent não está sendo usado — revisar UX ou documentação antes de avançar

**Critério objetivo**: se após 3-5 sessões de uso real (a) distribuição de domains for saudável (<40% "general"), (b) match rate for >60%, (c) rework rate de especialistas for menor que de generalistas, e (d) a knowledge layer estiver sendo consultada/promovida sem gerar lixo elegante — a fundação está sólida para avançar para a próxima especialização. Caso contrário, priorizar ajustes em classify_domain, profiles ou curadoria.

---

## Validação Real — Rodada 2 (5 Sessões) — 2026-03-11

Objetivo: rodar 5 sessões reais curtas cobrindo backend, frontend, infra, architecture e um request ambíguo. Validar domain router, agent profiles, matching, fallback, knowledge layer e métricas.

### Sessões Executadas

| # | Request | Domain esperado | Domain real | Agente worker | Match? | Fallback? | Knowledge consultada? | Métricas geradas? | Synthesize fechou? |
|---|---------|----------------|-------------|---------------|--------|-----------|----------------------|-------------------|--------------------|
| 1 | Rate limiting middleware para API auth | backend | backend | claude-backend | Sim | Não | Sim (vazia) | Sim (3 tasks) | Sim |
| 2 | Toast notification system com React | frontend | frontend | claude-frontend | Sim | Não | Sim (vazia) | Sim (4 tasks) | Sim |
| 3 | GitHub Actions CI/CD pipeline staging | infra | infra | claude-infra | Sim | Não | Sim (vazia) | Sim (4 tasks) | Sim |
| 4 | Event-driven microservices communication | architecture | architecture | codex-general | N/A (generalista) | Sim | Sim (vazia) | Sim (3 tasks) | Sim |
| 5 | Improve dashboard loading performance | frontend | **general** | claude-frontend | Não (cross-domain) | Sim | Sim (vazia) | Sim (3 tasks) | Sim |

### Evidências Consolidadas

| Critério | Resultado | Status |
|----------|-----------|--------|
| Domain classification accuracy | 4/5 corretos (80%) | Aprovado |
| Caso "general" indevido | Sessão 5 — "dashboard performance" sem keywords fortes | Esperado |
| Agent profile matching | 3/5 match perfeito (backend, frontend, infra) | Aprovado |
| Requested-agent generalista | Sessão 4 — tasks architecture já vieram com `requested_agent="codex-general"` e foram executadas sem bloqueio | Aprovado |
| Requested-agent cross-domain | Sessão 5 — tasks general já vieram com `requested_agent="claude-frontend"` e foram executadas sem bloqueio | Aprovado |
| Rework rate | 0% em todas as sessões | Aprovado |
| Fallback rate (metric) | 0% | Aprovado |
| Completion rate | 100% | Aprovado |
| Knowledge query antes de work | 5/5 sessões consultaram query_knowledge | Aprovado |
| Knowledge promotion | 1 entry promovida (react-toast-notification-pattern) | Aprovado |
| Knowledge approval cross-agent | Promovida por claude-frontend, aprovada por claude-backend | Aprovado |
| Knowledge query retorna active | query_knowledge(domain="frontend") → 1 entry active | Aprovado |
| Memória operacional | 3 entries gravadas (backend, frontend, general) | Aprovado |
| Decisões registradas | 2 decisions (infra: CI tool, architecture: event-driven) | Aprovado |
| recall_memory consultado | Sim, em sessões relevantes | Aprovado |
| query_decisions consultado | Sim, em sessões com decisão arquitetural | Aprovado |
| audit_log (`result_status='error'`) | 0 | Aprovado |

### Distribuição de Domains (esta rodada — 17 task_metrics novas)

| Domain | Tasks | % |
|--------|-------|---|
| backend | 3 | 17.6% |
| frontend | 4 | 23.5% |
| infra | 4 | 23.5% |
| architecture | 3 | 17.6% |
| general | 3 | 17.6% |

**general = 17.6%** — abaixo do limiar de 40%. Distribuição saudável.

### Agentes Ativos ao Final

| Agente | Domains | Tasks completadas (rodada) |
|--------|---------|----------------------------|
| claude-backend | ["backend"] | 2 work |
| claude-frontend | ["frontend"] | 5 work (3 frontend + 2 general) |
| claude-infra | ["infra"] | 3 work |
| codex-general | [] (generalista) | 2 work + 5 synthesize |
| legacy-inactive | ["backend"] (inactive) | 0 |

### Edge Cases Observados

1. **"Improve dashboard performance" → general**: keywords "dashboard", "loading", "performance" não casam com nenhum domain. Classificação correta pelo router (nenhum domain tem pontuação), mas semanticamente deveria ser "frontend". **Ação sugerida**: adicionar "dashboard" às keywords de frontend.
2. **Architecture sem especialista**: nenhum agente tem domains=["architecture"]. Nesta rodada, as tasks vieram explicitamente com `requested_agent="codex-general"`, então isso prova execução segura por generalista, não fallback puro de ranking. **Não é urgente** registrar um especialista architecture se o volume for baixo.
3. **Knowledge layer ainda com pouco conteúdo**: apenas 1 entry promovida em 5 sessões. É esperado — promoção só faz sentido para padrões genuínos reutilizáveis.
4. **same_author_warning não disparou**: promoção e aprovação por agentes diferentes. Fluxo correto.

### Decisão Operacional

**A fundação está sólida para avançar.** Critérios objetivos atendidos:
- (a) Distribuição de domains saudável: general=17.6% (< 40%) ✓
- (b) Match rate para agentes com perfil: 100% para tasks no domain do agente ✓
- (c) Rework rate: 0% ✓
- (d) Knowledge layer consultada e promovida sem gerar lixo ✓

**Ajuste aplicado**: `"dashboard"` isolado substituído por `"dashboard loading"` + `"dashboard load"` em `hub/domain.py`. Testes positivos e negativos adicionados (229/229 passando). `"Deploy dashboard to staging"` → infra, `"Update the dashboard"` → general, `"Improve dashboard loading performance"` → frontend.

---

## Fase B — Especialista de Automação (início) — 2026-03-12

Objetivo desta etapa: iniciar a trilha de automação com o menor corte útil e sem mudar schema ou adicionar tools MCP novas.

### Entregue

1. **Novo domain `automation` no router**
   - `hub/domain.py` agora reconhece requests de automação com keywords específicas:
     - `n8n`, `cron`, `trigger`, `webhook`, `scheduler`, `zapier`, `automate`, `automation`, `automated flow`, `integration flow`
   - `automation` entra na prioridade entre `infra` e `architecture`
   - Colisões evitadas:
     - `workflow` **não** foi adicionado em `automation` para não roubar casos de `process`
     - `make` **não** foi adicionado para evitar falso positivo broad

2. **Playbooks seeded para automação**
   - `get_playbook("work", "automation")` agora retorna trilha específica de automação
   - `get_playbook("review", "automation")` agora retorna trilha específica de revisão
   - Convenção de artifacts fixada no playbook:
     - work: `flow-definition-{task_id}`, `credentials-{task_id}`, `staging-evidence-{task_id}`, `rollback-plan-{task_id}`
     - review: leitura via `source_task_id` (`flow-definition-{source_task_id}`, etc.)

3. **Playbook genérico de `synthesize` adicionado**
   - Fecha uma lacuna anterior do hub: `task_kind="synthesize"` já era válido, mas não havia playbook genérico correspondente
   - Isso também viabiliza o fallback de `get_playbook("synthesize", "automation") -> domain='*'`

4. **Cobertura de testes ampliada**
   - Casos positivos:
     - `Create n8n workflow for notifications` -> `automation`
     - `Set up cron job for data sync` -> `automation`
     - `Add webhook integration for Slack` -> `automation`
   - Casos negativos:
     - `Automate deploy pipeline` -> `infra`
     - `Create workflow checklist` -> `process`
     - `Review sprint workflow` -> `process`
   - Playbooks:
     - automation work/review
     - fallback de synthesize para playbook genérico

### Estado

- **Schema**: inalterado
- **Tools MCP**: 35 (inalterado)
- **Tabelas SQLite**: 12 (inalterado)
- **Playbooks ativos**: 6
- **Testes**: **241/241 passando**

### Observacoes de design

1. **Acionamento do especialista agora suporta auto-routing**
   - `submit_request(... worker_agent="auto")` resolve para o especialista do domain classificado
   - Compatível com uso explícito: `submit_request(... worker_agent="claude-automation")` continua funcionando

2. **Lifecycle de automacao nao usa mutation de metadata nesta fase**
   - O hub ainda nao tem tool MCP para atualizar `task.metadata` apos criacao
   - Nesta fase, a trilha operacional fica documentada via playbooks + artifacts + notes

---

## Validação Real — Fase B (3 Sessões de Automação) — 2026-03-12

Objetivo: validar o domain `automation`, playbooks work/review/automation, agent profile `claude-automation`, artifact naming convention determinístico e knowledge layer para o novo domínio.

### Pré-requisitos executados

- MCP server reiniciado para carregar `VALID_DOMAINS` com `automation`
- `claude-automation` registrado com `domains=["automation"]`, `task_kinds=["work", "review"]`

### Sessões Executadas

| # | Request | Keyword trigger | Domain real | Agente worker | Playbook consultado | Artifacts determinísticos | Review aprovada | Synthesize fechou |
|---|---------|----------------|-------------|---------------|--------------------|--------------------------|-----------------|--------------------|
| 1 | Fluxo n8n webhook lead → Slack | n8n, webhook | automation ✅ | claude-automation | work/automation ✅ + review/automation ✅ | 5 publicados (4 obrigatórios + 1 flow extra) | Sim | Sim |
| 2 | Cron job limpeza de logs > 30d | cron | automation ✅ | claude-automation | work/automation ✅ + review/automation ✅ | 5 publicados (4 obrigatórios + 1 flow extra) | Sim | Sim |
| 3 | Webhook Stripe pagamento → order status | webhook | automation ✅ | claude-automation | work/automation ✅ + review/automation ✅ | 5 publicados (4 obrigatórios + 1 flow extra) | Sim | Sim |

### Métricas da Fase B (9 task_metrics geradas)

| Métrica | Valor |
|---------|-------|
| Tasks totais | 9 (6 work + 3 synthesize) |
| Domain | 100% automation |
| Completion rate | 100% |
| Rework rate | 0% |
| Fallback rate | 0% |
| Avg total duration | ~221s |

### Knowledge Layer — Fase B

| Ação | Detalhe |
|------|---------|
| query_knowledge(domain=automation) | Consultada 3/3 sessões (0 entries no início, esperado) |
| recall_memory(domain=automation) | Consultada em sessão 2 (0 entries) |
| store_memory | 1 entry gravada (artifact naming convention, confidence=0.95) |
| promote_knowledge | 1 entry promovida: slug `automation-artifact-naming-convention` |
| approve_knowledge | Aprovada cross-agent (promoted by claude-automation, reviewed by claude-opus) |
| record_decision | 1 decisão registrada: Fase B validada para uso operacional |

### Nuance operacional encontrada

- **`requested_agent` no synthesize**: `submit_request` propaga `synthesizer_agent` como `requested_agent`, e o match em `claim_next_task` é exato. O follow-up alinhou o default para `codex-general`, removendo o papercut legado de `codex`.

### Critérios de validação

| Critério | Meta | Resultado | Status |
|----------|------|-----------|--------|
| Domain accuracy | >= 80% | 100% (3/3) | ✅ Aprovado |
| Playbook consultado | 3/3 | 3/3 work + 3/3 review | ✅ Aprovado |
| Artifacts determinísticos | Usados em 3/3 | 15 artifacts prefixados publicados; pacote mínimo obrigatório presente nas 3/3 sessões | ✅ Aprovado |
| Rework rate | 0% | 0% | ✅ Aprovado |
| Knowledge layer usada | Sim | Consultada 3/3, 1 convention promovida e aprovada | ✅ Aprovado |
| Cross-agent approval | Sim | claude-automation → claude-opus | ✅ Aprovado |

### Decisão: Fase B Validada ✅

O domain `automation` agrega valor mensurável:
1. **Classificação precisa**: 3 keywords diferentes (n8n, cron, webhook) todas classificaram corretamente
2. **Playbooks específicos**: guiam o agente a produzir artifacts padronizados (flow, credentials, staging, rollback)
3. **Knowledge layer**: padrão de artifact naming emergiu e foi curado
4. **Agent profile funcional**: `claude-automation` claimou todas as tasks do seu domain sem fallback

---

## Estado Atual dos Agentes (6 registrados)

| Agente | Domains | Task kinds | Status |
|--------|---------|------------|--------|
| claude-backend | ["backend"] | ["work", "review"] | Ativo |
| claude-frontend | ["frontend"] | ["work", "review"] | Ativo |
| claude-infra | ["infra"] | ["work", "review"] | Ativo |
| claude-automation | ["automation"] | ["work", "review"] | Ativo |
| codex-general | [] (generalista) | [] (todos) | Ativo |
| legacy-inactive-c2-20260310 | ["backend"] | — | Inativo |

---

## Auto-routing de Agente — Implementação — 2026-03-12

### O que foi implementado

Auto-routing permite que `submit_request` resolva automaticamente `worker_agent` e `reviewer_agent` com base no domain classificado e nos agent profiles registrados, usando o sentinel `"auto"`.

#### Código adicionado em `tools/orchestration.py`

| Componente | Função |
|------------|--------|
| `AUTO_ROUTING_SENTINEL = "auto"` | Constante que ativa resolução |
| `_find_specialist(conn, domain, required_kind)` | Busca agente ativo com domain match + task_kind match (ou task_kinds vazio = aceita tudo). Tiebreak alfabético. Retorna None se domain="general" ou sem candidato. |
| `_resolve_agents(conn, domain, worker, reviewer, synthesizer, fallback)` | Aplica regras de decisão por role: `"auto"` → resolve, `""` → default legado, explícito → preserva. Retorna agents resolvidos + routing metadata. |
| `submit_request` modificado | Detecta sentinel em qualquer agent param → chama `_resolve_agents` → substitui valores → grava `routing` no root metadata → appenda note `hub-router` |

#### Regras de decisão

| Input | Comportamento |
|-------|---------------|
| `"auto"` | Resolve via `_find_specialist(domain, kind)` → specialist encontrado OU default legado |
| `""` | Volta ao default legado do role (`codex`, `claude`, etc.) |
| valor explícito | Preservado sem alteração |
| nenhum `"auto"` presente | Sem routing metadata, comportamento idêntico ao legado |

#### Observabilidade

- **`root.metadata.routing`**: fonte primária — contém input, resolved, method para cada role
- **Note `hub-router`**: complementar — texto legível no root task
- **`audit_log`**: NÃO captura agents resolvidos (registra args na entrada, antes da resolução)

#### Testes

- **+25 testes → 266/266 passando** (5 testes de `""` fallback + 19 testes de auto-routing + 1 teste do default de synthesize)
- Cobertura: specialist resolve, default_fallback, explicit preserve, no routing without sentinel, generalist (empty task_kinds), routing detail metadata

#### Regras arquiteturais preservadas

- Defaults preservados para worker/reviewer/fallback: `worker_agent="codex"`, `reviewer_agent="claude"`, `fallback_agent="gpt-fallback"`
- Follow-up aplicado: `synthesizer_agent` default agora é `codex-general` para alinhar com o generalista ativo
- Sentinel é opt-in: callers existentes não são afetados
- Funções internas (`_find_specialist`, `_resolve_agents`) NÃO são MCP tools

---

## Validação Real — Auto-routing (3 Sessões) — 2026-03-12

Objetivo: validar que `worker_agent="auto"` e `reviewer_agent="auto"` resolvem corretamente por domain + task_kinds, sem regressão de fluxo.

### Incidente operacional

Na primeira tentativa, o MCP server (PID 1845230, iniciado às 17:28) estava rodando código anterior à implementação do auto-routing (arquivo modificado às 18:28). Resultado: `submit_request` gravou `worker_agent="auto"` literal no metadata sem resolver. Solução: kill dos processos antigos (PIDs 1824094, 1845230), restart automático do server com código atualizado.

Root task afetado: `23931a29` (dados inválidos, ignorado na validação).

### Sessões Executadas (após restart)

| # | Request | Domain real | Worker input | Worker resolved | Method | Reviewer input | Reviewer resolved | Method | Synth | hub-router note |
|---|---------|------------|--------------|-----------------|--------|----------------|-------------------|--------|-------|-----------------|
| 1 | Add rate limiting middleware to REST API auth endpoints | backend | auto | claude-backend | specialist | auto | claude-backend | specialist | codex-general | Sim |
| 2 | Create automation scripts for the deployment workflow | automation | auto | claude-automation | specialist | auto | claude-automation | specialist | codex-general | Sim |
| 3 | Organize project files and improve documentation structure | architecture | auto | codex | default_fallback | auto | claude | default_fallback | codex-general | Sim |

### Claim match — owner vs requested_agent

| # | Work requested_agent | Work owner (claim) | Match | Review requested_agent | Review owner (claim) | Match | Synth requested_agent | Synth owner (claim) | Match |
|---|---------------------|-------------------|-------|----------------------|---------------------|-------|-----------------------|--------------------|-------|
| 1 | claude-backend | claude-backend | ✅ | claude-backend | claude-backend | ✅ | codex-general | codex-general | ✅ |
| 2 | claude-automation | claude-automation | ✅ | claude-automation | claude-automation | ✅ | codex-general | codex-general | ✅ |
| 3 | codex | codex | ✅ | claude | claude | ✅ | codex-general | codex-general | ✅ |

### Routing metadata verificada

Sessão 1 (root `e51a1077`):
```json
{"routing": {
  "worker_agent": {"input": "auto", "resolved": "claude-backend", "method": "specialist"},
  "reviewer_agent": {"input": "auto", "resolved": "claude-backend", "method": "specialist"},
  "synthesizer_agent": {"input": "codex-general", "resolved": "codex-general", "method": "explicit"},
  "fallback_agent": {"input": "gpt-fallback", "resolved": "gpt-fallback", "method": "explicit"}
}, "domain": "backend"}
```

Sessão 3 (root `3be07a4c`) — sem especialista para architecture:
```json
{"routing": {
  "worker_agent": {"input": "auto", "resolved": "codex", "method": "default_fallback"},
  "reviewer_agent": {"input": "auto", "resolved": "claude", "method": "default_fallback"},
  "synthesizer_agent": {"input": "codex-general", "resolved": "codex-general", "method": "explicit"},
  "fallback_agent": {"input": "gpt-fallback", "resolved": "gpt-fallback", "method": "explicit"}
}, "domain": "architecture"}
```

### Critérios de validação

| Critério | Meta | Resultado | Status |
|----------|------|-----------|--------|
| Specialist resolve correto | 2 sessões com specialist | backend → claude-backend, automation → claude-automation | ✅ |
| Default fallback correto | 1 sessão sem specialist | architecture → codex (worker), claude (reviewer) | ✅ |
| Routing metadata no root | 3/3 | 3/3 com chave `routing` completa | ✅ |
| hub-router note presente | 3/3 | 3/3 com author=hub-router | ✅ |
| Claim match (owner = requested_agent) | 9/9 tasks | 9/9 match exato | ✅ |
| Lifecycle completo (submit→claim→complete→review→synthesize) | 3/3 | 3/3 fechados | ✅ |
| Sem fallback ou rework | 3/3 | 0% rework, 0% fallback | ✅ |
| synthesizer_agent explícito funciona | 3/3 | codex-general claimou 3/3 synthesize tasks | ✅ |
| Sem regressão em testes | 266/266 | 266/266 passando | ✅ |

### Veredito: Auto-routing validado ✅

- Resolução por specialist funciona para domains com agente registrado (backend, automation)
- Fallback para defaults funciona para domains sem agente registrado (architecture)
- Routing metadata e notes fornecem rastreabilidade completa
- Lifecycle end-to-end sem regressão
- 0 bugs encontrados (exceto o incidente de server stale, que é operacional e não de código)

---

## Checklist Enforced (opt-in) — Implementado

Objetivo: transformar a checklist de `work/automation` de advisory em gate opt-in e reversível, sem espalhar enforcement para os demais domains.

### O que entrou

- Coluna `playbooks.enforcement` adicionada ao schema com default `advisory`
- Migration one-shot: na criação da coluna, `work/automation` é promovido para `enforcement='required'`
- Seed atualizado: bancos novos já nascem com `work/automation=required`
- Gate em `complete_task`: para playbooks com `enforcement='required'`, a task só vai para `done` se a validação de checklist mais recente tiver `score >= 1.0`
- Observabilidade por note explícita `[CHECKLIST GATE] blocked ...` na própria task quando o gate bloqueia

### Regra final do gate

- Escopo inicial: apenas `work/automation`
- Fonte de verdade: playbook ativo mais específico por `(task_kind, domain)`, com fallback para `*`
- Sem playbook ou `enforcement='advisory'` → sem bloqueio
- `enforcement='required'` → exige note `[CHECKLIST ADVISORY] ...` válida na task
- A validação mais recente prevalece
- `score < 1.0` bloqueia
- `score = 1.0` libera

### Testes adicionados

- Coluna `enforcement` existe
- `work/automation` nasce com `required`
- `ensure_ready()` subsequente não reimpõe `required`
- rollback manual para `advisory` persiste após novo bootstrap
- `get_playbook()` expõe `enforcement`
- gate bloqueia checklist ausente
- gate bloqueia score `< 1.0`
- gate passa com score `1.0`
- validação mais recente vence sobre score histórico melhor
- advisory em backend continua sem gate
- task sem playbook compatível continua sem gate

### Estado validado

- `296/296` testes passando
- migration aplicada também no banco real `db/hub.sqlite`
- `playbooks.enforcement` presente no banco real
- `work/automation` confirmado como `required`

---

## Validação Real — Checklist Enforced (2 Sessões) — 2026-03-12

Objetivo: validar operacionalmente o gate de checklist enforced em `work/automation` com 2 sessões reais — uma forçando bloqueios e outra como happy path limpo.

### Sessões Executadas

| # | Request | Domain | Worker | Reviewer | Bloqueios do gate | Score final | complete_task passou? | Árvore fechada? |
|---|---------|--------|--------|----------|-------------------|-------------|----------------------|-----------------|
| 1 | Create automation scripts for deployment workflow | automation | claude-automation | claude-automation | **2** (sem checklist + score 0.75) | 1.0 | Sim (3a tentativa) | Sim (4/4 done) |
| 2 | Create webhook automation flow for payment status notifications | automation | claude-automation | claude-automation | **0** | 1.0 | Sim (1a tentativa) | Sim (4/4 done) |

### Root task IDs

| Sessão | Root task ID |
|--------|-------------|
| 1 (Gate blocking) | `ddfe1c5e-3116-441e-ad94-2a48e739bf7b` |
| 2 (Happy path) | `d07e7b20-8478-4e76-8b35-1cd3f6c18340` |

### Comportamento do gate observado

| Cenário | Resultado | Sessão |
|---------|-----------|--------|
| complete_task sem checklist | BLOQUEADO — "no checklist validation found" | 1 |
| complete_task com score 0.75 | BLOQUEADO — "checklist score 0.75 < 1.0", failed_items reportados | 1 |
| complete_task com score 1.0 | PASSOU — status=done | 1, 2 |
| Note `[CHECKLIST GATE] blocked` appendada | Sim, com reason + task_kind + domain + failed_items | 1 |
| Happy path sem fricção | Sim — score 1.0 na 1a tentativa passa direto | 2 |

### Observabilidade

- Gate notes incluem: reason, task_kind, domain, failed_items (quando aplicável)
- Checklist advisory notes incluem: score, total, passed, failed_items, responses detalhadas
- Auto-routing resolveu corretamente para `claude-automation` (method=specialist) em ambas as sessões

### Veredito: Checklist Enforced validado operacionalmente ✅

- Gate bloqueia corretamente nos 3 cenários testados (sem checklist, score insuficiente, score perfeito)
- Notes de observabilidade fornecem rastreabilidade completa
- Happy path funciona sem fricção
- Lifecycle end-to-end sem regressão
- **0 bugs encontrados**

---

## Retrospective On-Demand (etapa 1) — 2026-03-13

Objetivo: institucionalizar retrospectiva curta, rastreável e por `root_task_id`, sem LLM e sem duplicar memory/decisions/knowledge.

### Entrega implementada

- `generate_retrospective(root_task_id, generated_by)` — gera e persiste retrospectiva imutável
- `get_retrospective(root_task_id)` — leitura MCP da retrospectiva persistida
- `1 retro por root` enforced por `UNIQUE INDEX` em `retrospectives(root_task_id)`
- heurística determinística sem parsing de planner/artifacts
- `review_rounds` derivado de `COUNT task_kind='review'`
- root `request` pode permanecer aberta; apenas tasks operacionais precisam estar em estado final

### Regras do MVP

- retrospectiva é **on-demand**, não automática
- retrospectiva é **imutável** após criação
- 2a chamada de `generate_retrospective` retorna a existente com `already_exists=true`
- tasks follow-up criadas depois não recalculam a retro existente
- etapa automática (gatilho no fechamento do root) continua adiada

### Cobertura automatizada

- `generate_retrospective` gera para árvore operacional fechada mesmo com root `request` pendente
- bloqueia `root-only request` sem tasks operacionais
- bloqueia quando há tasks operacionais não finalizadas
- `get_retrospective` lê a retro persistida
- idempotência e imutabilidade por `root_task_id`
- `UNIQUE INDEX idx_retrospectives_root`

### Estado validado

- `308/308` testes passando
- `37` tools MCP ativas
- `2` retrospectives já persistidas no banco real `db/hub.sqlite`
- geração/leitura confirmadas em roots reais:
  - `ddfe1c5e-3116-441e-ad94-2a48e739bf7b`
  - `d07e7b20-8478-4e76-8b35-1cd3f6c18340`

---

## Especialista Front-end / UI — 2026-03-13

Objetivo: fechar a última trilha especialista pendente do projeto original sem adicionar tools novas nem schema novo.

### Entrega implementada

- `work/frontend` seedado com `enforcement='advisory'`
- `review/frontend` seedado com `enforcement='advisory'`
- convenção de evidência textual: `ui-evidence-{task_id}`
- `magic-mcp` tratado apenas como scaffold opcional no playbook, nunca como entregável final
- keywords novas em `frontend`: `animation`, `accessibility`
- `theme` adiado do MVP para evitar reclassificação indevida
- guideline curada ativa: `magic-mcp-usage-guideline` (`frontend`, `guideline`)

### Regras do MVP

- sem gate bloqueante para frontend; checklist permanece advisory
- reviewer lê `ui-evidence-{source_task_id}` e verifica plausibilidade no código
- nenhuma integração programática com `magic-mcp`
- conhecimento sobre `magic-mcp` não é promovido automaticamente; depende de validação real

### Cobertura automatizada

- seed cria `work/frontend` e `review/frontend`
- `get_playbook("work", "frontend")` usa match exato e não fallback genérico
- `get_playbook("review", "frontend")` usa a convenção `ui-evidence-{source_task_id}`
- auto-routing de frontend testado com isolamento de agentes no próprio bloco
- `work/frontend` advisory não bloqueia `complete_task`
- `animation` e `accessibility` classificam como `frontend`
- `"Theme configuration in env"` permanece `infra`

### Estado validado

- `308/308` testes passando
- `37` tools MCP ativas
- `8` playbooks ativos
- `18` keywords em `frontend`
- nenhuma mudança de schema

---

## Validação Operacional — Trilha Front-end/UI (2026-03-13)

2 sessões reais validando o fluxo end-to-end com playbooks frontend, auto-routing, artifact `ui-evidence`, checklist advisory e retrospective.

### Sessão 1 — Componente visual (sidebar responsivo)

| Campo | Valor |
|-------|-------|
| Request | "Build a responsive sidebar component with accessibility improvements" |
| root_task_id | `c2c39e69-0427-4182-9d8e-7580fa0591bc` |
| domain classificado | `frontend` |
| worker resolved | `claude-frontend` (specialist) |
| reviewer resolved | `claude-frontend` (specialist) |
| hub-router note | Presente |
| work requested_agent | `claude-frontend` |
| review requested_agent | `claude-frontend` |
| synthesize requested_agent | `codex-general` |
| work owner (claim) | `claude-frontend` |
| artifact de código | `sidebar-component-bc3f0713` |
| artifact ui-evidence | `ui-evidence-bc3f0713` |
| checklist score | 1.0 (4/4 advisory) |
| complete_task | Passou de primeira |
| review verdict | approve |
| quality_status | approved |
| retrospective | Gerada: outcome=`all_done`, 0 gate_blocks, 1 review_round |

### Sessão 2 — Frontend menos visual (animação em modal)

| Campo | Valor |
|-------|-------|
| Request | "Add animation to the React modal close interaction" |
| root_task_id | `e1a3ea91-3416-4858-861a-952d94534e15` |
| domain classificado | `frontend` (keyword: animation) |
| worker resolved | `claude-frontend` (specialist) |
| reviewer resolved | `claude-frontend` (specialist) |
| hub-router note | Presente |
| work requested_agent | `claude-frontend` |
| review requested_agent | `claude-frontend` |
| synthesize requested_agent | `codex-general` |
| work owner (claim) | `claude-frontend` |
| artifact de código | `modal-animation-4d44f0fe` |
| artifact ui-evidence | `ui-evidence-4d44f0fe` |
| checklist score | 1.0 (4/4 advisory) |
| complete_task | Passou de primeira |
| review verdict | approve |
| quality_status | approved |
| retrospective | Gerada: outcome=`all_done`, 0 gate_blocks, 1 review_round |

### Verificações cruzadas

- `get_playbook("work", "frontend")` retorna playbook domain-specific (não genérico `*`) ✓
- `get_playbook("review", "frontend")` retorna playbook domain-specific (não genérico `*`) ✓
- Ambos playbooks enforcement=`advisory` ✓
- `complete_task` não bloqueou em nenhuma sessão (advisory) ✓
- Keyword `animation` classificou corretamente como `frontend` ✓
- Artifact `ui-evidence-{task_id}` publicado e preenchido em ambas sessões ✓
- Retrospective On-Demand gerada com domain=`frontend` em ambas ✓
- Root request permanece `pending` (regra de request) ✓

### Veredito

**Trilha Front-end/UI VALIDADA.** 0 bugs encontrados. Todos os componentes funcionam end-to-end: domain classification, auto-routing, playbooks específicos, artifact textual de evidência, checklist advisory, retrospective on-demand.

---

## Especialista Architecture — 2026-03-13

Objetivo: transformar o domain `architecture` em trilha especialista real sem criar ADR pesado, sem enforcement novo e sem schema adicional.

### Entrega implementada

- `work/architecture` seedado com `enforcement='advisory'`
- `review/architecture` seedado com `enforcement='advisory'`
- convenção de artifact textual: `arch-decision-{task_id}`
- playbook exige `record_decision(domain='architecture', source_task_id=<task_id>, root_task_id=<root_task_id>)`
- keywords novas em `architecture`: `boundary`, `tradeoff`
- nenhum tool MCP novo, nenhuma tabela nova, nenhum gate bloqueante

### Cobertura automatizada

- seed cria `work/architecture` e `review/architecture`
- `get_playbook("work", "architecture")` usa match exato e não fallback genérico
- `get_playbook("review", "architecture")` usa a convenção `arch-decision-{source_task_id}`
- playbook work explicita o vínculo com `source_task_id` e `root_task_id`
- auto-routing de architecture testado com agente dedicado isolado
- `work/architecture` advisory não bloqueia `complete_task`
- `boundary` e `tradeoff` classificam como `architecture`
- `Refactor auth middleware to reduce coupling` permanece `backend`

### Estado validado

- `328/328` testes passando no estado consolidado atual
- `37` tools MCP ativas
- `10` playbooks ativos
- `12` keywords em `architecture`
- nenhuma mudança de schema
- playbooks `architecture` seedados no banco real via `ensure_ready()`
- `claude-architecture` registrado em runtime com `domains=["architecture"]` e `task_kinds=["work","review"]`

### Próxima validação necessária

- ~~rodar 1 sessão real com `worker_agent="auto"` e `reviewer_agent="auto"`~~
- ~~confirmar artifact `arch-decision-{task_id}`, `record_decision` vinculado e retrospective com `domain="architecture"`~~

### Validação operacional — 1 sessão real (2026-03-13)

| Campo | Valor |
|-------|-------|
| Request | "Evaluate tradeoff between monolith and microservices for module separation" |
| root_task_id | `7298c498-1a5a-47a6-b1e6-23f8bc098a22` |
| domain classificado | `architecture` |
| worker resolved | `claude-architecture` (specialist) |
| reviewer resolved | `claude-architecture` (specialist) |
| hub-router note | Presente |
| work requested_agent | `claude-architecture` |
| review requested_agent | `claude-architecture` |
| synthesize requested_agent | `codex-general` |
| work owner (claim) | `claude-architecture` |
| artifact | `arch-decision-ccf52ee1` |
| decision registrada | `record_decision(domain='architecture')` com `source_task_id=ccf52ee1-6bca-409b-a7b3-61907941c1fe` e `root_task_id=7298c498-1a5a-47a6-b1e6-23f8bc098a22` |
| checklist score | 1.0 (4/4 advisory) |
| complete_task | Passou de primeira |
| review verdict | approve |
| quality_status | approved |
| retrospective | Gerada: outcome=`all_done`, 0 gate_blocks, 1 review_round |

### Veredito

**Trilha Architecture VALIDADA.** 0 bugs encontrados. Domain classification, auto-routing, playbooks específicos, artifact `arch-decision`, `record_decision` vinculado à árvore, checklist advisory e retrospective funcionaram end-to-end.

---

## Policy Claude vs Ask GPT — 2026-03-13

Objetivo: tornar explícito, rastreável e reversível quando Claude trabalha sozinho, quando GPT entra como segunda camada e em que papel esse uso deve ser registrado.

### Entrega implementada

- knowledge entry ativa: `policy-claude-vs-gpt` (`domain="general"`, `kind="guideline"`)
- upgrade versionado de 3 playbooks no banco real:
  - `review/*` v2
  - `work/architecture` v2
  - `review/architecture` v2
- convenção canônica de note manual:
  - `[GPT-CONSULT] role=<counterpoint|auditor> | purpose=<motivo> | result=<agreed|diverged|partial> | action=<adopted|discarded|noted>`
- nenhuma mudança de schema
- nenhuma tool MCP nova
- nenhum gate novo

### Cobertura automatizada

- `review/*` agora documenta a convenção `[GPT-CONSULT]`
- `work/architecture` documenta uso opcional de `ask_gpt` como `counterpoint`
- `review/architecture` documenta uso opcional de `ask_gpt` para tradeoffs não-triviais
- helper `upgrade_default_playbooks()` migra playbooks legados no banco existente
- migration é idempotente: segunda execução não reimpõe nova versão
- invariantes antigas de `arch-decision-{task_id}` e `source_task_id=<task_id>` foram preservadas

### Estado validado

- `328/328` testes passando
- knowledge entry `policy-claude-vs-gpt` ativa e consultável
- banco real atualizado com:
  - `review/*` v2 ativa
  - `work/architecture` v2 ativa
  - `review/architecture` v2 ativa
- `delegate_task_to_gpt` permanece rastreável via note + artifact automáticos
- rastreabilidade de planner mantida via plan artifact (`strategy`) + planner note

### Observabilidade real do MVP

- `ask_gpt` direto: auditável via `audit_log`, consultável de forma humana via note `[GPT-CONSULT]`
- `delegate_task_to_gpt`: já rastreável via note automática e artifact `{task_kind}-{task_id}.md`
- planner GPT: rastreável por `strategy` no artifact `{root_task_id}-plan.json` e pela note do planner
- contagem automática de `[GPT-CONSULT]` em retrospective: **adiada**, fora deste corte

### Bloqueio objetivo para validação operacional

- `OPENAI_API_KEY` estava **unset** no ambiente desta rodada
- por isso, a policy foi implementada e validada em código, mas **não** em 2 sessões reais com `ask_gpt`
- esse bloqueio é de configuração de ambiente, não de código

### Veredito

**Policy Claude vs Ask GPT IMPLEMENTADA e VALIDADA.** O corte mínimo ficou coerente com o hub: knowledge curada + playbooks versionados + convenção `[GPT-CONSULT]`, sem schema novo e sem segunda tool wrapper. Validação operacional concluída com `OPENAI_API_KEY` configurada — 2 sessões (architecture review com GPT counterpoint + backend sem GPT), 6/6 checks PASS.

---

## Especialista Documentation/Planning — 2026-03-13

Objetivo: fechar o gap de cobertura do domínio `process` — já existente como domínio mas sem playbooks nem agente especialista.

### Entrega implementada

| Componente | O que fez |
|------------|-----------|
| Keywords | +2 em `DOMAIN_KEYWORDS["process"]`: `documentation`, `roadmap`. Total: 11 keywords. |
| Playbooks | +2 novos: `work/process` (5 steps, 4 checklist, advisory) e `review/process` (4 steps, 4 checklist, advisory). |
| Artifact convention | `doc-{task_id}` para work, `doc-{source_task_id}` para review. |
| Testes | +11 testes → 339/339 passando. Testes isolados de routing process com save/restore (padrão frontend/architecture). |

### Playbooks

**work/process** (advisory):
1. Identificar tipo de entregável (doc, plano, relatório, guia)
2. Consultar `query_knowledge` e `recall_memory` para padrões existentes
3. Identificar audiência e estado atual do sistema como referência
4. Publicar artifact `doc-{task_id}` (text/markdown)
5. Registrar decisões de escopo com `record_decision` se aplicável

**review/process** (advisory):
1. Ler artifact `doc-{source_task_id}` e comparar com pedido
2. Verificar se referencia estado real (não é genérico/placeholder)
3. Avaliar completude, clareza e definição de audiência
4. Se afeta decisões técnicas, verificar consistência com `query_decisions`

### Decisões de design

| Decisão | Rationale |
|---------|-----------|
| Sem domínio novo | `process` já existe com 9 keywords, classificação e propagação. Criar domínio `documentation` fragmentaria sem benefício. |
| Sem ask_gpt nos playbooks | Docs são autoria, não análise. GPT counterpoint não agrega. Review genérico já documenta [GPT-CONSULT] para quem precisar. |
| Enforcement advisory | Documentação com gate obrigatório cria atrito sem ganho proporcional. |
| Títulos mistos → architecture | "Improve documentation structure" classifica como `architecture` via DOMAIN_PRIORITY (6ª > 7ª). Comportamento correto e documentado. |

### Validação operacional

**Session A (process):**
- Task "Write project documentation for onboarding" → domain auto-classificado: `process`
- Playbook `work/process` resolvido com 5 steps
- Artifact `doc-{task_id}` publicado (text/markdown)
- Review `review/process` resolvido com 4 steps
- Resultado: 6/6 checks PASS

**Session B (backend negativo):**
- Task "Add rate limiting to API endpoints" → domain: `backend`
- Playbook `work/backend` sem convenção `doc-{task_id}`
- Confirmado: nenhuma contaminação cruzada

### Review crítica

`[GPT-CONSULT] role=counterpoint | purpose=validate process playbook implementation | result=agreed | action=noted`

GPT levantou 6 pontos. Classificação: 0 bloqueantes, 0 importantes, 6 opcionais. Keyword "documentation" analisada — weight 2 só ganha sem keyword de outro domínio no título. Ausência de ask_gpt nos playbooks é divergência consciente (docs são autoria).

### Veredito

**Trilha Documentation/Planning VALIDADA.** Domínio `process` operacionalizado com playbooks específicos, artifact naming convention, keywords expandidas, e routing isolado. Sem schema novo, sem tool nova, sem breaking change.

---

## Pendências e Próximos Passos

### Próximos passos candidatos (por prioridade)

1. ~~**Validação operacional da trilha Front-end/UI**~~ — **CONCLUÍDA** em 2 sessões reais (2026-03-13)
2. ~~**Promoção de magic-mcp para knowledge**~~ — **CONCLUÍDA** após validação real da trilha Front-end/UI
3. ~~**Validação operacional da trilha Architecture**~~ — **CONCLUÍDA** em 1 sessão real (2026-03-13)
4. ~~**Validação operacional da policy Claude vs Ask GPT**~~ — **CONCLUÍDA** (2026-03-13). API key configurada, 2 sessões (architecture review com GPT counterpoint + backend negativo), 6/6 checks PASS.
5. ~~**Especialista Documentation/Planning**~~ — **CONCLUÍDA** (2026-03-13). Playbooks `work/process` + `review/process`, keywords expandidas, validação operacional concluída.
6. ~~**CLI para knowledge layer**~~ — **CONCLUÍDA** (2026-03-13). `hub_cli.py` agora expõe `query-knowledge`, `promote-knowledge`, `approve-knowledge`, `supersede-knowledge`, `deprecate-knowledge`; `submit` também usa `codex-general` como synth default e `ensure_ready()` no bootstrap. 347/347 testes.

### Known limitations do auto-routing

1. **Tiebreak alfabético**: se dois agentes têm o mesmo domain e task_kind, o primeiro alfabeticamente é escolhido. Pode não ser o melhor em todos os casos.
2. **Single-domain resolution**: `_find_specialist` busca pelo domain do request. Se o request tiver keywords de múltiplos domains, o classify_domain escolhe um só.
3. **Server stale**: alterações em código requerem restart do MCP server. Não há hot-reload.

### Dados acumulados

| Métrica | Total |
|---------|-------|
| task_metrics | 57 |
| completion_rate global | 100% |
| rework_rate global | 0% |
| fallback_rate global | 0% |
| memory_entries | 4 |
| decisions | 5 |
| knowledge_entries ativas | 4 |
| playbooks ativos | 12 |
| agent_profiles | 7 (6 ativos + 1 inativo) |
| retrospectives persistidas | 9 |
| validações operacionais | 8 rodadas (5 anteriores + 2 frontend + 1 architecture) |

---

## Handoff para Próxima Sessão

**Projeto**: agent-hub-mcp em `~/agent-hub-mcp`
**O que foi entregue**: v3.0–v3.4 completas + Fase B validada + Auto-routing implementado e validado + Checklist Enforced (opt-in) implementado e validado operacionalmente para `work/automation` + Retrospective On-Demand (etapa 1) implementada com 2 tools MCP, índice `UNIQUE` por root e leitura persistida + trilha Front-end/UI implementada com playbooks específicos advisory e validada operacionalmente + trilha Architecture implementada com playbooks específicos advisory e integração explícita com `record_decision` + policy Claude vs Ask GPT implementada e validada operacionalmente + trilha Documentation/Planning implementada com playbooks `process` específicos advisory e validada operacionalmente + CLI para knowledge layer implementada no `hub_cli.py`.
**Estado**: 347/347 testes passando. 37 tools MCP, 12 tabelas, 8 domínios. 12 playbooks ativos.
**Checkpoint**: `docs/agent-hub-v3-status.md` (este arquivo — canônico, arquivo único)
**Memória persistente**: `~/.claude/projects/-home-rdios/memory/agent-hub.md`
**Repo**: github.com/rdioscaio/agent-hub (branch main)
**Próximo**: retrospective automática etapa 2, observabilidade de `[GPT-CONSULT]` na retrospective, ou ajuste orientado por uso real.
