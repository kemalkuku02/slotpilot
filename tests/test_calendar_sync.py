from datetime import date, datetime, time, timedelta, timezone

import httpx
import pytest

from app.booking.service import book
from app.calendar.google import GoogleCalendar
from app.calendar_sync import process_one
from app.db.models import CalendarOutbox, Client


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


def test_calendar_outbox_keeps_failed_item_for_retry(db, config, monkeypatch):
    client = Client(telegram_id="calendar-retry")
    db.add(client)
    db.commit()
    day = date.today() + timedelta(days=2)
    book(db, config=config, client_id=client.id, service_id="service", specialist_id="one",
         start_at=datetime.combine(day, time(10), tzinfo=timezone.utc), idempotency_key="calendar-retry-booking")

    class BrokenCalendar(FakeCalendar):
        def create_event(self, **kwargs):
            raise RuntimeError("calendar unavailable")

    import app.calendar_sync as sync

    monkeypatch.setattr(sync, "get_config", lambda: config)
    item = db.query(CalendarOutbox).filter_by(action="create").one()

    assert not process_one(db, BrokenCalendar())
    db.refresh(item)
    assert item.attempts == 1
    assert item.processed_at is None


@pytest.mark.parametrize("status_code", [404, 410])
def test_google_delete_treats_missing_event_as_already_deleted(monkeypatch, status_code):
    calendar = GoogleCalendar(access_token="test-token")

    def missing_event(*args, **kwargs):
        request = httpx.Request("DELETE", "https://calendar.test/events/missing")
        response = httpx.Response(status_code, request=request)
        raise httpx.HTTPStatusError("not found", request=request, response=response)

    monkeypatch.setattr(calendar, "_request", missing_event)
    calendar.delete_event("missing")
