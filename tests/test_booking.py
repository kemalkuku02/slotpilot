from datetime import UTC, date, datetime, time, timedelta, timezone

import pytest

from app.booking.service import book, cancel, find_slots, reschedule
from app.config.schema import BusinessConfig
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


def test_find_slots_respects_inclusive_local_time_window(db, config):
    day = future_date()

    slots = find_slots(db, config, "service", day, day, after=time(10, 30), before=time(11))

    starts = [datetime.fromisoformat(item["local_start"]).time().replace(tzinfo=None) for item in slots]
    assert starts
    assert min(starts) == time(10, 30)
    assert max(starts) == time(11)


def test_service_longer_than_work_interval_has_no_slots(db, config):
    value = config.model_dump()
    value["services"][0]["duration_min"] = 121
    long_service_config = BusinessConfig.model_validate(value)
    day = future_date()

    assert find_slots(db, long_service_config, "service", day, day) == []


def test_specialist_off_day_has_no_slots(db, config):
    value = config.model_dump()
    day = future_date()
    value["specialists"][0]["schedule"][day.weekday()] = []
    off_day_config = BusinessConfig.model_validate(value)

    assert find_slots(db, off_day_config, "service", day, day) == []


def test_find_slots_converts_business_local_time_to_utc(db, config):
    value = config.model_dump()
    value["timezone"] = "America/Los_Angeles"
    los_angeles_config = BusinessConfig.model_validate(value)
    day = future_date()

    slot = find_slots(db, los_angeles_config, "service", day, day, limit=1)[0]

    local_start = datetime.fromisoformat(slot["local_start"])
    utc_start = datetime.fromisoformat(slot["start_at"])
    assert local_start.hour == 10
    assert utc_start.tzinfo is not None
    assert local_start.astimezone(UTC) == utc_start


def _nth_sunday(year: int, month: int, number: int) -> date:
    first = date(year, month, 1)
    first_sunday = first + timedelta(days=(6 - first.weekday()) % 7)
    return first_sunday + timedelta(days=7 * (number - 1))


def _dst_config(config, day: date) -> BusinessConfig:
    value = config.model_dump()
    value["timezone"] = "America/New_York"
    value["slot_step_min"] = 30
    value["specialists"] = [{
        "id": "one",
        "name": "Specialist",
        "schedule": {day.weekday(): [{"start": "01:00", "end": "04:00"}]},
    }]
    return BusinessConfig.model_validate(value)


def test_spring_forward_skips_nonexistent_and_transition_crossing_slots(db, config):
    today = date.today()
    day = _nth_sunday(today.year, 3, 2)
    if day <= today:
        day = _nth_sunday(today.year + 1, 3, 2)
    spring_config = _dst_config(config, day)

    slots = find_slots(db, spring_config, "service", day, day, limit=50)

    local_starts = [datetime.fromisoformat(item["local_start"]).strftime("%H:%M") for item in slots]
    assert local_starts == ["01:00", "03:00", "03:30"]
    assert all(datetime.fromisoformat(item["local_start"]).utcoffset() == timedelta(hours=-4)
               for item in slots if item["local_start"].startswith(f"{day.isoformat()}T03:"))


def test_fall_back_skips_ambiguous_repeated_hour(db, config):
    today = date.today()
    day = _nth_sunday(today.year, 11, 1)
    if day <= today:
        day = _nth_sunday(today.year + 1, 11, 1)
    fall_config = _dst_config(config, day)

    slots = find_slots(db, fall_config, "service", day, day, limit=50)

    local_starts = [datetime.fromisoformat(item["local_start"]).strftime("%H:%M") for item in slots]
    assert local_starts == ["02:00", "02:30", "03:00", "03:30"]
    assert all(datetime.fromisoformat(item["local_start"]).utcoffset() == -timedelta(hours=5)
               for item in slots)


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
