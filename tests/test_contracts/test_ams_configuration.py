"""Current database transport facts, never plaintext fallback or caller digest."""
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text
from modules.printers.telemetry.bambu.ams_configuration import load_ams_configuration, AMSConfigurationUnavailable


@pytest.fixture
def configured(monkeypatch):
    key = Fernet.generate_key()
    monkeypatch.setenv('ENCRYPTION_KEY', key.decode())
    engine = create_engine('sqlite://')
    with engine.begin() as db:
        db.execute(text('CREATE TABLE printers(id INTEGER PRIMARY KEY,org_id INTEGER,api_type TEXT,api_host TEXT,api_key TEXT,is_active,shared)'))
        db.execute(text('INSERT INTO printers VALUES(1,2,\'bambu\',\'printer.local\',:secret,1,0)'),
                   {'secret': Fernet(key).encrypt(b'SYNTHETIC|test-only-access').decode()})
        yield db, key
    engine.dispose()


def test_current_configuration_digest_preserves_transport_changes(configured):
    db, key = configured
    first = load_ams_configuration(db, 1)
    assert first.owner_org_id == 2 and first.transport.use_tls
    assert 'test-only-access' not in repr(first) and 'SYNTHETIC' not in repr(first)
    assert load_ams_configuration(db, 1).configuration_key == first.configuration_key
    # Re-encryption of identical transport facts is not a configuration change.
    db.execute(text('UPDATE printers SET api_key=:secret'), {'secret': Fernet(key).encrypt(b'SYNTHETIC|test-only-access').decode()})
    assert load_ams_configuration(db, 1).configuration_key == first.configuration_key
    db.execute(text("UPDATE printers SET api_host='PRINTER.local'"))
    assert load_ams_configuration(db, 1).configuration_key != first.configuration_key
    db.execute(text('UPDATE printers SET org_id=3'))
    assert load_ams_configuration(db, 1).owner_org_id == 3


@pytest.mark.parametrize('column,value', [('api_key','SYNTHETIC|plaintext'), ('api_key','x'*4097),
    ('api_type','BAMBU'), ('api_host','host/route'), ('api_host','host:8883'),
    ('api_host','fe80::1%unverified-interface'),
    ('is_active',0), ('is_active','true'), ('shared',1), ('shared','false'), ('org_id',0)])
def test_invalid_current_facts_refuse_without_leaking_values(configured,column,value):
    db, _ = configured
    assert column in {'api_key','api_type','api_host','is_active','shared','org_id'}
    db.execute(text('UPDATE printers SET '+column+'=:value'), {'value':value})
    with pytest.raises(AMSConfigurationUnavailable, match='^Fresh printer configuration unavailable$'):
        load_ams_configuration(db,1)


@pytest.mark.parametrize('decoded', [b'SYNTHETIC|one|two', b'bad/topic|test-only', b'SYNTHETIC|white space', b'SYNTHETIC|', b'\xff'])
def test_invalid_decoded_structure_refuses(configured,decoded):
    db,key=configured
    db.execute(text('UPDATE printers SET api_key=:value'), {'value':Fernet(key).encrypt(decoded).decode()})
    with pytest.raises(AMSConfigurationUnavailable):load_ams_configuration(db,1)


def test_missing_key_and_deleted_printer_refuse(configured,monkeypatch):
    db,_=configured
    monkeypatch.delenv('ENCRYPTION_KEY')
    with pytest.raises(AMSConfigurationUnavailable):load_ams_configuration(db,1)
    db.execute(text('DELETE FROM printers'))
    with pytest.raises(AMSConfigurationUnavailable):load_ams_configuration(db,1)
