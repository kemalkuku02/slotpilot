import logging
import os
import time
from datetime import timedelta

from sqlalchemy import select

from app.calendar.google import GoogleCalendar
from app.config.loader import get_config
from app.db.models import Booking, CalendarOutbox, utcnow
from app.db.session import SessionLocal

logger = logging.getLogger(__name__)


def _retry_delay_seconds(attempts: int) -> int:
    base = max(1, int(os.getenv("CALENDAR_RETRY_SECONDS", "20")))
    maximum = max(base, 3600)
    return min(base * (2 ** min(max(attempts - 1, 0), 30)), maximum)


def process_one(db, provider: GoogleCalendar | None = None) -> bool:
    now = utcnow()
    item = db.scalar(select(CalendarOutbox).where(
        CalendarOutbox.processed_at.is_(None),
        CalendarOutbox.next_attempt_at <= now,
    ).order_by(CalendarOutbox.next_attempt_at, CalendarOutbox.id)
        .with_for_update(skip_locked=True).limit(1))
    if item is None:
        return False
    provider = provider or GoogleCalendar()
    booking = db.get(Booking, item.booking_id)
    if booking is None:
        item.processed_at = utcnow()
        db.commit()
        return True
    try:
        if item.action == "create":
            config = get_config()
            service = next(s for s in config.services if s.id == booking.service_id)
            title = f"{service.name} — запись #{booking.id}"
            event_id = provider.create_event(title=title, start=booking.start_at, end=booking.end_at, external_id=str(booking.id))
            booking.calendar_event_id = event_id
        elif item.action == "delete" and booking.calendar_event_id:
            provider.delete_event(booking.calendar_event_id)
            booking.calendar_event_id = None
        item.processed_at = utcnow()
        db.commit()
    except Exception as exc:
        item.attempts += 1
        item.next_attempt_at = now + timedelta(seconds=_retry_delay_seconds(item.attempts))
        item.last_error = type(exc).__name__[:100]
        db.commit()
        logger.warning("Calendar outbox item %s failed on attempt %s (%s)",
                       item.id, item.attempts, item.last_error)
        return False
    return True


def main():
    delay = int(os.getenv("CALENDAR_RETRY_SECONDS", "20"))
    provider = GoogleCalendar()
    while True:
        with SessionLocal() as db:
            worked = process_one(db, provider)
        if not worked:
            time.sleep(delay)


if __name__ == "__main__":
    main()
