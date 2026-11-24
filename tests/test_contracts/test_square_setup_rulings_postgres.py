"""ODIN Dev: run against the existing disposable PostgreSQL harness, never production."""
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from tests.test_contracts.test_square_store_concurrency import pg_url
from tests.test_contracts.test_square_readonly_workflow import shop
from tests.test_contracts.test_square_setup_delegation import setup_policy
from tests.test_contracts.test_square_setup_rulings import (
    education_mode_gate, test_two_token_baselines_race_one_wins,
)

from tests.test_contracts.test_square_setup_leftovers import (
    test_audit_failure_rolls_back_demotion_and_grants, test_owner_change_removes_each_grant,
    test_refusal_window_serializes_concurrent_requests, test_demotion_while_grant_waits_requires_a_new_grant,
)

pytestmark = pytest.mark.postgresql


@pytest.fixture
def review_db(pg_url):
    from core.schema import bootstrap_database
    # Bootstrap's security triggers use public; isolate each case in an owned DB.
    database = 'square_setup_' + uuid4().hex
    admin = create_engine(pg_url, isolation_level='AUTOCOMMIT')
    with admin.connect() as db:
        db.execute(text(f'CREATE DATABASE {database}'))
    engine = create_engine(make_url(pg_url).set(database=database))
    try:
        bootstrap_database(engine)
        with engine.begin() as db:
            db.execute(text("INSERT INTO groups(id,name,is_org) VALUES(1,'Shop',TRUE),(2,'Other',TRUE)"))
            db.execute(text("INSERT INTO users(id,username,password_hash,role,is_active,group_id) VALUES"
                            "(3,'admin','fixture','admin',TRUE,1),(5,'other','fixture','admin',TRUE,2)"))
        with Session(engine) as db:
            yield db
    finally:
        engine.dispose()
        with admin.connect() as db:
            db.execute(text(f'DROP DATABASE {database}'))
        admin.dispose()


def test_education_mode_gate_on_postgresql(shop):
    # ORM stores a JSON boolean; a raw PostgreSQL SELECT returns Python True.
    education_mode_gate(shop)
