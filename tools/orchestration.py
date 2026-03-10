"""
High-level orchestration tools for turning a user request into a multi-agent task tree.
"""

import json
import time
from typing import Any

from hub.audit import audit
from hub.db import get_conn
from hub.domain import VALID_DOMAINS, classify_domain
from tools.artifacts import publish_artifact
from tools.ask_gpt import ask_gpt
from tools.notes import append_note
from tools.tasks import (
    claim_task,
    complete_task,
    create_task,
    fail_task,
    get_task,
    heartbeat_task,
    list_tasks,
)

REVIEW_VERDICTS = {"approve", "revise", "fallback"}
PLANNED_TASK_KINDS = {"work", "review", "synthesize"}


def _truncate(text: str, limit: int = 72) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _normalize_plan_item(item: dict[str, Any], fallback_agents: dict[str, str], default_priority: int) -> dict | None:
    key = str(item.get("key") or "").strip()
    title = str(item.get("title") or "").strip()
    description = str(item.get("description") or "").strip()
    task_kind = str(item.get("task_kind") or "work").strip()
    if not key or not title or task_kind not in PLANNED_TASK_KINDS:
        return None
    depends_on = item.get("depends_on") or []
    if not isinstance(depends_on, list):
        depends_on = []
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    metadata = {
        **metadata,
        "fallback_agent": metadata.get("fallback_agent") or fallback_agents["fallback_agent"],
        "reviewer_agent": metadata.get("reviewer_agent") or fallback_agents["reviewer_agent"],
    }
    return {
        "key": key,
        "title": title,
        "description": description or title,
        "task_kind": task_kind,
        "requested_agent": str(item.get("requested_agent") or fallback_agents.get(f"{task_kind}_agent", "")).strip(),
        "depends_on": [str(dep).strip() for dep in depends_on if str(dep).strip()],
        "source_key": str(item.get("source_key") or "").strip(),
        "priority": int(item.get("priority") or default_priority),
        "review_policy": str(item.get("review_policy") or ("required" if task_kind == "work" else "none")).strip(),
        "metadata": metadata,
    }


def _split_request(request: str, max_work_items: int) -> list[str]:
    lines = [line.strip(" -\t") for line in request.splitlines() if line.strip()]
    if len(lines) > 1:
        return lines[:max_work_items]
    chunks = [part.strip() for part in request.replace("\n", " ").split(";") if part.strip()]
    if len(chunks) > 1:
        return chunks[:max_work_items]
    return [request.strip()]


def _heuristic_plan(
    request: str,
    priority: int,
    worker_agent: str,
    reviewer_agent: str,
    synthesizer_agent: str,
    fallback_agent: str,
    max_work_items: int,
) -> dict:
    work_items = _split_request(request, max_work_items)
    tasks = []
    review_keys = []

    for index, item in enumerate(work_items, start=1):
        work_key = f"work_{index}"
        work_title = _truncate(item, 64) or f"Execute part {index}"
        tasks.append(
            {
                "key": work_key,
                "title": f"Execute: {work_title}",
                "description": item,
                "task_kind": "work",
                "requested_agent": worker_agent,
                "depends_on": [],
                "priority": priority,
                "review_policy": "required" if reviewer_agent else "none",
                "metadata": {
                    "fallback_agent": fallback_agent,
                    "reviewer_agent": reviewer_agent,
                },
            }
        )
        if reviewer_agent:
            review_key = f"review_{index}"
            review_keys.append(review_key)
            tasks.append(
                {
                    "key": review_key,
                    "title": f"Review: {work_title}",
                    "description": (
                        "Review the preceding result for gaps, hallucinations, weak reasoning, "
                        "or quality regressions. Approve, request revision, or request fallback."
                    ),
                    "task_kind": "review",
                    "requested_agent": reviewer_agent,
                    "depends_on": [work_key],
                    "source_key": work_key,
                    "priority": priority + 1,
                    "review_policy": "none",
                    "metadata": {
                        "fallback_agent": fallback_agent,
                        "reviewer_agent": reviewer_agent,
                    },
                }
            )

    synth_deps = review_keys or [task["key"] for task in tasks if task["task_kind"] == "work"]
    tasks.append(
        {
            "key": "synthesize",
            "title": f"Synthesize final answer: {_truncate(request, 56)}",
            "description": (
                "Combine approved outputs, review notes, and artifacts into a final user-facing answer. "
                "If any work item remains unresolved, explicitly say so."
            ),
            "task_kind": "synthesize",
            "requested_agent": synthesizer_agent or worker_agent,
            "depends_on": synth_deps,
            "priority": priority,
            "review_policy": "none",
            "metadata": {
                "fallback_agent": fallback_agent,
                "reviewer_agent": reviewer_agent,
            },
        }
    )
    return {
        "strategy": "heuristic",
        "summary": _truncate(request, 120),
        "tasks": tasks,
    }


def _plan_with_gpt(
    request: str,
    priority: int,
    worker_agent: str,
    reviewer_agent: str,
    synthesizer_agent: str,
    fallback_agent: str,
    max_work_items: int,
) -> dict | None:
    question = f"""
Break the following user request into a small task graph for a multi-agent software assistant hub.

Return strict JSON with this shape:
{{
  "summary": "short summary",
  "tasks": [
    {{
      "key": "work_1",
      "title": "short executable task title",
      "description": "clear instructions",
      "task_kind": "work" | "review" | "synthesize",
      "requested_agent": "agent name",
      "depends_on": ["other_key"],
      "source_key": "work key for review tasks only",
      "priority": {priority},
      "review_policy": "required" | "none",
      "metadata": {{"fallback_agent": "{fallback_agent}", "reviewer_agent": "{reviewer_agent}"}}
    }}
  ]
}}

Rules:
- Create at most {max_work_items} work tasks.
- Always create at least one work task.
- If a reviewer exists, create one review task per work task.
- Always create one synthesize task that depends on all reviews, or all work tasks if no reviews exist.
- Use only these agents:
  worker={worker_agent}
  reviewer={reviewer_agent or worker_agent}
  synthesizer={synthesizer_agent or worker_agent}
  fallback={fallback_agent}
- Keep titles short and operational.

User request:
{request}
""".strip()

    result = ask_gpt(
        purpose="plan multi-agent request",
        question=question,
        data_policy="summary_only",
        response_format="json",
        max_tokens=1200,
    )
    if not result.get("ok"):
        return None

    try:
        parsed = json.loads(result["answer"])
    except (TypeError, json.JSONDecodeError):
        return None

    tasks = parsed.get("tasks")
    if not isinstance(tasks, list):
        return None

    defaults = {
        "work_agent": worker_agent,
        "review_agent": reviewer_agent or worker_agent,
        "synthesize_agent": synthesizer_agent or worker_agent,
        "fallback_agent": fallback_agent,
        "reviewer_agent": reviewer_agent or worker_agent,
    }
    normalized = []
    for item in tasks:
        if not isinstance(item, dict):
            continue
        normalized_item = _normalize_plan_item(item, defaults, priority)
        if normalized_item:
            normalized.append(normalized_item)

    if not normalized or not any(item["task_kind"] == "work" for item in normalized):
        return None
    if not any(item["task_kind"] == "synthesize" for item in normalized):
        return None

    return {
        "strategy": "gpt",
        "summary": str(parsed.get("summary") or _truncate(request, 120)),
        "tasks": normalized,
    }


def _generate_plan(
    request: str,
    priority: int,
    planner_mode: str,
    worker_agent: str,
    reviewer_agent: str,
    synthesizer_agent: str,
    fallback_agent: str,
    max_work_items: int,
) -> dict:
    if planner_mode == "gpt":
        planned = _plan_with_gpt(
            request,
            priority,
            worker_agent,
            reviewer_agent,
            synthesizer_agent,
            fallback_agent,
            max_work_items,
        )
        if planned:
            return planned
    if planner_mode == "auto":
        planned = _plan_with_gpt(
            request,
            priority,
            worker_agent,
            reviewer_agent,
            synthesizer_agent,
            fallback_agent,
            max_work_items,
        )
        if planned:
            return planned
    return _heuristic_plan(
        request,
        priority,
        worker_agent,
        reviewer_agent,
        synthesizer_agent,
        fallback_agent,
        max_work_items,
    )


def _append_dependency(task_id: str, dependency_id: str) -> None:
    now = time.time()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT depends_on FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if not row:
            return
        try:
            depends_on = json.loads(row["depends_on"] or "[]")
        except json.JSONDecodeError:
            depends_on = []
        if dependency_id not in depends_on:
            depends_on.append(dependency_id)
            conn.execute(
                "UPDATE tasks SET depends_on=?, updated_at=? WHERE id=?",
                (json.dumps(depends_on), now, task_id),
            )


def _mark_open_review_task(source_task_id: str, reviewer: str, quality_status: str) -> str:
    now = time.time()
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT id FROM tasks
            WHERE source_task_id = ? AND task_kind = 'review' AND status IN ('pending', 'claimed', 'running', 'blocked')
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (source_task_id,),
        ).fetchone()
        if not row:
            return ""
        conn.execute(
            """
            UPDATE tasks
            SET status='done', owner=?, heartbeat_at=?, updated_at=?, quality_status=?
            WHERE id=?
            """,
            (reviewer, now, now, quality_status, row["id"]),
        )
        return row["id"]


def submit_request(
    request: str,
    requested_by: str = "user",
    priority: int = 5,
    planner_mode: str = "auto",
    planner_agent: str = "codex-planner",
    worker_agent: str = "codex",
    reviewer_agent: str = "claude",
    fallback_agent: str = "gpt-fallback",
    synthesizer_agent: str = "codex",
    max_work_items: int = 3,
) -> dict:
    """Turn a natural-language request into a rooted task tree with review and synthesis stages."""
    args = dict(
        requested_by=requested_by,
        priority=priority,
        planner_mode=planner_mode,
        planner_agent=planner_agent,
        worker_agent=worker_agent,
        reviewer_agent=reviewer_agent,
        fallback_agent=fallback_agent,
        synthesizer_agent=synthesizer_agent,
        max_work_items=max_work_items,
        request_preview=_truncate(request, 96),
    )
    with audit("submit_request", args):
        # Classify domain once from the original request text — propagated to all tasks
        request_domain = classify_domain(request, "")

        root = create_task(
            title=f"Request: {_truncate(request, 56)}",
            description=request,
            priority=priority + 1,
            task_kind="request",
            requested_agent="root",
            review_policy="none",
            metadata={
                "requested_by": requested_by,
                "planner_agent": planner_agent,
                "worker_agent": worker_agent,
                "reviewer_agent": reviewer_agent,
                "fallback_agent": fallback_agent,
                "synthesizer_agent": synthesizer_agent,
            },
            domain=request_domain,
        )
        root_task_id = root["task_id"]
        plan = _generate_plan(
            request,
            priority,
            planner_mode,
            worker_agent,
            reviewer_agent,
            synthesizer_agent,
            fallback_agent,
            max_work_items,
        )

        artifact = publish_artifact(
            f"{root_task_id}-plan.json",
            json.dumps(plan, indent=2, ensure_ascii=True),
            task_id=root_task_id,
            content_type="application/json",
            published_by=planner_agent,
        )
        append_note(
            (
                f"{planner_agent} created a {plan['strategy']} execution plan with "
                f"{len(plan['tasks'])} tasks for request '{_truncate(request, 64)}'."
            ),
            task_id=root_task_id,
            author=planner_agent,
        )

        created_task_ids: dict[str, str] = {}
        for item in plan["tasks"]:
            depends_on = [created_task_ids[key] for key in item["depends_on"] if key in created_task_ids]
            source_task_id = created_task_ids.get(item.get("source_key") or "", "")
            created = create_task(
                title=item["title"],
                description=item["description"],
                priority=item["priority"],
                parent_task_id=root_task_id,
                root_task_id=root_task_id,
                depends_on=depends_on,
                task_kind=item["task_kind"],
                requested_agent=item["requested_agent"],
                review_policy=item["review_policy"],
                source_task_id=source_task_id,
                metadata=item["metadata"],
                domain=request_domain,
            )
            created_task_ids[item["key"]] = created["task_id"]

        return {
            "ok": True,
            "root_task_id": root_task_id,
            "plan_strategy": plan["strategy"],
            "plan_artifact_id": artifact.get("artifact_id"),
            "created_tasks": created_task_ids,
            "task_count": len(created_task_ids),
        }


def record_review(
    task_id: str,
    reviewer: str,
    verdict: str,
    feedback: str = "",
    fallback_agent: str = "",
    rework_agent: str = "",
) -> dict:
    """Record a review result and optionally create follow-up fallback/rework tasks."""
    args = dict(
        task_id=task_id,
        reviewer=reviewer,
        verdict=verdict,
        fallback_agent=fallback_agent,
        rework_agent=rework_agent,
    )
    with audit("record_review", args, task_id):
        if verdict not in REVIEW_VERDICTS:
            return {"ok": False, "error": f"invalid verdict '{verdict}'"}

        source = get_task(task_id)
        if not source.get("ok"):
            return source
        task = source["task"]
        if task["status"] != "done":
            return {"ok": False, "error": "task must be done before review"}

        metadata = task.get("metadata") or {}
        fallback_agent = fallback_agent or metadata.get("fallback_agent") or "gpt-fallback"
        rework_agent = rework_agent or task.get("requested_agent") or "codex"
        reviewer_agent = metadata.get("reviewer_agent") or reviewer
        quality_status = {
            "approve": "approved",
            "revise": "needs_revision",
            "fallback": "fallback_requested",
        }[verdict]

        append_note(
            f"Review by {reviewer} [{verdict}]: {feedback or 'no extra feedback provided'}",
            task_id=task_id,
            author=reviewer,
        )
        review_task_id = _mark_open_review_task(task_id, reviewer, quality_status)

        with get_conn() as conn:
            conn.execute(
                "UPDATE tasks SET quality_status=?, updated_at=strftime('%s','now') WHERE id=?",
                (quality_status, task_id),
            )

        followup = None
        followup_review = None
        if verdict in {"revise", "fallback"}:
            # Resolve domain from source task: valid domain > re-classify > "general"
            source_domain = task.get("domain") or ""
            if source_domain not in VALID_DOMAINS or not source_domain:
                source_domain = classify_domain(task.get("title", ""), task.get("description", ""))

            target_agent = rework_agent if verdict == "revise" else fallback_agent
            followup_kind = "rework" if verdict == "revise" else "fallback"
            followup_desc = (
                f"Follow up on task '{task['title']}'. Address this review feedback directly: {feedback}. "
                f"Original task description: {task['description']}"
            )
            followup = create_task(
                title=f"{followup_kind.title()}: {_truncate(task['title'], 56)}",
                description=followup_desc,
                priority=int(task.get("priority") or 5) + 1,
                parent_task_id=task.get("parent_task_id") or task["root_task_id"],
                root_task_id=task["root_task_id"],
                depends_on=[task_id],
                task_kind=followup_kind,
                requested_agent=target_agent,
                review_policy="required" if reviewer_agent else "none",
                source_task_id=task_id,
                metadata={
                    **metadata,
                    "triggered_by_review": True,
                    "review_feedback": feedback,
                    "fallback_agent": fallback_agent,
                    "reviewer_agent": reviewer_agent,
                },
                domain=source_domain,
            )

            synths = list_tasks(root_task_id=task["root_task_id"], task_kind="synthesize", limit=50)
            for synth in synths.get("tasks", []):
                _append_dependency(synth["id"], followup["task_id"])

            if reviewer_agent:
                followup_review = create_task(
                    title=f"Review {followup_kind}: {_truncate(task['title'], 52)}",
                    description=(
                        f"Review the {followup_kind} output for task '{task['title']}'. "
                        "Approve only if the review feedback has been fully addressed."
                    ),
                    priority=int(task.get("priority") or 5) + 2,
                    parent_task_id=task.get("parent_task_id") or task["root_task_id"],
                    root_task_id=task["root_task_id"],
                    depends_on=[followup["task_id"]],
                    task_kind="review",
                    requested_agent=reviewer_agent,
                    review_policy="none",
                    source_task_id=followup["task_id"],
                    metadata={
                        "fallback_agent": fallback_agent,
                        "reviewer_agent": reviewer_agent,
                    },
                    domain=source_domain,
                )
                for synth in synths.get("tasks", []):
                    _append_dependency(synth["id"], followup_review["task_id"])

        return {
            "ok": True,
            "task_id": task_id,
            "review_task_id": review_task_id,
            "quality_status": quality_status,
            "followup_task_id": followup and followup["task_id"],
            "followup_review_task_id": followup_review and followup_review["task_id"],
        }


def list_task_tree(root_task_id: str) -> dict:
    """List all tasks in a request tree as a flattened depth-annotated view."""
    args = dict(root_task_id=root_task_id)
    with audit("list_task_tree", args, root_task_id):
        root = get_task(root_task_id)
        if not root.get("ok"):
            return root
        listed = list_tasks(root_task_id=root_task_id, limit=500)
        tasks = listed.get("tasks", [])
        children: dict[str, list[dict]] = {}
        for task in tasks:
            parent_id = task.get("parent_task_id") or ""
            children.setdefault(parent_id, []).append(task)
        for group in children.values():
            group.sort(key=lambda item: (-int(item.get("priority") or 0), float(item.get("created_at") or 0)))

        flattened = []

        def walk(task: dict, depth: int) -> None:
            flattened.append(
                {
                    **task,
                    "depth": depth,
                }
            )
            for child in children.get(task["id"], []):
                walk(child, depth + 1)

        walk(root["task"], 0)
        summary = {"total": len(flattened), "by_status": {}, "by_quality": {}}
        for task in flattened:
            summary["by_status"][task["status"]] = summary["by_status"].get(task["status"], 0) + 1
            quality = task.get("quality_status") or "pending"
            summary["by_quality"][quality] = summary["by_quality"].get(quality, 0) + 1

        return {"ok": True, "root_task_id": root_task_id, "tasks": flattened, "summary": summary}


def summarize_request(root_task_id: str) -> dict:
    """Return a compact progress summary for a request tree."""
    args = dict(root_task_id=root_task_id)
    with audit("summarize_request", args, root_task_id):
        tree = list_task_tree(root_task_id)
        if not tree.get("ok"):
            return tree
        tasks = tree["tasks"]
        ready = []
        for task in tasks:
            if task["id"] == root_task_id:
                continue
            if task["status"] != "pending":
                continue
            if all(
                next((candidate for candidate in tasks if candidate["id"] == dep), {"status": ""})["status"] == "done"
                for dep in task.get("depends_on", [])
            ):
                ready.append(task)
        return {
            "ok": True,
            "root_task_id": root_task_id,
            "summary": tree["summary"],
            "ready_tasks": [
                {
                    "id": task["id"],
                    "title": task["title"],
                    "task_kind": task.get("task_kind"),
                    "requested_agent": task.get("requested_agent"),
                }
                for task in ready
            ],
        }


def delegate_task_to_gpt(
    task_id: str,
    owner: str = "gpt-fallback",
    data_policy: str = "snippets",
    model: str = "gpt-4o",
) -> dict:
    """Claim a task, ask GPT to execute it, publish the answer as an artifact, and complete the task."""
    args = dict(task_id=task_id, owner=owner, data_policy=data_policy, model=model)
    with audit("delegate_task_to_gpt", args, task_id):
        claimed = claim_task(task_id, owner)
        if not claimed.get("ok"):
            return claimed

        heartbeat_task(task_id, owner, status="running")
        task_result = get_task(task_id)
        if not task_result.get("ok"):
            return task_result
        task = task_result["task"]

        with get_conn() as conn:
            artifact_rows = conn.execute(
                """
                SELECT id FROM artifacts
                WHERE task_id IN (?, ?, ?)
                ORDER BY created_at DESC
                LIMIT 6
                """,
                (task_id, task.get("source_task_id"), task.get("root_task_id")),
            ).fetchall()
            note_rows = conn.execute(
                """
                SELECT id FROM notes
                WHERE task_id IN (?, ?, ?)
                ORDER BY created_at DESC
                LIMIT 6
                """,
                (task_id, task.get("source_task_id"), task.get("root_task_id")),
            ).fetchall()

        context_refs = [row["id"] for row in artifact_rows] + [row["id"] for row in note_rows]
        question = (
            "Execute the following agent-hub task and produce a directly usable result.\n\n"
            f"Task title: {task['title']}\n"
            f"Task kind: {task.get('task_kind')}\n"
            f"Description:\n{task['description']}\n\n"
            "Use any provided context to improve accuracy. If this is a fallback or rework task, "
            "explicitly address the review feedback from the notes or artifacts."
        )
        response = ask_gpt(
            purpose=f"execute task {task_id}",
            question=question,
            data_policy=data_policy,
            context_refs=context_refs,
            max_tokens=1600,
            task_id=task_id,
            model=model,
        )
        if not response.get("ok"):
            fail_task(task_id, owner, response.get("error", "ask_gpt failed"))
            return response

        artifact_name = f"{task.get('task_kind') or 'task'}-{task_id}.md"
        artifact = publish_artifact(
            artifact_name,
            response["answer"],
            task_id=task_id,
            content_type="text/markdown",
            published_by=owner,
        )
        append_note(
            f"{owner} completed the task via {model} and published artifact '{artifact_name}'.",
            task_id=task_id,
            author=owner,
        )
        complete_task(task_id, owner)
        return {
            "ok": True,
            "task_id": task_id,
            "artifact_id": artifact.get("artifact_id"),
            "model": response["model"],
            "usage": response["usage"],
        }
