# Configuration

This project keeps the runtime intentionally small. Most behavior is controlled by a short set of environment variables plus a few code-level allowlists.

## Core Environment Variables

| Variable | Required | Used by | Default | Notes |
|---|---|---|---|---|
| `OPENAI_API_KEY` | only if using GPT tools | `tools/ask_gpt.py`, `delegate_task_to_gpt` | none | Required for `ask_gpt` and GPT fallback execution. |
| `HCLOUD_TOKEN` | optional | `tools/hetzner.py` | empty | Required only for Hetzner inventory tools. |
| `HUB_DB_PATH` | optional | `hub/db.py` | `db/hub.sqlite` | Moves the SQLite file. Directory is created automatically. |
| `REMOTE_EXEC_ENABLED` | optional | `server.py` | unset / false | Enables `remote_exec` on the stdio/admin surface only when set to a truthy value such as `1` or `true`. |
| `MCP_SSE_HOST` | optional | `server_sse.py` | `127.0.0.1` | Bind host for the SSE surface. |
| `MCP_SSE_PORT` | optional | `server_sse.py` | `8100` | Bind port for the SSE surface. |
| `MCP_SSE_ALLOW_PUBLIC_BIND` | optional | `server_sse.py` | unset | Required only if you intentionally bind SSE on a non-loopback interface. |

## Repo Conventions Under `/home/rdios`
- shared user-level environment usually lives in `/home/rdios/.env`
- repo-local defaults live in `.env.example`
- inspect repo `.env` before introducing new config or asking for values already present

Cross-VPS scope boundaries are documented in `docs/env-scope-matrix.md`.
That matrix is the source of truth for deciding whether a variable belongs in:
- a VPS host-shared file
- a domain-specific shared file
- a service-local file
- an app-local file

`tools/env_scope_checker.py` is the frozen v1 baseline for scope placement.

Operational drift can be checked against that same matrix with:
```bash
python3 tools/env_scope_checker.py --vps maincua --report markdown
```

The checker validates only:
- file presence for monitored env files
- variable-name presence and absence
- variable placement by declared scope
- discovered `.env` files inside monitored roots that are outside the matrix

The checker does not validate:
- secret values
- equality or difference between secret values
- systemd `EnvironmentFile` order
- Docker or runtime-specific env-loading semantics

Static wiring drift is checked separately with:
```bash
python3 tools/env_wiring_checker.py --vps maincua --report markdown
```

The wiring checker validates only:
- systemd `EnvironmentFile` declarations and their order for mapped units
- Docker Compose `env_file` declarations for mapped services
- symlink targets for mapped secret pointers
- required static wiring patterns in mapped loader files

The wiring checker does not validate:
- runtime environment inherited by a live process
- whether a restart has already been applied
- secret values
- env-loading semantics outside the mapped static targets

Recurring advisory audit for scope, wiring, and discovery is consolidated in:
```bash
python3 tools/run_env_audit.py --vps maincua --mode advisory --report markdown
```

Runner exit codes:
- `0`: no findings
- `10`: findings found in advisory mode
- `1`: findings found in strict mode
- `2`: execution, transport, schema, or tooling error

This repo's `.env.example` is intentionally minimal. It covers only the variables that are common to normal local startup. Feature-specific runbooks have their own env examples under `tools/n8n/`.

## SQLite Configuration
`hub/db.py` configures SQLite with:
- `journal_mode=WAL`
- `busy_timeout=5000`
- `foreign_keys=ON`

Those are code-level defaults, not environment variables.

## Remote Host Access
`tools/remote.py` is not open-ended. It has two fixed code-level policies:
- host allowlist: `maincua-prod`, `next`, `backup-arm`
- path-prefix allowlist per host alias, with default deny

Those names must resolve through `~/.ssh/config` on the machine running the hub.

Current path-prefix policy in `tools/remote.py`:
- `maincua-prod`: `/home/rdios`, `/etc/caddy`, `/etc/systemd/system`, `/root/agent-hub-sse`, `/tmp`
- `next`: `/home/rdios`, `/etc/agent-hub-mcp`, `/etc/nextcloud-backup`, `/etc/systemd/system`, `/usr/local/sbin`, `/var/backups/nextcloud`, `/root/nc-backup-2026-03-24`, `/tmp`
- `backup-arm`: `/home/rdios`, `/etc/nextcloud-backup`, `/etc/systemd/system`, `/usr/local/sbin`, `/var/backups/nextcloud`, `/root/nc-backup-2026-03-24`, `/tmp`

Current process allowlist in `tools/remote.py` for `remote_run_process`:
- `maincua-prod`: `/usr/bin/systemctl`, `/usr/bin/journalctl`, `/usr/bin/ss`, `/usr/bin/caddy`, `/usr/sbin/ufw`
- `next`: `/usr/bin/systemctl`, `/usr/bin/journalctl`, `/usr/bin/ss`, `/usr/sbin/ufw`
- `backup-arm`: `/usr/bin/systemctl`, `/usr/bin/journalctl`, `/usr/bin/ss`, `/usr/sbin/ufw`

Current unit allowlist in `tools/remote.py` for `systemctl` and `journalctl`:
- `maincua-prod`: `caddy.service`, `ssh.service`
- `next`: `agent-hub-sse.service`, `nextcloud-heartbeat-monitor.service`, `nextcloud-heartbeat-monitor.timer`, `nextcloud-probe.service`, `nextcloud-probe.timer`, `ssh.service`
- `backup-arm`: `nextcloud-alert-chain-probe.service`, `nextcloud-alert-chain-probe.timer`, `nextcloud-whatsapp-chain-probe.service`, `nextcloud-whatsapp-chain-probe.timer`, `ssh.service`

Current argument policy in `tools/remote.py` for `remote_run_process`:
- `systemctl`: only `status`, `is-active`, `show`, with exactly one allowlisted unit
- `journalctl`: requires `-u <unit>` and optionally `-n <N>` where `1 <= N <= 500`
- `ss`: only `-lntp` or `-lntu`
- `ufw`: only `status` or `status numbered`
- `caddy`: only `version`, `list-modules`, or `validate --adapter caddyfile --config <allowlisted absolute path>`

Current remote guardrails in `tools/remote.py`:
- command timeout: `30s`
- stdout/stderr cap: `256 KB`
- write cap: `512 KB`
- remote stat/hash cap: `32 MiB`
- SSH mode: `BatchMode=yes`, `ConnectTimeout=10`
- process execution allowlist is code-level and host-specific
- path validation requires absolute, normalized paths inside the per-host prefix allowlist
- symlink resolution is checked remotely for path-based tools so indirect escapes are rejected

## Hetzner Access
`tools/hetzner.py` uses `HCLOUD_TOKEN` directly from the environment.
There is no separate config file in the repo for Hetzner.

## Feature-Specific Env Files
These are not global repo settings. They belong to their own runbooks.

### Nextcloud / n8n alerting
Examples live under `tools/n8n/`:
- `tools/n8n/notify.env.example`
- `tools/n8n/heartbeat.env.example`
- `tools/n8n/chain-probe.env.example`
- `tools/n8n/whatsapp-chain-probe.env.example`

Use those files only when deploying the alerting/monitoring stack described in `tools/n8n/README.md`.

## What Is Not Configured Here
- `server_sse.py` does not configure bearer auth by itself. External auth/TLS still belongs outside the process even after the public-bind guard.
- remote host allowlist is code-level, not env-driven
- SQLite pragmas are code-level, not env-driven
