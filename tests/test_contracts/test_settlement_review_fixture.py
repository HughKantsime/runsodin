"""Check the native race's real HTTP fixture without requiring PostgreSQL."""
import pytest
from sqlalchemy import create_engine, text
from core.schema import bootstrap_database
from tests.test_contracts.test_settlement_review_postgresql import settlement_client, seed_completion


@pytest.mark.parametrize('first', ['complete', 'settle'])
@pytest.mark.parametrize('shared_spool', [True, False])
def test_native_race_fixture_reaches_completion_accounting(tmp_path, monkeypatch, first, shared_spool):
    from modules.jobs.routes.jobs_lifecycle import router
    from modules.inventory import services

    monkeypatch.setattr(services, 'push_consumption_to_spoolman', lambda *_: [])
    engine = create_engine('sqlite:///' + str(tmp_path / 'settlement.db'))
    try:
        bootstrap_database(engine)
        with settlement_client(engine, monkeypatch) as (client, _, archive, _):
            client.app.include_router(router)
            seed_completion(engine, shared_spool)
            if first == 'settle':
                response = client.post(f'/archives/{archive}/settle-filament',
                                       json={'entries': [{'spool_id': 991240, 'grams': 40}]})
                assert response.status_code == 200, response.text
            response = client.post('/jobs/991240/complete')
            assert response.status_code == 200, response.text
            with engine.connect() as db:
                winning_spool = 991241 if first == 'complete' and not shared_spool else 991240
                assert db.execute(text('SELECT id,remaining_weight_g FROM spools ORDER BY id')).all() == [
                    (spool, 960 if spool == winning_spool else 1000)
                    for spool in ([991240] if shared_spool else [991240, 991241])]
                assert db.execute(text('SELECT COUNT(*),SUM(weight_used_g) FROM spool_usage')).one() == (1, 40)
                assert db.execute(text("SELECT status FROM jobs WHERE id=991240")).scalar_one() == 'completed'
                if first == 'complete':
                    assert db.execute(text('SELECT notes FROM spool_usage')).scalar_one().startswith('auto:operator_complete:')
    finally:
        engine.dispose()
