"""Ephemeral, authenticated diagnostics exchanged between CouchMate clients."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import secrets
from typing import Any, Final


SCREENSHOT_REQUEST_LIFETIME_SECONDS: Final[int] = 45
SCREENSHOT_RESULT_LIFETIME_SECONDS: Final[int] = 5 * 60
SCREENSHOT_MAX_BYTES: Final[int] = 8 * 1024 * 1024


@dataclass
class ScreenshotRequest:
    """One on-demand screenshot request and its short-lived result."""

    request_id: str
    requester_client_id: str
    target_client_id: str
    created_at: datetime
    expires_at: datetime
    status: str = "pending"
    captured_at: datetime | None = None
    content_type: str | None = None
    image_data: bytes | None = None

    def refresh_status(self) -> str:
        if self.status == "pending" and datetime.now(timezone.utc) >= self.expires_at:
            self.status = "expired"
        return self.status

    def metadata(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "target_client_id": self.target_client_id,
            "status": self.refresh_status(),
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "captured_at": self.captured_at.isoformat() if self.captured_at else None,
            "content_type": self.content_type,
            "byte_count": len(self.image_data) if self.image_data is not None else None,
        }


class DiagnosticsManager:
    """Hold screenshot requests in memory and purge them automatically."""

    def __init__(self) -> None:
        self._requests: dict[str, ScreenshotRequest] = {}

    def create_screenshot_request(
        self,
        requester_client_id: str,
        target_client_id: str,
    ) -> ScreenshotRequest:
        self.cleanup()
        now = datetime.now(timezone.utc)
        request = ScreenshotRequest(
            request_id=secrets.token_urlsafe(24),
            requester_client_id=requester_client_id,
            target_client_id=target_client_id,
            created_at=now,
            expires_at=now + timedelta(seconds=SCREENSHOT_REQUEST_LIFETIME_SECONDS),
        )
        self._requests[request.request_id] = request
        return request

    def pending_for_target(self, target_client_id: str) -> ScreenshotRequest | None:
        self.cleanup()
        candidates = [
            request
            for request in self._requests.values()
            if request.target_client_id == target_client_id
            and request.refresh_status() == "pending"
        ]
        return max(candidates, key=lambda request: request.created_at, default=None)

    def request_for_requester(
        self,
        request_id: str,
        requester_client_id: str,
    ) -> ScreenshotRequest | None:
        self.cleanup()
        request = self._requests.get(request_id)
        if request is None or request.requester_client_id != requester_client_id:
            return None
        request.refresh_status()
        return request

    def complete_screenshot_request(
        self,
        request_id: str,
        target_client_id: str,
        image_data: bytes,
        content_type: str,
    ) -> ScreenshotRequest | None:
        self.cleanup()
        request = self._requests.get(request_id)
        if (
            request is None
            or request.target_client_id != target_client_id
            or request.refresh_status() != "pending"
        ):
            return None
        request.status = "ready"
        request.captured_at = datetime.now(timezone.utc)
        request.content_type = content_type
        request.image_data = image_data
        return request

    def cleanup(self) -> None:
        now = datetime.now(timezone.utc)
        stale_ids: list[str] = []
        for request_id, request in self._requests.items():
            request.refresh_status()
            retention_start = request.captured_at or request.expires_at
            if now >= retention_start + timedelta(seconds=SCREENSHOT_RESULT_LIFETIME_SECONDS):
                stale_ids.append(request_id)
        for request_id in stale_ids:
            self._requests.pop(request_id, None)
