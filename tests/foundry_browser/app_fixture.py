"""Disposable ODIN using real schema, auth and routes; no daemons or hardware.
Usage: python app_fixture.py TEMP_DIR PORT [--setup]
"""
import json
import os
from pathlib import Path
from datetime import datetime, timezone
import secrets
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'backend')]


def main():
    directory = Path(sys.argv[1]).resolve()
    port = int(sys.argv[2])
    directory.mkdir(parents=True, exist_ok=True)
    assert not (directory / 'review.db').exists(), 'Fixture requires a fresh disposable directory'
    from cryptography.fernet import Fernet
    os.environ.update(ENCRYPTION_KEY=Fernet.generate_key().decode(), JWT_SECRET_KEY=secrets.token_urlsafe(48),
                      DATABASE_URL=f"sqlite:///{directory / 'review.db'}", DATABASE_PATH=str(directory / 'review.db'),
                      API_KEY='', AUTO_DISPATCH='false', COOKIE_SECURE='false',
                      LICENSE_SERVER_URL='http://127.0.0.1:1', LICENSE_DIR=str(directory / 'license'),
                      CORS_ORIGINS=f'http://127.0.0.1:{port}', TRUSTED_HOSTS='127.0.0.1,localhost',
                      EDUCATION_UPLOAD_ROOT=str(directory / 'uploads'),
                      GO2RTC_CONFIG=str(directory / 'go2rtc.yaml'), GO2RTC_LOCKFILE=str(directory / 'go2rtc.lock'),
                      ODIN_GO2RTC_API_URL='http://127.0.0.1:1')
    from sqlalchemy import create_engine, event, text
    from core.schema import bootstrap_database
    from core.auth import hash_password
    from tests.foundry_browser.seed import seed
    review_now = datetime.fromisoformat(os.environ.get('ODIN_FOUNDRY_REVIEW_TIME', '2026-11-04T12:00:00+00:00'))
    engine = create_engine(os.environ['DATABASE_URL'])
    # SQLite's server defaults otherwise put wall-clock dates on printers/spools.
    @event.listens_for(engine, 'connect')
    def fixed_defaults(connection, _):
        connection.create_function('current_timestamp', 0, lambda: review_now.replace(tzinfo=None).isoformat(' '))
    bootstrap_database(engine, ROOT / 'backend')
    if '--setup' not in sys.argv:
        with engine.begin() as connection:
            manifest = seed(connection, hash_password(os.environ['ODIN_FOUNDRY_TEST_PASSWORD']), review_now)
        (directory / 'seed.json').write_text(json.dumps(manifest, indent=2))
        # Freeze display clocks to the seeded review time; authentication and
        # session expiry continue to use the real clock.
        from core import printer_display_facts
        class ReviewClock(datetime):
            @staticmethod
            def now(tz=None):
                return review_now.astimezone(tz) if tz else review_now.replace(tzinfo=None)
        printer_display_facts.datetime = ReviewClock
        # Timeline computes the end of running MQTT jobs on the server, not in JS.
        from modules.jobs import scheduler_routes
        scheduler_routes.datetime = ReviewClock
        from modules.reporting.routes import analytics, reports
        from modules.reporting import education_usage
        analytics.datetime = reports.datetime = education_usage.datetime = ReviewClock
    # Local ephemeral test issuer, same pattern as education_browser/app_fixture.py.
    import base64
    import license_manager
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    key = Ed25519PrivateKey.generate()
    license_manager.ODIN_PUBLIC_KEY = key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
    payload = json.dumps({'tier': 'education', 'licensee': 'Disposable Foundry Review', 'email': 'reviewer@example.test', 'expires_at': '2099-12-31', 'max_printers': 100, 'max_users': 20, 'features': license_manager.TIERS['education']['features']}, sort_keys=True).encode()
    license_dir = Path(os.environ['LICENSE_DIR']); license_dir.mkdir()
    (license_dir / 'odin.license').write_text(base64.urlsafe_b64encode(payload).decode() + '.' + base64.urlsafe_b64encode(key.sign(payload)).decode())
    license_manager._cached_license = None
    from core.app import create_app
    from fastapi.responses import JSONResponse
    app = create_app()
    # Keep real camera enumeration while suppressing its side effect on host go2rtc.
    import modules.printers.camera_routes as camera_routes
    camera_routes.sync_go2rtc_config = lambda db: None

    @app.middleware('http')
    async def isolate_hardware(request, call_next):
        # Never connect to a real go2rtc instance or physical printer during review.
        path = request.url.path
        if ('/cameras/' in path and path.rsplit('/', 1)[-1] in {'webrtc', 'mjpeg', 'snapshot', 'stream'}) or ('/printers/' in path and request.method not in {'GET', 'HEAD'}):
            return JSONResponse({'detail': 'Camera/hardware transport is unavailable in this disposable fixture.'}, status_code=503)
        response = await call_next(request)
        if path == '/api/auth/login' and response.status_code == 200:
            # Authentication writes both login_success and auth.login. Keep its
            # lockout/session behavior intact; remove only fixture login noise.
            with engine.begin() as connection:
                connection.execute(text("DELETE FROM audit_logs WHERE action IN ('login_success','auth.login')"))
                connection.execute(text("UPDATE users SET last_login=:at WHERE id=1"), {'at': review_now.replace(tzinfo=None)})
        return response

    if '--check' in sys.argv:
        from fastapi.testclient import TestClient
        client = TestClient(app, base_url=f'http://127.0.0.1:{port}')
        login = client.post('/api/auth/login', data={'username': 'reviewer', 'password': os.environ['ODIN_FOUNDRY_TEST_PASSWORD']})
        assert login.status_code == 200, f'Login: {login.status_code}'
        routes = ['/api/printers', '/api/print-jobs?status=running', '/api/jobs', '/api/alerts', '/api/timeline', '/api/cameras', '/api/printers/103/nozzle-status',
                  '/api/models', '/api/products', '/api/orders', '/api/consumables', '/api/projects', '/api/projects/901',
                  '/api/archives', '/api/archives/log', '/api/timelapses', '/api/profiles', '/api/vision/detections',
                  '/api/education/capabilities', '/api/education/cost-centers?org_id=1601', '/api/overlay/101',
                  '/api/stats', '/api/analytics', '/api/education/usage-report?org_id=1601']
        visible_records = {
            '/api/models': 'Review enclosure', '/api/products': 'Review enclosure kit',
            '/api/orders': 'REVIEW-701', '/api/consumables': 'Review M3 fasteners',
            '/api/projects': 'Review workshop fixtures', '/api/projects/901': 'Review workshop fixtures',
            '/api/archives': 'Review completed bracket', '/api/archives/log': 'Review completed bracket',
            '/api/timelapses': 'review-unavailable.mp4', '/api/profiles': 'Review PLA process',
            '/api/vision/detections': 'spaghetti', '/api/education/cost-centers?org_id=1601': 'Review workshop',
        }
        for route in routes:
            response = client.get(route)
            assert response.status_code == 200, f'{route}: {response.status_code} {response.text[:200]}'
            if route in visible_records:
                assert visible_records[route] in response.text, f'{route}: seeded record was not visible'
        assert client.get('/api/smtp-config').json()['base_url'] == 'https://odin-review.example.test'
        first_timeline = client.get('/api/timeline').json()
        running_slots = [slot for slot in first_timeline['slots'] if slot.get('mqtt_job_id') in (1, 2)]
        assert len(running_slots) == 2
        for slot in running_slots:
            assert datetime.fromisoformat(slot['end']).replace(tzinfo=None) == review_now.replace(tzinfo=None)
        with engine.connect() as connection:
            first_audit = connection.execute(text('SELECT id,timestamp,action FROM audit_logs ORDER BY id')).fetchall()
        client.post('/api/auth/login', data={'username': 'reviewer', 'password': os.environ['ODIN_FOUNDRY_TEST_PASSWORD']})
        assert client.get('/api/timeline').json() == first_timeline
        with engine.connect() as connection:
            second_audit = connection.execute(text('SELECT id,timestamp,action FROM audit_logs ORDER BY id')).fetchall()
            assert second_audit == first_audit, f'Audit changed: {first_audit!r} -> {second_audit!r}'
        assert first_audit and all(row.action not in {'auth.login', 'login_success'} for row in first_audit)
        for route in ['/api/stats', '/api/analytics', '/api/education/usage-report?org_id=1601']:
            assert client.get(route).json() == client.get(route).json(), f'Non-deterministic display response: {route}'
        printers = client.get('/api/printers').json()
        x1 = next(p for p in printers if p['id'] == 101)
        h2d = next(p for p in printers if p['id'] == 103)
        assert x1['feeding_slot'] == 1 and x1['chamber_temp'] == 36
        assert h2d['chamber_temp'] == 30 and h2d['plate_clear_confirmed_at']
        jobs = client.get('/api/jobs').json()
        assert next(j for j in jobs if j['id'] == 203)['ams_slot_nozzles'][0]['nozzle'] == 'right'
        print(f'Fixture check: repeat-login audit/timeline determinism, auth, {len(routes)} read routes, {len(manifest["page_records"])} Part B record types and all 4 board facts passed')
        engine.dispose()
        return
    import uvicorn
    try:
        uvicorn.run(app, host='127.0.0.1', port=port, lifespan='off', access_log=False)
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()
