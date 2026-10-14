"""Actual parent fences remain separate from job and observation authority."""
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_product_bom_integrity import bom_client
from tests.test_contracts.test_order_production_behavior import production_order


@pytest.mark.parametrize('executor_kind', ['session', 'connection', 'raw'])
def test_parent_graph_fences_orders_items_without_writing_jobs(production_order, review_db, monkeypatch, executor_kind):
    from modules.orders.production import Execution
    from core.order_production import prepare_parent_graph
    client, _, order_id = production_order
    response = client.post(f'/orders/{order_id}/schedule')
    assert response.status_code == 200, response.text
    jobs = review_db.execute(text('SELECT id FROM jobs WHERE order_item_id IN (SELECT id FROM order_items WHERE order_id=:id) ORDER BY id'), {'id': order_id}).scalars().all()
    review_db.commit()
    statements = []
    original = Execution.execute
    def execute(self, statement, parameters=None):
        statements.append(statement)
        return original(self, statement, parameters)
    monkeypatch.setattr(Execution, 'execute', execute)
    engine = review_db.get_bind()
    if executor_kind == 'session':
        with Session(engine) as executor:
            executor.begin()
            graph = prepare_parent_graph(executor, reversed(jobs)); executor.rollback()
    elif executor_kind == 'connection':
        with engine.begin() as executor:
            graph = prepare_parent_graph(executor, reversed(jobs))
    else:
        raw = engine.raw_connection()
        try:
            raw.execute('PRAGMA foreign_keys=ON'); raw.execute('BEGIN IMMEDIATE')
            graph = prepare_parent_graph(raw, reversed(jobs)); raw.rollback()
        finally:
            raw.close()
    assert graph[0] == (order_id,)
    assert len(graph[1]) == 1 and len(graph[2]) == len(jobs)
    writes = [statement for statement in statements if statement.startswith('UPDATE')]
    assert writes and writes[0].startswith('UPDATE orders')
    assert all(statement.startswith(('UPDATE orders', 'UPDATE order_items')) for statement in writes)
    assert [row[0] for row in graph[2]] == sorted(jobs)


def test_parent_graph_refuses_changed_edge_after_parent_acquisition(production_order, review_db, monkeypatch):
    from modules.orders.production import Execution
    from core.order_production import prepare_parent_graph, ProductionLinkageChanged
    client, _, order_id = production_order
    response = client.post(f'/orders/{order_id}/schedule'); assert response.status_code == 200
    item_id = review_db.execute(text('SELECT min(id) FROM order_items WHERE order_id=:id'), {'id': order_id}).scalar_one()
    # Unidentified legacy linkage is mutable; a registered economic job's edge
    # is already immutable and correctly rejects this mutation in storage.
    job_id = 700
    review_db.execute(text("INSERT INTO jobs(id,item_name,order_item_id) VALUES(:id,'Owned mutable parent candidate',:item)"), {'id': job_id, 'item': item_id})
    review_db.commit()
    original = Execution.execute
    changed = False
    def execute(self, statement, parameters=None):
        nonlocal changed
        result = original(self, statement, parameters)
        if statement.startswith('UPDATE order_items') and not changed:
            changed = True
            original(self, 'UPDATE jobs SET order_item_id=NULL WHERE id=:id', {'id': job_id})
        return result
    monkeypatch.setattr(Execution, 'execute', execute)
    with Session(review_db.get_bind()) as executor:
        executor.begin()
        with pytest.raises(ProductionLinkageChanged):
            prepare_parent_graph(executor, [job_id])
        executor.rollback()
    assert changed
    assert review_db.execute(text('SELECT order_item_id FROM jobs WHERE id=:id'), {'id': job_id}).scalar_one() is not None


@pytest.mark.parametrize('values', [[True], [0], [-1], ['1'], [1, '2'], range(1, 18)])
def test_parent_graph_rejects_invalid_or_excessive_ids_before_provider(values, monkeypatch):
    from core.order_production import prepare_parent_graph
    from core.registry import registry
    from fastapi import HTTPException
    def forbidden(name):
        pytest.fail('Invalid candidates must refuse before provider lookup')
    monkeypatch.setattr(registry, 'get_provider', forbidden)
    with pytest.raises(HTTPException) as error:
        prepare_parent_graph(None, values)
    assert error.value.status_code == 400


@pytest.mark.parametrize('kind', ['session', 'connection', 'adapter'])
@pytest.mark.parametrize('drift', [False, True])
def test_nonlocking_recheck_after_job_lock_preserves_or_refuses_ancestry(production_order, review_db, monkeypatch, kind, drift):
    """Real caller locks job; recheck must SELECT only and own no transaction."""
    from core.order_production import prepare_parent_graph, recheck_parent_graph, ProductionLinkageChanged
    from core.db_utils import _ConnectionAdapter
    from core.economic_source_creation import _execute
    from modules.orders.production import Execution
    client, _, order_id = production_order
    response = client.post(f'/orders/{order_id}/schedule')
    assert response.status_code == 200, response.text
    item_id = review_db.execute(text('SELECT min(id) FROM order_items WHERE order_id=:id'), {'id': order_id}).scalar_one()
    # Unanchored fixture permits a real mutable edge, without disabling guards.
    review_db.execute(text("INSERT INTO jobs(id,item_name,order_item_id) VALUES(799,'Owned recheck candidate',:item)"), {'item': item_id})
    review_db.commit()
    engine = review_db.get_bind(); raw = None
    if kind == 'session':
        executor = Session(engine); executor.begin()
    elif kind == 'connection':
        executor = engine.connect(); executor.begin()
    else:
        raw = engine.raw_connection(); raw.execute('PRAGMA foreign_keys=ON'); raw.execute('BEGIN IMMEDIATE')
        executor = _ConnectionAdapter(raw, False)
    try:
        graph = prepare_parent_graph(executor, [799])
        _execute(executor, 'UPDATE jobs SET id=id WHERE id=?', (799,))
        if drift:
            _execute(executor, 'UPDATE jobs SET order_item_id=NULL WHERE id=?', (799,))
        statements = []; original = Execution.execute
        def observe(self, statement, parameters=None):
            statements.append(statement)
            return original(self, statement, parameters)
        monkeypatch.setattr(Execution, 'execute', observe)
        if drift:
            with pytest.raises(ProductionLinkageChanged):
                recheck_parent_graph(executor, graph)
        else:
            recheck_parent_graph(executor, graph)
        assert statements, 'Recheck must read actual current ancestry'
        assert all(statement.lstrip().upper().startswith('SELECT') for statement in statements)
        assert all('FOR UPDATE' not in statement.upper() and 'LOCK TABLE' not in statement.upper() for statement in statements)
        (raw if raw is not None else executor).rollback()
        with engine.connect() as observer:
            assert observer.execute(text('SELECT order_item_id FROM jobs WHERE id=799')).scalar_one() == item_id
    finally:
        (raw if raw is not None else executor).rollback()
        (raw if raw is not None else executor).close()


@pytest.mark.parametrize('graph', [None, ((), (), ((True,None,None,None,None,None),)), ((True,), (2,), ((3,2,1,None,None,None),)), ((), (), tuple((x,None,None,None,None,None) for x in range(1,18))), ((), (), ((2,None,None,None,None,None),(1,None,None,None,None,None)))])
def test_recheck_invalid_graph_refuses_before_provider(graph, monkeypatch):
    from core.order_production import recheck_parent_graph
    from core.registry import registry
    from fastapi import HTTPException
    def forbidden(name):
        pytest.fail('Invalid prepared graph must not reach provider')
    monkeypatch.setattr(registry, 'get_provider', forbidden)
    with pytest.raises(HTTPException) as error:
        recheck_parent_graph(None, graph)
    assert error.value.status_code == 400


def test_recheck_missing_provider_refuses_even_empty_graph(monkeypatch):
    from core.order_production import recheck_parent_graph
    from core.registry import registry
    from fastapi import HTTPException
    monkeypatch.setattr(registry, 'get_provider', lambda name: None)
    with pytest.raises(HTTPException) as error:
        recheck_parent_graph(None, ((), (), ()))
    assert error.value.status_code == 503


@pytest.mark.parametrize('kind', ['session', 'connection'])
def test_recheck_refuses_logical_only_sqlite_transaction_without_begin_immediate(review_db, monkeypatch, kind):
    from sqlalchemy import event
    from core.order_production import recheck_parent_graph
    from core.economic_source_creation import SourceCreationError
    from core.registry import registry
    from modules.orders.production import OrderFulfillmentService
    monkeypatch.setattr(registry, 'get_provider', lambda name: OrderFulfillmentService())
    engine = review_db.get_bind(); statements = []
    def observe(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(engine, 'before_cursor_execute', observe)
    executor = Session(engine) if kind == 'session' else engine.connect()
    try:
        executor.begin()
        connection = executor.connection() if kind == 'session' else executor
        assert not connection.connection.driver_connection.in_transaction
        with pytest.raises(SourceCreationError, match='Caller physical transaction required'):
            recheck_parent_graph(executor, ((), (), ()))
        assert not connection.connection.driver_connection.in_transaction
        assert statements == [], 'Refusal must issue no SQL or writer BEGIN'
    finally:
        executor.rollback(); executor.close()
        event.remove(engine, 'before_cursor_execute', observe)
