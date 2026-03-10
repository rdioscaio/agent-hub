#!/usr/bin/env python3
"""Small local CLI for using agent-hub without an MCP client."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from hub.db import init_db
from tools.artifacts import publish_artifact
from tools.notes import append_note
from tools.orchestration import (
    delegate_task_to_gpt,
    list_task_tree,
    record_review,
    submit_request,
    summarize_request,
)
from tools.tasks import claim_next_task, complete_task, get_task


def _print(result: dict) -> int:
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="agent-hub local CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    submit = sub.add_parser("submit", help="submit a natural-language request")
    submit.add_argument("request")
    submit.add_argument("--requested-by", default="user")
    submit.add_argument("--priority", type=int, default=5)
    submit.add_argument("--planner-mode", choices=["auto", "gpt", "heuristic"], default="auto")
    submit.add_argument("--planner-agent", default="codex-planner")
    submit.add_argument("--worker-agent", default="codex")
    submit.add_argument("--reviewer-agent", default="claude")
    submit.add_argument("--fallback-agent", default="gpt-fallback")
    submit.add_argument("--synthesizer-agent", default="codex")
    submit.add_argument("--max-work-items", type=int, default=3)

    claim_next = sub.add_parser("claim-next", help="claim the next runnable task")
    claim_next.add_argument("owner")
    claim_next.add_argument("--task-kind", default="")
    claim_next.add_argument("--root-task-id", default="")
    claim_next.add_argument("--requested-agent", default="")
    claim_next.add_argument("--limit", type=int, default=100)

    get_cmd = sub.add_parser("get", help="fetch a task")
    get_cmd.add_argument("task_id")

    tree = sub.add_parser("tree", help="show the task tree for a request")
    tree.add_argument("root_task_id")

    status = sub.add_parser("status", help="summarize request progress")
    status.add_argument("root_task_id")

    review = sub.add_parser("review", help="record a review verdict for a done task")
    review.add_argument("task_id")
    review.add_argument("reviewer")
    review.add_argument("verdict", choices=["approve", "revise", "fallback"])
    review.add_argument("--feedback", default="")
    review.add_argument("--fallback-agent", default="")
    review.add_argument("--rework-agent", default="")

    delegate = sub.add_parser("delegate-gpt", help="execute a task through ask_gpt")
    delegate.add_argument("task_id")
    delegate.add_argument("--owner", default="gpt-fallback")
    delegate.add_argument("--data-policy", choices=["summary_only", "snippets", "full_text"], default="snippets")
    delegate.add_argument("--model", default="gpt-4o")

    complete = sub.add_parser("complete", help="complete a claimed task")
    complete.add_argument("task_id")
    complete.add_argument("owner")

    note = sub.add_parser("append-note", help="append a note to a task")
    note.add_argument("task_id")
    note.add_argument("author")
    note.add_argument("content")

    artifact = sub.add_parser("publish-artifact", help="publish a text artifact")
    artifact.add_argument("task_id")
    artifact.add_argument("name")
    artifact.add_argument("published_by")
    artifact.add_argument("content")
    artifact.add_argument("--content-type", default="text/plain")

    return parser


def main(argv: list[str] | None = None) -> int:
    init_db()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "submit":
        result = submit_request(
            request=args.request,
            requested_by=args.requested_by,
            priority=args.priority,
            planner_mode=args.planner_mode,
            planner_agent=args.planner_agent,
            worker_agent=args.worker_agent,
            reviewer_agent=args.reviewer_agent,
            fallback_agent=args.fallback_agent,
            synthesizer_agent=args.synthesizer_agent,
            max_work_items=args.max_work_items,
        )
        return _print(result)

    if args.command == "claim-next":
        return _print(
            claim_next_task(
                owner=args.owner,
                task_kind=args.task_kind,
                root_task_id=args.root_task_id,
                requested_agent=args.requested_agent,
                limit=args.limit,
            )
        )

    if args.command == "get":
        return _print(get_task(args.task_id))

    if args.command == "tree":
        return _print(list_task_tree(args.root_task_id))

    if args.command == "status":
        return _print(summarize_request(args.root_task_id))

    if args.command == "review":
        return _print(
            record_review(
                task_id=args.task_id,
                reviewer=args.reviewer,
                verdict=args.verdict,
                feedback=args.feedback,
                fallback_agent=args.fallback_agent,
                rework_agent=args.rework_agent,
            )
        )

    if args.command == "delegate-gpt":
        return _print(
            delegate_task_to_gpt(
                task_id=args.task_id,
                owner=args.owner,
                data_policy=args.data_policy,
                model=args.model,
            )
        )

    if args.command == "complete":
        return _print(complete_task(args.task_id, args.owner))

    if args.command == "append-note":
        return _print(append_note(args.content, task_id=args.task_id, author=args.author))

    if args.command == "publish-artifact":
        return _print(
            publish_artifact(
                args.name,
                args.content,
                task_id=args.task_id,
                published_by=args.published_by,
                content_type=args.content_type,
            )
        )

    parser.error(f"unsupported command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
