"""The open-brain surface — `get-protocol`, platform-neutral tool
descriptions, and the model-free `assemble-context` contract.

Pins the cross-tool story:
- `get-protocol` is a registered endpoint (REST + MCP via the registry),
  so any consumer can fetch usage order without reading source.
- No platform-specific language (Claude / Anthropic / SDK / claude.ai /
  model / tokens) surfaces in ANY MCP tool description or in the
  `assemble-context` output shape.
- `assemble-context` returns data only: context_pack, entity_ids_used,
  gaps. The old tokens/model/duration_ms keys are gone.

These run against the real registry (endpoints auto-register on import),
mirroring how `test_endpoint_flag_surface` reads `registry.all()`.
"""
from __future__ import annotations

import asyncio

import pytest

from apps.core.registry import registry
from apps.core.testing import call_endpoint
from apps.mind.endpoints import AssembleContext, GetProtocol

#: Slugs whose descriptions are part of the MCP tool surface.
_TOOL_SLUGS = {
    "ping",
    "get-index",
    "list-notes",
    "get-note",
    "get-lens",
    "get-identity",
    "get-raw",
    "assemble-context",
    "get-protocol",
}
_PLATFORM_WORDS = (
    "claude",
    "anthropic",
    "sdk",
    "claude.ai",
    "tokens",
    "duration_ms",
)


def _spec(slug: str):
    specs = [s for s in registry.all() if s.slug == slug]
    assert specs, f"{slug} is not registered — did the app configs load?"
    return specs[0]


@pytest.fixture(autouse=True)
def registry_loaded():
    assert any(s.slug == "assemble-context" for s in registry.all()), (
        "registry is empty — endpoint app configs did not load"
    )
    yield


def test_get_protocol_is_registered_and_callable():
    spec = _spec("get-protocol")
    out = asyncio.run(call_endpoint(GetProtocol, {}))
    assert "Reading order" in out.protocol
    assert "propose-feed" in out.protocol
    assert "get-identity" in out.protocol
    # Same class is what the registry registers under get-protocol.
    assert spec.cls is GetProtocol


def test_every_tool_description_is_platform_neutral():
    for slug in sorted(_TOOL_SLUGS):
        desc = _spec(slug).description.lower()
        for word in _PLATFORM_WORDS:
            assert word not in desc, f"{slug}: description leaks '{word}'"


def test_tool_descriptions_encode_sequencing():
    """The cross-tool fix: an agent reading only the tool list must learn
    the order — get-index first, identity before voice work, propose-feed
    is the only write door."""
    assert "first" in _spec("get-index").description.lower()
    assert "before" in _spec("get-identity").description.lower()
    assert "only" in _spec("propose-feed").description.lower()
    assert "only write" in _spec("propose-feed").description.lower()


def test_assemble_context_output_is_data_only():
    model = AssembleContext.Output.model_fields
    assert set(model.keys()) == {"tier", "context_pack", "entity_ids_used", "gaps"}
    for key in ("tokens", "model", "duration_ms", "operation_id"):
        assert key not in model


def test_assemble_context_description_is_platform_neutral_and_open():
    desc = _spec("assemble-context").description
    for word in _PLATFORM_WORDS:
        assert word not in desc.lower()
    assert "deterministic" in desc.lower()
    assert "open" in desc.lower()
