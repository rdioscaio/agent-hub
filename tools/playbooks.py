"""
Playbook tools: get_playbook, validate_checklist.

Playbooks provide structured guidance for agents executing tasks.
Mode: ADVISORY only — validate_checklist records scores as notes but never
blocks task completion.

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
    # Remove internal fields from output
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
        "steps": [
            "1. Ler artifact da work task (via source_task_id)",
            "2. Comparar com pedido original (root task description)",
            "3. Avaliar: correção, completude, edge cases ignorados",
            "4. Se usar ask_gpt como contraponto, enviar com data_policy='snippets'",
        ],
        "checklist": [
            "Leu artifact da work task?",
            "Comparou com pedido original?",
            "Feedback é específico e acionável?",
            "Verdict é justificado?",
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
            conn.execute(
                """
                INSERT INTO playbooks
                    (id, task_kind, domain, steps, checklist, version, active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 1, 1, ?, ?)
                """,
                (
                    playbook_id,
                    playbook["task_kind"],
                    playbook["domain"],
                    json.dumps(playbook["steps"], ensure_ascii=False),
                    json.dumps(playbook["checklist"], ensure_ascii=False),
                    now,
                    now,
                ),
            )
            created += 1

    return {"ok": True, "created": created, "skipped": skipped}
