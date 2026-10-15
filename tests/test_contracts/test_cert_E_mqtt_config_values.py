"""Review L4: the MQTT power-connection snapshot reads system_config values like every other reader."""
import json

from sqlalchemy import text

from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401


def test_power_snapshot_accepts_json_encoded_and_native_scalar_values(review_db, monkeypatch):  # noqa: F811
    from modules.notifications import mqtt_republish
    monkeypatch.setattr(mqtt_republish, 'engine', review_db.get_bind())
    for key, value in [('mqtt_republish_enabled', json.dumps('true')), ('mqtt_republish_host', json.dumps('broker.example.test')),
                       ('mqtt_republish_port', '8883'), ('mqtt_republish_use_tls', 'true')]:
        review_db.execute(text('INSERT INTO system_config(key,value) VALUES(:k,:v)'), {'k': key, 'v': value})
    review_db.commit()
    snapshot = mqtt_republish.get_power_connection_snapshot()
    assert snapshot is not None
    assert (snapshot.host, snapshot.port, snapshot.use_tls) == ('broker.example.test', 8883, True)
