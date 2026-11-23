import pytest
from sqlalchemy import text
from tests.test_contracts.test_finance_currency_authority import finance_client, signed, interactive_headers, rows
from tests.test_contracts.test_api_token_delegation_behavior import token_client, review_db


@pytest.fixture
def config_client(finance_client):
    from modules.system.routes_config import router
    client, issue = finance_client
    client.app.include_router(router, prefix='/api')
    return client, issue


@pytest.mark.parametrize('role', ['viewer', 'operator', 'admin', 'token'])
def test_config_currency_is_scoped_and_read_only(config_client, review_db, role):
    client, issue = config_client
    review_db.execute(text("UPDATE users SET role='operator' WHERE id=2"))
    review_db.execute(text("INSERT INTO finance_currency_settings(owner_scope,owner_key,currency) VALUES('global',0,'GBP'),('org',2,'JPY')"))
    review_db.commit()
    headers = issue(['read']) if role == 'token' else signed({'viewer':'student','operator':'manager','admin':'admin'}[role])
    def read():
        response = client.get('/api/config', headers=headers)
        assert response.status_code == 200, response.text
        return response.json()['currency'], response.json()['currency_basis']
    assert read() == (None, None)
    assert rows(review_db, 'finance_currency_settings') == 2
    assert client.put('/api/orders/finance-settings', json={'currency':'EUR'}, headers=interactive_headers()).status_code == 200
    assert read() == ('EUR', 'organization')
    review_db.execute(text("UPDATE users SET group_id=NULL WHERE id=3")); review_db.commit()
    assert client.get('/api/config', headers=interactive_headers()).json()['currency_basis'] == 'global'
    review_db.execute(text("UPDATE users SET group_id=NULL WHERE id=1")); review_db.commit()
    assert client.get('/api/config', headers=signed('student')).json()['currency'] == 'GBP'


def test_browser_config_projections(config_client, review_db):
    """Capture the config fields consumed by the browser scenarios with CAPTURE=1."""
    from pathlib import Path
    from core.farm_time import set_farm_timezone
    from tests.test_contracts.cert_D_capture import publish
    fixtures = Path(__file__).parents[1] / 'accessibility' / 'fixtures'
    set_farm_timezone(review_db, 'America/New_York')
    review_db.commit()
    response = config_client[0].get('/api/config', headers=interactive_headers())
    assert response.status_code == 200
    config = response.json()
    for filename in ('fleet', 'admin', 'common'):
        fields = ['currency', 'currency_basis']
        if filename == 'fleet':
            fields.append('farm_timezone_effective')
        publish(fixtures / (filename + '.json'), {k: config[k] for k in fields}, key='GET /api/config')


def test_web_has_no_dollar_template_for_money():
    from pathlib import Path
    src = Path(__file__).parents[2] / 'frontend' / 'src'
    assert not [str(p.relative_to(src)) for p in src.rglob('*') if p.suffix in ('.ts','.tsx')
                and '.test.' not in p.name and '$${' in p.read_text()]
