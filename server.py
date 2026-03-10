"""
agent-hub-mcp — MCP server for shared multi-agent coordination.

Tools exposed:
  Tasks    : create_task, get_task, claim_task, claim_next_task, heartbeat_task,
             complete_task, fail_task, list_tasks
  Notes    : append_note, list_notes
  Artifacts: publish_artifact, read_artifact
  Locks    : acquire_lock, release_lock
  Delegate : ask_gpt, delegate_task_to_gpt
  Orchestr.: submit_request, record_review, list_task_tree, summarize_request
  Memory   : store_memory, recall_memory, record_decision, query_decisions
  Playbooks: get_playbook, validate_checklist
  Metrics  : get_metrics
"""

import sys
from pathlib import Path

# Make sure hub/ and tools/ are importable when server.py is the entry point
sys.path.insert(0, str(Path(__file__).parent))

from fastmcp import FastMCP

from tools.artifacts import publish_artifact, read_artifact
from tools.ask_gpt import ask_gpt
from tools.locks import acquire_lock, release_lock
from tools.memory import query_decisions, recall_memory, record_decision, store_memory
from tools.metrics import get_metrics
from tools.notes import append_note, list_notes
from tools.playbooks import get_playbook, validate_checklist
from tools.orchestration import (
    delegate_task_to_gpt,
    list_task_tree,
    record_review,
    submit_request,
    summarize_request,
)
from tools.tasks import (
    claim_next_task,
    claim_task,
    complete_task,
    create_task,
    fail_task,
    get_task,
    heartbeat_task,
    list_tasks,
)

mcp = FastMCP("agent-hub")

# --- Tasks ---
mcp.tool()(create_task)
mcp.tool()(get_task)
mcp.tool()(claim_task)
mcp.tool()(claim_next_task)
mcp.tool()(heartbeat_task)
mcp.tool()(complete_task)
mcp.tool()(fail_task)
mcp.tool()(list_tasks)

# --- Notes ---
mcp.tool()(append_note)
mcp.tool()(list_notes)

# --- Artifacts ---
mcp.tool()(publish_artifact)
mcp.tool()(read_artifact)

# --- Locks ---
mcp.tool()(acquire_lock)
mcp.tool()(release_lock)

# --- Delegation ---
mcp.tool()(ask_gpt)
mcp.tool()(delegate_task_to_gpt)

# --- Memory ---
mcp.tool()(store_memory)
mcp.tool()(recall_memory)
mcp.tool()(record_decision)
mcp.tool()(query_decisions)

# --- Playbooks ---
mcp.tool()(get_playbook)
mcp.tool()(validate_checklist)

# --- Metrics ---
mcp.tool()(get_metrics)

# --- Orchestration ---
mcp.tool()(submit_request)
mcp.tool()(record_review)
mcp.tool()(list_task_tree)
mcp.tool()(summarize_request)

if __name__ == "__main__":
    from hub.bootstrap import ensure_ready
    ensure_ready()
    mcp.run()
