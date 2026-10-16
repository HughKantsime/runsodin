"""Cert-E F13-13.2-03 / F10-10.9-07: quiet-hours deferred deliveries (migration 032).

Migration 032 is registered through the post-027 mechanism (docs/SCHEMA_MIGRATIONS.md); these
tests also re-apply it to the bootstrapped test database (idempotent) and then exercise the real
production chain: org quiet hours configured over HTTP, both dispatchers, the real digest
driver, a real SMTP protocol peer and a real Web Push receiver.
"""
import importlib
import json
from datetime import timedelta

import http_ece
import pytest
from sqlalchemy import inspect, text

from tests.test_contracts.test_cert_E_notif_quiet import (  # noqa: F401
    admin_api, browser, configure_smtp, dispatch_printer, move_quiet_clock, notif_env, org_api, prefs, push_api,
    put_org, receiver, review_db, run_digest, signed_license, smtp_peer, subscribe, synthetic_dns, vapid, window)

MIGRATION = importlib.import_module("core.schema.migrations.032_quiet_hours_deferred_deliveries")


@pytest.fixture
def deferral_table(notif_env):
    engine = notif_env.get_bind()
    with engine.connect() as conn:
        MIGRATION.apply(conn)
        conn.commit()
    with engine.connect() as conn:
        MIGRATION.validate(conn)
    return engine


def _rows(engine, channels=("email", "push")):
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(
            "SELECT user_id,org_id,scope_key,channel,alert_type,state,outcome_reason FROM quiet_hours_deferred_deliveries "
            "ORDER BY user_id,channel")).mappings() if r["channel"] in channels]


def test_migration_032_is_additive_and_idempotent(notif_env):
    engine = notif_env.get_bind()
    with engine.connect() as conn:
        before = set(inspect(conn).get_table_names())
        MIGRATION.apply(conn); conn.commit()
        MIGRATION.apply(conn); conn.commit()  # second apply: no error, no change
        MIGRATION.validate(conn)
        assert set(inspect(conn).get_table_names()) - before == {"quiet_hours_deferred_deliveries"}
    with pytest.raises(Exception):
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO quiet_hours_deferred_deliveries(id,user_id,scope_key,channel,alert_type,severity,title) "
                              "VALUES('x',1,'system','sms','print_failed','info','t')"))


@pytest.mark.parametrize("path", ["orm", "daemon"])
def test_email_only_user_receives_digest_of_suppressed_alert(org_api, admin_api, notif_env, deferral_table, smtp_peer,
                                                             monkeypatch, path):
    base, start, end = window()
    put_org(org_api, 1, quiet_hours_enabled=True, quiet_hours_start=start, quiet_hours_end=end)
    configure_smtp(admin_api, smtp_peer)
    prefs(notif_env, 1, "print_failed", in_app=False, email=True)
    dispatch_printer(path, 9, notif_env)
    assert smtp_peer.messages == []  # suppressed in real time
    with notif_env.get_bind().connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM alerts WHERE user_id=1")).scalar_one() == 0  # no in-app row
    assert _rows(deferral_table) == [{"user_id": 1, "org_id": 1, "scope_key": "org:1", "channel": "email",
                                      "alert_type": "print_failed", "state": "pending", "outcome_reason": None}]
    move_quiet_clock(monkeypatch, base + timedelta(minutes=35))
    run_digest(notif_env)
    message, = smtp_peer.wait(1)
    assert message["To"] == "student@example.test"
    html = message.get_body(preferencelist=("html",)).get_content()
    assert "Synthetic failure on 9" in html
    assert _rows(deferral_table)[0]["state"] == "sent"
    run_digest(notif_env)  # same window: idempotent
    assert len(smtp_peer.messages) == 1


def test_push_only_user_and_in_app_user_are_not_double_delivered(org_api, admin_api, push_api, notif_env, deferral_table,
                                                                 receiver, smtp_peer, vapid, browser, monkeypatch):
    """Push-only user gets the deferred push; a user with an in-app row is digested from alerts only (no row here)."""
    base, start, end = window()
    receiver.status = 201
    put_org(org_api, 1, quiet_hours_enabled=True, quiet_hours_start=start, quiet_hours_end=end)
    configure_smtp(admin_api, smtp_peer)
    subscribe(push_api, receiver, browser)
    prefs(notif_env, 1, "print_failed", in_app=False, email=False, push=True)
    prefs(notif_env, 3, "print_failed", in_app=True, email=True)
    dispatch_printer("daemon", 9, notif_env)
    assert [(r["user_id"], r["channel"]) for r in _rows(deferral_table)] == [(1, "push")]
    move_quiet_clock(monkeypatch, base + timedelta(minutes=35))
    run_digest(notif_env)
    request, = receiver.wait(1)
    payload = json.loads(http_ece.decrypt(request["body"], private_key=browser["private"],
                                          auth_secret=browser["auth_raw"], version="aes128gcm"))
    assert payload["title"] == "1 alert during quiet hours"
    assert _rows(deferral_table)[0]["state"] == "sent"


def test_sibling_org_rows_stay_in_their_own_scope(org_api, admin_api, notif_env, deferral_table, smtp_peer, monkeypatch):
    """Org-scoped: an org-2 row is not digested by org 1's window."""
    base, start, end = window()
    put_org(org_api, 1, quiet_hours_enabled=True, quiet_hours_start=start, quiet_hours_end=end)
    configure_smtp(admin_api, smtp_peer)
    with notif_env.get_bind().begin() as conn:
        conn.execute(text("INSERT INTO quiet_hours_deferred_deliveries(id,user_id,org_id,scope_key,channel,alert_type,severity,title) "
                          "VALUES('org2row',1,2,'org:2','email','print_failed','critical','Other org alert')"))
    move_quiet_clock(monkeypatch, base + timedelta(minutes=35))
    run_digest(notif_env)
    assert smtp_peer.messages == []
    assert [r["state"] for r in _rows(deferral_table)] == ["pending"]


def test_undeliverable_deferred_row_is_marked_failed_with_reason(org_api, notif_env, deferral_table, monkeypatch):
    """No SMTP configured: the row is recorded failed (visible), never silently kept or resent."""
    base, start, end = window()
    put_org(org_api, 1, quiet_hours_enabled=True, quiet_hours_start=start, quiet_hours_end=end)
    prefs(notif_env, 1, "print_failed", in_app=False, email=True)
    dispatch_printer("orm", 9, notif_env)
    move_quiet_clock(monkeypatch, base + timedelta(minutes=35))
    run_digest(notif_env)
    row, = _rows(deferral_table)
    assert (row["state"], row["outcome_reason"]) == ("failed", "email:not_delivered")
    with notif_env.get_bind().connect() as conn:
        assert conn.execute(text("SELECT delivery_status FROM quiet_hours_digest_sends WHERE user_id=1")).scalar_one() \
            == "failed:email:unconfigured"


def test_retention_purges_sent_after_7_days_and_unsent_after_30(notif_env, deferral_table):
    from datetime import datetime, timezone
    from modules.notifications import deferred_deliveries
    now = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
    rows = [("s-old", "sent", "2026-10-07 11:00:00"), ("s-new", "sent", "2026-10-09 13:00:00"),
            ("f-old", "failed", "2026-09-14 11:00:00"), ("p-new", "pending", "2026-09-16 13:00:00")]
    with deferral_table.begin() as conn:
        for row_id, state, created in rows:
            conn.execute(text("INSERT INTO quiet_hours_deferred_deliveries(id,user_id,scope_key,channel,alert_type,severity,"
                              "title,state,created_at) VALUES(:i,1,'system','email','print_failed','info','t',:s,:c)"),
                         {"i": row_id, "s": state, "c": created})
    with deferral_table.begin() as conn:
        assert deferred_deliveries.purge(conn, now=now) == 2
    with deferral_table.connect() as conn:
        assert sorted(conn.execute(text("SELECT id FROM quiet_hours_deferred_deliveries")).scalars()) == ["p-new", "s-new"]


def test_without_table_recording_is_a_noop(notif_env):
    """Pre-registration databases keep the previous behaviour (no error, nothing recorded)."""
    from modules.notifications import deferred_deliveries
    with notif_env.get_bind().begin() as conn:
        assert deferred_deliveries.available(conn) is False
        assert deferred_deliveries.record_suppressed(conn, org_id=1, scope="org:1", recipients={1: {"email"}},
                                                     alert_type="print_failed", severity="critical", title="t") == 0
        assert deferred_deliveries.pending_for_window(conn, "org:1", None, None) == []


def system_quiet_hours(db, start, end):
    from modules.notifications import quiet_hours
    for key, value in [('quiet_hours_enabled', 'true'), ('quiet_hours_start', start), ('quiet_hours_end', end)]:
        db.execute(text('DELETE FROM system_config WHERE key=:k'), {'k': key})
        db.execute(text('INSERT INTO system_config(key,value) VALUES(:k,:v)'), {'k': key, 'v': value})
    db.commit()
    quiet_hours.invalidate_cache()


@pytest.mark.parametrize("path", ["orm", "daemon"])
def test_org_alert_suppressed_by_system_quiet_hours_is_digested_by_the_system_digest(
        admin_api, notif_env, deferral_table, smtp_peer, monkeypatch, path):
    """Review M1: org 1 has no quiet hours of its own, so system quiet hours suppress its printer's
    alert. The deferral records scope 'system' and the system digest delivers it exactly once;
    the in-app recipient's digest (inherited gap) is delivered by the same system digest."""
    from modules.notifications import quiet_hours
    base, start, end = window()
    system_quiet_hours(notif_env, start, end)
    configure_smtp(admin_api, smtp_peer)
    prefs(notif_env, 1, "print_failed", in_app=False, email=True)   # email-only -> deferred row
    prefs(notif_env, 3, "print_failed", in_app=True, email=True)    # in-app row -> alerts table
    dispatch_printer(path, 9, notif_env)
    assert smtp_peer.messages == []
    rows = _rows(deferral_table)
    assert [(r["user_id"], r["org_id"], r["scope_key"]) for r in rows] == [(1, 1, "system")]
    move_quiet_clock(monkeypatch, base + timedelta(minutes=35))
    quiet_hours.invalidate_cache()
    run_digest(notif_env)
    messages = smtp_peer.wait(2)
    assert sorted(m["To"] for m in messages) == ["admin@example.test", "student@example.test"]
    assert _rows(deferral_table)[0]["state"] == "sent"
    run_digest(notif_env)
    assert len(smtp_peer.messages) == 2


def test_webhook_digest_lists_each_alert_once_including_alerts_without_in_app_rows(
        org_api, notif_env, deferral_table, receiver, synthetic_dns, monkeypatch):
    """Review M2: two in-app recipients of one alert, plus a second alert nobody receives in-app:
    the org webhook digest carries each alert exactly once."""
    base, start, end = window()
    receiver.status = 204
    put_org(org_api, 1, quiet_hours_enabled=True, quiet_hours_start=start, quiet_hours_end=end,
            webhook_url=f"{synthetic_dns}/digest", webhook_type="generic")
    prefs(notif_env, 1, "print_failed", in_app=True)
    prefs(notif_env, 3, "print_failed", in_app=True)
    prefs(notif_env, 1, "print_complete", in_app=False)
    from core.base import AlertSeverity, AlertType
    from modules.notifications import alert_dispatch
    alert_dispatch.dispatch_alert("print_failed", "critical", "Shared failure", "x", printer_id=9)
    alert_dispatch.dispatch_alert("print_complete", "info", "Nobody in-app", "x", printer_id=9)
    with notif_env.get_bind().connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM alerts WHERE title='Nobody in-app'")).scalar_one() == 0
    move_quiet_clock(monkeypatch, base + timedelta(minutes=35))
    run_digest(notif_env)
    request, = receiver.wait(1)
    body = json.loads(request["body"])
    assert body["alert_count"] == 2
    assert sorted(a["title"] for a in body["alerts"]) == ["Nobody in-app", "Shared failure"]
    assert {r["state"] for r in _rows(deferral_table, channels=("webhook",))} == {"sent"}
    run_digest(notif_env)
    assert len(receiver.requests) == 1
