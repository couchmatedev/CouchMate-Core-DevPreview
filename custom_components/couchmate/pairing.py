"""Secure local pairing manager for CouchMate clients."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
import hashlib
import hmac
import secrets
from typing import Any, Final

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    PAIRING_CLIENT_STORAGE_KEY,
    PAIRING_CLIENT_STORAGE_VERSION,
    PAIRING_SESSION_LIFETIME_SECONDS,
)

_CODE_ALPHABET: Final[str] = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_ALLOWED_CLIENT_CAPABILITIES: Final[frozenset[str]] = frozenset(
    {"configuration:write", "backgrounds:write", "dashboard:write"}
)


class PairingStatus(StrEnum):
    WAITING = "waiting"
    APPROVED = "approved"
    EXCHANGED = "exchanged"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


@dataclass(slots=True)
class PairingSession:
    session_id: str
    code: str
    device_name: str
    created_at: datetime
    expires_at: datetime
    status: PairingStatus = PairingStatus.WAITING
    exchange_token: str | None = None
    capabilities: tuple[str, ...] = ()

    def refresh_status(self) -> PairingStatus:
        if self.status in (PairingStatus.WAITING, PairingStatus.APPROVED) and datetime.now(UTC) >= self.expires_at:
            self.status = PairingStatus.EXPIRED
        return self.status

    @property
    def remaining_seconds(self) -> int:
        return max(0, int((self.expires_at - datetime.now(UTC)).total_seconds()))

    def public_dict(self) -> dict[str, Any]:
        status = self.refresh_status()
        return {
            "session_id": self.session_id,
            "code": self.code,
            "device_name": self.device_name,
            "status": status.value,
            "expires_in": self.remaining_seconds,
        }


class PairingManager:
    """Manage short-lived pairing requests and persistent client credentials."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._sessions: dict[str, PairingSession] = {}
        self._session_ids_by_code: dict[str, str] = {}
        self._clients: dict[str, dict[str, Any]] = {}
        self._exchange_lock = asyncio.Lock()
        self._store = Store(
            hass,
            PAIRING_CLIENT_STORAGE_VERSION,
            PAIRING_CLIENT_STORAGE_KEY,
        )

    async def async_initialize(self) -> None:
        data = await self._store.async_load() or {}
        self._clients = dict(data.get("clients", {}))

    async def _async_save_clients(self) -> None:
        await self._store.async_save({"clients": self._clients})

    def create_session(
        self,
        device_name: str,
        capabilities: list[str] | tuple[str, ...] | None = None,
    ) -> PairingSession:
        self.cleanup()
        session_id = secrets.token_urlsafe(32)
        code = self._generate_unique_code()
        now = datetime.now(UTC)
        normalized_device_name = " ".join(str(device_name).split())[:120] or "Apple TV"
        session = PairingSession(
            session_id=session_id,
            code=code,
            device_name=normalized_device_name,
            created_at=now,
            expires_at=now + timedelta(seconds=PAIRING_SESSION_LIFETIME_SECONDS),
            capabilities=tuple(
                sorted(
                    {
                        str(capability).strip()
                        for capability in (capabilities or ())
                        if str(capability).strip() in _ALLOWED_CLIENT_CAPABILITIES
                    }
                )
            ),
        )
        self._sessions[session_id] = session
        self._session_ids_by_code[code] = session_id
        return session

    def get_by_session_id(self, session_id: str) -> PairingSession | None:
        session = self._sessions.get(session_id)
        if session:
            session.refresh_status()
        return session

    def get_by_code(self, code: str) -> PairingSession | None:
        session_id = self._session_ids_by_code.get(self.normalize_code(code))
        return self.get_by_session_id(session_id) if session_id else None

    def approve(self, code: str) -> PairingSession | None:
        session = self.get_by_code(code)
        if session and session.refresh_status() == PairingStatus.WAITING:
            session.status = PairingStatus.APPROVED
            session.exchange_token = secrets.token_urlsafe(32)
        return session

    def cancel_by_code(self, code: str) -> PairingSession | None:
        session = self.get_by_code(code)
        if session and session.status in (PairingStatus.WAITING, PairingStatus.APPROVED):
            session.status = PairingStatus.CANCELLED
        return session

    def cancel(self, session_id: str) -> PairingSession | None:
        session = self.get_by_session_id(session_id)
        if session and session.status in (PairingStatus.WAITING, PairingStatus.APPROVED):
            session.status = PairingStatus.CANCELLED
        return session

    async def async_exchange(self, session_id: str, exchange_token: str) -> dict[str, str] | None:
        async with self._exchange_lock:
            session = self.get_by_session_id(session_id)
            if not session or session.refresh_status() != PairingStatus.APPROVED:
                return None
            if not session.exchange_token or not hmac.compare_digest(session.exchange_token, exchange_token):
                return None

            client_id = secrets.token_urlsafe(18)
            access_token = secrets.token_urlsafe(48)
            self._clients[client_id] = {
                "device_name": session.device_name,
                "token_hash": self._hash_token(access_token),
                "created_at": datetime.now(UTC).isoformat(),
                "last_seen": None,
                "capabilities": list(session.capabilities),
            }
            try:
                await self._async_save_clients()
            except Exception:
                self._clients.pop(client_id, None)
                raise
            session.status = PairingStatus.EXCHANGED
            session.exchange_token = None
            return {"client_id": client_id, "access_token": access_token}

    async def async_validate_client_token(self, token: str) -> str | None:
        token_hash = self._hash_token(token)
        for client_id, client in self._clients.items():
            if hmac.compare_digest(client.get("token_hash", ""), token_hash):
                client["last_seen"] = datetime.now(UTC).isoformat()
                await self._async_save_clients()
                return client_id
        return None

    def list_pending_sessions(self) -> list[PairingSession]:
        self.cleanup()
        return sorted(
            (
                session
                for session in self._sessions.values()
                if session.refresh_status() == PairingStatus.WAITING
            ),
            key=lambda session: session.created_at,
        )

    def list_clients(self) -> list[dict[str, Any]]:
        return [
            {"client_id": client_id, **{k: v for k, v in data.items() if k != "token_hash"}}
            for client_id, data in self._clients.items()
        ]

    def client_info(self, client_id: str) -> dict[str, Any] | None:
        """Return non-secret metadata for one paired client."""
        data = self._clients.get(client_id)
        if data is None:
            return None
        return {
            "client_id": client_id,
            **{key: value for key, value in data.items() if key != "token_hash"},
        }

    def client_has_capability(self, client_id: str, capability: str) -> bool:
        """Check an explicitly negotiated optional CouchMate capability."""
        data = self._clients.get(client_id, {})
        return capability in set(data.get("capabilities", []))

    async def async_rename_client(
        self,
        client_id: str,
        device_name: str,
    ) -> dict[str, Any] | None:
        """Persist a user-facing name without changing the client identity."""
        client = self._clients.get(client_id)
        if client is None:
            return None
        normalized_name = " ".join(str(device_name).split())[:120]
        if not normalized_name:
            raise ValueError("device_name_required")
        previous_name = client.get("device_name")
        client["device_name"] = normalized_name
        try:
            await self._async_save_clients()
        except Exception:
            if previous_name is None:
                client.pop("device_name", None)
            else:
                client["device_name"] = previous_name
            raise
        return self.client_info(client_id)

    async def async_revoke_client(self, client_id: str) -> bool:
        client = self._clients.pop(client_id, None)
        if client is not None:
            try:
                await self._async_save_clients()
            except Exception:
                self._clients[client_id] = client
                raise
        return client is not None

    def cleanup(self) -> None:
        expired = [sid for sid, session in self._sessions.items() if session.refresh_status() == PairingStatus.EXPIRED]
        for session_id in expired:
            session = self._sessions.pop(session_id)
            self._session_ids_by_code.pop(session.code, None)

    def _generate_unique_code(self) -> str:
        while True:
            first = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(4))
            second = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(4))
            code = f"CM-{first}-{second}"
            if code not in self._session_ids_by_code:
                return code

    @staticmethod
    def normalize_code(code: str) -> str:
        compact = "".join(ch for ch in code.upper() if ch.isalnum())
        if compact.startswith("CM"):
            compact = compact[2:]
        return f"CM-{compact[:4]}-{compact[4:8]}" if len(compact) >= 8 else code.upper().strip()

    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()
