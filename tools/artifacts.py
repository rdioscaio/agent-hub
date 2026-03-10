"""Artifact tools: publish_artifact, read_artifact."""

import time
import uuid

from hub.audit import audit
from hub.db import get_conn

MAX_ARTIFACT_BYTES = 512 * 1024  # 512 KB hard cap


def publish_artifact(
    name: str,
    content: str,
    task_id: str = "",
    content_type: str = "text/plain",
    published_by: str = "",
) -> dict:
    """Publish a named artifact (text/code/JSON). Content stored in SQLite."""
    args = dict(name=name, task_id=task_id, content_type=content_type, published_by=published_by)
    with audit("publish_artifact", args, task_id):
        if len(content.encode()) > MAX_ARTIFACT_BYTES:
            return {
                "ok": False,
                "error": f"content exceeds {MAX_ARTIFACT_BYTES // 1024} KB limit",
            }
        artifact_id = str(uuid.uuid4())
        now = time.time()
        with get_conn() as conn:
            conn.execute(
                """
                INSERT INTO artifacts
                    (id, name, task_id, content_type, content, published_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    name,
                    task_id or None,
                    content_type,
                    content,
                    published_by or None,
                    now,
                ),
            )
        return {"ok": True, "artifact_id": artifact_id, "name": name}


def read_artifact(artifact_id: str = "", name: str = "") -> dict:
    """Read an artifact by ID or by name (most recent if multiple)."""
    args = dict(artifact_id=artifact_id, name=name)
    with audit("read_artifact", args):
        if not artifact_id and not name:
            return {"ok": False, "error": "provide artifact_id or name"}
        with get_conn() as conn:
            if artifact_id:
                row = conn.execute(
                    "SELECT * FROM artifacts WHERE id = ?", (artifact_id,)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM artifacts WHERE name = ? ORDER BY created_at DESC LIMIT 1",
                    (name,),
                ).fetchone()
        if not row:
            return {"ok": False, "error": "artifact not found"}
        return {"ok": True, "artifact": dict(row)}
