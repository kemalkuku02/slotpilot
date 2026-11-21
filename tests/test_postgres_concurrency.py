"""Database-level concurrency checks; enabled by the PostgreSQL CI job."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import Booking, BookingStatus, Client

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is not configured")


def test_postgres_rejects_exactly_one_of_two_simultaneous_overlapping_bookings():
    engine = create_engine(TEST_DATABASE_URL, pool_size=4, max_overflow=0)
    marker = uuid4().hex
    clients = [Client(telegram_id=f"pg-race-{marker}-{index}") for index in range(2)]
    client_ids: list[int] = []
    try:
        with Session(engine) as db:
            db.add_all(clients)
            db.commit()
            client_ids = [client.id for client in clients]

        start_at = datetime.now(UTC) + timedelta(days=2)
        end_at = start_at + timedelta(minutes=30)
        barrier = Barrier(2)

        def attempt(index: int) -> tuple[str, str | None]:
            with Session(engine) as db:
                barrier.wait(timeout=10)
                db.add(Booking(
                    client_id=client_ids[index],
                    specialist_id=f"pg-race-specialist-{marker}",
                    service_id="pg-race-service",
                    start_at=start_at,
                    end_at=end_at,
                    status=BookingStatus.CONFIRMED.value,
                    idempotency_key=f"pg-race-key-{marker}-{index}",
                ))
                try:
                    db.commit()
                    return "created", None
                except IntegrityError as exc:
                    constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
                    db.rollback()
                    return "rejected", constraint

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(attempt, index) for index in range(2)]
            results = [future.result(timeout=20) for future in futures]

        assert sum(result[0] == "created" for result in results) == 1
        rejected = [result for result in results if result[0] == "rejected"]
        assert len(rejected) == 1
        assert rejected[0][1] == "ex_booking_no_overlap"
    finally:
        if client_ids:
            with engine.begin() as conn:
                conn.execute(delete(Booking).where(Booking.client_id.in_(client_ids)))
                conn.execute(delete(Client).where(Client.id.in_(client_ids)))
        engine.dispose()
