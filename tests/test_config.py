from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config.loader import load_config
from app.config.schema import BusinessConfig

ROOT = Path(__file__).resolve().parents[1]


def test_clinic_and_barbershop_configs_load():
    clinic = load_config(ROOT / "configs" / "clinic.yaml")
    barber = load_config(ROOT / "configs" / "barbershop.yaml")
    assert len(clinic.services) == 3
    assert clinic.specialists[0].id == "smirnova"
    assert len(barber.services) == 2


def test_unknown_specialist_rejected():
    with pytest.raises(ValidationError):
        BusinessConfig.model_validate({
            "business_name": "x", "timezone": "UTC",
            "services": [{"id": "a", "name": "a", "price": 1, "duration_min": 30, "specialist_ids": ["ghost"]}],
            "specialists": [], "red_flags": {"keywords": [], "reply": "-"},
        })


def test_invalid_work_interval_rejected(config):
    value = config.model_dump()
    value["specialists"][0]["schedule"][0] = [{"start": "13:00", "end": "12:00"}]
    with pytest.raises(ValidationError):
        BusinessConfig.model_validate(value)


def test_overlapping_work_intervals_rejected(config):
    value = config.model_dump()
    value["specialists"][0]["schedule"][0] = [
        {"start": "09:00", "end": "13:00"},
        {"start": "12:30", "end": "17:00"},
    ]

    with pytest.raises(ValidationError, match="must not overlap"):
        BusinessConfig.model_validate(value)
