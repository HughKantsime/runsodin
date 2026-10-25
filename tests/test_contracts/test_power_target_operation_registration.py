"""Current bootstrap must register027 without fixture allowlist overrides."""
import importlib
import pytest
from core.schema.migrator import MigrationError
from sqlalchemy import create_engine, inspect, text
from core.schema import bootstrap_database
from core.schema.bootstrap import RAW_REQUIRED_COLUMNS
from modules.system.postgres_canonical_executables import canonical_functions, canonical_triggers
M = importlib.import_module("core.schema.migrations.027_power_target_operations")


def test_current_bootstrap_registers_power_storage_and_survives_restart(tmp_path):
    url = "sqlite:///" + str(tmp_path / "registered-power.db")
    engine = create_engine(url)
    try:
        result = bootstrap_database(engine)
        assert M.MIGRATION_ID in result["applied"]
        assert {x["name"] for x in inspect(engine).get_columns(M.schema.TABLE)} == RAW_REQUIRED_COLUMNS[M.schema.TABLE]
        with engine.begin() as c:
            M.validate(c)
            assert c.execute(text("SELECT count(*) FROM odin_schema_migrations WHERE migration_id=:id"), {"id": M.MIGRATION_ID}).scalar_one() == 1
    finally:
        engine.dispose()
    restarted = create_engine(url)
    try:
        assert bootstrap_database(restarted)["applied"] == []
        with restarted.begin() as c:
            M.validate(c)
    finally:
        restarted.dispose()


def test_current_canonical_power_guards_are_exact_source_owned_union():
    functions, triggers = canonical_functions(), canonical_triggers()
    assert (len(functions), len(triggers)) == (29, 40)
    assert set(M.schema.pg_functions()) <= set(functions)
    assert all(type(key) is tuple and len(key)==2 for key in triggers)
    assert {(M.schema.TABLE,name) for name in M.schema.pg_triggers()} <= set(triggers)
    for name, definition in M.schema.pg_triggers().items():
        definition=definition.replace("INSERT OR UPDATE OR DELETE","INSERT OR DELETE OR UPDATE").replace("EXECUTE FUNCTION public.","EXECUTE FUNCTION ")
        assert triggers[(M.schema.TABLE,name)] == (definition, M.schema.FUNCTION, "public", "O")
    assert functions[M.schema.FUNCTION][1] == ["search_path=pg_catalog, public"]


def test_current_bootstrap_refuses_changed_power_schema_before_new_footprint(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + str(tmp_path / "changed-power.db"))
    monkeypatch.setattr(M, "SOURCE_SHA256", "0" * 64)
    try:
        with pytest.raises(MigrationError, match="Power storage source differs"):
            bootstrap_database(engine)
        assert M.schema.TABLE not in inspect(engine).get_table_names()
        with engine.connect() as c:
            assert c.execute(text("SELECT count(*) FROM odin_schema_migrations WHERE migration_id=:id"), {"id": M.MIGRATION_ID}).scalar_one() == 0
    finally:
        engine.dispose()
