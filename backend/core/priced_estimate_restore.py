"""Closed priced-table COPY extraction and caller-owned trusted staging/replay.

Unregistered internal transport only. Never executes an archive SQL statement,
commits, creates an engine, supplies account authority or changes live prices.
"""
from dataclasses import dataclass
from time import monotonic
import re
from sqlalchemy import text
from core import priced_estimate_schema as schema,priced_estimate_validation as validation

MAX_RECORD=4*(65536+4096)
MAX_DECODED_RECORD=65536+4096
PROFILE='postgresql-16.15-restricted-text'
ENVELOPE=(b'SET statement_timeout = 0;\n',b'SET lock_timeout = 0;\n',b'SET idle_in_transaction_session_timeout = 0;\n',b"SET client_encoding = 'UTF8';\n",b'SET standard_conforming_strings = on;\n',b"SELECT pg_catalog.set_config('search_path', '', false);\n",b'SET check_function_bodies = false;\n',b'SET xmloption = content;\n',b'SET client_min_messages = warning;\n',b'SET row_security = off;\n')

class PricedRestoreError(ValueError):pass

def _refuse():raise PricedRestoreError('Priced restore transport refused') from None

@dataclass
class RestoreBudget:
    """Caller-supplied remaining expansion/deadline budget, not an authority."""
    remaining_bytes: int
    deadline: float
    def check(self):
        if type(self.remaining_bytes) is not int or self.remaining_bytes<0 or type(self.deadline) not in (int,float) or not 0<self.deadline<float('inf') or monotonic()>=self.deadline:_refuse()
    def charge(self,count):
        self.check()
        if type(count) is not int or count<0 or count>self.remaining_bytes:_refuse()
        self.remaining_bytes-=count

@dataclass(frozen=True)
class TableDataEntry:
    dump_id: int
    table: str

@dataclass(frozen=True)
class CopyStart:
    table: str

@dataclass(frozen=True)
class CopyRecord:
    table: str
    raw: bytes

@dataclass(frozen=True)
class CopyEnd:
    table: str


def columns(table):
    if table not in schema.TABLES:_refuse()
    return tuple(col.split(' ',1)[0] for col in schema.definitions(True)[table][0])


def _record(line,count):
    if not line.endswith(b'\n') or b'\r' in line or b'\x00' in line or len(line)>MAX_RECORD:_refuse()
    fields=line[:-1].split(b'\t')
    if len(fields)!=count:_refuse()
    decoded_total=0
    for field in fields:
        if field==b'\\N':continue
        index=0;output=bytearray()
        while index<len(field):
            byte=field[index];index+=1
            if byte!=92:output.append(byte);continue
            if index>=len(field):_refuse()
            byte=field[index];index+=1
            escaped={92:92,98:8,102:12,110:10,114:13,116:9,118:11}
            if byte in escaped:output.append(escaped[byte])
            elif 48<=byte<=55:
                digits=bytes([byte])
                while index<len(field) and len(digits)<3 and 48<=field[index]<=55:digits+=bytes([field[index]]);index+=1
                number=int(digits,8)
                if number>255:_refuse()
                output.append(number)
            elif byte==120:
                digits=b''
                while index<len(field) and len(digits)<2 and chr(field[index]) in '0123456789abcdefABCDEF':digits+=bytes([field[index]]);index+=1
                if not digits:_refuse()
                output.append(int(digits,16))
            else:_refuse()
        if 0 in output:_refuse()
        try:output.decode('utf8','strict')
        except UnicodeError:_refuse()
        decoded_total+=len(output)
    if decoded_total>MAX_DECODED_RECORD:_refuse()
    return line


def extract_copy(stream,*,table_data,budget,profile=PROFILE):
    """Yield exact raw records from an explicitly qualified tool envelope.

    TOC entries must already belong to the separately validated archive profile.
    This closed extractor checks their exact seven-table membership only; it is
    not an archive/schema/credential authorization capability.
    """
    if type(budget) is not RestoreBudget or profile!=PROFILE or type(table_data) is not tuple or len(table_data)!=7:_refuse()
    if any(type(entry) is not TableDataEntry or type(entry.dump_id) is not int or entry.dump_id<=0 or entry.table not in schema.TABLES for entry in table_data):_refuse()
    if len({entry.dump_id for entry in table_data})!=7 or {entry.table for entry in table_data}!=set(schema.TABLES):_refuse()
    def read():
        budget.check();line=stream.readline(min(MAX_RECORD+1,budget.remaining_bytes+1))
        if type(line) is not bytes or len(line)>MAX_RECORD:_refuse()
        budget.charge(len(line))
        if line and not line.endswith(b'\n'):_refuse()
        return line
    def exact(expected):
        if read()!=expected:_refuse()
    for line in (b'--\n',b'-- PostgreSQL database dump\n',b'--\n',b'\n'):exact(line)
    restricted=read();match=re.fullmatch(rb'\\restrict ([A-Za-z0-9]{63})\n',restricted)
    if not match:_refuse()
    token=match[1];exact(b'\n')
    exact(b'-- Dumped from database version 16.15\n')
    if read() not in (b'-- Dumped by pg_dump version 16.15\n',b'-- Dumped by pg_dump version 16.15 (Debian 16.15-1.pgdg13+2)\n'):_refuse()
    exact(b'\n')
    for line in ENVELOPE:exact(line)
    exact(b'\n');seen=set()
    while len(seen)<7:
        exact(b'--\n');comment=read()
        match=re.fullmatch(rb'-- Data for Name: ([a-z_]+); Type: TABLE DATA; Schema: public; Owner: -\n',comment)
        if not match:_refuse()
        table=match[1].decode('ascii')
        if table not in schema.TABLES or table in seen:_refuse()
        exact(b'--\n');exact(b'\n')
        exact(('COPY public.'+table+' ('+', '.join(columns(table))+') FROM stdin;\n').encode())
        seen.add(table);yield CopyStart(table)
        while True:
            line=read()
            if line==b'\\.\n':break
            if not line:_refuse()
            yield CopyRecord(table,_record(line,len(columns(table))))
        yield CopyEnd(table)
        exact(b'\n');exact(b'\n')
    exact(b'--\n');exact(b'-- PostgreSQL database dump complete\n');exact(b'--\n');exact(b'\n');exact(b'\\unrestrict '+token+b'\n');exact(b'\n')
    if read()!=b'':_refuse()


class _Lease:
    """Mechanical original checkout/root binding, never principal authority."""
    def __init__(self,db,budget):
        from sqlalchemy.engine import Connection
        if type(db) is not Connection or type(budget) is not RestoreBudget:_refuse()
        self.db=db;self.budget=budget;self.root=db.get_transaction();self.proxy=db.connection;self.driver=self.proxy.driver_connection
        self.check()
    def check(self,copy=False):
        self.budget.check()
        if self.db.closed or self.db.invalidated or self.root is None or not self.root.is_active or self.db.get_transaction() is not self.root or self.db.connection is not self.proxy or self.proxy.driver_connection is not self.driver:_refuse()
        if self.db.dialect.name=='postgresql':
            if self.driver.autocommit or int(self.driver.info.transaction_status) not in ((1,2) if copy else (2,)):_refuse()
        elif self.db.dialect.name=='sqlite':
            if not self.driver.in_transaction:_refuse()
        else:_refuse()

    def sql_timeout(self):
        """Bound each server statement by remaining caller deadline, preserving
        an existing stricter positive setting; transaction-local only."""
        self.check()
        if self.db.dialect.name!='postgresql':return
        remaining=min(2147483647,int((self.budget.deadline-monotonic())*1000))
        if remaining<1:_refuse()
        previous=int(self.db.exec_driver_sql("SELECT setting FROM pg_settings WHERE name='statement_timeout'").scalar_one())
        if previous==0 or previous>remaining:
            self.db.execute(text("SELECT set_config('statement_timeout',:value,true)"),{'value':str(remaining)+'ms'})
        self.check()


class _Stage:
    def __init__(self,lease):
        from uuid import uuid4
        self.lease=lease;self.db=lease.db;self.pg=self.db.dialect.name=='postgresql';self.public=False
        self.names={table:'pe_stage_'+uuid4().hex[:16]+'_'+table for table in schema.TABLES};self.identities={}
        for table in schema.TABLES:
            lease.check();sql=schema.ddl(table,self.pg,False).replace('CREATE TABLE '+table,'CREATE TEMPORARY TABLE '+self.names[table],1)
            schema.execute_closed(self.db,sql+(' ON COMMIT DROP' if self.pg else ''))
            if self.pg:self.identities[table]=self.db.execute(text('SELECT to_regclass(:name)::oid'),{'name':'pg_temp.'+self.names[table]}).scalar_one()
            else:self.identities[table]=self.db.execute(text("SELECT rootpage FROM sqlite_temp_schema WHERE name=:name"),{'name':self.names[table]}).scalar_one()
    def name(self,table):
        if table not in self.names:_refuse()
        self.lease.check()
        name=(('public.' if self.public else 'pg_temp.') if self.pg else '')+self.names[table]
        if self.pg:identity=self.db.execute(text('SELECT to_regclass(:name)::oid'),{'name':name}).scalar_one()
        else:identity=self.db.execute(text("SELECT rootpage FROM "+('sqlite_schema' if self.public else 'sqlite_temp_schema')+" WHERE name=:name"),{'name':name}).scalar_one_or_none()  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fragment: catalog name selected from the closed pair sqlite_schema/sqlite_temp_schema; values are bound
        if identity!=self.identities[table]:_refuse()
        return name
    def query(self,sql,params=None):
        if self.public and sql not in self.allowed_reads:_refuse()
        self.lease.sql_timeout();result=self.db.execute(text(sql),params or {});self.lease.check();return result
    def rows(self,table,order=''):
        orders={schema.SOURCE:'source_uid,revision',schema.SELECTION_HISTORY:'owner_scope,owner_key,revision',schema.RECEIPT:'job_uid,receipt_revision',schema.COMPONENT:'receipt_uid,component_name',schema.REFERENCE_HISTORY:'job_uid,revision'}
        if table not in schema.TABLES or order not in ('',orders.get(table,'')):_refuse()
        self.lease.sql_timeout()
        clause=text('SELECT * FROM '+self.name(table)+((' ORDER BY '+order) if order else ''))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- table checked against the schema.TABLES allowlist and order against the fixed orders map
        if self.pg:clause=clause.execution_options(stream_results=True,max_row_buffer=128)
        result=self.db.execute(clause).mappings();error=None
        try:
            while True:
                self.lease.sql_timeout();chunk=result.fetchmany(128)
                if not chunk:break
                yield from chunk
        except BaseException as caught:error=caught;raise
        finally:
            try:result.close()
            except BaseException:
                if error is not None:error.add_note('Owned staged-read cursor cleanup uncertain')
                else:raise PricedRestoreError('Owned staged-read cursor cleanup uncertain') from None
    def drop(self):
        if self.public:_refuse()
        for table in reversed(schema.TABLES):self.query('DROP TABLE '+self.name(table))

    @classmethod
    def public_view(cls,lease):
        """Fixed seven-table read-only view, fresh row reads and OID checks.
        It carries no account/archive authorization and cannot drop or write."""
        view=object.__new__(cls);view.lease=lease;view.db=lease.db;view.pg=view.db.dialect.name=='postgresql';view.public=True
        view.names={table:table for table in schema.TABLES};view.identities={}
        for table in schema.TABLES:
            lease.check()
            if view.pg:view.identities[table]=view.db.execute(text('SELECT to_regclass(:name)::oid'),{'name':'public.'+table}).scalar_one()
            else:view.identities[table]=view.db.execute(text("SELECT rootpage FROM sqlite_schema WHERE name=:name"),{'name':table}).scalar_one()
        view.allowed_reads=_public_read_templates(view)
        return view


class _Snapshot:
    """Closed read-only templates used by the existing immutable validator.

    No generic SQL rewrite: only explicitly enumerated source-authored statements
    and their exact parameter-key sets execute against this original stage.
    """
    def __init__(self,stage):
        self.stage=stage;self.dialect=stage.db.dialect;pg=stage.pg
        prefix='public.' if pg else ''
        self.templates={}
        def add(sql,table,keys=()):self.templates[sql]=(table,frozenset(keys))
        add('SELECT resource_kind,owner_scope,owner_key FROM '+prefix+'cost_identities WHERE identity_uid=:uid',None,('uid',))
        add('SELECT * FROM '+prefix+schema.SOURCE+' WHERE source_uid=:uid AND revision=:revision',schema.SOURCE,('uid','revision'))
        add('SELECT * FROM '+prefix+schema.SELECTION_HISTORY+' WHERE owner_scope=:scope AND owner_key=:key AND revision=:revision',schema.SELECTION_HISTORY,('scope','key','revision'))
        add('SELECT * FROM '+prefix+schema.RECEIPT+' WHERE receipt_uid=:uid',schema.RECEIPT,('uid',))
        add('SELECT * FROM '+prefix+schema.COMPONENT+' WHERE receipt_uid=:uid ORDER BY component_name LIMIT 12',schema.COMPONENT,('uid',))
        for table,(cols,rules) in schema.definitions(pg).items():
            predicates=[]
            for col in cols:
                if ' NOT NULL' in col:predicates.append(col.split(' ',1)[0]+' IS NULL')
                if ' CHECK(' in col:predicates.append('('+col.split(' CHECK(',1)[1][:-1]+') IS NOT TRUE')
            predicates.extend('('+rule[6:-1]+') IS NOT TRUE' for rule in rules if rule.startswith('CHECK('))
            add('SELECT 1 FROM '+prefix+table+' WHERE '+' OR '.join(predicates)+' LIMIT 1',table)
    def execute(self,clause,params=None):
        from sqlalchemy.sql.elements import TextClause
        if type(clause) is not TextClause or str(clause) not in self.templates:_refuse()
        table,keys=self.templates[str(clause)]
        if type(params) is not dict or set(params)!=keys:_refuse()
        sql=str(clause)
        if table is not None:sql=sql.replace('FROM '+('public.' if self.stage.pg else '')+table,'FROM '+self.stage.name(table),1)
        return self.stage.query(sql,params)


def _public_read_templates(stage):
    """Closed immutable SQL shapes, not a cache of rows or authority proof."""
    queries=set(_Snapshot(stage).templates)
    name=lambda table:('public.' if stage.pg else '')+table
    queries.add('SELECT 1 FROM '+name(schema.SOURCE)+' AS NEW WHERE '+schema.source_json_invalid(stage.pg)+' LIMIT 1')
    queries.add('SELECT 1 FROM '+name(schema.SOURCE)+" GROUP BY source_uid HAVING COUNT(DISTINCT owner_scope||':'||CAST(owner_key AS TEXT))!=1 LIMIT 1")
    for table,keys,revision in ((schema.SOURCE,('source_uid',),'revision'),(schema.SELECTION_HISTORY,('owner_scope','owner_key'),'revision'),(schema.RECEIPT,('job_uid',),'receipt_revision'),(schema.REFERENCE_HISTORY,('job_uid',),'revision')):
        queries.add('SELECT 1 FROM '+name(table)+' GROUP BY '+','.join(keys)+' HAVING COUNT(*)>4096 OR MIN('+revision+')!=1 OR MAX('+revision+')!=COUNT(*) LIMIT 1')
    for table,(_,rules) in schema.definitions(stage.pg).items():
        for rule in rules:
            match=re.match(r'FOREIGN KEY\(([^)]+)\) REFERENCES (\w+)\(([^)]+)\)',rule)
            if not match:continue
            local,target,remote=match.groups();local=local.split(',');remote=remote.split(',')
            queries.add('SELECT 1 FROM '+name(table)+' a WHERE '+' AND '.join('a.'+n+' IS NOT NULL' for n in local)+' AND NOT EXISTS(SELECT 1 FROM '+name(target)+' b WHERE '+' AND '.join('a.'+a+'=b.'+b for a,b in zip(local,remote))+') LIMIT 1')
    for current,history,keys,fields in ((schema.SELECTION,schema.SELECTION_HISTORY,('owner_scope','owner_key'),('revision',)),(schema.CURRENT,schema.REFERENCE_HISTORY,('job_uid',),('owner_scope','owner_key','revision','receipt_uid','invalidation_reason','current_input_sha256'))):
        c,h=name(current),name(history);join=' AND '.join('c.'+n+'=h.'+n for n in keys);latest='h.revision=(SELECT MAX(z.revision) FROM '+h+' z WHERE '+' AND '.join('z.'+n+'=h.'+n for n in keys)+')'
        queries.add('SELECT 1 FROM '+c+' c WHERE NOT EXISTS(SELECT 1 FROM '+h+' h WHERE '+join+' AND '+latest+' AND '+' AND '.join('c.'+n+' IS NOT DISTINCT FROM h.'+n for n in fields)+') LIMIT 1')
        queries.add('SELECT 1 FROM '+h+' h WHERE '+latest+' AND NOT EXISTS(SELECT 1 FROM '+c+' c WHERE '+join+') LIMIT 1')
    return frozenset(queries)


def _validate_stage(stage):
    snapshot=_Snapshot(stage);validation._local_checks(snapshot)
    if stage.query('SELECT 1 FROM '+stage.name(schema.SOURCE)+' AS NEW WHERE '+schema.source_json_invalid(stage.pg)+' LIMIT 1').first():_refuse()
    for table,keys,revision in ((schema.SOURCE,('source_uid',),'revision'),(schema.SELECTION_HISTORY,('owner_scope','owner_key'),'revision'),(schema.RECEIPT,('job_uid',),'receipt_revision'),(schema.REFERENCE_HISTORY,('job_uid',),'revision')):
        query='SELECT 1 FROM '+stage.name(table)+' GROUP BY '+','.join(keys)+' HAVING COUNT(*)>4096 OR MIN('+revision+')!=1 OR MAX('+revision+')!=COUNT(*) LIMIT 1'
        if stage.query(query).first():_refuse()
    # Immutable owner for every source lifetime, independent of history triggers.
    if stage.query('SELECT 1 FROM '+stage.name(schema.SOURCE)+" GROUP BY source_uid HAVING COUNT(DISTINCT owner_scope||':'||CAST(owner_key AS TEXT))!=1 LIMIT 1").first():_refuse()
    for table,(_,rules) in schema.definitions(stage.pg).items():
        for rule in rules:
            match=re.match(r'FOREIGN KEY\(([^)]+)\) REFERENCES (\w+)\(([^)]+)\)',rule)
            if not match:continue
            local,target,remote=match.groups();local=local.split(',');remote=remote.split(',')
            parent=stage.name(target) if target in schema.TABLES else ('public.' if stage.pg else '')+target
            sql='SELECT 1 FROM '+stage.name(table)+' a WHERE '+' AND '.join('a.'+n+' IS NOT NULL' for n in local)+' AND NOT EXISTS(SELECT 1 FROM '+parent+' b WHERE '+' AND '.join('a.'+a+'=b.'+b for a,b in zip(local,remote))+') LIMIT 1'
            if stage.query(sql).first():_refuse()
    for row in stage.rows(schema.SOURCE):validation.validate_source(row)
    for row in stage.rows(schema.SELECTION_HISTORY):
        validation._time(row['created_at'])
        if row['expected_revision']!=row['revision']-1 or validation.digest(validation.selection_command(row))!=row['request_sha256']:_refuse()
        if row['source_uid'] is not None:
            source=validation._one(snapshot,'SELECT * FROM '+schema.SOURCE+' WHERE source_uid=:uid AND revision=:revision',{'uid':row['source_uid'],'revision':row['source_revision']})
            if (source['owner_scope'],source['owner_key'],source['currency'])!=(row['source_owner_scope'],row['source_owner_key'],row['currency']):_refuse()
    for row in stage.rows(schema.RECEIPT):validation.validate_receipt(snapshot,row)
    for row in stage.rows(schema.REFERENCE_HISTORY):validation._validate_reference(snapshot,row)
    _current_equals_history(stage)


def _current_equals_history(stage):
    for current,history,keys,fields in ((schema.SELECTION,schema.SELECTION_HISTORY,('owner_scope','owner_key'),('revision',)),(schema.CURRENT,schema.REFERENCE_HISTORY,('job_uid',),('owner_scope','owner_key','revision','receipt_uid','invalidation_reason','current_input_sha256'))):
        c,h=stage.name(current),stage.name(history)
        join=' AND '.join('c.'+n+'=h.'+n for n in keys)
        latest='h.revision=(SELECT MAX(z.revision) FROM '+h+' z WHERE '+' AND '.join('z.'+n+'=h.'+n for n in keys)+')'
        if stage.query('SELECT 1 FROM '+c+' c WHERE NOT EXISTS(SELECT 1 FROM '+h+' h WHERE '+join+' AND '+latest+' AND '+' AND '.join('c.'+n+' IS NOT DISTINCT FROM h.'+n for n in fields)+') LIMIT 1').first():_refuse()
        if stage.query('SELECT 1 FROM '+h+' h WHERE '+latest+' AND NOT EXISTS(SELECT 1 FROM '+c+' c WHERE '+join+') LIMIT 1').first():_refuse()


def _same_row(actual,expected):
    return set(actual)==set(expected) and all(type(actual[n]) is type(expected[n]) and actual[n]==expected[n] for n in expected)


def _insert(stage,table,row):
    stage.lease.check();names=columns(table)
    if set(row)!=set(names):_refuse()
    prefix='public.' if stage.pg else ''
    returned=stage.query('INSERT INTO '+prefix+table+'('+','.join(names)+') VALUES('+','.join(':'+n for n in names)+') RETURNING '+','.join(names),dict(row)).mappings().fetchmany(2)
    if len(returned)!=1 or not _same_row(dict(returned[0]),dict(row)):_refuse()


def _cas(stage,table,row,keys):
    names=columns(table);prefix='public.' if stage.pg else ''
    values={name:row[name] for name in names};values['old_revision']=row['revision']-1
    result=stage.query('UPDATE '+prefix+table+' SET '+','.join(n+'=:'+n for n in names if n not in keys)+' WHERE '+' AND '.join(n+'=:'+n for n in keys)+' AND revision=:old_revision RETURNING '+','.join(names),values).mappings().fetchmany(2)
    if len(result)!=1 or not _same_row(dict(result[0]),{n:row[n] for n in names}):_refuse()


def _replay(stage):
    for row in stage.rows(schema.SOURCE,'source_uid,revision'):_insert(stage,schema.SOURCE,row)
    for row in stage.rows(schema.SELECTION_HISTORY,'owner_scope,owner_key,revision'):
        _insert(stage,schema.SELECTION_HISTORY,row)
        current={n:row[n] for n in columns(schema.SELECTION)}
        if row['revision']==1:_insert(stage,schema.SELECTION,current)
        else:_cas(stage,schema.SELECTION,current,('owner_scope','owner_key'))
    for row in stage.rows(schema.RECEIPT,'job_uid,receipt_revision'):_insert(stage,schema.RECEIPT,row)
    for row in stage.rows(schema.COMPONENT,'receipt_uid,component_name'):_insert(stage,schema.COMPONENT,row)
    for row in stage.rows(schema.REFERENCE_HISTORY,'job_uid,revision'):
        _insert(stage,schema.REFERENCE_HISTORY,row)
        current={n:row[n] for n in columns(schema.CURRENT)}
        if row['revision']==1:_insert(stage,schema.CURRENT,current)
        else:_cas(stage,schema.CURRENT,current,('job_uid',))
    for table in (schema.SELECTION,schema.CURRENT):
        actual=('public.' if stage.pg else '')+table;archived=stage.name(table);names=columns(table)
        equal=' AND '.join('a.'+n+' IS NOT DISTINCT FROM b.'+n for n in names)
        for left,right in ((actual,archived),(archived,actual)):
            if stage.query('SELECT 1 FROM '+left+' a WHERE NOT EXISTS(SELECT 1 FROM '+right+' b WHERE '+equal+') LIMIT 1').first():_refuse()


def restore_priced_tables(connection,stream,*,table_data,budget,profile=PROFILE):
    """Stage/validate/replay into an EMPTY canonical family in caller PG TX.

    The caller separately owns disposable-target qualification, archive TOC/schema
    validation, ordinary prerequisite import, maintenance barrier and COMMIT/ACK.
    This helper cannot be used as their replacement and has no route activation.
    """
    if connection.dialect.name!='postgresql':_refuse()
    lease=_Lease(connection,budget)
    lease.sql_timeout()
    if connection.exec_driver_sql('SHOW transaction_isolation').scalar_one()!='read committed' or connection.exec_driver_sql('SELECT current_schema()').scalar_one()!='public':_refuse()
    timeout=int(connection.exec_driver_sql("SELECT setting FROM pg_settings WHERE name='lock_timeout'").scalar_one())
    if timeout==0 or timeout>2000:connection.exec_driver_sql("SELECT set_config('lock_timeout','2000ms',true)")
    for table in sorted(schema.TABLES):
        lease.sql_timeout();connection.exec_driver_sql('LOCK TABLE public.'+table+' IN ACCESS EXCLUSIVE MODE NOWAIT')
    lease.sql_timeout();validation.validate_schema(connection)
    for table in schema.TABLES:
        lease.sql_timeout()
        if connection.execute(text('SELECT 1 FROM public.'+table+' LIMIT 1')).first():_refuse()  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- table iterates the schema.TABLES constants
    stage=_Stage(lease);events=iter(extract_copy(stream,table_data=table_data,budget=budget,profile=profile))
    for event in events:
        if type(event) is not CopyStart:_refuse()
        table=event.table;cursor=lease.driver.cursor();error=None
        try:
            lease.sql_timeout()
            command='COPY '+stage.name(table)+' ('+', '.join(columns(table))+') FROM STDIN'
            with cursor.copy(command) as copy:
                for record in events:
                    lease.check(copy=True)
                    if type(record) is CopyEnd:
                        if record.table!=table:_refuse()
                        break
                    if type(record) is not CopyRecord or record.table!=table:_refuse()
                    copy.write(record.raw)
                else:_refuse()
        except BaseException as caught:error=caught;raise
        finally:
            try:cursor.close()
            except BaseException:
                if error is not None:error.add_note('Owned COPY cursor cleanup uncertain')
                else:raise PricedRestoreError('Owned COPY cursor cleanup uncertain') from None
    _validate_stage(stage);_replay(stage);stage.drop();validation.validate_schema(connection);_validate_stage(_Stage.public_view(lease));lease.check()
