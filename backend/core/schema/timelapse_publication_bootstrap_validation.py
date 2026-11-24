"""Versioned bootstrap entry points for the frozen 019 and 023 contracts."""
from sqlalchemy import inspect, text

from core.schema.migrator import MigrationError


def validate_retained(db):
    # Load recipe and validation helpers only after 019 has verified their bytes.
    from core.schema.timelapse_publication_bootstrap_retained import validate_retained as walk
    walk(db)


def validate_publications(module, db):
    module._source()
    module._boundary(db)
    module._previous.validate(db)
    module._owner.validate(db)
    for table in module.TABLES:
        if not inspect(db).has_table(table.name):
            raise MigrationError('Publication table missing')
        module._table(db, table)
    validate_retained(db)


def validate_membership(module, db, publication_transaction=None):
    module._source()
    module._boundary(db)
    if publication_transaction is None or publication_transaction is not db.get_transaction():
        validate_publications(module._previous, db)
    else:
        module._previous._source()
    for table in module.TABLES:
        if not inspect(db).has_table(table.name):
            raise MigrationError('Membership table missing')
        module._previous._table(db, table)
    if db.dialect.name == 'sqlite':
        expected = module.sqlite_triggers()
        actual = {name: db.execute(text("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=:name"),
                                   {'name': name}).scalar_one_or_none() for name in expected}
        if any(sql is None for sql in actual.values()):
            raise MigrationError('Membership trigger missing')
        if any(module._normal(actual[name]) != module._normal(sql) for name, sql in expected.items()):
            raise MigrationError('Membership trigger differs')
    else:
        _native_guards(module, db)
    module.validate_retained(db)


def _native_guards(module, db):
    for name, body in module.pg_functions().items():
        rows = db.execute(text("""
            SELECT p.prosrc,p.prosecdef,p.provolatile,p.proparallel,p.proisstrict,p.proleakproof,
                   p.proconfig,p.prorettype::regtype::text,p.pronargs,l.lanname
            FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
            JOIN pg_catalog.pg_language l ON l.oid=p.prolang
            WHERE n.nspname='public' AND p.proname=:name"""), {'name': name}).all()
        if rows != [(body, False, 'v', 'u', False, False, ['search_path=pg_catalog, public'], 'trigger', 0, 'plpgsql')]:
            raise MigrationError('Membership function differs')
    triggers = module.pg_triggers()
    for table in module.TABLES + (module._previous._contract.intent, module._previous._contract.objects):
        name = table.name + '_membership_guard'
        rows = db.execute(text("""
            SELECT t.tgenabled,pg_catalog.pg_get_triggerdef(t.oid),n.nspname,p.proname
            FROM pg_catalog.pg_trigger t JOIN pg_catalog.pg_proc p ON p.oid=t.tgfoid
            JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
            WHERE t.tgrelid=pg_catalog.to_regclass(:table) AND t.tgname=:name"""),
            {'table': 'public.' + table.name, 'name': name}).all()
        expected = triggers[name].replace('INSERT OR UPDATE OR DELETE', 'INSERT OR DELETE OR UPDATE').replace(
            'EXECUTE FUNCTION public.', 'EXECUTE FUNCTION ')
        if (len(rows) != 1 or rows[0][0] != 'O' or module._normal(rows[0][1]) != module._normal(expected)
                or rows[0][2:] != ('public', name)):
            raise MigrationError('Membership native trigger differs')
