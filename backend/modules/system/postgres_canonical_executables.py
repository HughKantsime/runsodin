"""Source-owned PostgreSQL guards and lexical archive-schema comparison.

Archive SQL is compared as evidence only; this module never executes it.
"""
import importlib
import re
from collections import Counter
from sqlalchemy import text
from modules.system.backup_service import BackupValidationError


def canonical_functions(*, _contract=None):
    from core.schema.pre026_restore_contract import check_contract,inventory_for,PRE027_RESTORE026
    check_contract(_contract)
    INVENTORY = inventory_for(_contract) if _contract is not None else None
    foundation=importlib.import_module('core.schema.migrations.009_cost_foundation')
    identity=importlib.import_module('core.schema.migrations.021_economic_identity')
    functions={}
    for row in foundation.EXPECTED_POSTGRES['functions']:
        name,source,config,definer,volatility,parallel,result,args,language=row
        functions[name]=(source,[v.replace('$ledger_schema','public') for v in config],definer,volatility,parallel,result,args,language,False,False,'f',False,0,100.0,0.0,'-')
    for table in identity.TABLES:
        functions[table+'_economic_guard']=(identity._pg_body(table),['search_path=pg_catalog, public'],False,'v','u','trigger','','plpgsql',False,False,'f',False,0,100.0,0.0,'-')
    lifetimes=importlib.import_module('core.schema.migrations.022_economic_source_lifetimes')
    for name,source in lifetimes._pg_functions().items():
        functions[name]=(source,['search_path=pg_catalog, public'],False,'v','u','trigger','','plpgsql',False,False,'f',False,0,100.0,0.0,'-')
    membership=importlib.import_module('core.timelapse_retirement_membership_validation')
    actor=importlib.import_module('core.audit_actor_schema')
    additions=[(name,body,['search_path=pg_catalog, public']) for name,body in membership.pg_functions().items()]
    additions.append((actor.FUNCTION,actor.PG_BODY,['search_path=pg_catalog']))
    for name,source,config in additions:
        if name in functions:raise BackupValidationError('Canonical function registry collision')
        functions[name]=(source,config,False,'v','u','trigger','','plpgsql',False,False,'f',False,0,100.0,0.0,'-')
    purchase=importlib.import_module('core.spool_purchase_schema')
    for name,source in purchase.pg_functions().items():
        if name in functions:raise BackupValidationError('Canonical function registry collision')
        functions[name]=(source,['search_path=pg_catalog, public'],False,'v','u','trigger','','plpgsql',False,False,'f',False,0,100.0,0.0,'-')
    if _contract is None or _contract is PRE027_RESTORE026:
        origins=importlib.import_module('core.schema.migrations.026_global_order_origins')
        origins._source()
        for name,source in origins.schema.pg_functions().items():
            if name in functions:raise BackupValidationError('Canonical function registry collision')
            functions[name]=(source,['search_path=pg_catalog, public'],False,'v','u','trigger','','plpgsql',False,False,'f',False,0,100.0,0.0,'-')
    if _contract is None:
        power=importlib.import_module('core.schema.migrations.027_power_target_operations')
        power._source()
        for name,source in power.schema.pg_functions().items():
            if name in functions:raise BackupValidationError('Canonical function registry collision')
            functions[name]=(source,['search_path=pg_catalog, public'],False,'v','u','trigger','','plpgsql',False,False,'f',False,0,100.0,0.0,'-')
    if _contract is None:
        retained=importlib.import_module('core.schema.migrations.037_education_memberships')
        for name,source in retained.pg_functions().items():
            if name in functions:raise BackupValidationError('Canonical function registry collision')
            functions[name]=(source,['search_path=pg_catalog, public'],False,'v','u','trigger','','plpgsql',False,False,'f',False,0,100.0,0.0,'-')
    if _contract is None:
        recovery=importlib.import_module('core.schema.migrations.038_dispatch_recovery')
        for name,source in recovery.pg_functions().items():
            if name in functions:raise BackupValidationError('Canonical function registry collision')
            functions[name]=(source,None,False,'v','u','trigger','','plpgsql',False,False,'f',False,0,100.0,0.0,'-')
    functions['odin_reject_education_audit_mutation']=(" BEGIN RAISE EXCEPTION 'education audit events are immutable'; END; ",None,False,'v','u','trigger','','plpgsql',False,False,'f',False,0,100.0,0.0,'-')
    return functions if _contract is None else {n:v for n,v in functions.items() if n in INVENTORY["functions"]}


def canonical_triggers(*, _contract=None):
    from core.schema.pre026_restore_contract import check_contract,inventory_for,PRE027_RESTORE026
    check_contract(_contract)
    INVENTORY = inventory_for(_contract) if _contract is not None else None
    foundation=importlib.import_module('core.schema.migrations.009_cost_foundation')
    identity=importlib.import_module('core.schema.migrations.021_economic_identity')
    triggers={}
    for table,name,definition,function,schema,enabled in foundation.EXPECTED_POSTGRES['triggers']:
        triggers[(table,name)]=(definition.replace('$ledger_schema','public'),function,'public',enabled)
    for table in identity.TABLES:
        name=table+'_economic_guard'
        triggers[(table,name)]=(f'CREATE TRIGGER {name} BEFORE INSERT OR UPDATE ON public.{table} FOR EACH ROW EXECUTE FUNCTION {name}()',name,'public','O')
    lifetimes=importlib.import_module('core.schema.migrations.022_economic_source_lifetimes')
    for (table,name),definition in lifetimes._pg_triggers().items():
        triggers[(table,name)]=(definition,name,'public','O')
    membership=importlib.import_module('core.timelapse_retirement_membership_validation')
    for name,definition in membership.pg_triggers().items():
        table=name.removesuffix('_membership_guard')
        key=(table,name)
        if key in triggers:raise BackupValidationError('Canonical trigger registry collision')
        definition=definition.replace('INSERT OR UPDATE OR DELETE','INSERT OR DELETE OR UPDATE').replace('EXECUTE FUNCTION public.','EXECUTE FUNCTION ')
        triggers[key]=(definition,name,'public','O')
    actor=importlib.import_module('core.audit_actor_schema')
    for name,definition in actor.trigger_sql(True).items():
        key=(actor.LEDGER,name)
        if key in triggers:raise BackupValidationError('Canonical trigger registry collision')
        definition=definition.replace('BEFORE UPDATE OR DELETE','BEFORE DELETE OR UPDATE').replace('ON '+actor.LEDGER+' ', 'ON public.'+actor.LEDGER+' ')
        triggers[key]=(definition,actor.FUNCTION,'public','O')
    purchase=importlib.import_module('core.spool_purchase_schema')
    for key,definition in purchase.pg_triggers().items():
        if key in triggers:raise BackupValidationError('Canonical trigger registry collision')
        function='spools_purchase_guard' if key[0]=='spools' else 'spool_purchase_evidence_guard'
        triggers[key]=(definition,function,'public','O')
    if _contract is None or _contract is PRE027_RESTORE026:
        origins=importlib.import_module('core.schema.migrations.026_global_order_origins')
        origins._source()
        for key,definition in origins.schema.pg_triggers().items():
            if key in triggers:raise BackupValidationError('Canonical trigger registry collision')
            function='orders_global_origin_guard' if key[0]=='orders' else key[0]+'_guard'
            triggers[key]=(definition,function,'public','O')
    if _contract is None:
        power=importlib.import_module('core.schema.migrations.027_power_target_operations')
        power._source()
        for name,definition in power.schema.pg_triggers().items():
            key=(power.schema.TABLE,name)
            if key in triggers:raise BackupValidationError('Canonical trigger registry collision')
            definition=definition.replace('INSERT OR UPDATE OR DELETE','INSERT OR DELETE OR UPDATE').replace('EXECUTE FUNCTION public.','EXECUTE FUNCTION ')
            triggers[key]=(definition,power.schema.FUNCTION,'public','O')
    if _contract is None:
        retained=importlib.import_module('core.schema.migrations.037_education_memberships')
        for key,definition in retained.pg_triggers().items():
            if key in triggers:raise BackupValidationError('Canonical trigger registry collision')
            triggers[key]=(definition,key[1],'public','O')
    if _contract is None:
        recovery=importlib.import_module('core.schema.migrations.038_dispatch_recovery')
        for key,definition in recovery.pg_triggers().items():
            if key in triggers:raise BackupValidationError('Canonical trigger registry collision')
            triggers[key]=(definition,'odin_recovery_immutable','public','O')
    for action in ('DELETE','UPDATE'):
        name='trg_education_audit_no_'+action.lower()
        function='odin_reject_education_audit_mutation'
        triggers[('education_audit_events',name)]=(f'CREATE TRIGGER {name} BEFORE {action} ON public.education_audit_events FOR EACH ROW EXECUTE FUNCTION {function}()',function,'public','O')
    return triggers if _contract is None else {k:v for k,v in triggers.items() if list(k) in INVENTORY["triggers"]}


def sql_statement_sequence(sql):
    """Lexical equality only: preserve quoted text, comments outside it ignored.

    No semantic normalization, case folding or expression rewriting. Reject
    malformed quotes/comments and psql controls except paired dump restrictions.
    """
    if type(sql) is not str:
        raise BackupValidationError('Archive schema SQL is invalid')
    statements=[];current=[];position=0;length=len(sql);restriction=None;seen_restriction=False
    def space():
        if current and current[-1]!=' ':current.append(' ')
    while position<length:
        character=sql[position]
        if character.isspace():space();position+=1;continue
        if sql.startswith('--',position):
            end=sql.find('\n',position+2);position=length if end<0 else end+1;space();continue
        if sql.startswith('/*',position):
            depth=1;position+=2
            while position<length and depth:
                if sql.startswith('/*',position):depth+=1;position+=2
                elif sql.startswith('*/',position):depth-=1;position+=2
                else:position+=1
            if depth:raise BackupValidationError('Archive schema has unterminated comment')
            space();continue
        if character=='\\':
            if ''.join(current).strip() or (position and sql[position-1]!='\n'):
                raise BackupValidationError('Archive schema contains unsupported control record')
            end=sql.find('\n',position);end=length if end<0 else end
            line=sql[position:end]
            match=re.fullmatch(r'\\(restrict|unrestrict) ([A-Za-z0-9]+)',line)
            if not match:raise BackupValidationError('Archive schema contains unsupported control record')
            action,token=match.groups()
            if action=='restrict':
                if restriction is not None or seen_restriction:raise BackupValidationError('Archive schema restriction mismatch')
                restriction=token;seen_restriction=True
            elif restriction!=token:
                raise BackupValidationError('Archive schema restriction mismatch')
            else:restriction=None
            position=end;continue
        if character in ("'",'"'):
            quote=character;start=position;position+=1
            # Only E-prefixed strings have backslash escapes; quoted content stays exact.
            escape=quote=="'" and start>0 and sql[start-1] in 'eE' and (start<2 or not (sql[start-2].isalnum() or sql[start-2]=='_'))
            closed=False
            while position<length:
                if escape and sql[position]=='\\':position+=2;continue
                if sql[position]==quote:
                    position+=1
                    if position<length and sql[position]==quote:position+=1;continue
                    closed=True;break
                position+=1
            if not closed:raise BackupValidationError('Archive schema has unterminated quote')
            current.append(sql[start:position]);continue
        if character=='$':
            match=re.match(r'\$(?:[A-Za-z_][A-Za-z_0-9]*)?\$',sql[position:])
            if match:
                delimiter=match.group();end=sql.find(delimiter,position+len(delimiter))
                if end<0:raise BackupValidationError('Archive schema has unterminated dollar quote')
                end+=len(delimiter);current.append(sql[position:end]);position=end;continue
        if character==';':
            statement=''.join(current).strip()
            if statement:statements.append(statement)
            current=[];position+=1;continue
        current.append(character);position+=1
    if restriction is not None:raise BackupValidationError('Archive schema restriction mismatch')
    if ''.join(current).strip():raise BackupValidationError('Archive schema has unterminated statement')
    return tuple(statements)


_CREATE_TABLE=re.compile(r'(CREATE TABLE [^ ()]+ \()(.*)\)')


def _table_elements(body):
    """Split a CREATE TABLE body at top-level commas; quotes and parentheses stay intact."""
    elements=[];current=[];depth=0;position=0
    while position<len(body):
        character=body[position]
        if character in ("'",'"'):
            end=position+1
            while end<len(body):
                if body[end]==character:
                    if end+1<len(body) and body[end+1]==character:end+=2;continue
                    break
                end+=1
            if end>=len(body):raise BackupValidationError('Archive schema has unterminated quote')
            current.append(body[position:end+1]);position=end+1;continue
        if character=='(':depth+=1
        elif character==')':
            depth-=1
            if depth<0:raise BackupValidationError('Archive schema table definition is malformed')
        elif character==',' and depth==0:
            elements.append(''.join(current).strip());current=[];position+=1;continue
        current.append(character);position+=1
    if depth:raise BackupValidationError('Archive schema table definition is malformed')
    elements.append(''.join(current).strip())
    if any(not element for element in elements):raise BackupValidationError('Archive schema table definition is malformed')
    return elements


def canonical_table_statement(statement):
    """Column/constraint order inside one CREATE TABLE is not restore evidence.

    In-place upgrades append ALTERed columns while a fresh create_all declares them in
    model order; data restores use pg_dump COPY column lists. Each element (name, type,
    nullability, default, inline constraint) keeps its exact lexical text.
    """
    match=_CREATE_TABLE.fullmatch(statement)
    if not match:return statement
    return match.group(1)+' '+', '.join(sorted(_table_elements(match.group(2))))+' )'


def sql_statements(sql):
    # Dump DDL order is not executable evidence; archive SQL is never run.
    return Counter(canonical_table_statement(statement) for statement in sql_statement_sequence(sql))


def validate_schema_sql(original,canonical):
    if sql_statements(original)!=sql_statements(canonical):
        raise BackupValidationError('Archive schema/executables differ from source-pinned canonical schema; unsupported historical/client compatibility')


def validate_executables(connection, *, _contract=None):
    actual={}
    rows=connection.execute(text("""SELECT p.proname,p.prosrc,p.proconfig,p.prosecdef,p.provolatile,p.proparallel,
        p.prorettype::regtype::text,pg_get_function_identity_arguments(p.oid),l.lanname,
        p.proisstrict,p.proleakproof,p.prokind,p.proretset,p.provariadic,p.procost,p.prorows,p.prosupport::regproc::text FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        JOIN pg_language l ON l.oid=p.prolang WHERE n.nspname='public' ORDER BY p.proname,p.oid""")).all()
    expected=(canonical_functions() if _contract is None else canonical_functions(_contract=_contract))
    for row in rows:
        name=row[0]
        if name in actual or name not in expected:
            raise BackupValidationError('PostgreSQL schema contains noncanonical executable objects')
        values=tuple(row[1:]);wanted=expected[name]
        # Source equality remains lexical and case-sensitive, preserving literals.
        if (values[0]!=wanted[0] and sql_statement_sequence(values[0])!=sql_statement_sequence(wanted[0])) or values[1:]!=wanted[1:]:
            raise BackupValidationError('PostgreSQL schema contains noncanonical executable objects')
        actual[name]=values
    if set(actual)!=set(expected):raise BackupValidationError('PostgreSQL schema contains noncanonical executable objects')
    rows=connection.execute(text("""SELECT c.relname,t.tgname,pg_get_triggerdef(t.oid),f.proname,fn.nspname,t.tgenabled
        FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_proc f ON f.oid=t.tgfoid JOIN pg_namespace fn ON fn.oid=f.pronamespace
        WHERE n.nspname='public' AND NOT t.tgisinternal ORDER BY c.relname,t.tgname""")).all()
    actual={(r[0],r[1]):tuple(r[2:]) for r in rows}
    if actual!=(canonical_triggers() if _contract is None else canonical_triggers(_contract=_contract)) or len(actual)!=len(rows):
        raise BackupValidationError('PostgreSQL schema contains noncanonical executable objects')


def remove_trusted_guards(connection, *, _contract=None):
    for table,name in (canonical_triggers() if _contract is None else canonical_triggers(_contract=_contract)):
        connection.exec_driver_sql(f'DROP TRIGGER IF EXISTS {name} ON public.{table}')
    for name in (canonical_functions() if _contract is None else canonical_functions(_contract=_contract)):
        connection.exec_driver_sql(f'DROP FUNCTION IF EXISTS public.{name}()')


def install_trusted_guards(connection, *, _contract=None):
    for name,values in (canonical_functions() if _contract is None else canonical_functions(_contract=_contract)).items():
        source,config,*_=values
        supported={None:'',('search_path=pg_catalog, public',):' SET search_path TO pg_catalog, public',('search_path=pg_catalog',):' SET search_path TO pg_catalog'}
        key=None if config is None else tuple(config)
        if key not in supported:raise BackupValidationError('Unsupported canonical function configuration')
        settings=supported[key]
        connection.exec_driver_sql(f'CREATE FUNCTION public.{name}() RETURNS trigger LANGUAGE plpgsql{settings} AS $odin_trusted$'+source+'$odin_trusted$')
    # Function qualification avoids dependence on connection search_path.
    for definition,function,schema,enabled in (canonical_triggers() if _contract is None else canonical_triggers(_contract=_contract)).values():
        connection.exec_driver_sql(definition.replace('EXECUTE FUNCTION '+function+'()', 'EXECUTE FUNCTION public.'+function+'()'))
    if _contract is None:validate_executables(connection)
    else:validate_executables(connection,_contract=_contract)
