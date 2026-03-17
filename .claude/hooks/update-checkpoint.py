#!/usr/bin/env python3
"""Auto-updates notes/session-checkpoint.md on session Stop.

Captures: date, branch, git status, last commits, test results.
Preserves: "Próximo passo" and "Riscos" sections from previous checkpoint.
"""

import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT = Path("/home/rdios/agent-hub-mcp")
CHECKPOINT = PROJECT / "notes" / "session-checkpoint.md"


def run(cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=PROJECT, timeout=60)
        return r.stdout.strip()
    except Exception as e:
        return f"(error: {e})"


def run_tests(script):
    try:
        r = subprocess.run(
            ["python3", script], capture_output=True, text=True, cwd=PROJECT, timeout=120
        )
        output = r.stdout + r.stderr
        m = re.search(r"\d+/\d+ tests? passed", output)
        if m:
            return m.group(0)
        return "PASS" if r.returncode == 0 else "FAILED"
    except Exception as e:
        return f"error: {e}"


def extract_section(text, heading):
    """Extract content of a ## section, stopping at the next ## or end of file."""
    pattern = rf"## {re.escape(heading)}\n(.*?)(?=\n## |\Z)"
    m = re.search(pattern, text, re.DOTALL)
    if not m:
        return None
    content = m.group(1).strip()
    if not content or content == "---":
        return None
    return content


# --- Gather static data ---
branch = run(["git", "branch", "--show-current"]) or "main"
git_status = run(["git", "status", "--short"]) or "(clean — no pending changes)"
git_log = run(["git", "log", "--oneline", "-5"])
smoke = run_tests("tests/smoke_test.py")
remote = run_tests("tests/test_remote.py")

# --- Preserve human/LLM-written sections from existing checkpoint ---
PLACEHOLDER = "(atualizar manualmente)"

proximo = PLACEHOLDER
riscos = PLACEHOLDER

if CHECKPOINT.exists():
    existing = CHECKPOINT.read_text()
    extracted_proximo = extract_section(existing, "Próximo passo")
    extracted_riscos = extract_section(existing, "Riscos")
    if extracted_proximo and PLACEHOLDER not in extracted_proximo:
        proximo = extracted_proximo
    if extracted_riscos and PLACEHOLDER not in extracted_riscos:
        riscos = extracted_riscos

# --- Write checkpoint ---
now = datetime.now().strftime("%Y-%m-%d %H:%M")

content = f"""\
# Session Checkpoint

**Atualizado automaticamente:** {now}
**Branch:** {branch}

---

## Testes

```
smoke_test:  {smoke}
test_remote: {remote}
```

## Arquivos modificados (git status)

```
{git_status}
```

## Últimos commits

```
{git_log}
```

## Próximo passo

{proximo}

## Riscos

{riscos}

---

*"Próximo passo" e "Riscos" são preservados entre sessões. Edite ou peça ao Claude para atualizar antes de encerrar.*
"""

CHECKPOINT.parent.mkdir(parents=True, exist_ok=True)
CHECKPOINT.write_text(content)
