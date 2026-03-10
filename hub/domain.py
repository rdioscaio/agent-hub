"""
Domain classification for tasks — keyword-based, advisory, deterministic.

classify_domain is an internal function, NOT an MCP tool.

Algorithm:
    1. Normalize title and description to lowercase
    2. For each domain (except "general"), count keyword matches:
       - title match = weight 2
       - description match = weight 1
       - matching uses word boundaries (regex \\b)
    3. Domain with highest score wins
    4. Ties broken by fixed priority order (DOMAIN_PRIORITY)
    5. Zero matches across all domains → "general"
"""

import re

VALID_DOMAINS = frozenset({
    "backend",
    "frontend",
    "database",
    "infra",
    "architecture",
    "process",
    "general",
})

# Keywords per domain. Order does not matter — scoring is by match count.
DOMAIN_KEYWORDS: dict[str, list[str]] = {
    "backend": [
        "api", "server", "endpoint", "rest", "graphql", "nestjs",
        "express", "controller", "service", "middleware", "route",
        "auth", "jwt", "oauth",
    ],
    "frontend": [
        "ui", "react", "component", "page", "layout", "css",
        "style", "vite", "tailwind", "responsive", "form",
        "button", "modal", "sidebar",
    ],
    "database": [
        "sql", "query", "migration", "schema", "table", "index",
        "postgres", "sqlite", "neon", "prisma", "typeorm", "column",
    ],
    "infra": [
        "deploy", "ci", "cd", "docker", "pipeline", "vercel",
        "railway", "nginx", "env", "config", "monitoring",
        "logging", "github actions",
    ],
    "architecture": [
        "refactor", "pattern", "abstraction", "module",
        "interface", "dependency", "coupling", "design",
        "structure", "separation",
    ],
    "process": [
        "review", "checklist", "workflow", "standup",
        "retrospective", "planning", "sprint", "backlog",
        "priority",
    ],
}

# Fixed priority for tie-breaking (lower index = higher priority)
DOMAIN_PRIORITY = [
    "backend",
    "frontend",
    "database",
    "infra",
    "architecture",
    "process",
]


def _count_keyword_matches(text: str, keywords: list[str]) -> int:
    """Count how many distinct keywords match in text using word boundaries."""
    count = 0
    for kw in keywords:
        pattern = r"\b" + re.escape(kw) + r"\b"
        if re.search(pattern, text):
            count += 1
    return count


def classify_domain(title: str, description: str = "") -> str:
    """Classify a task into a domain based on title and description keywords.

    Title matches have weight 2, description matches have weight 1.
    Ties broken by fixed priority order. Zero matches → "general".

    This is an internal function — NOT an MCP tool.
    """
    title_lower = (title or "").lower()
    desc_lower = (description or "").lower()

    scores: dict[str, int] = {}

    for domain in DOMAIN_PRIORITY:
        keywords = DOMAIN_KEYWORDS[domain]
        title_matches = _count_keyword_matches(title_lower, keywords)
        desc_matches = _count_keyword_matches(desc_lower, keywords)
        score = (title_matches * 2) + (desc_matches * 1)
        if score > 0:
            scores[domain] = score

    if not scores:
        return "general"

    max_score = max(scores.values())

    # DOMAIN_PRIORITY order resolves ties — first match with max_score wins
    for domain in DOMAIN_PRIORITY:
        if scores.get(domain) == max_score:
            return domain

    return "general"
