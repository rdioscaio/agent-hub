#!/usr/bin/env python3
"""Small local CLI for using agent-hub without an MCP client."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from hub.bootstrap import ensure_ready
from tools.artifacts import publish_artifact
from tools.knowledge import (
    approve_knowledge,
    deprecate_knowledge,
    promote_knowledge,
    query_knowledge,
    supersede_knowledge,
)
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
    submit.add_argument("--synthesizer-agent", default="codex-general")
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

    query_k = sub.add_parser("query-knowledge", help="query curated knowledge entries")
    query_k.add_argument("--domain", default="")
    query_k.add_argument("--kind", default="")
    query_k.add_argument("--status", default="active")
    query_k.add_argument("--keyword", default="")
    query_k.add_argument("--slug", default="")
    query_k.add_argument("--limit", type=int, default=10)
    query_k.add_argument("--tags", nargs="*", default=None)

    promote_k = sub.add_parser("promote-knowledge", help="create a draft knowledge entry")
    promote_k.add_argument("slug")
    promote_k.add_argument("domain")
    promote_k.add_argument("kind")
    promote_k.add_argument("title")
    promote_k.add_argument("content")
    promote_k.add_argument("source_type", choices=["memory", "decision", "manual"])
    promote_k.add_argument("promoted_by")
    promote_k.add_argument("--source-id", default="")
    promote_k.add_argument("--source-task-id", default="")
    promote_k.add_argument("--root-task-id", default="")
    promote_k.add_argument("--tags", nargs="*", default=None)

    approve_k = sub.add_parser("approve-knowledge", help="approve a draft knowledge entry")
    approve_k.add_argument("knowledge_id")
    approve_k.add_argument("reviewed_by")

    supersede_k = sub.add_parser("supersede-knowledge", help="supersede an active knowledge entry")
    supersede_k.add_argument("knowledge_id")
    supersede_k.add_argument("updated_by")
    supersede_k.add_argument("--new-title", default="")
    supersede_k.add_argument("--new-content", default="")
    supersede_k.add_argument("--domain", default="")
    supersede_k.add_argument("--tags", nargs="*", default=None)

    deprecate_k = sub.add_parser("deprecate-knowledge", help="deprecate a draft or active knowledge entry")
    deprecate_k.add_argument("knowledge_id")
    deprecate_k.add_argument("deprecated_by")
    deprecate_k.add_argument("reason")

    return parser


def main(argv: list[str] | None = None) -> int:
    ensure_ready()
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

    if args.command == "query-knowledge":
        return _print(
            query_knowledge(
                domain=args.domain,
                kind=args.kind,
                status=args.status,
                keyword=args.keyword,
                tags=args.tags,
                slug=args.slug,
                limit=args.limit,
            )
        )

    if args.command == "promote-knowledge":
        return _print(
            promote_knowledge(
                slug=args.slug,
                domain=args.domain,
                kind=args.kind,
                title=args.title,
                content=args.content,
                source_type=args.source_type,
                promoted_by=args.promoted_by,
                source_id=args.source_id,
                source_task_id=args.source_task_id,
                root_task_id=args.root_task_id,
                tags=args.tags,
            )
        )

    if args.command == "approve-knowledge":
        return _print(approve_knowledge(args.knowledge_id, args.reviewed_by))

    if args.command == "supersede-knowledge":
        return _print(
            supersede_knowledge(
                knowledge_id=args.knowledge_id,
                updated_by=args.updated_by,
                new_title=args.new_title,
                new_content=args.new_content,
                domain=args.domain,
                tags=args.tags,
            )
        )

    if args.command == "deprecate-knowledge":
        return _print(deprecate_knowledge(args.knowledge_id, args.deprecated_by, args.reason))

    parser.error(f"unsupported command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
