"""Canonical historical fact exception; no historical filesystem access."""
import pytest
from sqlalchemy import select,text
from sqlalchemy.orm import Session
from core.timelapse_publication_schema import publications,facts
from core.timelapse_media_lock import acquire_media_guard
from core.timelapse_manifest import _root
from core.timelapse_request_budget import VerificationBudget
from modules.archives.timelapse_snapshot_graph import fence_graph_scope,MAX_ROWS,MAX_BYTES
from modules.archives.timelapse_retired_fact_proof import retired_fact_exemptions
from modules.archives.timelapse_lifetime_action import retire_sealed_lifetime
from modules.archives.timelapse_retirement import RetirementUnavailable
from tests.test_contracts.test_timelapse_lifetime_action import credential
from tests.test_contracts.test_timelapse_lifetime_membership import prepared_source,prepare,encoder,frame_writer,review_db,owned_runtime


def proof_inputs(db):
    ops=[dict(row) for row in db.execute(select(publications)).mappings()]
    rows=[dict(row) for row in db.execute(select(facts).where(facts.c.role.in_(('output','private_root','private_creation')))).mappings()]
    return rows,ops


def test_exact_completed_members_exempt_only_their_fact_keys(prepared_source):
    engine,root,tid,_,nonce=prepared_source;prepare(prepared_source)
    assert retire_sealed_lifetime(engine,root,tid,credential(engine),expected_nonce=nonce).state=='complete'
    with acquire_media_guard(root,tid) as guard,_root(root) as (_,identity),Session(engine) as db:
        scope=fence_graph_scope(db,guard,identity,retirement_proofs=True)
        rows,ops=proof_inputs(db)
        assert rows
        proved=retired_fact_exemptions(db,rows,ops,scope=scope,budget=VerificationBudget())
        assert proved==frozenset((row['operation_id'],row['role'],row['relative_path']) for row in rows)
        assert {row['role'] for row in rows}=={'output','private_creation','private_root'}


@pytest.mark.parametrize('failure',['pending','false_completion','false_static','wrong_scope','no_budget','expired_budget','wrong_root','fact_changed','incomplete_operation','ambiguous_operation','row_limit','byte_limit','live_reference'])
def test_retired_fact_proof_failures_never_exempt(prepared_source,monkeypatch,failure):
    from modules.archives import timelapse_retired_fact_proof as proof
    engine,root,tid,_,nonce=prepared_source;prepare(prepared_source)
    if failure!='pending':assert retire_sealed_lifetime(engine,root,tid,credential(engine),expected_nonce=nonce).state=='complete'
    with acquire_media_guard(root,tid) as guard,_root(root) as (_,identity),Session(engine) as db:
        scope=fence_graph_scope(db,guard,identity,retirement_proofs=failure!='wrong_scope')
        rows,ops=proof_inputs(db);budget=VerificationBudget()
        if failure=='false_completion':monkeypatch.setattr(proof,'completion_proof',lambda *args:'0=1')
        elif failure=='false_static':monkeypatch.setattr(proof,'static_binding_proof',lambda *args:'0=1')
        elif failure=='no_budget':budget=None
        elif failure=='expired_budget':budget.deadline=0
        elif failure=='wrong_root':
            # Real guard/root stays intact; corrupt only the old lifetime read view.
            original=proof.SnapshotGraphReadView
            def view(*args,**kwargs):
                result=original(*args,**kwargs)
                from core.timelapse_publication_schema import lifetimes
                from types import MappingProxyType
                key=next(iter(result.rows[lifetimes.name]));row=dict(result.rows[lifetimes.name][key]);row['root_inode']+=1
                result.rows[lifetimes.name][key]=MappingProxyType(row)
                return result
            monkeypatch.setattr(proof,'SnapshotGraphReadView',view)
        elif failure=='fact_changed':rows[0]['inode']+=1
        elif failure=='incomplete_operation':ops[0]['state']='active'
        elif failure=='ambiguous_operation':ops.append(dict(ops[0]))
        elif failure=='row_limit':monkeypatch.setattr(proof,'MAX_ROWS',0)
        elif failure=='byte_limit':monkeypatch.setattr(proof,'MAX_BYTES',0)
        elif failure=='live_reference':
            # Persisting this forbidden source resurrection is itself guarded;
            # observe the live-reference predicate without fabricating a lifetime.
            execute=db.execute
            class Found:
                def first(self):return (1,)
            def observed(statement,*args,**kwargs):
                if str(statement).startswith('SELECT 1 FROM timelapses t WHERE t.retirement_operation_id'):return Found()
                return execute(statement,*args,**kwargs)
            monkeypatch.setattr(db,'execute',observed)
        if failure=='wrong_root':
            from core.schema.migrator import MigrationError
            with pytest.raises(MigrationError,match='Publication retained authority differs'):retired_fact_exemptions(db,rows,ops,scope=scope,budget=budget)
        else:
            with pytest.raises(RetirementUnavailable):retired_fact_exemptions(db,rows,ops,scope=scope,budget=budget)


def test_retirement_fence_flag_requires_exact_boolean(prepared_source):
    engine,root,tid,_,_=prepared_source
    with acquire_media_guard(root,tid) as guard,_root(root) as (_,identity),Session(engine) as db:
        with pytest.raises(RetirementUnavailable):fence_graph_scope(db,guard,identity,retirement_proofs=1)

@pytest.mark.parametrize('forbidden',['private_root','private_creation','output','active','private_created','non_frame','counter','duplicate_slot','namespace','member'])
def test_never_created_gap_rejects_every_forbidden_shape(prepared_source,monkeypatch,forbidden):
    from modules.archives import timelapse_retired_fact_proof as proof
    from core.timelapse_retirement_schema import objects
    from core.timelapse_retirement_membership_schema import members
    from core.timelapse_publication_schema import namespaces,bindings as frame_bindings
    from modules.archives.timelapse_snapshot_graph import SnapshotGraphReadView
    from types import MappingProxyType
    engine,root,tid,_,nonce=prepared_source;prepare(prepared_source)
    assert retire_sealed_lifetime(engine,root,tid,credential(engine),expected_nonce=nonce).state=='complete'
    with acquire_media_guard(root,tid) as guard,_root(root) as (_,identity),Session(engine) as db:
        scope=fence_graph_scope(db,guard,identity,retirement_proofs=True)
        _,ops=proof_inputs(db);gap=next(dict(op) for op in ops if op['phase']=='admitted' and op['state']=='blocked')
        lifetime=gap['source_lifetime'];view=SnapshotGraphReadView(db,ops,scope=scope,lifetime=lifetime)
        try:
            old_facts=[dict(row) for row in view.rows[facts.name].values()]
            old_objects=[dict(row) for row in db.execute(select(objects)).mappings()]
            old_members=[dict(row) for row in db.execute(select(members)).mappings()]
            budget=VerificationBudget()
            assert proof._never_created_gap_proves(db,gap,lifetime,view,old_facts,old_objects,old_members,budget)
            if forbidden in ('private_root','private_creation','output'):old_facts.append({'operation_id':gap['operation_id'],'role':forbidden})
            elif forbidden=='active':gap['state']='active'
            elif forbidden=='private_created':gap['phase']='private_created'
            elif forbidden=='non_frame':gap['kind']='encode'
            elif forbidden=='counter':gap['frame_number']=view.rows[frame_bindings.name][(lifetime,)]['next_number']
            elif forbidden=='duplicate_slot':
                execute=db.execute
                class Count:
                    def scalar_one(self):return 2
                def counted(statement,*args,**kwargs):
                    if str(statement).startswith('SELECT count(*)') and 'frame_number' in str(statement):return Count()
                    return execute(statement,*args,**kwargs)
                monkeypatch.setattr(db,'execute',counted)
            elif forbidden=='namespace':
                key=(gap['frame_namespace_id'],);row=dict(view.rows[namespaces.name][key]);row['phase']='bound';view.rows[namespaces.name][key]=MappingProxyType(row)
            elif forbidden=='member':old_members=[row for row in old_members if row['role']!='frame_namespace']
            assert not proof._never_created_gap_proves(db,gap,lifetime,view,old_facts,old_objects,old_members,budget)
        finally:view.close()


def test_trim_retained_original_output_has_exact_retired_mapping(edit):
    from tests.test_contracts.test_timelapse_edit_publication import run
    from core.timelapse_recipes import make_recipe
    from modules.archives.timelapse_lifetime_prepare import prepare_request_lifetime_retirement
    service,engine,root,tid,nonce,_=edit
    result=run(edit,recipe=make_recipe('trim',start_seconds=0,end_seconds=.09))
    authority=credential(engine)
    prepare_request_lifetime_retirement(engine,root,tid,authority,expected_nonce=result['result_nonce'],budget=VerificationBudget())
    assert retire_sealed_lifetime(engine,root,tid,authority,expected_nonce=result['result_nonce']).state=='complete'
    with acquire_media_guard(root,tid) as guard,_root(root) as (_,identity),Session(engine) as db:
        scope=fence_graph_scope(db,guard,identity,retirement_proofs=True)
        rows,ops=proof_inputs(db)
        assert any(op['kind']=='trim' for op in ops)
        assert retired_fact_exemptions(db,rows,ops,scope=scope,budget=VerificationBudget())==frozenset((row['operation_id'],row['role'],row['relative_path']) for row in rows)

from tests.test_contracts.test_timelapse_edit_publication import edit
