"""
Task lifecycle and orchestration-friendly query tools.

States: pending → claimed → running → blocked → done | failed | canceled
"""

import json
import time
import uuid

from hub.audit import audit
from hub.db import get_conn
from hub.domain import VALID_DOMAINS, classify_domain

VALID_STATES = {"pending", "claimed", "running", "blocked", "done", "failed", "canceled"}
ACTIVE_STATES = {"claimed", "running", "blocked"}
FINAL_STATES = {"done", "failed", "canceled"}


def _parse_depends_on(value) -> list[str]:
    if value in (None, "", []):
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item]
    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return [str(item) for item in parsed if item]
    except (TypeError, json.JSONDecodeError):
        pass
    return []


def _parse_metadata(value) -> dict:
    if value in (None, "", {}):
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    except (TypeError, json.JSONDecodeError):
        pass
    return {}


def _task_from_row(row) -> dict:
    task = dict(row)
    task["depends_on"] = _parse_depends_on(task.get("depends_on"))
    task["metadata"] = _parse_metadata(task.get("metadata"))
    task["is_expired"] = _is_expired(task)
    return task


def _is_expired(task: dict, now: float | None = None) -> bool:
    now = now or time.time()
    if task.get("status") not in ACTIVE_STATES:
        return False
    heartbeat_at = float(task.get("heartbeat_at") or 0)
    ttl = int(task.get("ttl") or 300)
    return heartbeat_at + ttl < now


def _resolve_root_task_id(conn, parent_task_id: str, root_task_id: str) -> str:
    if root_task_id:
        return root_task_id
    if not parent_task_id:
        return ""
    row = conn.execute(
        "SELECT root_task_id FROM tasks WHERE id = ?",
        (parent_task_id,),
    ).fetchone()
    if row and row["root_task_id"]:
        return row["root_task_id"]
    return parent_task_id


def _get_active_profile(conn, agent_name: str) -> dict | None:
    """Fetch active agent profile. Internal helper — NOT an MCP tool."""
    row = conn.execute(
        "SELECT * FROM agent_profiles WHERE agent_name = ? AND active = 1",
        (agent_name,),
    ).fetchone()
    return dict(row) if row else None


def _dependencies_satisfied(conn, depends_on: list[str]) -> bool:
    if not depends_on:
        return True
    placeholders = ",".join("?" for _ in depends_on)
    rows = conn.execute(
        f"SELECT id, status FROM tasks WHERE id IN ({placeholders})",
        depends_on,
    ).fetchall()
    statuses = {row["id"]: row["status"] for row in rows}
    return all(statuses.get(task_id) == "done" for task_id in depends_on)


def create_task(
    title: str,
    description: str = "",
    priority: int = 5,
    owner: str = "",
    ttl: int = 300,
    idempotency_key: str = "",
    parent_task_id: str = "",
    root_task_id: str = "",
    depends_on: list | None = None,
    task_kind: str = "work",
    requested_agent: str = "",
    review_policy: str = "none",
    source_task_id: str = "",
    metadata: dict | None = None,
    domain: str = "",
) -> dict:
    """Create a new task in pending state."""
    depends_on = _parse_depends_on(depends_on)
    metadata = metadata or {}

    # Domain: validate override or auto-classify
    if domain:
        domain = domain.strip().lower()
        if domain not in VALID_DOMAINS:
            return {"ok": False, "error": f"invalid domain '{domain}'. Valid: {', '.join(sorted(VALID_DOMAINS))}"}
    else:
        domain = classify_domain(title, description)

    args = dict(
        title=title,
        description=description,
        priority=priority,
        owner=owner,
        ttl=ttl,
        idempotency_key=idempotency_key,
        parent_task_id=parent_task_id,
        root_task_id=root_task_id,
        depends_on=depends_on,
        task_kind=task_kind,
        requested_agent=requested_agent,
        review_policy=review_policy,
        source_task_id=source_task_id,
        metadata=metadata,
        domain=domain,
    )
    with audit("create_task", args):
        if idempotency_key:
            with get_conn() as conn:
                row = conn.execute(
                    "SELECT * FROM tasks WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if row:
                    return {"ok": True, "idempotent": True, "task": _task_from_row(row)}

        task_id = str(uuid.uuid4())
        now = time.time()
        with get_conn() as conn:
            resolved_root = _resolve_root_task_id(conn, parent_task_id, root_task_id) or task_id
            conn.execute(
                """
                INSERT INTO tasks
                    (
                        id, title, description, status, owner, priority, idempotency_key,
                        ttl, heartbeat_at, created_at, updated_at, parent_task_id, root_task_id,
                        depends_on, task_kind, requested_agent, review_policy, source_task_id,
                        quality_status, metadata, domain
                    )
                VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    task_id,
                    title,
                    description,
                    owner or None,
                    priority,
                    idempotency_key or None,
                    ttl,
                    now,
                    now,
                    now,
                    parent_task_id or None,
                    resolved_root,
                    json.dumps(depends_on),
                    task_kind or "work",
                    requested_agent or "",
                    review_policy or "none",
                    source_task_id or None,
                    json.dumps(metadata),
                    domain,
                ),
            )
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        return {"ok": True, "task_id": task_id, "status": "pending", "task": _task_from_row(row)}


def get_task(task_id: str) -> dict:
    """Fetch a single task by ID."""
    args = dict(task_id=task_id)
    with audit("get_task", args, task_id):
        with get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        if not row:
            return {"ok": False, "error": "task not found"}
        return {"ok": True, "task": _task_from_row(row)}


def claim_task(task_id: str, owner: str) -> dict:
    """Claim a pending or expired active task."""
    args = dict(task_id=task_id, owner=owner)
    with audit("claim_task", args, task_id):
        now = time.time()
        with get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if not row:
                return {"ok": False, "error": "task not found"}

            task = _task_from_row(row)
            if task["owner"] == owner and task["status"] in ACTIVE_STATES:
                conn.execute(
                    "UPDATE tasks SET heartbeat_at=?, updated_at=? WHERE id=?",
                    (now, now, task_id),
                )
                task["heartbeat_at"] = now
                task["updated_at"] = now
                return {"ok": True, "task_id": task_id, "status": task["status"], "renewed": True, "task": task}

            if task["status"] not in {"pending", *ACTIVE_STATES}:
                return {"ok": False, "error": f"task is '{task['status']}', not claimable"}

            if task["status"] == "pending" and not _dependencies_satisfied(conn, task["depends_on"]):
                return {"ok": False, "error": "task dependencies are not done yet"}

            # Set claimed_at only on first claim (pending → claimed), not on reclaim
            result = conn.execute(
                """
                UPDATE tasks
                SET status='claimed', owner=?, heartbeat_at=?, updated_at=?,
                    claimed_at = CASE WHEN status = 'pending' THEN ? ELSE claimed_at END
                WHERE id=?
                  AND (
                        status='pending'
                        OR (
                            status IN ('claimed', 'running', 'blocked')
                            AND COALESCE(heartbeat_at, 0) + COALESCE(ttl, 300) < ?
                        )
                      )
                """,
                (owner, now, now, now, task_id, now),
            )
            if result.rowcount == 0:
                current = conn.execute(
                    "SELECT * FROM tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()
                current_task = _task_from_row(current) if current else None
                error = "task is no longer claimable"
                if current_task:
                    error = f"task is '{current_task['status']}' and owned by '{current_task['owner']}'"
                return {"ok": False, "error": error}

            claimed = conn.execute(
                "SELECT * FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        reclaimed = task["status"] in ACTIVE_STATES and task["is_expired"]
        return {
            "ok": True,
            "task_id": task_id,
            "status": "claimed",
            "reclaimed": reclaimed,
            "previous_owner": task["owner"],
            "task": _task_from_row(claimed),
        }


def claim_next_task(
    owner: str,
    task_kind: str = "",
    root_task_id: str = "",
    requested_agent: str = "",
    limit: int = 100,
) -> dict:
    """Claim the next runnable task for an agent.

    If the owner has an active agent profile, tasks are ranked by a tuple of
    (domain_match, kind_match, priority, -created_at) so the agent prefers
    tasks matching its declared capabilities.  Without a profile, behavior is
    identical to v3.2 (priority-only ordering with LIMIT).
    """
    args = dict(
        owner=owner,
        task_kind=task_kind,
        root_task_id=root_task_id,
        requested_agent=requested_agent,
        limit=limit,
    )
    with audit("claim_next_task", args):
        now = time.time()
        with get_conn() as conn:
            profile = _get_active_profile(conn, owner)

            # With a profile we need the full candidate set for correct
            # ranking.  _MAX_CANDIDATES is a safety cap — NOT a semantic
            # limit.  It exists solely to prevent unbounded queries if the
            # tasks table grows unexpectedly large.  At normal operating
            # volumes (tens to hundreds of active tasks) it has no effect.
            # If a deployment regularly hits this cap, the correct fix is a
            # composite index (status, domain, priority) with a WHERE
            # domain IN (...) clause, not raising the cap blindly.
            query_limit = _MAX_CANDIDATES if profile is not None else limit

            rows = conn.execute(
                """
                SELECT * FROM tasks
                WHERE status IN ('pending', 'claimed', 'running', 'blocked')
                ORDER BY priority DESC, created_at ASC
                LIMIT ?
                """,
                (query_limit,),
            ).fetchall()

            # Build candidate list applying existing filters
            candidates = []
            for row in rows:
                task = _task_from_row(row)
                if root_task_id and task.get("root_task_id") != root_task_id:
                    continue
                if not task_kind and task.get("task_kind") == "request":
                    continue
                if task_kind and task.get("task_kind") != task_kind:
                    continue

                target_agent = requested_agent or owner
                assigned_agent = task.get("requested_agent") or ""
                if assigned_agent and assigned_agent not in {target_agent, owner}:
                    continue

                if task["status"] == "pending" and not _dependencies_satisfied(conn, task["depends_on"]):
                    continue
                if task["status"] in ACTIVE_STATES and not _is_expired(task, now):
                    continue

                candidates.append(task)

            # Apply tuple ranking when a profile exists
            if profile is not None and candidates:
                agent_domains = set(json.loads(profile["domains"] or "[]"))
                agent_kinds = set(json.loads(profile["task_kinds"] or "[]"))

                def _rank(task):
                    domain_match = 1 if (agent_domains and task.get("domain") in agent_domains) else 0
                    kind_match = 1 if (not agent_kinds or task.get("task_kind") in agent_kinds) else 0
                    return (domain_match, kind_match, task.get("priority", 5), -(task.get("created_at") or 0))

                candidates.sort(key=_rank, reverse=True)

            # Try to claim the best candidate
            for task in candidates:
                result = claim_task(task["id"], owner)
                if result["ok"]:
                    return result

        return {"ok": False, "error": "no claimable task found"}


# Safety cap for candidate queries when an agent profile is active.
# See claim_next_task docstring for rationale.
_MAX_CANDIDATES = 1000


def heartbeat_task(task_id: str, owner: str, status: str = "") -> dict:
    """Update heartbeat timestamp to signal task is alive. Optionally change status."""
    args = dict(task_id=task_id, owner=owner, status=status)
    with audit("heartbeat_task", args, task_id):
        if status and status not in VALID_STATES:
            return {"ok": False, "error": f"invalid status '{status}'"}
        now = time.time()
        with get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if not row:
                return {"ok": False, "error": "task not found"}
            task = _task_from_row(row)
            if task["owner"] and task["owner"] != owner:
                return {"ok": False, "error": "task owned by another agent"}
            if status:
                conn.execute(
                    "UPDATE tasks SET heartbeat_at=?, status=?, updated_at=? WHERE id=?",
                    (now, status, now, task_id),
                )
            else:
                conn.execute(
                    "UPDATE tasks SET heartbeat_at=?, updated_at=? WHERE id=?",
                    (now, now, task_id),
                )
        return {"ok": True, "task_id": task_id, "heartbeat_at": now}


def _check_checklist_gate(conn, task_id: str, task_kind: str, domain: str) -> dict | None:
    """Check if a playbook enforcement gate blocks completion.

    Returns None if gate passes (or no gate applies).
    Returns {"ok": False, "error": ...} dict if gate blocks.
    """
    # Only enforce for task kinds that have playbooks
    playbook_row = conn.execute(
        "SELECT enforcement FROM playbooks WHERE task_kind = ? AND domain = ? AND active = 1 "
        "ORDER BY version DESC LIMIT 1",
        (task_kind, domain),
    ).fetchone()
    # Fallback to generic domain
    if not playbook_row and domain != "*":
        playbook_row = conn.execute(
            "SELECT enforcement FROM playbooks WHERE task_kind = ? AND domain = '*' AND active = 1 "
            "ORDER BY version DESC LIMIT 1",
            (task_kind,),
        ).fetchone()

    if not playbook_row:
        return None  # No playbook → no gate

    enforcement = playbook_row["enforcement"] or "advisory"
    if enforcement != "required":
        return None  # Advisory → no gate

    # Gate active: look for a valid checklist note
    notes = conn.execute(
        "SELECT content FROM notes WHERE task_id = ? ORDER BY created_at DESC",
        (task_id,),
    ).fetchall()

    for note in notes:
        content = note["content"] or ""
        if "[CHECKLIST ADVISORY]" not in content:
            continue
        # Parse the JSON payload after the pipe
        pipe_idx = content.find("| {")
        if pipe_idx < 0:
            continue
        try:
            payload = json.loads(content[pipe_idx + 2:])
            score = float(payload.get("score", 0))
            if score < 1.0:
                failed_items = payload.get("failed_items", [])
                reason = f"checklist score {score:.2f} < 1.0"
                _append_gate_note(conn, task_id, reason, task_kind, domain, failed_items)
                return {"ok": False, "error": f"checklist enforcement blocked: {reason}"}
            return None  # Most recent valid checklist passes the gate
        except (json.JSONDecodeError, ValueError, TypeError):
            continue

    # No valid checklist note found at all
    reason = "no checklist validation found"
    _append_gate_note(conn, task_id, reason, task_kind, domain)
    return {"ok": False, "error": f"checklist enforcement blocked: {reason}"}


def _append_gate_note(conn, task_id: str, reason: str, task_kind: str, domain: str, failed_items: list[str] | None = None) -> None:
    """Append a [CHECKLIST GATE] blocked note for observability."""
    import uuid as _uuid
    note_id = str(_uuid.uuid4())
    now = time.time()
    failed_items = failed_items or []
    suffix = f" | failed_items={json.dumps(failed_items, ensure_ascii=False)}" if failed_items else ""
    conn.execute(
        "INSERT INTO notes (id, task_id, author, content, created_at) VALUES (?, ?, ?, ?, ?)",
        (
            note_id,
            task_id,
            "checklist-gate",
            f"[CHECKLIST GATE] blocked | reason={reason}{suffix} | task_kind={task_kind} | domain={domain}",
            now,
        ),
    )


def complete_task(task_id: str, owner: str) -> dict:
    """Mark a task as done."""
    args = dict(task_id=task_id, owner=owner)
    with audit("complete_task", args, task_id):
        now = time.time()
        with get_conn() as conn:
            row = conn.execute(
                "SELECT status, owner, review_policy, task_kind, domain FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if not row:
                return {"ok": False, "error": "task not found"}
            if row["status"] in ("done", "canceled"):
                return {"ok": False, "error": f"task already '{row['status']}'"}
            if row["owner"] and row["owner"] != owner:
                return {"ok": False, "error": "task owned by another agent"}

            # Checklist enforcement gate (opt-in via playbook)
            gate_result = _check_checklist_gate(
                conn, task_id, row["task_kind"] or "work", row["domain"] or "general"
            )
            if gate_result is not None:
                return gate_result

            quality_status = "approved" if row["task_kind"] == "review" else "awaiting_review"
            if row["review_policy"] in ("none", "") or row["task_kind"] in {"review", "request", "synthesize"}:
                quality_status = "approved"
            conn.execute(
                "UPDATE tasks SET status='done', quality_status=?, updated_at=? WHERE id=?",
                (quality_status, now, task_id),
            )
        # Collect metric — lazy import to avoid circular dependency
        try:
            from tools.metrics import collect_task_metric
            collect_task_metric(task_id, "done")
        except Exception as exc:
            from hub.audit import audit as _audit
            with _audit("collect_metric_warning", {"task_id": task_id, "error": str(exc)}, task_id):
                pass
        return {"ok": True, "task_id": task_id, "status": "done"}


def fail_task(task_id: str, owner: str, error_message: str = "") -> dict:
    """Mark a task as failed."""
    args = dict(task_id=task_id, owner=owner, error_message=error_message)
    with audit("fail_task", args, task_id):
        now = time.time()
        with get_conn() as conn:
            row = conn.execute(
                "SELECT status, owner, retry_count FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if not row:
                return {"ok": False, "error": "task not found"}
            if row["status"] in ("done", "canceled"):
                return {"ok": False, "error": f"task already '{row['status']}'"}
            if row["owner"] and row["owner"] != owner:
                return {"ok": False, "error": "task owned by another agent"}
            conn.execute(
                """
                UPDATE tasks
                SET status='failed', quality_status='failed', error_message=?, retry_count=retry_count+1, updated_at=?
                WHERE id=?
                """,
                (error_message, now, task_id),
            )
        # Collect metric — lazy import to avoid circular dependency
        try:
            from tools.metrics import collect_task_metric
            collect_task_metric(task_id, "failed")
        except Exception as exc:
            from hub.audit import audit as _audit
            with _audit("collect_metric_warning", {"task_id": task_id, "error": str(exc)}, task_id):
                pass
        return {"ok": True, "task_id": task_id, "status": "failed", "retry_count": row["retry_count"] + 1}


def list_tasks(
    status: str = "",
    owner: str = "",
    limit: int = 50,
    parent_task_id: str = "",
    root_task_id: str = "",
    requested_agent: str = "",
    task_kind: str = "",
) -> dict:
    """List tasks, optionally filtered by status, owner, tree, agent, and type."""
    args = dict(
        status=status,
        owner=owner,
        limit=limit,
        parent_task_id=parent_task_id,
        root_task_id=root_task_id,
        requested_agent=requested_agent,
        task_kind=task_kind,
    )
    with audit("list_tasks", args):
        if status and status not in VALID_STATES:
            return {"ok": False, "error": f"invalid status '{status}'"}
        query = "SELECT * FROM tasks WHERE 1=1"
        params: list = []
        if status:
            query += " AND status = ?"
            params.append(status)
        if owner:
            query += " AND owner = ?"
            params.append(owner)
        if parent_task_id:
            query += " AND parent_task_id = ?"
            params.append(parent_task_id)
        if root_task_id:
            query += " AND root_task_id = ?"
            params.append(root_task_id)
        if requested_agent:
            query += " AND requested_agent = ?"
            params.append(requested_agent)
        if task_kind:
            query += " AND task_kind = ?"
            params.append(task_kind)
        query += " ORDER BY priority DESC, created_at ASC LIMIT ?"
        params.append(limit)
        with get_conn() as conn:
            rows = conn.execute(query, params).fetchall()
        tasks = [_task_from_row(row) for row in rows]
        return {"ok": True, "tasks": tasks, "count": len(tasks)}
