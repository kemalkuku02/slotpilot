import json
import os
import time

from sqlalchemy import select

from app.calendar.google import GoogleCalendar
from app.config.loader import get_config
from app.db.models import Booking, CalendarOutbox, Client
from app.db.session import SessionLocal


def process_one(db) -> bool:
    item = db.scalar(select(CalendarOutbox).where(CalendarOutbox.processed_at.is_(None)).order_by(CalendarOutbox.id).with_for_update(skip_locked=True).limit(1))
    if item is None:
        return False
    provider = GoogleCalendar()
    booking = db.get(Booking, item.booking_id)
    if booking is None:
        item.processed_at = __import__("app.db.models", fromlist=["utcnow"]).utcnow()
        db.commit()
        return True
    try:
        if item.action == "create":
            config = get_config()
            service = next(s for s in config.services if s.id == booking.service_id)
            client = db.get(Client, booking.client_id)
            title = f"{service.name} — {client.name or 'Клиент'}"
            event_id = provider.create_event(title=title, start=booking.start_at, end=booking.end_at, external_id=str(booking.id))
            booking.calendar_event_id = event_id
        elif item.action == "delete" and booking.calendar_event_id:
            provider.delete_event(booking.calendar_event_id)
            booking.calendar_event_id = None
        item.processed_at = __import__("app.db.models", fromlist=["utcnow"]).utcnow()
        db.commit()
    except Exception:
        item.attempts += 1
        db.commit()
        return False
    return True


def main():
    delay = int(os.getenv("CALENDAR_RETRY_SECONDS", "20"))
    while True:
        with SessionLocal() as db:
            worked = process_one(db)
        if not worked:
            time.sleep(delay)


if __name__ == "__main__":
    main()
