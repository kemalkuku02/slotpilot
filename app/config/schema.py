from datetime import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator


class WorkInterval(BaseModel):
    start: time
    end: time

    @model_validator(mode="after")
    def check_order(self):
        if self.start >= self.end:
            raise ValueError("work interval start must be before end")
        return self


class Specialist(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9_-]+$")
    name: str
    schedule: dict[int, list[WorkInterval]]

    @model_validator(mode="after")
    def check_weekdays(self):
        if any(day not in range(7) for day in self.schedule):
            raise ValueError("schedule keys must be weekdays 0..6")
        for intervals in self.schedule.values():
            ordered = sorted(intervals, key=lambda interval: interval.start)
            if intervals != ordered:
                raise ValueError("work intervals must be in chronological order")
            if any(previous.end > current.start for previous, current in zip(ordered, ordered[1:])):
                raise ValueError("work intervals must not overlap")
        return self


class Service(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9_-]+$")
    name: str
    price: int = Field(gt=0)
    duration_min: int = Field(gt=0, le=1440)
    specialist_ids: list[str] = Field(min_length=1)


class RedFlags(BaseModel):
    keywords: list[str]
    reply: str


class BusinessConfig(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    business_name: str
    timezone: str
    slot_step_min: int = Field(default=15, gt=0, le=240)
    minimum_lead_min: int = Field(default=30, ge=0, le=10080)
    services: list[Service]
    specialists: list[Specialist]
    red_flags: RedFlags
    qualification_questions: list[str] = Field(default_factory=list)
    tone: str = "Доброжелательно, кратко и по делу."

    @model_validator(mode="after")
    def check_references_and_uniqueness(self):
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone: {self.timezone}") from exc
        specialists = [s.id for s in self.specialists]
        services = [s.id for s in self.services]
        if len(set(specialists)) != len(specialists) or len(set(services)) != len(services):
            raise ValueError("specialist and service ids must be unique")
        known = set(specialists)
        for service in self.services:
            missing = set(service.specialist_ids) - known
            if missing:
                raise ValueError(f"service {service.id}: unknown specialists {sorted(missing)}")
        return self
