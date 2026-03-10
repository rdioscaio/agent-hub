"""
Metrics tools: get_metrics (MCP tool) + collect_task_metric (internal helper).

collect_task_metric is called automatically by complete_task and fail_task.
It is NOT an MCP tool.

Duplicate protection:
    Before inserting, checks if a metric already exists for the same
    (task_id, final_status). If so, skips insertion silently.

Aggregate calculations (get_metrics):
    - avg_total_duration_ms: average of total_duration_ms (NULLs excluded)
    - completion_rate: count(final_status='done') / count(total)
    - rework_rate: count(rework_count > 0) / count(total)
    - fallback_rate: count(fallback_used > 0) / count(total)
    All aggregates computed in Python over the filtered result set.
"""

import time
import uuid

from hub.audit import audit
from hub.db import get_conn


# ---------------------------------------------------------------------------
# collect_task_metric (internal helper — NOT an MCP tool)
# ---------------------------------------------------------------------------

def collect_task_metric(task_id: str, final_status: str) -> dict:
    """Collect and store a metric for a completed or failed task.

    Called internally by complete_task/fail_task. Never breaks the caller —
    returns error dict on failure but does NOT raise.

    Duplicate protection: skips if metric for (task_id, final_status) exists.
    """
    try:
        now = time.time()

        with get_conn() as conn:
            # Duplicate protection
            existing = conn.execute(
                "SELECT id FROM task_metrics WHERE task_id = ? AND final_status = ? LIMIT 1",
                (task_id, final_status),
            ).fetchone()
            if existing:
                return {"ok": True, "skipped": True, "reason": "metric already exists"}

            # Fetch task data for metric calculation
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if not row:
                return {"ok": False, "error": f"task '{task_id}' not found for metric collection"}

            task = dict(row)

            # Calculate timings
            created_at = task.get("created_at") or now
            claimed_at = task.get("claimed_at")  # may be NULL
            updated_at = task.get("updated_at") or now

            time_to_claim_ms = None
            time_to_complete_ms = None
            if claimed_at is not None:
                time_to_claim_ms = int((claimed_at - created_at) * 1000)
                time_to_complete_ms = int((now - claimed_at) * 1000)
            total_duration_ms = int((now - created_at) * 1000)

            # Count reworks and fallbacks for this task
            rework_count = conn.execute(
                "SELECT COUNT(*) as cnt FROM tasks WHERE source_task_id = ? AND task_kind = 'rework'",
                (task_id,),
            ).fetchone()["cnt"]

            fallback_exists = conn.execute(
                "SELECT COUNT(*) as cnt FROM tasks WHERE source_task_id = ? AND task_kind = 'fallback'",
                (task_id,),
            ).fetchone()["cnt"]

            metric_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO task_metrics
                    (id, task_id, root_task_id, task_kind, domain, agent, final_status,
                     time_to_claim_ms, time_to_complete_ms, total_duration_ms,
                     review_verdict, rework_count, fallback_used, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    metric_id,
                    task_id,
                    task.get("root_task_id") or None,
                    task.get("task_kind") or None,
                    task.get("domain") or "general",
                    task.get("owner") or None,
                    final_status,
                    time_to_claim_ms,
                    time_to_complete_ms,
                    total_duration_ms,
                    task.get("quality_status") or None,
                    rework_count,
                    1 if fallback_exists > 0 else 0,
                    now,
                ),
            )

        return {"ok": True, "metric_id": metric_id, "task_id": task_id}

    except Exception as exc:
        # Record warning in audit log — never break the caller
        try:
            with audit("collect_task_metric_warning", {
                "task_id": task_id,
                "final_status": final_status,
                "error": str(exc),
            }, task_id):
                pass  # audit context records the warning
        except Exception:
            pass  # absolute last resort — audit itself failed
        return {"ok": False, "error": f"metric collection failed: {exc}"}


# ---------------------------------------------------------------------------
# get_metrics (MCP tool)
# ---------------------------------------------------------------------------

def get_metrics(
    domain: str = "",
    agent: str = "",
    task_kind: str = "",
    root_task_id: str = "",
    limit: int = 50,
) -> dict:
    """Query task metrics with optional filters and computed aggregates.

    Aggregates are computed in Python over the filtered result set:
    - avg_total_duration_ms: mean of total_duration_ms (NULLs excluded)
    - completion_rate: done / total
    - rework_rate: tasks with rework_count > 0 / total
    - fallback_rate: tasks with fallback_used > 0 / total

    Args:
        domain:       Optional. Filter by domain.
        agent:        Optional. Filter by agent.
        task_kind:    Optional. Filter by task kind.
        root_task_id: Optional. Filter by root request.
        limit:        Max results (default 50).
    """
    args = dict(
        domain=domain,
        agent=agent,
        task_kind=task_kind,
        root_task_id=root_task_id,
        limit=limit,
    )
    with audit("get_metrics", args):
        query = "SELECT * FROM task_metrics WHERE 1=1"
        params: list = []

        if domain:
            query += " AND domain = ?"
            params.append(domain.strip())
        if agent:
            query += " AND agent = ?"
            params.append(agent.strip())
        if task_kind:
            query += " AND task_kind = ?"
            params.append(task_kind.strip())
        if root_task_id:
            query += " AND root_task_id = ?"
            params.append(root_task_id.strip())

        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        with get_conn() as conn:
            rows = conn.execute(query, params).fetchall()

        metrics = [dict(row) for row in rows]
        total = len(metrics)

        # Compute aggregates
        aggregates = {
            "avg_total_duration_ms": None,
            "completion_rate": None,
            "rework_rate": None,
            "fallback_rate": None,
        }

        if total > 0:
            durations = [m["total_duration_ms"] for m in metrics if m["total_duration_ms"] is not None]
            if durations:
                aggregates["avg_total_duration_ms"] = round(sum(durations) / len(durations))

            done_count = sum(1 for m in metrics if m["final_status"] == "done")
            aggregates["completion_rate"] = round(done_count / total, 2)

            rework_count = sum(1 for m in metrics if (m.get("rework_count") or 0) > 0)
            aggregates["rework_rate"] = round(rework_count / total, 2)

            fallback_count = sum(1 for m in metrics if (m.get("fallback_used") or 0) > 0)
            aggregates["fallback_rate"] = round(fallback_count / total, 2)

        return {"ok": True, "metrics": metrics, "count": total, "aggregates": aggregates}
