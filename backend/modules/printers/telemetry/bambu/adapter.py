"""Bambu telemetry adapter (V2).

Replaces the 684-line `backend/modules/printers/adapters/bambu.py`
coupling of MQTT client + parsing + state machine + status storage.
This adapter does ONLY ingestion: it subscribes to the printer's MQTT
topic, validates bytes → `BambuReport`, and emits `TelemetryEvent`
objects through an injected emitter callable.

The state machine (`transition`) is driven by the adapter on each
event, producing an updated `PrinterStatus` that the adapter stores
and a list of `StateTransitionEvent`s that flow through the emitter
alongside the raw event.

Fail-loud semantics throughout:

- Bytes that fail JSON parse → `DegradedEvent` + WARN log, no crash.
- Valid JSON but invalid `BambuReport` → `DegradedEvent` + WARN log,
  observer.observe on extras.
- `BambuGcodeState` with unknown value → `DegradedEvent` (the Pydantic
  validator raised, caller caught it).
- `transition()` raises (OutOfOrder, UnhandledEvent, UnknownStage) →
  adapter logs ERROR and re-raises. These are hard bugs, not runtime
  conditions. Supervisor restarts the adapter.
- `on_message` never calls `except Exception: pass` — legacy did.
"""
from __future__ import annotations

import json
import logging
import ssl
import threading
from dataclasses import dataclass
from typing import Callable, Optional

import paho.mqtt.client as mqtt

from backend.modules.printers.telemetry.bambu.raw import BambuReport
from backend.modules.printers.telemetry.events import (
    BambuInfoEvent,
    BambuReportEvent,
    ConnectionEvent,
    DegradedEvent,
    TelemetryEvent,
)
from backend.modules.printers.telemetry.observability import observer
from backend.modules.printers.telemetry.state import (
    PrinterStatus,
    StateTransitionEvent,
)
from backend.modules.printers.telemetry.transition import transition

logger = logging.getLogger(__name__)

# Emitter callable type: receives either a TelemetryEvent or a
# StateTransitionEvent. The caller decides what to do (push to event
# bus, write to DB, forward to UI). Keeping this narrow-typed avoids
# the adapter needing to know about the rest of ODIN.
Emitter = Callable[[object], None]


@dataclass
class BambuAdapterConfig:
    """Connection config for one Bambu printer.

    `use_tls` defaults to True because Bambu MQTT on 8883 is the only
    production path. The embedded replayer broker (live_replay.py) is
    plain TCP on a random port — tests set `use_tls=False` to connect.
    """

    printer_id: str                # ODIN's internal ID (stable across sessions)
    serial: str                    # Bambu device serial — used in topic
    host: str                      # IP or hostname
    access_code: str               # Bambu "LAN-only" access code (MQTT password)
    port: int = 8883
    username: str = "bblp"
    use_tls: bool = True

    @property
    def topic_report(self) -> str:
        return f"device/{self.serial}/report"

    @property
    def topic_request(self) -> str:
        return f"device/{self.serial}/request"


class BambuTelemetryAdapter:
    """One-printer Bambu MQTT telemetry adapter.

    Lifecycle:
        adapter = BambuTelemetryAdapter(config, emitter)
        adapter.start()     # connects, subscribes, spawns network loop
        ...                 # emitter receives events
        adapter.stop()      # disconnects, joins loop

    Thread safety: `_status` is mutated from the paho-mqtt network loop
    thread. A lock guards reads and updates. Read the current status via
    `adapter.status()` (returns an immutable snapshot — the dataclass is
    frozen so the caller can't accidentally mutate it).
    """

    # Testing hook — production code should never set this. Test code
    # monkeypatches this to return a FakeMqttClient. Default uses real paho
    # with protocol=MQTTv311 (Bambu firmware + amqtt test broker are both
    # 3.1.1; paho's default V2-callback-API setting would negotiate MQTT5
    # which neither end supports).
    _client_factory: Callable[[], mqtt.Client] = staticmethod(
        lambda: mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            protocol=mqtt.MQTTv311,
        )
    )

    def __init__(self, config: BambuAdapterConfig, emitter: Emitter):
        self._config = config
        self._emitter = emitter
        self._status = PrinterStatus.initial()
        self._lock = threading.Lock()
        self._client: Optional[mqtt.Client] = None
        self._connected = False

    # ---- Public lifecycle ----

    def start(self) -> None:
        """Begin connecting to the broker. Non-blocking — the network loop
        runs in a paho-managed background thread."""
        if self._client is not None:
            raise RuntimeError("adapter already started")
        client = self._client_factory()
        # Only set credentials when access_code is non-empty — embedded
        # test broker is anonymous and rejects empty-password logins.
        if self._config.access_code:
            client.username_pw_set(self._config.username, self._config.access_code)

        if self._config.use_tls:
            # Bambu uses a self-signed cert on port 8883. Legacy adapter disabled
            # verification; V2 keeps the same behavior (documented in spec:
            # local LAN-only, verification doesn't add defense against a MITM
            # who's already inside the printer VLAN).
            tls = ssl.create_default_context()
            tls.check_hostname = False
            tls.verify_mode = ssl.CERT_NONE
            client.tls_set_context(tls)

        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message

        self._client = client
        # Use sync connect() to get immediate TCP setup + reliable
        # on_connect firing. The network loop runs async after loop_start().
        client.connect(self._config.host, self._config.port, keepalive=60)
        client.loop_start()

    def stop(self) -> None:
        """Disconnect and stop the network loop."""
        if self._client is None:
            return
        try:
            self._client.disconnect()
        finally:
            self._client.loop_stop()
            self._client = None
            self._connected = False

    def status(self) -> PrinterStatus:
        """Return a snapshot of the current PrinterStatus (frozen dataclass)."""
        with self._lock:
            return self._status

    # ---- Paho callbacks ----

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        """Called by paho on connection complete."""
        if reason_code == 0 or (hasattr(reason_code, "value") and reason_code.value == 0):
            self._connected = True
            client.subscribe(self._config.topic_report, qos=0)
            logger.info("bambu adapter connected: printer=%s", self._config.printer_id)
            self._process_event(ConnectionEvent(
                printer_id=self._config.printer_id,
                ts=_now_ts(),
                kind="connected",
            ))
        else:
            logger.warning(
                "bambu adapter connect failed: printer=%s rc=%s",
                self._config.printer_id, reason_code,
            )
            self._process_event(ConnectionEvent(
                printer_id=self._config.printer_id,
                ts=_now_ts(),
                kind="error",
                detail=f"connect reason_code={reason_code}",
            ))

    def _on_disconnect(self, client, userdata, *args, **kwargs):
        """Called by paho on disconnection (planned or dropped)."""
        self._connected = False
        logger.info("bambu adapter disconnected: printer=%s", self._config.printer_id)
        self._process_event(ConnectionEvent(
            printer_id=self._config.printer_id,
            ts=_now_ts(),
            kind="disconnected",
        ))

    def _on_message(self, client, userdata, msg) -> None:
        """The hot path — every Bambu status report comes through here.

        Fail-loud branches:
        - Bad bytes (not JSON) → DegradedEvent with raw excerpt.
        - Bad shape (no print/info) → skip (InvalidBambuReport caught).
        - Validation error → DegradedEvent with exception message.

        The legacy adapter swallowed both JSONDecodeError AND the generic
        Exception path. V2 surfaces both via DegradedEvent so the state
        machine marks the printer DEGRADED and the observer logs.
        """
        payload_bytes = msg.payload
        ts = _now_ts()
        try:
            report = BambuReport.model_validate_json(payload_bytes)
        except Exception as exc:
            excerpt = payload_bytes[:200].decode("utf-8", errors="replace")
            logger.warning(
                "bambu adapter model validation failed: printer=%s err=%s excerpt=%r",
                self._config.printer_id, exc, excerpt,
            )
            self._process_event(DegradedEvent(
                printer_id=self._config.printer_id,
                ts=ts,
                reason=str(exc)[:200],
                raw_excerpt=excerpt,
            ))
            return

        # Surface any unknown-field extras to the observer. Pydantic stores
        # them on `model_extra`. Walks through the full report.
        if report.model_extra:
            observer.observe("bambu", report.model_extra)
        if report.print is not None and report.print.model_extra:
            observer.observe("bambu", report.print.model_extra)

        if report.print is not None:
            self._process_event(BambuReportEvent(
                printer_id=self._config.printer_id,
                ts=ts,
                section=report.print,
            ))
        elif report.info is not None:
            self._process_event(BambuInfoEvent(
                printer_id=self._config.printer_id,
                ts=ts,
                section=report.info,
            ))

    # ---- State machine integration ----

    def _process_event(self, event: TelemetryEvent) -> None:
        """Feed an event through transition() and emit everything.

        Guards:
        - If transition raises (OutOfOrder, UnhandledEvent, UnknownStage),
          log ERROR and re-raise. These are bugs, not runtime conditions.
        - All emitter calls are OUTSIDE the lock to avoid deadlocks if
          the emitter itself calls back into the adapter.
        """
        with self._lock:
            try:
                new_status, transitions = transition(self._status, event)
            except Exception:
                logger.exception(
                    "transition() failed: printer=%s event=%s",
                    self._config.printer_id, type(event).__name__,
                )
                raise
            self._status = new_status
            to_emit = [event, *transitions]

        for item in to_emit:
            try:
                self._emitter(item)
            except Exception:
                # Emitter failures must NOT crash the adapter — they're
                # downstream bugs. Log + continue.
                logger.exception(
                    "emitter raised; continuing: printer=%s item=%s",
                    self._config.printer_id, type(item).__name__,
                )


def _now_ts() -> float:
    """Adapter uses wall-clock only for *message arrival times* — this is
    the one place where non-pure behavior is legitimate: we are producing
    a timestamp to be embedded in a TelemetryEvent, not deriving state
    from the clock. The state machine itself remains pure."""
    import time
    return time.time()
