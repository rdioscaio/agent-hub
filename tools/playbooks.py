"""
Playbook tools: get_playbook, validate_checklist.

Playbooks provide structured guidance for agents executing tasks.
Mode: ADVISORY by default — validate_checklist records scores as notes.
When a playbook has enforcement='required', complete_task blocks unless
a valid checklist with score >= 1.0 exists (opt-in gate).

Fallback policy (get_playbook):
    1. Try exact match: (task_kind, domain, active=1)
    2. If not found and domain != '*', fall back to (task_kind, domain='*', active=1)
    3. If still not found, return error.

Limits:
    - steps: max 5 items (truncated if seed/data has more)
    - checklist: max 4 items (truncated if seed/data has more)

seed_default_playbooks is an internal bootstrap function, NOT an MCP tool.
"""

import json
import time
import uuid

from hub.audit import audit
from hub.db import get_conn
from tools.notes import append_note

MAX_STEPS = 5
MAX_CHECKLIST = 4

VALID_TASK_KINDS = {"work", "review", "rework", "fallback", "synthesize"}

_GENERIC_REVIEW_STEPS_V1 = [
    "1. Ler artifact da work task (via source_task_id)",
    "2. Comparar com pedido original (root task description)",
    "3. Avaliar: correção, completude, edge cases ignorados",
    "4. Se usar ask_gpt como contraponto, enviar com data_policy='snippets'",
]

_GENERIC_REVIEW_STEPS_V2 = [
    "1. Ler artifact da work task (via source_task_id)",
    "2. Comparar com pedido original (root task description)",
    "3. Avaliar: correção, completude, edge cases ignorados",
    "4. Se usar ask_gpt, registrar note '[GPT-CONSULT] role=<counterpoint|auditor> | purpose=<motivo> | result=<agreed|diverged|partial> | action=<adopted|discarded|noted>'",
]

_ARCHITECTURE_WORK_STEPS_V1 = [
    "1. Ler a description e identificar a decisao ou mudanca estrutural esperada",
    "2. Consultar query_decisions(domain='architecture') e recall_memory(domain='architecture') para contexto previo",
    "3. Analisar boundaries, dependencias e tradeoffs relevantes ao problema",
    "4. Publicar artifact 'arch-decision-{task_id}' com decisao, rationale, alternativas e impacto",
    "5. Registrar a decisao com record_decision(domain='architecture', source_task_id=<task_id>, root_task_id=<root_task_id>)",
]

_ARCHITECTURE_WORK_STEPS_V2 = [
    "1. Ler a description e identificar a decisao ou mudanca estrutural esperada",
    "2. Consultar query_decisions e recall_memory(domain='architecture') para contexto, boundaries, dependencias e tradeoffs",
    "3. Se houver >2 alternativas viaveis, considerar ask_gpt como counterpoint e registrar note '[GPT-CONSULT]'",
    "4. Publicar artifact 'arch-decision-{task_id}' com decisao, rationale, alternativas e impacto",
    "5. Registrar a decisao com record_decision(domain='architecture', source_task_id=<task_id>, root_task_id=<root_task_id>)",
]

_ARCHITECTURE_REVIEW_STEPS_V1 = [
    "1. Ler artifact 'arch-decision-{source_task_id}' da work task",
    "2. Verificar se o rationale e sustentado por constraints reais do projeto",
    "3. Avaliar se alternativas foram genuinamente consideradas e documentadas",
    "4. Verificar se impacto em boundaries e dependencias existentes foi mapeado",
]

_ARCHITECTURE_REVIEW_STEPS_V2 = [
    "1. Ler artifact 'arch-decision-{source_task_id}' da work task",
    "2. Verificar se o rationale e sustentado por constraints reais do projeto",
    "3. Avaliar se alternativas foram genuinamente consideradas e documentadas",
    "4. Verificar se impacto em boundaries e dependencias existentes foi mapeado",
    "5. Para tradeoffs nao-triviais, considerar ask_gpt como counterpoint e registrar note '[GPT-CONSULT]'",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_json_list(value) -> list[str]:
    """Parse a JSON list stored as TEXT, returning a list of strings."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(item) for item in parsed]
        except (TypeError, json.JSONDecodeError):
            pass
    return []


def _playbook_from_row(row) -> dict:
    """Convert a DB row to a playbook dict with parsed lists and enforced limits."""
    playbook = dict(row)
    playbook["steps"] = _parse_json_list(playbook.get("steps"))[:MAX_STEPS]
    playbook["checklist"] = _parse_json_list(playbook.get("checklist"))[:MAX_CHECKLIST]
    # Remove internal fields from output (keep enforcement)
    for key in ("active", "created_at", "updated_at"):
        playbook.pop(key, None)
    return playbook


# ---------------------------------------------------------------------------
# get_playbook
# ---------------------------------------------------------------------------

def get_playbook(task_kind: str, domain: str = "*") -> dict:
    """Get the active playbook for a task kind, with fallback from specific domain to generic.

    Fallback: if no playbook exists for the requested domain, tries domain='*'.

    Args:
        task_kind: Required. One of: work, review, rework, fallback, synthesize.
        domain:    Optional. Specific domain or '*' for generic (default '*').
    """
    args = dict(task_kind=task_kind, domain=domain)
    with audit("get_playbook", args):
        if not task_kind or not task_kind.strip():
            return {"ok": False, "error": "'task_kind' is required and cannot be empty"}
        task_kind = task_kind.strip()
        if task_kind not in VALID_TASK_KINDS:
            return {"ok": False, "error": f"invalid task_kind '{task_kind}'. Valid: {', '.join(sorted(VALID_TASK_KINDS))}"}

        domain = (domain or "*").strip()

        with get_conn() as conn:
            # Try exact domain match first
            row = conn.execute(
                "SELECT * FROM playbooks WHERE task_kind = ? AND domain = ? AND active = 1 "
                "ORDER BY version DESC LIMIT 1",
                (task_kind, domain),
            ).fetchone()

            # Fallback to generic if specific domain not found
            if not row and domain != "*":
                row = conn.execute(
                    "SELECT * FROM playbooks WHERE task_kind = ? AND domain = ? AND active = 1 "
                    "ORDER BY version DESC LIMIT 1",
                    (task_kind, "*"),
                ).fetchone()

        if not row:
            return {
                "ok": False,
                "error": f"no playbook found for task_kind='{task_kind}', domain='{domain}' (also tried generic '*')",
            }

        return {"ok": True, "playbook": _playbook_from_row(row)}


# ---------------------------------------------------------------------------
# validate_checklist
# ---------------------------------------------------------------------------

def validate_checklist(
    task_id: str,
    responses: list,
    validator: str = "",
) -> dict:
    """Validate checklist responses and record score as an advisory note.

    This is ADVISORY only — it never blocks task completion.
    Each response should be a dict with: {"item": str, "passed": bool, "note": str (optional)}.

    Args:
        task_id:    Required. The task being validated.
        responses:  Required. List of checklist response dicts.
        validator:  Optional. Who performed the validation.
    """
    args = dict(task_id=task_id, validator=validator, response_count=len(responses) if responses else 0)
    with audit("validate_checklist", args, task_id):
        if not task_id or not task_id.strip():
            return {"ok": False, "error": "'task_id' is required and cannot be empty"}

        if not responses or not isinstance(responses, list) or len(responses) == 0:
            return {"ok": False, "error": "'responses' is required and must be a non-empty list"}

        # Validate and normalize responses (max 4 items)
        normalized = []
        for i, resp in enumerate(responses[:MAX_CHECKLIST]):
            if not isinstance(resp, dict):
                return {"ok": False, "error": f"response at index {i} must be a dict with 'item' and 'passed' keys"}
            item = resp.get("item")
            if not item or not str(item).strip():
                return {"ok": False, "error": f"response at index {i} is missing required 'item' field"}
            passed = resp.get("passed")
            if not isinstance(passed, bool):
                return {"ok": False, "error": f"response at index {i} is missing required 'passed' field (must be bool)"}
            normalized.append({
                "item": str(item).strip(),
                "passed": passed,
                "note": str(resp.get("note") or "").strip(),
            })

        total = len(normalized)
        passed_count = sum(1 for r in normalized if r["passed"])
        score = passed_count / total if total > 0 else 0.0
        failed_items = [r["item"] for r in normalized if not r["passed"]]

        # Record as structured advisory note
        note_content = json.dumps({
            "type": "checklist_validation",
            "advisory": True,
            "validator": validator or "unknown",
            "score": round(score, 2),
            "total": total,
            "passed": passed_count,
            "failed_items": failed_items,
            "responses": normalized,
        }, ensure_ascii=False)

        note_result = append_note(
            content=f"[CHECKLIST ADVISORY] score={score:.0%} ({passed_count}/{total}) | {note_content}",
            task_id=task_id.strip(),
            author=validator or "system",
        )

        return {
            "ok": True,
            "task_id": task_id.strip(),
            "score": round(score, 2),
            "total": total,
            "passed": passed_count,
            "failed_items": failed_items,
            "advisory": True,
            "note_id": note_result.get("note_id", ""),
        }


# ---------------------------------------------------------------------------
# seed_default_playbooks (internal bootstrap, NOT an MCP tool)
# ---------------------------------------------------------------------------

_DEFAULT_PLAYBOOKS = [
    {
        "task_kind": "work",
        "domain": "*",
        "steps": [
            "1. Ler a description da task e identificar o entregável esperado",
            "2. Consultar recall_memory e query_decisions para contexto relevante",
            "3. Executar o trabalho e publicar resultado como artifact",
            "4. Registrar decisões relevantes com record_decision",
            "5. Registrar aprendizados novos com store_memory",
        ],
        "checklist": [
            "Entregável corresponde ao pedido na description?",
            "Artifact foi publicado com nome descritivo?",
            "Decisões técnicas foram registradas?",
            "Nota de conclusão foi adicionada?",
        ],
    },
    {
        "task_kind": "review",
        "domain": "*",
        "steps": _GENERIC_REVIEW_STEPS_V2,
        "checklist": [
            "Leu artifact da work task?",
            "Comparou com pedido original?",
            "Feedback é específico e acionável?",
            "Verdict é justificado?",
        ],
    },
    {
        "task_kind": "synthesize",
        "domain": "*",
        "steps": [
            "1. Ler artifacts e notes das tasks aprovadas no root_task_id",
            "2. Consolidar o que foi validado e separar riscos ou gaps pendentes",
            "3. Montar uma resposta final clara, sem esconder pendencias",
            "4. Publicar artifact final se houver entregavel consolidado",
        ],
        "checklist": [
            "Leu os artifacts relevantes das tasks anteriores?",
            "Separou claramente resultado final de riscos pendentes?",
            "Resposta final esta coerente com reviews aprovadas?",
            "Artifact final foi publicado quando aplicavel?",
        ],
    },
    {
        "task_kind": "work",
        "domain": "backend",
        "steps": [
            "1. Identificar arquivos impactados no servidor (NestJS/Express)",
            "2. Consultar recall_memory(domain='backend') para padrões do projeto",
            "3. Implementar seguindo convenções existentes do codebase",
            "4. Validar que não introduz vulnerabilidade OWASP top 10",
            "5. Publicar código como artifact e registrar decisões",
        ],
        "checklist": [
            "Código segue padrões existentes do projeto?",
            "Sem vulnerabilidade de segurança introduzida?",
            "Artifact publicado com nome descritivo?",
            "Decisões técnicas registradas?",
        ],
    },
    {
        "task_kind": "work",
        "domain": "frontend",
        "enforcement": "advisory",
        "steps": [
            "1. Ler a description e identificar o componente ou pagina esperada",
            "2. Consultar recall_memory(domain='frontend') e query_knowledge(domain='frontend') para padroes existentes",
            "3. Se o componente for visual-first (landing, dashboard, hero), considerar magic-mcp como scaffold inicial, nunca como entregavel final",
            "4. Implementar seguindo convencoes do projeto (React, Tailwind, mobile-first) e publicar codigo como artifact",
            "5. Publicar artifact 'ui-evidence-{task_id}' com evidencias textuais de responsividade, acessibilidade e consistencia",
        ],
        "checklist": [
            "Componente renderiza sem erros no console?",
            "Artifact 'ui-evidence-{task_id}' publicado com breakpoints e acessibilidade verificados?",
            "Acessibilidade basica respeitada (labels, contraste, foco)?",
            "Artifact de codigo publicado com nome descritivo?",
        ],
    },
    {
        "task_kind": "review",
        "domain": "frontend",
        "enforcement": "advisory",
        "steps": [
            "1. Ler artifact de codigo da work task e comparar com o pedido original",
            "2. Ler artifact 'ui-evidence-{source_task_id}' e verificar se os itens foram preenchidos de forma plausivel",
            "3. Avaliar no codigo responsividade, labels ou aria-attributes e consistencia de spacing ou tipografia",
            "4. Se houver problema no ui-evidence ou no codigo, incluir feedback com referencia concreta",
        ],
        "checklist": [
            "Artifact 'ui-evidence-{source_task_id}' existe e esta preenchido?",
            "Codigo demonstra responsividade (classes responsivas ou media queries)?",
            "Sem violacao de acessibilidade obvia no codigo (labels, contraste, foco)?",
            "Feedback referencia elementos ou linhas concretas?",
        ],
    },
    {
        "task_kind": "work",
        "domain": "architecture",
        "enforcement": "advisory",
        "steps": _ARCHITECTURE_WORK_STEPS_V2,
        "checklist": [
            "Artifact 'arch-decision-{task_id}' publicado com decisao e rationale?",
            "Alternativas foram consideradas e documentadas?",
            "Decisao registrada via record_decision com source_task_id e root_task_id?",
            "Impacto em boundaries ou dependencias existentes foi avaliado?",
        ],
    },
    {
        "task_kind": "review",
        "domain": "architecture",
        "enforcement": "advisory",
        "steps": _ARCHITECTURE_REVIEW_STEPS_V2,
        "checklist": [
            "Artifact 'arch-decision-{source_task_id}' existe e contem decisao com rationale?",
            "Rationale e sustentado por constraints reais, nao por preferencia?",
            "Alternativas foram genuinamente consideradas?",
            "Impacto em boundaries ou dependencias existentes foi mapeado?",
        ],
    },
    {
        "task_kind": "work",
        "domain": "automation",
        "enforcement": "required",
        "steps": [
            "1. Consultar query_knowledge(domain='automation') para templates existentes",
            "2. Desenhar o fluxo e publicar como artifact 'flow-definition-{task_id}'",
            "3. Inventariar dependencias externas em artifact 'credentials-{task_id}'",
            "4. Testar e publicar evidencia em artifact 'staging-evidence-{task_id}'",
            "5. Preparar rollback plan em artifact 'rollback-plan-{task_id}'",
        ],
        "checklist": [
            "Artifact 'flow-definition-{task_id}' publicado?",
            "Dependencias e credenciais inventariadas sem expor secrets?",
            "Rollback plan documentado em artifact?",
            "Staging testado com evidencia publicada?",
        ],
    },
    {
        "task_kind": "review",
        "domain": "automation",
        "steps": [
            "1. Ler artifact 'flow-definition-{source_task_id}' da work task",
            "2. Verificar que 'rollback-plan-{source_task_id}' existe e e executavel",
            "3. Confirmar que 'credentials-{source_task_id}' nao expoe secrets",
            "4. Verificar que 'staging-evidence-{source_task_id}' comprova teste",
        ],
        "checklist": [
            "Flow definition e completa e correta?",
            "Rollback plan e executavel em caso de falha?",
            "Nenhum secret hardcoded no flow ou credenciais?",
            "Staging testado com evidencia publicada?",
        ],
    },
    {
        "task_kind": "work",
        "domain": "process",
        "enforcement": "advisory",
        "steps": [
            "1. Ler a description e identificar o tipo de entregavel (doc, plano, relatorio, guia)",
            "2. Consultar query_knowledge(domain='process') e recall_memory(domain='process') para padroes e docs existentes",
            "3. Identificar a audiencia do documento e o estado atual do sistema como referencia",
            "4. Produzir o conteudo e publicar como artifact 'doc-{task_id}' (content_type text/markdown)",
            "5. Registrar decisoes de escopo ou estrutura com record_decision se aplicavel",
        ],
        "checklist": [
            "Artifact 'doc-{task_id}' publicado com conteudo substantivo?",
            "Audiencia do documento esta clara (para quem e)?",
            "Conteudo referencia estado real e atual do sistema?",
            "Decisoes de escopo registradas quando aplicavel?",
        ],
    },
    {
        "task_kind": "review",
        "domain": "process",
        "enforcement": "advisory",
        "steps": [
            "1. Ler artifact 'doc-{source_task_id}' e comparar com o pedido original",
            "2. Verificar se o conteudo referencia o estado real do sistema (nao e generico ou placeholder)",
            "3. Avaliar completude, clareza e se a audiencia esta bem definida",
            "4. Se o doc afeta decisoes tecnicas, verificar consistencia com query_decisions(domain relevante)",
        ],
        "checklist": [
            "Artifact 'doc-{source_task_id}' existe e tem conteudo substantivo?",
            "Conteudo e verificavel contra o estado real do sistema?",
            "Audiencia e proposito estao claros?",
            "Feedback e especifico e referencia trechos concretos?",
        ],
    },
]


def seed_default_playbooks() -> dict:
    """Seed default playbooks if they don't already exist.

    Idempotent: skips creation if an active playbook for the same
    (task_kind, domain) already exists.

    This is an INTERNAL function — not exposed as an MCP tool.
    """
    created = 0
    skipped = 0
    now = time.time()

    with get_conn() as conn:
        for playbook in _DEFAULT_PLAYBOOKS:
            existing = conn.execute(
                "SELECT id FROM playbooks WHERE task_kind = ? AND domain = ? AND active = 1 LIMIT 1",
                (playbook["task_kind"], playbook["domain"]),
            ).fetchone()

            if existing:
                skipped += 1
                continue

            playbook_id = str(uuid.uuid4())
            enforcement = playbook.get("enforcement", "advisory")
            conn.execute(
                """
                INSERT INTO playbooks
                    (id, task_kind, domain, steps, checklist, enforcement, version, active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 1, 1, ?, ?)
                """,
                (
                    playbook_id,
                    playbook["task_kind"],
                    playbook["domain"],
                    json.dumps(playbook["steps"], ensure_ascii=False),
                    json.dumps(playbook["checklist"], ensure_ascii=False),
                    enforcement,
                    now,
                    now,
                ),
            )
            created += 1

    return {"ok": True, "created": created, "skipped": skipped}


def _insert_playbook_version(
    conn,
    task_kind: str,
    domain: str,
    steps: list[str],
    checklist: list[str],
    enforcement: str,
) -> None:
    now = time.time()
    next_version = conn.execute(
        "SELECT COALESCE(MAX(version), 0) + 1 AS next_version FROM playbooks WHERE task_kind = ? AND domain = ?",
        (task_kind, domain),
    ).fetchone()["next_version"]
    conn.execute(
        "UPDATE playbooks SET active = 0, updated_at = ? WHERE task_kind = ? AND domain = ? AND active = 1",
        (now, task_kind, domain),
    )
    conn.execute(
        """
        INSERT INTO playbooks
            (id, task_kind, domain, steps, checklist, enforcement, version, active, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            task_kind,
            domain,
            json.dumps(steps, ensure_ascii=False),
            json.dumps(checklist, ensure_ascii=False),
            enforcement,
            next_version,
            now,
            now,
        ),
    )


def upgrade_default_playbooks() -> dict:
    """Upgrade selected legacy playbooks in-place for existing databases.

    This migration is intentionally conservative: it upgrades only when the
    currently active playbook exactly matches the known legacy v1 content.
    That preserves operator customizations while allowing the hub to evolve
    canonical defaults for fresh databases.
    """
    targets = {
        ("review", "*"): {
            "legacy_steps": _GENERIC_REVIEW_STEPS_V1,
            "new_steps": _GENERIC_REVIEW_STEPS_V2,
            "checklist": [
                "Leu artifact da work task?",
                "Comparou com pedido original?",
                "Feedback é específico e acionável?",
                "Verdict é justificado?",
            ],
            "enforcement": "advisory",
        },
        ("work", "architecture"): {
            "legacy_steps": _ARCHITECTURE_WORK_STEPS_V1,
            "new_steps": _ARCHITECTURE_WORK_STEPS_V2,
            "checklist": [
                "Artifact 'arch-decision-{task_id}' publicado com decisao e rationale?",
                "Alternativas foram consideradas e documentadas?",
                "Decisao registrada via record_decision com source_task_id e root_task_id?",
                "Impacto em boundaries ou dependencias existentes foi avaliado?",
            ],
            "enforcement": "advisory",
        },
        ("review", "architecture"): {
            "legacy_steps": _ARCHITECTURE_REVIEW_STEPS_V1,
            "new_steps": _ARCHITECTURE_REVIEW_STEPS_V2,
            "checklist": [
                "Artifact 'arch-decision-{source_task_id}' existe e contem decisao com rationale?",
                "Rationale e sustentado por constraints reais, nao por preferencia?",
                "Alternativas foram genuinamente consideradas?",
                "Impacto em boundaries ou dependencias existentes foi mapeado?",
            ],
            "enforcement": "advisory",
        },
    }

    upgraded = 0
    skipped = 0

    with get_conn() as conn:
        for (task_kind, domain), target in targets.items():
            row = conn.execute(
                "SELECT * FROM playbooks WHERE task_kind = ? AND domain = ? AND active = 1 ORDER BY version DESC LIMIT 1",
                (task_kind, domain),
            ).fetchone()
            if not row:
                skipped += 1
                continue

            playbook = _playbook_from_row(row)
            if (
                playbook["steps"] == target["new_steps"]
                and playbook["checklist"] == target["checklist"]
                and playbook.get("enforcement", "advisory") == target["enforcement"]
            ):
                skipped += 1
                continue

            if playbook["steps"] != target["legacy_steps"]:
                skipped += 1
                continue

            _insert_playbook_version(
                conn,
                task_kind,
                domain,
                target["new_steps"],
                target["checklist"],
                target["enforcement"],
            )
            upgraded += 1

    return {"ok": True, "upgraded": upgraded, "skipped": skipped}
