"""Owned MQTT plug command transport, without database or shared publisher."""
import time
import threading
from core.mqtt_connection_snapshot import MqttConnectionSnapshot


def valid_power_topic(topic):
    return (isinstance(topic, str) and bool(topic) and len(topic) <= 512
            and topic == topic.strip()
            and not any(ord(char) < 32 or ord(char) == 127 or char in '+#' for char in topic))


def send_owned_mqtt_on(snapshot, topic, deadline):
    """One explicit ON publication; acknowledgment is not a power observation.

    No reconnect or application retry. MQTT QoS1 is at-least-once delivery,
    not proof that a physical device executes a command exactly once.
    """
    if not isinstance(snapshot, MqttConnectionSnapshot) or not valid_power_topic(topic):
        return 'refused'
    client = None
    attempted = False
    outcome = 'refused'
    connected = threading.Event()
    failed = threading.Event()
    try:
        import paho.mqtt.client as mqtt
        from core.itar import enforce_host_destination
        enforce_host_destination(snapshot.host, scheme='mqtt')
        if time.monotonic() >= deadline:
            return 'refused'
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv311,
                             reconnect_on_failure=False)
        def on_connect(client, userdata, flags, reason_code, properties=None):
            code = getattr(reason_code, 'value', reason_code)
            if type(code) is not int or code != 0:
                failed.set()
            connected.set()
        def on_disconnect(client, userdata, flags, reason_code, properties=None):
            failed.set()
            connected.set()
        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        if snapshot.username:
            client.username_pw_set(snapshot.username, snapshot.password)
        if snapshot.use_tls:
            client.tls_set()
        if client.connect(snapshot.host, snapshot.port, keepalive=60) != mqtt.MQTT_ERR_SUCCESS:
            return 'refused'
        if client.loop_start() != mqtt.MQTT_ERR_SUCCESS:
            return 'refused'
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not connected.wait(remaining) or failed.is_set():
            return 'refused'
        if time.monotonic() >= deadline:
            return 'refused'
        attempted = True
        outcome = 'uncertain'
        receipt = client.publish(topic, 'ON', qos=1, retain=False)
        if not isinstance(receipt.rc, int) or isinstance(receipt.rc, bool) or receipt.rc != 0:
            return 'uncertain'
        remaining = deadline - time.monotonic()
        if remaining > 0:
            receipt.wait_for_publish(timeout=remaining)
            if receipt.is_published() is True and time.monotonic() < deadline:
                outcome = 'accepted'
    except Exception:
        outcome = 'uncertain' if attempted else 'refused'
    finally:
        if client is not None:
            for operation in ('disconnect', 'loop_stop'):
                try:
                    getattr(client, operation)()
                except Exception:
                    outcome = 'uncertain' if attempted else 'refused'
    return outcome
