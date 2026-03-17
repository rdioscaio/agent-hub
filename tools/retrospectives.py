"""
Retrospective tools: generate_retrospective, get_retrospective.

Retrospective On-Demand (etapa 1):
    - generate_retrospective: builds and persists a deterministic retrospective
      for a completed request tree. Immutable — second call returns existing.
    - get_retrospective: reads a stored retrospective by root_task_id.

Rules:
    - 1 retrospective per root_task_id (enforced by UNIQUE index + application check)
    - Immutable after creation — no update, recalculation, or regeneration
    - All operational tasks in the tree must be in a final state before generation
    - No LLM involved — purely deterministic heuristics
"""

import json
import sqlite3
import time
import uuid

from hub.audit import audit
from hub.db import get_conn


FINAL_STATES = {"done", "failed", "canceled"}


def _build_retrospective(tasks: list[dict], metrics: list[dict], gate_block_count: int) -> dict:
    """Build a deterministic retrospective summary from raw data.

    Internal helper — NOT an MCP tool.
    """
    total_tasks = len(tasks)
    operational_tasks = [task for task in tasks if task.get("task_kind") != "request"]
    tasks_by_status: dict[str, int] = {}
    quality_summary: dict[str, int] = {}
    review_rounds = 0

    min_created = float("inf")
    max_updated = 0.0

    for task in tasks:
        status = task.get("status") or "unknown"
        tasks_by_status[status] = tasks_by_status.get(status, 0) + 1

        quality = task.get("quality_status") or "pending"
        quality_summary[quality] = quality_summary.get(quality, 0) + 1

        if task.get("task_kind") == "review":
            review_rounds += 1

        created = task.get("created_at") or 0
        updated = task.get("updated_at") or 0
        if created and created < min_created:
            min_created = created
        if updated and updated > max_updated:
            max_updated = updated

    total_duration_s = round(max_updated - min_created, 1) if min_created < float("inf") and max_updated > 0 else 0

    has_failed = any((task.get("status") or "unknown") == "failed" for task in operational_tasks)
    has_pending = any((task.get("status") or "unknown") not in FINAL_STATES for task in operational_tasks)
    if has_failed:
        outcome = "has_failed"
    elif has_pending:
        outcome = "has_pending"
    else:
        outcome = "all_done"

    return {
        "total_tasks": total_tasks,
        "tasks_by_status": tasks_by_status,
        "total_duration_s": total_duration_s,
        "gate_blocks": gate_block_count,
        "review_rounds": review_rounds,
        "quality_summary": quality_summary,
        "outcome": outcome,
    }


def generate_retrospective(root_task_id: str, generated_by: str = "system") -> dict:
    """Generate and persist an immutable retrospective for a completed request tree.

    Idempotent: if a retrospective already exists for this root_task_id,
    returns it with already_exists=True. Never recalculates.

    All operational tasks in the tree must be in a final state
    (done, failed, canceled). The root request task may remain open.

    Args:
        root_task_id: Required. The root task ID of the request tree.
        generated_by:  Optional. Who triggered generation (default "system").
    """
    args = dict(root_task_id=root_task_id, generated_by=generated_by)
    with audit("generate_retrospective", args, root_task_id):
        if not root_task_id or not str(root_task_id).strip():
            return {"ok": False, "error": "'root_task_id' is required and cannot be empty"}

        root_task_id = root_task_id.strip()

        with get_conn() as conn:
            # Check for existing retrospective (immutability rule)
            existing = conn.execute(
                "SELECT * FROM retrospectives WHERE root_task_id = ? LIMIT 1",
                (root_task_id,),
            ).fetchone()
            if existing:
                retro = dict(existing)
                retro["summary"] = json.loads(retro["summary"]) if isinstance(retro["summary"], str) else retro["summary"]
                retro["bottlenecks"] = json.loads(retro["bottlenecks"]) if isinstance(retro["bottlenecks"], str) else retro["bottlenecks"]
                retro["improvements"] = json.loads(retro["improvements"]) if isinstance(retro["improvements"], str) else retro["improvements"]
                return {"ok": True, "retrospective_id": retro["id"], "root_task_id": root_task_id, "already_exists": True, "retrospective": retro}

            # Verify root task exists
            root_row = conn.execute(
                "SELECT id, domain FROM tasks WHERE id = ?",
                (root_task_id,),
            ).fetchone()
            if not root_row:
                return {"ok": False, "error": "root task not found"}

            domain = root_row["domain"] or "general"

            # Fetch all tasks in the tree
            task_rows = conn.execute(
                "SELECT id, status, task_kind, quality_status, created_at, updated_at FROM tasks WHERE root_task_id = ?",
                (root_task_id,),
            ).fetchall()
            tasks = [dict(row) for row in task_rows]

            if not tasks:
                return {"ok": False, "error": "no tasks found for this root_task_id"}

            operational_tasks = [t for t in tasks if t.get("task_kind") != "request"]
            if not operational_tasks:
                return {"ok": False, "error": "request has no operational tasks to summarize"}

            # Verify all operational tasks are in final state.
            # The root request task may remain open; it is orchestration state,
            # not a work artifact that should block retrospective generation.
            non_final = [
                t for t in operational_tasks
                if t.get("status") not in FINAL_STATES
            ]
            if non_final:
                return {"ok": False, "error": f"request has {len(non_final)} tasks not in final state"}

            # Count gate blocks from notes
            gate_block_count = conn.execute(
                "SELECT COUNT(*) as cnt FROM notes WHERE task_id IN "
                "(SELECT id FROM tasks WHERE root_task_id = ?) "
                "AND content LIKE '%[CHECKLIST GATE] blocked%'",
                (root_task_id,),
            ).fetchone()["cnt"]

            # Fetch metrics for this root
            metric_rows = conn.execute(
                "SELECT * FROM task_metrics WHERE root_task_id = ?",
                (root_task_id,),
            ).fetchall()
            metrics = [dict(row) for row in metric_rows]

            # Build deterministic summary
            summary = _build_retrospective(tasks, metrics, gate_block_count)

            # Derive bottlenecks heuristically
            bottlenecks = []
            if gate_block_count > 0:
                bottlenecks.append(f"checklist gate blocked {gate_block_count} time(s)")
            failed_count = summary["tasks_by_status"].get("failed", 0)
            if failed_count > 0:
                bottlenecks.append(f"{failed_count} task(s) failed")

            # Derive improvements heuristically
            improvements = []
            if gate_block_count > 1:
                improvements.append("reduce checklist failures by validating earlier in the workflow")
            if failed_count > 0:
                improvements.append("investigate failure root causes before next request")

            # Persist
            retro_id = str(uuid.uuid4())
            now = time.time()
            try:
                conn.execute(
                    """
                    INSERT INTO retrospectives
                        (id, root_task_id, summary, bottlenecks, improvements, domain, generated_by, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        retro_id,
                        root_task_id,
                        json.dumps(summary, ensure_ascii=False),
                        json.dumps(bottlenecks, ensure_ascii=False),
                        json.dumps(improvements, ensure_ascii=False),
                        domain,
                        generated_by or "system",
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                # Race condition: another call inserted between our SELECT and INSERT
                existing = conn.execute(
                    "SELECT * FROM retrospectives WHERE root_task_id = ? LIMIT 1",
                    (root_task_id,),
                ).fetchone()
                if existing:
                    retro = dict(existing)
                    retro["summary"] = json.loads(retro["summary"]) if isinstance(retro["summary"], str) else retro["summary"]
                    retro["bottlenecks"] = json.loads(retro["bottlenecks"]) if isinstance(retro["bottlenecks"], str) else retro["bottlenecks"]
                    retro["improvements"] = json.loads(retro["improvements"]) if isinstance(retro["improvements"], str) else retro["improvements"]
                    return {"ok": True, "retrospective_id": retro["id"], "root_task_id": root_task_id, "already_exists": True, "retrospective": retro}
                return {"ok": False, "error": "failed to persist retrospective"}

        retro_data = {
            "id": retro_id,
            "root_task_id": root_task_id,
            "summary": summary,
            "bottlenecks": bottlenecks,
            "improvements": improvements,
            "domain": domain,
            "generated_by": generated_by or "system",
            "created_at": now,
        }
        return {"ok": True, "retrospective_id": retro_id, "root_task_id": root_task_id, "already_exists": False, "retrospective": retro_data}


def get_retrospective(root_task_id: str) -> dict:
    """Read a stored retrospective by root_task_id.

    Args:
        root_task_id: Required. The root task ID to look up.
    """
    args = dict(root_task_id=root_task_id)
    with audit("get_retrospective", args, root_task_id):
        if not root_task_id or not str(root_task_id).strip():
            return {"ok": False, "error": "'root_task_id' is required and cannot be empty"}

        root_task_id = root_task_id.strip()

        with get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM retrospectives WHERE root_task_id = ? LIMIT 1",
                (root_task_id,),
            ).fetchone()

        if not row:
            return {"ok": False, "error": f"no retrospective found for root_task_id '{root_task_id}'"}

        retro = dict(row)
        retro["summary"] = json.loads(retro["summary"]) if isinstance(retro["summary"], str) else retro["summary"]
        retro["bottlenecks"] = json.loads(retro["bottlenecks"]) if isinstance(retro["bottlenecks"], str) else retro["bottlenecks"]
        retro["improvements"] = json.loads(retro["improvements"]) if isinstance(retro["improvements"], str) else retro["improvements"]
        return {"ok": True, "retrospective": retro}
