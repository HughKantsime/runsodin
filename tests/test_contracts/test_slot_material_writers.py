"""All live slot writers normalize protocol labels before enum-backed reads."""
from pathlib import Path
from types import SimpleNamespace
import ast
import re

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from core.base import FilamentType, slot_filament_value
from tests.test_contracts.test_education_review_workflow import review_db


@pytest.mark.parametrize('raw,expected', [('', 'empty'), (None, 'empty'), ('EMPTY','empty'),
    ('unknown','Unknown'), ('PLA+','PLA'), ('pla+','PLA'), ('Silk PLA','OTHER'),
    ('PA-S','PLA_SUPPORT'), ('PET-CF','PETG_CF'), ('PA6-GF','NYLON_GF'),
    ('ABS-GF','OTHER'), ('PLA-SILK','PLA'), ('PETG_CF','PETG_CF')])
def test_slot_value(raw, expected):
    assert slot_filament_value(raw) == expected
    assert FilamentType(slot_filament_value(raw)).value == expected


@pytest.mark.parametrize('writer', ['moonraker','bambu','anycubic'])
@pytest.mark.parametrize('existing', [False, True])
@pytest.mark.parametrize('raw,expected', [('PLA+','PLA'), ('pla+','PLA'), ('Silk PLA','OTHER'), ('','empty')])
def test_writer_fresh_printer_serialization(review_db, monkeypatch, writer, existing, raw, expected):
    from modules.printers.models import Printer
    from modules.printers.schemas import PrinterResponse
    db=review_db
    db.execute(text("UPDATE printers SET slot_count=4,tags='[]',timelapse_enabled=0,shared=0"))
    if not existing:
        db.execute(text('DELETE FROM filament_slots WHERE printer_id=9'))
    db.commit()
    engine=db.get_bind()
    if writer=='moonraker':
        from modules.printers.monitors import moonraker_monitor as module
        monkeypatch.setattr(module,'engine',engine)
        monitor=object.__new__(module.MoonrakerMonitor)
        monitor.printer_id=9
        monitor.name='fixture'
        monitor._sync_filament_slots([SimpleNamespace(gate=0,material=raw,color_hex=None,name='')])
    elif writer=='anycubic':
        from modules.printers.monitors import anycubic_lan_monitor as module
        monkeypatch.setattr(module,'engine',engine)
        module.sync_slots(9,[{'id':0,'slots':[{'index':0,'type':raw}]}])
    else:
        from modules.printers.monitors.ams_inventory import sync_ams_inventory
        with engine.begin() as connection:
            sync_ams_inventory(connection.connection,9,[{'id':'0','tray':[{'id':'0','tray_type':raw}]}])
    with Session(engine) as fresh:
        printers=[PrinterResponse.model_validate(p).model_dump(mode='json') for p in fresh.query(Printer).all()]
        slots=next(p for p in printers if p['id']==9)['filament_slots']
        if writer=='bambu' and raw and raw.upper()!='PLA':
            # AMS sync treats a non-canonical tray label as an unreported delta:
            # nothing is written, and whatever is stored still serializes.
            assert all(FilamentType(slot['filament_type']) for slot in slots)
            return
        assert slots[0]['filament_type']==expected


def _writer_violations(source):
    """Track normalized values through local SQL bindings and ORM assignments."""
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    violations = []

    def scope(node):
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return node
        return tree

    def definitions(name, owner):
        return [n.value for n in ast.walk(owner) if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == name for t in n.targets)]

    def safe(value, owner, seen=frozenset()):
        if isinstance(value, ast.Call):
            if isinstance(value.func, ast.Name) and value.func.id == 'slot_filament_value':
                return True
            if isinstance(value.func, ast.Name) and value.func.id == 'FilamentType' and value.args:
                return safe(value.args[0], owner, seen)
        if isinstance(value, ast.Name) and value.id not in seen:
            values = definitions(value.id, owner)
            return bool(values) and all(safe(v, owner, seen | {value.id}) for v in values)
        if isinstance(value, ast.Subscript) and isinstance(value.value, ast.Name):
            values = definitions(value.value.id, owner)
            key = value.slice.value if isinstance(value.slice, ast.Constant) else None
            selected = [dict_value(v, key) for v in values]
            return bool(selected) and all(v is not None and safe(v, owner, seen) for v in selected)
        return False

    def dict_value(value, key):
        if isinstance(value, ast.Dict):
            return next((v for k, v in zip(value.keys, value.values)
                         if isinstance(k, ast.Constant) and k.value == key), None)
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == 'dict':
            return next((k.value for k in value.keywords if k.arg == key), None)
        return None

    def binding(params, key, owner):
        if isinstance(params, ast.Name):
            values = definitions(params.id, owner)
            return bool(values) and all(binding(v, key, owner) for v in values)
        if isinstance(key, int) and isinstance(params, (ast.Tuple, ast.List)):
            return key < len(params.elts) and safe(params.elts[key], owner)
        value = dict_value(params, key)
        return value is not None and safe(value, owner)

    def sql_string(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            return ''.join(v.value if isinstance(v, ast.Constant) else 'NOW()' for v in node.values)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'text' and node.args:
            return sql_string(node.args[0])
        return ''

    for node in ast.walk(tree):
        owner = scope(node)
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Attribute) and t.attr == 'filament_type' for t in node.targets):
            if not safe(node.value, owner):
                violations.append(node.lineno)
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id == 'FilamentSlot':
            for kw in node.keywords:
                if kw.arg == 'filament_type' and not safe(kw.value, owner):
                    violations.append(node.lineno)
        # SQLAlchemy Core insert().values() for the dynamic filament_slots table.
        if isinstance(node.func, ast.Attribute) and node.func.attr == 'values':
            for kw in node.keywords:
                if kw.arg == 'filament_type' and not safe(kw.value, owner):
                    violations.append(node.lineno)
        if isinstance(node.func, ast.Name) and node.func.id == 'setattr' and len(node.args) >= 3:
            target, field, value = node.args[:3]
            if isinstance(target, ast.Name) and target.id in ('slot', 'db_slot'):
                # Dynamic patch fields must normalize the material branch.
                if 'slot_filament_value(' not in ast.unparse(value):
                    violations.append(node.lineno)
        is_execute = isinstance(node.func, ast.Attribute) and node.func.attr == 'execute'
        is_insert = isinstance(node.func, ast.Name) and node.func.id == '_insert_id'
        if not (is_execute or is_insert):
            continue
        for index, arg in enumerate(node.args):
            sql = sql_string(arg)
            update = re.search(r'UPDATE\s+filament_slots\s+SET\b.*?\bfilament_type\s*=\s*([^,\s]+)', sql, re.I | re.S)
            insert = re.search(r'INSERT\s+INTO\s+filament_slots\s*\(([^)]+)\)\s*VALUES\s*\((.*)', sql, re.I | re.S)
            token = None
            position = None
            if update:
                token = update.group(1)
                position = sql[:update.start(1)].count('?')
            elif insert:
                columns = [c.strip().lower() for c in insert.group(1).split(',')]
                if 'filament_type' in columns:
                    values = insert.group(2).split(',')
                    offset = columns.index('filament_type')
                    token = values[offset].strip().rstrip(')')
                    position = ','.join(values[:offset]).count('?')
            if token is None:
                continue
            key = position if token == '?' else token[1:] if token.startswith(':') else None
            if key is None or index + 1 >= len(node.args) or not binding(node.args[index + 1], key, owner):
                violations.append(node.lineno)
    return sorted(set(violations))


def test_slot_writer_census():
    backend = Path(__file__).resolve().parents[2] / 'backend'
    violations = []
    for path in backend.rglob('*.py'):
        # Frozen restore bundles must stay intact; live migrations are scanned too.
        if any(part.startswith('preserved') for part in path.relative_to(backend).parts):
            continue
        for line in _writer_violations(path.read_text()):
            violations.append((str(path.relative_to(backend)), line))
    assert violations == []


@pytest.mark.parametrize('statement', [
    "slot.filament_type = raw",
    "FilamentSlot(filament_type=raw)",
    "db.execute(text('UPDATE filament_slots SET color=:c, filament_type=:ft WHERE id=:id'), dict(c='red', ft=raw, id=1))",
    "db.execute('INSERT INTO filament_slots(printer_id,filament_type) VALUES(?,?)', (1,raw))",
    "slot_table.insert().values(filament_type=raw)",
])
def test_census_detects_each_unmapped_writer(statement):
    assert _writer_violations('def write(raw):\n    ' + statement)
    normalized = re.sub(r'\braw\b', 'slot_filament_value(raw)', statement)
    assert not _writer_violations('def write(raw):\n    ' + normalized)


def test_census_checks_each_binding_even_after_another_normalized_write():
    source = """def write(raw):
    mapped = slot_filament_value(raw)
    db.execute('UPDATE filament_slots SET filament_type=? WHERE id=?', (mapped, 1))
    db.execute('UPDATE filament_slots SET filament_type=? WHERE id=?', (raw, 2))
"""
    assert _writer_violations(source) == [4]
