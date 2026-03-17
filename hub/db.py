import os
import sqlite3
from pathlib import Path

DB_PATH = os.environ.get(
    "HUB_DB_PATH",
    str(Path(__file__).parent.parent / "db" / "hub.sqlite"),
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id                TEXT PRIMARY KEY,
    title             TEXT NOT NULL,
    description       TEXT DEFAULT '',
    status            TEXT DEFAULT 'pending',
    owner             TEXT,
    priority          INTEGER DEFAULT 5,
    idempotency_key   TEXT UNIQUE,
    retry_count       INTEGER DEFAULT 0,
    error_message     TEXT,
    heartbeat_at      REAL,
    ttl               INTEGER DEFAULT 300,
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS locks (
    id          TEXT PRIMARY KEY,
    path        TEXT UNIQUE NOT NULL,
    owner       TEXT NOT NULL,
    acquired_at REAL NOT NULL,
    expires_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS notes (
    id         TEXT PRIMARY KEY,
    task_id    TEXT,
    author     TEXT,
    content    TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    task_id      TEXT,
    content_type TEXT DEFAULT 'text/plain',
    content      TEXT NOT NULL,
    published_by TEXT,
    created_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id            TEXT PRIMARY KEY,
    timestamp     REAL NOT NULL,
    caller        TEXT,
    tool_name     TEXT NOT NULL,
    args_hash     TEXT,
    task_id       TEXT,
    result_status TEXT,
    duration_ms   INTEGER
);

CREATE TABLE IF NOT EXISTS memory_entries (
    id              TEXT PRIMARY KEY,
    domain          TEXT NOT NULL,
    tags            TEXT NOT NULL DEFAULT '[]',
    content         TEXT NOT NULL,
    source_task_id  TEXT,
    author          TEXT NOT NULL,
    confidence      REAL NOT NULL DEFAULT 1.0,
    superseded_by   TEXT,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    id              TEXT PRIMARY KEY,
    domain          TEXT NOT NULL,
    question        TEXT NOT NULL,
    decision        TEXT NOT NULL,
    rationale       TEXT NOT NULL,
    alternatives    TEXT NOT NULL DEFAULT '[]',
    outcome         TEXT,
    source_task_id  TEXT,
    root_task_id    TEXT,
    decided_by      TEXT NOT NULL,
    reviewed_by     TEXT,
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS playbooks (
    id          TEXT PRIMARY KEY,
    task_kind   TEXT NOT NULL,
    domain      TEXT NOT NULL DEFAULT '*',
    steps       TEXT NOT NULL DEFAULT '[]',
    checklist   TEXT NOT NULL DEFAULT '[]',
    enforcement TEXT NOT NULL DEFAULT 'advisory',
    version     INTEGER NOT NULL DEFAULT 1,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS task_metrics (
    id                  TEXT PRIMARY KEY,
    task_id             TEXT NOT NULL,
    root_task_id        TEXT,
    task_kind           TEXT,
    domain              TEXT,
    agent               TEXT,
    final_status        TEXT NOT NULL,
    time_to_claim_ms    INTEGER,
    time_to_complete_ms INTEGER,
    total_duration_ms   INTEGER,
    review_verdict      TEXT,
    rework_count        INTEGER NOT NULL DEFAULT 0,
    fallback_used       INTEGER NOT NULL DEFAULT 0,
    created_at          REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS retrospectives (
    id              TEXT PRIMARY KEY,
    root_task_id    TEXT NOT NULL,
    summary         TEXT NOT NULL,
    bottlenecks     TEXT NOT NULL DEFAULT '[]',
    improvements    TEXT NOT NULL DEFAULT '[]',
    domain          TEXT,
    generated_by    TEXT,
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_profiles (
    id             TEXT PRIMARY KEY,
    agent_name     TEXT UNIQUE NOT NULL,
    domains        TEXT NOT NULL DEFAULT '[]',
    task_kinds     TEXT NOT NULL DEFAULT '[]',
    max_concurrent INTEGER NOT NULL DEFAULT 3,
    active         INTEGER NOT NULL DEFAULT 1,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_entries (
    id                 TEXT PRIMARY KEY,
    slug               TEXT NOT NULL,
    version            INTEGER NOT NULL DEFAULT 1,
    domain             TEXT NOT NULL,
    kind               TEXT NOT NULL,
    title              TEXT NOT NULL,
    content            TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'draft',
    tags               TEXT NOT NULL DEFAULT '[]',
    source_type        TEXT NOT NULL,
    source_id          TEXT,
    source_task_id     TEXT,
    root_task_id       TEXT,
    superseded_by      TEXT,
    deprecation_reason TEXT,
    promoted_by        TEXT NOT NULL,
    reviewed_by        TEXT,
    created_at         REAL NOT NULL,
    updated_at         REAL NOT NULL,
    UNIQUE(slug, version)
);
"""

_TASK_MIGRATIONS = {
    "parent_task_id": "TEXT",
    "root_task_id": "TEXT",
    "depends_on": "TEXT DEFAULT ''",
    "task_kind": "TEXT DEFAULT 'work'",
    "requested_agent": "TEXT DEFAULT ''",
    "review_policy": "TEXT DEFAULT 'none'",
    "source_task_id": "TEXT",
    "quality_status": "TEXT DEFAULT 'pending'",
    "metadata": "TEXT DEFAULT '{}'",
    "claimed_at": "REAL",
    "domain": "TEXT DEFAULT 'general'",
}

_PLAYBOOK_MIGRATIONS = {
    "enforcement": "TEXT NOT NULL DEFAULT 'advisory'",
}


def _ensure_playbook_columns(conn: sqlite3.Connection) -> None:
    columns = _table_columns(conn, "playbooks")
    for name, definition in _PLAYBOOK_MIGRATIONS.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE playbooks ADD COLUMN {name} {definition}")
            # One-shot: set enforcement='required' for work/automation on first migration only
            if name == "enforcement":
                conn.execute(
                    """
                    UPDATE playbooks
                    SET enforcement = 'required'
                    WHERE task_kind = 'work' AND domain = 'automation' AND active = 1
                    """
                )


_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_tasks_root_created ON tasks(root_task_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_parent_created ON tasks(parent_task_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_status_priority ON tasks(status, priority DESC, created_at ASC)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_requested_agent_status ON tasks(requested_agent, status)",
    # F1: Memory
    "CREATE INDEX IF NOT EXISTS idx_memory_domain_updated ON memory_entries(domain, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_memory_source_task ON memory_entries(source_task_id)",
    "CREATE INDEX IF NOT EXISTS idx_decisions_domain_created ON decisions(domain, created_at DESC)",
    # F2: Playbooks
    "CREATE INDEX IF NOT EXISTS idx_playbooks_kind_domain ON playbooks(task_kind, domain)",
    # F3: Metrics
    "CREATE INDEX IF NOT EXISTS idx_metrics_domain_created ON task_metrics(domain, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_metrics_agent_created ON task_metrics(agent, created_at DESC)",
    # Agent Profiles
    "CREATE INDEX IF NOT EXISTS idx_agent_profiles_name_active ON agent_profiles(agent_name, active)",
    # Knowledge
    "CREATE INDEX IF NOT EXISTS idx_knowledge_domain_status ON knowledge_entries(domain, status)",
    "CREATE INDEX IF NOT EXISTS idx_knowledge_kind_status ON knowledge_entries(kind, status)",
    "CREATE INDEX IF NOT EXISTS idx_knowledge_slug_status ON knowledge_entries(slug, status)",
    "CREATE INDEX IF NOT EXISTS idx_knowledge_source ON knowledge_entries(source_type, source_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_knowledge_one_open_per_slug "
    "ON knowledge_entries(slug) WHERE status IN ('draft', 'active')",
    # Retrospectives
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_retrospectives_root ON retrospectives(root_task_id)",
)


def get_conn() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row["name"] for row in rows}


def _ensure_task_columns(conn: sqlite3.Connection) -> None:
    columns = _table_columns(conn, "tasks")
    for name, definition in _TASK_MIGRATIONS.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")

    conn.execute(
        """
        UPDATE tasks
        SET
            root_task_id = CASE
                WHEN root_task_id IS NULL OR root_task_id = '' THEN id
                ELSE root_task_id
            END,
            depends_on = COALESCE(depends_on, ''),
            task_kind = COALESCE(NULLIF(task_kind, ''), 'work'),
            requested_agent = COALESCE(requested_agent, ''),
            review_policy = COALESCE(NULLIF(review_policy, ''), 'none'),
            quality_status = COALESCE(NULLIF(quality_status, ''), 'pending'),
            metadata = COALESCE(NULLIF(metadata, ''), '{}')
        """
    )


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(_SCHEMA)
        _ensure_task_columns(conn)
        _ensure_playbook_columns(conn)
        for statement in _INDEXES:
            conn.execute(statement)
