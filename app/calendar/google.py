import os
import hashlib
import time
from datetime import datetime
from urllib.parse import quote

import httpx

from app.calendar.provider import CalendarProvider


class GoogleCalendar(CalendarProvider):
    """Google Calendar mirror using a pre-authorized access token or OAuth refresh token."""

    def __init__(self, calendar_id: str | None = None, *, access_token: str | None = None,
                 client_id: str | None = None, client_secret: str | None = None, refresh_token: str | None = None):
        self.calendar_id = calendar_id or os.getenv("GOOGLE_CALENDAR_ID", "primary")
        self.access_token = access_token or os.getenv("GOOGLE_ACCESS_TOKEN")
        self.client_id = client_id or os.getenv("GOOGLE_CLIENT_ID")
        self.client_secret = client_secret or os.getenv("GOOGLE_CLIENT_SECRET")
        self.refresh_token = refresh_token or os.getenv("GOOGLE_REFRESH_TOKEN")
        self._expiry = 0.0

    def _token(self) -> str:
        if self.access_token and not self.refresh_token:
            return self.access_token
        if not all((self.client_id, self.client_secret, self.refresh_token)):
            raise RuntimeError("Set GOOGLE_ACCESS_TOKEN or GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET/GOOGLE_REFRESH_TOKEN")
        if time.monotonic() >= self._expiry:
            response = httpx.post("https://oauth2.googleapis.com/token", data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
            }, timeout=20)
            response.raise_for_status()
            payload = response.json()
            self.access_token = payload["access_token"]
            self._expiry = time.monotonic() + int(payload.get("expires_in", 3600)) - 60
        return self.access_token or ""

    def _request(self, method: str, path: str, **kwargs):
        calendar_id = quote(self.calendar_id, safe="")
        response = httpx.request(method, f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events{path}",
                                 headers={"Authorization": f"Bearer {self._token()}"}, timeout=20, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else {}

    def create_event(self, *, title: str, start: datetime, end: datetime, external_id: str) -> str:
        event_id = hashlib.sha256(external_id.encode("utf-8")).hexdigest()
        try:
            event = self._request("POST", "", json={
                "id": event_id,
                "summary": title,
                "start": {"dateTime": start.isoformat()},
                "end": {"dateTime": end.isoformat()},
                "extendedProperties": {"private": {"slotpilotBookingId": external_id}},
            })
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 409:
                raise
            event = self._request("GET", f"/{event_id}")
        return event["id"]

    def update_event(self, event_id: str, *, title: str, start: datetime, end: datetime) -> None:
        self._request("PATCH", f"/{event_id}", json={"summary": title, "start": {"dateTime": start.isoformat()}, "end": {"dateTime": end.isoformat()}})

    def delete_event(self, event_id: str) -> None:
        try:
            self._request("DELETE", f"/{event_id}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in {404, 410}:
                raise
