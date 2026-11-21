import hashlib
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from app.booking.service import book
from app.calendar.google import GoogleCalendar
from app.calendar_sync import process_one
from app.db.models import CalendarOutbox, Client, utcnow


class FakeCalendar:
    def __init__(self):
        self.created = []
        self.deleted = []

    def create_event(self, *, title, start, end, external_id):
        self.created.append({"title": title, "start": start, "end": end, "external_id": external_id})
        return f"event-{external_id}"

    def update_event(self, event_id, *, title, start, end):
        raise AssertionError("update_event is not used by the outbox")

    def delete_event(self, event_id):
        self.deleted.append(event_id)


def test_calendar_outbox_creates_and_deletes_mirror_event(db, config, monkeypatch):
    import app.calendar_sync as sync

    monkeypatch.setattr(sync, "get_config", lambda: config)
    client = Client(telegram_id="calendar-test")
    db.add(client)
    db.commit()
    day = date.today() + timedelta(days=2)
    booking = book(db, config=config, client_id=client.id, service_id="service", specialist_id="one",
                   start_at=datetime.combine(day, time(10), tzinfo=timezone.utc), idempotency_key="calendar-booking")
    provider = FakeCalendar()

    assert process_one(db, provider)
    db.refresh(booking)
    create_item = db.query(CalendarOutbox).filter_by(action="create").one()
    assert create_item.processed_at is not None
    assert booking.calendar_event_id == f"event-{booking.id}"
    assert "calendar-test" not in provider.created[0]["title"]

    db.add(CalendarOutbox(booking_id=booking.id, action="delete", payload="{}"))
    db.commit()
    assert process_one(db, provider)
    db.refresh(booking)
    delete_item = db.query(CalendarOutbox).filter_by(action="delete").one()
    assert delete_item.processed_at is not None
    assert provider.deleted == [f"event-{booking.id}"]
    assert booking.calendar_event_id is None


def test_calendar_outbox_backoff_does_not_block_later_items(db, config, monkeypatch):
    client = Client(telegram_id="calendar-retry")
    db.add(client)
    db.commit()
    day = date.today() + timedelta(days=2)
    first = book(db, config=config, client_id=client.id, service_id="service", specialist_id="one",
                 start_at=datetime.combine(day, time(10), tzinfo=timezone.utc), idempotency_key="calendar-retry-booking")
    second = book(db, config=config, client_id=client.id, service_id="service", specialist_id="one",
                  start_at=datetime.combine(day, time(11), tzinfo=timezone.utc), idempotency_key="calendar-later-booking")

    class BrokenCalendar(FakeCalendar):
        def create_event(self, **kwargs):
            if kwargs["external_id"] == str(first.id):
                raise RuntimeError("calendar unavailable")
            return super().create_event(**kwargs)

    import app.calendar_sync as sync

    monkeypatch.setattr(sync, "get_config", lambda: config)
    item = db.query(CalendarOutbox).filter_by(action="create", booking_id=first.id).one()

    provider = BrokenCalendar()
    assert not process_one(db, provider)
    db.refresh(item)
    assert item.attempts == 1
    assert item.processed_at is None
    assert item.last_error == "RuntimeError"
    retry_at = item.next_attempt_at
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    assert retry_at > utcnow()

    assert process_one(db, provider)
    db.refresh(item)
    db.refresh(second)
    assert item.processed_at is None
    assert second.calendar_event_id == f"event-{second.id}"
    assert [entry["external_id"] for entry in provider.created] == [str(second.id)]


@pytest.mark.parametrize("status_code", [404, 410])
def test_google_delete_treats_missing_event_as_already_deleted(monkeypatch, status_code):
    calendar = GoogleCalendar(access_token="test-token")

    def missing_event(*args, **kwargs):
        request = httpx.Request("DELETE", "https://calendar.test/events/missing")
        response = httpx.Response(status_code, request=request)
        raise httpx.HTTPStatusError("not found", request=request, response=response)

    monkeypatch.setattr(calendar, "_request", missing_event)
    calendar.delete_event("missing")


def test_google_oauth_refresh_token_is_cached_until_expiry(monkeypatch):
    import app.calendar.google as google

    now = [100.0]
    monkeypatch.setattr(google, "time", SimpleNamespace(monotonic=lambda: now[0]))
    calls = []

    def refresh_token(url, *, data, timeout):
        calls.append((url, data, timeout))
        request = httpx.Request("POST", url)
        return httpx.Response(200, json={"access_token": f"access-{len(calls)}", "expires_in": 3600}, request=request)

    monkeypatch.setattr(google.httpx, "post", refresh_token)
    calendar = GoogleCalendar(client_id="client-id", client_secret="client-secret", refresh_token="refresh-token")

    assert calendar._token() == "access-1"
    assert calendar._token() == "access-1"
    assert len(calls) == 1
    assert calls[0] == ("https://oauth2.googleapis.com/token", {
        "client_id": "client-id",
        "client_secret": "client-secret",
        "refresh_token": "refresh-token",
        "grant_type": "refresh_token",
    }, 20)

    now[0] = 3641.0
    assert calendar._token() == "access-2"
    assert len(calls) == 2


def test_google_create_event_uses_stable_id_and_calendar_payload(monkeypatch):
    import app.calendar.google as google

    calendar = GoogleCalendar(calendar_id="owner+shared@example.com", access_token="test-token")
    captured = {}

    def send(method, url, *, headers, timeout, **kwargs):
        captured.update(method=method, url=url, headers=headers, timeout=timeout, **kwargs)
        request = httpx.Request(method, url)
        return httpx.Response(200, json={"id": kwargs["json"]["id"]}, request=request)

    monkeypatch.setattr(google.httpx, "request", send)
    start = datetime(2026, 1, 2, 10, tzinfo=timezone.utc)
    end = start + timedelta(minutes=30)
    event_id = calendar.create_event(title="Консультация", start=start, end=end, external_id="booking:42")
    expected_id = hashlib.sha256(b"booking:42").hexdigest()

    assert event_id == expected_id
    assert captured["method"] == "POST"
    assert captured["url"] == "https://www.googleapis.com/calendar/v3/calendars/owner%2Bshared%40example.com/events"
    assert captured["headers"]["Authorization"] == "Bearer test-token"
    assert captured["json"] == {
        "id": expected_id,
        "summary": "Консультация",
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
        "extendedProperties": {"private": {"slotpilotBookingId": "booking:42"}},
    }


def test_google_create_event_recovers_from_duplicate_id(monkeypatch):
    calendar = GoogleCalendar(access_token="test-token")
    event_id = hashlib.sha256(b"booking:duplicate").hexdigest()
    requests = []

    def duplicate_then_get(method, path, **kwargs):
        requests.append((method, path))
        if method == "POST":
            request = httpx.Request("POST", "https://calendar.test/events")
            response = httpx.Response(409, request=request)
            raise httpx.HTTPStatusError("duplicate", request=request, response=response)
        return {"id": event_id}

    monkeypatch.setattr(calendar, "_request", duplicate_then_get)

    result = calendar.create_event(
        title="Consultation",
        start=datetime(2026, 1, 2, 10, tzinfo=timezone.utc),
        end=datetime(2026, 1, 2, 10, 30, tzinfo=timezone.utc),
        external_id="booking:duplicate",
    )

    assert result == event_id
    assert requests == [("POST", ""), ("GET", f"/{event_id}")]
