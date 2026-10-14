"""Owned offline native PostgreSQL schema proof, not migration/admission proof.

Run with PYTHONPATH=backend python ops/database_parity/ams_schema_probe.py.
Only this probe's random container is removed. No host ports or durable volumes.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import time
import uuid

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable
from core.ams_evidence_schema import EVIDENCE_TABLES

IMAGE = 'postgres@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea'


def run(*args, input=None):
    return subprocess.run(args, input=input, text=True, capture_output=True,
                          timeout=60, check=True).stdout


def rejection(sql, condition):
    return "DO $$ BEGIN BEGIN " + sql + "; RAISE EXCEPTION 'expected refusal missing'; " + \
        "EXCEPTION WHEN " + condition + " THEN NULL; END; END $$;"


def main():
    name = 'odin-ams-schema-' + uuid.uuid4().hex[:12]
    statements = ['CREATE TABLE printers(id INTEGER PRIMARY KEY);',
                  'INSERT INTO printers VALUES(1),(2);']
    statements.extend(str(CreateTable(table).compile(dialect=postgresql.dialect())) + ';'
                      for table in EVIDENCE_TABLES)
    statements.append("INSERT INTO ams_read_generations VALUES(repeat('a',32),1,1,repeat('b',64),repeat('c',32),'2026-10-12T00:00:00Z','acquiring',NULL,NULL);")
    refusals = [
        ("INSERT INTO ams_current_reads VALUES(2,repeat('a',32),repeat('c',32))", 'foreign_key_violation'),
        ("INSERT INTO ams_current_reads VALUES(1,repeat('a',32),repeat('f',32))", 'foreign_key_violation'),
        ("UPDATE ams_read_generations SET state='ready'", 'check_violation'),
        ("UPDATE ams_read_generations SET state='invalidated'", 'check_violation'),
        ("UPDATE ams_read_generations SET state='failed',completed_at='2026-10-11T00:00:00Z'", 'check_violation'),
        ("UPDATE ams_read_generations SET owner_org_id=0", 'check_violation'),
        ("UPDATE ams_read_generations SET configuration_key=repeat('x',64)", 'check_violation'),
    ]
    statements.extend(rejection(*item) for item in refusals)
    statements.extend([
        "INSERT INTO ams_current_reads VALUES(1,repeat('a',32),repeat('c',32));",
        "INSERT INTO ams_report_packets(packet_id,generation_id,received_at,receipt_provenance) VALUES(repeat('d',32),repeat('a',32),'2026-10-12T00:00:00Z','db-clock-estimate-v1');",
        "CREATE TEMP TABLE old_sequence AS SELECT packet_sequence FROM ams_report_packets;",
        "DELETE FROM ams_report_packets;",
        "INSERT INTO ams_report_packets(packet_id,generation_id,received_at,receipt_provenance) VALUES(repeat('e',32),repeat('a',32),'2026-10-12T00:00:00Z','db-clock-estimate-v1');",
        "DO $$ BEGIN IF (SELECT packet_sequence FROM ams_report_packets)<=(SELECT packet_sequence FROM old_sequence) THEN RAISE EXCEPTION 'sequence reused'; END IF; END $$;",
    ])
    bad_trays = [
        (2,0,0,"'PETG'","'reported'",'NULL'),
        (1,4,0,"'PETG'","'reported'",'NULL'),
        (1,0,-1,"'PETG'","'reported'",'NULL'),
        (1,0,0,"'petg'","'reported'",'NULL'),
        (1,0,0,'NULL',"'reported'",'NULL'),
        (1,0,0,"'PETG'","'unknown'",'NULL'),
        (1,0,0,"'PETG'","'reported'",'1'),
    ]
    for slot, unit, tray, material, state, ready in bad_trays:
        statements.append(rejection(
            'INSERT INTO ams_report_trays SELECT packet_sequence,%s,%s,%s,%s,%s,%s FROM ams_report_packets' %
            (slot,unit,tray,material,state,ready), 'check_violation'))
    statements.extend([
        "INSERT INTO ams_report_trays SELECT packet_sequence,1,0,0,'PETG','reported',NULL FROM ams_report_packets;",
        rejection('DELETE FROM ams_report_packets', 'foreign_key_violation'),
        "DELETE FROM printers WHERE id=1;",
        "DO $$ BEGIN IF (SELECT count(*) FROM ams_current_reads)<>0 OR (SELECT count(*) FROM ams_read_generations)<>1 OR (SELECT count(*) FROM ams_report_trays)<>1 THEN RAISE EXCEPTION 'history or pointer deletion wrong'; END IF; END $$;",
        "DO $$ BEGIN IF EXISTS(SELECT 1 FROM pg_constraint WHERE conrelid IN ('ams_current_reads'::regclass,'ams_report_packets'::regclass,'ams_report_trays'::regclass) AND contype='f' AND (NOT convalidated OR condeferrable)) THEN RAISE EXCEPTION 'foreign key enforcement wrong'; END IF; END $$;",
        "DO $$ BEGIN IF (SELECT attidentity FROM pg_attribute WHERE attrelid='ams_report_packets'::regclass AND attname='packet_sequence')<>'a' OR (SELECT seqcycle FROM pg_sequence WHERE seqrelid=pg_get_serial_sequence('ams_report_packets','packet_sequence')::regclass) THEN RAISE EXCEPTION 'native identity contract wrong'; END IF; END $$;",
        "SELECT 'native-ams-schema-pass';",
    ])
    sql = '\n'.join(statements)
    output_dir = Path('artifacts/edu-workflow-check')
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir/'ams-schema-native-current.sql').write_text(sql)
    receipt = dict(image=IMAGE, scope='native schema only, not adoption or runtime authority',
                   sql_sha256=hashlib.sha256(sql.encode()).hexdigest(), passed=False,
                   schema_sha256=hashlib.sha256(Path('backend/core/ams_evidence_schema.py').read_bytes()).hexdigest())
    try:
        run('docker','run','-d','--name',name,'--network','none',
            '--tmpfs','/var/lib/postgresql/data:rw,size=128m',
            '-e','POSTGRES_HOST_AUTH_METHOD=trust',IMAGE)
        deadline = time.monotonic()+30
        while True:
            ready = subprocess.run(['docker','exec',name,'pg_isready','-U','postgres'],
                                   capture_output=True, timeout=5)
            if ready.returncode == 0:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError('Owned PostgreSQL did not become ready')
            time.sleep(.2)
        output = run('docker','exec','-i',name,'psql','-U','postgres','-v','ON_ERROR_STOP=1',input=sql)
        (output_dir/'ams-schema-native-current.log').write_text(output)
        if 'native-ams-schema-pass' not in output:
            raise RuntimeError('Native schema proof missing')
        receipt['passed'] = True
    finally:
        cleanup = subprocess.run(['docker','rm','-f',name], capture_output=True, timeout=20)
        receipt['owned_container_removed'] = cleanup.returncode == 0
        (output_dir/'ams-schema-native-current.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps(receipt))


if __name__ == '__main__':
    main()
