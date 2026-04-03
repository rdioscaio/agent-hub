import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp = tempfile.mktemp(suffix=".sqlite")
os.environ["HUB_DB_PATH"] = _tmp

from hub.bootstrap import ensure_ready
from hub.db import get_conn
from scripts.run_claude_review_worker import main as review_worker_main
from tools.artifacts import publish_artifact
from tools.notes import list_notes
from tools.tasks import complete_task, create_task, get_task

ensure_ready()


class TestClaudeReviewWorker(unittest.TestCase):
    def setUp(self):
        with get_conn() as conn:
            for table in (
                "notes",
                "artifacts",
                "task_metrics",
                "retrospectives",
                "tasks",
            ):
                conn.execute(f"DELETE FROM {table}")

    def _write_fake_claude(self, payload: dict, exit_code: int = 0) -> str:
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        script_path = Path(tmpdir.name) / "claude"
        script_path.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            f"payload = {json.dumps(payload, ensure_ascii=False)}\n"
            f"sys.stdout.write(json.dumps(payload, ensure_ascii=False))\n"
            f"raise SystemExit({exit_code})\n"
        )
        script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC)
        return str(script_path)

    def _seed_review_task(self) -> tuple[str, str]:
        root = create_task(
            "Request: test review worker",
            task_kind="request",
            requested_agent="root",
            domain="backend",
        )
        work = create_task(
            "Execute: verify backend worker",
            description="Implement the deterministic review worker.",
            parent_task_id=root["task_id"],
            root_task_id=root["task_id"],
            task_kind="work",
            requested_agent="codex",
            review_policy="required",
            domain="backend",
        )
        publish_artifact(
            "work-result.md",
            "Implemented a deterministic Claude review worker and validated it locally.",
            task_id=work["task_id"],
            published_by="codex",
        )
        complete_task(work["task_id"], "codex")
        review = create_task(
            "Review: backend worker",
            description="Review the worker output and approve only if evidence is sufficient.",
            parent_task_id=root["task_id"],
            root_task_id=root["task_id"],
            depends_on=[work["task_id"]],
            task_kind="review",
            requested_agent="claude-backend",
            source_task_id=work["task_id"],
            domain="backend",
        )
        return work["task_id"], review["task_id"]

    def test_worker_claims_review_and_records_verdict(self):
        source_task_id, review_task_id = self._seed_review_task()
        claude_bin = self._write_fake_claude(
            {
                "type": "result",
                "subtype": "success",
                "structured_output": {
                    "verdict": "approve",
                    "feedback": "Evidence is sufficient.",
                    "confidence": "high",
                    "evidence": ["artifact describes implemented worker", "review task has matching scope"],
                },
                "session_id": "session-123",
                "total_cost_usd": 0.01,
            }
        )

        exit_code = review_worker_main(
            [
                "--owner",
                "claude-backend",
                "--claude-bin",
                claude_bin,
                "--max-budget-usd",
                "0.10",
            ]
        )

        self.assertEqual(exit_code, 0)
        source_task = get_task(source_task_id)["task"]
        review_task = get_task(review_task_id)["task"]
        self.assertEqual(source_task["quality_status"], "approved")
        self.assertEqual(review_task["status"], "done")
        self.assertEqual(review_task["owner"], "claude-backend")

        notes = list_notes(task_id=source_task_id)
        self.assertTrue(
            any("Review by claude-backend [approve]" in note["content"] for note in notes["notes"])
        )

        with get_conn() as conn:
            artifact_row = conn.execute(
                "SELECT name FROM artifacts WHERE task_id = ? ORDER BY created_at DESC LIMIT 1",
                (review_task_id,),
            ).fetchone()
        self.assertIsNotNone(artifact_row)
        self.assertTrue(artifact_row["name"].startswith("review-"))

    def test_worker_fails_review_task_on_invalid_verdict(self):
        _, review_task_id = self._seed_review_task()
        claude_bin = self._write_fake_claude(
            {
                "type": "result",
                "subtype": "success",
                "structured_output": {
                    "verdict": "maybe",
                    "feedback": "unclear",
                },
            }
        )

        exit_code = review_worker_main(
            [
                "--owner",
                "claude-backend",
                "--claude-bin",
                claude_bin,
                "--max-budget-usd",
                "0.10",
            ]
        )

        self.assertEqual(exit_code, 1)
        review_task = get_task(review_task_id)["task"]
        self.assertEqual(review_task["status"], "failed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
