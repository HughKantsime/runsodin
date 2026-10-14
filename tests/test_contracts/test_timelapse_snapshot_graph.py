"""Actual journal fixtures; adapter equivalence, complete domains and no reuse."""
import pytest
from contextlib import contextmanager
from core.timelapse_media_lock import acquire_media_guard
from core.timelapse_manifest import _root
from modules.archives.timelapse_snapshot_graph import fence_graph_scope

@contextmanager
def graph_session(edit):
    with acquire_media_guard(edit[2],edit[3]) as guard,_root(edit[2]) as (_,identity),Session(edit[1]) as db:
        scope=fence_graph_scope(db,guard,identity)
        yield db,scope

from sqlalchemy import select,func,text,event
from sqlalchemy.orm import Session
from tests.test_contracts.test_timelapse_edit_publication import edit,run,prepared_source,encoder,frame_writer,review_db,owned_runtime
from core.timelapse_recipes import make_recipe
from core.timelapse_publication_schema import publications,facts
from core.timelapse_publication_validation import operation
from modules.archives.timelapse_snapshot_graph import SnapshotGraphReadView

def setup_graph(edit):
    result=run(edit,recipe=make_recipe('speed',multiplier=2))
    run(edit,identifier='b'*32,recipe=make_recipe('trim',start_seconds=0,end_seconds=.09))
    return result

def test_frozen_operation_equivalence_all_kinds_and_fresh_transaction(edit):
    setup_graph(edit);engine=edit[1]
    with graph_session(edit) as (db,scope):
        ops=[dict(r) for r in db.execute(select(publications)).mappings()]
        assert {r['kind'] for r in ops}=={'frame','encode','trim','speed'}
        view=SnapshotGraphReadView(db,ops,scope=scope,lifetime=ops[0]['source_lifetime'])
        try:
            for op in ops:operation(db,op);operation(view,op)
            with pytest.raises(Exception):view.execute(select(facts))
            base=select(func.count()).select_from(publications).where(publications.c.result_nonce==ops[-1]['result_nonce'],publications.c.kind!='frame')
            for bad in (base.distinct(),base.group_by(publications.c.actor_id),base.having(func.count()>0),base.limit(1),base.prefix_with('DISTINCT'),select(func.count(publications.c.actor_id)).where(publications.c.result_nonce==ops[-1]['result_nonce'],publications.c.kind!='frame')):
                with pytest.raises(Exception):view.execute(bad)
            with pytest.raises(Exception):view.execute(select(func.count()).select_from(publications).where(publications.c.result_nonce=='unknown',publications.c.kind!='frame'))
            db.rollback()
            with pytest.raises(Exception):operation(view,ops[0])
        finally:view.close()
        with pytest.raises(Exception):view.execute(select(publications))

def test_pretransfer_byte_allowance_refuses_before_full_rows(edit,monkeypatch):
    setup_graph(edit);engine=edit[1]
    import modules.archives.timelapse_snapshot_graph as graph
    with graph_session(edit) as (db,scope):
        ops=[dict(r) for r in db.execute(select(publications)).mappings()]
        queries=[]
        def record(c,cur,statement,*args):queries.append(statement)
        event.listen(engine,'before_cursor_execute',record)
        try:
            monkeypatch.setattr(graph,'MAX_BYTES',1)
            with pytest.raises(Exception):SnapshotGraphReadView(db,ops,scope=scope,lifetime=ops[0]['source_lifetime'])
            assert len(queries)==1 and 'count(' in queries[0] and 'sum(' in queries[0]
        finally:event.remove(engine,'before_cursor_execute',record)

def test_initial_speed_count_includes_unrelated_malformed_publication(edit):
    result=setup_graph(edit);engine=edit[1]
    # Owned database corruption fixture: insert a deliberately invalid relation
    # outside the target lifetime predicate, retaining the same result nonce.
    with engine.connect() as c:
        c.exec_driver_sql('PRAGMA foreign_keys=OFF')
        triggers=c.execute(text("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='timelapse_publications'")).scalars().all()
        for name in triggers:c.exec_driver_sql('DROP TRIGGER "'+name.replace('"','""')+'"')
        original=dict(c.execute(select(publications).where(publications.c.operation_id==result['operation_id'])).mappings().one())
        forged=dict(original,operation_id='c'*32,source_lifetime='d'*32,private_directory='publish_'+'c'*32,destination_path='unrelated.mp4')
        # Storage constraints permit this shape; FK drift is intentionally owned.
        c.execute(publications.insert().values(**forged));c.commit();c.exec_driver_sql('PRAGMA foreign_keys=ON')
    with graph_session(edit) as (db,scope):
        ops=[dict(r) for r in db.execute(select(publications).where((publications.c.source_lifetime==original['source_lifetime'])|(publications.c.result_lifetime==original['source_lifetime']))).mappings()]
        assert forged['operation_id'] not in {r['operation_id'] for r in ops}
        view=SnapshotGraphReadView(db,ops,scope=scope,lifetime=ops[0]['source_lifetime'])
        try:
            assert view.execute(select(func.count()).select_from(publications).where(publications.c.result_nonce==original['result_nonce'],publications.c.kind!='frame')).scalar_one()==2
            with pytest.raises(Exception):operation(db,original)
            with pytest.raises(Exception):operation(view,original)
        finally:view.close()

def test_next_snapshot_reloads_changed_fact_and_rejects_closed_prior_view(edit):
    result=setup_graph(edit);engine=edit[1]
    with graph_session(edit) as (db,scope):
        ops=[dict(r) for r in db.execute(select(publications)).mappings()]
        old=SnapshotGraphReadView(db,ops,scope=scope,lifetime=ops[0]['source_lifetime'])
        for op in ops:operation(old,op)
        old.close()
    # Explicit owned retained-journal drift fixture, not a sanctioned writer.
    with engine.begin() as c:
        for name in c.execute(text("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='timelapse_publication_facts'")).scalars():c.exec_driver_sql('DROP TRIGGER "'+name.replace('"','""')+'"')
        c.execute(facts.update().where(facts.c.operation_id==result['operation_id'],facts.c.role=='source').values(digest_sha256='0'*64))
    from modules.archives.timelapse_speed_dependencies import physical_partition
    with graph_session(edit) as (db,scope):
        with pytest.raises(Exception):physical_partition(db,ops[0]['source_lifetime'],ops,scope=scope)
        with pytest.raises(Exception):operation(old,ops[0])


def test_guard_and_fence_scope_are_mandatory_and_live(edit):
    setup_graph(edit)
    from modules.archives.timelapse_retirement import RetirementUnavailable
    with graph_session(edit) as (db,scope):
        ops=[dict(r) for r in db.execute(select(publications)).mappings()]
        with pytest.raises(RetirementUnavailable):SnapshotGraphReadView(db,ops,scope=None,lifetime=ops[0]['source_lifetime'])
        view=SnapshotGraphReadView(db,ops,scope=scope,lifetime=ops[0]['source_lifetime'])
        with Session(edit[1]) as other:
            with pytest.raises(RetirementUnavailable):SnapshotGraphReadView(other,ops,scope=scope,lifetime=ops[0]['source_lifetime'])
        scope.guard.close()
        with pytest.raises(Exception):operation(view,ops[0])
        view.close()


def test_nullable_pretransfer_accounting_matches_retained_allowance(edit,monkeypatch):
    setup_graph(edit)
    import modules.archives.timelapse_snapshot_graph as graph
    with graph_session(edit) as (db,scope):
        ops=[dict(r) for r in db.execute(select(publications)).mappings()]
        assert any(v is None for r in ops for v in r.values())
        budget=sum(len(v.encode('utf8'))+16 if isinstance(v,str) else len(v)+16 if isinstance(v,bytes) else 64 for r in ops for v in r.values())-1
        monkeypatch.setattr(graph,'MAX_BYTES',budget)
        queries=[]
        def record(c,cur,statement,*args):queries.append(statement)
        event.listen(edit[1],'before_cursor_execute',record)
        try:
            with pytest.raises(Exception):SnapshotGraphReadView(db,ops,scope=scope,lifetime=ops[0]['source_lifetime'])
            assert len(queries)==1 and 'count(' in queries[0] and 'CASE WHEN' in queries[0]
        finally:event.remove(edit[1],'before_cursor_execute',record)

def test_immutable_templates_reused_with_fresh_rows_and_index_cleanup(edit):
    setup_graph(edit)
    import modules.archives.timelapse_snapshot_graph as graph
    templates=dict(graph.TEMPLATES)
    assert all(pair[0].compile().params.get('keys') is None and pair[1].compile().params.get('keys') is None for pair in templates.values())
    previous=None
    for _ in range(2):
        with graph_session(edit) as (db,scope):
            ops=[dict(r) for r in db.execute(select(publications)).mappings()]
            view=SnapshotGraphReadView(db,ops,scope=scope,lifetime=ops[0]['source_lifetime'])
            row=view.execute(select(publications).where(publications.c.operation_id==ops[0]['operation_id'])).mappings().first()
            if previous is not None:assert row is not previous and dict(row)==dict(previous)
            previous=row
            assert view.indices
            view.close()
            assert view.rows==view.indices==view.domains=={}
    assert all(graph.TEMPLATES[key][0] is pair[0] and graph.TEMPLATES[key][1] is pair[1] for key,pair in templates.items())

def test_structural_decoder_refuses_foreign_from_and_projection_modifiers(edit):
    setup_graph(edit)
    from sqlalchemy import Table,MetaData,Column,String
    from modules.archives.timelapse_retirement import RetirementUnavailable
    foreign=Table(publications.name,MetaData(),Column('operation_id',String))
    with graph_session(edit) as (db,scope):
        ops=[dict(r) for r in db.execute(select(publications)).mappings()]
        view=SnapshotGraphReadView(db,ops,scope=scope,lifetime=ops[0]['source_lifetime'])
        base=select(publications).where(publications.c.operation_id==ops[0]['operation_id'])
        try:
            for bad in (select(foreign).where(foreign.c.operation_id==ops[0]['operation_id']),base.select_from(foreign),base.where(foreign.c.operation_id==ops[0]['operation_id']),base.correlate(publications),base.fetch(1),base.order_by(publications.c.actor_id),base.with_for_update(skip_locked=True),select(func.count(publications.c.actor_id)).where(publications.c.result_nonce==ops[-1]['result_nonce'],publications.c.kind!='frame')):
                with pytest.raises(RetirementUnavailable):view.execute(bad)
        finally:view.close()


def test_duplicate_role_in_matches_live_sql_and_only_exact_limits(edit):
    setup_graph(edit)
    from modules.archives.timelapse_retirement import RetirementUnavailable
    from core.timelapse_manifest import MAX_OBJECTS
    with graph_session(edit) as (db,scope):
        ops=[dict(r) for r in db.execute(select(publications)).mappings()]
        view=SnapshotGraphReadView(db,ops,scope=scope,lifetime=ops[0]['source_lifetime'])
        try:
            duplicate=select(facts).where(facts.c.operation_id==ops[0]['operation_id'],facts.c.role.in_(('root','root'))).order_by(*facts.primary_key.columns)
            actual=[dict(r) for r in db.execute(duplicate).mappings()]
            assert len(actual)==1 and [dict(r) for r in view.execute(duplicate).mappings()]==actual
            base=select(facts).where(facts.c.operation_id==ops[0]['operation_id'])
            assert [dict(r) for r in view.execute(base.limit(MAX_OBJECTS+1)).mappings()]
            prior=next(r for r in ops if r['kind']=='speed')
            publication=select(publications).where(publications.c.result_nonce==prior['source_nonce'],publications.c.kind!='frame')
            assert [dict(r) for r in view.execute(publication.limit(2)).mappings()]
            for bad in (base.limit(1),base.limit(MAX_OBJECTS),duplicate.limit(MAX_OBJECTS+1),publication.limit(1),publication.limit(3),select(publications).where(publications.c.operation_id==ops[0]['operation_id']).limit(2)):
                with pytest.raises(RetirementUnavailable):view.execute(bad)
        finally:view.close()
