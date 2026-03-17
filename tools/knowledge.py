"""
Knowledge tools for curated/project-approved knowledge.

Lifecycle:
    promote_knowledge   -> creates draft
    approve_knowledge   -> draft -> active
    supersede_knowledge -> active -> superseded + creates new active
    deprecate_knowledge -> draft|active -> deprecated
    query_knowledge     -> consult curated knowledge
"""

import json
import sqlite3
import time
import uuid

from hub.audit import audit
from hub.db import get_conn
from hub.domain import VALID_DOMAINS


VALID_KNOWLEDGE_KINDS = frozenset({
    "convention",
    "architecture",
    "pattern",
    "postmortem",
    "guideline",
    "reference",
})

VALID_KNOWLEDGE_SOURCE_TYPES = frozenset({
    "memory",
    "decision",
    "manual",
})

VALID_KNOWLEDGE_STATUSES = frozenset({
    "draft",
    "active",
    "superseded",
    "deprecated",
})

_DOMAIN_ALIASES = {
    "arch": "architecture",
}


def _parse_tags(value) -> list[str]:
    """Parse tags from JSON/list input into a normalized string list."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(tag).strip() for tag in value if str(tag).strip()]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(tag).strip() for tag in parsed if str(tag).strip()]
        except (TypeError, json.JSONDecodeError):
            pass
    return []


def _require(fields: dict[str, str]) -> dict | None:
    """Validate that required string fields are present and non-empty."""
    for field_name, value in fields.items():
        if not value or not str(value).strip():
            return {"ok": False, "error": f"'{field_name}' is required and cannot be empty"}
    return None


def _normalize_domain(domain: str) -> str:
    """Normalize legacy domain aliases to canonical VALID_DOMAINS values."""
    cleaned = str(domain or "").strip().lower()
    return _DOMAIN_ALIASES.get(cleaned, cleaned)


def _normalize_slug(slug: str) -> str:
    """Normalize slugs to a stable, case-insensitive lookup key."""
    return str(slug or "").strip().lower()


def _knowledge_from_row(row) -> dict:
    """Convert a DB row to a response dict with parsed tags."""
    entry = dict(row)
    entry["tags"] = _parse_tags(entry.get("tags"))
    return entry


def _load_source(conn, source_type: str, source_id: str):
    """Fetch and validate the operational source row for a promotion."""
    table = {
        "memory": "memory_entries",
        "decision": "decisions",
    }.get(source_type)
    if not table:
        return None
    return conn.execute(f"SELECT * FROM {table} WHERE id = ? LIMIT 1", (source_id,)).fetchone()


def promote_knowledge(
    slug: str,
    domain: str,
    kind: str,
    title: str,
    content: str,
    source_type: str,
    promoted_by: str,
    source_id: str = "",
    source_task_id: str = "",
    root_task_id: str = "",
    tags: list = None,
) -> dict:
    """Promote operational knowledge into a curated draft entry."""
    args = dict(
        slug=slug,
        domain=domain,
        kind=kind,
        source_type=source_type,
        promoted_by=promoted_by,
        source_id=source_id,
    )
    with audit("promote_knowledge", args, source_task_id or root_task_id):
        error = _require({
            "slug": slug,
            "domain": domain,
            "kind": kind,
            "title": title,
            "content": content,
            "source_type": source_type,
            "promoted_by": promoted_by,
        })
        if error:
            return error

        slug = _normalize_slug(slug)
        domain = _normalize_domain(domain)
        kind = str(kind).strip().lower()
        source_type = str(source_type).strip().lower()

        if domain not in VALID_DOMAINS:
            return {"ok": False, "error": f"invalid domain '{domain}'. Valid: {', '.join(sorted(VALID_DOMAINS))}"}
        if kind not in VALID_KNOWLEDGE_KINDS:
            return {"ok": False, "error": f"invalid kind '{kind}'. Valid: {', '.join(sorted(VALID_KNOWLEDGE_KINDS))}"}
        if source_type not in VALID_KNOWLEDGE_SOURCE_TYPES:
            return {
                "ok": False,
                "error": f"invalid source_type '{source_type}'. Valid: {', '.join(sorted(VALID_KNOWLEDGE_SOURCE_TYPES))}",
            }
        if source_type != "manual" and not str(source_id or "").strip():
            return {"ok": False, "error": "'source_id' is required when source_type is not 'manual'"}

        parsed_tags = _parse_tags(tags)
        knowledge_id = str(uuid.uuid4())
        now = time.time()

        with get_conn() as conn:
            source_row = None
            if source_type != "manual":
                source_row = _load_source(conn, source_type, source_id.strip())
                if not source_row:
                    return {"ok": False, "error": f"{source_type} source '{source_id}' not found"}

            open_row = conn.execute(
                "SELECT id FROM knowledge_entries WHERE slug = ? AND status IN ('draft', 'active') LIMIT 1",
                (slug,),
            ).fetchone()
            if open_row:
                return {
                    "ok": False,
                    "error": f"slug '{slug}' has an open entry (draft or active). "
                             "Use supersede_knowledge to update, or deprecate_knowledge first.",
                }

            version = conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS max_version FROM knowledge_entries WHERE slug = ?",
                (slug,),
            ).fetchone()["max_version"] + 1

            source_payload = dict(source_row) if source_row else {}
            resolved_source_task_id = str(source_task_id or source_payload.get("source_task_id") or "").strip() or None
            resolved_root_task_id = str(root_task_id or source_payload.get("root_task_id") or "").strip() or None

            try:
                conn.execute(
                    """
                    INSERT INTO knowledge_entries
                        (id, slug, version, domain, kind, title, content, status, tags,
                         source_type, source_id, source_task_id, root_task_id, superseded_by,
                         deprecation_reason, promoted_by, reviewed_by, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        knowledge_id,
                        slug,
                        version,
                        domain,
                        kind,
                        title.strip(),
                        content.strip(),
                        "draft",
                        json.dumps(parsed_tags),
                        source_type,
                        source_id.strip() if source_type != "manual" else None,
                        resolved_source_task_id,
                        resolved_root_task_id,
                        None,
                        None,
                        promoted_by.strip(),
                        None,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                conn.rollback()
                return {
                    "ok": False,
                    "error": f"slug '{slug}' has an open entry (draft or active). "
                             "Use supersede_knowledge to update, or deprecate_knowledge first.",
                }

        return {
            "ok": True,
            "knowledge_id": knowledge_id,
            "slug": slug,
            "version": version,
            "status": "draft",
        }


def approve_knowledge(knowledge_id: str, reviewed_by: str) -> dict:
    """Approve a draft knowledge entry, making it active."""
    args = dict(knowledge_id=knowledge_id, reviewed_by=reviewed_by)
    with audit("approve_knowledge", args, knowledge_id):
        error = _require({"knowledge_id": knowledge_id, "reviewed_by": reviewed_by})
        if error:
            return error

        with get_conn() as conn:
            row = conn.execute("SELECT * FROM knowledge_entries WHERE id = ? LIMIT 1", (knowledge_id.strip(),)).fetchone()
            if not row:
                return {"ok": False, "error": f"knowledge entry '{knowledge_id}' not found"}
            if row["status"] != "draft":
                return {"ok": False, "error": f"knowledge entry '{knowledge_id}' is not in draft status"}

            same_author_warning = row["promoted_by"] == reviewed_by.strip()
            conn.execute(
                """
                UPDATE knowledge_entries
                SET status = 'active', reviewed_by = ?, updated_at = ?
                WHERE id = ?
                """,
                (reviewed_by.strip(), time.time(), knowledge_id.strip()),
            )

        result = {
            "ok": True,
            "knowledge_id": knowledge_id.strip(),
            "slug": row["slug"],
            "status": "active",
            "same_author_warning": same_author_warning,
        }
        if same_author_warning:
            result["warning"] = "promoted_by and reviewed_by are the same agent"
        return result


def supersede_knowledge(
    knowledge_id: str,
    updated_by: str,
    new_title: str = "",
    new_content: str = "",
    domain: str = "",
    tags: list = None,
) -> dict:
    """Supersede an active knowledge entry with a new active version."""
    args = dict(
        knowledge_id=knowledge_id,
        updated_by=updated_by,
        has_new_title=bool(str(new_title or "").strip()),
        has_new_content=bool(str(new_content or "").strip()),
        domain=domain,
        tags=tags,
    )
    with audit("supersede_knowledge", args, knowledge_id):
        error = _require({"knowledge_id": knowledge_id, "updated_by": updated_by})
        if error:
            return error
        if not str(new_title or "").strip() and not str(new_content or "").strip():
            return {"ok": False, "error": "at least one of 'new_title' or 'new_content' must be provided"}

        domain_override = ""
        if str(domain or "").strip():
            domain_override = _normalize_domain(domain)
            if domain_override not in VALID_DOMAINS:
                return {
                    "ok": False,
                    "error": f"invalid domain '{domain_override}'. Valid: {', '.join(sorted(VALID_DOMAINS))}",
                }

        new_id = str(uuid.uuid4())
        now = time.time()
        with get_conn() as conn:
            row = conn.execute("SELECT * FROM knowledge_entries WHERE id = ? LIMIT 1", (knowledge_id.strip(),)).fetchone()
            if not row:
                return {"ok": False, "error": f"knowledge entry '{knowledge_id}' not found"}
            if row["status"] != "active":
                return {"ok": False, "error": f"knowledge entry '{knowledge_id}' is not active"}

            row_data = dict(row)
            parsed_tags = _parse_tags(tags) if tags is not None else _parse_tags(row_data.get("tags"))

            try:
                conn.execute(
                    """
                    UPDATE knowledge_entries
                    SET status = 'superseded', superseded_by = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (new_id, now, knowledge_id.strip()),
                )
                conn.execute(
                    """
                    INSERT INTO knowledge_entries
                        (id, slug, version, domain, kind, title, content, status, tags,
                         source_type, source_id, source_task_id, root_task_id, superseded_by,
                         deprecation_reason, promoted_by, reviewed_by, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_id,
                        row_data["slug"],
                        row_data["version"] + 1,
                        domain_override or row_data["domain"],
                        row_data["kind"],
                        str(new_title or "").strip() or row_data["title"],
                        str(new_content or "").strip() or row_data["content"],
                        "active",
                        json.dumps(parsed_tags),
                        row_data["source_type"],
                        row_data["source_id"],
                        row_data["source_task_id"],
                        row_data["root_task_id"],
                        None,
                        None,
                        updated_by.strip(),
                        updated_by.strip(),
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                return {"ok": False, "error": f"failed to supersede knowledge entry: {exc}"}

        return {
            "ok": True,
            "old_id": knowledge_id.strip(),
            "new_id": new_id,
            "slug": row_data["slug"],
            "old_version": row_data["version"],
            "new_version": row_data["version"] + 1,
        }


def deprecate_knowledge(knowledge_id: str, deprecated_by: str, reason: str) -> dict:
    """Deprecate an open knowledge entry without creating a replacement."""
    args = dict(knowledge_id=knowledge_id, deprecated_by=deprecated_by, reason=reason[:80] if reason else "")
    with audit("deprecate_knowledge", args, knowledge_id):
        error = _require({
            "knowledge_id": knowledge_id,
            "deprecated_by": deprecated_by,
            "reason": reason,
        })
        if error:
            return error

        with get_conn() as conn:
            row = conn.execute("SELECT * FROM knowledge_entries WHERE id = ? LIMIT 1", (knowledge_id.strip(),)).fetchone()
            if not row:
                return {"ok": False, "error": f"knowledge entry '{knowledge_id}' not found"}
            if row["status"] not in {"draft", "active"}:
                return {"ok": False, "error": f"knowledge entry '{knowledge_id}' cannot be deprecated from status '{row['status']}'"}

            conn.execute(
                """
                UPDATE knowledge_entries
                SET status = 'deprecated', deprecation_reason = ?, updated_at = ?
                WHERE id = ?
                """,
                (reason.strip(), time.time(), knowledge_id.strip()),
            )

        return {
            "ok": True,
            "knowledge_id": knowledge_id.strip(),
            "slug": row["slug"],
            "status": "deprecated",
        }


def query_knowledge(
    domain: str = "",
    kind: str = "",
    status: str = "active",
    keyword: str = "",
    tags: list = None,
    slug: str = "",
    limit: int = 10,
) -> dict:
    """Query curated knowledge entries.

    Default behavior only returns active entries. Historical states appear only
    when the caller passes an explicit status.
    """
    args = dict(domain=domain, kind=kind, status=status, keyword=keyword, tags=tags, slug=slug, limit=limit)
    with audit("query_knowledge", args):
        if not isinstance(limit, int) or limit < 1:
            return {"ok": False, "error": "'limit' must be an integer >= 1"}

        slug = _normalize_slug(slug)
        normalized_status = str(status or "active").strip().lower()
        normalized_kind = str(kind or "").strip().lower()

        if domain:
            domain = _normalize_domain(domain)
            if domain not in VALID_DOMAINS:
                return {"ok": False, "error": f"invalid domain '{domain}'. Valid: {', '.join(sorted(VALID_DOMAINS))}"}
        if normalized_kind:
            if normalized_kind not in VALID_KNOWLEDGE_KINDS:
                return {
                    "ok": False,
                    "error": f"invalid kind '{normalized_kind}'. Valid: {', '.join(sorted(VALID_KNOWLEDGE_KINDS))}",
                }
        if normalized_status not in VALID_KNOWLEDGE_STATUSES:
            return {
                "ok": False,
                "error": f"invalid status '{normalized_status}'. Valid: {', '.join(sorted(VALID_KNOWLEDGE_STATUSES))}",
            }

        query = "SELECT * FROM knowledge_entries WHERE status = ?"
        params: list = [normalized_status]

        if slug:
            query += " AND slug = ?"
            params.append(slug)
        if domain:
            query += " AND domain = ?"
            params.append(domain)
        if normalized_kind:
            query += " AND kind = ?"
            params.append(normalized_kind)
        if keyword:
            like_pattern = f"%{keyword.strip()}%"
            query += " AND (title LIKE ? COLLATE NOCASE OR content LIKE ? COLLATE NOCASE)"
            params.extend([like_pattern, like_pattern])

        if slug:
            query += " ORDER BY version DESC, updated_at DESC LIMIT ?"
        else:
            query += " ORDER BY updated_at DESC, version DESC LIMIT ?"
        params.append(limit)

        with get_conn() as conn:
            rows = conn.execute(query, params).fetchall()

        requested_tags = _parse_tags(tags)
        results = []
        for row in rows:
            entry = _knowledge_from_row(row)
            if requested_tags and not all(tag in entry["tags"] for tag in requested_tags):
                continue
            results.append(entry)

        return {"ok": True, "knowledge": results, "count": len(results)}
