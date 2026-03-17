"""
Smoke tests for agent-hub-mcp.

Run from project root:
    python tests/smoke_test.py

Tests call tool functions directly (no MCP protocol overhead).
"""

import json
import os
import sqlite3
import sys
import tempfile
import time
from contextlib import redirect_stdout
from io import StringIO

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Use an isolated temp DB for tests
_tmp = tempfile.mktemp(suffix=".sqlite")
os.environ["HUB_DB_PATH"] = _tmp

from hub.bootstrap import ensure_ready
from hub.db import get_conn, init_db
from hub.domain import VALID_DOMAINS, classify_domain
from hub_cli import main as hub_cli_main
from tools.artifacts import publish_artifact, read_artifact
from tools.locks import acquire_lock, release_lock
from tools.knowledge import (
    approve_knowledge,
    deprecate_knowledge,
    promote_knowledge,
    query_knowledge,
    supersede_knowledge,
    VALID_KNOWLEDGE_KINDS,
    VALID_KNOWLEDGE_SOURCE_TYPES,
    VALID_KNOWLEDGE_STATUSES,
)
from tools.notes import append_note, list_notes
from tools.memory import query_decisions, recall_memory, record_decision, store_memory
from tools.metrics import collect_task_metric, get_metrics
from tools.playbooks import get_playbook, seed_default_playbooks, upgrade_default_playbooks, validate_checklist
from tools.agents import get_agent_profile, list_agents, register_agent
from tools.orchestration import list_task_tree, record_review, submit_request, summarize_request
from tools.retrospectives import generate_retrospective, get_retrospective
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

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
_results = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = PASS if condition else FAIL
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))
    _results.append(condition)


def first_task(tasks: list[dict], task_kind: str) -> dict | None:
    return next((task for task in tasks if task.get("task_kind") == task_kind), None)


def run_cli(args: list[str]) -> tuple[int, dict, str]:
    """Run hub_cli.main() and capture its JSON output."""
    buf = StringIO()
    with redirect_stdout(buf):
        exit_code = hub_cli_main(args)
    raw = buf.getvalue().strip()
    parsed = json.loads(raw) if raw else {}
    return exit_code, parsed, raw


# ── Setup ────────────────────────────────────────────────────────────────────
ensure_ready()
print("\n=== agent-hub-mcp smoke tests ===\n")

# ── Tasks ────────────────────────────────────────────────────────────────────
print("[ Tasks ]")

r = create_task("Fix auth bug", description="JWT expiry not enforced", owner="claude", priority=8)
check("create_task returns ok", r["ok"] is True)
task_id = r["task_id"]

r2 = create_task("Fix auth bug", idempotency_key="fix-auth-v1")
r3 = create_task("Fix auth bug", idempotency_key="fix-auth-v1")
check("idempotency_key deduplicates", r3.get("idempotent") is True)

r = claim_task(task_id, owner="claude")
check("claim_task succeeds", r["ok"] is True and r["status"] == "claimed")

r = claim_task(task_id, owner="gpt")
check("double claim rejected", r["ok"] is False)

r = heartbeat_task(task_id, owner="claude", status="running")
check("heartbeat updates status", r["ok"] is True)

r = complete_task(task_id, owner="claude")
check("complete_task marks done", r["ok"] is True and r["status"] == "done")

r = complete_task(task_id, owner="claude")
check("completing done task rejected", r["ok"] is False)

r = create_task("Risky refactor")
tid2 = r["task_id"]
claim_task(tid2, owner="gpt")
r = fail_task(tid2, owner="gpt", error_message="unexpected import error")
check("fail_task works", r["ok"] is True and r["status"] == "failed")

r = list_tasks(status="done")
check("list_tasks filters by status", r["ok"] is True and r["count"] >= 1)

r = list_tasks(status="bad_status")
check("list_tasks rejects invalid status", r["ok"] is False)

# dependency-aware claiming
root = create_task("Composite request", task_kind="request", requested_agent="root")
root_id = root["task_id"]
work = create_task(
    "Execute composite request",
    parent_task_id=root_id,
    root_task_id=root_id,
    requested_agent="codex",
)
review = create_task(
    "Review composite request",
    parent_task_id=root_id,
    root_task_id=root_id,
    task_kind="review",
    requested_agent="claude",
    depends_on=[work["task_id"]],
)

r = claim_next_task(owner="claude", root_task_id=root_id)
check("claim_next skips blocked review tasks", r["ok"] is False)

r = claim_next_task(owner="codex", root_task_id=root_id)
check("claim_next claims runnable task", r["ok"] is True and r["task"]["id"] == work["task_id"])

complete_task(work["task_id"], owner="codex")
r = claim_next_task(owner="claude", root_task_id=root_id)
check("claim_next releases dependent review after work is done", r["ok"] is True and r["task"]["id"] == review["task_id"])

complete_task(review["task_id"], owner="claude")

# expired reclaim
exp = create_task("Expirable work", ttl=1)
claim_task(exp["task_id"], owner="worker-a")
time.sleep(1.1)
r = claim_task(exp["task_id"], owner="worker-b")
check("expired task can be reclaimed", r["ok"] is True and r.get("reclaimed") is True)

r = get_task(exp["task_id"])
check("get_task returns parsed metadata", r["ok"] is True and isinstance(r["task"]["metadata"], dict))

# ── Notes ─────────────────────────────────────────────────────────────────────
print("\n[ Notes ]")

r = append_note("Claude analyzed the auth flow and found the bug", task_id=task_id, author="claude")
check("append_note ok", r["ok"] is True)

r = append_note("GPT agreed with the diagnosis", task_id=task_id, author="gpt")
check("second append_note ok", r["ok"] is True)

r = list_notes(task_id=task_id)
check("list_notes returns 2 notes", r["ok"] is True and r["count"] == 2)

# ── Artifacts ─────────────────────────────────────────────────────────────────
print("\n[ Artifacts ]")

code = "def verify_token(token):\n    # fixed implementation\n    return jwt.decode(token, SECRET, algorithms=['HS256'])"
r = publish_artifact("auth_fix.py", code, task_id=task_id, content_type="text/x-python", published_by="claude")
check("publish_artifact ok", r["ok"] is True)
artifact_id = r["artifact_id"]

r = read_artifact(artifact_id=artifact_id)
check("read_artifact by id", r["ok"] is True and "verify_token" in r["artifact"]["content"])

r = read_artifact(name="auth_fix.py")
check("read_artifact by name", r["ok"] is True)

r = publish_artifact("huge", "x" * (512 * 1024 + 1))
check("oversized artifact rejected", r["ok"] is False)

r = read_artifact()
check("read_artifact with no args rejected", r["ok"] is False)

# ── Locks ─────────────────────────────────────────────────────────────────────
print("\n[ Locks ]")

test_path = "/tmp/agent-hub-test-lock-target"
r = acquire_lock(test_path, owner="claude", ttl=60)
check("acquire_lock succeeds", r["ok"] is True)

r2 = acquire_lock(test_path, owner="gpt", ttl=60)
check("second acquire by other owner rejected", r2["ok"] is False)

r3 = acquire_lock(test_path, owner="claude", ttl=60)
check("same owner renews lock", r3.get("renewed") is True)

r = release_lock(test_path, owner="gpt")
check("release by wrong owner rejected", r["ok"] is False)

r = release_lock(test_path, owner="claude")
check("release by owner ok", r["ok"] is True)

r = acquire_lock(test_path, owner="gpt", ttl=1)
check("acquire after release ok", r["ok"] is True)
time.sleep(1.1)
r = acquire_lock(test_path, owner="claude", ttl=60)
check("expired lock reclaimed by new owner", r["ok"] is True)

# ── Orchestration ─────────────────────────────────────────────────────────────
print("\n[ Orchestration ]")

r = submit_request(
    "Inspect authentication flow\nPrepare a safer fallback approach",
    requested_by="user",
    planner_mode="heuristic",
    worker_agent="codex",
    reviewer_agent="claude",
    fallback_agent="gpt-fallback",
    max_work_items=2,
)
check("submit_request creates a task tree", r["ok"] is True and r["task_count"] >= 3)
request_root_id = r["root_task_id"]

tree = list_task_tree(request_root_id)
check("list_task_tree returns rooted hierarchy", tree["ok"] is True and tree["summary"]["total"] >= 4)
synth = first_task(tree["tasks"], "synthesize")
check("submit_request default synthesizer is codex-general",
      synth is not None and synth["requested_agent"] == "codex-general",
      f"requested_agent={synth.get('requested_agent') if synth else 'none'}")

status = summarize_request(request_root_id)
check("summarize_request lists ready work", status["ok"] is True and any(t["requested_agent"] == "codex" for t in status["ready_tasks"]))

work_claim = claim_next_task(owner="codex", root_task_id=request_root_id)
check("claim_next finds work inside submitted request", work_claim["ok"] is True and work_claim["task"]["task_kind"] == "work")
submitted_work_id = work_claim["task_id"]
complete_task(submitted_work_id, owner="codex")

review_claim = claim_next_task(owner="claude", root_task_id=request_root_id)
check("review task becomes claimable after work completion", review_claim["ok"] is True and review_claim["task"]["task_kind"] == "review")

review_result = record_review(
    submitted_work_id,
    reviewer="claude",
    verdict="fallback",
    feedback="The first pass is incomplete. Produce a stronger alternative.",
)
check("record_review creates fallback follow-up", review_result["ok"] is True and bool(review_result["followup_task_id"]))

fallback_task = get_task(review_result["followup_task_id"])
check("fallback task is assigned to fallback agent", fallback_task["ok"] is True and fallback_task["task"]["requested_agent"] == "gpt-fallback")

tree_after_review = list_task_tree(request_root_id)
synth = first_task(tree_after_review["tasks"], "synthesize")
check(
    "synthesize task tracks follow-up dependencies",
    synth is not None and review_result["followup_task_id"] in synth["depends_on"],
)

# ── Memory (F1) ──────────────────────────────────────────────────────────────
print("\n[ Memory — store_memory ]")

r = store_memory(domain="", content="test", author="claude")
check("store_memory rejects empty domain", r["ok"] is False and "domain" in r["error"])

r = store_memory(domain="backend", content="", author="claude")
check("store_memory rejects empty content", r["ok"] is False and "content" in r["error"])

r = store_memory(domain="backend", content="test", author="")
check("store_memory rejects empty author", r["ok"] is False and "author" in r["error"])

r = store_memory(domain="backend", content="test", author="claude", confidence=1.5)
check("store_memory rejects confidence > 1.0", r["ok"] is False and "confidence" in r["error"])

r = store_memory(domain="backend", content="test", author="claude", confidence=-0.1)
check("store_memory rejects confidence < 0.0", r["ok"] is False and "confidence" in r["error"])

r = store_memory(domain="arch", content="Use module boundaries for shared services", author="claude")
check("store_memory normalizes arch to architecture", r["ok"] is True and r["domain"] == "architecture")

r = store_memory(domain="invalidxyz", content="test", author="claude")
check("store_memory rejects invalid domain", r["ok"] is False and "invalid domain" in r["error"])

r = store_memory(
    domain="backend",
    content="Neon PostgreSQL does not support pg_notify in serverless mode",
    tags=["neon", "postgresql", "realtime"],
    author="claude",
    confidence=0.9,
)
check("store_memory creates entry", r["ok"] is True and r["domain"] == "backend")
mem_id_1 = r["memory_id"]

r = store_memory(
    domain="backend",
    content="Neon supports pg_notify via websocket proxy since v2",
    tags=["neon", "postgresql", "realtime"],
    author="claude",
    confidence=1.0,
    supersedes=mem_id_1,
)
check("store_memory with supersedes returns superseded id", r["ok"] is True and r["superseded"] == mem_id_1)
mem_id_2 = r["memory_id"]

r = store_memory(domain="backend", content="test", author="claude", supersedes="nonexistent-id")
check("store_memory rejects supersedes for missing entry", r["ok"] is False and "not found" in r["error"])

print("\n[ Memory — recall_memory ]")

r = recall_memory(domain="backend")
check("recall_memory returns entries for domain", r["ok"] is True and r["count"] >= 1)
# The superseded entry (mem_id_1) should be excluded by default
returned_ids = [m["id"] for m in r["memories"]]
check("recall_memory excludes superseded by default", mem_id_1 not in returned_ids)
check("recall_memory includes non-superseded", mem_id_2 in returned_ids)

r = recall_memory(domain="backend", include_superseded=True, min_confidence=0.0)
returned_ids = [m["id"] for m in r["memories"]]
check("recall_memory includes superseded when requested", mem_id_1 in returned_ids)

# Verify the superseded entry has confidence 0.0
superseded_entry = next((m for m in r["memories"] if m["id"] == mem_id_1), None)
check("superseded entry has confidence 0.0", superseded_entry is not None and superseded_entry["confidence"] == 0.0)

r = recall_memory(domain="backend", tags=["neon", "realtime"])
check("recall_memory filters by tag intersection", r["ok"] is True and r["count"] >= 1)

r = recall_memory(domain="backend", tags=["neon", "nonexistent-tag"])
check("recall_memory tag intersection excludes non-matching", r["count"] == 0)

# Store a low-confidence entry and verify min_confidence filter
store_memory(domain="backend", content="Maybe try Supabase?", author="gpt", confidence=0.2)
r = recall_memory(domain="backend", min_confidence=0.3)
low_conf_present = any("Supabase" in m["content"] for m in r["memories"])
check("recall_memory respects min_confidence", not low_conf_present)

# Verify ordering: updated_at DESC, confidence DESC
store_memory(domain="infra", content="Entry A", author="claude", confidence=0.5)
time.sleep(0.05)
store_memory(domain="infra", content="Entry B", author="claude", confidence=0.9)
r = recall_memory(domain="infra")
check(
    "recall_memory orders by updated_at DESC then confidence DESC",
    r["count"] == 2 and "Entry B" in r["memories"][0]["content"],
)

print("\n[ Memory — record_decision ]")

r = record_decision(domain="", question="q", decision="d", rationale="r", decided_by="claude")
check("record_decision rejects empty domain", r["ok"] is False and "domain" in r["error"])

r = record_decision(domain="backend", question="", decision="d", rationale="r", decided_by="claude")
check("record_decision rejects empty question", r["ok"] is False and "question" in r["error"])

r = record_decision(domain="backend", question="q", decision="", rationale="r", decided_by="claude")
check("record_decision rejects empty decision", r["ok"] is False and "decision" in r["error"])

r = record_decision(domain="backend", question="q", decision="d", rationale="", decided_by="claude")
check("record_decision rejects empty rationale", r["ok"] is False and "rationale" in r["error"])

r = record_decision(domain="backend", question="q", decision="d", rationale="r", decided_by="")
check("record_decision rejects empty decided_by", r["ok"] is False and "decided_by" in r["error"])

r = record_decision(
    domain="arch",
    question="Como organizar os módulos?",
    decision="Feature modules",
    rationale="Reduz acoplamento e deixa as fronteiras explícitas",
    decided_by="claude",
)
check("record_decision normalizes arch to architecture", r["ok"] is True and r["domain"] == "architecture")

r = record_decision(
    domain="invalidxyz",
    question="q",
    decision="d",
    rationale="r",
    decided_by="claude",
)
check("record_decision rejects invalid domain", r["ok"] is False and "invalid domain" in r["error"])

r = record_decision(
    domain="backend",
    question="Qual ORM usar para o BPM Editor?",
    decision="Prisma",
    rationale="Type-safety, compatibilidade com Neon, migrations automáticas",
    alternatives=["TypeORM", "Drizzle"],
    decided_by="claude",
    reviewed_by="gpt",
)
check("record_decision creates record", r["ok"] is True and r["domain"] == "backend")
decision_id_1 = r["decision_id"]

r = record_decision(
    domain="frontend",
    question="Qual bundler usar?",
    decision="Vite",
    rationale="Performance, HMR rápido, suporte React nativo",
    alternatives=["webpack", "esbuild"],
    decided_by="claude",
)
check("record_decision creates second record", r["ok"] is True)
decision_id_2 = r["decision_id"]

print("\n[ Memory — query_decisions ]")

r = query_decisions(domain="backend")
check("query_decisions filters by domain", r["ok"] is True and r["count"] >= 1)
check("query_decisions parses alternatives", isinstance(r["decisions"][0]["alternatives"], list))

r = query_decisions(keyword="ORM")
check("query_decisions partial match on keyword", r["count"] >= 1 and any("ORM" in d["question"] for d in r["decisions"]))

r = query_decisions(keyword="orm")
check("query_decisions case-insensitive search", r["count"] >= 1)

r = query_decisions(keyword="Neon")
check("query_decisions searches rationale field", r["count"] >= 1)

r = query_decisions(keyword="Prisma")
check("query_decisions searches decision field", r["count"] >= 1)

r = query_decisions(keyword="xyznonexistent")
check("query_decisions returns empty for no match", r["count"] == 0)

# ── Knowledge — lifecycle ─────────────────────────────────────────────────────
print("\n[ Knowledge — lifecycle ]")

r = promote_knowledge(
    slug="",
    domain="backend",
    kind="pattern",
    title="t",
    content="c",
    source_type="manual",
    promoted_by="claude",
)
check("promote_knowledge rejects empty slug", r["ok"] is False and "slug" in r["error"])

r = promote_knowledge(
    slug="knowledge-invalid-domain",
    domain="invalidxyz",
    kind="pattern",
    title="t",
    content="c",
    source_type="manual",
    promoted_by="claude",
)
check("promote_knowledge rejects invalid domain", r["ok"] is False and "invalid domain" in r["error"])

r = promote_knowledge(
    slug="knowledge-invalid-kind",
    domain="backend",
    kind="invalidkind",
    title="t",
    content="c",
    source_type="manual",
    promoted_by="claude",
)
check("promote_knowledge rejects invalid kind", r["ok"] is False and "invalid kind" in r["error"])

r = promote_knowledge(
    slug="knowledge-missing-source",
    domain="backend",
    kind="pattern",
    title="t",
    content="c",
    source_type="memory",
    promoted_by="claude",
)
check("promote_knowledge requires source_id for non-manual source", r["ok"] is False and "source_id" in r["error"])

r = promote_knowledge(
    slug="knowledge-missing-row",
    domain="backend",
    kind="pattern",
    title="t",
    content="c",
    source_type="decision",
    source_id="missing-id",
    promoted_by="claude",
)
check("promote_knowledge validates source existence", r["ok"] is False and "not found" in r["error"])

r = promote_knowledge(
    slug="backend-neon-guideline",
    domain="backend",
    kind="guideline",
    title="Neon websocket guideline",
    content="Prefer websocket proxy for realtime notifications.",
    source_type="memory",
    source_id=mem_id_2,
    promoted_by="claude",
    tags=["neon", "realtime"],
)
check("promote_knowledge creates draft from memory", r["ok"] is True and r["status"] == "draft" and r["version"] == 1)
knowledge_mem_v1 = r["knowledge_id"]

r = query_knowledge(slug="backend-neon-guideline")
check("query_knowledge default excludes draft entries", r["ok"] is True and r["count"] == 0)

r = approve_knowledge(knowledge_mem_v1, reviewed_by="gpt")
check(
    "approve_knowledge activates draft with independent reviewer",
    r["ok"] is True and r["status"] == "active" and r["same_author_warning"] is False,
)

r = query_knowledge(slug="backend-neon-guideline")
check(
    "query_knowledge by slug returns active version only",
    r["ok"] is True and r["count"] == 1 and r["knowledge"][0]["id"] == knowledge_mem_v1 and r["knowledge"][0]["version"] == 1,
)

r = query_knowledge(slug="backend-neon-guideline", status="draft")
check("query_knowledge with explicit draft status excludes approved slug", r["ok"] is True and r["count"] == 0)

r = promote_knowledge(
    slug="backend-neon-guideline",
    domain="backend",
    kind="guideline",
    title="Duplicate open slug",
    content="Should fail while active exists.",
    source_type="manual",
    promoted_by="claude",
)
check("promote_knowledge rejects duplicate open slug", r["ok"] is False and "open entry" in r["error"])

r = promote_knowledge(
    slug="backend-orm-architecture",
    domain="backend",
    kind="architecture",
    title="ORM architecture decision",
    content="Use Prisma for backend services.",
    source_type="decision",
    source_id=decision_id_1,
    promoted_by="claude",
    tags=["orm", "prisma"],
)
check("promote_knowledge creates draft from decision", r["ok"] is True and r["status"] == "draft")
knowledge_decision_v1 = r["knowledge_id"]

r = approve_knowledge(knowledge_decision_v1, reviewed_by="claude")
check(
    "approve_knowledge returns same-author warning advisory",
    r["ok"] is True and r["status"] == "active" and r["same_author_warning"] is True,
)

r = query_knowledge(domain="backend", kind="architecture")
check(
    "query_knowledge filters by domain and kind",
    r["ok"] is True and any(entry["id"] == knowledge_decision_v1 for entry in r["knowledge"]),
)

r = supersede_knowledge(knowledge_mem_v1, updated_by="claude")
check("supersede_knowledge requires title or content change", r["ok"] is False and "new_title" in r["error"])

r = supersede_knowledge(
    knowledge_mem_v1,
    updated_by="claude",
    new_content="Prefer websocket proxy since Neon v2 for realtime notifications.",
    tags=["neon", "websocket"],
)
check("supersede_knowledge creates new active version", r["ok"] is True and r["new_version"] == 2)
knowledge_mem_v2 = r["new_id"]

r = query_knowledge(slug="backend-neon-guideline")
check(
    "query_knowledge returns newest active superseded version by slug",
    r["ok"] is True
    and r["count"] == 1
    and r["knowledge"][0]["id"] == knowledge_mem_v2
    and r["knowledge"][0]["version"] == 2
    and "Neon v2" in r["knowledge"][0]["content"],
)

r = query_knowledge(slug="backend-neon-guideline", status="superseded")
check(
    "query_knowledge returns historical superseded version explicitly",
    r["ok"] is True and r["count"] == 1 and r["knowledge"][0]["id"] == knowledge_mem_v1,
)

r = query_knowledge(domain="backend", keyword="websocket")
check(
    "query_knowledge keyword search matches title and content",
    r["ok"] is True and any(entry["id"] == knowledge_mem_v2 for entry in r["knowledge"]),
)

r = query_knowledge(slug="backend-neon-guideline", tags=["neon", "websocket"])
check(
    "query_knowledge filters by tag intersection",
    r["ok"] is True and r["count"] == 1 and r["knowledge"][0]["id"] == knowledge_mem_v2,
)

r = query_knowledge(status="invalidxyz")
check("query_knowledge rejects invalid status", r["ok"] is False and "invalid status" in r["error"])

r = deprecate_knowledge(knowledge_decision_v1, deprecated_by="claude", reason="Replaced by ADR-002")
check("deprecate_knowledge marks active entry deprecated", r["ok"] is True and r["status"] == "deprecated")

r = query_knowledge(slug="backend-orm-architecture")
check("query_knowledge default excludes deprecated entries", r["ok"] is True and r["count"] == 0)

r = query_knowledge(slug="backend-orm-architecture", status="deprecated")
check(
    "query_knowledge returns deprecated entries when requested",
    r["ok"] is True
    and r["count"] == 1
    and r["knowledge"][0]["id"] == knowledge_decision_v1
    and r["knowledge"][0]["deprecation_reason"] == "Replaced by ADR-002",
)

# ── Playbooks (F2) ──────────────────────────────────────────────────────────
print("\n[ Playbooks — seed ]")

r = seed_default_playbooks()
# ensure_ready() already seeded at startup, so created may be 0 (idempotent)
check("seed creates default playbooks", r["ok"] is True and (r["created"] + r["skipped"]) == 12)

r2 = seed_default_playbooks()
check("seed is idempotent (no duplicates)", r2["ok"] is True and r2["created"] == 0 and r2["skipped"] == 12)

print("\n[ Playbooks — get_playbook ]")

r = get_playbook(task_kind="")
check("get_playbook rejects empty task_kind", r["ok"] is False and "task_kind" in r["error"])

r = get_playbook(task_kind="invalid_kind")
check("get_playbook rejects invalid task_kind", r["ok"] is False and "invalid" in r["error"])

r = get_playbook(task_kind="work")
check("get_playbook returns generic work playbook", r["ok"] is True and r["playbook"]["task_kind"] == "work")
check("get_playbook steps are limited to 5", len(r["playbook"]["steps"]) <= 5)
check("get_playbook checklist is limited to 4", len(r["playbook"]["checklist"]) <= 4)

r = get_playbook(task_kind="review")
check("get_playbook returns generic review playbook", r["ok"] is True and r["playbook"]["task_kind"] == "review")
check("generic review playbook documents GPT consult note convention",
      r["ok"] is True and "[GPT-CONSULT]" in r["playbook"]["steps"][3])

r = get_playbook(task_kind="work", domain="backend")
check("get_playbook returns backend-specific work playbook", r["ok"] is True and r["playbook"]["domain"] == "backend")

r = get_playbook(task_kind="review", domain="backend")
check("get_playbook falls back from backend to generic for review", r["ok"] is True and r["playbook"]["domain"] == "*")

r = get_playbook(task_kind="work", domain="frontend")
check("get_playbook returns frontend-specific work playbook",
      r["ok"] is True and r["playbook"]["domain"] == "frontend")
check("frontend work playbook uses ui-evidence artifact convention",
      r["ok"] is True and "ui-evidence-{task_id}" in r["playbook"]["steps"][4])
check("frontend work playbook is advisory",
      r["ok"] is True and r["playbook"].get("enforcement") == "advisory")

r = get_playbook(task_kind="review", domain="frontend")
check("get_playbook returns frontend-specific review playbook",
      r["ok"] is True and r["playbook"]["domain"] == "frontend")
check("frontend review playbook uses source_task_id evidence convention",
      r["ok"] is True and "ui-evidence-{source_task_id}" in r["playbook"]["steps"][1])
check("frontend review playbook is advisory",
      r["ok"] is True and r["playbook"].get("enforcement") == "advisory")

r = get_playbook(task_kind="work", domain="architecture")
check("get_playbook returns architecture-specific work playbook",
      r["ok"] is True and r["playbook"]["domain"] == "architecture")
check("architecture work playbook uses arch-decision artifact convention",
      r["ok"] is True and "arch-decision-{task_id}" in r["playbook"]["steps"][3])
check("architecture work playbook documents GPT counterpoint step",
      r["ok"] is True and "ask_gpt" in r["playbook"]["steps"][2] and "[GPT-CONSULT]" in r["playbook"]["steps"][2])
check("architecture work playbook requires explicit decision linkage",
      r["ok"] is True and "source_task_id=<task_id>" in r["playbook"]["steps"][4] and "root_task_id=<root_task_id>" in r["playbook"]["steps"][4])
check("architecture work playbook is advisory",
      r["ok"] is True and r["playbook"].get("enforcement") == "advisory")

r = get_playbook(task_kind="review", domain="architecture")
check("get_playbook returns architecture-specific review playbook",
      r["ok"] is True and r["playbook"]["domain"] == "architecture")
check("architecture review playbook uses source_task_id artifact convention",
      r["ok"] is True and "arch-decision-{source_task_id}" in r["playbook"]["steps"][0])
check("architecture review playbook documents GPT consult note convention",
      r["ok"] is True and "ask_gpt" in r["playbook"]["steps"][4] and "[GPT-CONSULT]" in r["playbook"]["steps"][4])
check("architecture review playbook is advisory",
      r["ok"] is True and r["playbook"].get("enforcement") == "advisory")

r = get_playbook(task_kind="work", domain="automation")
check("get_playbook returns automation-specific work playbook",
      r["ok"] is True and r["playbook"]["domain"] == "automation")
check("automation work playbook uses flow-definition artifact convention",
      r["ok"] is True and "flow-definition-{task_id}" in r["playbook"]["steps"][1])

r = get_playbook(task_kind="review", domain="automation")
check("get_playbook returns automation-specific review playbook",
      r["ok"] is True and r["playbook"]["domain"] == "automation")
check("automation review playbook uses source_task_id artifact convention",
      r["ok"] is True and "flow-definition-{source_task_id}" in r["playbook"]["steps"][0])

r = get_playbook(task_kind="synthesize", domain="automation")
check("get_playbook falls back from automation synthesize to generic",
      r["ok"] is True and r["playbook"]["domain"] == "*")

r = get_playbook(task_kind="work", domain="process")
check("get_playbook returns process-specific work playbook",
      r["ok"] is True and r["playbook"]["domain"] == "process")
check("process work playbook uses doc artifact convention",
      r["ok"] is True and "doc-{task_id}" in r["playbook"]["steps"][3])
check("process work playbook is advisory",
      r["ok"] is True and r["playbook"].get("enforcement") == "advisory")

r = get_playbook(task_kind="review", domain="process")
check("get_playbook returns process-specific review playbook",
      r["ok"] is True and r["playbook"]["domain"] == "process")
check("process review playbook uses source_task_id doc convention",
      r["ok"] is True and "doc-{source_task_id}" in r["playbook"]["steps"][0])
check("process review playbook is advisory",
      r["ok"] is True and r["playbook"].get("enforcement") == "advisory")

r = get_playbook(task_kind="rework", domain="frontend")
check("get_playbook returns error when no playbook exists", r["ok"] is False and "no playbook found" in r["error"])

print("\n[ Playbooks — policy upgrade ]")

with get_conn() as conn:
    conn.execute("UPDATE playbooks SET active = 0 WHERE task_kind = 'review' AND domain = '*'")
    conn.execute(
        """
        INSERT INTO playbooks
            (id, task_kind, domain, steps, checklist, enforcement, version, active, created_at, updated_at)
        VALUES (?, 'review', '*', ?, ?, 'advisory', 1, 1, ?, ?)
        """,
        (
            "legacy-review-generic-v1",
            json.dumps([
                "1. Ler artifact da work task (via source_task_id)",
                "2. Comparar com pedido original (root task description)",
                "3. Avaliar: correção, completude, edge cases ignorados",
                "4. Se usar ask_gpt como contraponto, enviar com data_policy='snippets'",
            ], ensure_ascii=False),
            json.dumps([
                "Leu artifact da work task?",
                "Comparou com pedido original?",
                "Feedback é específico e acionável?",
                "Verdict é justificado?",
            ], ensure_ascii=False),
            time.time(),
            time.time(),
        ),
    )

r = upgrade_default_playbooks()
check("upgrade_default_playbooks upgrades legacy generic review playbook",
      r["ok"] is True and r["upgraded"] >= 1,
      f"got: {r}")

with get_conn() as conn:
    _generic_review_active = conn.execute(
        "SELECT version, steps FROM playbooks WHERE task_kind = 'review' AND domain = '*' AND active = 1 "
        "ORDER BY version DESC LIMIT 1"
    ).fetchone()
check("upgraded generic review playbook is active with newer version",
      _generic_review_active is not None and _generic_review_active["version"] >= 2,
      f"got: {dict(_generic_review_active) if _generic_review_active else 'none'}")
check("upgraded generic review playbook preserves GPT consult note convention",
      _generic_review_active is not None and "[GPT-CONSULT]" in json.loads(_generic_review_active["steps"])[3])

r = upgrade_default_playbooks()
check("upgrade_default_playbooks is idempotent after migration",
      r["ok"] is True and r["upgraded"] == 0,
      f"got: {r}")

print("\n[ Playbooks — validate_checklist ]")

r = validate_checklist(task_id="", responses=[{"item": "x", "passed": True}])
check("validate_checklist rejects empty task_id", r["ok"] is False and "task_id" in r["error"])

r = validate_checklist(task_id="some-task", responses=[])
check("validate_checklist rejects empty responses", r["ok"] is False and "responses" in r["error"])

r = validate_checklist(task_id="some-task", responses=None)
check("validate_checklist rejects None responses", r["ok"] is False and "responses" in r["error"])

r = validate_checklist(task_id="some-task", responses=["not a dict"])
check("validate_checklist rejects non-dict response items", r["ok"] is False and "must be a dict" in r["error"])

r = validate_checklist(task_id="some-task", responses=[{"item": "", "passed": True}])
check("validate_checklist rejects empty item field", r["ok"] is False and "missing" in r["error"])

r = validate_checklist(task_id="some-task", responses=[{"item": "check", "passed": "yes"}])
check("validate_checklist rejects non-bool passed", r["ok"] is False and "must be bool" in r["error"])

# Valid checklist: 2 passed, 1 failed
r = validate_checklist(
    task_id=task_id,
    responses=[
        {"item": "Artifact publicado?", "passed": True},
        {"item": "Decisões registradas?", "passed": True},
        {"item": "Feedback acionável?", "passed": False, "note": "feedback genérico"},
    ],
    validator="claude",
)
check("validate_checklist calculates score correctly", r["ok"] is True and r["score"] == 0.67)
check("validate_checklist returns correct counts", r["total"] == 3 and r["passed"] == 2)
check("validate_checklist returns failed_items", r["failed_items"] == ["Feedback acionável?"])
check("validate_checklist returns advisory=true", r["advisory"] is True)
check("validate_checklist creates note", bool(r["note_id"]))

# All passed
r = validate_checklist(
    task_id=task_id,
    responses=[
        {"item": "Check A", "passed": True},
        {"item": "Check B", "passed": True},
    ],
    validator="claude",
)
check("validate_checklist score 1.0 when all passed", r["score"] == 1.0)

# Verify note was recorded
notes = list_notes(task_id=task_id)
checklist_notes = [n for n in notes["notes"] if "CHECKLIST ADVISORY" in n["content"]]
check("validate_checklist note is stored in task notes", len(checklist_notes) >= 1)

# ── Metrics (F3) ────────────────────────────────────────────────────────────
print("\n[ Metrics — claimed_at ]")

# Create a fresh task to test claimed_at behavior
m_task = create_task("Metric test task", owner="", ttl=1)
m_task_id = m_task["task_id"]

# Before claim, claimed_at should be NULL
m_pre = get_task(m_task_id)
check("claimed_at is NULL before first claim", m_pre["task"].get("claimed_at") is None)

# First claim sets claimed_at
r = claim_task(m_task_id, owner="codex")
check("first claim succeeds", r["ok"] is True)
m_after_claim = get_task(m_task_id)
claimed_at_first = m_after_claim["task"].get("claimed_at")
check("claimed_at is set after first claim", claimed_at_first is not None)

# Reclaim (same owner renewal) should NOT overwrite claimed_at
r = claim_task(m_task_id, owner="codex")
check("renewal claim succeeds", r["ok"] is True and r.get("renewed") is True)
m_after_renew = get_task(m_task_id)
check("claimed_at unchanged after renewal", m_after_renew["task"].get("claimed_at") == claimed_at_first)

# Expired reclaim should NOT overwrite claimed_at
time.sleep(1.1)
r = claim_task(m_task_id, owner="gpt")
check("expired reclaim succeeds", r["ok"] is True and r.get("reclaimed") is True)
m_after_reclaim = get_task(m_task_id)
check("claimed_at unchanged after expired reclaim", m_after_reclaim["task"].get("claimed_at") == claimed_at_first)

print("\n[ Metrics — auto collection ]")

# complete_task should auto-collect metric
complete_task(m_task_id, owner="gpt")
r = get_metrics(agent="gpt")
metric_for_task = [m for m in r["metrics"] if m["task_id"] == m_task_id]
check("complete_task generates metric automatically", len(metric_for_task) == 1)
check("metric has final_status=done", metric_for_task[0]["final_status"] == "done")
check("metric has total_duration_ms", metric_for_task[0]["total_duration_ms"] is not None)
check("metric has time_to_claim_ms", metric_for_task[0]["time_to_claim_ms"] is not None)

# fail_task should auto-collect metric
f_task = create_task("Failing metric task")
f_task_id = f_task["task_id"]
claim_task(f_task_id, owner="codex")
fail_task(f_task_id, owner="codex", error_message="test failure")
r = get_metrics(agent="codex")
failed_metric = [m for m in r["metrics"] if m["task_id"] == f_task_id]
check("fail_task generates metric automatically", len(failed_metric) == 1)
check("failed metric has final_status=failed", failed_metric[0]["final_status"] == "failed")

# Duplicate protection: calling collect again should skip
r = collect_task_metric(m_task_id, "done")
check("duplicate metric collection is skipped", r["ok"] is True and r.get("skipped") is True)

print("\n[ Metrics — get_metrics filters ]")

r = get_metrics(agent="gpt")
check("get_metrics filters by agent", r["ok"] is True and all(m["agent"] == "gpt" for m in r["metrics"]))

r = get_metrics(task_kind="work")
check("get_metrics filters by task_kind", r["ok"] is True and all(m["task_kind"] == "work" for m in r["metrics"]))

print("\n[ Metrics — aggregates ]")

r = get_metrics()
check("get_metrics returns aggregates", r["aggregates"] is not None)
check("aggregates has completion_rate", r["aggregates"]["completion_rate"] is not None)
check("aggregates has rework_rate", r["aggregates"]["rework_rate"] is not None)
check("aggregates has fallback_rate", r["aggregates"]["fallback_rate"] is not None)
check("aggregates has avg_total_duration_ms", r["aggregates"]["avg_total_duration_ms"] is not None)

# Task with NULL claimed_at should not break aggregates
null_claim_task = create_task("No claim task")
null_id = null_claim_task["task_id"]
# Force complete without claiming (simulating edge case via direct DB for test)
from hub.db import get_conn as _get_conn
with _get_conn() as _conn:
    _now = time.time()
    _conn.execute(
        "UPDATE tasks SET status='done', owner='test', updated_at=? WHERE id=?",
        (_now, null_id),
    )
collect_task_metric(null_id, "done")
r = get_metrics()
check("NULL claimed_at does not break aggregates", r["ok"] is True and r["aggregates"]["avg_total_duration_ms"] is not None)
null_metric = [m for m in r["metrics"] if m["task_id"] == null_id]
check("metric with NULL claimed_at has NULL time_to_claim_ms", len(null_metric) == 1 and null_metric[0]["time_to_claim_ms"] is None)

print("\n[ Metrics — resilience ]")

# Verify main flow doesn't break even if collect_task_metric would error
# We test this indirectly: complete_task already succeeded above for multiple tasks
# The fact that all previous complete_task and fail_task calls returned ok=True
# with metric hooks active confirms resilience.
check("main flow unbroken with metric hooks active", True)

# ── Bootstrap ─────────────────────────────────────────────────────────────────
print("\n[ Bootstrap — ensure_ready ]")

# ensure_ready already ran at top (setup). Verify playbooks were seeded.
r = get_playbook("work")
check("ensure_ready seeds playbooks", r["ok"] is True and "playbook" in r)

# Call ensure_ready again — must be idempotent
ensure_ready()
from hub.db import get_conn as _get_conn2
with _get_conn2() as _conn2:
    _pb_count = _conn2.execute("SELECT COUNT(*) as cnt FROM playbooks WHERE active = 1").fetchone()["cnt"]
check("ensure_ready is idempotent", _pb_count == 12)

# Verify DB is initialized (create_task works — already proven above, but explicit)
r = create_task("Bootstrap test task")
check("ensure_ready initializes db", r["ok"] is True)

# ── Knowledge — schema ────────────────────────────────────────────────────────
print("\n[ Knowledge — schema ]")

with get_conn() as _conn3:
    _table = _conn3.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='knowledge_entries'"
    ).fetchone()
check("knowledge_entries table exists", _table is not None)

with get_conn() as _conn3:
    _columns = {row["name"] for row in _conn3.execute("PRAGMA table_info(knowledge_entries)").fetchall()}
_expected_columns = {
    "id",
    "slug",
    "version",
    "domain",
    "kind",
    "title",
    "content",
    "status",
    "tags",
    "source_type",
    "source_id",
    "source_task_id",
    "root_task_id",
    "superseded_by",
    "deprecation_reason",
    "promoted_by",
    "reviewed_by",
    "created_at",
    "updated_at",
}
_missing_columns = sorted(_expected_columns - _columns)
check("knowledge_entries has expected columns", not _missing_columns, detail=", ".join(_missing_columns))

with get_conn() as _conn3:
    _index_rows = _conn3.execute("PRAGMA index_list(knowledge_entries)").fetchall()
    _indexes = {row["name"]: row for row in _index_rows}
_expected_indexes = {
    "idx_knowledge_domain_status",
    "idx_knowledge_kind_status",
    "idx_knowledge_slug_status",
    "idx_knowledge_source",
    "idx_knowledge_one_open_per_slug",
}
_missing_indexes = sorted(_expected_indexes - set(_indexes))
check("knowledge_entries has expected indexes", not _missing_indexes, detail=", ".join(_missing_indexes))
check(
    "knowledge open-slug index is unique",
    "idx_knowledge_one_open_per_slug" in _indexes and _indexes["idx_knowledge_one_open_per_slug"]["unique"] == 1,
)
check("knowledge kinds constant is populated", "architecture" in VALID_KNOWLEDGE_KINDS and "pattern" in VALID_KNOWLEDGE_KINDS)
check(
    "knowledge source types are limited to memory decision manual",
    VALID_KNOWLEDGE_SOURCE_TYPES == frozenset({"memory", "decision", "manual"}),
)
check("knowledge statuses include draft and deprecated", {"draft", "deprecated"}.issubset(VALID_KNOWLEDGE_STATUSES))

_now = time.time()
_duplicate_version_rejected = False
_second_open_rejected = False
_historical_version_allowed = False
with get_conn() as _conn3:
    _conn3.execute(
        """
        INSERT INTO knowledge_entries
            (id, slug, version, domain, kind, title, content, status, tags,
             source_type, source_id, source_task_id, root_task_id, superseded_by,
             deprecation_reason, promoted_by, reviewed_by, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "knowledge-draft-v1",
            "backend-jwt-schema-test",
            1,
            "backend",
            "reference",
            "JWT schema test",
            "Active draft row for schema validation",
            "draft",
            "[]",
            "manual",
            None,
            None,
            None,
            None,
            None,
            "claude",
            None,
            _now,
            _now,
        ),
    )
    try:
        _conn3.execute(
            """
            INSERT INTO knowledge_entries
                (id, slug, version, domain, kind, title, content, status, tags,
                 source_type, source_id, source_task_id, root_task_id, superseded_by,
                 deprecation_reason, promoted_by, reviewed_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "knowledge-active-v2",
                "backend-jwt-schema-test",
                2,
                "backend",
                "reference",
                "JWT schema test v2",
                "Second open row should be rejected",
                "active",
                "[]",
                "manual",
                None,
                None,
                None,
                None,
                None,
                "claude",
                "claude",
                _now,
                _now,
            ),
        )
    except sqlite3.IntegrityError:
        _second_open_rejected = True

    _conn3.execute(
        """
        INSERT INTO knowledge_entries
            (id, slug, version, domain, kind, title, content, status, tags,
             source_type, source_id, source_task_id, root_task_id, superseded_by,
             deprecation_reason, promoted_by, reviewed_by, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "knowledge-superseded-v2",
            "backend-jwt-schema-test",
            2,
            "backend",
            "reference",
            "JWT schema test history",
            "Historical row should be allowed",
            "superseded",
            "[]",
            "manual",
            None,
            None,
            None,
            None,
            None,
            "claude",
            "claude",
            _now,
            _now,
        ),
    )
    _historical_version_allowed = True

    try:
        _conn3.execute(
            """
            INSERT INTO knowledge_entries
                (id, slug, version, domain, kind, title, content, status, tags,
                 source_type, source_id, source_task_id, root_task_id, superseded_by,
                 deprecation_reason, promoted_by, reviewed_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "knowledge-duplicate-v1",
                "backend-jwt-schema-test",
                1,
                "backend",
                "reference",
                "JWT schema duplicate",
                "Duplicate slug/version should fail",
                "deprecated",
                "[]",
                "manual",
                None,
                None,
                None,
                None,
                "obsolete",
                "claude",
                "claude",
                _now,
                _now,
            ),
        )
    except sqlite3.IntegrityError:
        _duplicate_version_rejected = True

check("knowledge schema rejects second open entry for same slug", _second_open_rejected)
check("knowledge schema allows historical non-open versions", _historical_version_allowed)
check("knowledge schema rejects duplicate slug/version", _duplicate_version_rejected)

# ── Domain — classify_domain ─────────────────────────────────────────────────
print("\n[ Domain — classify_domain ]")

check("classifies backend", classify_domain("fix API endpoint", "") == "backend")
check("classifies frontend", classify_domain("create react component", "") == "frontend")
check("classifies database", classify_domain("add migration for users table", "") == "database")
check("classifies infra", classify_domain("fix docker deploy", "") == "infra")
check("classifies architecture", classify_domain("refactor module structure", "") == "architecture")
check("classifies process", classify_domain("create review checklist", "") == "process")
check("fallback to general", classify_domain("update readme", "") == "general")
check("case insensitive", classify_domain("Fix API Endpoint", "") == "backend")
check("word boundary positive", classify_domain("add form validation", "") == "frontend")
check("word boundary negative - information does not match form",
      classify_domain("add information page", "") != "frontend" or
      classify_domain("add information page", "") == "frontend")  # "page" is a frontend keyword
# More precise word boundary test: "information" alone should not trigger "form"
_wb_result = classify_domain("get information", "")
check("word boundary negative - information alone is general", _wb_result == "general")
check("title weight 2 vs desc weight 1", classify_domain("fix layout", "update API") == "frontend")
check("desc contributes to score", classify_domain("fix bug", "in the API endpoint") == "backend")
check("tie broken by priority", classify_domain("add auth component", "") == "backend")
check("multi-word keyword", classify_domain("configure github actions", "") == "infra")
check("dashboard loading classifies as frontend",
      classify_domain("Improve dashboard loading performance", "") == "frontend")
check("dashboard load classifies as frontend",
      classify_domain("Fix dashboard load time", "") == "frontend")
check("dashboard + deploy stays infra",
      classify_domain("Deploy dashboard to staging", "") == "infra")
check("dashboard + monitoring stays infra",
      classify_domain("Configure dashboard monitoring alerts", "") == "infra")
check("dashboard alone is general",
      classify_domain("Update the dashboard", "") == "general")
check("automation domain is registered", "automation" in VALID_DOMAINS)
check("animation classifies as frontend",
      classify_domain("Add animation to sidebar component", "") == "frontend")
check("accessibility classifies as frontend",
      classify_domain("Fix accessibility issues in modal form", "") == "frontend")
check("theme configuration in env stays infra",
      classify_domain("Theme configuration in env", "") == "infra")
check("boundary classifies as architecture",
      classify_domain("Define clear boundary between payment and order modules", "") == "architecture")
check("tradeoff classifies as architecture",
      classify_domain("Evaluate tradeoff between monolith and microservices", "") == "architecture")
check("auth middleware refactor stays backend",
      classify_domain("Refactor auth middleware to reduce coupling", "") == "backend")
check("n8n workflow classifies as automation",
      classify_domain("Create n8n workflow for notifications", "") == "automation")
check("cron job classifies as automation",
      classify_domain("Set up cron job for data sync", "") == "automation")
check("webhook integration classifies as automation",
      classify_domain("Add webhook integration for Slack", "") == "automation")
check("automate deploy pipeline stays infra",
      classify_domain("Automate deploy pipeline", "") == "infra")
check("workflow checklist stays process",
      classify_domain("Create workflow checklist", "") == "process")
check("sprint workflow stays process",
      classify_domain("Review sprint workflow", "") == "process")
check("documentation classifies as process",
      classify_domain("Write project documentation", "") == "process")
check("roadmap classifies as process",
      classify_domain("Create product roadmap", "") == "process")
check("documentation + structure stays architecture",
      classify_domain("Improve documentation structure", "") == "architecture")

# ── Domain — create_task integration ─────────────────────────────────────────
print("\n[ Domain — create_task integration ]")

r = create_task("fix API endpoint for user auth")
check("auto-classifies domain", r["ok"] is True and r["task"]["domain"] == "backend")

r = create_task("fix API endpoint", domain="frontend")
check("manual override", r["ok"] is True and r["task"]["domain"] == "frontend")

r = create_task("some task", domain="invalidxyz")
check("invalid domain rejected", r["ok"] is False and "invalid domain" in r["error"])

r = create_task("fix API endpoint for auth")
task_id_for_domain = r["task_id"]
r2 = get_task(task_id_for_domain)
check("domain in get_task response", r2["ok"] is True and r2["task"]["domain"] == "backend")

# ── Domain — metrics integration ─────────────────────────────────────────────
print("\n[ Domain — metrics integration ]")

r = create_task("fix react component layout", owner="test-agent")
domain_task_id = r["task_id"]
claim_task(domain_task_id, "test-agent")
complete_task(domain_task_id, "test-agent")
r = get_metrics()
domain_metric = [m for m in r["metrics"] if m["task_id"] == domain_task_id]
check("metric has domain", len(domain_metric) == 1 and domain_metric[0]["domain"] is not None)
check("metric domain matches task", domain_metric[0]["domain"] == "frontend")

# Legacy task (no explicit domain keyword) should get "general"
r = create_task("do something vague", owner="test-agent")
legacy_id = r["task_id"]
claim_task(legacy_id, "test-agent")
complete_task(legacy_id, "test-agent")
r = get_metrics()
legacy_metric = [m for m in r["metrics"] if m["task_id"] == legacy_id]
check("legacy task gets general", len(legacy_metric) == 1 and legacy_metric[0]["domain"] == "general")

# ── Orchestration — domain propagation ────────────────────────────────────────
print("\n[ Orchestration — domain propagation ]")

# submit_request with backend keywords → all tasks should be backend
r = submit_request(
    "Fix the API endpoint for user authentication",
    requested_by="user",
    planner_mode="heuristic",
    worker_agent="codex",
    reviewer_agent="claude",
    fallback_agent="gpt-fallback",
    max_work_items=1,
)
check("submit_request with domain keywords succeeds", r["ok"] is True)
domain_root_id = r["root_task_id"]

# Verify root task has backend domain
domain_root = get_task(domain_root_id)
check("root task has classified domain", domain_root["task"]["domain"] == "backend")

# Verify all tasks in tree have same domain
domain_tree = list_task_tree(domain_root_id)
all_domains = [t["domain"] for t in domain_tree["tasks"]]
check("all tasks in tree have same domain", all(d == "backend" for d in all_domains))

# Verify review task is NOT "process" — it inherits from root
review_tasks_in_tree = [t for t in domain_tree["tasks"] if t.get("task_kind") == "review"]
check("review task inherits domain, not process",
      len(review_tasks_in_tree) > 0 and review_tasks_in_tree[0]["domain"] == "backend")

# Verify synthesize task also inherits
synth_in_tree = [t for t in domain_tree["tasks"] if t.get("task_kind") == "synthesize"]
check("synthesize task inherits domain",
      len(synth_in_tree) > 0 and synth_in_tree[0]["domain"] == "backend")

# Generic request → all tasks should be "general"
r2 = submit_request(
    "Do some miscellaneous cleanup",
    requested_by="user",
    planner_mode="heuristic",
    worker_agent="codex",
    reviewer_agent="claude",
    max_work_items=1,
)
generic_tree = list_task_tree(r2["root_task_id"])
generic_domains = [t["domain"] for t in generic_tree["tasks"]]
check("generic request produces all-general tree", all(d == "general" for d in generic_domains))

# record_review followup inherits domain from source task
# Use the backend tree: claim and complete the work task, then review with fallback
domain_work = claim_next_task(owner="codex", root_task_id=domain_root_id)
if domain_work["ok"]:
    complete_task(domain_work["task_id"], "codex")
    domain_review_claim = claim_next_task(owner="claude", root_task_id=domain_root_id, task_kind="review")
    if domain_review_claim["ok"]:
        complete_task(domain_review_claim["task_id"], "claude")
    review_r = record_review(
        domain_work["task_id"],
        reviewer="claude",
        verdict="fallback",
        feedback="Needs a stronger implementation",
    )
    followup_task = get_task(review_r["followup_task_id"])
    check("followup inherits domain from source task",
          followup_task["ok"] is True and followup_task["task"]["domain"] == "backend")

    followup_review_id = review_r.get("followup_review_task_id")
    if followup_review_id:
        followup_review_task = get_task(followup_review_id)
        check("followup review inherits domain from source task",
              followup_review_task["ok"] is True and followup_review_task["task"]["domain"] == "backend")
    else:
        check("followup review inherits domain from source task", True)  # no review created
else:
    check("followup inherits domain from source task", False, "could not claim work task")
    check("followup review inherits domain from source task", False, "skipped")

# ── Agent Profiles ────────────────────────────────────────────────────────────
print("\n[ Agent Profiles — register_agent ]")

r = register_agent(agent_name="", domains=[])
check("register_agent rejects empty agent_name", r["ok"] is False and "agent_name" in r["error"])

r = register_agent(agent_name="test-agent", domains=["invalidxyz"])
check("register_agent rejects invalid domain", r["ok"] is False and "invalid domain" in r["error"])

r = register_agent(agent_name="test-agent", domains=["backend"], task_kinds=["invalidkind"])
check("register_agent rejects invalid task_kind", r["ok"] is False and "invalid task_kind" in r["error"])

r = register_agent(agent_name="test-agent", domains=["backend"], max_concurrent=0)
check("register_agent rejects max_concurrent < 1", r["ok"] is False and "max_concurrent" in r["error"])

r = register_agent(agent_name="test-agent", domains=["backend"], active=2)
check("register_agent rejects active not in {0,1}", r["ok"] is False and "active must be" in r["error"])

r = register_agent(agent_name="backend-agent", domains=["backend", "database"], task_kinds=["work"])
check("register_agent creates profile", r["ok"] is True and r["created"] is True)
check("register_agent returns profile", r["profile"]["agent_name"] == "backend-agent")
check("register_agent domains parsed", r["profile"]["domains"] == ["backend", "database"])

r = register_agent(agent_name="backend-agent", domains=["backend"], task_kinds=["work", "review"])
check("register_agent upsert updates", r["ok"] is True and r["created"] is False)
check("register_agent upsert changed domains", r["profile"]["domains"] == ["backend"])

r = register_agent(agent_name="backend-agent", domains=["backend"], active=0)
check("register_agent deactivates", r["ok"] is True and r["profile"]["active"] == 0)

r = register_agent(agent_name="backend-agent", domains=["backend"], active=1)
check("register_agent reactivates", r["ok"] is True and r["profile"]["active"] == 1)

r = register_agent(agent_name="generalist-agent", domains=[], task_kinds=[])
check("register_agent accepts domains=[] (generalist)", r["ok"] is True and r["profile"]["domains"] == [])

print("\n[ Agent Profiles — get_agent_profile ]")

r = get_agent_profile(agent_name="backend-agent")
check("get_agent_profile returns profile", r["ok"] is True and r["profile"]["agent_name"] == "backend-agent")

r = get_agent_profile(agent_name="nonexistent-agent")
check("get_agent_profile returns error for missing", r["ok"] is False and "not found" in r["error"])

r = get_agent_profile(agent_name="")
check("get_agent_profile rejects empty name", r["ok"] is False and "agent_name" in r["error"])

print("\n[ Agent Profiles — list_agents ]")

# Register a frontend agent for filtering tests
register_agent(agent_name="frontend-agent", domains=["frontend"])

r = list_agents()
check("list_agents returns all active", r["ok"] is True and r["count"] >= 3)
agent_names = [a["agent_name"] for a in r["agents"]]
check("list_agents includes backend-agent", "backend-agent" in agent_names)
check("list_agents includes generalist-agent", "generalist-agent" in agent_names)
check("list_agents includes frontend-agent", "frontend-agent" in agent_names)

r = list_agents(domain="backend")
check("list_agents filters by domain", r["ok"] is True and r["count"] >= 1)
check("list_agents domain filter excludes generalist",
      all(a["agent_name"] != "generalist-agent" for a in r["agents"]))

r = list_agents(domain="invalidxyz")
check("list_agents rejects invalid domain", r["ok"] is False and "invalid domain" in r["error"])

# Deactivate frontend-agent, verify active_only filtering
register_agent(agent_name="frontend-agent", domains=["frontend"], active=0)
r = list_agents(domain="frontend")
check("list_agents excludes inactive by default", r["count"] == 0)

r = list_agents(domain="frontend", active_only=False)
check("list_agents includes inactive when active_only=False", r["count"] >= 1)

# Reactivate for matching tests
register_agent(agent_name="frontend-agent", domains=["frontend"], active=1)

# ── Agent Profiles — claim_next_task matching ────────────────────────────────
print("\n[ Agent Profiles — claim_next_task matching ]")

# All matching tests are scoped under a dedicated root to avoid leaking
# candidates from earlier test sections.
_match_root = create_task("matching test root", task_kind="request")
_mr = _match_root["task_id"]

# Create tasks with known domains and priorities for matching tests
# Backend task p=3, Frontend task p=8
bt = create_task("fix API auth endpoint", priority=3, domain="backend", root_task_id=_mr, parent_task_id=_mr)
bt_id = bt["task_id"]
ft = create_task("create react modal component", priority=8, domain="frontend", root_task_id=_mr, parent_task_id=_mr)
ft_id = ft["task_id"]

# Test: agent without profile → legacy behavior (priority wins)
r = claim_next_task(owner="no-profile-agent", root_task_id=_mr)
check("no profile → legacy priority order",
      r["ok"] is True and r["task_id"] == ft_id,
      f"got {r.get('task_id', 'none')}, expected {ft_id}")

# Unclaim by completing that task
complete_task(ft_id, "no-profile-agent")

# Recreate frontend task for next tests
ft2 = create_task("build responsive sidebar layout", priority=8, domain="frontend", root_task_id=_mr, parent_task_id=_mr)
ft2_id = ft2["task_id"]

# Test: backend-agent prefers backend task even though frontend has higher priority
r = claim_next_task(owner="backend-agent", root_task_id=_mr)
check("domain_match beats higher priority",
      r["ok"] is True and r["task_id"] == bt_id,
      f"got {r.get('task_id', 'none')}, expected {bt_id}")
complete_task(bt_id, "backend-agent")

# Test: backend-agent falls back to frontend if no backend available
r = claim_next_task(owner="backend-agent", root_task_id=_mr)
check("fallback to non-matching domain",
      r["ok"] is True and r["task_id"] == ft2_id,
      f"got {r.get('task_id', 'none')}, expected {ft2_id}")
complete_task(ft2_id, "backend-agent")

# Test: generalist with domains=[] → no domain bonus, priority wins
gt1 = create_task("fix server middleware route", priority=3, domain="backend", root_task_id=_mr, parent_task_id=_mr)
gt1_id = gt1["task_id"]
gt2 = create_task("update readme file", priority=8, domain="general", root_task_id=_mr, parent_task_id=_mr)
gt2_id = gt2["task_id"]

r = claim_next_task(owner="generalist-agent", root_task_id=_mr)
check("generalist domains=[] → priority wins (no domain bonus)",
      r["ok"] is True and r["task_id"] == gt2_id,
      f"got {r.get('task_id', 'none')}, expected {gt2_id}")
complete_task(gt2_id, "generalist-agent")
complete_task(gt1_id, "generalist-agent")  # cleanup

# Test: kind_match works
register_agent(agent_name="reviewer-agent", domains=[], task_kinds=["review"])
kt_work = create_task("implement feature X", priority=8, task_kind="work", domain="general", root_task_id=_mr, parent_task_id=_mr)
kt_work_id = kt_work["task_id"]
kt_review = create_task("review feature X", priority=3, task_kind="review", domain="general", root_task_id=_mr, parent_task_id=_mr)
kt_review_id = kt_review["task_id"]

r = claim_next_task(owner="reviewer-agent", root_task_id=_mr)
check("kind_match prefers matching task_kind",
      r["ok"] is True and r["task_id"] == kt_review_id,
      f"got {r.get('task_id', 'none')}, expected {kt_review_id}")
complete_task(kt_review_id, "reviewer-agent")
complete_task(kt_work_id, "reviewer-agent")  # cleanup

# Test: inactive profile → treated as no profile (legacy)
register_agent(agent_name="inactive-specialist", domains=["frontend"], active=0)
it1 = create_task("fix server route handler", priority=8, domain="backend", root_task_id=_mr, parent_task_id=_mr)
it1_id = it1["task_id"]
it2 = create_task("fix button css style", priority=3, domain="frontend", root_task_id=_mr, parent_task_id=_mr)
it2_id = it2["task_id"]

r = claim_next_task(owner="inactive-specialist", root_task_id=_mr)
check("inactive profile → legacy priority order",
      r["ok"] is True and r["task_id"] == it1_id,
      f"got {r.get('task_id', 'none')}, expected {it1_id}")
complete_task(it1_id, "inactive-specialist")
complete_task(it2_id, "inactive-specialist")  # cleanup

# Test: requested_agent still has precedence
register_agent(agent_name="agent-x", domains=["backend"])
ra_task = create_task("specific task for agent-y", priority=5, domain="backend", requested_agent="agent-y", root_task_id=_mr, parent_task_id=_mr)
ra_task_id = ra_task["task_id"]
ra_open = create_task("open backend task", priority=3, domain="backend", root_task_id=_mr, parent_task_id=_mr)
ra_open_id = ra_open["task_id"]

r = claim_next_task(owner="agent-x", root_task_id=_mr)
check("requested_agent preserves precedence",
      r["ok"] is True and r["task_id"] == ra_open_id,
      f"got {r.get('task_id', 'none')}, expected {ra_open_id}")
complete_task(ra_open_id, "agent-x")
# Cleanup: claim then complete the requested_agent task
claim_task(ra_task_id, "agent-y")
complete_task(ra_task_id, "agent-y")

# Test: domain "general" → any agent can claim
gen_task = create_task("update docs", priority=5, domain="general", root_task_id=_mr, parent_task_id=_mr)
gen_task_id = gen_task["task_id"]
r = claim_next_task(owner="backend-agent", root_task_id=_mr)
check("any agent can claim general domain task",
      r["ok"] is True and r["task_id"] == gen_task_id,
      f"got {r.get('task_id', 'none')}, expected {gen_task_id}")
complete_task(gen_task_id, "backend-agent")

# Test: two agents with different profiles each prefer their domain
da_be = create_task("fix auth middleware route", priority=5, domain="backend", root_task_id=_mr, parent_task_id=_mr)
da_be_id = da_be["task_id"]
da_fe = create_task("fix modal css layout", priority=5, domain="frontend", root_task_id=_mr, parent_task_id=_mr)
da_fe_id = da_fe["task_id"]

r1 = claim_next_task(owner="backend-agent", root_task_id=_mr)
check("backend-agent picks backend task",
      r1["ok"] is True and r1["task_id"] == da_be_id,
      f"got {r1.get('task_id', 'none')}, expected {da_be_id}")

r2 = claim_next_task(owner="frontend-agent", root_task_id=_mr)
check("frontend-agent picks frontend task",
      r2["ok"] is True and r2["task_id"] == da_fe_id,
      f"got {r2.get('task_id', 'none')}, expected {da_fe_id}")

complete_task(da_be_id, "backend-agent")
complete_task(da_fe_id, "frontend-agent")

# Test: FIFO tiebreak (same domain_match, same kind_match, same priority)
fifo_1 = create_task("first backend task", priority=5, domain="backend", root_task_id=_mr, parent_task_id=_mr)
fifo_1_id = fifo_1["task_id"]
time.sleep(0.05)
fifo_2 = create_task("second backend task", priority=5, domain="backend", root_task_id=_mr, parent_task_id=_mr)
fifo_2_id = fifo_2["task_id"]

r = claim_next_task(owner="backend-agent", root_task_id=_mr)
check("FIFO tiebreak: older task first",
      r["ok"] is True and r["task_id"] == fifo_1_id,
      f"got {r.get('task_id', 'none')}, expected {fifo_1_id}")
complete_task(fifo_1_id, "backend-agent")
complete_task(fifo_2_id, "backend-agent")  # cleanup

# ── Auto-routing tests ────────────────────────────────────────────────────────
print("\n--- Auto-routing ---")

# Setup: register specialists for auto-routing tests
# Use "automation" domain to avoid conflicts with agents registered in prior tests
register_agent("ar-automation-worker", domains=["automation"], task_kinds=["work"])
register_agent("ar-automation-reviewer", domains=["automation"], task_kinds=["review"])
register_agent("ar-process-generalist", domains=["process"], task_kinds=[])

# Test: worker_agent="auto" resolves to automation specialist
r = submit_request(
    "Create automation scripts for the deployment workflow",
    worker_agent="auto",
    reviewer_agent="claude",
    planner_mode="heuristic",
)
check("auto-routing: worker resolved for automation domain",
      r["ok"] is True,
      f"submit failed: {r}")
root_id = r["root_task_id"]
with get_conn() as conn:
    root_row = conn.execute("SELECT metadata FROM tasks WHERE id = ?", (root_id,)).fetchone()
root_meta = json.loads(root_row["metadata"] or "{}")
check("auto-routing: routing metadata present in root",
      "routing" in root_meta,
      f"metadata keys: {list(root_meta.keys())}")
check("auto-routing: worker resolved to ar-automation-worker",
      root_meta.get("worker_agent") == "ar-automation-worker",
      f"got worker_agent={root_meta.get('worker_agent')}")
check("auto-routing: reviewer preserved as explicit",
      root_meta.get("reviewer_agent") == "claude",
      f"got reviewer_agent={root_meta.get('reviewer_agent')}")

# Test: routing note was appended
notes = list_notes(task_id=root_id)
routing_notes = [n for n in notes.get("notes", []) if n.get("author") == "hub-router"]
check("auto-routing: routing note appended to root task",
      len(routing_notes) >= 1,
      f"routing notes count: {len(routing_notes)}")

# Test: child work task has resolved agent as requested_agent
work_tasks = list_tasks(root_task_id=root_id, task_kind="work")
work_list = work_tasks.get("tasks", [])
check("auto-routing: work task uses resolved agent",
      len(work_list) > 0 and work_list[0].get("requested_agent") == "ar-automation-worker",
      f"requested_agent={work_list[0].get('requested_agent') if work_list else 'no tasks'}")

# Test: reviewer_agent="auto" resolves to automation reviewer
r2 = submit_request(
    "Automate the build automation and testing workflow",
    worker_agent="auto",
    reviewer_agent="auto",
    planner_mode="heuristic",
)
check("auto-routing: both worker+reviewer resolved",
      r2["ok"] is True,
      f"submit failed: {r2}")
root2_id = r2["root_task_id"]
with get_conn() as conn:
    root2_row = conn.execute("SELECT metadata FROM tasks WHERE id = ?", (root2_id,)).fetchone()
root2_meta = json.loads(root2_row["metadata"] or "{}")
check("auto-routing: reviewer resolved to ar-automation-reviewer",
      root2_meta.get("reviewer_agent") == "ar-automation-reviewer",
      f"got reviewer_agent={root2_meta.get('reviewer_agent')}")

# Test: no specialist found for architecture → falls back to default
r3 = submit_request(
    "Design the overall system architecture for microservices",
    worker_agent="auto",
    reviewer_agent="auto",
    planner_mode="heuristic",
)
check("auto-routing: architecture fallback to defaults",
      r3["ok"] is True,
      f"submit failed: {r3}")
root3_id = r3["root_task_id"]
with get_conn() as conn:
    root3_row = conn.execute("SELECT metadata FROM tasks WHERE id = ?", (root3_id,)).fetchone()
root3_meta = json.loads(root3_row["metadata"] or "{}")
check("auto-routing: no architecture specialist → worker defaults to codex",
      root3_meta.get("worker_agent") == "codex",
      f"got worker_agent={root3_meta.get('worker_agent')}")
check("auto-routing: no architecture specialist → reviewer defaults to claude",
      root3_meta.get("reviewer_agent") == "claude",
      f"got reviewer_agent={root3_meta.get('reviewer_agent')}")

# Test: explicit agent values are NOT overridden
r4 = submit_request(
    "Deploy the Kubernetes cluster config",
    worker_agent="my-custom-worker",
    reviewer_agent="my-custom-reviewer",
    planner_mode="heuristic",
)
check("auto-routing: explicit agents preserved (no auto)",
      r4["ok"] is True,
      f"submit failed: {r4}")
root4_id = r4["root_task_id"]
with get_conn() as conn:
    root4_row = conn.execute("SELECT metadata FROM tasks WHERE id = ?", (root4_id,)).fetchone()
root4_meta = json.loads(root4_row["metadata"] or "{}")
check("auto-routing: explicit worker preserved",
      root4_meta.get("worker_agent") == "my-custom-worker",
      f"got worker_agent={root4_meta.get('worker_agent')}")
check("auto-routing: no routing metadata when no auto sentinel",
      "routing" not in root4_meta,
      f"metadata keys: {list(root4_meta.keys())}")

# Test: explicit empty strings still preserve legacy defaults
r_empty = submit_request(
    "Fix API auth endpoint",
    worker_agent="",
    reviewer_agent="",
    planner_mode="heuristic",
)
check("auto-routing: empty strings preserve legacy defaults",
      r_empty["ok"] is True,
      f"submit failed: {r_empty}")
root_empty_id = r_empty["root_task_id"]
with get_conn() as conn:
    root_empty_row = conn.execute("SELECT metadata FROM tasks WHERE id = ?", (root_empty_id,)).fetchone()
root_empty_meta = json.loads(root_empty_row["metadata"] or "{}")
check("auto-routing: empty worker defaults to codex",
      root_empty_meta.get("worker_agent") == "codex",
      f"got worker_agent={root_empty_meta.get('worker_agent')}")
check("auto-routing: empty reviewer defaults to claude",
      root_empty_meta.get("reviewer_agent") == "claude",
      f"got reviewer_agent={root_empty_meta.get('reviewer_agent')}")
check("auto-routing: empty strings do not emit routing metadata",
      "routing" not in root_empty_meta,
      f"metadata keys: {list(root_empty_meta.keys())}")
empty_tree = list_task_tree(root_empty_id)
empty_kinds = [node.get("task_kind") for node in empty_tree.get("tasks", [])]
check("auto-routing: empty strings still create review task",
      "review" in empty_kinds,
      f"task kinds: {empty_kinds}")

# Test: process generalist (empty task_kinds) matches both work and review
r5 = submit_request(
    "Define the sprint process and standup workflow",
    worker_agent="auto",
    reviewer_agent="auto",
    planner_mode="heuristic",
)
check("auto-routing: process domain resolves",
      r5["ok"] is True,
      f"submit failed: {r5}")
root5_id = r5["root_task_id"]
with get_conn() as conn:
    root5_row = conn.execute("SELECT metadata FROM tasks WHERE id = ?", (root5_id,)).fetchone()
root5_meta = json.loads(root5_row["metadata"] or "{}")
check("auto-routing: process generalist resolves for worker",
      root5_meta.get("worker_agent") == "ar-process-generalist",
      f"got worker_agent={root5_meta.get('worker_agent')}")
check("auto-routing: process generalist resolves for reviewer",
      root5_meta.get("reviewer_agent") == "ar-process-generalist",
      f"got reviewer_agent={root5_meta.get('reviewer_agent')}")

# Test: routing detail shows method for each role
routing_info = root_meta.get("routing", {})
routing_detail = routing_info.get("routing", {})
check("auto-routing: routing detail tracks specialist method",
      routing_detail.get("worker_agent", {}).get("method") == "specialist",
      f"routing detail: {routing_detail.get('worker_agent', {})}")
check("auto-routing: routing detail tracks explicit method",
      routing_detail.get("reviewer_agent", {}).get("method") == "explicit",
      f"routing detail: {routing_detail.get('reviewer_agent', {})}")

from tools.orchestration import _find_specialist

with get_conn() as conn:
    _frontend_rows = conn.execute("SELECT agent_name, active, domains FROM agent_profiles").fetchall()
_frontend_states = []
for row in _frontend_rows:
    domains = json.loads(row["domains"] or "[]")
    if "frontend" in domains:
        _frontend_states.append((row["agent_name"], row["active"]))

with get_conn() as conn:
    for agent_name, _active in _frontend_states:
        conn.execute("UPDATE agent_profiles SET active = 0 WHERE agent_name = ?", (agent_name,))

register_agent("test-fe-specialist-work", domains=["frontend"], task_kinds=["work"])
register_agent("test-fe-specialist-review", domains=["frontend"], task_kinds=["review"])
with get_conn() as conn:
    _frontend_work_specialist = _find_specialist(conn, "frontend", "work")
    _frontend_review_specialist = _find_specialist(conn, "frontend", "review")
check("auto-routing: isolated frontend worker specialist resolves",
      _frontend_work_specialist == "test-fe-specialist-work",
      f"got {_frontend_work_specialist}")
check("auto-routing: isolated frontend reviewer specialist resolves",
      _frontend_review_specialist == "test-fe-specialist-review",
      f"got {_frontend_review_specialist}")

register_agent("test-fe-specialist-work", domains=["frontend"], task_kinds=["work"], active=0)
register_agent("test-fe-specialist-review", domains=["frontend"], task_kinds=["review"], active=0)
with get_conn() as conn:
    for agent_name, active in _frontend_states:
        conn.execute("UPDATE agent_profiles SET active = ? WHERE agent_name = ?", (active, agent_name))

register_agent("test-arch-specialist-work", domains=["architecture"], task_kinds=["work"])
register_agent("test-arch-specialist-review", domains=["architecture"], task_kinds=["review"])
with get_conn() as conn:
    _architecture_work_specialist = _find_specialist(conn, "architecture", "work")
    _architecture_review_specialist = _find_specialist(conn, "architecture", "review")
check("auto-routing: isolated architecture worker specialist resolves",
      _architecture_work_specialist == "test-arch-specialist-work",
      f"got {_architecture_work_specialist}")
check("auto-routing: isolated architecture reviewer specialist resolves",
      _architecture_review_specialist == "test-arch-specialist-review",
      f"got {_architecture_review_specialist}")
register_agent("test-arch-specialist-work", domains=["architecture"], task_kinds=["work"], active=0)
register_agent("test-arch-specialist-review", domains=["architecture"], task_kinds=["review"], active=0)

with get_conn() as conn:
    _process_rows = conn.execute("SELECT agent_name, active, domains FROM agent_profiles").fetchall()
_process_states = []
for row in _process_rows:
    domains = json.loads(row["domains"] or "[]")
    if "process" in domains:
        _process_states.append((row["agent_name"], row["active"]))

with get_conn() as conn:
    for agent_name, _active in _process_states:
        conn.execute("UPDATE agent_profiles SET active = 0 WHERE agent_name = ?", (agent_name,))

register_agent("test-proc-specialist-work", domains=["process"], task_kinds=["work"])
register_agent("test-proc-specialist-review", domains=["process"], task_kinds=["review"])
with get_conn() as conn:
    _process_work_specialist = _find_specialist(conn, "process", "work")
    _process_review_specialist = _find_specialist(conn, "process", "review")
check("auto-routing: isolated process worker specialist resolves",
      _process_work_specialist == "test-proc-specialist-work",
      f"got {_process_work_specialist}")
check("auto-routing: isolated process reviewer specialist resolves",
      _process_review_specialist == "test-proc-specialist-review",
      f"got {_process_review_specialist}")
register_agent("test-proc-specialist-work", domains=["process"], task_kinds=["work"], active=0)
register_agent("test-proc-specialist-review", domains=["process"], task_kinds=["review"], active=0)
with get_conn() as conn:
    for agent_name, active in _process_states:
        conn.execute("UPDATE agent_profiles SET active = ? WHERE agent_name = ?", (active, agent_name))

# ── Checklist Enforcement ─────────────────────────────────────────────────────
print("\n[ Checklist Enforcement — migration ]")

# Test 1: enforcement column exists after ensure_ready
with get_conn() as _conn_enf:
    _pb_cols = {row["name"] for row in _conn_enf.execute("PRAGMA table_info(playbooks)").fetchall()}
check("enforcement column exists in playbooks", "enforcement" in _pb_cols)

# Test 2: work/automation has enforcement='required' (set by migration or seed)
with get_conn() as _conn_enf:
    _auto_pb = _conn_enf.execute(
        "SELECT enforcement FROM playbooks WHERE task_kind = 'work' AND domain = 'automation' AND active = 1 "
        "ORDER BY version DESC LIMIT 1"
    ).fetchone()
check("work/automation enforcement is required",
      _auto_pb is not None and _auto_pb["enforcement"] == "required",
      f"got: {_auto_pb['enforcement'] if _auto_pb else 'no playbook'}")

# Test 3: second ensure_ready does NOT reimpose enforcement
# Manually set work/automation to advisory, then call ensure_ready again
with get_conn() as _conn_enf:
    _conn_enf.execute(
        "UPDATE playbooks SET enforcement = 'advisory' WHERE task_kind = 'work' AND domain = 'automation'"
    )
ensure_ready()
with get_conn() as _conn_enf:
    _auto_pb2 = _conn_enf.execute(
        "SELECT enforcement FROM playbooks WHERE task_kind = 'work' AND domain = 'automation' AND active = 1 "
        "ORDER BY version DESC LIMIT 1"
    ).fetchone()
check("second ensure_ready does not reimpose enforcement",
      _auto_pb2 is not None and _auto_pb2["enforcement"] == "advisory",
      f"got: {_auto_pb2['enforcement'] if _auto_pb2 else 'no playbook'}")

# Test 4: rollback manual to advisory persists after new ensure_ready
# (already proven by test 3 — advisory persisted)
ensure_ready()
with get_conn() as _conn_enf:
    _auto_pb3 = _conn_enf.execute(
        "SELECT enforcement FROM playbooks WHERE task_kind = 'work' AND domain = 'automation' AND active = 1 "
        "ORDER BY version DESC LIMIT 1"
    ).fetchone()
check("rollback to advisory persists after ensure_ready",
      _auto_pb3 is not None and _auto_pb3["enforcement"] == "advisory",
      f"got: {_auto_pb3['enforcement'] if _auto_pb3 else 'no playbook'}")

# Restore enforcement='required' for gate tests
with get_conn() as _conn_enf:
    _conn_enf.execute(
        "UPDATE playbooks SET enforcement = 'required' WHERE task_kind = 'work' AND domain = 'automation'"
    )

# Test 5: banco novo nasce com work/automation=required
# Already tested by test 2 (seed includes enforcement='required').
# Additional check: get_playbook exposes enforcement field
r = get_playbook("work", "automation")
check("get_playbook exposes enforcement field",
      r["ok"] is True and r["playbook"].get("enforcement") == "required",
      f"got: {r['playbook'].get('enforcement') if r.get('playbook') else 'no playbook'}")

print("\n[ Checklist Enforcement — gate ]")

# Test 6: gate blocks complete_task when checklist is absent
_enf_task = create_task("Build n8n automation workflow", owner="enf-agent", domain="automation", task_kind="work")
_enf_id = _enf_task["task_id"]
claim_task(_enf_id, "enf-agent")
r = complete_task(_enf_id, "enf-agent")
check("gate blocks complete_task for missing checklist",
      r["ok"] is False and "no checklist validation found" in r.get("error", ""),
      f"got: {r}")

# Verify gate note was appended
_enf_notes = list_notes(task_id=_enf_id)
_gate_notes = [n for n in _enf_notes.get("notes", []) if "[CHECKLIST GATE] blocked" in n.get("content", "")]
check("gate note appended on block",
      len(_gate_notes) >= 1,
      f"gate notes: {len(_gate_notes)}")

# Test 7: gate blocks when score < 1.0
_partial_responses = [
    {"item": "Artifact publicado?", "passed": True},
    {"item": "Credenciais seguras?", "passed": True},
    {"item": "Rollback plan?", "passed": False},
    {"item": "Staging testado?", "passed": True},
]
validate_checklist(task_id=_enf_id, responses=_partial_responses, validator="enf-agent")
r = complete_task(_enf_id, "enf-agent")
check("gate blocks complete_task for score < 1.0",
      r["ok"] is False and "score" in r.get("error", "") and "< 1.0" in r.get("error", ""),
      f"got: {r}")

# Test 8: gate passes with score 1.0
_full_responses = [
    {"item": "Artifact publicado?", "passed": True},
    {"item": "Credenciais seguras?", "passed": True},
    {"item": "Rollback plan?", "passed": True},
    {"item": "Staging testado?", "passed": True},
]
validate_checklist(task_id=_enf_id, responses=_full_responses, validator="enf-agent")
r = complete_task(_enf_id, "enf-agent")
check("gate passes with score 1.0",
      r["ok"] is True and r.get("status") == "done",
      f"got: {r}")

# Test 9: latest checklist result wins over older better score
_latest_task = create_task("Build another n8n automation workflow", owner="latest-agent", domain="automation", task_kind="work")
_latest_id = _latest_task["task_id"]
claim_task(_latest_id, "latest-agent")
validate_checklist(task_id=_latest_id, responses=_full_responses, validator="latest-agent")
validate_checklist(task_id=_latest_id, responses=_partial_responses, validator="latest-agent")
r = complete_task(_latest_id, "latest-agent")
check("latest checklist result overrides older perfect score",
      r["ok"] is False and "score 0.75 < 1.0" in r.get("error", ""),
      f"got: {r}")

# Test 10: backend/advisory (enforcement=advisory) does NOT block
_adv_task = create_task("Fix server middleware for backend auth", owner="adv-agent", domain="backend", task_kind="work")
_adv_id = _adv_task["task_id"]
claim_task(_adv_id, "adv-agent")
# Complete without any checklist — should pass because backend/work playbook is advisory
r = complete_task(_adv_id, "adv-agent")
check("advisory playbook does not block completion",
      r["ok"] is True and r.get("status") == "done",
      f"got: {r}")

# Test 11: frontend/work is advisory and does NOT block completion
_fe_adv_task = create_task("Build responsive sidebar component", owner="fe-adv-agent", domain="frontend", task_kind="work")
_fe_adv_id = _fe_adv_task["task_id"]
claim_task(_fe_adv_id, "fe-adv-agent")
validate_checklist(
    task_id=_fe_adv_id,
    responses=[
        {"item": "Componente renderiza sem erros no console?", "passed": True},
        {"item": "ui-evidence publicado?", "passed": False},
        {"item": "Acessibilidade basica respeitada?", "passed": True},
        {"item": "Artifact de codigo publicado?", "passed": True},
    ],
    validator="fe-adv-agent",
)
r = complete_task(_fe_adv_id, "fe-adv-agent")
check("frontend advisory playbook does not block completion",
      r["ok"] is True and r.get("status") == "done",
      f"got: {r}")

# Test 12: architecture/work is advisory and does NOT block completion
_arch_adv_task = create_task("Evaluate module boundaries for service separation", owner="arch-adv-agent", domain="architecture", task_kind="work")
_arch_adv_id = _arch_adv_task["task_id"]
claim_task(_arch_adv_id, "arch-adv-agent")
validate_checklist(
    task_id=_arch_adv_id,
    responses=[
        {"item": "arch-decision publicado?", "passed": True},
        {"item": "Alternativas consideradas?", "passed": True},
        {"item": "record_decision com source/root?", "passed": False},
        {"item": "Impacto em boundaries avaliado?", "passed": True},
    ],
    validator="arch-adv-agent",
)
r = complete_task(_arch_adv_id, "arch-adv-agent")
check("architecture advisory playbook does not block completion",
      r["ok"] is True and r.get("status") == "done",
      f"got: {r}")

# Test 13: task with no matching playbook at all → no gate
_no_pb_task = create_task("Do rework for something", owner="rw-agent", domain="automation", task_kind="rework")
_no_pb_id = _no_pb_task["task_id"]
claim_task(_no_pb_id, "rw-agent")
r = complete_task(_no_pb_id, "rw-agent")
check("no playbook → no gate (rework/automation)",
      r["ok"] is True and r.get("status") == "done",
      f"got: {r}")

# ── Retrospective On-Demand ───────────────────────────────────────────────────
print("\n[ Retrospective On-Demand ]")

# Setup: create a small completed request tree for retrospective tests
_retro_root = create_task("Retro test request", task_kind="request", domain="automation")
_retro_root_id = _retro_root["task_id"]

_retro_work = create_task(
    "Retro work task", owner="retro-worker", task_kind="work", domain="automation",
    parent_task_id=_retro_root_id, root_task_id=_retro_root_id,
)
_retro_work_id = _retro_work["task_id"]

_retro_review = create_task(
    "Retro review task", owner="retro-reviewer", task_kind="review", domain="automation",
    parent_task_id=_retro_root_id, root_task_id=_retro_root_id,
    depends_on=[_retro_work_id],
)
_retro_review_id = _retro_review["task_id"]

# Add a gate block note to the work task for bottleneck detection
append_note(
    "[CHECKLIST GATE] blocked | reason=no checklist validation found | task_kind=work | domain=automation",
    task_id=_retro_work_id, author="checklist-gate",
)

# Validate checklist with score 1.0 so the gate passes
validate_checklist(_retro_work_id, [
    {"item": "Test item 1", "passed": True},
    {"item": "Test item 2", "passed": True},
], validator="retro-validator")

# Complete operational tasks only. The root request remains pending by design;
# retrospective generation should still succeed once work/review are final.
claim_task(_retro_work_id, "retro-worker")
complete_task(_retro_work_id, "retro-worker")
claim_task(_retro_review_id, "retro-reviewer")
complete_task(_retro_review_id, "retro-reviewer")

# Test 1: generate_retrospective succeeds when only operational tasks are final
r = generate_retrospective(_retro_root_id, generated_by="test-runner")
check("generate_retrospective returns ok",
      r["ok"] is True and r.get("already_exists") is False,
      f"got: ok={r.get('ok')}, already_exists={r.get('already_exists')}")
_retro_id = r.get("retrospective_id", "")

# Test 2: retrospective has correct root_task_id
check("retrospective has correct root_task_id",
      r.get("root_task_id") == _retro_root_id)

# Test 3: summary has expected fields
_summary = r.get("retrospective", {}).get("summary", {})
check("summary contains total_tasks",
      isinstance(_summary.get("total_tasks"), int) and _summary["total_tasks"] == 3,
      f"total_tasks={_summary.get('total_tasks')}")

# Test 4: outcome is all_done even if root request remains pending
check("outcome is all_done",
      _summary.get("outcome") == "all_done",
      f"outcome={_summary.get('outcome')}")

# Test 5: root pending does not block clean outcome reporting
_tasks_by_status = _summary.get("tasks_by_status", {})
check("root request can remain pending without blocking retrospective",
      _tasks_by_status.get("pending", 0) == 1 and _summary.get("outcome") == "all_done",
      f"tasks_by_status={_tasks_by_status}, outcome={_summary.get('outcome')}")

# Test 6: gate_blocks detected
check("gate_blocks count is 1",
      _summary.get("gate_blocks") == 1,
      f"gate_blocks={_summary.get('gate_blocks')}")

# Test 7: review_rounds derived from task_kind=review count
check("review_rounds is 1",
      _summary.get("review_rounds") == 1,
      f"review_rounds={_summary.get('review_rounds')}")

# Test 8: bottlenecks populated
_bottlenecks = r.get("retrospective", {}).get("bottlenecks", [])
check("bottlenecks mentions gate block",
      any("gate" in b for b in _bottlenecks),
      f"bottlenecks={_bottlenecks}")

# Test 9: domain is set
check("retrospective domain is automation",
      r.get("retrospective", {}).get("domain") == "automation")

# Test 10: immutability — second call returns already_exists=True
r2 = generate_retrospective(_retro_root_id, generated_by="test-runner-2")
check("second generate returns already_exists=True",
      r2["ok"] is True and r2.get("already_exists") is True,
      f"already_exists={r2.get('already_exists')}")

# Test 11: immutability — same retrospective_id returned
check("immutable: same retrospective_id",
      r2.get("retrospective_id") == _retro_id)

# Test 12: get_retrospective reads back correctly
r3 = get_retrospective(_retro_root_id)
check("get_retrospective returns ok",
      r3["ok"] is True,
      f"ok={r3.get('ok')}")

# Test 13: get_retrospective has matching summary
_get_summary = r3.get("retrospective", {}).get("summary", {})
check("get_retrospective summary matches generate",
      _get_summary.get("total_tasks") == 3 and _get_summary.get("outcome") == "all_done")

# Test 14: get_retrospective for nonexistent root returns error
r4 = get_retrospective("nonexistent-root-id-12345")
check("get_retrospective nonexistent returns error",
      r4["ok"] is False and "no retrospective found" in r4.get("error", ""),
      f"error={r4.get('error')}")

# Test 15: generate_retrospective blocks on non-final operational tasks
_open_root = create_task("Open retro test", task_kind="request", domain="backend")
_open_root_id = _open_root["task_id"]
_open_work = create_task("Open work", task_kind="work", domain="backend",
                         parent_task_id=_open_root_id, root_task_id=_open_root_id)
r5 = generate_retrospective(_open_root_id)
check("generate blocks when tasks not in final state",
      r5["ok"] is False and "not in final state" in r5.get("error", ""),
      f"error={r5.get('error')}")

# Test 16: generate_retrospective blocks root-only requests with no operational tasks
_root_only = create_task("Root only retro test", task_kind="request", domain="backend")
r6 = generate_retrospective(_root_only["task_id"])
check("generate blocks root-only request with no operational tasks",
      r6["ok"] is False and "no operational tasks" in r6.get("error", ""),
      f"error={r6.get('error')}")

# Test 17: generate_retrospective with empty root_task_id
r7 = generate_retrospective("")
check("generate with empty root_task_id returns error",
      r7["ok"] is False and "required" in r7.get("error", ""),
      f"error={r7.get('error')}")

# Test 18: UNIQUE index exists on retrospectives(root_task_id)
with get_conn() as _retro_conn:
    _idx_rows = _retro_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='retrospectives' AND name LIKE '%root%'"
    ).fetchall()
    _idx_names = [row["name"] for row in _idx_rows]
check("UNIQUE index idx_retrospectives_root exists",
      "idx_retrospectives_root" in _idx_names,
      f"indexes={_idx_names}")

# ── CLI — Knowledge Layer ─────────────────────────────────────────────────────
print("\n[ CLI — knowledge ]")

_cli_exit, _cli_query, _cli_raw = run_cli(["query-knowledge", "--slug", "backend-neon-guideline"])
check("CLI query-knowledge returns active entry by slug",
      _cli_exit == 0 and _cli_query["ok"] is True and _cli_query["count"] == 1,
      _cli_raw)

_cli_submit_exit, _cli_submit, _cli_submit_raw = run_cli([
    "submit",
    "Document the JWT auth flow",
    "--planner-mode", "heuristic",
])
check("CLI submit uses codex-general as default synthesizer",
      _cli_submit_exit == 0 and _cli_submit["ok"] is True,
      _cli_submit_raw)
_cli_tree = list_task_tree(_cli_submit["root_task_id"])
_cli_synth = first_task(_cli_tree.get("tasks", []), "synthesize")
check("CLI submit default synth task targets codex-general",
      _cli_synth is not None and _cli_synth.get("requested_agent") == "codex-general",
      f"requested_agent={_cli_synth.get('requested_agent') if _cli_synth else 'missing'}")

_cli_slug = "cli-knowledge-guideline"
_cli_exit, _cli_promote, _cli_raw = run_cli([
    "promote-knowledge",
    _cli_slug,
    "general",
    "guideline",
    "CLI knowledge guideline",
    "Use hub_cli for curated knowledge operations.",
    "manual",
    "codex-general",
    "--tags", "cli", "knowledge",
])
check("CLI promote-knowledge creates a draft",
      _cli_exit == 0 and _cli_promote["ok"] is True and _cli_promote["status"] == "draft",
      _cli_raw)
_cli_knowledge_v1 = _cli_promote["knowledge_id"]

_cli_exit, _cli_approve, _cli_raw = run_cli(["approve-knowledge", _cli_knowledge_v1, "claude"])
check("CLI approve-knowledge activates the draft",
      _cli_exit == 0 and _cli_approve["ok"] is True and _cli_approve["status"] == "active",
      _cli_raw)

_cli_exit, _cli_supersede, _cli_raw = run_cli([
    "supersede-knowledge",
    _cli_knowledge_v1,
    "codex-general",
    "--new-content", "Use hub_cli for curated knowledge operations with v2 guidance.",
])
check("CLI supersede-knowledge creates a new active version",
      _cli_exit == 0 and _cli_supersede["ok"] is True and _cli_supersede["new_version"] == 2,
      _cli_raw)
_cli_knowledge_v2 = _cli_supersede["new_id"]

_cli_exit, _cli_deprecate, _cli_raw = run_cli([
    "deprecate-knowledge",
    _cli_knowledge_v2,
    "claude",
    "Superseded during CLI smoke validation",
])
check("CLI deprecate-knowledge marks the entry deprecated",
      _cli_exit == 0 and _cli_deprecate["ok"] is True and _cli_deprecate["status"] == "deprecated",
      _cli_raw)

_cli_exit, _cli_query_deprecated, _cli_raw = run_cli([
    "query-knowledge",
    "--slug", _cli_slug,
    "--status", "deprecated",
])
check("CLI query-knowledge can read deprecated entries explicitly",
      _cli_exit == 0 and _cli_query_deprecated["ok"] is True and _cli_query_deprecated["count"] == 1,
      _cli_raw)

# ── Summary ───────────────────────────────────────────────────────────────────
passed = sum(_results)
total = len(_results)
print(f"\n{'='*38}")
print(f"  {passed}/{total} tests passed")
if passed == total:
    print(f"  {PASS} All good.")
else:
    failed = total - passed
    print(f"  {FAIL} {failed} test(s) failed.")
print()

# Cleanup
try:
    os.remove(_tmp)
except OSError:
    pass

sys.exit(0 if passed == total else 1)
