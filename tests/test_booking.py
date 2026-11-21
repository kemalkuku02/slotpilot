from datetime import date, datetime, time, timedelta, timezone

import pytest

from app.booking.service import book, cancel, find_slots, reschedule
from app.db.models import Booking, CalendarOutbox, Client, Conversation, ConversationStatus, Message


def setup_client(db):
    client = Client(telegram_id="test-user")
    db.add(client)
    db.commit()
    db.refresh(client)
    return client


def future_date():
    return date.today() + timedelta(days=2)


def test_finds_slots_and_respects_existing_booking(db, config):
    day = future_date()
    client = setup_client(db)
    first = datetime.combine(day, time(10), tzinfo=timezone.utc)
    book(db, config=config, client_id=client.id, service_id="service", specialist_id="one", start_at=first, idempotency_key="first-booking")
    slots = find_slots(db, config, "service", day, day)
    assert all(datetime.fromisoformat(s["start_at"]) != first for s in slots)
    assert any(datetime.fromisoformat(s["start_at"]) == first + timedelta(minutes=30) for s in slots)


def test_idempotency_prevents_duplicate(db, config):
    client = setup_client(db)
    at = datetime.combine(future_date(), time(10), tzinfo=timezone.utc)
    one = book(db, config=config, client_id=client.id, service_id="service", specialist_id="one", start_at=at, idempotency_key="same-request")
    two = book(db, config=config, client_id=client.id, service_id="service", specialist_id="one", start_at=at, idempotency_key="same-request")
    assert one.id == two.id
    assert db.query(Booking).count() == 1


def test_idempotency_key_cannot_be_reused_for_different_booking(db, config):
    client = setup_client(db)
    day = future_date()
    first_at = datetime.combine(day, time(10), tzinfo=timezone.utc)
    second_at = first_at + timedelta(minutes=30)
    book(db, config=config, client_id=client.id, service_id="service", specialist_id="one",
         start_at=first_at, idempotency_key="reused-key")

    with pytest.raises(ValueError, match="Ключ идемпотентности"):
        book(db, config=config, client_id=client.id, service_id="service", specialist_id="one",
             start_at=second_at, idempotency_key="reused-key")


def test_find_slots_validates_limit_and_time_window(db, config):
    day = future_date()

    with pytest.raises(ValueError, match="Лимит слотов"):
        find_slots(db, config, "service", day, day, limit=0)
    with pytest.raises(ValueError, match="Лимит слотов"):
        find_slots(db, config, "service", day, day, limit=1001)
    with pytest.raises(ValueError, match="Время начала"):
        find_slots(db, config, "service", day, day, after=time(15), before=time(14))


def test_cancel_releases_slot(db, config):
    client = setup_client(db)
    at = datetime.combine(future_date(), time(10), tzinfo=timezone.utc)
    booking = book(db, config=config, client_id=client.id, service_id="service", specialist_id="one", start_at=at, idempotency_key="cancel-me")
    cancel(db, booking.id)
    cancel(db, booking.id)
    assert db.query(CalendarOutbox).filter_by(booking_id=booking.id, action="delete").count() == 1
    slots = find_slots(db, config, "service", at.date(), at.date())
    assert any(datetime.fromisoformat(s["start_at"]) == at for s in slots)


def test_reschedule_is_atomic_and_releases_old_slot(db, config):
    client = setup_client(db)
    day = future_date()
    at = datetime.combine(day, time(10), tzinfo=timezone.utc)
    booking = book(db, config=config, client_id=client.id, service_id="service", specialist_id="one", start_at=at, idempotency_key="move-me")
    moved = reschedule(db, booking.id, at + timedelta(minutes=30), config)
    assert moved.start_at.replace(tzinfo=timezone.utc) == at + timedelta(minutes=30)
    assert db.get(Booking, booking.id).status == "cancelled"
    assert any(datetime.fromisoformat(s["start_at"]) == at for s in find_slots(db, config, "service", day, day))


def test_overlap_is_rejected(db, config):
    client = setup_client(db)
    at = datetime.combine(future_date(), time(10), tzinfo=timezone.utc)
    book(db, config=config, client_id=client.id, service_id="service", specialist_id="one", start_at=at, idempotency_key="overlap-one")
    with pytest.raises(ValueError, match="занято"):
        book(db, config=config, client_id=client.id, service_id="service", specialist_id="one", start_at=at + timedelta(minutes=15), idempotency_key="overlap-two")
