"""Alert identity is policy context, never part of the external MQTT wire."""
import json
from types import SimpleNamespace

import pytest

from tests.test_contracts.test_controlled_parts import database, actor, mark


@pytest.mark.parametrize('marked', [False, True])
@pytest.mark.parametrize('via_bus', [False, True])
def test_alert_job_identity_is_out_of_band(database, tier_license, monkeypatch, marked, via_bus):
    from core import controlled_mqtt
    from core.event_bus import InMemoryEventBus
    from core.interfaces.event_bus import Event
    from modules.notifications import mqtt_republish, quiet_hours
    from modules.organizations.routes_controlled_parts import change_mark

    tier_license('enterprise')
    if marked:
        change_mark(database, 'jobs', 1, mark(), actor())
    controlled_mqtt.invalidate()
    sent = []
    monkeypatch.setattr(mqtt_republish, 'engine', database.get_bind())
    monkeypatch.setattr(mqtt_republish, '_get_config', lambda: {'topic_prefix': 'fixture'})
    monkeypatch.setattr(mqtt_republish, '_get_client', lambda: SimpleNamespace(
        publish=lambda topic, payload, **flags: sent.append((topic, json.loads(payload), flags))))
    monkeypatch.setattr(quiet_hours, 'should_suppress_notification', lambda **kwargs: False)
    data = dict(alert_type='queue_added', severity='info', title='Private part',
                message='Private details', job_id=1)
    if via_bus:
        bus = InMemoryEventBus()
        mqtt_republish.register_subscribers(bus)
        bus.publish(Event(event_type='notifications.alert_dispatched', source_module='fixture', data=data))
    else:
        mqtt_republish.republish_alert(**data)
    assert len(sent) == 1
    topic, payload, flags = sent[0]
    assert topic == 'fixture/alerts' and flags == {'qos': 0, 'retain': False}
    assert 'job_id' not in payload
    assert payload['title'] == ('Controlled work' if marked else 'Private part')
    assert payload['message'] == ('Controlled work status changed' if marked else 'Private details')
    assert data['job_id'] == 1  # In-process notification consumers keep their identity.
