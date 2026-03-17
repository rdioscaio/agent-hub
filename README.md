# agent-hub-mcp

Single-host MCP hub for multi-agent work orchestration with shared tasks, notes, artifacts, review, and GPT fallback.

The hub is no longer limited to "Claude asks GPT". It now supports:

- `Claude Code` as an MCP client
- `Codex` or any local terminal workflow through `hub_cli.py`
- multiple named agents sharing the same SQLite-backed task graph
- automatic request planning, review, rework, and fallback task creation

The hub still coordinates work only. File editing remains with the agent that claims the task.

---

## Architecture

```text
You
 ├─▶ Claude Code (MCP client)
 ├─▶ Codex / terminal (hub_cli.py)
 └─▶ Other MCP / script clients
        └─▶ agent-hub
              ├─▶ task graph (root request, subtasks, dependencies)
              ├─▶ notes
              ├─▶ artifacts
              ├─▶ locks
              ├─▶ review + rework/fallback
              └─▶ ask_gpt / delegate_task_to_gpt
                        └─▶ OpenAI API
```

Typical flow:

1. `submit_request` turns a natural-language request into a rooted task tree.
2. Agents call `claim_next_task` and work only on tasks assigned to them.
3. Workers publish artifacts and notes.
4. Reviewers call `record_review`.
5. If review fails, the hub creates rework or fallback tasks.
6. A synthesizer task produces the final answer after approved dependencies are done.

---

## Requirements

- Python 3.10+
- `pip install fastmcp openai`

---

## Setup

### 1. Install dependencies

```bash
cd /home/rdios/agent-hub-mcp
pip install fastmcp openai
```

### 2. Set your OpenAI key

```bash
export OPENAI_API_KEY=sk-...
```

Or add it to your shell profile (`~/.bashrc`, `~/.zshrc`).

### 3. Initialize the database

```bash
cd /home/rdios/agent-hub-mcp
python3 -c "from hub.bootstrap import ensure_ready; ensure_ready(); print('Hub ready')"
```

### 4. Verify the server lists tools

```bash
cd /home/rdios/agent-hub-mcp
fastmcp list server.py
```

Expected output now includes 37 tools:

`create_task, get_task, claim_task, claim_next_task, heartbeat_task, complete_task,`
`fail_task, list_tasks, append_note, list_notes, publish_artifact, read_artifact,`
`acquire_lock, release_lock, ask_gpt, delegate_task_to_gpt, submit_request,`
`record_review, list_task_tree, summarize_request, store_memory, recall_memory,`
`record_decision, query_decisions, promote_knowledge, approve_knowledge,`
`supersede_knowledge, deprecate_knowledge, query_knowledge, get_playbook,`
`validate_checklist, get_metrics, register_agent, get_agent_profile, list_agents,`
`generate_retrospective, get_retrospective`

---

## Configure Claude Code

Edit (or create) `~/.claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "agent-hub": {
      "command": "python3",
      "args": ["/home/rdios/agent-hub-mcp/server.py"],
      "env": {
        "OPENAI_API_KEY": "sk-YOUR_KEY_HERE",
        "HUB_DB_PATH": "/home/rdios/agent-hub-mcp/db/hub.sqlite"
      }
    }
  }
}
```

Restart Claude Code. You should see `agent-hub` in the MCP tools list.

---

## Use It From Codex / Terminal

`hub_cli.py` gives you a local entry point to the same hub state without an MCP client.

```bash
cd /home/rdios/agent-hub-mcp

# Submit a natural-language request
python3 hub_cli.py submit "Review src/auth.py and propose a safer JWT flow"

# Claim the next task for Codex
python3 hub_cli.py claim-next codex

# Summarize progress for a root request
python3 hub_cli.py status <root_task_id>

# Query active curated knowledge
python3 hub_cli.py query-knowledge --domain frontend --kind guideline

# Promote and approve curated knowledge from the terminal
python3 hub_cli.py promote-knowledge my-slug general guideline "Title" "Content" manual codex-general
python3 hub_cli.py approve-knowledge <knowledge_id> claude

# Record a review and trigger fallback if needed
python3 hub_cli.py review <task_id> claude fallback --feedback "Need a stronger second pass"

# Let GPT execute a fallback task automatically
python3 hub_cli.py delegate-gpt <task_id>
```

This is the path that lets Claude and Codex share one hub even if only Claude is attached over MCP.

---

## Real Usage Pattern

Recommended role split:

- `codex-planner`: submits or refines the initial plan
- `codex`: primary worker / synthesizer
- `claude`: reviewer or second worker
- `gpt-fallback`: automated fallback execution through `delegate_task_to_gpt`

Example:

```text
User: Review auth.py, propose a safer JWT design, and double-check the answer.

Planner:
1. submit_request(...)

Codex:
2. claim_next_task(owner="codex")
3. inspect files and publish_artifact(...)
4. append_note(...)
5. complete_task(...)

Claude:
6. claim_next_task(owner="claude")
7. review the result
8. record_review(..., verdict="approve" | "revise" | "fallback")

GPT fallback:
9. if fallback task was created, delegate_task_to_gpt(...)

Codex:
10. claim synthesize task
11. prepare final answer
```

---

## Tool Reference

### Core task tools

| Tool | Description |
|---|---|
| `create_task` | Create a task with optional parent, dependencies, agent hint, and metadata |
| `get_task` | Read a single task by ID |
| `claim_task` | Claim a pending task or reclaim an expired active task |
| `claim_next_task` | Claim the next runnable task for an agent |
| `heartbeat_task` | Update heartbeat; optionally change status |
| `complete_task` | Mark task done |
| `fail_task` | Mark task failed and increment retry count |
| `list_tasks` | Query tasks by status, owner, root, parent, agent, or kind |

### Collaboration tools

| Tool | Description |
|---|---|
| `append_note` | Add a note to a task |
| `list_notes` | Read notes |
| `publish_artifact` | Store text/code/JSON artifact (max 512 KB) |
| `read_artifact` | Read artifact by ID or name |
| `acquire_lock` | Exclusive lock on a path with TTL |
| `release_lock` | Release a lock you own |

### Delegation tools

| Tool | Description |
|---|---|
| `ask_gpt` | Ask OpenAI directly with controlled payload rules |
| `delegate_task_to_gpt` | Claim a task, execute it through GPT, publish an artifact, complete it |

### Orchestration tools

| Tool | Description |
|---|---|
| `submit_request` | Convert a natural-language request into a task tree with review and synthesis stages |
| `record_review` | Record approval, revision request, or fallback request and create follow-up tasks |
| `list_task_tree` | Flatten a request tree with depth annotations |
| `summarize_request` | Return progress summary and currently ready tasks |

### Memory and knowledge tools

| Tool | Description |
|---|---|
| `store_memory` | Persist operational facts, patterns, or limitations |
| `recall_memory` | Read operational memory by domain/tags/confidence |
| `record_decision` | Store a structured decision with rationale and alternatives |
| `query_decisions` | Search historical decisions by domain/keyword |
| `promote_knowledge` | Create a curated draft from memory, decision, or manual input |
| `approve_knowledge` | Approve a draft and mark it active |
| `supersede_knowledge` | Replace an active knowledge entry with a new active version |
| `deprecate_knowledge` | Mark a draft/active knowledge entry deprecated |
| `query_knowledge` | Query curated knowledge; default returns only active entries |

### Retrospective tools

| Tool | Description |
|---|---|
| `generate_retrospective` | Persist a deterministic retrospective snapshot for a completed request tree |
| `get_retrospective` | Read a persisted retrospective by `root_task_id` |

### Advisory and observability tools

| Tool | Description |
|---|---|
| `get_playbook` | Return the advisory playbook for a task kind/domain |
| `validate_checklist` | Record checklist scoring as an advisory note |
| `get_metrics` | Query passive performance metrics and aggregates |
| `register_agent` | Upsert an agent capability profile |
| `get_agent_profile` | Read one agent profile |
| `list_agents` | List active/inactive agent profiles, optionally by domain |

---

## Planning Modes

`submit_request` supports three planning modes:

- `auto`: try GPT planning first, fall back to heuristic planning
- `gpt`: require GPT planning
- `heuristic`: local deterministic planning only

If no OpenAI key is available, `auto` falls back to heuristic planning.

---

## Task Kinds

Common task kinds now include:

- `request`
- `work`
- `review`
- `rework`
- `fallback`
- `synthesize`

Dependencies are stored directly on each task, and `claim_next_task` only claims tasks whose dependencies are already `done`.

---

## Review and Fallback

The intended loop is:

1. Worker completes a `work` task.
2. Reviewer claims a `review` task.
3. Reviewer calls `record_review`.
4. If verdict is `revise`, the hub creates a `rework` task.
5. If verdict is `fallback`, the hub creates a `fallback` task, typically assigned to `gpt-fallback`.
6. Synth tasks are updated to depend on new follow-up work automatically.

This gives you a repeatable "second pass" quality loop instead of relying on manual discipline.

---

## Smoke Tests

```bash
cd /home/rdios/agent-hub-mcp
python3 tests/smoke_test.py
```

The smoke test covers:

- task lifecycle
- dependency-aware claiming
- expired-task reclaim
- notes
- artifacts
- locks
- request submission
- review-triggered fallback

---

## Concurrency Notes

- This is a single-host SQLite hub, not a distributed queue.
- SQLite runs in WAL mode with `busy_timeout=5000ms`.
- `claim_task` now reclaims expired active tasks based on heartbeat + TTL.
- `claim_next_task` is the preferred primitive for multiple agents.
- Start with a small number of active agents and scale only after measuring.

---

## Security Notes

- `OPENAI_API_KEY` is read from environment only.
- Audit log records every tool call with args hash, task_id, duration, and status.
- Locks use `os.path.realpath` to prevent duplicate locks through path aliases.
- Artifacts are capped at 512 KB.
- `ask_gpt` payloads are capped at 12,000 characters.
