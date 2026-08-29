"""The write door's API surface: `propose-feed` (PLAN.md §6, M2.1).

One endpoint, auto-registered as REST + MCP + docs. It only creates a
pending Feed — the repo is written exclusively by the M2.5 approval
handler after the operator approves in the ops UI. Per-key rate limiting
comes from the registry pipeline (apps.mind.throttle); the payload cap
and trusted URL fetch live in apps.feeds.services.intake.

Tier gate: public-tier keys are refused. The write door is for the
operator's own agents (least privilege — a future audience-facing key
must never be able to spam the approval queue).
"""
from __future__ import annotations

from asgiref.sync import sync_to_async
from pydantic import BaseModel, Field

from apps.core.ctx import Ctx
from apps.core.registry import Endpoint, endpoint
from apps.events.models import credential_via, emit
from apps.feeds.services import intake
from apps.mind import tiers


def _api_key_or_none(credential):
    """`Feed.consumer` and `Event.consumer` are FKs to `api_keys.APIKey`.

    A `URLMCPToken` caller is authenticated and can be above the tier
    gate, but assigning it to either FK raises ValueError — so the
    proposal would 500 for the exact credential claude.ai uses. Attribute
    it to NULL instead, the same way the ops UI form does, and let the
    proposal through. The token's own `last_used_at` records that it
    called; the queue row simply doesn't name a key.
    """
    from apps.api_keys.models import APIKey

    return credential if isinstance(credential, APIKey) else None


@endpoint(
    slug="propose-feed",
    description=(
        "Propose a new source for the mind (a URL, a transcript, a raw "
        "thought). The ONLY write tool. Lands in the human approval queue "
        "as a pending feed — nothing is written to the brain until the "
        "operator approves. Requires an agents-only key or above."
    ),
)
class ProposeFeed(Endpoint):
    class Input(BaseModel):
        source_kind: str = Field(
            default="doc",
            max_length=20,
            description="Short source-kind slug used in the source id: yt, blog, x, repo, doc, thought…",
        )
        title: str = Field(default="", max_length=300, description="Human title of the source, if known.")
        url: str = Field(
            default="",
            max_length=2000,
            description="Source URL. Fetched server-side when no content is pasted.",
        )
        content: str = Field(
            default="",
            description="The source text itself (transcript, post body…). Skips the server-side fetch.",
        )
        notes: str = Field(
            default="",
            max_length=2000,
            description="Guidance for the feeder: what matters in this source, why it's being fed.",
        )
        source_id: str = Field(
            default="",
            max_length=120,
            description="Optional explicit source id (kebab-case). Derived from kind+date+title when empty.",
        )

    class Output(BaseModel):
        feed_id: int
        source_id: str
        status: str
        payload_bytes: int
        fetch_ok: bool | None = Field(
            description="True/False when the server fetched the URL; null when content was pasted."
        )

    async def run(self, inp: Input, ctx: Ctx) -> Output:
        return await sync_to_async(self._work, thread_sensitive=True)(inp, ctx)

    def _work(self, inp: Input, ctx: Ctx) -> Output:
        tier = tiers.tier_for_credential(ctx.credential)
        if tier == "public":
            emit(
                "auth_denied",
                consumer=_api_key_or_none(ctx.credential),
                surface="propose-feed",
                tier=tier,
                **credential_via(ctx.credential),
            )
            raise ValueError("propose-feed requires an agents-only key or above.")
        feed = intake.propose(
            channel="mcp" if ctx.source == "mcp" else "api",
            source_kind=inp.source_kind,
            title=inp.title,
            source_url=inp.url,
            content=inp.content,
            notes=inp.notes,
            source_id=inp.source_id,
            consumer=_api_key_or_none(ctx.credential),
        )
        return self.Output(
            feed_id=feed.pk,
            source_id=feed.source_id,
            status=feed.status,
            payload_bytes=feed.payload_bytes,
            fetch_ok=feed.raw_payload.get("fetch", {}).get("ok"),
        )
