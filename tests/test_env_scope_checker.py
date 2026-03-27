"""Tests for tools/env_scope_checker.py."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "tools" / "env_scope_checker.py"


def _write_env(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


class TestEnvScopeChecker(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name) / "maincua"
        self.shared = self.root / ".env"
        self.cluster = self.root / "cua" / "config" / ".env"
        self.service = self.root / "cua" / ".env"
        self.matrix = self.root / "env-scope-matrix.md"
        self.extra = self.root / "extra" / ".env"

        _write_env(
            self.shared,
            "# comment\n\nPROXY_AUTH_TOKEN=S3CR3T-DO-NOT-LEAK\n",
        )
        _write_env(
            self.cluster,
            "OPENAI_API_KEY=cluster-secret\n",
        )
        _write_env(
            self.service,
            "# blank lines should be ignored\n\nPORT=8080\n",
        )
        self.matrix.write_text(self._render_matrix(), encoding="utf-8")

    def _render_matrix(self) -> str:
        return f"""# Test Matrix

## Matrix By VPS

### MAINCUA VPS

| VPS | File | Scope | Current role | Source of truth | Consumers | Mutation rule |
|---|---|---|---|---|---|---|
| `MAINCUA VPS` | `{self.shared}` | `host-shared` | shared env | yes | local | host shared only |
| `MAINCUA VPS` | `{self.cluster}` | `cluster-shared` | cluster env | yes | local | cluster shared only |
| `MAINCUA VPS` | `{self.service}` | `service-local` | service env | yes | local | service local only |

## Checker Spec

```json
{{
  "version": 1,
  "vps": [
    {{
      "id": "maincua",
      "label": "MAINCUA VPS",
      "access": {{
        "mode": "local"
      }},
      "discovery": {{
        "paths": [
          "{self.shared}"
        ],
        "roots": [
          "{self.root}"
        ],
        "ignore_globs": []
      }},
      "files": [
        {{
          "path": "{self.shared}",
          "scope": "host-shared",
          "mutation_rule": "host shared only",
          "strict_allowlist": true,
          "required_vars": [
            "PROXY_AUTH_TOKEN"
          ],
          "allowed_vars": [
            "PROXY_AUTH_TOKEN"
          ]
        }},
        {{
          "path": "{self.cluster}",
          "scope": "cluster-shared",
          "mutation_rule": "cluster shared only",
          "strict_allowlist": true,
          "required_vars": [
            "OPENAI_API_KEY"
          ],
          "allowed_vars": [
            "OPENAI_API_KEY"
          ]
        }},
        {{
          "path": "{self.service}",
          "scope": "service-local",
          "mutation_rule": "service local only",
          "strict_allowlist": true,
          "required_vars": [
            "PORT"
          ],
          "allowed_vars": [
            "PORT"
          ]
        }}
      ]
    }}
  ]
}}
```
"""

    def _run(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--matrix-path",
                str(self.matrix),
                "--vps",
                "maincua",
                "--report",
                "json",
            ],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_allowed_variable_in_correct_scope(self):
        completed = self._run()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["findings"], [])

    def test_forbidden_variable_in_maincua_host_shared(self):
        _write_env(
            self.shared,
            "PROXY_AUTH_TOKEN=S3CR3T-DO-NOT-LEAK\nOPENAI_API_KEY=wrong-place\n",
        )
        completed = self._run()
        self.assertEqual(completed.returncode, 1, completed.stderr)
        payload = json.loads(completed.stdout)
        codes = {item["code"] for item in payload["findings"]}
        self.assertIn("FORBIDDEN_IN_SCOPE", codes)

    def test_undeclared_variable_in_monitored_file(self):
        _write_env(self.service, "PORT=8080\nEXTRA_VAR=surprise\n")
        completed = self._run()
        self.assertEqual(completed.returncode, 1, completed.stderr)
        payload = json.loads(completed.stdout)
        codes = {item["code"] for item in payload["findings"]}
        self.assertIn("UNDECLARED_VARIABLE", codes)

    def test_missing_listed_file(self):
        self.service.unlink()
        completed = self._run()
        self.assertEqual(completed.returncode, 1, completed.stderr)
        payload = json.loads(completed.stdout)
        codes = {item["code"] for item in payload["findings"]}
        self.assertIn("MISSING_ALLOWED_ENTRY", codes)

    def test_unknown_path_discovered_outside_matrix(self):
        _write_env(self.extra, "WHATEVER=1\n")
        completed = self._run()
        self.assertEqual(completed.returncode, 1, completed.stderr)
        payload = json.loads(completed.stdout)
        codes = {item["code"] for item in payload["findings"]}
        self.assertIn("UNKNOWN_PATH", codes)

    def test_parser_ignores_comments_and_blank_lines(self):
        _write_env(
            self.service,
            "\n# ignore\n\nPORT=8080\n# trailing comment\n",
        )
        completed = self._run()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["findings"], [])

    def test_output_does_not_leak_values(self):
        completed = self._run()
        self.assertNotIn("S3CR3T-DO-NOT-LEAK", completed.stdout)
        self.assertNotIn("cluster-secret", completed.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
