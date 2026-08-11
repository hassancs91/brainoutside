"""Flush junk from the feed approval queue.

Closes (rejects) terminal failures and abandoned pending captures so the
operator can clear a stuck queue without opening each row. Ready-to-review
proposals and in-flight work are left alone — those still need a human
decision or a worker to finish.
"""
from __future__ import annotations

from django.db.models import Q, QuerySet
from django.utils import timezone

from apps.events.models import emit
from apps.feeds.models import Feed

DEFAULT_REASON = "Flushed from feed queue"


def flushable_queryset() -> QuerySet[Feed]:
    """Feeds safe to close in bulk.

    Included:
      - ``failed`` (terminal apply/push failure — ops UI otherwise dead-ends)
      - ``pending`` with an ``error`` (extraction refused / exhausted retries)
      - ``pending`` with no proposal and extraction not in flight (abandoned)

    Excluded:
      - ``pending`` with a proposal (ready to review)
      - ``pending`` with ``extract_queued_at`` set (worker may still be running;
        ``extraction_in_flight`` is time-bounded, so we also keep any row that
        still has the stamp — operator can retry or wait)
      - ``approving`` / ``approved`` / ``edited`` / ``rejected``
    """
    abandoned_pending = Q(status="pending", proposal__isnull=True, extract_queued_at__isnull=True)
    errored_pending = Q(status="pending") & ~Q(error="")
    return Feed.objects.filter(Q(status="failed") | abandoned_pending | errored_pending)


def flush_queue(*, reason: str = DEFAULT_REASON) -> int:
    """Reject every flushable feed. Returns how many rows closed."""
    note = (reason or DEFAULT_REASON).strip() or DEFAULT_REASON
    now = timezone.now()
    targets = list(flushable_queryset().order_by("pk"))
    if not targets:
        return 0

    ids = [f.pk for f in targets]
    Feed.objects.filter(pk__in=ids).update(
        status="rejected",
        decided_at=now,
        decision_note=note,
        extract_queued_at=None,
        approve_claimed_at=None,
    )
    for feed in targets:
        emit(
            "feed",
            action="rejected",
            feed_id=feed.pk,
            source_id=feed.source_id,
            reason=note,
            via="flush",
        )
    return len(targets)
