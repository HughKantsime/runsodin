"""Read physical cost-schema constraints and guards without reading user records."""
from sqlalchemy import inspect,text

TABLES=('cost_identities','cost_order_memberships','cost_job_bindings','cost_attempt_bindings','cost_obligations','cost_attempt_participation','finance_currency_settings')
FUNCTIONS=('cost_anchor_immutable','cost_participation_guard')


def sqlite_signature(connection):
    rows=connection.execute(text("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE tbl_name IN ("+','.join(':t'+str(i) for i in range(len(TABLES)))+") ORDER BY type,name"), {'t'+str(i):t for i,t in enumerate(TABLES)}).all()
    return [list(row) for row in rows]


def postgres_signature(connection,schema):
    inspector=inspect(connection)
    result={'tables':{},'functions':[],'triggers':[],'foreignTargets':[]}
    for table in TABLES:
        if not inspector.has_table(table,schema=schema):continue
        result['tables'][table]={
            'columns':[{k:(str(c[k]) if k=='type' else c.get(k)) for k in ('name','type','nullable','default')} for c in inspector.get_columns(table,schema=schema)],
            'pk':inspector.get_pk_constraint(table,schema=schema),
            'unique':inspector.get_unique_constraints(table,schema=schema),
            'checks':inspector.get_check_constraints(table,schema=schema),
            'indexes':inspector.get_indexes(table,schema=schema),
        }
    rows=connection.execute(text("""SELECT p.proname,p.prosrc,p.proconfig,p.prosecdef,p.provolatile,p.proparallel,p.prorettype::regtype::text,pg_get_function_identity_arguments(p.oid),l.lanname
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace JOIN pg_language l ON l.oid=p.prolang
        WHERE n.nspname=:schema AND p.proname IN ('cost_anchor_immutable','cost_participation_guard') ORDER BY p.proname,p.oid"""),{'schema':schema}).all()
    for row in rows:
        values=list(row);config=values[2]
        if config:
            values[2]=[x.replace('search_path=pg_catalog, '+schema,'search_path=pg_catalog, $ledger_schema').replace('search_path=pg_catalog, "'+schema+'"','search_path=pg_catalog, $ledger_schema') for x in config]
        result['functions'].append(values)
    rows=connection.execute(text("""SELECT c.relname,t.tgname,pg_get_triggerdef(t.oid),f.proname,fn.nspname,t.tgenabled
        FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_proc f ON f.oid=t.tgfoid JOIN pg_namespace fn ON fn.oid=f.pronamespace
        WHERE n.nspname=:schema AND NOT t.tgisinternal AND c.relname IN ('cost_identities','cost_order_memberships','cost_job_bindings','cost_attempt_bindings','cost_obligations','cost_attempt_participation','finance_currency_settings') ORDER BY c.relname,t.tgname"""),{'schema':schema}).all()
    for row in rows:
        values=list(row)
        values[2]=values[2].replace('"'+schema+'".','$ledger_schema.').replace(schema+'.','$ledger_schema.')
        values[4]='$ledger_schema' if values[4]==schema else values[4]
        result['triggers'].append(values)
    rows=connection.execute(text("""SELECT child.relname,c.conname,parent.relname,pn.nspname,pg_get_constraintdef(c.oid)
        FROM pg_constraint c JOIN pg_class child ON child.oid=c.conrelid JOIN pg_namespace cn ON cn.oid=child.relnamespace
        JOIN pg_class parent ON parent.oid=c.confrelid JOIN pg_namespace pn ON pn.oid=parent.relnamespace
        WHERE cn.nspname=:schema AND child.relname IN ('cost_identities','cost_order_memberships','cost_job_bindings','cost_attempt_bindings','cost_obligations','cost_attempt_participation','finance_currency_settings') AND c.contype='f' ORDER BY child.relname,c.conname"""),{'schema':schema}).all()
    for row in rows:
        values=list(row);values[3]='$ledger_schema' if values[3]==schema else values[3]
        values[4]=values[4].replace('"'+schema+'".','$ledger_schema.').replace(schema+'.','$ledger_schema.')
        result['foreignTargets'].append(values)
    return result
