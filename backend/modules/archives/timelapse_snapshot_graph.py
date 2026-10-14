"""Fresh, bounded, transaction-local inputs for frozen publication validation.

No permission proof, physical descriptor or result survives this view's scope.
"""
from types import MappingProxyType
import hashlib,json
from sqlalchemy import select,func,cast,LargeBinary,String,tuple_,case,bindparam
from sqlalchemy.sql import operators
from sqlalchemy.sql.selectable import Select
from sqlalchemy.sql.functions import count as Count
from sqlalchemy.sql.elements import BinaryExpression,BooleanClauseList,BindParameter,Grouping
from core.timelapse_publication_schema import publications,facts,versions,lifetimes,namespaces,bindings,retained
from core.timelapse_ownership_schema import anchors
from core.timelapse_manifest import MAX_OBJECTS
from modules.archives.timelapse_retirement import _refuse

from core.timelapse_media_lock import MediaGuard
from core.timelapse_manifest import ObjectIdentity

_PROOF=object()

class GraphFenceScope:
    def __init__(self,db,guard,identity,proof,*,retirement_proofs=False):
        if proof is not _PROOF or type(retirement_proofs) is not bool:_refuse('unknown_authority')
        self.db=db;self.guard=guard;self.identity=identity;self.proof=proof
        self.retirement_proofs=retirement_proofs
        self.connection=db.connection();self.transaction=db.get_transaction()
    def check(self,db):
        if self.proof is not _PROOF or self.db is not db or db.get_transaction() is not self.transaction or db.connection() is not self.connection:_refuse('unknown_authority')
        self.guard.assert_root_identity(self.identity)

def fence_graph_scope(db,guard,identity,*,preparation=False,retirement_proofs=False):
    """Only construction path: real live root guard, then actual DB fences."""
    if type(guard) is not MediaGuard or type(identity) is not ObjectIdentity:_refuse('unknown_authority')
    guard.assert_root_identity(identity)
    from modules.archives.timelapse_action_authority import fence_authorization
    fence_authorization(db,preparation=preparation,retirement_proofs=retirement_proofs)
    return GraphFenceScope(db,guard,identity,_PROOF,retirement_proofs=retirement_proofs)

MAX_ROWS=65536
MAX_BYTES=32*1024*1024
CHUNK=128

TRUSTED=(anchors,versions,lifetimes,facts,bindings,namespaces,retained,publications)
KEYS={anchors:'operation_id',versions:'version_nonce',lifetimes:'lifetime_nonce',facts:'operation_id',bindings:'lifetime_nonce',namespaces:'namespace_id',retained:'version_nonce',publications:'operation_id'}
MANIFEST=MappingProxyType({
 'timelapse_ownership_anchors':'d808109c0c50d56c0f93eb5a6a9baca3cd85880d0f49f69610e8675dccb24154',
 'timelapse_media_versions':'99b04298e354cc31550d7a5c2538ec163c4a1dbfb013fc87f24cda82cc625705',
 'timelapse_media_lifetimes':'5451145be5e7edb36ca92f1e999581ea943dd1d637eee186e54240c7610cb8a1',
 'timelapse_publication_facts':'7d56a145edb6d5d2fd92fb72c1bed0059e610ddf92712dfe70604824582fa8e0',
 'timelapse_frame_bindings':'4592a4d02edb5dcc34b26325276f79e15b88b0ddf1a183ee58ccb6525b68c019',
 'timelapse_frame_namespaces':'ce782794854920e94a3e6a44a91b7188ce3301ad63ae73f2ce16f0ae578f1eb0',
 'timelapse_retained_versions':'f1bfc287ff8b767e21de0df4971e03530fc60ed5e994f47acd490ef2e162f290',
 'timelapse_publications':'e5dcd60ecf0d8706674c9f468a297e5f652696b63350a8687f20ae0376ea696b'})

def _templates():
    result={}
    for table in TRUSTED:
        signature=[(c.name,type(c.type).__name__,str(c.type),getattr(c.type,'timezone',None),c.nullable,c.primary_key) for c in table.c]
        if hashlib.sha256(json.dumps(signature,separators=(',',':')).encode()).hexdigest()!=MANIFEST[table.name]:_refuse('phase_uncertain')
        for dialect in ('sqlite','postgresql'):
            size=0
            for col in table.c:
                if isinstance(col.type,(String,LargeBinary)):
                    length=func.octet_length(col) if dialect=='postgresql' else func.length(cast(col,LargeBinary))
                    size=size+case((col.is_(None),64),else_=length+16)
                else:size=size+64
            domains={KEYS[table]:table.c[KEYS[table]].in_(bindparam('keys',expanding=True))}
            if table is publications:
                domains.update(nonframe_result_nonce=publications.c.result_nonce.in_(bindparam('keys',expanding=True)) & (publications.c.kind!='frame'),frame_pair=(publications.c.kind=='frame') & tuple_(publications.c.frame_namespace_id,publications.c.frame_number).in_(bindparam('keys',expanding=True)))
            for domain,condition in domains.items():
                bounded=select(func.count(),func.coalesce(func.sum(size),0)).select_from(table).where(condition)
                rows=select(table).where(condition).order_by(*table.primary_key.columns).with_for_update(read=True,nowait=True).execution_options(stream_results=True,max_row_buffer=CHUNK)
                result[(table,dialect,domain)]=(bounded,rows)
    return MappingProxyType(result)

TEMPLATES=_templates()
MAX_INDEX_REFS=6*MAX_ROWS

class _Result:
    def __init__(self,rows,count=False):self.rows=rows;self.count=count
    def mappings(self):return self
    def __iter__(self):return iter(self.rows)
    def first(self):return self.rows[0] if self.rows else None
    def scalar_one(self):
        if not self.count or len(self.rows)!=1:_refuse('phase_uncertain')
        return self.rows[0]

class SnapshotGraphReadView:
    def __init__(self,db,ops,*,scope,lifetime,row_limit=None,byte_limit=None):
        if type(scope) is not GraphFenceScope:_refuse('unknown_authority')
        row_limit=MAX_ROWS if row_limit is None else row_limit
        byte_limit=MAX_BYTES if byte_limit is None else byte_limit
        if type(row_limit) is not int or not 0<=row_limit<=MAX_ROWS or type(byte_limit) is not int or not 0<=byte_limit<=MAX_BYTES:_refuse('manifest_limit')
        self.row_limit=row_limit;self.byte_limit=byte_limit
        scope.check(db)
        self.scope=scope;self.lifetime=lifetime
        if any(r['source_lifetime']!=lifetime and r['result_lifetime']!=lifetime for r in ops):_refuse('phase_uncertain')
        self.db=db;self.connection=db.connection();self.transaction=db.get_transaction()
        self.rows={};self.domains={};self.indices={};self.used_rows=0;self.used_bytes=0;self.closed=False
        if self.transaction is None:_refuse('phase_uncertain')
        try:self._build(ops);self._index()
        except BaseException:self.close();raise
    def close(self):self.closed=True;self.rows.clear();self.domains.clear();self.indices.clear()
    def _check(self):
        if self.closed or self.db.get_transaction() is not self.transaction or self.db.connection() is not self.connection:_refuse('phase_uncertain')
        self.scope.check(self.db)
    def _batch(self,table,domain,values):
        self._check();values=set(values)
        template=TEMPLATES.get((table,self.connection.dialect.name,domain))
        if template is None:_refuse('phase_uncertain')
        parameters={'keys':sorted(values)}
        n,total=self.db.execute(template[0],parameters).one()
        if n>self.row_limit-self.used_rows or total>self.byte_limit-self.used_bytes:_refuse('manifest_limit')
        cursor=self.db.execute(template[1],parameters)
        try:
            while True:
                chunk=cursor.mappings().fetchmany(CHUNK)
                if not chunk:break
                for raw in chunk:
                    row=dict(raw);charge=sum(len(v.encode('utf8'))+16 if isinstance(v,str) else len(v)+16 if isinstance(v,bytes) else 64 for v in row.values())
                    if self.used_rows+1>self.row_limit or self.used_bytes+charge>self.byte_limit:_refuse('manifest_limit')
                    key=tuple(row[c.name] for c in table.primary_key.columns)
                    old=self.rows.setdefault(table.name,{}).get(key)
                    if old is not None:
                        if dict(old)!=row:_refuse('phase_uncertain')
                    else:
                        self.rows[table.name][key]=MappingProxyType(row);self.used_rows+=1;self.used_bytes+=charge
        finally:cursor.close()
        self.domains.setdefault((table.name,domain),set()).update(values)
    def _by(self,table,key,values):
        values=set(values)-self.domains.get((table.name,key),set())
        if values:self._batch(table,key,values)
    def _values(self,table):return list(self.rows.get(table.name,{}).values())
    def _build(self,ops):
        ids={r['operation_id'] for r in ops};self._by(publications,'operation_id',ids)
        actual={r['operation_id']:dict(r) for r in self._values(publications)}
        if any(actual.get(r['operation_id'])!=r for r in ops):_refuse('phase_uncertain')
        priors={r['source_nonce'] for r in ops if r['kind']=='speed'}
        self._batch(publications,'nonframe_result_nonce',priors)
        validation=ids|{r['operation_id'] for r in self._values(publications) if r['result_nonce'] in priors and r['kind']!='frame'}
        validation_ops=[r for r in self._values(publications) if r['operation_id'] in validation]
        self._by(facts,'operation_id',validation)
        nonce={r[k] for r in validation_ops for k in ('source_nonce','result_nonce','source_lifetime','result_lifetime')}
        life={r[k] for r in validation_ops for k in ('source_lifetime','result_lifetime')}
        for _ in range(32):
            before=(len(nonce),len(life))
            self._by(versions,'version_nonce',nonce);life.update(r['lifetime_nonce'] for r in self._values(versions))
            self._by(lifetimes,'lifetime_nonce',life);nonce.update(life)
            self._by(anchors,'operation_id',nonce)
            if before==(len(nonce),len(life)):break
        else:_refuse('manifest_limit')
        results={r['result_nonce'] for r in validation_ops if r['kind']!='frame'}|{r['version_nonce'] for r in self._values(versions) if r['version_nonce']!=r['lifetime_nonce']}
        self._batch(publications,'nonframe_result_nonce',results)
        capture={r['source_lifetime'] for r in validation_ops if r['kind'] in ('frame','encode')}
        self._by(bindings,'lifetime_nonce',capture)
        spaces={r['namespace_id'] for r in self._values(bindings)}|{r['frame_namespace_id'] for r in validation_ops if r['kind']=='frame'}
        self._by(namespaces,'namespace_id',spaces)
        pairs=set()
        for op in validation_ops:
            if op['kind']!='encode':continue
            binding=[r for r in self._values(bindings) if r['lifetime_nonce']==op['source_lifetime']]
            if len(binding)!=1:_refuse('phase_uncertain')
            ns=binding[0]['namespace_id'];prefix='frames_'+ns+'/frame_'
            for f in self._values(facts):
                if f['operation_id']!=op['operation_id'] or f['role']!='source':continue
                path=f['relative_path'];stem=path.removeprefix(prefix).removesuffix('.jpg')
                if not path.startswith(prefix) or not path.endswith('.jpg') or not stem.isascii() or not stem.isdigit() or stem!=str(int(stem)).zfill(6):_refuse('phase_uncertain')
                pairs.add((ns,int(stem)))
        self._batch(publications,'frame_pair',pairs)
        frames={r['operation_id'] for r in self._values(publications) if r['kind']=='frame' and (r['frame_namespace_id'],r['frame_number']) in pairs}
        self._by(facts,'operation_id',frames)
        self._by(retained,'version_nonce',{r['source_nonce'] for r in validation_ops if r['kind']=='trim' and r['phase']=='complete'})
    def _index(self):
        refs=0
        for table in (facts,publications):
            for row in self._values(table):
                keys=(('facts_op',row['operation_id']),('facts_role',(row['operation_id'],row['role']))) if table is facts else ((('pub_result',row['result_nonce']),) if row['kind']!='frame' else (('pub_frame',(row['frame_namespace_id'],row['frame_number'])),))
                for key in keys:
                    refs+=1
                    if refs>MAX_INDEX_REFS:_refuse('manifest_limit')
                    self.indices.setdefault(key,[]).append(row)
        for key,rows in self.indices.items():
            table=facts if key[0].startswith('facts_') else publications
            rows.sort(key=lambda r:tuple(r[c.name] for c in table.primary_key.columns))
            self.indices[key]=tuple(rows)

    def _predicates(self,expr,table):
        if isinstance(expr,Grouping):return self._predicates(expr.element,table)
        if isinstance(expr,BooleanClauseList) and expr.operator is operators.and_:
            return sum((self._predicates(c,table) for c in expr.clauses),[])
        if not isinstance(expr,BinaryExpression) or expr.operator not in (operators.eq,operators.ne,operators.in_op) or not isinstance(expr.right,BindParameter):_refuse('phase_uncertain')
        left=expr.left
        if getattr(left,'table',None) is not table or left.name not in table.c or left is not table.c[left.name]:_refuse('phase_uncertain')
        return [(left.name,expr.operator,expr.right.value)]
    def execute(self,statement,*args,**kwargs):
        self._check()
        if args or kwargs or type(statement) is not Select:_refuse('phase_uncertain')
        if statement._distinct or statement._distinct_on or statement._group_by_clauses or statement._having_criteria or statement._prefixes or statement._suffixes or statement._hints or statement._statement_hints or statement._independent_ctes or statement._correlate or statement._correlate_except is not None or not statement._auto_correlate or statement._fetch_clause is not None or statement._offset_clause is not None:_refuse('phase_uncertain')
        if len(statement._raw_columns)!=1:_refuse('phase_uncertain')
        projection=statement._raw_columns[0];count=type(projection) is Count
        if count:
            if len(statement._from_obj)!=1 or statement._from_obj[0] is not publications:_refuse('phase_uncertain')
            table=publications
        else:
            table=next((t for t in TRUSTED if projection is t),None)
            if table is None or len(statement._from_obj)>1 or any(t is not table for t in statement._from_obj):_refuse('phase_uncertain')
        lock=statement._for_update_arg
        if count and lock is not None:_refuse('phase_uncertain')
        if lock is not None and (not lock.read or not lock.nowait or lock.of is not None or lock.skip_locked or lock.key_share):_refuse('phase_uncertain')
        pred=sum((self._predicates(c,table) for c in statement._where_criteria),[])
        if not pred:_refuse('phase_uncertain')
        def complete(key,value):return value in self.domains.get((table.name,key),set())
        if count:
            clauses=list(statement._raw_columns[0].clauses)
            if len(clauses)!=1 or getattr(clauses[0],'name',None)!='*' or getattr(clauses[0],'table',None) is not None or statement._limit_clause is not None or statement._order_by_clauses:_refuse('phase_uncertain')
            if table is not publications or len(pred)!=2 or not any(k=='kind' and op is operators.ne and v=='frame' for k,op,v in pred):_refuse('phase_uncertain')
            nonce=next((v for k,op,v in pred if k=='result_nonce' and op is operators.eq),None)
            if not complete('nonframe_result_nonce',nonce):_refuse('phase_uncertain')
        else:
            if list(statement._raw_columns)!=[table]:_refuse('phase_uncertain')
            covered=any(op is operators.eq and complete(k,v) or op is operators.in_op and all(complete(k,x) for x in v) for k,op,v in pred)
            frame=dict((k,v) for k,op,v in pred if op is operators.eq)
            covered=covered or table is publications and frame.get('kind')=='frame' and complete('frame_pair',(frame.get('frame_namespace_id'),frame.get('frame_number')))
            # Speed prior full rows are bounded by the nonframe nonce domain.
            covered=covered or table is publications and any(k=='result_nonce' and op is operators.eq and complete('nonframe_result_nonce',v) for k,op,v in pred) and any(k=='kind' and op is operators.ne and v=='frame' for k,op,v in pred)
            if not covered:_refuse('phase_uncertain')
        # Exact supported domains only, using this fresh view's row refs.
        terms={k:(op,v) for k,op,v in pred}
        if len(terms)!=len(pred):_refuse('phase_uncertain')
        key=KEYS[table]
        if table is not facts and set(terms)=={key} and terms[key][0] is operators.eq:
            if not complete(key,terms[key][1]):_refuse('phase_uncertain')
            row=self.rows.get(table.name,{}).get((terms[key][1],));rows=[] if row is None else [row]
        elif table is facts and set(terms) in ({'operation_id','role'},{'operation_id'}):
            identifier=terms['operation_id']
            if identifier[0] is not operators.eq or not complete('operation_id',identifier[1]):_refuse('phase_uncertain')
            role=terms.get('role')
            if role is None:rows=list(self.indices.get(('facts_op',identifier[1]),()))
            elif role[0] is operators.eq:rows=list(self.indices.get(('facts_role',(identifier[1],role[1])),()))
            elif role[0] is operators.in_op:rows=[r for value in dict.fromkeys(role[1]) for r in self.indices.get(('facts_role',(identifier[1],value)),())]
            else:_refuse('phase_uncertain')
            if role and any(v not in ('root','source','source_parent','private_root','private_creation','output','output_parent') for v in ([role[1]] if role[0] is operators.eq else role[1])):_refuse('phase_uncertain')
        elif table is publications and set(terms)=={'result_nonce','kind'} and terms['result_nonce'][0] is operators.eq and terms['kind']==(operators.ne,'frame'):
            value=terms['result_nonce'][1]
            if not complete('nonframe_result_nonce',value):_refuse('phase_uncertain')
            rows=list(self.indices.get(('pub_result',value),()))
        elif table is publications and set(terms)=={'kind','frame_namespace_id','frame_number'} and terms['kind']==(operators.eq,'frame') and all(op is operators.eq for op,v in terms.values()):
            pair=(terms['frame_namespace_id'][1],terms['frame_number'][1])
            if not complete('frame_pair',pair):_refuse('phase_uncertain')
            rows=list(self.indices.get(('pub_frame',pair),()))
        else:_refuse('phase_uncertain')
        ordering=tuple(statement._order_by_clauses);primary=tuple(table.primary_key.columns)
        if ordering and (len(ordering)!=len(primary) or any(o is not c for o,c in zip(ordering,primary))):_refuse('phase_uncertain')
        if ordering:rows.sort(key=lambda r:tuple(r[c.name] for c in primary))
        if statement._offset_clause is not None:_refuse('phase_uncertain')
        if statement._limit_clause is not None:
            value=statement._limit_clause.value
            if type(value) is not int or not ((table is facts and set(terms)=={'operation_id'} and value==MAX_OBJECTS+1) or (table is publications and set(terms)=={'result_nonce','kind'} and terms['kind']==(operators.ne,'frame') and value==2)):_refuse('phase_uncertain')
            rows=rows[:value]
        return _Result([len(rows)] if count else rows,count)
