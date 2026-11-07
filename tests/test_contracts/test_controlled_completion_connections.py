"""Native alert fan-out must keep policy reads on its caller's connection."""
import logging
import sqlite3
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, text

from tests.test_contracts.test_cert_A_harness import bind_engine, review_db  # noqa: F401


@pytest.fixture(params=['live_proxy', 'module_override'])
def notification_engine(request, review_db, monkeypatch):
    engine = review_db.get_bind()
    bind_engine(monkeypatch, engine)
    if request.param == 'live_proxy':
        yield engine
    else:
        # A distinct Engine for the same database lets the real policy queries
        # run, while proving they honour the module override, not core.db.engine.
        override = create_engine(engine.url)
        try:
            yield override
        finally:
            override.dispose()


def test_mobile_policy_reads_share_connection_with_pending_writer(review_db, notification_engine, monkeypatch, caplog):
    from modules.notifications import alert_dispatch, alert_outbox

    engine = notification_engine
    if engine is not review_db.get_bind():
        monkeypatch.setattr(alert_dispatch, 'engine', engine)
    # This harness deliberately uses rollback-journal SQLite, where an unfinished
    # user cursor blocks a writer's commit. No sleeps or scheduling luck needed.
    assert review_db.execute(text('PRAGMA journal_mode')).scalar() == 'delete'
    review_db.commit()
    deliver = alert_outbox.deliver_mobile
    connections = set()
    writer_blocked = []

    def deliver_with_pending_writer(db, plan):
        assert db.get_bind() is engine
        writer = sqlite3.connect(engine.url.database, timeout=0)

        def before(conn, cursor, statement, params, context, many):
            connections.add(id(conn.connection.driver_connection))
            # Fail immediately in this regression if a fresh reader is opened
            # behind the pending writer. Production timeouts are unchanged.
            cursor.execute('PRAGMA busy_timeout=0')

        def after(conn, cursor, statement, params, context, many):
            if statement == 'SELECT id,role,group_id FROM users WHERE is_active IS TRUE':
                writer.execute('UPDATE printers SET name=name WHERE id=9')
                try:
                    writer.commit()
                except sqlite3.OperationalError as exc:
                    assert 'locked' in str(exc)
                    writer_blocked.append(True)
                else:
                    raise AssertionError('user cursor did not hold the expected read lock')

        event.listen(engine, 'before_cursor_execute', before)
        event.listen(engine, 'after_cursor_execute', after)
        try:
            deliver(db, plan)
        finally:
            writer.rollback()
            writer.close()
            event.remove(engine, 'before_cursor_execute', before)
            event.remove(engine, 'after_cursor_execute', after)

    monkeypatch.setattr(alert_outbox, 'deliver_mobile', deliver_with_pending_writer)
    with caplog.at_level(logging.WARNING):
        alert_dispatch.dispatch_alert('print_complete', 'info', 'Print Complete: benchy', printer_id=9)
    assert writer_blocked == [True]
    assert len(connections) == 1, 'native delivery opened a connection per policy query'
    assert 'Mobile alert delivery failed' not in caplog.text
    assert 'Failed to dispatch alert' not in caplog.text


def test_mqtt_alert_scope_reads_share_connection(review_db, notification_engine, monkeypatch):
    from modules.notifications import mqtt_republish, quiet_hours

    engine = notification_engine
    if engine is not review_db.get_bind():
        monkeypatch.setattr(mqtt_republish, 'engine', engine)
    review_db.commit()
    monkeypatch.setattr(mqtt_republish, '_get_config', lambda: {'enabled': True})
    monkeypatch.setattr(quiet_hours, 'should_suppress_notification', lambda **kwargs: False)
    published = []
    monkeypatch.setattr(mqtt_republish, 'republish_alert', lambda **kwargs: published.append(kwargs))
    connections = set()
    statements = []

    def before(conn, cursor, statement, params, context, many):
        connections.add(id(conn.connection.driver_connection))
        statements.append(statement)

    event.listen(engine, 'before_cursor_execute', before)
    try:
        mqtt_republish._on_alert_dispatched(SimpleNamespace(data={
            'alert_type': 'print_complete', 'printer_id': 9, 'title': 'Print complete',
        }))
    finally:
        event.remove(engine, 'before_cursor_execute', before)
    assert len(published) == 1
    assert len(statements) > 2, 'exercise recipient enumeration and controlled policy reads'
    assert len(connections) == 1, 'MQTT alert scope opened a connection per policy query'
