#!/usr/bin/env python3
"""Claim one review task for a Claude-backed agent and record the verdict."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hub.bootstrap import ensure_ready
from hub.db import get_conn
from tools.artifacts import publish_artifact
from tools.notes import append_note
from tools.orchestration import record_review
from tools.tasks import claim_next_task, fail_task, get_task, heartbeat_task


DEFAULT_MODEL = "sonnet"
DEFAULT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["approve", "revise", "fallback"],
        },
        "feedback": {"type": "string"},
        "confidence": {
            "type": "string",
            "enum": ["low", "medium", "high"],
        },
        "evidence": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 5,
        },
    },
    "required": ["verdict", "feedback"],
    "additionalProperties": False,
}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", default="claude-backend")
    parser.add_argument("--requested-agent", default="")
    parser.add_argument("--root-task-id", default="")
    parser.add_argument("--claude-bin", default=os.environ.get("CLAUDE_BIN", ""))
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--effort", default="low", choices=["low", "medium", "high", "max"])
    parser.add_argument("--max-budget-usd", type=float, default=0.50)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--heartbeat-interval-seconds", type=int, default=60)
    parser.add_argument("--artifact-limit", type=int, default=6)
    parser.add_argument("--note-limit", type=int, default=6)
    parser.add_argument("--context-char-budget", type=int, default=18_000)
    return parser.parse_args(argv)


def _discover_claude_bin(explicit: str = "") -> str:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))

    path_bin = shutil.which("claude")
    if path_bin:
        candidates.append(Path(path_bin))

    home = Path.home()
    candidates.extend(sorted(home.glob(".vscode-server/extensions/anthropic.claude-code-*/resources/native-binary/claude"), reverse=True))
    candidates.extend(sorted(home.glob(".claude/local/**/claude"), reverse=True))

    seen: set[str] = set()
    for candidate in candidates:
        resolved = str(candidate)
        if resolved in seen:
            continue
        seen.add(resolved)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return resolved

    raise FileNotFoundError(
        "Claude binary not found. Set CLAUDE_BIN or install Claude Code so the worker can execute reviews."
    )


def _row_to_task(row) -> dict:
    if row is None:
        return {}
    task = dict(row)
    metadata = task.get("metadata")
    if isinstance(metadata, str) and metadata:
        try:
            task["metadata"] = json.loads(metadata)
        except json.JSONDecodeError:
            task["metadata"] = {}
    return task


def _load_related_context(review_task: dict, artifact_limit: int, note_limit: int) -> dict:
    task_ids = [review_task["id"]]
    if review_task.get("source_task_id"):
        task_ids.append(review_task["source_task_id"])
    if review_task.get("root_task_id"):
        task_ids.append(review_task["root_task_id"])

    dedup_task_ids = []
    for task_id in task_ids:
        if task_id and task_id not in dedup_task_ids:
            dedup_task_ids.append(task_id)

    placeholders = ",".join("?" for _ in dedup_task_ids)
    with get_conn() as conn:
        source_row = None
        if review_task.get("source_task_id"):
            source_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (review_task["source_task_id"],)).fetchone()

        root_row = None
        if review_task.get("root_task_id"):
            root_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (review_task["root_task_id"],)).fetchone()

        artifact_rows = conn.execute(
            f"""
            SELECT id, name, task_id, content_type, content, created_at
            FROM artifacts
            WHERE task_id IN ({placeholders})
            ORDER BY created_at DESC
            LIMIT ?
            """,
            [*dedup_task_ids, artifact_limit],
        ).fetchall()

        note_rows = conn.execute(
            f"""
            SELECT id, task_id, author, content, created_at
            FROM notes
            WHERE task_id IN ({placeholders})
            ORDER BY created_at DESC
            LIMIT ?
            """,
            [*dedup_task_ids, note_limit],
        ).fetchall()

    return {
        "source_task": _row_to_task(source_row),
        "root_task": _row_to_task(root_row),
        "artifacts": [dict(row) for row in artifact_rows],
        "notes": [dict(row) for row in note_rows],
    }


def _trim_block(label: str, text: str, budget: int) -> str:
    if budget <= 0:
        return ""
    clean = (text or "").strip()
    if not clean:
        return ""
    if len(clean) > budget:
        clean = clean[: budget - 32].rstrip() + "\n[truncated]"
    return f"{label}\n{clean}\n"


def _task_summary(task: dict) -> str:
    if not task:
        return ""
    payload = {
        "id": task.get("id"),
        "title": task.get("title"),
        "description": task.get("description"),
        "task_kind": task.get("task_kind"),
        "requested_agent": task.get("requested_agent"),
        "quality_status": task.get("quality_status"),
        "domain": task.get("domain"),
        "source_task_id": task.get("source_task_id"),
        "root_task_id": task.get("root_task_id"),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _build_prompt(review_task: dict, context: dict, char_budget: int) -> str:
    remaining = max(char_budget, 4_000)
    parts: list[str] = []

    intro = (
        "You are the review worker for agent-hub, acting as the agent "
        f"'{review_task.get('requested_agent') or review_task.get('owner') or 'claude-backend'}'.\n"
        "Review the completed source task using only the supplied metadata, notes, and artifacts.\n"
        "Rules:\n"
        "- approve only when the evidence is sufficient and the work meets the stated goal\n"
        "- use revise when evidence is missing, incomplete, or fixable by the same path\n"
        "- use fallback when the current path or assigned agent is the wrong fit\n"
        "- keep feedback direct and actionable\n"
        "- if the evidence is thin, prefer revise over approve\n"
    )
    parts.append(intro)
    remaining -= len(intro)

    for label, payload in (
        ("Review Task:", _task_summary(review_task)),
        ("Source Task:", _task_summary(context.get("source_task") or {})),
        ("Root Task:", _task_summary(context.get("root_task") or {})),
    ):
        block = _trim_block(label, payload, max(0, remaining))
        if block:
            parts.append(block)
            remaining -= len(block)

    if context.get("notes") and remaining > 0:
        note_lines = []
        for note in context["notes"]:
            note_lines.append(
                json.dumps(
                    {
                        "task_id": note.get("task_id"),
                        "author": note.get("author"),
                        "content": note.get("content"),
                    },
                    ensure_ascii=False,
                )
            )
        block = _trim_block("Related Notes:", "\n".join(note_lines), remaining)
        if block:
            parts.append(block)
            remaining -= len(block)

    if context.get("artifacts") and remaining > 0:
        artifact_chunks = []
        per_artifact = max(500, remaining // max(len(context["artifacts"]), 1))
        for artifact in context["artifacts"]:
            content = artifact.get("content") or ""
            if len(content) > per_artifact:
                content = content[: per_artifact - 32].rstrip() + "\n[truncated]"
            artifact_chunks.append(
                json.dumps(
                    {
                        "task_id": artifact.get("task_id"),
                        "name": artifact.get("name"),
                        "content_type": artifact.get("content_type"),
                        "content": content,
                    },
                    ensure_ascii=False,
                )
            )
        block = _trim_block("Related Artifacts:", "\n".join(artifact_chunks), remaining)
        if block:
            parts.append(block)

    return "\n".join(parts).strip()


class _HeartbeatLoop:
    def __init__(self, task_id: str, owner: str, interval_seconds: int) -> None:
        self.task_id = task_id
        self.owner = owner
        self.interval_seconds = max(15, interval_seconds)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> "_HeartbeatLoop":
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._stop.set()
        self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                heartbeat_task(self.task_id, self.owner, status="running")
            except Exception:
                return


def _run_claude(prompt: str, args: argparse.Namespace) -> tuple[dict, dict]:
    claude_bin = _discover_claude_bin(args.claude_bin)
    schema_json = json.dumps(DEFAULT_SCHEMA, ensure_ascii=False)
    cmd = [
        claude_bin,
        "--print",
        "--output-format",
        "json",
        "--model",
        args.model,
        "--effort",
        args.effort,
        "--tools",
        "",
        "--permission-mode",
        "dontAsk",
        "--max-budget-usd",
        f"{args.max_budget_usd:.2f}",
        "--json-schema",
        schema_json,
        prompt,
    ]
    proc = subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=args.timeout_seconds,
        env=os.environ.copy(),
    )
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0:
        message = stderr or stdout or f"Claude exited with code {proc.returncode}"
        raise RuntimeError(message)
    if not stdout:
        raise RuntimeError("Claude returned no stdout payload")

    envelope = json.loads(stdout)
    structured = envelope.get("structured_output")
    if not isinstance(structured, dict):
        raise RuntimeError("Claude output missing structured_output")
    return envelope, structured


def _format_feedback(structured: dict) -> str:
    feedback = (structured.get("feedback") or "").strip()
    if not feedback:
        feedback = "no feedback provided"
    evidence = [str(item).strip() for item in structured.get("evidence") or [] if str(item).strip()]
    if evidence:
        feedback += "\n\nEvidence:\n- " + "\n- ".join(evidence)
    confidence = (structured.get("confidence") or "").strip()
    if confidence:
        feedback += f"\n\nConfidence: {confidence}"
    return feedback


def run_worker(args: argparse.Namespace) -> dict:
    ensure_ready()

    claimed = claim_next_task(
        owner=args.owner,
        task_kind="review",
        root_task_id=args.root_task_id,
        requested_agent=args.requested_agent or args.owner,
    )
    if not claimed.get("ok"):
        if claimed.get("error") == "no claimable task found":
            return {"ok": True, "claimed": False, "reason": "no claimable review task found"}
        raise RuntimeError(claimed.get("error", "failed to claim review task"))

    review_task = claimed["task"]
    review_task_id = review_task["id"]
    if review_task.get("task_kind") != "review":
        fail_task(review_task_id, args.owner, "claimed task is not a review task")
        raise RuntimeError("claimed task is not a review task")
    if not review_task.get("source_task_id"):
        fail_task(review_task_id, args.owner, "review task missing source_task_id")
        raise RuntimeError("review task missing source_task_id")

    source_task = get_task(review_task["source_task_id"])
    if not source_task.get("ok"):
        fail_task(review_task_id, args.owner, "source task not found")
        raise RuntimeError("source task not found")

    heartbeat_task(review_task_id, args.owner, status="running")
    context = _load_related_context(review_task, args.artifact_limit, args.note_limit)
    prompt = _build_prompt(review_task, context, args.context_char_budget)

    try:
        with _HeartbeatLoop(review_task_id, args.owner, args.heartbeat_interval_seconds):
            envelope, structured = _run_claude(prompt, args)
    except Exception as exc:
        append_note(
            f"Claude review worker failed before verdict: {exc}",
            task_id=review_task_id,
            author=args.owner,
        )
        fail_task(review_task_id, args.owner, str(exc))
        raise

    verdict = structured.get("verdict")
    if verdict not in {"approve", "revise", "fallback"}:
        append_note(
            f"Claude returned invalid verdict payload: {json.dumps(structured, ensure_ascii=False)}",
            task_id=review_task_id,
            author=args.owner,
        )
        fail_task(review_task_id, args.owner, "invalid Claude review verdict")
        raise RuntimeError("invalid Claude review verdict")

    artifact_payload = {
        "review_task_id": review_task_id,
        "source_task_id": review_task["source_task_id"],
        "owner": args.owner,
        "model": args.model,
        "structured_output": structured,
        "claude_result": envelope,
    }
    artifact = publish_artifact(
        f"review-{review_task_id}.json",
        json.dumps(artifact_payload, ensure_ascii=False, indent=2),
        task_id=review_task_id,
        content_type="application/json",
        published_by=args.owner,
    )
    append_note(
        f"Claude review artifact published: {artifact['artifact_id']}",
        task_id=review_task_id,
        author=args.owner,
    )

    feedback = _format_feedback(structured)
    review_result = record_review(
        review_task["source_task_id"],
        reviewer=args.owner,
        verdict=verdict,
        feedback=feedback,
    )
    if not review_result.get("ok"):
        fail_task(review_task_id, args.owner, review_result.get("error", "record_review failed"))
        raise RuntimeError(review_result.get("error", "record_review failed"))

    return {
        "ok": True,
        "claimed": True,
        "review_task_id": review_task_id,
        "source_task_id": review_task["source_task_id"],
        "verdict": verdict,
        "quality_status": review_result.get("quality_status"),
        "artifact_id": artifact.get("artifact_id"),
        "session_id": envelope.get("session_id"),
        "cost_usd": envelope.get("total_cost_usd"),
    }


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        result = run_worker(args)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
