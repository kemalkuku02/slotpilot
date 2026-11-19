from abc import ABC, abstractmethod
from datetime import datetime


class CalendarProvider(ABC):
    @abstractmethod
    def create_event(self, *, title: str, start: datetime, end: datetime, external_id: str) -> str: ...

    @abstractmethod
    def update_event(self, event_id: str, *, title: str, start: datetime, end: datetime) -> None: ...

    @abstractmethod
    def delete_event(self, event_id: str) -> None: ...


class DbCalendar(CalendarProvider):
    """Database bookings are canonical; this provider is intentionally a no-op mirror."""

    def create_event(self, *, title: str, start: datetime, end: datetime, external_id: str) -> str:
        return external_id

    def update_event(self, event_id: str, *, title: str, start: datetime, end: datetime) -> None:
        return None

    def delete_event(self, event_id: str) -> None:
        return None
