"""Closed parser and owned SQLite staging/replay phases, not PG COPY proof."""
import io,json
from time import monotonic
from uuid import uuid4
import pytest
from sqlalchemy import text
from core import priced_estimate_restore as r,priced_estimate_schema as s,priced_estimate_validation as v
from core.schema.migrator import MigrationError
from tests.test_contracts.test_priced_estimate_storage import db,source,select,receipt,reference,insert,update,NOW

TOKEN=b'A'*63
TOC=tuple(r.TableDataEntry(i+1,table) for i,table in enumerate(s.TABLES))


def dump(rows=None):
    rows=rows or {};parts=[b'--\n-- PostgreSQL database dump\n--\n\n',b'\\restrict '+TOKEN+b'\n\n',b'-- Dumped from database version 16.15\n-- Dumped by pg_dump version 16.15\n\n',*r.ENVELOPE,b'\n']
    for table in reversed(s.TABLES):
        parts.extend([('--\n-- Data for Name: '+table+'; Type: TABLE DATA; Schema: public; Owner: -\n--\n\n').encode(),('COPY public.'+table+' ('+', '.join(r.columns(table))+') FROM stdin;\n').encode()])
        parts.extend(rows.get(table,()));parts.append(b'\\.\n\n\n')
    parts.extend([b'--\n-- PostgreSQL database dump complete\n--\n\n',b'\\unrestrict '+TOKEN+b'\n\n']);return b''.join(parts)


def budget(payload):return r.RestoreBudget(len(payload),monotonic()+30)
def parse(payload,**kw):return list(r.extract_copy(io.BytesIO(payload),table_data=TOC,budget=budget(payload),**kw))


def test_exact_seven_empty_blocks_and_qualified_profile():
    events=parse(dump())
    assert [event.table for event in events if isinstance(event,r.CopyStart)]==list(reversed(s.TABLES))
    assert len(events)==14

@pytest.mark.parametrize('replacement',[b'SELECT 1;\n',b'SET statement_timeout = 1;\n',b'SET statement_timeout = 0; DROP TABLE public.users;\n',b'-- arbitrary lookalike\n'])
def test_executable_or_unknown_envelope_refused(replacement):
    payload=dump().replace(r.ENVELOPE[0],replacement)
    with pytest.raises(r.PricedRestoreError):parse(payload)

@pytest.mark.parametrize('mutation',[lambda p:p.replace(b'public.priced_source_revisions (source_uid,',b'public.users (source_uid,'),lambda p:p.replace(b'FROM stdin;',b'FROM stdin WITH (FORMAT binary);',1),lambda p:p.replace(b'source_uid, revision',b'revision, source_uid'),lambda p:p.replace(b'-- Dumped by pg_dump version 16.15',b'-- Dumped by pg_dump version 18.0'),lambda p:p.replace(b'\\unrestrict '+TOKEN,b'\\unrestrict '+b'B'*63),lambda p:p[:-1],lambda p:p+b'SELECT pg_sleep(1);\n',lambda p:p.replace(b'\\.\n',b'',1)])
def test_bad_header_profile_pair_truncation_and_extra_sql(mutation):
    with pytest.raises(r.PricedRestoreError):parse(mutation(dump()))


def test_duplicate_or_incomplete_toc_refused():
    payload=dump()
    for entries in (TOC[:-1],TOC[:-1]+(TOC[0],),tuple(r.TableDataEntry(1,entry.table) for entry in TOC)):
        with pytest.raises(r.PricedRestoreError):list(r.extract_copy(io.BytesIO(payload),table_data=entries,budget=budget(payload)))


def test_raw_copy_escapes_null_and_utf8_preserved():
    fields=[b'\\N']*len(r.columns(s.SOURCE));fields[0]=b'utf8\xc3\xa9\\t\\n\\r\\b\\f\\v\\\\\\141\\x62'
    raw=b'\t'.join(fields)+b'\n';events=parse(dump({s.SOURCE:[raw]}))
    assert next(event.raw for event in events if isinstance(event,r.CopyRecord))==raw

@pytest.mark.parametrize('bad',[b'\\q',b'\\',b'\\x',b'\\400',b'\\000',b'\xff',b'raw\r'])
def test_malformed_record_encoding_escape_and_nul_refused(bad):
    fields=[b'\\N']*len(r.columns(s.SOURCE));fields[0]=bad
    with pytest.raises(r.PricedRestoreError):parse(dump({s.SOURCE:[b'\t'.join(fields)+b'\n']}))


def test_record_field_count_and_expansion_bound():
    with pytest.raises(r.PricedRestoreError):parse(dump({s.SOURCE:[b'one\n']}))
    fields=[b'\\N']*len(r.columns(s.SOURCE));fields[0]=b'a'*(r.MAX_DECODED_RECORD+1)
    with pytest.raises(r.PricedRestoreError):parse(dump({s.SOURCE:[b'\t'.join(fields)+b'\n']}))
    fields[0]=b'\\141'*(r.MAX_DECODED_RECORD+1)
    with pytest.raises(r.PricedRestoreError):parse(dump({s.SOURCE:[b'\t'.join(fields)+b'\n']}))


def test_inherited_bytes_and_deadline_fail_closed():
    payload=dump()
    for allowance,deadline in ((len(payload)-1,monotonic()+30),(len(payload),monotonic()-1)):
        with pytest.raises(r.PricedRestoreError):list(r.extract_copy(io.BytesIO(payload),table_data=TOC,budget=r.RestoreBudget(allowance,deadline)))


def _saved_graph(db):
    job=str(uuid4());insert(db,'cost_identities',dict(identity_uid=job,resource_kind='job',resource_id=1,owner_scope='org',owner_key=1));db.commit();db.exec_driver_sql('BEGIN')
    src=source(db);h=select(db,src);one=receipt(db,src,h,job=job,register=False);reference(db,one)
    newer=source(db,uid=src['source_uid'],revision=2);h2=select(db,newer,revision=2);two=receipt(db,newer,h2,job=job,revision=2,register=False);reference(db,two,2);reference(db,two,3,True)
    cleared={**h2,'revision':3,'expected_revision':2,'command_nonce':uuid4().hex}
    for name in s.SOURCE_LINK:cleared[name]=None
    cleared['request_sha256']=v.digest(v.selection_command(cleared));insert(db,s.SELECTION_HISTORY,cleared);update(db,s.SELECTION,{'revision':3},'owner_scope=:scope AND owner_key=:key',{'scope':'org','key':1});v.validate(db)
    saved={table:[dict(row) for row in db.execute(text('SELECT * FROM '+table)).mappings()] for table in s.TABLES}
    db.rollback();db.exec_driver_sql('BEGIN');return saved


def _stage_rows(db,saved):
    stage=r._Stage(r._Lease(db,r.RestoreBudget(10**7,monotonic()+30)))
    for table,rows in saved.items():
        for row in reversed(rows):insert(db,stage.names[table],row)
    return stage


def test_unordered_nonempty_histories_replay_and_null_current_exact(db):
    saved=_saved_graph(db);stage=_stage_rows(db,saved);r._validate_stage(stage);r._replay(stage)
    for table in s.TABLES:
        actual=[dict(row) for row in db.execute(text('SELECT * FROM '+table)).mappings()]
        assert sorted(actual,key=str)==sorted(saved[table],key=str)
    assert db.execute(text('SELECT receipt_uid FROM '+s.CURRENT)).scalar_one() is None
    stage.drop();v.validate(db);db.commit()
    assert db.execute(text("SELECT count(*) FROM sqlite_temp_schema WHERE name LIKE 'pe_stage_%'")).scalar_one()==0

@pytest.mark.parametrize('corruption',['missing_component','hash','selection_revision','current_snapshot'])
def test_staged_drift_refuses_before_any_public_replay(db,corruption):
    saved=_saved_graph(db)
    if corruption=='missing_component':saved[s.COMPONENT].pop()
    elif corruption=='hash':saved[s.SOURCE][0]['source_sha256']='a'*64
    elif corruption=='selection_revision':saved[s.SELECTION][0]['revision']=2
    else:saved[s.CURRENT][0]['revision']=2
    stage=_stage_rows(db,saved)
    with pytest.raises((r.PricedRestoreError,MigrationError)):r._validate_stage(stage)
    assert all(db.execute(text('SELECT count(*) FROM '+table)).scalar_one()==0 for table in s.TABLES)


def test_snapshot_refuses_unrecognized_query_projection_params(db):
    stage=_stage_rows(db,_saved_graph(db));snapshot=r._Snapshot(stage)
    for statement,params in ((text('SELECT * FROM users'),{}),(text('SELECT * FROM '+s.SOURCE+' WHERE source_uid=:uid AND revision=:revision'),{'uid':'x','revision':1,'extra':0})):
        with pytest.raises(r.PricedRestoreError):snapshot.execute(statement,params)


def test_original_lease_expires_on_commit_no_reconnect(db):
    saved=_saved_graph(db);stage=_stage_rows(db,saved);db.commit()
    with pytest.raises(r.PricedRestoreError):r._validate_stage(stage)


def test_replay_failure_whole_caller_rollback(db,monkeypatch):
    saved=_saved_graph(db);stage=_stage_rows(db,saved);r._validate_stage(stage);original=r._cas;calls=0
    def fail(stage,table,row,keys):
        nonlocal calls
        calls+=1
        if calls==2:raise RuntimeError('Owned phase fault')
        return original(stage,table,row,keys)
    monkeypatch.setattr(r,'_cas',fail)
    with pytest.raises(RuntimeError):r._replay(stage)
    db.rollback();db.exec_driver_sql('BEGIN')
    assert all(db.execute(text('SELECT count(*) FROM '+table)).scalar_one()==0 for table in s.TABLES)
    assert db.execute(text("SELECT count(*) FROM sqlite_temp_schema WHERE name LIKE 'pe_stage_%'")).scalar_one()==0

@pytest.mark.parametrize('table',(s.CURRENT,s.RECEIPT,s.SOURCE))
def test_malformed_copy_first_middle_last_never_completes_extraction(table):
    payload=dump({table:[b'wrong-field-count\n']})
    with pytest.raises(r.PricedRestoreError):parse(payload)

@pytest.mark.parametrize('table',(s.SOURCE,s.RECEIPT,s.REFERENCE_HISTORY))
def test_first_middle_last_replay_fault_preserves_all_public_rollback(db,monkeypatch,table):
    saved=_saved_graph(db);stage=_stage_rows(db,saved);r._validate_stage(stage);original=r._insert
    def fail(stage,target,row):
        original(stage,target,row)
        if target==table:raise RuntimeError('Owned post-write phase fault')
    monkeypatch.setattr(r,'_insert',fail)
    with pytest.raises(RuntimeError):r._replay(stage)
    db.rollback();db.exec_driver_sql('BEGIN')
    assert all(db.execute(text('SELECT count(*) FROM '+target)).scalar_one()==0 for target in s.TABLES)
    assert db.execute(text('SELECT count(*) FROM cost_identities')).scalar_one()==1


def test_parser_budget_not_reset_between_records_or_tables():
    payload=dump();shared=budget(payload)
    assert len(list(r.extract_copy(io.BytesIO(payload),table_data=TOC,budget=shared)))==14
    assert shared.remaining_bytes==0
    with pytest.raises(r.PricedRestoreError):list(r.extract_copy(io.BytesIO(payload),table_data=TOC,budget=shared))


def test_stage_identity_replacement_and_expired_deadline_refuse(db):
    saved=_saved_graph(db);stage=_stage_rows(db,saved)
    stage.query('DROP TABLE '+stage.name(s.SOURCE))
    with pytest.raises(r.PricedRestoreError):r._validate_stage(stage)
    stage.lease.budget.deadline=monotonic()-1
    with pytest.raises(r.PricedRestoreError):stage.lease.check()


def test_public_transport_refuses_sqlite_no_mutation(db):
    with pytest.raises(r.PricedRestoreError):r.restore_priced_tables(db,io.BytesIO(dump()),table_data=TOC,budget=budget(dump()))
    assert db.execute(text("SELECT count(*) FROM sqlite_temp_schema WHERE name LIKE 'pe_stage_%'")).scalar_one()==0


def test_final_public_validation_streams_fresh_data_and_cannot_write(db):
    saved=_saved_graph(db);stage=_stage_rows(db,saved);r._validate_stage(stage);r._replay(stage);lease=stage.lease;stage.drop()
    public=r._Stage.public_view(lease);r._validate_stage(public)
    with pytest.raises(r.PricedRestoreError):public.query('DELETE FROM '+s.SOURCE)
    with pytest.raises(r.PricedRestoreError):public.drop()
    with pytest.raises(r.PricedRestoreError):public.query('SELECT 1; DELETE FROM '+s.SOURCE)
    with pytest.raises(r.PricedRestoreError):list(public.rows(s.SOURCE,'source_uid; DELETE FROM '+s.SOURCE))
    db.exec_driver_sql('DROP TRIGGER '+s.COMPONENT+'_update_guard')
    update(db,s.COMPONENT,{'numerator':'7'},"component_name='total'",{})
    with pytest.raises(MigrationError):r._validate_stage(public)


def test_insert_and_cas_returned_rows_use_exact_types():
    assert r._same_row({'revision':1,'receipt_uid':None},{'receipt_uid':None,'revision':1})
    assert not r._same_row({'revision':True},{'revision':1})
    assert not r._same_row({'revision':1,'extra':None},{'revision':1})


def test_staged4096_history_bound_and_unordered_domain_validation(db):
    row=source(db);db.rollback();db.exec_driver_sql('BEGIN')
    stage=r._Stage(r._Lease(db,r.RestoreBudget(10**7,monotonic()+30)))
    for revision in range(4096,0,-1):insert(db,stage.names[s.SOURCE],{**row,'revision':revision})
    r._validate_stage(stage)
    assert sum(1 for _ in stage.rows(s.SOURCE,'source_uid,revision'))==4096
    stage.drop()


def test_actual_qualified_f4_producer_build_header_and_no_general_suffix():
    payload=dump().replace(b'-- Dumped by pg_dump version 16.15\n',b'-- Dumped by pg_dump version 16.15 (Debian 16.15-1.pgdg13+2)\n')
    assert len(parse(payload))==14
    with pytest.raises(r.PricedRestoreError):parse(payload.replace(b'pgdg13+2',b'unknown-build'))
