"""
Agent profile tools: register_agent, get_agent_profile, list_agents.

Agent profiles declare capabilities per agent (domains, task_kinds) and are
used by claim_next_task for domain-aware preferential matching.

Profiles are ADVISORY — they influence task ordering, never block claims.
An agent without a profile gets legacy behavior (priority-only ordering).
An agent with domains=[] is a registered generalist (no domain bonus,
but task_kinds and active status are respected).
"""

import json
import time
import uuid

from hub.audit import audit
from hub.db import get_conn
from hub.domain import VALID_DOMAINS

VALID_TASK_KINDS = frozenset({"work", "review", "rework", "fallback", "synthesize"})


def register_agent(
    agent_name: str,
    domains: list,
    task_kinds: list | None = None,
    max_concurrent: int = 3,
    active: int = 1,
) -> dict:
    """Create or update an agent profile (upsert by agent_name).

    Args:
        agent_name:     Required. Unique agent identifier.
        domains:        Required. List of domains (subset of VALID_DOMAINS).
                        Empty list = generalist agent.
        task_kinds:     Optional. List of accepted task kinds. Empty = all.
        max_concurrent: Optional. Advisory limit (not enforced this release).
                        Must be >= 1.
        active:         Optional. 1 = active, 0 = deactivated.
    """
    task_kinds = task_kinds if task_kinds is not None else []

    args = dict(
        agent_name=agent_name,
        domains=domains,
        task_kinds=task_kinds,
        max_concurrent=max_concurrent,
        active=active,
    )
    with audit("register_agent", args):
        # --- Validations ---
        if not agent_name or not agent_name.strip():
            return {"ok": False, "error": "agent_name is required"}
        agent_name = agent_name.strip()

        if not isinstance(domains, list):
            return {"ok": False, "error": "domains must be a list"}
        for d in domains:
            d_clean = str(d).strip().lower()
            if d_clean not in VALID_DOMAINS:
                return {"ok": False, "error": f"invalid domain '{d}' in domains. Valid: {', '.join(sorted(VALID_DOMAINS))}"}

        if not isinstance(task_kinds, list):
            return {"ok": False, "error": "task_kinds must be a list"}
        for tk in task_kinds:
            tk_clean = str(tk).strip().lower()
            if tk_clean not in VALID_TASK_KINDS:
                return {"ok": False, "error": f"invalid task_kind '{tk}' in task_kinds. Valid: {', '.join(sorted(VALID_TASK_KINDS))}"}

        if not isinstance(max_concurrent, int) or max_concurrent < 1:
            return {"ok": False, "error": "max_concurrent must be >= 1"}

        if active not in (0, 1):
            return {"ok": False, "error": "active must be 0 or 1"}

        # Normalize
        domains_clean = [str(d).strip().lower() for d in domains]
        kinds_clean = [str(tk).strip().lower() for tk in task_kinds]

        now = time.time()
        with get_conn() as conn:
            existing = conn.execute(
                "SELECT id FROM agent_profiles WHERE agent_name = ?",
                (agent_name,),
            ).fetchone()

            if existing:
                conn.execute(
                    """UPDATE agent_profiles
                       SET domains = ?, task_kinds = ?, max_concurrent = ?,
                           active = ?, updated_at = ?
                       WHERE agent_name = ?""",
                    (json.dumps(domains_clean), json.dumps(kinds_clean),
                     max_concurrent, active, now, agent_name),
                )
                created = False
            else:
                profile_id = str(uuid.uuid4())
                conn.execute(
                    """INSERT INTO agent_profiles
                           (id, agent_name, domains, task_kinds,
                            max_concurrent, active, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (profile_id, agent_name, json.dumps(domains_clean),
                     json.dumps(kinds_clean), max_concurrent, active, now, now),
                )
                created = True

            row = conn.execute(
                "SELECT * FROM agent_profiles WHERE agent_name = ?",
                (agent_name,),
            ).fetchone()

        profile = _profile_from_row(row)
        return {"ok": True, "agent_name": agent_name, "created": created, "profile": profile}


def get_agent_profile(agent_name: str) -> dict:
    """Fetch an agent profile by name.

    Args:
        agent_name: Required. The agent identifier.
    """
    args = dict(agent_name=agent_name)
    with audit("get_agent_profile", args):
        if not agent_name or not agent_name.strip():
            return {"ok": False, "error": "agent_name is required"}
        agent_name = agent_name.strip()

        with get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM agent_profiles WHERE agent_name = ?",
                (agent_name,),
            ).fetchone()

        if not row:
            return {"ok": False, "error": f"agent profile '{agent_name}' not found"}
        return {"ok": True, "profile": _profile_from_row(row)}


def list_agents(
    domain: str = "",
    active_only: bool = True,
) -> dict:
    """List agent profiles, optionally filtered by domain and active status.

    Args:
        domain:      Optional. Filter by domain capability. Must be in
                     VALID_DOMAINS if provided. Generalist agents (domains=[])
                     are NOT included when filtering by domain.
        active_only: Optional. Default True. If False, include inactive agents.
    """
    args = dict(domain=domain, active_only=active_only)
    with audit("list_agents", args):
        if domain:
            domain = domain.strip().lower()
            if domain not in VALID_DOMAINS:
                return {"ok": False, "error": f"invalid domain '{domain}'. Valid: {', '.join(sorted(VALID_DOMAINS))}"}

        query = "SELECT * FROM agent_profiles WHERE 1=1"
        params: list = []

        if active_only:
            query += " AND active = 1"

        query += " ORDER BY agent_name ASC"

        with get_conn() as conn:
            rows = conn.execute(query, params).fetchall()

        agents = [_profile_from_row(row) for row in rows]

        if domain:
            agents = [a for a in agents if domain in a["domains"]]

        return {"ok": True, "agents": agents, "count": len(agents)}


# ---------------------------------------------------------------------------
# Internal helpers — NOT MCP tools
# ---------------------------------------------------------------------------

def _profile_from_row(row) -> dict:
    """Convert a sqlite3.Row to a plain dict with parsed JSON fields."""
    profile = dict(row)
    profile["domains"] = _parse_json_list(profile.get("domains"))
    profile["task_kinds"] = _parse_json_list(profile.get("task_kinds"))
    return profile


def _parse_json_list(value) -> list:
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except (TypeError, json.JSONDecodeError):
        return []
