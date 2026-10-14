"""Owned schema boundaries for restricted enrollment authority."""
import pytest
from sqlalchemy import create_engine,text,inspect
from tests.test_contracts.test_education_review_workflow import review_db,BACKEND
from tests.test_contracts.test_api_token_delegation_behavior import token_client


def test_fresh_enrollment_schema_rejects_invalid_authority_states(review_db):
    from sqlalchemy.exc import IntegrityError
    valid={'jti':'synthetic-authority','uid':1,'expiry':2_000_000_000.0,'created':1_900_000_000.0}
    for mode in ('empty_jti','bad_state','past_bound','terminal_secret'):
        values=dict(valid)
        if mode=='empty_jti':values['jti']=''
        if mode=='past_bound':values['expiry']=-1
        state='other' if mode=='bad_state' else ('consumed' if mode=='terminal_secret' else 'pending')
        secret='synthetic-ciphertext' if mode=='terminal_secret' else None
        with pytest.raises(IntegrityError):
            review_db.execute(text('INSERT INTO mfa_enrollments(jti,user_id,expires_at,created_at,state,encrypted_pending_secret) VALUES(:jti,:uid,:expiry,:created,:state,:secret)'),{**values,'state':state,'secret':secret})
        review_db.rollback()


def test_authority_ciphertext_is_erased_with_user_deletion(review_db):
    review_db.execute(text("INSERT INTO mfa_enrollments(jti,user_id,expires_at,created_at,encrypted_pending_secret) VALUES('synthetic-deletion',4,2000000000,1900000000,'synthetic-ciphertext')"))
    review_db.commit()
    # Fixture engines do not globally enable SQLite FK enforcement.
    with review_db.get_bind().connect() as connection:
        connection.execute(text('PRAGMA foreign_keys=ON'))
        connection.execute(text('DELETE FROM users WHERE id=4'))
        connection.commit()
        assert connection.execute(text('SELECT COUNT(*) FROM mfa_enrollments')).scalar_one()==0


@pytest.mark.parametrize('defect',['missing_fk','wrong_state_check','missing_pk','bad_type'])
def test_malformed_existing_table_is_not_silently_adopted(tmp_path,defect):
    from core.schema import bootstrap_database
    sql=(BACKEND/'modules/organizations/migrations/004_mfa_enrollments.sql').read_text()
    if defect=='missing_fk':sql=sql.replace(' REFERENCES users(id) ON DELETE CASCADE','')
    elif defect=='wrong_state_check':sql=sql.replace("state IN ('pending', 'consumed', 'cancelled')",'1 = 1')
    elif defect=='missing_pk':sql=sql.replace('TEXT NOT NULL PRIMARY KEY','TEXT NOT NULL')
    elif defect=='bad_type':sql=sql.replace('expires_at DOUBLE PRECISION','expires_at TEXT')
    engine=create_engine('sqlite:///'+str(tmp_path/'malformed.db'))
    with engine.begin() as db:
        for statement in sql.split(';'):
            if statement.strip():db.exec_driver_sql(statement)
    with pytest.raises(RuntimeError,match='Invalid MFA enrollment authority schema'):
        bootstrap_database(engine,BACKEND)
    engine.dispose()


def test_backup_round_trip_preserves_enrollment_ciphertext_and_rebootstrap(review_db,tmp_path):
    from core.schema import bootstrap_database
    from modules.system.backup_service import create_online_backup,validate_database
    review_db.execute(text("INSERT INTO mfa_enrollments(jti,user_id,expires_at,created_at,encrypted_pending_secret) VALUES('synthetic-backup',4,2000000000,1900000000,'synthetic-ciphertext')"))
    review_db.commit()
    backup,metadata=create_online_backup(str(review_db.get_bind().url),base_dir=tmp_path/'owned-backups')
    assert validate_database(backup)['table_count']>0
    restored=create_engine('sqlite:///'+str(backup))
    bootstrap_database(restored,BACKEND)
    bootstrap_database(restored,BACKEND)
    with restored.connect() as db:
        row=db.execute(text("SELECT user_id,state,encrypted_pending_secret FROM mfa_enrollments WHERE jti='synthetic-backup'")).one()
        assert tuple(row)==(4,'pending','synthetic-ciphertext')
    restored.dispose()


def test_privacy_erasure_removes_pending_authority(token_client,review_db):
    from tests.test_contracts.test_api_token_delegation_behavior import interactive_headers
    client,_=token_client
    review_db.execute(text("INSERT INTO mfa_enrollments(jti,user_id,expires_at,created_at,encrypted_pending_secret) VALUES('synthetic-erasure',4,2000000000,1900000000,'synthetic-ciphertext')"))
    review_db.commit()
    response=client.delete('/users/4/erase',headers=interactive_headers())
    assert response.status_code==200
    assert review_db.execute(text('SELECT COUNT(*) FROM mfa_enrollments WHERE user_id=4')).scalar_one()==0
    assert review_db.execute(text('SELECT is_active FROM users WHERE id=4')).scalar_one()==0


@pytest.mark.parametrize('defect',['literal_case','extra_default','extra_check'])
def test_adopted_authority_contract_preserves_literals_and_exact_shape(tmp_path,defect):
    from core.schema import bootstrap_database
    sql=(BACKEND/'modules/organizations/migrations/004_mfa_enrollments.sql').read_text()
    if defect=='literal_case':sql=sql.replace("'pending', 'consumed', 'cancelled'","'PENDING', 'CONSUMED', 'CANCELLED'")
    elif defect=='extra_default':sql=sql.replace('created_at DOUBLE PRECISION NOT NULL','created_at DOUBLE PRECISION NOT NULL DEFAULT 0')
    else:sql=sql.replace("CHECK (length(jti) > 0)","CHECK (length(jti) > 0), CONSTRAINT synthetic_extra CHECK (user_id=1)")
    engine=create_engine('sqlite:///'+str(tmp_path/'contract.db'))
    with engine.begin() as db:
        for statement in sql.split(';'):
            if statement.strip():db.exec_driver_sql(statement)
    with pytest.raises(RuntimeError,match='Invalid MFA enrollment authority schema'):
        bootstrap_database(engine,BACKEND)
    engine.dispose()


def test_adoption_rejects_invalid_retained_authority_rows(review_db):
    from core.schema import bootstrap_database
    review_db.execute(text("INSERT INTO mfa_enrollments(jti,user_id,expires_at,created_at) VALUES('synthetic-invalid-row',4,2000000000,-1)"))
    review_db.commit()
    with pytest.raises(RuntimeError,match='Invalid MFA enrollment authority rows'):
        bootstrap_database(review_db.get_bind(),BACKEND)


@pytest.mark.parametrize('defect',['varchar_identity','composite_fk'])
def test_authority_types_and_foreign_key_are_exact(tmp_path,defect):
    from core.schema import bootstrap_database
    sql=(BACKEND/'modules/organizations/migrations/004_mfa_enrollments.sql').read_text()
    if defect=='varchar_identity':sql=sql.replace('jti TEXT NOT NULL','jti VARCHAR(16) NOT NULL')
    else:
        sql=sql.replace(' REFERENCES users(id) ON DELETE CASCADE','')
        sql=sql.replace('CONSTRAINT mfa_enrollment_jti_nonempty',"FOREIGN KEY(user_id,state) REFERENCES users(id,username) ON DELETE CASCADE, CONSTRAINT mfa_enrollment_jti_nonempty")
    engine=create_engine('sqlite:///'+str(tmp_path/'exact.db'))
    if defect=='composite_fk':
        bootstrap_database(engine,BACKEND)
        with engine.begin() as db:
            db.exec_driver_sql('CREATE UNIQUE INDEX synthetic_parent_pair ON users(id,username)')
            db.exec_driver_sql('DROP TABLE mfa_enrollments')
    with engine.begin() as db:
        for statement in sql.split(';'):
            if statement.strip():db.exec_driver_sql(statement)
    with pytest.raises(RuntimeError,match='Invalid MFA enrollment authority schema'):
        bootstrap_database(engine,BACKEND)
    engine.dispose()
