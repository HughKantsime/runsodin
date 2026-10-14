"""Read-only report proof on genuine org/global economic graphs."""
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_prospective_economic_identity import engine, graph, Job
from core.economic_identity import _add_created
from core.report_job_ownership import proved_job_owner_sql
import sqlite3


def eligible(db, identifier):
    return db.execute(text('SELECT j.id FROM jobs j WHERE j.id=:id AND ('+proved_job_owner_sql()+')'),{'id':identifier}).scalar_one_or_none()


@pytest.mark.parametrize('org',[None,7])
@pytest.mark.parametrize('linked',[False,True])
def test_genuine_org_global_graph_is_readonly(engine,org,linked):
    with Session(engine) as db:
        job=graph(db,org)[2] if linked else _add_created(db,Job(charged_to_org_id=org),'job',owner_org_id=org,global_authority=org is None)
        db.commit();identifier=job.id
        tables=('jobs','orders','order_items','cost_identities','cost_order_memberships','cost_job_bindings')
        before={table:[tuple(r) for r in db.execute(text('SELECT * FROM '+table))] for table in tables}
        assert eligible(db,identifier)==identifier
        assert {table:[tuple(r) for r in db.execute(text('SELECT * FROM '+table))] for table in tables}==before


def test_retained_uid_does_not_authorize_numeric_job_reuse(engine):
    with Session(engine) as db:
        job=_add_created(db,Job(charged_to_org_id=7),'job',owner_org_id=7)
        db.commit();identifier=job.id
        assert eligible(db,identifier)==identifier
        db.execute(text('DELETE FROM jobs WHERE id=:id'),{'id':identifier});db.commit()
        db.execute(text('INSERT INTO jobs(id,charged_to_org_id) VALUES(:id,7)'),{'id':identifier});db.commit()
        assert eligible(db,identifier) is None
        assert db.execute(text('SELECT COUNT(*) FROM cost_identities')).scalar_one()==1


@pytest.mark.parametrize('corruption',[
    "DELETE FROM cost_identities WHERE resource_kind='job'",
    "UPDATE cost_identities SET resource_kind='item' WHERE resource_kind='job'",
    "UPDATE cost_identities SET owner_key=99 WHERE resource_kind='job'",
    'DELETE FROM cost_job_bindings',
    'UPDATE cost_job_bindings SET owner_key=99',
    "DELETE FROM cost_order_memberships WHERE child_kind='job'",
    "DELETE FROM cost_order_memberships WHERE child_kind='item'",
    "UPDATE cost_order_memberships SET owner_key=99 WHERE child_kind='job'",
])
def test_unvalidated_restored_graph_is_not_report_authority(engine,corruption):
    # Deliberately unvalidated restore-shaped database: copy real column schemas
    # and genuine graph rows into a private database with no triggers. Production
    # guards are untouched; this tests the reader's independent contradiction
    # checks, not whether production permits these writes or restore is valid.
    with Session(engine) as db:
        identifier=graph(db,7)[2].id;db.commit()
        tables=('jobs','orders','order_items','cost_identities','cost_order_memberships','cost_job_bindings')
        with sqlite3.connect(':memory:') as restored:
            for table in tables:
                columns=db.execute(text('PRAGMA table_info('+table+')')).all()
                restored.execute('CREATE TABLE '+table+' ('+','.join('"'+r[1]+'" '+r[2] for r in columns)+')')
                rows=[tuple(r) for r in db.execute(text('SELECT * FROM '+table))]
                if rows:restored.executemany('INSERT INTO '+table+' VALUES ('+','.join('?' for _ in columns)+')',rows)
            query='SELECT j.id FROM jobs j WHERE j.id=? AND ('+proved_job_owner_sql()+')'
            assert restored.execute(query,(identifier,)).fetchone()==(identifier,)
            restored.execute(corruption)
            assert restored.execute(query,(identifier,)).fetchone() is None
        assert eligible(db,identifier)==identifier
