# Environment Scope Matrix

This document is the source of truth for environment-file scope across the current VPS fleet.

## Goal
- stop treating every `.env` file as equivalent
- make each file belong to exactly one scope
- define where a secret may be centralized and where it must stay local
- require explicit VPS labels for every path

## Scope Classes

| Scope | Meaning | Allowed contents | Not allowed |
|---|---|---|---|
| `host-shared` | shared by multiple services on the same VPS | shell/session variables, infrastructure tokens, non-conflicting shared service tokens | provider keys that differ between apps on the same VPS |
| `cluster-shared` | shared by a subset of apps on the same VPS/domain | keys intentionally reused by one domain such as `CUA/Gateway` | unrelated app secrets from other domains |
| `service-local` | owned by one service unit or one deployment surface | bind host/port, DB path, service toggles | unrelated app secrets |
| `app-local` | owned by one application only | app-specific credentials and provider keys | secrets for other apps |
| `repo-local override` | repo-specific override file kept near code | non-secret local overrides when needed | duplicated shared secrets |
| `documentation` | host-local or repo-local documentation file | operational contract and ownership notes | live secrets |

## Fleet Identity

| VPS | Hostname | Address | Role |
|---|---|---|---|
| `HUB VPS` | `ubuntu-4gb-hel1-2` | `157.180.26.156` | main hub and local admin runtime |
| `NEXT VPS` | `next` | `157.180.23.54` | remote SSE surface for agent-hub |
| `MAINCUA VPS` | `maincua` | `37.27.252.206` | CUA, legacy gateway, n8n, app stack |

## Matrix By VPS

### HUB VPS

| VPS | File | Scope | Current role | Source of truth | Consumers | Mutation rule |
|---|---|---|---|---|---|---|
| `HUB VPS` | `/home/rdios/.env` | `host-shared` | shared secrets and infrastructure tokens | yes | bash sessions, local wrappers, `agent-hub-sse.service` via systemd drop-in | add only host-shared, non-conflicting values |
| `HUB VPS` | `/home/rdios/agent-hub-mcp/.env` | `repo-local override` | repo-local overrides for `agent-hub-mcp` | no | local repo startup paths | keep `HUB_DB_PATH` or other repo-local overrides only |
| `HUB VPS` | `/etc/systemd/system/agent-hub-sse.service.d/10-env.conf` | `service-local` | injects `EnvironmentFile=-/home/rdios/.env` into `agent-hub-sse.service` | yes | `agent-hub-sse.service` | change only if the service load order changes |
| `HUB VPS` | `/etc/systemd/system/agent-hub-sse.service` | `service-local` | SSE unit definition | yes | systemd | keep unit concerns only; do not inline secrets |

### NEXT VPS

| VPS | File | Scope | Current role | Source of truth | Consumers | Mutation rule |
|---|---|---|---|---|---|---|
| `NEXT VPS` | `/home/rdios/.env` | `host-shared` | shared user secrets for this VPS | yes | bash sessions, optional service loaders | keep only secrets intentionally shared on `NEXT VPS` |
| `NEXT VPS` | `/home/rdios/agent-hub-mcp/.env` | `repo-local override` | repo-local overrides for `agent-hub-mcp` | no | repo-local startup only | do not duplicate `MANUS_API_KEY`, `HCLOUD_TOKEN`, or other shared secrets here |
| `NEXT VPS` | `/etc/agent-hub-mcp/server_sse.env` | `service-local` | SSE runtime config | yes | `agent-hub-sse.service` | keep `HUB_DB_PATH`, `MCP_SSE_HOST`, `MCP_SSE_PORT`, `MCP_SSE_ALLOW_PUBLIC_BIND`, optional `HCLOUD_TOKEN` |
| `NEXT VPS` | `/etc/systemd/system/agent-hub-sse.service` | `service-local` | SSE unit definition | yes | systemd | keep service wiring only; load shared secrets through `EnvironmentFile` |
| `NEXT VPS` | `/home/rdios/ENVIRONMENT.md` | `documentation` | host-local operational contract | yes | operators and agents | every file path in this document must remain prefixed with `NEXT VPS` context |

### MAINCUA VPS

| VPS | File | Scope | Current role | Source of truth | Consumers | Mutation rule |
|---|---|---|---|---|---|---|
| `MAINCUA VPS` | `/home/rdios/.env` | `host-shared` | host-wide non-conflicting shared values | yes | bash sessions, selected services | only variables that are reused on this VPS and do not collide by meaning or value |
| `MAINCUA VPS` | `/home/rdios/cua/config/.env` | `cluster-shared` | provider keys for the `CUA/Gateway` domain | yes | `cua` agents, `gateway` paths, `/opt/secrets/maincua.env` consumers | shared only inside the `CUA/Gateway` domain |
| `MAINCUA VPS` | `/opt/secrets/maincua.env` | `cluster-shared` | stable pointer for `gateway/config.py` | yes | legacy `gateway/config.py` path | may point only to `MAINCUA VPS /home/rdios/cua/config/.env` |
| `MAINCUA VPS` | `/home/rdios/cua/.env` | `service-local` | local routing settings for `cua` | no | `cua` runtime | keep listener and routing settings only |
| `MAINCUA VPS` | `/home/rdios/gateway/.env` | `service-local` | local listener settings for legacy gateway | no | legacy gateway entrypoints | keep listener and local toggles only |
| `MAINCUA VPS` | `/etc/systemd/system/gateway.service.d/10-env.conf` | `service-local` | adds `EnvironmentFile` order for `gateway.service` | yes | `gateway.service` | keep loading `/home/rdios/.env` before `/home/rdios/cua/config/.env` |
| `MAINCUA VPS` | `/home/rdios/apps/nummus-auto-backend/.env` | `app-local` | app-specific runtime and provider key | yes | `nummus-auto-backend` | do not centralize into `/home/rdios/.env` while its provider key differs from `CUA/Gateway` |
| `MAINCUA VPS` | `/home/rdios/cua/n8n/.env` | `app-local` | n8n runtime and integration keys | yes | `n8n` container/runtime | do not centralize into `/home/rdios/.env` while its Gemini key differs from `CUA/Gateway` |
| `MAINCUA VPS` | `/home/rdios/apps/spa-renata-protocolos/.env` | `app-local` | app-specific runtime and secrets | yes | `spa-renata-protocolos` | keep local unless explicitly refactored |
| `MAINCUA VPS` | `/home/rdios/apps/spa-renata-protocolos/packages/db/.env` | `app-local` | package-local database settings for `spa-renata-protocolos` | yes | spa package database layer | keep local to the package |
| `MAINCUA VPS` | `/home/rdios/apps/bpm-editor-mvp/server/.env` | `app-local` | BPM editor backend runtime and database settings | yes | `bpm-editor-mvp` backend | keep local to the app |
| `MAINCUA VPS` | `/home/rdios/evolution-api/.env` | `app-local` | Evolution runtime and infra credentials | yes | `evolution_api` compose stack | keep local; contains unrelated infra credentials |
| `MAINCUA VPS` | `/home/rdios/kommo-gateway/.env` | `app-local` | Kommo-specific secrets | yes | `kommo-gateway.service` | keep local |
| `MAINCUA VPS` | `/home/rdios/kommo-merge/.env` | `app-local` | Kommo merge runtime and tokens | yes | `kommo-merge` jobs | keep local |
| `MAINCUA VPS` | `/home/rdios/claude-scripts/.env` | `app-local` | helper scripts local database access | yes | `claude-scripts` | keep local to the scripts |

## Shared Variable Families

| Variable family | VPS | Observed sources | Result | Rule |
|---|---|---|---|---|
| `HCLOUD_TOKEN` | `HUB VPS` | `HUB VPS /home/rdios/.env` | safe in `host-shared` | keep in `HUB VPS /home/rdios/.env` |
| `OPENAI_API_KEY` | `HUB VPS` | `HUB VPS /home/rdios/.env` | safe in `host-shared` | keep in `HUB VPS /home/rdios/.env` |
| `MANUS_API_KEY` | `HUB VPS` | `HUB VPS /home/rdios/.env` | safe in `host-shared` | keep in `HUB VPS /home/rdios/.env` |
| `MANUS_API_KEY` | `NEXT VPS` | `NEXT VPS /home/rdios/.env` | safe in `host-shared` | keep in `NEXT VPS /home/rdios/.env` |
| `PROXY_AUTH_TOKEN` | `MAINCUA VPS` | `MAINCUA VPS /home/rdios/.env`, previously duplicated in `MAINCUA VPS /home/rdios/cua/.env` and `MAINCUA VPS /home/rdios/gateway/.env` | safe in `host-shared` | keep in `MAINCUA VPS /home/rdios/.env` |
| `REPLIT_HMAC_SECRET` | `MAINCUA VPS` | `MAINCUA VPS /home/rdios/.env`, previously duplicated in `MAINCUA VPS /home/rdios/cua/.env` and `MAINCUA VPS /home/rdios/gateway/.env` | safe in `host-shared` | keep in `MAINCUA VPS /home/rdios/.env` |
| `OPENAI_API_KEY` | `MAINCUA VPS` | `MAINCUA VPS /home/rdios/cua/config/.env`, `MAINCUA VPS /home/rdios/apps/nummus-auto-backend/.env` | collision detected | do not move to `MAINCUA VPS /home/rdios/.env` |
| `GEMINI_API_KEY` | `MAINCUA VPS` | `MAINCUA VPS /home/rdios/cua/config/.env`, `MAINCUA VPS /home/rdios/cua/n8n/.env` | collision detected | do not move to `MAINCUA VPS /home/rdios/.env` |

## Checker Baselines

- `Checker Spec` is the frozen v1 baseline for variable scope placement. It drives `tools/env_scope_checker.py`.
- `Wiring Spec` is the static wiring baseline. It drives `tools/env_wiring_checker.py`.
- The scope checker answers `can this variable live here?`
- The wiring checker answers `is this service or loader pointing to the expected file path, in the expected order?`

## Static Wiring Matrix

| VPS | Target | Kind | Path | Expected wiring | Mutation rule |
|---|---|---|---|---|---|
| `HUB VPS` | `agent-hub-sse.service` | `systemd-unit` | `/etc/systemd/system/agent-hub-sse.service` | `EnvironmentFile=-/home/rdios/.env` | change only if the service load order changes |
| `HUB VPS` | `run-agent-hub-mcp.py` | `path-patterns` | `/home/rdios/.claude/run-agent-hub-mcp.py` | load `/home/rdios/.env` before repo `.env` | load `/home/rdios/.env` before repo `.env` |
| `NEXT VPS` | `agent-hub-sse.service` | `systemd-unit` | `/etc/systemd/system/agent-hub-sse.service` | `EnvironmentFile=-/home/rdios/.env` then `/etc/agent-hub-mcp/server_sse.env` | keep `/home/rdios/.env` before `/etc/agent-hub-mcp/server_sse.env` |
| `MAINCUA VPS` | `gateway.service` | `systemd-unit` | `/etc/systemd/system/gateway.service` | `EnvironmentFile=-/home/rdios/.env` then `/home/rdios/cua/config/.env` | keep loading `/home/rdios/.env` before `/home/rdios/cua/config/.env` |
| `MAINCUA VPS` | `maincua.env symlink` | `symlink` | `/opt/secrets/maincua.env` | resolve to `/home/rdios/cua/config/.env` | may point only to `MAINCUA VPS /home/rdios/cua/config/.env` |
| `MAINCUA VPS` | `gateway/config.py` | `path-patterns` | `/home/rdios/gateway/config.py` | reference `/opt/secrets/maincua.env` and `load_dotenv(SECRETS_FILE)` | keep `/opt/secrets/maincua.env` as the first shared secrets path |
| `MAINCUA VPS` | `cua/agent_main.py` | `path-patterns` | `/home/rdios/cua/agent_main.py` | build `CONFIG_DIR/.env` and call `load_dotenv(dotenv_path=ENV_PATH)` | keep loading `CONFIG_DIR/.env` explicitly via `load_dotenv` |
| `MAINCUA VPS` | `evolution_api compose` | `compose-env-file` | `/home/rdios/evolution-api/docker-compose.yaml` | service `evolution_api` uses `env_file: [.env]` | keep `.env` as the compose `env_file` for `evolution_api` |
| `MAINCUA VPS` | `kommo-gateway.service` | `systemd-unit` | `/etc/systemd/system/kommo-gateway.service` | `EnvironmentFile=/home/rdios/kommo-gateway/.env` | keep `/home/rdios/kommo-gateway/.env` as the only `EnvironmentFile` |

## Checker Spec

```json
{
  "version": 1,
  "vps": [
    {
      "id": "hub",
      "label": "HUB VPS",
      "access": {
        "mode": "local"
      },
      "discovery": {
        "paths": [
          "/home/rdios/.env"
        ],
        "roots": [
          "/home/rdios/agent-hub-mcp"
        ],
        "ignore_globs": []
      },
      "files": [
        {
          "path": "/home/rdios/.env",
          "scope": "host-shared",
          "mutation_rule": "add only host-shared, non-conflicting values",
          "strict_allowlist": true,
          "required_vars": [
            "HCLOUD_TOKEN",
            "OPENAI_API_KEY",
            "MANUS_API_KEY"
          ],
          "allowed_vars": [
            "HCLOUD_TOKEN",
            "OPENAI_API_KEY",
            "MANUS_API_KEY"
          ]
        },
        {
          "path": "/home/rdios/agent-hub-mcp/.env",
          "scope": "repo-local override",
          "mutation_rule": "keep `HUB_DB_PATH` or other repo-local overrides only",
          "strict_allowlist": true,
          "required_vars": [
            "HUB_DB_PATH"
          ],
          "allowed_vars": [
            "HUB_DB_PATH"
          ],
          "forbidden_vars": [
            "HCLOUD_TOKEN",
            "OPENAI_API_KEY",
            "MANUS_API_KEY"
          ]
        }
      ]
    },
    {
      "id": "next",
      "label": "NEXT VPS",
      "access": {
        "mode": "ssh",
        "host_alias": "next",
        "sudo": true
      },
      "discovery": {
        "paths": [
          "/home/rdios/.env"
        ],
        "roots": [
          "/home/rdios/agent-hub-mcp",
          "/etc/agent-hub-mcp"
        ],
        "ignore_globs": []
      },
      "files": [
        {
          "path": "/home/rdios/.env",
          "scope": "host-shared",
          "mutation_rule": "keep only secrets intentionally shared on `NEXT VPS`",
          "strict_allowlist": true,
          "required_vars": [
            "MANUS_API_KEY"
          ],
          "allowed_vars": [
            "MANUS_API_KEY"
          ]
        },
        {
          "path": "/home/rdios/agent-hub-mcp/.env",
          "scope": "repo-local override",
          "mutation_rule": "do not duplicate `MANUS_API_KEY`, `HCLOUD_TOKEN`, or other shared secrets here",
          "strict_allowlist": true,
          "required_vars": [],
          "allowed_vars": [],
          "forbidden_vars": [
            "MANUS_API_KEY",
            "HCLOUD_TOKEN",
            "OPENAI_API_KEY"
          ]
        },
        {
          "path": "/etc/agent-hub-mcp/server_sse.env",
          "scope": "service-local",
          "mutation_rule": "keep `HUB_DB_PATH`, `MCP_SSE_HOST`, `MCP_SSE_PORT`, `MCP_SSE_ALLOW_PUBLIC_BIND`, optional `HCLOUD_TOKEN`",
          "strict_allowlist": true,
          "required_vars": [
            "HUB_DB_PATH",
            "MCP_SSE_HOST",
            "MCP_SSE_PORT",
            "MCP_SSE_ALLOW_PUBLIC_BIND"
          ],
          "allowed_vars": [
            "HUB_DB_PATH",
            "MCP_SSE_HOST",
            "MCP_SSE_PORT",
            "MCP_SSE_ALLOW_PUBLIC_BIND",
            "HCLOUD_TOKEN"
          ]
        }
      ]
    },
    {
      "id": "maincua",
      "label": "MAINCUA VPS",
      "access": {
        "mode": "ssh",
        "host_alias": "maincua-prod"
      },
      "discovery": {
        "paths": [
          "/home/rdios/.env"
        ],
        "roots": [
          "/home/rdios/cua",
          "/home/rdios/gateway",
          "/home/rdios/apps",
          "/home/rdios/evolution-api",
          "/home/rdios/kommo-gateway",
          "/home/rdios/kommo-merge",
          "/home/rdios/claude-scripts",
          "/opt/secrets"
        ],
        "ignore_globs": [
          "/home/rdios/apps/*/node_modules/*",
          "/home/rdios/apps/*/.next/*"
        ]
      },
      "files": [
        {
            "path": "/home/rdios/.env",
            "scope": "host-shared",
            "mutation_rule": "only variables that are reused on this VPS and do not collide by meaning or value",
            "strict_allowlist": true,
            "required_vars": [
              "PGPASSWORD",
              "PROXY_AUTH_TOKEN",
              "REPLIT_HMAC_SECRET"
            ],
            "allowed_vars": [
              "PGPASSWORD",
              "PROXY_AUTH_TOKEN",
              "REPLIT_HMAC_SECRET"
            ],
            "forbidden_vars": [
              "OPENAI_API_KEY",
              "ANTHROPIC_API_KEY",
              "GEMINI_API_KEY",
              "GEMINI_API_KEY_2",
              "CLAUDE_API_KEY"
            ]
          },
          {
            "path": "/home/rdios/cua/config/.env",
            "scope": "cluster-shared",
            "mutation_rule": "shared only inside the `CUA/Gateway` domain",
            "strict_allowlist": true,
            "required_vars": [
              "ANTHROPIC_API_KEY",
              "CLAUDE_API_KEY",
              "GEMINI_API_KEY",
              "GEMINI_API_KEY_2",
              "OLLAMA_ENDPOINT",
              "OPENAI_API_KEY",
              "OPENAI_MODEL"
            ],
            "allowed_vars": [
              "ANTHROPIC_API_KEY",
              "CLAUDE_API_KEY",
              "GEMINI_API_KEY",
              "GEMINI_API_KEY_2",
              "OLLAMA_ENDPOINT",
              "OPENAI_API_KEY",
              "OPENAI_MODEL"
            ]
          },
          {
            "path": "/opt/secrets/maincua.env",
            "scope": "cluster-shared",
            "mutation_rule": "may point only to `MAINCUA VPS /home/rdios/cua/config/.env`",
            "strict_allowlist": true,
            "required_vars": [
              "ANTHROPIC_API_KEY",
              "CLAUDE_API_KEY",
              "GEMINI_API_KEY",
              "GEMINI_API_KEY_2",
              "OLLAMA_ENDPOINT",
              "OPENAI_API_KEY",
              "OPENAI_MODEL"
            ],
            "allowed_vars": [
              "ANTHROPIC_API_KEY",
              "CLAUDE_API_KEY",
              "GEMINI_API_KEY",
              "GEMINI_API_KEY_2",
              "OLLAMA_ENDPOINT",
              "OPENAI_API_KEY",
              "OPENAI_MODEL"
            ]
          },
          {
            "path": "/home/rdios/cua/.env",
            "scope": "service-local",
            "mutation_rule": "keep listener and routing settings only",
            "strict_allowlist": true,
            "required_vars": [
              "GATEWAY_PORT",
              "HOST",
              "PORT"
            ],
            "allowed_vars": [
              "GATEWAY_PORT",
              "HOST",
              "PORT"
            ]
          },
          {
            "path": "/home/rdios/gateway/.env",
            "scope": "service-local",
            "mutation_rule": "keep listener and local toggles only",
            "strict_allowlist": true,
            "required_vars": [
              "GATEWAY_PORT",
              "HOST",
              "PORT"
            ],
            "allowed_vars": [
              "GATEWAY_PORT",
              "HOST",
              "PORT"
            ]
          },
          {
            "path": "/home/rdios/apps/nummus-auto-backend/.env",
            "scope": "app-local",
            "mutation_rule": "do not centralize into `/home/rdios/.env` while its provider key differs from `CUA/Gateway`",
            "strict_allowlist": true,
            "required_vars": [
              "OPENAI_API_KEY",
              "OPENAI_MODEL",
              "PORT",
              "WP_APP_PASSWORD",
              "WP_URL",
              "WP_USERNAME"
            ],
            "allowed_vars": [
              "MAX_POSTS_PER_DAY",
              "MIN_QUALITY_SCORE",
              "MODERATOR_EMAIL",
              "NODE_ENV",
              "OPENAI_API_KEY",
              "OPENAI_MODEL",
              "PORT",
              "RSS_AGENCIA_BRASIL_ECONOMIA",
              "RSS_CONTABEIS_TRIBUTARIO",
              "SCRAPE_INTERVAL_HOURS",
              "WP_APP_PASSWORD",
              "WP_URL",
              "WP_USERNAME"
            ]
          },
          {
            "path": "/home/rdios/cua/n8n/.env",
            "scope": "app-local",
            "mutation_rule": "do not centralize into `/home/rdios/.env` while its Gemini key differs from `CUA/Gateway`",
            "strict_allowlist": true,
            "required_vars": [
              "EVOLUTION_API_KEY",
              "GEMINI_API_KEY",
              "N8N_ENCRYPTION_KEY",
              "N8N_USER_MANAGEMENT_JWT_SECRET"
            ],
            "allowed_vars": [
              "EVOLUTION_API_KEY",
              "EVOLUTION_API_URL",
              "EVOLUTION_INSTANCE",
              "GEMINI_API_KEY",
              "GEMINI_API_KEY_FALLBACK",
              "GENERIC_TIMEZONE",
              "N8N_ENCRYPTION_KEY",
              "N8N_USER_MANAGEMENT_JWT_SECRET"
            ]
          },
          {
            "path": "/home/rdios/apps/spa-renata-protocolos/.env",
            "scope": "app-local",
            "mutation_rule": "keep local unless explicitly refactored",
            "strict_allowlist": true,
            "required_vars": [
              "DATABASE_URL",
              "JWT_SECRET",
              "PORT"
            ],
            "allowed_vars": [
              "ADMIN_CORE_ENABLED",
              "APP_BASE_URL",
              "AUTH_STRATEGY",
              "DATABASE_URL",
              "EMAIL_FROM",
              "JWT_SECRET",
              "LOG_LEVEL",
              "NODE_ENV",
              "PORT",
              "SEED_ADMIN_PASSWORD",
              "SEED_SUPERADMIN_EMAIL",
              "SMTP_HOST",
              "SMTP_PASS",
              "SMTP_PORT",
              "SMTP_USER"
            ]
          },
          {
            "path": "/home/rdios/apps/spa-renata-protocolos/packages/db/.env",
            "scope": "app-local",
            "mutation_rule": "keep local to the package",
            "strict_allowlist": true,
            "required_vars": [
              "DATABASE_URL"
            ],
            "allowed_vars": [
              "DATABASE_URL"
            ]
          },
          {
            "path": "/home/rdios/apps/bpm-editor-mvp/server/.env",
            "scope": "app-local",
            "mutation_rule": "keep local to the app",
            "strict_allowlist": true,
            "required_vars": [
              "DB_HOST",
              "DB_NAME",
              "DB_PASSWORD",
              "DB_PORT",
              "DB_USER",
              "PORT"
            ],
            "allowed_vars": [
              "CORS_ORIGIN",
              "DB_HOST",
              "DB_NAME",
              "DB_PASSWORD",
              "DB_PORT",
              "DB_USER",
              "NODE_ENV",
              "PORT",
              "TYPEORM_SYNCHRONIZE"
            ]
          },
          {
            "path": "/home/rdios/evolution-api/.env",
            "scope": "app-local",
            "mutation_rule": "keep local; contains unrelated infra credentials",
            "strict_allowlist": false,
            "required_vars": [],
            "allowed_vars": []
          },
          {
            "path": "/home/rdios/kommo-gateway/.env",
            "scope": "app-local",
            "mutation_rule": "keep local",
            "strict_allowlist": true,
            "required_vars": [
              "CLIENT_SECRET",
              "PORT",
              "REFRESH_TOKEN",
              "WEBHOOK_SECRET"
            ],
            "allowed_vars": [
              "BASE_URL",
              "CLIENT_ID",
              "CLIENT_SECRET",
              "INTEGRATION_ID",
              "LOG_LEVEL",
              "PORT",
              "REFRESH_TOKEN",
              "WEBHOOK_SECRET"
            ]
          },
          {
            "path": "/home/rdios/kommo-merge/.env",
            "scope": "app-local",
            "mutation_rule": "keep local",
            "strict_allowlist": true,
            "required_vars": [
              "ACCESS_TOKEN",
              "KOMMO_TOKEN"
            ],
            "allowed_vars": [
              "ACCESS_TOKEN",
              "BASE_URL",
              "CONCURRENCY",
              "HTTP_TIMEOUT_MS",
              "KOMMO_BASE",
              "KOMMO_TOKEN",
              "REQS_PER_SECOND"
            ]
          },
          {
            "path": "/home/rdios/claude-scripts/.env",
            "scope": "app-local",
            "mutation_rule": "keep local to the scripts",
            "strict_allowlist": true,
            "required_vars": [
              "PGPASSWORD"
            ],
            "allowed_vars": [
              "PGPASSWORD"
            ]
          }
      ]
    }
  ]
}
```

## Wiring Spec

```json
{
  "version": 1,
  "vps": [
    {
      "id": "hub",
      "label": "HUB VPS",
      "access": {
        "mode": "local"
      },
      "targets": [
        {
          "name": "agent-hub-sse.service",
          "kind": "systemd-unit",
          "path": "/etc/systemd/system/agent-hub-sse.service",
          "mutation_rule": "change only if the service load order changes",
          "service_name": "agent-hub-sse.service",
          "expected_environment_files": [
            "-/home/rdios/.env"
          ]
        },
        {
          "name": "run-agent-hub-mcp.py",
          "kind": "path-patterns",
          "path": "/home/rdios/.claude/run-agent-hub-mcp.py",
          "mutation_rule": "load `/home/rdios/.env` before repo `.env`",
          "required_patterns": [
            "USER_ENV_FILE = Path(\"/home/rdios/.env\")",
            "REPO_ENV_FILE = BASE / \".env\"",
            "load_env_file(USER_ENV_FILE, env)",
            "load_env_file(REPO_ENV_FILE, env)"
          ]
        }
      ]
    },
    {
      "id": "next",
      "label": "NEXT VPS",
      "access": {
        "mode": "ssh",
        "host_alias": "next",
        "sudo": true
      },
      "targets": [
        {
          "name": "agent-hub-sse.service",
          "kind": "systemd-unit",
          "path": "/etc/systemd/system/agent-hub-sse.service",
          "mutation_rule": "keep `/home/rdios/.env` before `/etc/agent-hub-mcp/server_sse.env`",
          "service_name": "agent-hub-sse.service",
          "expected_environment_files": [
            "-/home/rdios/.env",
            "/etc/agent-hub-mcp/server_sse.env"
          ]
        }
      ]
    },
    {
      "id": "maincua",
      "label": "MAINCUA VPS",
      "access": {
        "mode": "ssh",
        "host_alias": "maincua-prod"
      },
      "targets": [
        {
          "name": "gateway.service",
          "kind": "systemd-unit",
          "path": "/etc/systemd/system/gateway.service",
          "mutation_rule": "keep loading `/home/rdios/.env` before `/home/rdios/cua/config/.env`",
          "service_name": "gateway.service",
          "expected_environment_files": [
            "-/home/rdios/.env",
            "/home/rdios/cua/config/.env"
          ]
        },
        {
          "name": "maincua.env symlink",
          "kind": "symlink",
          "path": "/opt/secrets/maincua.env",
          "mutation_rule": "may point only to `MAINCUA VPS /home/rdios/cua/config/.env`",
          "expected_target": "/home/rdios/cua/config/.env"
        },
        {
          "name": "gateway/config.py",
          "kind": "path-patterns",
          "path": "/home/rdios/gateway/config.py",
          "mutation_rule": "keep `/opt/secrets/maincua.env` as the first shared secrets path",
          "required_patterns": [
            "SECRETS_FILE = Path(\"/opt/secrets/maincua.env\")",
            "load_dotenv(SECRETS_FILE)"
          ]
        },
        {
          "name": "cua/agent_main.py",
          "kind": "path-patterns",
          "path": "/home/rdios/cua/agent_main.py",
          "mutation_rule": "keep loading `CONFIG_DIR/.env` explicitly via `load_dotenv`",
          "required_patterns": [
            "CONFIG_DIR = os.path.join(CUA_DIR, \"config\")",
            "ENV_PATH = os.path.join(CONFIG_DIR, \".env\")",
            "load_dotenv(dotenv_path=ENV_PATH)"
          ]
        },
        {
          "name": "evolution_api compose",
          "kind": "compose-env-file",
          "path": "/home/rdios/evolution-api/docker-compose.yaml",
          "mutation_rule": "keep `.env` as the compose `env_file` for `evolution_api`",
          "service_name": "evolution_api",
          "expected_environment_files": [
            ".env"
          ]
        },
        {
          "name": "kommo-gateway.service",
          "kind": "systemd-unit",
          "path": "/etc/systemd/system/kommo-gateway.service",
          "mutation_rule": "keep `/home/rdios/kommo-gateway/.env` as the only `EnvironmentFile`",
          "service_name": "kommo-gateway.service",
          "expected_environment_files": [
            "/home/rdios/kommo-gateway/.env"
          ]
        }
      ]
    }
  ]
}
```

## Change Policy

1. If a variable is needed by multiple services on one VPS and its value is identical by intent, place it in that VPS `host-shared` file.
2. If a variable is shared only within one domain on one VPS, place it in that VPS `cluster-shared` file.
3. If a variable differs by app, keep it `app-local`.
4. Never centralize a variable family into `host-shared` when the same variable name already carries different values on that VPS.
5. For systemd services, prefer `EnvironmentFile` over inline `Environment=` for shared secrets.
6. For Docker Compose or runtime-managed `.env` files, do not assume shell `source` semantics.

## Restart Boundary

| VPS | File class changed | Restart expectation |
|---|---|---|
| `HUB VPS` | `host-shared` used by already-running `server.py` wrapper | restart needed for the running process to consume new values |
| `NEXT VPS` | `host-shared` not currently consumed by active SSE-only runtime except via systemd load order | restart only if a moved variable is needed by the active service |
| `MAINCUA VPS` | `host-shared`, `cluster-shared`, or `app-local` | restart required per affected service; do not assume hot reload |
