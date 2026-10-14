"""Readonly parent oracle using literal027 catalogue, never live registry maps."""
import json
import re
from sqlalchemy import inspect,text
from .integrity import Preserved027Error

def sql_statement_sequence(sql):
    """Lexical equality only: preserve quoted text, comments outside it ignored.

    No semantic normalization, case folding or expression rewriting. Reject
    malformed quotes/comments and psql controls except paired dump restrictions.
    """
    if type(sql) is not str:
        raise Preserved027Error('Archive schema SQL is invalid')
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
            if depth:raise Preserved027Error('Archive schema has unterminated comment')
            space();continue
        if character=='\\':
            if ''.join(current).strip() or (position and sql[position-1]!='\n'):
                raise Preserved027Error('Archive schema contains unsupported control record')
            end=sql.find('\n',position);end=length if end<0 else end
            line=sql[position:end]
            match=re.fullmatch(r'\\(restrict|unrestrict) ([A-Za-z0-9]+)',line)
            if not match:raise Preserved027Error('Archive schema contains unsupported control record')
            action,token=match.groups()
            if action=='restrict':
                if restriction is not None or seen_restriction:raise Preserved027Error('Archive schema restriction mismatch')
                restriction=token;seen_restriction=True
            elif restriction!=token:
                raise Preserved027Error('Archive schema restriction mismatch')
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
            if not closed:raise Preserved027Error('Archive schema has unterminated quote')
            current.append(sql[start:position]);continue
        if character=='$':
            match=re.match(r'\$(?:[A-Za-z_][A-Za-z_0-9]*)?\$',sql[position:])
            if match:
                delimiter=match.group();end=sql.find(delimiter,position+len(delimiter))
                if end<0:raise Preserved027Error('Archive schema has unterminated dollar quote')
                end+=len(delimiter);current.append(sql[position:end]);position=end;continue
        if character==';':
            statement=''.join(current).strip()
            if statement:statements.append(statement)
            current=[];position+=1;continue
        current.append(character);position+=1
    if restriction is not None:raise Preserved027Error('Archive schema restriction mismatch')
    if ''.join(current).strip():raise Preserved027Error('Archive schema has unterminated statement')
    return tuple(statements)

def validate_catalogue(connection,profile):
    if connection.dialect.name != "postgresql":raise Preserved027Error("Preserved027 PG required")
    i=inspect(connection)
    if sorted(i.get_table_names(schema="public")) != sorted(profile["schema"]):raise Preserved027Error("Preserved027 tables differ")
    if i.get_view_names(schema="public"):raise Preserved027Error("Preserved027 views refuse")
    for table,expected in profile["schema"].items():
        columns=sorted([c["name"],str(c["type"]),bool(c.get("nullable",True)),str(c.get("default"))] for c in i.get_columns(table,schema="public"))
        if columns!=expected["columns"]:raise Preserved027Error("Preserved027 columns differ")
    if [list(r) for r in connection.execute(text("SELECT migration_id,checksum,dialect FROM odin_schema_migrations ORDER BY migration_id"))] != profile["ledger"]:raise Preserved027Error("Preserved027 ledger differs")
    # Closed original capture projections; no OID literal copied between DBs.
    from . import CATALOG_QUERIES
    for name,query in CATALOG_QUERIES.items():
        if [list(r) for r in connection.execute(text(query))] != profile[name]:raise Preserved027Error("Preserved027 catalogue differs")
    rows=connection.execute(text("SELECT p.proname,p.prosrc,p.proconfig,p.prosecdef,p.provolatile,p.proparallel,p.prorettype::regtype::text,pg_get_function_identity_arguments(p.oid),l.lanname,p.proisstrict,p.proleakproof,p.prokind,p.proretset,p.provariadic,p.procost,p.prorows,p.prosupport::regproc::text FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace JOIN pg_language l ON l.oid=p.prolang WHERE n.nspname='public' ORDER BY p.proname,p.oid"))
    seen=set()
    for row in rows:
        name=row[0];actual=list(row[1:]);expected=profile["functions"].get(name)
        if name in seen or expected is None or actual[1:]!=expected[1:] or (actual[0]!=expected[0] and sql_statement_sequence(actual[0])!=sql_statement_sequence(expected[0])):raise Preserved027Error("Preserved027 function differs")
        seen.add(name)
    if seen!=set(profile["functions"]):raise Preserved027Error("Preserved027 functions incomplete")
