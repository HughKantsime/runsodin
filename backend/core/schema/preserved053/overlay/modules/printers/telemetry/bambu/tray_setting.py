"""Bounded tray command/reply session. A broker ACK is not printer acceptance."""
import json
import threading
import time

from modules.printers.telemetry.bambu.commands import BambuCommandAdapter


class TraySettingAdapter(BambuCommandAdapter):
    def __init__(self, config):
        super().__init__(config)
        self.subscribed = threading.Event()
        self.replied = threading.Event()
        self.target = None
        self.reply = None

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        super()._on_connect(client, userdata, flags, reason_code, properties)
        if self.is_connected():
            client.on_message = self._on_message
            client.on_subscribe = self._on_subscribe
            client.subscribe(self._config.topic_report)

    def _on_subscribe(self, client, userdata, mid, reason_codes, properties=None):
        if reason_codes and all(getattr(code, 'value', code) < 128 for code in reason_codes):
            self.subscribed.set()

    def _on_message(self, client, userdata, message):
        try:
            packet = json.loads(message.payload).get('print', {})
            if (self.target is None or packet.get('command') != 'ams_filament_setting'
                    or getattr(message, 'retain', False)
                    or (int(packet.get('ams_id', -1)), int(packet.get('tray_id', -1))) != self.target):
                return
            if isinstance(packet.get('result'), (str, int)):
                self.reply = {key: packet.get(key) for key in ('result', 'reason')}
                self.replied.set()
        except (ValueError, TypeError, AttributeError):
            return


def set_tray(config, ams_id, slot_id, preset_id, material, color, nozzle_temp_min, nozzle_temp_max, *, timeout=15):
    adapter = TraySettingAdapter(config)
    deadline = time.monotonic() + timeout
    try:
        adapter.start()
        if not adapter.subscribed.wait(max(0, deadline - time.monotonic())):
            return {'result': None, 'reason': 'subscription_timeout'}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {'result': None, 'reason': 'subscription_timeout'}
        adapter.PUBLISH_TIMEOUT_SEC = min(adapter.PUBLISH_TIMEOUT_SEC, remaining)
        adapter.target = (ams_id, slot_id)
        if not adapter.set_ams_filament(ams_id, slot_id, preset_id, material, color, nozzle_temp_min, nozzle_temp_max):
            return {'result': None, 'reason': 'publish_failed'}
        if not adapter.replied.wait(max(0, deadline - time.monotonic())):
            return {'result': None, 'reason': 'reply_timeout'}
        return adapter.reply
    except Exception:
        return {'result': None, 'reason': 'transport_failed'}
    finally:
        try:
            adapter.stop()
        except Exception:
            # Cleanup failure cannot turn an unconfirmed command into success.
            pass
