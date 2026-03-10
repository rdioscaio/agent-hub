"""Note tools: append_note, list_notes."""

import time
import uuid

from hub.audit import audit
from hub.db import get_conn


def append_note(content: str, task_id: str = "", author: str = "") -> dict:
    """Append a note, optionally linked to a task."""
    args = dict(content=content, task_id=task_id, author=author)
    with audit("append_note", args, task_id):
        note_id = str(uuid.uuid4())
        now = time.time()
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO notes (id, task_id, author, content, created_at) VALUES (?, ?, ?, ?, ?)",
                (note_id, task_id or None, author or None, content, now),
            )
        return {"ok": True, "note_id": note_id}


def list_notes(task_id: str = "", limit: int = 50) -> dict:
    """List notes, optionally filtered by task."""
    args = dict(task_id=task_id, limit=limit)
    with audit("list_notes", args, task_id):
        if task_id:
            query = "SELECT * FROM notes WHERE task_id = ? ORDER BY created_at ASC LIMIT ?"
            params = [task_id, limit]
        else:
            query = "SELECT * FROM notes ORDER BY created_at DESC LIMIT ?"
            params = [limit]
        with get_conn() as conn:
            rows = conn.execute(query, params).fetchall()
        return {"ok": True, "notes": [dict(r) for r in rows], "count": len(rows)}
