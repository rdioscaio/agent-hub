"""
Memory tools: store_memory, recall_memory, record_decision, query_decisions.

L2 (memory_entries): Facts, patterns, conventions, limitations.
L3 (decisions): Structured decision records with rationale and alternatives.

Ordering policy (recall_memory):
    Results are ordered by updated_at DESC, then confidence DESC.
    This prioritizes recently updated entries first, breaking ties by
    confidence level. Superseded entries are excluded by default.

Keyword search policy (query_decisions):
    Searches across question, decision, and rationale fields.
    Case-insensitive. Partial match (SQL LIKE %keyword%).
"""

import json
import time
import uuid

from hub.audit import audit
from hub.db import get_conn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_tags(value) -> list[str]:
    """Parse tags from various input formats into a clean list of strings."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(t).strip() for t in value if str(t).strip()]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(t).strip() for t in parsed if str(t).strip()]
        except (TypeError, json.JSONDecodeError):
            pass
    return []


def _parse_alternatives(value) -> list[str]:
    """Parse alternatives from various input formats into a list of strings."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(a).strip() for a in value if str(a).strip()]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(a).strip() for a in parsed if str(a).strip()]
        except (TypeError, json.JSONDecodeError):
            pass
    return []


def _require(fields: dict[str, str]) -> dict | None:
    """Validate that all required fields are non-empty strings.

    Returns an error dict if any field is missing/empty, or None if all valid.
    """
    for field_name, value in fields.items():
        if not value or not str(value).strip():
            return {"ok": False, "error": f"'{field_name}' is required and cannot be empty"}
    return None


# ---------------------------------------------------------------------------
# store_memory
# ---------------------------------------------------------------------------

def store_memory(
    domain: str,
    content: str,
    author: str,
    tags: list = None,
    source_task_id: str = "",
    confidence: float = 1.0,
    supersedes: str = "",
) -> dict:
    """Store a memory entry (fact, pattern, convention, limitation).

    Args:
        domain:         Required. Category: 'backend', 'frontend', 'infra', 'arch', 'process'.
        content:        Required. The knowledge to persist.
        author:         Required. Who is recording this memory.
        tags:           Optional. List of keyword tags for filtering.
        source_task_id: Optional. Task that originated this learning.
        confidence:     Optional. 0.0 to 1.0 (default 1.0).
        supersedes:     Optional. ID of memory_entry this one replaces.
    """
    args = dict(
        domain=domain,
        content=content[:80] if content else "",
        author=author,
        tags=tags,
        source_task_id=source_task_id,
        confidence=confidence,
        supersedes=supersedes,
    )
    with audit("store_memory", args):
        # Validate required fields
        error = _require({"domain": domain, "content": content, "author": author})
        if error:
            return error

        # Validate confidence range
        if not isinstance(confidence, (int, float)) or confidence < 0.0 or confidence > 1.0:
            return {"ok": False, "error": "confidence must be a number between 0.0 and 1.0"}

        parsed_tags = _parse_tags(tags)
        memory_id = str(uuid.uuid4())
        now = time.time()

        with get_conn() as conn:
            # If superseding, update the old entry
            superseded_id = ""
            if supersedes:
                old = conn.execute(
                    "SELECT id FROM memory_entries WHERE id = ?",
                    (supersedes,),
                ).fetchone()
                if not old:
                    return {"ok": False, "error": f"supersedes target '{supersedes}' not found"}
                conn.execute(
                    "UPDATE memory_entries SET superseded_by = ?, confidence = 0.0, updated_at = ? WHERE id = ?",
                    (memory_id, now, supersedes),
                )
                superseded_id = supersedes

            conn.execute(
                """
                INSERT INTO memory_entries
                    (id, domain, tags, content, source_task_id, author, confidence,
                     superseded_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    memory_id,
                    domain.strip(),
                    json.dumps(parsed_tags),
                    content,
                    source_task_id or None,
                    author.strip(),
                    confidence,
                    now,
                    now,
                ),
            )

        return {
            "ok": True,
            "memory_id": memory_id,
            "domain": domain.strip(),
            "superseded": superseded_id,
        }


# ---------------------------------------------------------------------------
# recall_memory
# ---------------------------------------------------------------------------

def recall_memory(
    domain: str = "",
    tags: list = None,
    limit: int = 10,
    min_confidence: float = 0.3,
    include_superseded: bool = False,
) -> dict:
    """Recall memory entries by domain and/or tags.

    Ordering: updated_at DESC, then confidence DESC.
    Tag filtering: intersection (entry must contain ALL requested tags).
    Superseded entries are excluded by default.

    Args:
        domain:             Optional. Filter by domain.
        tags:               Optional. Filter by tag intersection.
        limit:              Max results (default 10).
        min_confidence:     Ignore entries below this threshold (default 0.3).
        include_superseded: If True, include entries that have been superseded.
    """
    args = dict(
        domain=domain,
        tags=tags,
        limit=limit,
        min_confidence=min_confidence,
        include_superseded=include_superseded,
    )
    with audit("recall_memory", args):
        query = "SELECT * FROM memory_entries WHERE confidence >= ?"
        params: list = [min_confidence]

        if not include_superseded:
            query += " AND superseded_by IS NULL"

        if domain:
            query += " AND domain = ?"
            params.append(domain.strip())

        query += " ORDER BY updated_at DESC, confidence DESC LIMIT ?"
        params.append(limit)

        with get_conn() as conn:
            rows = conn.execute(query, params).fetchall()

        # Tag intersection filtering (in Python, consistent with hub patterns)
        requested_tags = _parse_tags(tags)
        results = []
        for row in rows:
            entry = dict(row)
            entry_tags = _parse_tags(entry.get("tags"))
            if requested_tags:
                if not all(tag in entry_tags for tag in requested_tags):
                    continue
            entry["tags"] = entry_tags
            results.append(entry)

        return {"ok": True, "memories": results, "count": len(results)}


# ---------------------------------------------------------------------------
# record_decision
# ---------------------------------------------------------------------------

def record_decision(
    domain: str,
    question: str,
    decision: str,
    rationale: str,
    decided_by: str,
    alternatives: list = None,
    source_task_id: str = "",
    root_task_id: str = "",
    reviewed_by: str = "",
) -> dict:
    """Record a structured decision with rationale and alternatives.

    Args:
        domain:         Required. Category: 'backend', 'frontend', 'infra', 'arch', 'process'.
        question:       Required. The question that was decided. E.g. 'Which ORM to use?'
        decision:       Required. The choice made. E.g. 'Prisma'.
        rationale:      Required. Why this choice was made.
        decided_by:     Required. Agent that made the decision.
        alternatives:   Optional. List of alternatives considered.
        source_task_id: Optional. Task where decision was made.
        root_task_id:   Optional. Root request for context.
        reviewed_by:    Optional. Agent that validated the decision.
    """
    args = dict(
        domain=domain,
        question=question[:80] if question else "",
        decision=decision[:80] if decision else "",
        decided_by=decided_by,
    )
    with audit("record_decision", args):
        # Validate required fields
        error = _require({
            "domain": domain,
            "question": question,
            "decision": decision,
            "rationale": rationale,
            "decided_by": decided_by,
        })
        if error:
            return error

        parsed_alternatives = _parse_alternatives(alternatives)
        decision_id = str(uuid.uuid4())
        now = time.time()

        with get_conn() as conn:
            conn.execute(
                """
                INSERT INTO decisions
                    (id, domain, question, decision, rationale, alternatives,
                     outcome, source_task_id, root_task_id, decided_by, reviewed_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    domain.strip(),
                    question.strip(),
                    decision.strip(),
                    rationale.strip(),
                    json.dumps(parsed_alternatives),
                    source_task_id or None,
                    root_task_id or None,
                    decided_by.strip(),
                    reviewed_by.strip() or None,
                    now,
                ),
            )

        return {"ok": True, "decision_id": decision_id, "domain": domain.strip()}


# ---------------------------------------------------------------------------
# query_decisions
# ---------------------------------------------------------------------------

def query_decisions(
    domain: str = "",
    keyword: str = "",
    limit: int = 5,
) -> dict:
    """Query past decisions by domain and/or keyword.

    Keyword search: case-insensitive partial match across question, decision,
    and rationale fields (SQL LIKE %keyword%).
    Results ordered by created_at DESC (most recent first).

    Args:
        domain:  Optional. Filter by domain.
        keyword: Optional. Search in question, decision, rationale.
        limit:   Max results (default 5).
    """
    args = dict(domain=domain, keyword=keyword, limit=limit)
    with audit("query_decisions", args):
        query = "SELECT * FROM decisions WHERE 1=1"
        params: list = []

        if domain:
            query += " AND domain = ?"
            params.append(domain.strip())

        if keyword:
            query += (
                " AND (question LIKE ? COLLATE NOCASE"
                " OR decision LIKE ? COLLATE NOCASE"
                " OR rationale LIKE ? COLLATE NOCASE)"
            )
            like_pattern = f"%{keyword.strip()}%"
            params.extend([like_pattern, like_pattern, like_pattern])

        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        with get_conn() as conn:
            rows = conn.execute(query, params).fetchall()

        results = []
        for row in rows:
            entry = dict(row)
            entry["alternatives"] = _parse_alternatives(entry.get("alternatives"))
            results.append(entry)

        return {"ok": True, "decisions": results, "count": len(results)}
