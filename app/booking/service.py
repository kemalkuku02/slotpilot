import json
import threading
from datetime import UTC, date, datetime, time, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config.schema import BusinessConfig
from app.db.models import Booking, BookingStatus, CalendarOutbox

_BOOKING_LOCK = threading.RLock()


def _utc(dt: datetime, *, require_aware: bool = False) -> datetime:
    if require_aware and (dt.tzinfo is None or dt.utcoffset() is None):
        raise ValueError("Время должно содержать часовой пояс")
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def find_slots(
    db: Session,
    config: BusinessConfig,
    service_id: str,
    start_date: date,
    end_date: date,
    after: time | None = None,
    before: time | None = None,
    limit: int = 20,
) -> list[dict]:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
        raise ValueError("Лимит слотов должен быть от 1 до 1000")
    if after is not None and before is not None and after > before:
        raise ValueError("Время начала должно быть раньше или равно времени окончания")
    service = next((item for item in config.services if item.id == service_id), None)
    if service is None:
        raise ValueError("Неизвестная услуга")
    if end_date < start_date or (end_date - start_date).days > 31:
        raise ValueError("Диапазон поиска должен быть от 0 до 31 дня")
    zone = ZoneInfo(config.timezone)
    range_start = datetime.combine(start_date, time.min, zone).astimezone(UTC)
    range_end = datetime.combine(end_date + timedelta(days=1), time.min, zone).astimezone(UTC)
    specialists = [s for s in config.specialists if s.id in service.specialist_ids]
    bookings = db.scalars(select(Booking).where(
        Booking.specialist_id.in_([s.id for s in specialists]),
        Booking.status == BookingStatus.CONFIRMED.value,
        Booking.start_at < range_end,
        Booking.end_at > range_start,
    )).all()
    occupied: dict[str, list[tuple[datetime, datetime]]] = {s.id: [] for s in specialists}
    for item in bookings:
        occupied[item.specialist_id].append((_utc(item.start_at), _utc(item.end_at)))
    now = datetime.now(UTC) + timedelta(minutes=config.minimum_lead_min)
    step = timedelta(minutes=config.slot_step_min)
    duration = timedelta(minutes=service.duration_min)
    slots = []
    current_day = start_date
    while current_day <= end_date:
        for specialist in specialists:
            for interval in specialist.schedule.get(current_day.weekday(), []):
                wall_cursor = datetime.combine(current_day, interval.start)
                wall_end = datetime.combine(current_day, interval.end)
                while wall_cursor + duration <= wall_end:
                    # Skip ambiguous wall-clock starts and slots whose duration crosses a DST offset change.
                    cursor = wall_cursor.replace(tzinfo=zone, fold=0)
                    alternate_fold = wall_cursor.replace(tzinfo=zone, fold=1)
                    ambiguous_start = cursor.utcoffset() != alternate_fold.utcoffset()
                    slot_start = cursor.astimezone(UTC)
                    roundtrip = slot_start.astimezone(zone).replace(tzinfo=None)
                    slot_end = slot_start + duration
                    local_end = slot_end.astimezone(zone)
                    local_time = wall_cursor.time()
                    fits_schedule = (not ambiguous_start and roundtrip == wall_cursor
                                     and local_end.utcoffset() == cursor.utcoffset()
                                     and local_end.date() == current_day
                                     and local_end.timetz().replace(tzinfo=None) <= interval.end)
                    if fits_schedule and slot_start >= now and (after is None or local_time >= after) and (before is None or local_time <= before):
                        if all(slot_end <= busy_start or slot_start >= busy_end for busy_start, busy_end in occupied[specialist.id]):
                            slots.append({
                                "service_id": service.id,
                                "specialist_id": specialist.id,
                                "specialist_name": specialist.name,
                                "start_at": slot_start.isoformat(),
                                "end_at": slot_end.isoformat(),
                                "local_start": cursor.isoformat(),
                                "price": service.price,
                            })
                    wall_cursor += step
                    if len(slots) >= limit:
                        return slots
        current_day += timedelta(days=1)
    return slots


def book(
    db: Session,
    *,
    config: BusinessConfig,
    client_id: int,
    service_id: str,
    specialist_id: str,
    start_at: datetime,
    idempotency_key: str | None = None,
    commit: bool = True,
) -> Booking:
    service = next((item for item in config.services if item.id == service_id), None)
    if service is None or specialist_id not in service.specialist_ids:
        raise ValueError("Услуга или специалист не найдены")
    start = _utc(start_at, require_aware=True)
    key = idempotency_key or str(uuid4())

    def existing_request() -> Booking | None:
        previous = db.scalar(select(Booking).where(Booking.idempotency_key == key))
        if previous is None:
            return None
        same_request = (
            previous.client_id == client_id
            and previous.service_id == service_id
            and previous.specialist_id == specialist_id
            and _utc(previous.start_at) == start
        )
        if not same_request:
            raise ValueError("Ключ идемпотентности уже использован для другой записи")
        return previous

    with _BOOKING_LOCK:
        previous = existing_request()
        if previous:
            return previous
        start_local = start.astimezone(ZoneInfo(config.timezone))
        slot = find_slots(db, config, service_id, start_local.date(), start_local.date(),
                          after=start_local.timetz().replace(tzinfo=None),
                          limit=1000)
        allowed = any(item["specialist_id"] == specialist_id and _utc(datetime.fromisoformat(item["start_at"])) == start for item in slot)
        if not allowed:
            raise ValueError("Это время уже занято или не входит в расписание")
        booking = Booking(
            client_id=client_id,
            specialist_id=specialist_id,
            service_id=service_id,
            start_at=start,
            end_at=start + timedelta(minutes=service.duration_min),
            status=BookingStatus.CONFIRMED.value,
            idempotency_key=key,
        )
        try:
            db.add(booking)
            db.flush()
            db.add(CalendarOutbox(booking_id=booking.id, action="create", payload=json.dumps({"start_at": start.isoformat()})))
            if commit:
                db.commit()
                db.refresh(booking)
        except IntegrityError as exc:
            db.rollback()
            previous = existing_request()
            if previous:
                return previous
            raise ValueError("Это время уже занято") from exc
        return booking


def cancel(db: Session, booking_id: int) -> Booking:
    booking = db.scalar(select(Booking).where(Booking.id == booking_id).with_for_update()
                        .execution_options(populate_existing=True))
    if booking is None:
        raise ValueError("Запись не найдена")
    if booking.status != BookingStatus.CANCELLED.value:
        booking.status = BookingStatus.CANCELLED.value
        db.add(CalendarOutbox(booking_id=booking.id, action="delete", payload="{}"))
        db.commit()
        db.refresh(booking)
    return booking


def reschedule(db: Session, booking_id: int, new_start: datetime, config: BusinessConfig) -> Booking:
    with _BOOKING_LOCK:
        old = db.scalar(select(Booking).where(Booking.id == booking_id).with_for_update()
                        .execution_options(populate_existing=True))
        if old is None:
            raise ValueError("Запись не найдена")
        if old.status != BookingStatus.CONFIRMED.value:
            raise ValueError("Перенести можно только действующую запись")
        old.status = BookingStatus.CANCELLED.value
        db.flush()
        try:
            replacement = book(db, config=config, client_id=old.client_id, service_id=old.service_id,
                               specialist_id=old.specialist_id, start_at=new_start,
                               idempotency_key=f"reschedule:{booking_id}:{_utc(new_start, require_aware=True).isoformat()}", commit=False)
        except Exception:
            db.rollback()
            raise
        db.add(CalendarOutbox(booking_id=old.id, action="delete", payload="{}"))
        db.commit()
        return replacement
