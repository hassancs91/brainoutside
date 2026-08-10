"""The brain's dumb read layer — endpoints serving tier-snapshot bytes.

Every endpoint (REST + MCP, one class each — the vendored registry):
1. resolves the caller's tier from its API key's consumer profile
   (no profile -> public; anonymous never reaches here),
2. serves ONLY from that tier's materialized snapshot,
3. answers "unknown ..." identically for missing and above-tier targets
   (existence is never revealed downward),
4. emits a `read` Event with the entity ids served.

`assemble-context` (M3.2) is the smart layer: same tier resolution and
snapshot boundary, but a deterministic reader selects and assembles the
context (open, model-free). `get-protocol` teaches any consumer — any
MCP/AI tool, not just one vendor's — the order and contract for using
the brain's tools.
"""
from __future__ import annotations

from asgiref.sync import sync_to_async
from pydantic import BaseModel, Field

from apps.brain.models import Entity
from apps.core.ctx import Ctx
from apps.core.registry import Endpoint, endpoint
from apps.events.models import emit
from apps.mind import files, tiers

PROTOCOL_TEXT = """# Brain MCP protocol

The brain is a read-first system with one gated write path. Tools are
tier-scoped: you only ever see and serve what your key is allowed to see.

## Reading order

1. **get-index** — start here. The tier-scoped index of the whole brain.
2. **get-identity** — before any task in the owner's voice or context,
   load the identity core (voice rules, beliefs, boundaries).
3. **get-lens / list-notes / get-note / get-raw** — targeted retrieval.
   list-notes filters by kind/topic/project; get-note returns one note's
   full markdown; get-raw serves archived files only when a visible note
   links them.
4. **assemble-context** — for a task, ask the brain to assemble a
   task-shaped context pack (identity + the right notes, VERBATIM quotes
   preserved). Deterministic and open — no model required.

## Writing

5. **propose-feed** — the ONLY write path. It creates a pending proposal
   in the human approval queue; nothing is written to the brain until the
   operator approves. Always submit the real source (URL or pasted
   content) plus guidance on what matters in it.

## Rules for consumers

- The brain returns data, not decisions: no model, no tokens, no
  platform-specific metadata. Bring your own.
- Tier is set by your key, never by a request parameter.
- Read the identity core before voice/context work.
- Never retry assemble-context aggressively — it is deterministic and
  cheap, but a retry returns the same pack for the same task.
- propose-feed is gated: a public-tier key is refused.
"""


def _tier(ctx: Ctx) -> str:
    return tiers.tier_for_credential(ctx.credential)


def _cred(ctx: Ctx):
    from apps.api_keys.models import APIKey

    return ctx.credential if isinstance(ctx.credential, APIKey) else None


def _via(ctx: Ctx) -> dict:
    """`events.models.credential_via` for this ctx — the label that keeps
    a connector read from rendering as "ops" in the activity feed."""
    from apps.events.models import credential_via

    return credential_via(ctx.credential)


def _visible_entities(tier: str):
    # `.get(tier, 0)` like every sibling rank check: an unknown tier reads
    # as public — fail closed, never a KeyError that 500s the endpoint.
    rank = tiers.TIER_ORDER.get(tier, 0)
    return [e for e in Entity.objects.all() if tiers.TIER_ORDER.get(e.visibility, 2) <= rank]


class NoteRow(BaseModel):
    entity_id: str
    kind: str
    title: str
    description: str
    status: str
    date: str
    last_verified: str
    path: str
    topics: list[str]


def _row(e: Entity) -> NoteRow:
    return NoteRow(
        entity_id=e.entity_id,
        kind=e.kind,
        title=e.title,
        description=e.description,
        status=e.status,
        date=e.date,
        last_verified=e.last_verified.isoformat() if e.last_verified else "",
        path=e.path,
        topics=list(e.topics),
    )


@endpoint(slug="ping", description="Authenticated liveness check for the brain server.")
class Ping(Endpoint):
    class Input(BaseModel):
        pass

    class Output(BaseModel):
        pong: bool
        service: str
        tier: str

    async def run(self, inp: Input, ctx: Ctx) -> Output:
        return await sync_to_async(self._work, thread_sensitive=True)(inp, ctx)

    def _work(self, inp: Input, ctx: Ctx) -> Output:
        return self.Output(pong=True, service="my-brain-web-app", tier=_tier(ctx))


@endpoint(
    slug="get-protocol",
    description=(
        "How to use this brain's tools, in order — read before anything "
        "else. Cross-tool and platform-neutral: works from any MCP/AI "
        "consumer."
    ),
)
class GetProtocol(Endpoint):
    class Input(BaseModel):
        pass

    class Output(BaseModel):
        protocol: str

    async def run(self, inp: Input, ctx: Ctx) -> Output:
        return await sync_to_async(self._work, thread_sensitive=True)(inp, ctx)

    def _work(self, inp: Input, ctx: Ctx) -> Output:
        return self.Output(protocol=PROTOCOL_TEXT)


@endpoint(
    slug="get-index",
    description=(
        "The brain's index, generated for your visibility tier. Read this "
        "first — before any other tool — to see what the brain holds."
    ),
)
class GetIndex(Endpoint):
    class Input(BaseModel):
        pass

    class Output(BaseModel):
        tier: str
        index: str

    async def run(self, inp: Input, ctx: Ctx) -> Output:
        return await sync_to_async(self._work, thread_sensitive=True)(inp, ctx)

    def _work(self, inp: Input, ctx: Ctx) -> Output:
        tier = _tier(ctx)
        content = files.read(tier, "INDEX.md")
        emit("read", consumer=_cred(ctx), entity_ids=["INDEX"], endpoint="get-index", tier=tier, **_via(ctx))
        return self.Output(tier=tier, index=content)


@endpoint(
    slug="list-notes",
    description=(
        "List brain entities visible to you, filterable by kind "
        "(take/story/lesson/fact/project/identity/catalog/lens), topic, "
        "project, and status. Use after get-index, before get-note."
    ),
)
class ListNotes(Endpoint):
    class Input(BaseModel):
        kind: str = Field(default="", description="Filter by kind; empty = all.")
        topic: str = Field(default="", description="Filter by taxonomy topic; empty = all.")
        project: str = Field(default="", description="Filter by touched project; empty = all.")
        status: str = Field(default="current", description="'current' (default), 'superseded', or '' for all.")

    class Output(BaseModel):
        tier: str
        count: int
        notes: list[NoteRow]

    async def run(self, inp: Input, ctx: Ctx) -> Output:
        return await sync_to_async(self._work, thread_sensitive=True)(inp, ctx)

    def _work(self, inp: Input, ctx: Ctx) -> Output:
        tier = _tier(ctx)
        ents = _visible_entities(tier)
        if inp.kind:
            ents = [e for e in ents if e.kind == inp.kind]
        if inp.topic:
            ents = [e for e in ents if inp.topic in e.topics]
        if inp.project:
            ents = [e for e in ents if inp.project in e.projects]
        if inp.status == "current":
            ents = [e for e in ents if e.status != "superseded"]
        elif inp.status == "superseded":
            ents = [e for e in ents if e.status == "superseded"]
        rows = [_row(e) for e in sorted(ents, key=lambda x: (x.kind, x.entity_id))]
        emit(
            "read",
            consumer=_cred(ctx),
            entity_ids=[r.entity_id for r in rows][:100],
            endpoint="list-notes",
            tier=tier,
            filters=inp.model_dump(),
            count=len(rows),
            **_via(ctx),
        )
        return self.Output(tier=tier, count=len(rows), notes=rows)


@endpoint(slug="get-note", description="Full markdown of one entity by its entity_id.")
class GetNote(Endpoint):
    class Input(BaseModel):
        entity_id: str = Field(min_length=1, max_length=200)

    class Output(BaseModel):
        tier: str
        entity_id: str
        path: str
        content: str

    async def run(self, inp: Input, ctx: Ctx) -> Output:
        return await sync_to_async(self._work, thread_sensitive=True)(inp, ctx)

    def _work(self, inp: Input, ctx: Ctx) -> Output:
        tier = _tier(ctx)
        e = Entity.objects.filter(entity_id=inp.entity_id).first()
        if e is None or not tiers.allows(tier, e.visibility):
            if e is not None:
                emit("auth_denied", consumer=_cred(ctx), entity_ids=[inp.entity_id], surface="get-note", tier=tier, **_via(ctx))
            raise ValueError(f"unknown entity: {inp.entity_id}")
        content = files.read(tier, e.path)
        emit("read", consumer=_cred(ctx), entity_ids=[e.entity_id], endpoint="get-note", tier=tier, **_via(ctx))
        return self.Output(tier=tier, entity_id=e.entity_id, path=e.path, content=content)


@endpoint(
    slug="get-lens",
    description=(
        "A lens file — a named retrieval scope (topics, types, ceiling). "
        "Pass a lens to assemble-context to scope the pack."
    ),
)
class GetLens(Endpoint):
    class Input(BaseModel):
        name: str = Field(min_length=1, max_length=100, description="Lens name, e.g. 'self-hosting'.")

    class Output(BaseModel):
        tier: str
        name: str
        content: str

    async def run(self, inp: Input, ctx: Ctx) -> Output:
        return await sync_to_async(self._work, thread_sensitive=True)(inp, ctx)

    def _work(self, inp: Input, ctx: Ctx) -> Output:
        tier = _tier(ctx)
        e = Entity.objects.filter(entity_id=f"lens-{inp.name}").first()
        if e is None or not tiers.allows(tier, e.visibility):
            raise ValueError(f"unknown lens: {inp.name}")
        content = files.read(tier, e.path)
        emit("read", consumer=_cred(ctx), entity_ids=[e.entity_id], endpoint="get-lens", tier=tier, **_via(ctx))
        return self.Output(tier=tier, name=inp.name, content=content)


class IdentityFile(BaseModel):
    entity_id: str
    path: str
    content: str


@endpoint(
    slug="get-identity",
    description=(
        "The identity core visible at your tier — voice rules, beliefs, "
        "boundaries. Load before ANY task in the owner's voice or "
        "context; it is the single source for how to sound like them."
    ),
)
class GetIdentity(Endpoint):
    class Input(BaseModel):
        pass

    class Output(BaseModel):
        tier: str
        files: list[IdentityFile]

    async def run(self, inp: Input, ctx: Ctx) -> Output:
        return await sync_to_async(self._work, thread_sensitive=True)(inp, ctx)

    def _work(self, inp: Input, ctx: Ctx) -> Output:
        tier = _tier(ctx)
        out: list[IdentityFile] = []
        for e in _visible_entities(tier):
            if e.kind != "identity":
                continue
            out.append(IdentityFile(entity_id=e.entity_id, path=e.path, content=files.read(tier, e.path)))
        emit(
            "read",
            consumer=_cred(ctx),
            entity_ids=[f.entity_id for f in out],
            endpoint="get-identity",
            tier=tier,
            **_via(ctx),
        )
        return self.Output(tier=tier, files=out)


@endpoint(
    slug="get-raw",
    description=(
        "A raw/ archive file, reachable ONLY when a note visible to you "
        "links it. Use for depth after reading the linking note; never "
        "before."
    ),
)
class GetRaw(Endpoint):
    class Input(BaseModel):
        path: str = Field(min_length=5, max_length=300, description="e.g. 'raw/toolerbox-tools-catalog.md'")

    class Output(BaseModel):
        tier: str
        path: str
        content: str

    async def run(self, inp: Input, ctx: Ctx) -> Output:
        return await sync_to_async(self._work, thread_sensitive=True)(inp, ctx)

    def _work(self, inp: Input, ctx: Ctx) -> Output:
        tier = _tier(ctx)
        if not inp.path.startswith("raw/"):
            raise ValueError("get-raw serves only raw/ paths.")
        try:
            # subdir= is the guard that holds. The prefix test above only
            # produces the friendlier message: `raw/../INDEX.md` passes it.
            content = files.read(tier, inp.path, subdir="raw")
        except files.SnapshotMiss:
            emit("auth_denied", consumer=_cred(ctx), entity_ids=[], surface="get-raw", tier=tier, path=inp.path, **_via(ctx))
            raise ValueError(f"unknown path: {inp.path}") from None
        emit("read", consumer=_cred(ctx), entity_ids=[inp.path], endpoint="get-raw", tier=tier, **_via(ctx))
        return self.Output(tier=tier, path=inp.path, content=content)


@endpoint(
    slug="assemble-context",
    description=(
        "Smart reader: assembles a task-shaped context pack from the mind "
        "at your tier — identity, the right notes, VERBATIM quotes "
        "preserved. Deterministic and open (no model required). Returns "
        "the pack and the exact entity ids it drew from."
    ),
)
class AssembleContext(Endpoint):
    class Input(BaseModel):
        task: str = Field(
            min_length=1,
            max_length=8000,
            description="What the consumer is about to do — the reader selects context for THIS, not a search query.",
        )
        lens: str = Field("", max_length=100, description="Optional lens name, e.g. 'self-hosting'.")

    class Output(BaseModel):
        tier: str
        context_pack: str
        entity_ids_used: list[str]
        gaps: list[str]

    async def run(self, inp: Input, ctx: Ctx) -> Output:
        from apps.reader.services import reader

        tier = await sync_to_async(_tier, thread_sensitive=True)(ctx)
        try:
            result = await reader.assemble_context(
                task=inp.task, lens=inp.lens, tier=tier, subject=_cred(ctx)
            )
        except reader.ReaderError as exc:
            raise ValueError(str(exc)) from exc
        await sync_to_async(emit, thread_sensitive=True)(
            "read",
            consumer=_cred(ctx),
            entity_ids=result["entity_ids_used"],
            endpoint="assemble-context",
            tier=tier,
            lens=inp.lens or "",
            **_via(ctx),
        )
        return self.Output(
            tier=tier,
            context_pack=result["context_pack"],
            entity_ids_used=result["entity_ids_used"],
            gaps=result["gaps"],
        )
