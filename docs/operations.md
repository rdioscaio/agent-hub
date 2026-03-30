# Operations

## Bootstrap
Initialize or migrate the SQLite database and seed functional defaults:

```bash
python3 -c "from hub.bootstrap import ensure_ready; ensure_ready(); print('hub ready')"
```

Use this before:
- running `server.py`
- running `server_sse.py`
- running `hub_cli.py`
- running tests in a fresh environment

## Start Commands

### stdio MCP server
```bash
python3 server.py
```

This is the default safe mode:
- `remote_run_process` is available
- `remote_exec` is not registered

### stdio MCP server with exceptional `remote_exec`
```bash
REMOTE_EXEC_ENABLED=1 python3 server.py
```

Use this only for exceptional administrative cases that are not covered by `remote_run_process`.

Expected startup evidence:
- `server.py` emits a `server_startup` JSON log line on startup
- the log line includes:
  - `surface`
  - effective `remote_exec_enabled`
  - `timestamp`
  - `version` when it can be resolved

### SSE MCP server
```bash
MCP_SSE_HOST=127.0.0.1 MCP_SSE_PORT=8100 python3 server_sse.py
```

### local CLI
```bash
python3 hub_cli.py --help
```

## Health Checks
Minimum checks that prove the hub is alive enough to work on:

```bash
python3 -c "from hub.bootstrap import ensure_ready; ensure_ready(); print('hub ready')"
fastmcp list server.py
python3 tests/smoke_test.py
```

For env-scope drift by VPS, run:
```bash
python3 tools/env_scope_checker.py --vps hub --report markdown
python3 tools/env_scope_checker.py --vps next --report markdown
python3 tools/env_scope_checker.py --vps maincua --report markdown
```

For the recurring advisory audit that combines scope and wiring, run:
```bash
python3 tools/run_env_audit.py --vps hub --mode advisory --report markdown
python3 tools/run_env_audit.py --vps next --mode advisory --report markdown
python3 tools/run_env_audit.py --vps maincua --mode advisory --report markdown
```

For a full fleet advisory pass:
```bash
python3 tools/run_env_audit.py --vps all --mode advisory --report markdown
```

For manual CI advisory, use the GitHub Actions workflow `.github/workflows/env-audit-advisory.yml`.

To generate a JSON artifact for manual CI or operator review:
```bash
python3 tools/run_env_audit.py \
  --vps all \
  --mode advisory \
  --report json \
  --output-path /tmp/agent-hub-env-audit.json
```

Runner exit codes:
- `0`: no findings
- `10`: advisory findings detected
- `1`: strict findings detected
- `2`: checker execution or schema error

Advisory CI/manual policy:
- treat `10` as warning/report, not as deploy blocker
- treat `2` as real failure
- switch to strict or gate only after repeated advisory runs prove signal quality

Workflow requirements:
- repository secret `ENV_AUDIT_SSH_PRIVATE_KEY` must contain the SSH private key used for advisory audit access
- the workflow runs on a GitHub-hosted runner and audits `HUB VPS` by SSH override because `/home/rdios/...` does not exist on the hosted runner
- `HUB VPS`, `NEXT VPS`, and `MAINCUA VPS` must accept the audit key for user `rdios`
- the workflow pins SSH host keys in `.github/workflows/env-audit-advisory.yml`; do not replace that with runtime `ssh-keyscan`

Pinned SSH host key rotation:
1. On each target host, read the active ED25519 host key and fingerprint:
```bash
sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
sudo cat /etc/ssh/ssh_host_ed25519_key.pub
```
2. Compare the fingerprint with the current pinned entry in `.github/workflows/env-audit-advisory.yml`.
3. Update the `known_hosts` block in `.github/workflows/env-audit-advisory.yml` only for the host that rotated.
4. Validate locally:
```bash
python3 -m unittest tests.test_env_audit_access tests.test_env_scope_checker tests.test_env_wiring_checker tests.test_run_env_audit tests.test_env_audit_workflow
python3 tests/smoke_test.py
```
5. Publish the workflow change to `main`.
6. Rerun `Env Audit Advisory` for `hub` if the rotated host is `HUB VPS`; otherwise rerun `all` to prove the fleet still authenticates cleanly.

Rotation guardrails:
- never use `.pub` user keys from `/home/rdios/.ssh/`; pin only `/etc/ssh/ssh_host_ed25519_key.pub` from the target VPS
- never reintroduce `ssh-keyscan` in CI just to avoid updating the pinned key
- treat unexpected host key change as an infrastructure event first; verify the target before publishing a new key
- keep the pin to `ssh-ed25519` unless there is an explicit operational reason to expand accepted host key types

Review ownership and SLA:
- default review owner is the operator who triggered the workflow or manual command
- `exit_code=2` must be triaged the same day because it means the control itself is broken
- `exit_code=10` must be reviewed within 1 business day
- if the live state changed intentionally, update the matrix/spec in the same change set
- if the matrix/spec is still correct, fix the environment or service wiring instead of rewriting the contract

For a full fleet pass:
```bash
python3 tools/env_scope_checker.py --vps all --report markdown
```

This reads `docs/env-scope-matrix.md` and uses the embedded checker spec.
Remote checks depend on the SSH aliases declared there, currently `next` and `maincua-prod`.

What v1 validates:
- listed env file exists when the matrix says it must exist
- required variable names exist in the listed file
- forbidden or out-of-scope variable names are flagged
- undeclared variable names are flagged in strict files
- discovered `.env` files inside monitored roots but outside the matrix are flagged as `UNKNOWN_PATH`

What v1 does not validate:
- secret values
- equality or drift of secret values
- systemd `EnvironmentFile` order
- runtime-specific env inheritance

Policy for findings:
- `MISSING_ALLOWED_ENTRY`: either restore the file/entry or update the matrix if the contract changed
- `FORBIDDEN_IN_SCOPE`: move or delete the variable from the wrong scope; do not auto-rewrite yet
- `UNDECLARED_VARIABLE`: either declare the variable in the matrix or remove it from the file
- `UNKNOWN_PATH`: either add the file to the matrix with explicit scope or move it outside monitored roots

`tools/env_scope_checker.py` is the frozen v1 baseline for scope placement. Extend it incrementally; do not overload it with wiring or runtime concerns.

For static service wiring by VPS, run:
```bash
python3 tools/env_wiring_checker.py --vps hub --report markdown
python3 tools/env_wiring_checker.py --vps next --report markdown
python3 tools/env_wiring_checker.py --vps maincua --report markdown
```

For a full fleet wiring pass:
```bash
python3 tools/env_wiring_checker.py --vps all --report markdown
```

This also reads `docs/env-scope-matrix.md`, but from the embedded wiring spec.

What the static wiring checker validates:
- mapped systemd `EnvironmentFile` declarations and their order
- mapped Docker Compose `env_file` declarations and their order
- mapped symlink targets such as `/opt/secrets/maincua.env`
- mapped loader files that must contain specific static path-wiring patterns

What the static wiring checker does not validate:
- runtime environment of a live process
- whether systemd or containers were restarted after a file change
- secret values
- unmapped services or loader paths outside the wiring spec

Policy for wiring findings:
- `MISSING_ENVIRONMENT_FILE`: restore the declared file reference or update the wiring spec if the contract changed
- `UNEXPECTED_ENVIRONMENT_FILE_ORDER`: fix the declared order before restart or deploy
- `UNEXPECTED_SYMLINK_TARGET`: repoint the symlink to the documented target
- `SERVICE_NOT_MAPPED`: either restore the mapped service/compose entry or update the wiring spec
- `WIRING_PATH_MISMATCH`: fix the declared path, source unit path, or static loader pattern

Handling flow for advisory audit findings:
- if the runner returns `10`, read the consolidated report first, then drill into `env_scope_checker` or `env_wiring_checker` only where needed
- if the finding reflects intended change, update the matrix/spec before changing the service again
- if the finding reflects drift, fix the file or service wiring first and rerun the runner
- if the exception is temporary, record owner and expiry in the operational notes before accepting it

If you are using the SSE surface, also verify the bind parameters you expect:
```bash
MCP_SSE_HOST=127.0.0.1 MCP_SSE_PORT=8100 python3 server_sse.py
```
Then test it through the transport you actually expose.

If you intentionally need a non-loopback bind, the process now requires explicit opt-in:
```bash
MCP_SSE_HOST=0.0.0.0 MCP_SSE_PORT=8100 MCP_SSE_ALLOW_PUBLIC_BIND=1 python3 server_sse.py
```

Recommended deployment still keeps SSE on loopback and puts auth/TLS in front of it.
See `docs/sse-deployment.md`.
Concrete reference artifacts live in:
- `deploy/systemd/agent-hub-sse.service.example`
- `deploy/caddy/agent-hub-sse.Caddyfile.example`

## SQLite Backup
The database uses WAL mode. Prefer a SQLite-aware backup instead of copying only the main file while writers are active.

### live logical backup
```bash
sqlite3 "${HUB_DB_PATH:-db/hub.sqlite}" ".backup 'db/hub.backup.sqlite'"
```

### cold file backup
Stop writers first, then copy:
- `db/hub.sqlite`
- `db/hub.sqlite-wal` if present
- `db/hub.sqlite-shm` if present

## SQLite Restore
Conservative restore sequence:
1. stop all writers
2. restore the backup file to the target DB path
3. start the process again
4. run `ensure_ready()`
5. run `python3 tests/smoke_test.py`

Example:
```bash
cp db/hub.backup.sqlite db/hub.sqlite
python3 -c "from hub.bootstrap import ensure_ready; ensure_ready(); print('hub ready')"
python3 tests/smoke_test.py
```

## Remote Tool Guardrails
`tools/remote.py` has fixed operational caps:
- host allowlist only: `maincua-prod`, `next`, `backup-arm`
- path-prefix allowlist per host alias, with default deny
- process allowlist per host alias for `remote_run_process`
- per-binary argument policy for `remote_run_process`
- SSH batch mode only
- command timeout: `30s`
- read output cap: `256 KB`
- write cap: `512 KB`
- remote stat supports `include_hash=true` for regular files only
- remote stat hashing is capped at `32 MiB`
- remote writes use atomic replace on the target host
- remote writes can enforce `expected_sha256_before` as a precondition
- remote writes create a point-in-time backup by default when replacing an existing file
- remote writes now rely on `python3` being available on the remote host

Operational implication:
- if a remote host is missing from `~/.ssh/config`, the tool will fail even if DNS would otherwise resolve
- if a host is not in the allowlist, it will be rejected by design
- if a path is absolute but outside the configured prefix policy for that host, it will be rejected by design
- `remote_run_process` is the preferred path for controlled remote execution; it runs allowlisted binaries via a remote Python bridge that calls `subprocess.run(..., shell=False)`
- `remote_run_process` now enforces explicit argv policy by binary:
  - `systemctl`: only `status`, `is-active`, `show`, with one allowlisted unit
  - `journalctl`: requires `-u <unit>` and optional `-n <N>` with `N <= 500`
  - `ss`: only `-lntp` or `-lntu`
  - `ufw`: only `status` or `status numbered`
  - `caddy`: only read-only inspection forms, currently `version`, `list-modules`, and `validate --adapter caddyfile --config <allowlisted-path>`
- `remote_run_process` and `remote_exec` are already distinguishable in audit by tool name; `remote_exec` is additionally marked as `execution_mode=escape_hatch` while `remote_run_process` is marked as `execution_mode=structured_process`
- when a `remote_exec` command exactly matches the structured policy, it now emits an extra audit event `remote_exec_structured_candidate` and returns a migration hint to prefer `remote_run_process`
- `remote_exec` is no longer exposed on `server_sse.py`
- `remote_exec` is now disabled by default on `server.py`; enable it explicitly with `REMOTE_EXEC_ENABLED=1`
- lexical prefix matching is normalized and path-traversal-safe; `/etc/caddy-malicioso` does not match `/etc/caddy`
- symlink resolution is enforced remotely for path-based tools so indirect escape outside the allowlist is rejected
- `remote_stat_file` uses `lstat`; symlinks are reported as `kind=symlink` and are never hashed
- `remote_exec` remains the exceptional escape hatch; it only validates `working_dir` and still accepts arbitrary shell command strings
- `remote_write_file` is safer than raw overwrite, but it still mutates remote state and should be used with explicit path intent

## Remote Exec Observation
Use these two evidence paths:

1. startup logs
- `server.py` logs a `server_startup` event with the effective `REMOTE_EXEC_ENABLED` state

2. usage logs + audit recurrence
- each `remote_exec` call emits a `remote_exec_usage` JSON log line with:
  - `timestamp`
  - `host_alias`
  - `working_dir`
  - `command_summary`
  - `outcome`
- recurrence can be summarized from `audit_log` with:

```bash
python3 scripts/report_remote_exec_usage.py \
  --window-days 14 \
  --event-log /tmp/agent-hub-mcp-14d.jsonl
```

Operational interpretation:
- frequent `remote_exec_structured_candidate` entries mean operators are still using the escape hatch where `remote_run_process` already covers the case
- repeated `remote_exec` use across multiple days or maintenance windows is the evidence to watch before creating `server_admin.py`
- if `remote_exec` stays rare and exceptional, keep the current flag-gated stdio design
- the formal decision rule now lives in `docs/remote-exec-observation-window.md`

## Remote Exec Migration Table
Use `remote_run_process` by default for routine inspection.

Decision rule:
- prefer `remote_run_process` for inspection and read-only operational checks
- enable `REMOTE_EXEC_ENABLED=1` only when there is no structured equivalent and you explicitly accept the escape-hatch risk

| Use case | Before | After | Status |
|---|---|---|---|
| service status | `remote_exec(host, "systemctl status caddy")` | `remote_run_process(host, ["systemctl", "status", "caddy"])` | migrable now |
| service active check | `remote_exec(host, "systemctl is-active caddy")` | `remote_run_process(host, ["systemctl", "is-active", "caddy"])` | migrable now |
| service properties | `remote_exec(host, "systemctl show caddy")` | `remote_run_process(host, ["systemctl", "show", "caddy"])` | migrable now |
| journal tail | `remote_exec(host, "journalctl -u caddy -n 100")` | `remote_run_process(host, ["journalctl", "-u", "caddy", "-n", "100"])` | migrable now |
| listening sockets | `remote_exec(host, "ss -lntp")` | `remote_run_process(host, ["ss", "-lntp"])` | migrable now |
| firewall status | `remote_exec(host, "ufw status")` | `remote_run_process(host, ["ufw", "status"])` | migrable now |
| caddy validation | `remote_exec(host, "caddy validate --adapter caddyfile --config /etc/caddy/Caddyfile")` | `remote_run_process(host, ["caddy", "validate", "--adapter", "caddyfile", "--config", "/etc/caddy/Caddyfile"])` | migrable now |
| compound shell, pipes, redirects | `remote_exec(host, "systemctl status caddy && whoami")` | stays `remote_exec` | exceptional |
| arbitrary maintenance shell | `remote_exec(host, "<shell command>")` | stays `remote_exec` | exceptional |

## Hetzner Tool Guardrails
`tools/hetzner.py` is inventory-oriented and depends on `HCLOUD_TOKEN`.

Operational implication:
- without `HCLOUD_TOKEN`, Hetzner tools fail fast with a clear error
- there is no separate credentials file in the repo

## Rollback Basics
For most operational changes, rollback means one of:
- restore the previous SQLite backup
- revert the code change and rerun `ensure_ready()`
- stop using `server_sse.py` if an exposure or auth assumption is unclear

## Runbook Boundary
The Nextcloud backup/alerting automation under `tools/n8n/` is not required to run the hub itself.
Use it only if you are operating the external alerting workflows documented there.
