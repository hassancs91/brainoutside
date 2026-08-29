"""The open, deterministic reader — `assemble-context` without a model.

Replaces the Claude Agent SDK path for `assemble-context`: a pure-Python
assembler selects entities and builds the context pack. No
`ANTHROPIC_API_KEY`, no SDK, no latency budget, no `tokens`/`model`/
`duration_ms` — the brain returns data and the entity ids it drew from;
whatever MCP/AI consumes the pack brings its own model and metering.

Tier safety is inherited, not re-encoded: selection runs over the
caller's visible entities (same rule as the dumb layer), and bodies are
read ONLY through `files.read(tier, path)` on the tier snapshot. The
pack is assembled deterministically — keyword/relevance scoring, never
model judgement — so two identical calls produce identical packs.

The heavy lifting is split so the core is DB-free and unit-testable:

- `assemble_pack(...)` — pure: takes already-visible `entities` and a
  `read_body` callable, returns the pack dict. No Django imports inside.
- `reader.assemble_context(...)` — wires the DB (visible entities) and
  the tier snapshot (`files.read`) into it, then runs the belt-and-braces
  `_verify_entities` pass (unchanged, still logs above-tier leaks).
"""
from __future__ import annotations

import re
from typing import Callable, Iterable

#: Maximum notes (non-identity) the pack will quote.
MAX_NOTES = 8
#: Cap on the verbatim body excerpt per note, in characters.
MAX_EXCERPT_CHARS = 1500
#: Words that carry no retrieval signal.
_STOPWORDS = frozenset(
    {
        "a", "about", "after", "again", "all", "also", "am", "an", "and", "any",
        "are", "as", "at", "be", "because", "been", "before", "being", "but",
        "by", "can", "could", "did", "do", "does", "for", "from", "get", "had",
        "has", "have", "he", "her", "hers", "him", "his", "how", "i", "if", "in",
        "into", "is", "it", "its", "just", "let", "like", "me", "more", "most",
        "my", "no", "not", "of", "on", "one", "or", "our", "out", "own", "say",
        "she", "should", "so", "some", "than", "that", "the", "their", "them",
        "then", "there", "these", "they", "this", "those", "through", "to",
        "too", "under", "up", "us", "was", "we", "were", "what", "when", "where",
        "which", "while", "who", "why", "will", "with", "would", "you", "your",
    }
)
_WORD_RE = re.compile(r"[a-z0-9]+")


def _terms(text: str) -> set[str]:
    """Lowercase word tokens, stopwords and singles dropped."""
    return {
        w
        for w in _WORD_RE.findall((text or "").lower())
        if len(w) > 2 and w not in _STOPWORDS
    }


def _match_bonus(needle: str, haystack: str) -> int:
    """1 per word of `needle` that appears in `haystack` (word-boundary)."""
    if not needle:
        return 0
    return sum(1 for w in _terms(needle) if w in _terms(haystack))


def _score(entity, task_terms: set[str], body: str) -> int:
    """Deterministic relevance: task terms against topics/projects/title/
    description/kind (weighted), plus the body (word matches, capped)."""
    score = 0
    for t in task_terms:
        if t in {str(x).lower() for x in (entity.topics or [])}:
            score += 3
        if t in {str(x).lower() for x in (entity.projects or [])}:
            score += 3
        if t in (entity.title or "").lower().split():
            score += 2
        if t in (entity.description or "").lower():
            score += 2
        if t in (entity.kind or "").lower():
            score += 2
    if body:
        score += min(_match_bonus(" ".join(sorted(task_terms)), body), 10)
    return score


def _heading(text: str) -> str:
    """First H1 line of a body, for the pack's note header."""
    for line in (text or "").splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def _excerpt(body: str) -> str:
    """Verbatim excerpt: leading lines up to the char cap, no elision
    marks — the quote is preserved character-for-character."""
    return (body or "").strip()[:MAX_EXCERPT_CHARS]


def assemble_pack(
    *,
    task: str,
    tier: str,
    entities: Iterable,
    lens_name: str = "",
    lens_content: str = "",
    read_body: Callable,
) -> dict:
    """Build a deterministic context pack.

    Args:
        task: the consumer's task text (already validated non-empty).
        tier: the caller's tier (for the pack header only — visibility
            filtering is the caller's job).
        entities: iterable of ALREADY-VISIBLE Entity-like objects. The
            caller must filter by tier before this function is called.
        lens_name: optional lens the caller resolved ("" when none).
        lens_content: the lens body the caller resolved ("" when none).
        read_body: callable(entity) -> str, reads the entity's note body
            from the caller's tier snapshot.

    Returns:
        Dict with `context_pack`, `entity_ids_used`, `gaps`. Note: body
        reads happen here, so callers that need a DB-free test must hand
        a fake `read_body`.
    """
    visible = list(entities)
    identities = [e for e in visible if e.kind == "identity"]
    candidates = [e for e in visible if e.kind != "identity"]

    task_terms = _terms(task)

    selected: list = []
    scored: list[tuple[int, object, str]] = []
    for e in candidates:
        try:
            body = read_body(e)
        except Exception:  # snapshot miss / read error — skip, don't fail the pack
            body = ""
        score = _score(e, task_terms, body)
        if score > 0:
            scored.append((score, e, body))
    scored.sort(key=lambda x: (-x[0], getattr(x[1], "entity_id", "")))
    selected = scored[:MAX_NOTES]

    gaps: list[str] = []
    if task_terms and not selected:
        gaps.append("no notes matched the task at this tier")
    if not identities and visible:
        gaps.append("no identity files visible at this tier")

    # ---- assemble the pack -------------------------------------------------
    lines: list[str] = [
        "# Context pack",
        "",
        f"- Tier: `{tier}`",
        "- Assembled by: deterministic reader (open, model-free)",
        f"- Task: {task}",
    ]
    if lens_name:
        lines += [f"- Lens: `{lens_name}`", ""]
        if lens_content:
            lines += ["> Lens scope:", lens_content.strip(), ""]
    lines += [""]

    lines.append("## Identity")
    if identities:
        for e in identities:
            body = ""
            try:
                body = read_body(e)
            except Exception:
                body = ""
            lines += [
                f"### {getattr(e, 'entity_id', '')} — {getattr(e, 'title', '')}",
                f"`{getattr(e, 'path', '')}`",
                _excerpt(body),
                "",
            ]
    else:
        lines += ["_none visible at this tier_", ""]

    lines.append("## Selected notes")
    if selected:
        for score, e, body in selected:
            title = _heading(body) or getattr(e, "title", "") or getattr(e, "entity_id", "")
            lines += [
                f"### {title} — {getattr(e, 'entity_id', '')}",
                f"- kind: `{getattr(e, 'kind', '')}` | path: `{getattr(e, 'path', '')}` | score: {score}",
            ]
            if getattr(e, "topics", None):
                lines.append(f"- topics: {', '.join(str(t) for t in e.topics)}")
            excerpt = _excerpt(body)
            if excerpt:
                lines += ["", excerpt]
            lines += [""]
    else:
        lines += ["_nothing relevant at this tier_", ""]

    lines.append("## Gaps")
    lines += [f"- {g}" for g in gaps] or ["_none_"]
    lines += [""]

    return {
        "context_pack": "\n".join(lines),
        "entity_ids_used": [getattr(e, "entity_id", "") for _, e, _ in selected]
        + [getattr(e, "entity_id", "") for e in identities],
        "gaps": gaps,
    }
