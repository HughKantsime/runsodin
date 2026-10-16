"""Write real 025, 026 and 027-era PostgreSQL archives with the preserved029 sources.

Run with PYTHONPATH=<backend>/core/schema/preserved029/bundle only: the archives are
produced by exactly the qualified sources of each era (the 025/026 contract stages and
the 027-era ordinary bootstrap), never by live code. Usage:
  fixtures.py <admin_url> <base_url_without_db> <password_file> <out_dir>
"""
import json
from pathlib import Path
import sys
import uuid

from sqlalchemy import create_engine, text

import core
from core.database_config import create_database_engine
from core.schema import bootstrap_database
from core.schema.pre026_restore_contract import PRE026_RESTORE025, PRE027_RESTORE026, bootstrap_stage, check_sources, owned_stage_database
from modules.system import postgres_backup_service as b


def main():
    admin_url, base_url, password_file, out = sys.argv[1:5]
    out = Path(out)
    assert "/preserved029/bundle/" in core.__file__, core.__file__
    check_sources(PRE026_RESTORE025); check_sources(PRE027_RESTORE026)
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    facts = {}
    try:
        for era in (25, 26, 27):
            name = "odin_validate_" + uuid.uuid4().hex[:20]
            with admin.connect() as c:
                c.exec_driver_sql('CREATE DATABASE "' + name + '"')
            url = base_url + "/" + name
            engine = create_database_engine(url, role="restore", password_file=password_file)
            try:
                if era == 27:
                    bootstrap_database(engine)
                else:
                    bootstrap_stage(owned_stage_database(engine, name, PRE026_RESTORE025 if era == 25 else PRE027_RESTORE026))
                with engine.begin() as c:
                    c.execute(text("INSERT INTO system_config(key,value) VALUES('historical_marker',:v)"), {"v": json.dumps(f"source{era}")})
                    c.execute(text("INSERT INTO filament_library(id,brand,name,material) VALUES(80,'Owned','Era fixture','PLA'),(81,'Owned','Second','PETG')"))
                    ledger = sorted(row[0] for row in c.execute(text("SELECT migration_id FROM odin_schema_migrations")))
                def dump(path):
                    b._run_pg_tool(["pg_dump", "--format=custom", "--schema", "public", "--no-owner", "--no-privileges", "--no-comments",
                                    "--file", str(path), "--dbname", b._credentialless_cli_url(url, "odin-backup")], url, password_file, out)
                archive = out / f"era{era}.dump"
                dump(archive)
                facts[era] = {"archive": str(archive), "sha256": b._sha256(archive), "ledger": ledger}
                # Tampered variants, written after the genuine archive of the same database.
                tamper = {27: ("bad-checksum", "UPDATE odin_schema_migrations SET checksum=repeat('0',64) WHERE migration_id='python:027-power-target-operations'"),
                          26: ("extra-table", "CREATE TABLE public.unqualified_extra(id integer)")}.get(era)
                if tamper:
                    with engine.begin() as c:
                        c.exec_driver_sql(tamper[1])
                    path = out / f"era{era}-{tamper[0]}.dump"
                    dump(path)
                    facts[era]["tampered"] = str(path)
            finally:
                engine.dispose()
                with admin.connect() as c:
                    c.exec_driver_sql('DROP DATABASE IF EXISTS "' + name + '" WITH (FORCE)')
    finally:
        admin.dispose()
    print(json.dumps(facts))


if __name__ == "__main__":
    main()
