"""Discover the live post-037 DDL footprint when constructing a pre-037 schema."""
import importlib
import re


def membership_dependents(registry):
    ids, modules, absent_tables = [], set(), {'education_memberships'}
    for migration_id, module_name in registry:
        if int(migration_id.split(':')[1].split('-')[0]) <= 37:
            continue
        ddl = getattr(importlib.import_module(module_name), 'DDL', {})
        if isinstance(ddl, dict) and any(
            re.search(r'\bREFERENCES\s+' + re.escape(table) + r'\s*\(', definition, re.I)
            for definition in ddl.values() for table in absent_tables
        ):
            ids.append(migration_id)
            modules.add(module_name)
            absent_tables.update(ddl)
    return ids, modules, absent_tables
