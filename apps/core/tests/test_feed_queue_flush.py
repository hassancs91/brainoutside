"""Flush closes failed/stuck feeds without touching ready-to-review ones."""
from __future__ import annotations

import pytest

from apps.feeds.models import Feed
from apps.feeds.services import flush


pytestmark = pytest.mark.django_db


def _feed(**kwargs) -> Feed:
    defaults = {
        "source_id": "src",
        "channel": "ui",
        "status": "pending",
        "raw_payload": {"source_kind": "doc", "content": "x"},
        "error": "",
    }
    defaults.update(kwargs)
    return Feed.objects.create(**defaults)


def test_flush_rejects_failed_and_errored_pending_and_abandoned():
    failed = _feed(source_id="failed-one", status="failed", error="push died")
    errored = _feed(source_id="errored", status="pending", error="extraction attempt 3/3 failed: Timeout")
    abandoned = _feed(source_id="abandoned", status="pending", proposal=None)
    ready = _feed(
        source_id="ready",
        status="pending",
        proposal={"source_id": "ready", "summary": "ok", "files": [], "index_lines": [], "supersedes": [], "taxonomy_additions": [], "issues": []},
    )
    approved = _feed(source_id="landed", status="approved")
    from django.utils import timezone

    extracting = _feed(source_id="extracting", status="pending", extract_queued_at=timezone.now())

    n = flush.flush_queue()

    assert n == 3
    for f in (failed, errored, abandoned):
        f.refresh_from_db()
        assert f.status == "rejected"
        assert f.decision_note == flush.DEFAULT_REASON
        assert f.extract_queued_at is None
    for f in (ready, approved, extracting):
        f.refresh_from_db()
        assert f.status in ("pending", "approved")
        if f.source_id == "ready":
            assert f.status == "pending" and f.proposal is not None
        if f.source_id == "extracting":
            assert f.status == "pending" and f.extract_queued_at is not None


def test_flush_is_noop_when_queue_is_clean():
    _feed(
        source_id="ready",
        proposal={"source_id": "ready", "summary": "ok", "files": [], "index_lines": [], "supersedes": [], "taxonomy_additions": [], "issues": []},
    )
    assert flush.flush_queue() == 0
    assert Feed.objects.filter(status="rejected").count() == 0


def test_queue_post_flush_closes_junk(brain):
    """Drive the ops queue view POST path (auth/rendering are out of scope)."""
    from django.test import RequestFactory

    import apps.feeds.ops_views as ops_views

    _feed(source_id="junk", status="failed", error="boom")
    request = RequestFactory().post("/ops/feeds/", {"action": "flush"})
    request._messages = _NullMessages(request)
    # staff_member_required is on the view; call the body via unbound
    # method by patching the decorator layer — invoke the POST branch
    # the same way production does after auth.
    response = ops_views.queue.__wrapped__(request)  # type: ignore[attr-defined]

    assert response.status_code in (302, 200)
    assert Feed.objects.get(source_id="junk").status == "rejected"


def test_reject_works_on_failed_detail(brain):
    import apps.feeds.ops_views as ops_views
    from django.test import RequestFactory

    feed = _feed(source_id="dead", status="failed", error="push")
    request = RequestFactory().post(
        f"/ops/feeds/{feed.pk}/", {"action": "reject", "reason": "drop it"}
    )
    request._messages = _NullMessages(request)
    ops_views._handle_action(request, feed)
    feed.refresh_from_db()
    assert feed.status == "rejected"
    assert feed.decision_note == "drop it"


class _NullMessages:
    def __init__(self, request):
        self.request = request
        self.messages = []

    def add(self, level, message, extra_tags=""):
        self.messages.append((level, str(message)))
