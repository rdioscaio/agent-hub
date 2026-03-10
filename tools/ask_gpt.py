"""
ask_gpt — delegate a question to OpenAI.

Data policies:
  summary_only  — sends only purpose + question, no extra context
  snippets      — includes referenced artifacts/notes (with size cap)
  full_text     — includes full content of refs (hard cap enforced)

OpenAI key is read exclusively from the OPENAI_API_KEY environment variable.
It is never written to config files or logged.
"""

import hashlib
import json
import os
import time

from hub.audit import audit
from hub.db import get_conn

MAX_PAYLOAD_CHARS = 12_000   # ~3K tokens, safe for gpt-4o
MAX_RESPONSE_TOKENS = 2_000
VALID_POLICIES = {"summary_only", "snippets", "full_text"}
VALID_FORMATS = {"text", "json"}


def _fetch_refs(ref_ids: list[str], conn) -> str:
    """Load artifact/note content for context_refs."""
    parts = []
    for ref_id in ref_ids:
        row = conn.execute(
            "SELECT 'artifact' AS kind, name, content FROM artifacts WHERE id=? "
            "UNION SELECT 'note', id, content FROM notes WHERE id=?",
            (ref_id, ref_id),
        ).fetchone()
        if row:
            parts.append(f"[{row['kind']}:{row['name']}]\n{row['content']}")
    return "\n\n".join(parts)


def ask_gpt(
    purpose: str,
    question: str,
    data_policy: str = "summary_only",
    context_refs: list = None,
    response_format: str = "text",
    max_tokens: int = 1_000,
    task_id: str = "",
    model: str = "gpt-4o",
) -> dict:
    """
    Delegate a question to OpenAI GPT.

    Args:
        purpose:        Why this delegation is happening (logged, not sent).
        question:       The actual prompt/question for GPT.
        data_policy:    summary_only | snippets | full_text
        context_refs:   List of artifact IDs or note IDs to include as context.
        response_format: text | json
        max_tokens:     Max tokens in GPT response (capped at 2000).
        task_id:        Associated task ID for audit trail.
        model:          OpenAI model to use (default: gpt-4o).
    """
    context_refs = context_refs or []
    args = dict(
        purpose=purpose,
        data_policy=data_policy,
        context_refs=context_refs,
        response_format=response_format,
        max_tokens=max_tokens,
        task_id=task_id,
        model=model,
        question_hash=hashlib.sha256(question.encode()).hexdigest()[:12],
    )

    with audit("ask_gpt", args, task_id):
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            return {"ok": False, "error": "OPENAI_API_KEY environment variable not set"}

        if data_policy not in VALID_POLICIES:
            return {"ok": False, "error": f"invalid data_policy '{data_policy}'"}
        if response_format not in VALID_FORMATS:
            return {"ok": False, "error": f"invalid response_format '{response_format}'"}

        max_tokens = min(max_tokens, MAX_RESPONSE_TOKENS)

        # Build context based on policy
        context_block = ""
        if data_policy != "summary_only" and context_refs:
            with get_conn() as conn:
                context_block = _fetch_refs(context_refs, conn)
            if data_policy == "snippets":
                context_block = context_block[:3_000]
            # full_text: include up to MAX_PAYLOAD_CHARS after question

        system_msg = (
            "You are a specialist assistant. Answer concisely and precisely. "
            + ("Respond with valid JSON only." if response_format == "json" else "")
        )

        user_msg = question
        if context_block:
            user_msg = f"Context:\n{context_block}\n\n---\n{question}"

        # Hard cap on total payload
        if len(user_msg) > MAX_PAYLOAD_CHARS:
            user_msg = user_msg[:MAX_PAYLOAD_CHARS]
            user_msg += "\n\n[context truncated to fit payload limit]"

        try:
            from openai import OpenAI
        except ImportError:
            return {"ok": False, "error": "openai package not installed. Run: pip install openai"}

        client = OpenAI(api_key=api_key)
        start = time.time()
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": user_msg},
                ],
                max_tokens=max_tokens,
                **({"response_format": {"type": "json_object"}} if response_format == "json" else {}),
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        elapsed_ms = int((time.time() - start) * 1000)
        answer = resp.choices[0].message.content or ""
        usage = {
            "prompt_tokens": resp.usage.prompt_tokens,
            "completion_tokens": resp.usage.completion_tokens,
        }

        return {
            "ok": True,
            "answer": answer,
            "model": model,
            "elapsed_ms": elapsed_ms,
            "usage": usage,
        }
