"""
Functional bootstrap for agent-hub-mcp.

Separates structural startup (db schema/migrations) from functional startup
(data seeds, integrity checks). This is the single entry point for any
execution mode — CLI, module import, or test harness.

ensure_ready() is idempotent but NOT silent on failure: if structural or
functional bootstrap fails, the exception propagates. A partially
initialized hub is worse than a crash.
"""

from hub.db import init_db


def ensure_ready() -> None:
    """Bootstrap the hub: schema + migrations + functional seeds.

    Idempotent — safe to call multiple times.
    NOT silent — raises on failure. A broken startup must be visible.
    """
    # Structural: schema, migrations, indexes
    init_db()

    # Functional: default playbooks seed
    from tools.playbooks import seed_default_playbooks, upgrade_default_playbooks
    seed_default_playbooks()
    upgrade_default_playbooks()
