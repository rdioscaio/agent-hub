"""
Lock tools: acquire_lock, release_lock.

Locks are per real path, have an owner and a TTL.
Orphaned locks (expires_at < now) are reclaimed automatically.
"""

import os
import time
import uuid

from hub.audit import audit
from hub.db import get_conn

DEFAULT_TTL = 300  # seconds


def _normalize(path: str) -> str:
    """Resolve to absolute real path to prevent double-lock on same file."""
    return os.path.realpath(os.path.abspath(path))


def acquire_lock(path: str, owner: str, ttl: int = DEFAULT_TTL) -> dict:
    """
    Acquire an exclusive lock on a path.

    Automatically expires orphaned locks older than their TTL before attempting.
    Returns ok=False if lock is held by another owner and not yet expired.
    """
    args = dict(path=path, owner=owner, ttl=ttl)
    real_path = _normalize(path)
    with audit("acquire_lock", args):
        now = time.time()
        expires_at = now + ttl
        lock_id = str(uuid.uuid4())

        with get_conn() as conn:
            # Expire orphaned locks on this path
            conn.execute(
                "DELETE FROM locks WHERE path = ? AND expires_at < ?",
                (real_path, now),
            )
            existing = conn.execute(
                "SELECT owner, expires_at FROM locks WHERE path = ?", (real_path,)
            ).fetchone()

            if existing:
                if existing["owner"] == owner:
                    # Renew own lock
                    conn.execute(
                        "UPDATE locks SET expires_at=?, acquired_at=? WHERE path=?",
                        (expires_at, now, real_path),
                    )
                    return {"ok": True, "renewed": True, "path": real_path, "expires_at": expires_at}
                return {
                    "ok": False,
                    "error": f"path locked by '{existing['owner']}' until {existing['expires_at']:.0f}",
                    "locked_by": existing["owner"],
                    "expires_at": existing["expires_at"],
                }

            conn.execute(
                "INSERT INTO locks (id, path, owner, acquired_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                (lock_id, real_path, owner, now, expires_at),
            )
        return {"ok": True, "lock_id": lock_id, "path": real_path, "expires_at": expires_at}


def release_lock(path: str, owner: str) -> dict:
    """Release a lock owned by this agent."""
    args = dict(path=path, owner=owner)
    real_path = _normalize(path)
    with audit("release_lock", args):
        with get_conn() as conn:
            row = conn.execute(
                "SELECT owner FROM locks WHERE path = ?", (real_path,)
            ).fetchone()
            if not row:
                return {"ok": False, "error": "no lock found for this path"}
            if row["owner"] != owner:
                return {"ok": False, "error": f"lock owned by '{row['owner']}', cannot release"}
            conn.execute("DELETE FROM locks WHERE path = ? AND owner = ?", (real_path, owner))
        return {"ok": True, "path": real_path, "released": True}
