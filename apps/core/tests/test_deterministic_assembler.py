"""The deterministic assembler — open `assemble-context`, no model.

DB-free on purpose (see CLAUDE.md — the host venv has no django_redis):
`assembler.assemble_pack` takes already-visible entities and a `read_body`
callable, so the selection and pack-building logic is testable with plain
objects and no Django. The DB wiring (tier filtering, snapshot reads) is
`reader.assemble_context`'s job and is covered by the integration tests.

What these pin:
- selection is keyword/relevance scoring, never model judgement;
- the pack carries ONLY data (context_pack, entity_ids_used, gaps) —
  no tokens, no model, no latency keys;
- identity files always lead the pack when visible;
- a task that matches nothing yields an honest empty pack with a gap,
  not an invented one;
- body reads go through the injected `read_body` (the snapshot door),
  so containment lives with the caller.
"""
from __future__ import annotations

import pytest

from apps.reader.services import assembler


class _E:
    def __init__(
        self,
        entity_id: str,
        *,
        kind: str = "take",
        title: str = "",
        description: str = "",
        topics: list | None = None,
        projects: list | None = None,
        path: str = "",
    ):
        self.entity_id = entity_id
        self.kind = kind
        self.title = title
        self.description = description
        self.topics = topics or []
        self.projects = projects or []
        self.path = path or f"knowledge/takes/{entity_id}.md"


def _body(entity_id: str, text: str) -> dict:
    return {entity_id: text}


def _read(body: dict):
    def read_body(entity) -> str:
        return body.get(entity.entity_id, "")
    return read_body


IDENTITY = _E("identity-core", kind="identity", title="Identity core", path="identity/core.md")


def test_identity_always_leads_the_pack():
    note = _E("take-deploy", title="Deploy notes", topics=["self-hosting"], path="knowledge/takes/take-deploy.md")
    body = _body("take-deploy", "# Deploy notes\n\n> VERBATIM: the exact words.")
    entities = [note, IDENTITY]

    pack = assembler.assemble_pack(
        task="how do we deploy?",
        tier="agents-only",
        entities=entities,
        read_body=_read(body),
    )

    assert pack["context_pack"].index("# Identity") < pack["context_pack"].index("# Selected notes")
    assert "identity-core" in pack["context_pack"]
    assert "identity-core" in pack["entity_ids_used"]
    # The verbatim quote survives character-for-character.
    assert "VERBATIM: the exact words." in pack["context_pack"]


def test_keyword_relevance_selects_the_matching_note_only():
    hit = _E("take-deploy", title="Deployment", topics=["self-hosting"], path="knowledge/takes/take-deploy.md")
    miss = _E("take-voice", title="Voice rules", topics=["writing"], path="knowledge/takes/take-voice.md")
    body = _body("take-deploy", "# Deployment\n\nhow to deploy the stack")
    body.update(_body("take-voice", "# Voice\n\ntone rules"))

    pack = assembler.assemble_pack(
        task="deployment runbook",
        tier="agents-only",
        entities=[hit, miss, IDENTITY],
        read_body=_read(body),
    )

    assert pack["entity_ids_used"] == ["take-deploy", "identity-core"]
    assert "take-voice" not in pack["entity_ids_used"]
    assert "take-voice" not in pack["context_pack"]


def test_no_match_returns_honest_gap_not_invented_context():
    note = _E("take-voice", title="Voice rules", topics=["writing"], path="knowledge/takes/take-voice.md")
    body = _body("take-voice", "# Voice\n\ntone rules")

    pack = assembler.assemble_pack(
        task="quantum chemistry",
        tier="public",
        entities=[note],
        read_body=_read(body),
    )

    assert pack["entity_ids_used"] == []
    assert any("no notes matched" in g for g in pack["gaps"])
    assert "nothing relevant" in pack["context_pack"]


def test_pack_carries_only_data_keys():
    note = _E("take-deploy", title="Deployment", topics=["self-hosting"], path="knowledge/takes/take-deploy.md")
    body = _body("take-deploy", "# Deployment\n\ndeployment text")

    pack = assembler.assemble_pack(
        task="deployment",
        tier="agents-only",
        entities=[note],
        read_body=_read(body),
    )

    assert set(pack.keys()) == {"context_pack", "entity_ids_used", "gaps"}
    # No platform/model/token metadata in the response surface.
    assert "model" not in pack
    assert "tokens" not in pack
    assert "duration_ms" not in pack
    assert "operation_id" not in pack


def test_lens_content_is_inlined_when_resolved():
    note = _E("take-deploy", title="Deployment", topics=["self-hosting"], path="knowledge/takes/take-deploy.md")
    body = _body("take-deploy", "# Deployment\n\ndeployment text")
    body.update(_body("identity-core", "# Identity\n\nObi"))
    lens_body = "Only self-hosting material. Ceiling: agents-only."

    pack = assembler.assemble_pack(
        task="deployment",
        tier="agents-only",
        entities=[note, IDENTITY],
        lens_name="self-hosting",
        lens_content=lens_body,
        read_body=_read(body),
    )

    assert "self-hosting" in pack["context_pack"]
    assert lens_body in pack["context_pack"]
    assert pack["gaps"] == []


def test_read_body_failure_skips_note_not_the_pack():
    """A body read that raises (snapshot miss mid-run) must not 500 the
    whole pack — the note is simply not quoted."""
    note = _E("take-voice", title="Voice rules", topics=["writing"], path="knowledge/takes/take-voice.md")

    def broken_read(entity):
        raise FileNotFoundError(entity.path)

    pack = assembler.assemble_pack(
        task="deployment",
        tier="agents-only",
        entities=[note],
        read_body=broken_read,
    )

    # Unreadable body -> no match, no crash, honest gap.
    assert pack["entity_ids_used"] == []
    assert any("no notes matched" in g for g in pack["gaps"])
    assert pack["context_pack"]


def test_excerpt_is_truncated_verbatim():
    note = _E("take-deploy", title="Deployment", topics=["self-hosting"], path="knowledge/takes/take-deploy.md")
    long_text = "x" * (assembler.MAX_EXCERPT_CHARS * 2)
    body = _body("take-deploy", long_text)

    pack = assembler.assemble_pack(
        task="deployment",
        tier="agents-only",
        entities=[note],
        read_body=_read(body),
    )

    # The excerpt in the pack is capped; the source is not.
    assert len(pack["context_pack"]) < len(long_text)
    assert "x" * assembler.MAX_EXCERPT_CHARS in pack["context_pack"]


def test_max_notes_caps_selected_count():
    notes = [
        _E(f"take-{i}", title=f"Note {i}", topics=["t"], description="shared body text")
        for i in range(20)
    ]
    body = {n.entity_id: "# Note\n\nshared body text" for n in notes}

    pack = assembler.assemble_pack(
        task="shared body text",
        tier="agents-only",
        entities=notes,
        read_body=_read(body),
    )

    assert len(pack["entity_ids_used"]) == assembler.MAX_NOTES
