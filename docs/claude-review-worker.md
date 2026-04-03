# Claude Review Worker

This document defines the deterministic review worker for `claude-backend`.

## Goal
- turn a pending `review` task addressed to `claude-backend` into a real Claude run
- keep the loop additive: claim, review, publish artifact, call `record_review`
- avoid pretending a review happened when no reviewer actually consumed the queue

## Entry Point
- [run_claude_review_worker.py](/home/rdios/agent-hub-mcp/scripts/run_claude_review_worker.py)

## What It Does
1. claims the next claimable `review` task for `claude-backend`
2. gathers task, source task, notes, and artifacts from SQLite
3. sends a structured review prompt to the Claude CLI
4. publishes a JSON artifact with the raw Claude result
5. records the verdict with `record_review`

## Scope
- current scope is intentionally narrow: `review` tasks only
- this closes the `claude-backend` review gap without introducing a generic task runner prematurely

## Usage
```bash
cd /home/rdios/agent-hub-mcp
python3 scripts/run_claude_review_worker.py --owner claude-backend
```

Optional filters:
```bash
python3 scripts/run_claude_review_worker.py \
  --owner claude-backend \
  --root-task-id <root-task-id> \
  --requested-agent claude-backend
```

## Operational Notes
- the worker uses the Claude binary from one of:
  - `CLAUDE_BIN`
  - `claude` in `PATH`
  - the installed VS Code Claude Code extension path under the current home directory
- it does not use built-in Claude tools for this review flow; the review context is passed inline from the hub
- if Claude execution fails, the claimed review task is marked `failed`
- if no claimable review task exists, the worker exits successfully with a no-op JSON result

## Current Limitation
- this is a single-run worker
- recurring execution should be provided by a timer or external scheduler
