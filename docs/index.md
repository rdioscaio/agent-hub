# Documentation Index

This directory is the canonical entry point for project documentation.

## Start Here
- `README.md`
  - fast onboarding: what the repo is, how to bootstrap it, and where the deeper docs live
- `docs/configuration.md`
  - environment variables, defaults, SSH aliases, and runbook-specific configuration boundaries
- `docs/env-scope-matrix.md`
  - VPS-by-VPS secret scope matrix, static wiring matrix, source-of-truth files, and change policy
- `docs/architecture.md`
  - core layers, data flow, and persistence model
- `docs/mcp-surfaces.md`
  - current MCP entry points and exact tool surfaces
- `docs/sse-deployment.md`
  - explicit bind-safety and deployment contract for `server_sse.py`
- `deploy/caddy/agent-hub-sse.Caddyfile.example`
  - concrete reference proxy config for the SSE surface
- `deploy/systemd/agent-hub-sse.service.example`
  - concrete reference unit file for the SSE process
- `docs/operations.md`
  - startup, health checks, SQLite backup/restore, and operational guardrails
- `.github/workflows/env-audit-advisory.yml`
  - manual GitHub Actions workflow for consolidated advisory env audit
- `docs/development.md`
  - local development flow, test matrix, and how to extend the hub safely

## Historical / Process Documents
- `docs/agent-hub-v3-status.md`
  - historical checkpoint and implementation timeline
  - useful for context, but it is not the source of truth for the current surface
- `docs/engineering-closeout-template.md`
  - closeout template for implementation handoff and review

## Specialized Runbooks
- `tools/n8n/README.md`
  - index for the Nextcloud backup alerting, heartbeat, ntfy, and chain-monitor runbooks

## Documentation Boundary
Use the docs in this order when advancing the project:
1. `README.md`
2. `docs/configuration.md`
3. `docs/env-scope-matrix.md`
   - then run `tools/run_env_audit.py` for recurring advisory audit across scope, wiring, and discovery
   - use `tools/env_scope_checker.py` for v1 scope drill-down
   - use `tools/env_wiring_checker.py` for static wiring drill-down
   - use `tools/env_discovery_checker.py` for advisory discovery of unmapped env-sensitive candidates
4. `docs/architecture.md`
5. `docs/mcp-surfaces.md`
6. `docs/sse-deployment.md`
7. `docs/operations.md`
8. `docs/development.md`
9. feature-specific runbooks such as `tools/n8n/README.md`
