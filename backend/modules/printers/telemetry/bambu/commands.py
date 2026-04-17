"""Bambu command-plane adapter — publishes to `device/<serial>/request`.

Byte-equivalent to legacy `adapters/bambu.py` command methods. Every
method here builds the exact same payload dict legacy builds, publishes
it to the same topic, and returns the same success signal.

This adapter is **separate** from the telemetry adapter:
- Telemetry (read) → subscribe to `device/<serial>/report`, parse, emit events.
- Commands (write) → publish to `device/<serial>/request`, return ACK bool.

Separating them keeps each half simple. A printer uses both adapters,
each over its own paho client (Bambu's MQTT broker supports multiple
connections from the same credentials).

File-upload commands (FTPS-based `upload_file`) are NOT in scope of
this MQTT adapter — they use port 990 + FTP protocol. A separate
`BambuFileTransfer` helper lives elsewhere; this class is MQTT-only.
"""
from __future__ import annotations

import json
import logging
import ssl
import threading
import time
from typing import Any, Callable, Optional

import paho.mqtt.client as mqtt

from backend.modules.printers.telemetry.bambu.adapter import BambuAdapterConfig

logger = logging.getLogger(__name__)


class BambuCommandAdapter:
    """Sends control commands to a Bambu printer over MQTT.

    Lifecycle:
        cmd = BambuCommandAdapter(config)
        cmd.start()
        cmd.send_gcode("M105")
        cmd.pause_print()
        cmd.stop()
    """

    # Testing hook — tests inject a FakeMqttClient via monkeypatch.
    _client_factory: Callable[[], mqtt.Client] = staticmethod(
        lambda: mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            protocol=mqtt.MQTTv311,
        )
    )

    PUBLISH_TIMEOUT_SEC = 5.0

    def __init__(self, config: BambuAdapterConfig):
        self._config = config
        self._client: Optional[mqtt.Client] = None
        self._connected = False
        self._lock = threading.Lock()

    # ---- Lifecycle ----

    def start(self) -> None:
        if self._client is not None:
            raise RuntimeError("command adapter already started")
        client = self._client_factory()
        if self._config.access_code:
            client.username_pw_set(self._config.username, self._config.access_code)
        if self._config.use_tls:
            tls = ssl.create_default_context()
            tls.check_hostname = False
            tls.verify_mode = ssl.CERT_NONE
            client.tls_set_context(tls)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.connect(self._config.host, self._config.port, keepalive=60)
        client.loop_start()
        self._client = client

    def stop(self) -> None:
        if self._client is None:
            return
        try:
            self._client.disconnect()
        finally:
            self._client.loop_stop()
            self._client = None
            self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code == 0 or (hasattr(reason_code, "value") and reason_code.value == 0):
            self._connected = True
            logger.info("bambu command adapter connected: printer=%s", self._config.printer_id)
        else:
            logger.warning(
                "bambu command adapter connect failed: printer=%s rc=%s",
                self._config.printer_id, reason_code,
            )

    def _on_disconnect(self, client, userdata, *args, **kwargs):
        self._connected = False

    # ---- Publish core ----

    def _publish(self, payload: dict[str, Any]) -> bool:
        """Publish a command payload to `device/<serial>/request` and wait
        for broker ACK. Byte-identical in semantics to legacy's `_publish`.
        """
        if self._client is None or not self._connected:
            return False
        try:
            result = self._client.publish(
                self._config.topic_request,
                json.dumps(payload),
            )
            if result.rc != mqtt.MQTT_ERR_SUCCESS:
                return False
            result.wait_for_publish(timeout=self.PUBLISH_TIMEOUT_SEC)
            return result.is_published()
        except Exception:
            logger.exception("bambu command publish error: printer=%s", self._config.printer_id)
            return False

    # ---- Command surface — byte-equivalent to legacy ----

    def request_full_status(self) -> bool:
        """Request `pushall` state dump. Matches legacy payload."""
        return self._publish({
            "pushing": {"sequence_id": "0", "command": "pushall"}
        })

    def pause_print(self) -> bool:
        return self._publish({
            "print": {"sequence_id": "0", "command": "pause"}
        })

    def resume_print(self) -> bool:
        return self._publish({
            "print": {"sequence_id": "0", "command": "resume"}
        })

    def stop_print(self) -> bool:
        return self._publish({
            "print": {"sequence_id": "0", "command": "stop"}
        })

    def send_gcode(self, gcode: str) -> bool:
        """Send raw G-code. Matches legacy `_send_gcode` / `send_gcode`."""
        return self._publish({
            "print": {
                "sequence_id": "0",
                "command": "gcode_line",
                "param": gcode,
            }
        })

    def set_bed_temp(self, temp: int) -> bool:
        return self.send_gcode(f"M140 S{temp}")

    def set_nozzle_temp(self, temp: int) -> bool:
        return self.send_gcode(f"M104 S{temp}")

    def turn_light_on(self) -> bool:
        return self.send_gcode("M355 S1")

    def turn_light_off(self) -> bool:
        return self.send_gcode("M355 S0")

    def set_fan_speed(self, fan: str, speed: int) -> bool:
        """Set fan via M106. `fan` in {"part_cooling", "auxiliary", "chamber"}."""
        fan_map = {"part_cooling": 1, "auxiliary": 2, "chamber": 3}
        p = fan_map.get(fan)
        if p is None:
            return False
        speed = max(0, min(255, speed))
        return self.send_gcode(f"M106 P{p} S{speed}")

    def refresh_ams_rfid(self) -> bool:
        """Trigger AMS RFID re-read for all trays — target=255 (no change)."""
        return self._publish({
            "print": {
                "sequence_id": "0",
                "command": "ams_change_filament",
                "target": 255,
                "curr_temp": 0,
                "tar_temp": 0,
            }
        })

    def set_ams_filament(
        self,
        ams_id: int,
        slot_id: int,
        material: str,
        color: str,
        k_factor: float = 0.0,
    ) -> bool:
        """Configure an AMS slot. `color` is RRGGBB or RRGGBBAA (# optional)."""
        color = color.lstrip("#")
        if len(color) == 6:
            color = color + "FF"
        tray_id = ams_id * 4 + slot_id
        return self._publish({
            "print": {
                "sequence_id": "0",
                "command": "ams_filament_setting",
                "ams_id": ams_id,
                "tray_id": tray_id,
                "tray_info_idx": material,
                "tray_color": color.upper(),
                "nozzle_temp_min": 190,
                "nozzle_temp_max": 240,
                "tray_type": material,
                "setting_id": "",
                "k": k_factor,
            }
        })

    def clear_print_errors(self) -> bool:
        return self._publish({
            "print": {"sequence_id": "0", "command": "clean_print_error"}
        })

    def skip_objects(self, object_ids: list[int]) -> bool:
        return self._publish({
            "print": {
                "sequence_id": "0",
                "command": "skip_objects",
                "obj_list": [str(oid) for oid in object_ids],
            }
        })

    def set_print_speed(self, speed_level: int) -> bool:
        """1=Silent, 2=Standard, 3=Sport, 4=Ludicrous."""
        if speed_level not in (1, 2, 3, 4):
            return False
        return self._publish({
            "print": {
                "sequence_id": "0",
                "command": "print_speed",
                "param": str(speed_level),
            }
        })

    def start_print(
        self,
        remote_filename: str,
        plate_num: int = 1,
        use_ams: bool = True,
        bed_leveling: bool = True,
        timelapse: bool = False,
    ) -> bool:
        """Start a print of an already-uploaded .3mf file.

        `sequence_id` is dynamic (legacy uses `str(int(time.time()) % 100000)`).
        Matching that exactly keeps the payload byte-equivalent when tested
        at a fixed wall-clock moment.
        """
        subtask_name = remote_filename.replace(".3mf", "")
        return self._publish({
            "print": {
                "sequence_id": str(int(time.time()) % 100000),
                "command": "project_file",
                "param": f"Metadata/plate_{plate_num}.gcode",
                "subtask_name": subtask_name,
                "url": f"ftp:///{remote_filename}",
                "bed_type": "auto",
                "timelapse": timelapse,
                "bed_leveling": bed_leveling,
                "flow_cali": False,
                "vibration_cali": True,
                "layer_inspect": False,
                "use_ams": use_ams,
                "profile_id": "0",
                "project_id": "0",
                "subtask_id": "0",
                "task_id": "0",
            }
        })
