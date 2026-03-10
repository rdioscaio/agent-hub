import hashlib
import json
import time
import uuid
from typing import Any

from hub.db import get_conn


class AuditContext:
    def __init__(self, tool_name: str, args: dict[str, Any], task_id: str = ""):
        self.tool_name = tool_name
        self.args_hash = hashlib.sha256(
            json.dumps(args, sort_keys=True, default=str).encode()
        ).hexdigest()[:16]
        self.task_id = task_id
        self.audit_id = str(uuid.uuid4())
        self.start = time.time()

    def __enter__(self) -> "AuditContext":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        status = "error" if exc_type else "ok"
        duration_ms = int((time.time() - self.start) * 1000)
        try:
            with get_conn() as conn:
                conn.execute(
                    """
                    INSERT INTO audit_log
                        (id, timestamp, tool_name, args_hash, task_id, result_status, duration_ms)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self.audit_id,
                        self.start,
                        self.tool_name,
                        self.args_hash,
                        self.task_id,
                        status,
                        duration_ms,
                    ),
                )
        except Exception:
            pass  # audit must never break the caller
        return False  # do not suppress exceptions


def audit(tool_name: str, args: dict[str, Any], task_id: str = "") -> AuditContext:
    return AuditContext(tool_name, args, task_id)
