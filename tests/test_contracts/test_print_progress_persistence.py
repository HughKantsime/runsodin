"""Actual progress persistence against owned SQLite and commit refusal."""
from contextlib import contextmanager
from types import SimpleNamespace
from sqlalchemy import text
from tests.test_contracts.test_printer_offline_behavior import offline_store, review_db
from tests.test_contracts.test_api_token_delegation_behavior import token_client
from tests.test_contracts.test_printer_offline_behavior import v2_offline_monitor


def test_moonraker_failed_progress_commit_remains_immediately_retryable(offline_store, token_client, monkeypatch, caplog):
    from modules.printers.monitors import moonraker_monitor as module
    monitor = module.MoonrakerMonitor(199, 'Synthetic', '127.0.0.1')
    monitor._current_job_db_id = 199
    monitor._last_progress_write = 0
    monkeypatch.setattr(module, 'time', SimpleNamespace(time=lambda: 2000))
    real = offline_store.get_bind()
    class Engine:
        fail = True
        @contextmanager
        def begin(self):
            with real.begin() as conn:
                yield conn
                if self.fail:
                    self.fail = False
                    raise RuntimeError('synthetic-private-progress-commit')
    monkeypatch.setattr(module, 'engine', Engine())
    from modules.jobs.routes import presets
    client, issue = token_client
    client.app.include_router(presets.router)
    headers = issue(['read'])
    def readback():
        response = client.get('/print-jobs?printer_id=199&status=running', headers=headers)
        assert response.status_code == 200
        assert len(response.json()) == 1
        row = response.json()[0]
        assert row['id'] == 199 and row['status'] == 'running' and row['ended_at'] is None
        return row['progress_percent'], row['current_layer'], row['remaining_minutes']
    status = SimpleNamespace(progress_percent=50, current_layer=10, print_duration=600)
    before = tuple(offline_store.execute(text('SELECT progress_percent,current_layer,remaining_minutes FROM print_jobs WHERE id=199')).one())
    assert readback() == before
    monitor._update_progress(status)
    assert tuple(offline_store.execute(text('SELECT progress_percent,current_layer,remaining_minutes FROM print_jobs WHERE id=199')).one()) == before
    assert monitor._last_progress_write == 0
    assert 'synthetic-private-progress-commit' not in caplog.text
    assert readback() == before
    monitor._update_progress(status)
    assert tuple(offline_store.execute(text('SELECT progress_percent,current_layer,remaining_minutes FROM print_jobs WHERE id=199')).one()) == (50,10,10)
    assert monitor._last_progress_write == 2000
    assert readback() == (50,10,10)


def test_bambu_progress_commit_refusal_preserves_zero_retry_and_private_detail(v2_offline_monitor, offline_store, monkeypatch, caplog):
    monitor, _, module = v2_offline_monitor
    original = module.get_db
    failed = [False]
    class Connection:
        def __init__(self, conn): self.conn = conn
        def __getattr__(self, name): return getattr(self.conn, name)
        def commit(self):
            if not failed[0]:
                failed[0] = True
                self.conn.rollback()
                raise RuntimeError('synthetic-private-bambu-progress')
            self.conn.commit()
    @contextmanager
    def database():
        with original() as conn: yield Connection(conn)
    monkeypatch.setattr(module, 'get_db', database)
    monitor._current_job_id = 199
    monitor._last_progress_update = 0
    monitor._state = {'mc_percent': 0, 'mc_remaining_time': 0, 'layer_num': 0}
    before = tuple(offline_store.execute(text('SELECT progress_percent,current_layer,remaining_minutes FROM print_jobs WHERE id=199')).one())
    monitor._update_progress()
    assert tuple(offline_store.execute(text('SELECT progress_percent,current_layer,remaining_minutes FROM print_jobs WHERE id=199')).one()) == before
    assert monitor._last_progress_update == 0
    assert 'synthetic-private-bambu-progress' not in caplog.text
    monitor._update_progress()
    assert tuple(offline_store.execute(text('SELECT progress_percent,current_layer,remaining_minutes FROM print_jobs WHERE id=199')).one()) == (0,0,0)
    assert monitor._last_progress_update == 2000
