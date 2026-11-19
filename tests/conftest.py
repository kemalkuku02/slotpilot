from datetime import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config.schema import BusinessConfig
from app.db.models import Base


@pytest.fixture
def config():
    return BusinessConfig.model_validate({
        "business_name": "Test Studio", "timezone": "UTC", "slot_step_min": 15, "minimum_lead_min": 0,
        "specialists": [{"id": "one", "name": "Specialist", "schedule": {i: [{"start": "10:00", "end": "12:00"}] for i in range(7)}}],
        "services": [{"id": "service", "name": "Service", "price": 1000, "duration_min": 30, "specialist_ids": ["one"]}],
        "red_flags": {"keywords": ["острая боль", "аллергия"], "reply": "Передал администратору."},
    })


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=__import__("sqlalchemy.pool", fromlist=["StaticPool"]).StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    Base.metadata.drop_all(engine)
    engine.dispose()
