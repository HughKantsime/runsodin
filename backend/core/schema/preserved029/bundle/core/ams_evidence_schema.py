"""Durable AMS evidence contract, not yet registered in database bootstrap.

Historical records have no printer deletion cascade. Only the current pointer
references the live printer. Authority transitions and immutable writes require
the separately reviewed service; table shape alone is not admission authority.
"""
from sqlalchemy import (MetaData, Table, Column, Integer, String, DateTime,
                        Identity, CheckConstraint, UniqueConstraint,
                        ForeignKeyConstraint)


metadata = MetaData()
# Frozen historical DDL vocabulary. New supported labels require an explicit
# additive schema revision rather than changing an adopted contract implicitly.
SCHEMA_MATERIALS = (
    'ABS', 'ABS-GF', 'ASA', 'HIPS', 'PA', 'PA-CF', 'PA-GF', 'PA12-CF',
    'PA6', 'PA6-CF', 'PA6-GF', 'PC', 'PC-ABS', 'PC-CF', 'PETG', 'PETG-CF',
    'PLA', 'PLA-AERO', 'PLA-CF', 'PLA-MATTE', 'PLA-S', 'PLA-SILK', 'PVA',
    'SUPPORT', 'SUPPORT-G', 'SUPPORT-W', 'TPU', 'TPU-83A', 'TPU-95A',
)
# Reference-only shape for the existing live table. Never create through this
# metadata's create_all: callers must create only EVIDENCE_TABLES.
Table('printers', metadata, Column('id', Integer, primary_key=True))


def _hex(column, size):
    expression = column
    for digit in '0123456789abcdef':
        expression = "replace(%s,'%s','')" % (expression, digit)
    return "length(%s)=%s AND %s=''" % (column, size, expression)


generations = Table(
    'ams_read_generations', metadata,
    Column('generation_id', String(32), primary_key=True, nullable=False),
    Column('printer_id', Integer, nullable=False),
    Column('owner_org_id', Integer),
    Column('configuration_key', String(64), nullable=False),
    Column('runtime_incarnation', String(32), nullable=False),
    Column('started_at', DateTime(timezone=True), nullable=False),
    Column('state', String(16), nullable=False),
    Column('completed_at', DateTime(timezone=True)),
    Column('invalidated_at', DateTime(timezone=True)),
    UniqueConstraint('generation_id', 'printer_id', 'runtime_incarnation',
                     name='uq_ams_generation_pointer'),
    CheckConstraint('printer_id>0 AND (owner_org_id IS NULL OR owner_org_id>0)',
                    name='ck_ams_generation_owner'),
    CheckConstraint(' AND '.join(_hex(key, size) for key, size in (
        ('generation_id', 32), ('configuration_key', 64),
        ('runtime_incarnation', 32))), name='ck_ams_generation_keys'),
    CheckConstraint(
        "(state='acquiring' AND completed_at IS NULL AND invalidated_at IS NULL) OR "
        "(state IN ('ready','failed') AND completed_at IS NOT NULL AND "
        "completed_at>=started_at AND invalidated_at IS NULL) OR "
        "(state='invalidated' AND completed_at IS NOT NULL AND "
        "completed_at>=started_at AND invalidated_at IS NOT NULL AND "
        "invalidated_at>=completed_at)", name='ck_ams_generation_phase'),
)

current_reads = Table(
    'ams_current_reads', metadata,
    Column('printer_id', Integer, primary_key=True, nullable=False),
    Column('generation_id', String(32), nullable=False),
    Column('runtime_incarnation', String(32), nullable=False),
    ForeignKeyConstraint(['printer_id'], ['printers.id'], ondelete='CASCADE',
                         name='fk_ams_current_printer'),
    ForeignKeyConstraint(['generation_id', 'printer_id', 'runtime_incarnation'],
                         ['ams_read_generations.generation_id',
                          'ams_read_generations.printer_id',
                          'ams_read_generations.runtime_incarnation'],
                         name='fk_ams_current_generation'),
)

packets = Table(
    'ams_report_packets', metadata,
    Column('packet_sequence', Integer, Identity(always=True, cycle=False),
           primary_key=True, nullable=False),
    Column('packet_id', String(32), nullable=False),
    Column('generation_id', String(32), nullable=False),
    Column('received_at', DateTime(timezone=True), nullable=False),
    Column('receipt_provenance', String(32), nullable=False),
    UniqueConstraint('packet_id', name='uq_ams_packet_id'),
    ForeignKeyConstraint(['generation_id'], ['ams_read_generations.generation_id'],
                         name='fk_ams_packet_generation'),
    CheckConstraint('packet_sequence>0', name='ck_ams_packet_sequence'),
    CheckConstraint(_hex('packet_id', 32), name='ck_ams_packet_key'),
    CheckConstraint("receipt_provenance='db-clock-estimate-v1'",
                    name='ck_ams_packet_provenance'),
    sqlite_autoincrement=True,
)

trays = Table(
    'ams_report_trays', metadata,
    Column('packet_sequence', Integer, primary_key=True, nullable=False),
    Column('slot_number', Integer, primary_key=True, nullable=False),
    Column('unit_id', Integer, nullable=False),
    Column('tray_id', Integer, nullable=False),
    Column('material', String(32)),
    Column('material_state', String(16), nullable=False),
    # NULL means unknown. Reported labels do not prove mechanical readiness.
    Column('mechanically_ready', Integer),
    ForeignKeyConstraint(['packet_sequence'], ['ams_report_packets.packet_sequence'],
                         name='fk_ams_tray_packet'),
    UniqueConstraint('packet_sequence', 'unit_id', 'tray_id',
                     name='uq_ams_tray_physical'),
    CheckConstraint('unit_id BETWEEN 0 AND 3 AND tray_id BETWEEN 0 AND 3 AND '
                    'slot_number=unit_id*4+tray_id+1', name='ck_ams_tray_identity'),
    CheckConstraint('mechanically_ready IS NULL', name='ck_ams_tray_readiness'),
    CheckConstraint(
        "(material_state='reported' AND material IS NOT NULL AND material IN (" +
        ','.join("'%s'" % item for item in SCHEMA_MATERIALS) + ")) OR "
        "(material_state IN ('explicit_empty','unknown','invalid') AND material IS NULL)",
        name='ck_ams_tray_material'),
)

EVIDENCE_TABLES = (generations, current_reads, packets, trays)
