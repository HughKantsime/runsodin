"""WebSocket audience enforcement and bounded event delivery."""
import asyncio
import logging
import threading
import time
from fastapi import WebSocket
from sqlalchemy import text
log = logging.getLogger("odin.api")

class ConnectionManager:
    """Manages authenticated WebSockets and scopes tenant/user events."""

    def __init__(self):
        self.active: list[tuple[WebSocket, dict]] = []
        self._policy_lock = threading.Lock()
        self._policy_checked = 0.0
        self._policy = None

    async def connect(self, ws: WebSocket, principal: dict):
        await ws.accept()
        self.active.append((ws, principal))

    def disconnect(self, ws: WebSocket):
        self.active = [(active_ws, principal) for active_ws, principal in self.active if active_ws is not ws]

    def policy_snapshot(self, *, cached=False):
        from core.license_lock import enabled, snapshot
        if not enabled():
            return None
        from core.db import SessionLocal
        with self._policy_lock:
            tick = time.monotonic()
            if cached and self._policy is not None and tick - self._policy_checked < 1:
                return self._policy
            with SessionLocal() as db:
                self._policy = snapshot(db)
            self._policy_checked = tick
            return self._policy

    def principal_allowed(self, principal, *, cached=False):
        from core.license_lock import user_allowed
        try:
            policy = self.policy_snapshot(cached=cached)
            return policy is None or user_allowed(policy, principal.get('id'))
        except Exception as exc:
            from core.license_lock import LicenseLockError
            if isinstance(exc, LicenseLockError):
                raise
            raise LicenseLockError('license_policy_unavailable', status=503) from None

    @staticmethod
    def _can_receive(principal: dict, user_ids: set[int] | None, org_id: int | None, shared: bool) -> bool:
        if user_ids is not None:
            return principal.get("id") in user_ids
        if shared or org_id is None:
            return True
        return (
            principal.get("role") == "admin" and principal.get("group_id") is None
        ) or principal.get("group_id") == org_id

    @staticmethod
    def _printer_scope(message: dict) -> tuple[int | None, bool]:
        printer_id = message.get("data", {}).get("printer_id")
        if printer_id is None:
            return None, False
        try:
            from core.db import SessionLocal

            with SessionLocal() as db:
                row = db.execute(
                    text("SELECT org_id, shared FROM printers WHERE id = :id"),
                    {"id": printer_id},
                ).fetchone()
            if not row:
                return -1, False
            return row[0], bool(row[1])
        except Exception:
            log.exception("Unable to resolve WebSocket printer audience")
            return -1, False

    @staticmethod
    def _education_principal_current(principal: dict, message: dict) -> bool:
        data = message.get("data", {})
        if not (message.get("education_internal") or data.get("education_internal")):
            return True
        try:
            from core.db import SessionLocal
            from modules.organizations.education_access import capability_snapshot_id

            with SessionLocal() as db:
                row = db.execute(
                    text(
                        "SELECT id, username, role, group_id, is_active FROM users "
                        "WHERE id=:id AND is_active IS TRUE"
                    ),
                    {"id": principal.get("id")},
                ).fetchone()
                if not row:
                    return False
                live_principal = dict(row._mapping)
                return capability_snapshot_id(db, live_principal) == principal.get(
                    "_capability_snapshot_id"
                )
        except Exception:
            log.exception("Unable to re-authorize Education WebSocket audience")
            return False

    async def broadcast(self, message: dict) -> int:
        """Deliver an event only to its explicit user or tenant audience."""
        audience = message.get("_audience", {})
        raw_user_ids = audience.get("user_ids")
        user_ids = set(raw_user_ids) if raw_user_ids is not None else None
        org_id = audience.get("org_id")
        shared = bool(audience.get("shared"))
        if user_ids is None and org_id is None and message.get("data", {}).get("printer_id") is not None:
            org_id, shared = await asyncio.to_thread(self._printer_scope, message)
        public_message = {key: value for key, value in message.items() if key != "_audience"}
        dead = []
        delivered = 0
        audience = list(self.active)
        from core.controlled_events import audience_messages
        messages = await asyncio.to_thread(audience_messages, [p for _, p in audience], public_message)
        from core.license_lock import user_allowed
        try:
            policy = await asyncio.to_thread(self.policy_snapshot)
        except Exception:
            # Existing sockets already passed admission. A transient policy DB
            # outage must not discard the event batch; retain the last decision.
            policy = self._policy
            log.warning('WebSocket license policy temporarily unavailable; using last snapshot')
        for (ws, principal), controlled_message in zip(audience, messages):
            if policy is not None and not user_allowed(policy, principal.get('id')):
                try:
                    await asyncio.wait_for(ws.close(code=4403, reason='license_user_locked'), timeout=1)
                except Exception:
                    pass
                finally:
                    dead.append(ws)
                continue
            if not self._can_receive(principal, user_ids, org_id, shared):
                continue
            if not await asyncio.to_thread(self._education_principal_current, principal, message):
                continue
            if controlled_message is None:
                continue
            try:
                await asyncio.wait_for(ws.send_json(controlled_message), timeout=1)
                delivered += 1
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)
        return delivered


ws_manager = ConnectionManager()


async def _ws_broadcaster(last_id=None):
    """Own independently scheduled TTL cleanup and bounded event delivery."""
    cleanup = asyncio.create_task(_ws_event_cleanup())
    try:
        await _ws_delivery(last_id)
    finally:
        cleanup.cancel()
        from contextlib import suppress
        with suppress(asyncio.CancelledError):
            await cleanup


async def _ws_event_cleanup():
    from core.ws_hub import cleanup_events, _CLEANUP_INTERVAL
    while True:
        await asyncio.to_thread(cleanup_events)
        await asyncio.sleep(_CLEANUP_INTERVAL)


async def _ws_delivery(last_id=None):
    """Drain bounded batches off-loop; expire events even without listeners."""
    from core.ws_hub import read_events_since, current_event_id

    if last_id is None:
        last_id = await asyncio.to_thread(current_event_id)
    while True:
        if not ws_manager.active:
            # Idle events are not a replay queue for future connections.
            last_id = await asyncio.to_thread(current_event_id)
            await asyncio.sleep(1)
            continue
        events, newest_id = await asyncio.to_thread(read_events_since, last_id)
        for evt in events:
            await ws_manager.broadcast(evt)
            await asyncio.sleep(0)
        advanced = newest_id != last_id
        last_id = newest_id
        await asyncio.sleep(0 if advanced else 1)
