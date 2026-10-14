"""Fresh persisted broker snapshot for an owned power worker, no connection."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_education_review_workflow import review_db


@pytest.fixture
def settings(review_db,monkeypatch):
    from modules.notifications import mqtt_republish as module
    monkeypatch.setattr(module,'engine',review_db.get_bind())
    monkeypatch.setattr(module,'_config_cache',{'host':'obsolete.invalid','password':'obsolete synthetic'})
    monkeypatch.setattr(module,'_config_ts',10**20)
    monkeypatch.setattr(module,'_get_client',lambda:pytest.fail('Snapshot must not acquire connection'))
    def save(**changes):
        values={'enabled':'true','host':'127.0.0.1','port':'1883','username':'synthetic-user','password':'synthetic-password','use_tls':'false','topic_prefix':'not-worker-input'}
        values.update(changes)
        for key,value in values.items():
            review_db.execute(text('DELETE FROM system_config WHERE key=:key'),{'key':'mqtt_republish_'+key})
            review_db.execute(text('INSERT INTO system_config(key,value) VALUES(:key,:value)'),{'key':'mqtt_republish_'+key,'value':value})
        review_db.commit()
    save()
    return module,save


def test_snapshot_reads_latest_committed_settings_without_cached_client(settings):
    module,save=settings
    first=module.get_power_connection_snapshot()
    assert (first.host,first.port,first.use_tls,first.username,first.password)==('127.0.0.1',1883,False,'synthetic-user','synthetic-password')
    save(host='localhost',port='8883',use_tls='true')
    second=module.get_power_connection_snapshot()
    assert (second.host,second.port,second.use_tls)==('localhost',8883,True)
    assert first.host=='127.0.0.1'
    assert 'topic_prefix' not in vars(second)
    assert 'synthetic-password' not in repr(second)
    assert 'synthetic-user' not in repr(second)


@pytest.mark.parametrize('changes',[{'enabled':'false'},{'enabled':'unexpected'},{'host':''},{'host':'user@private.invalid'},{'host':'http://localhost'},{'port':'0'},{'port':'65536'},{'port':'invalid'},{'use_tls':'uncertain'}])
def test_invalid_or_disabled_settings_refuse_without_fallback(settings,changes):
    module,save=settings;save(**changes)
    assert module.get_power_connection_snapshot() is None


def test_password_decryption_preserves_only_decoded_snapshot(settings,monkeypatch):
    import core.crypto as crypto
    module,save=settings;save(password='synthetic-encrypted-value')
    monkeypatch.setattr(crypto,'decrypt',lambda value:'synthetic-decoded-password' if value=='synthetic-encrypted-value' else pytest.fail('unexpected password'))
    snapshot=module.get_power_connection_snapshot()
    assert snapshot.password=='synthetic-decoded-password'
    assert 'synthetic-decoded-password' not in repr(snapshot)


def test_snapshot_database_failure_has_no_exception_or_secret_echo(settings,monkeypatch,caplog):
    from types import SimpleNamespace
    module,save=settings
    def failed():raise RuntimeError('synthetic private marker')
    monkeypatch.setattr(module,'engine',SimpleNamespace(connect=failed))
    assert module.get_power_connection_snapshot() is None
    assert 'synthetic private marker' not in caplog.text


def test_ipv6_and_legacy_plaintext_supported_without_host_repr(settings,monkeypatch):
    import core.crypto as crypto
    module,save=settings;save(host='::1')
    monkeypatch.setattr(crypto,'decrypt',lambda value:value)
    snapshot=module.get_power_connection_snapshot()
    assert snapshot.host=='::1'
    assert snapshot.password=='synthetic-password'
    assert '::1' not in repr(snapshot)
    from dataclasses import FrozenInstanceError
    with pytest.raises(FrozenInstanceError):snapshot.host='changed.invalid'


@pytest.mark.parametrize('failure',['exception','undecoded'])
def test_decryption_failure_refuses_without_plaintext_guess(settings,monkeypatch,caplog,failure):
    import core.crypto as crypto
    module,save=settings
    save(password='gAAAAA'+'x'*60)
    def decrypt(value):
        if failure=='exception':raise RuntimeError('synthetic private marker')
        return value
    monkeypatch.setattr(crypto,'decrypt',decrypt)
    assert module.get_power_connection_snapshot() is None
    assert 'synthetic private marker' not in caplog.text


@pytest.mark.parametrize('port',[True,False,0,-1,65536,1.5,None])
def test_invalid_typed_port_in_settings_never_becomes_worker_target(settings,monkeypatch,port):
    from types import SimpleNamespace
    module,save=settings
    rows=[('mqtt_republish_enabled','true'),('mqtt_republish_host','localhost'),('mqtt_republish_port',port)]
    class Connection:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def execute(self,*args):return rows
    monkeypatch.setattr(module,'engine',SimpleNamespace(connect=Connection))
    assert module.get_power_connection_snapshot() is None


def test_snapshot_uses_uncommitted_caller_connection_without_closing_it(settings):
    module,save=settings
    with module.engine.connect() as conn:
        tx=conn.begin()
        conn.execute(text("UPDATE system_config SET value='transaction.example.test' WHERE key='mqtt_republish_host'"))
        assert module.get_power_connection_snapshot(connection=conn).host=='transaction.example.test'
        assert not conn.closed and tx.is_active
        tx.rollback()
    assert module.get_power_connection_snapshot().host=='127.0.0.1'
