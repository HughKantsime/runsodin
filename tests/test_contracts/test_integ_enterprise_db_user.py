"""Enterprise compose on PostgreSQL boots: the entrypoint's database steps run as odin.

The PostgreSQL restore gate (core/postgres_restore_gate.py) accepts a data directory
owned by root or by the reading process. /data is odin's (Dockerfile, and the named
volume is seeded from it), so the bootstrap migration run (api) and the schema wait
(monitors, vision, reports) refused as root and the API never became healthy. Live
proof: test_integ_enterprise_go2rtc_live.py (fails at "api is unhealthy" before).
"""
from pathlib import Path

ENTRYPOINT = (Path(__file__).resolve().parents[2] / "docker" / "entrypoint.sh").read_text()


def test_postgres_bootstrap_runs_as_the_app_user():
    start = ENTRYPOINT.index('if [ "${ODIN_DB_BOOTSTRAP_OWNER:-1}" = "1" ]')
    block = ENTRYPOINT[start:ENTRYPOINT.index("python3 -m scripts.wait_for_database_schema", start)]
    postgres = block[block.rindex("postgresql://*|postgres://*)"):]
    assert postgres.split(";;")[0].strip().endswith(
        "ODIN_DB_ROLE=bootstrap /usr/sbin/runuser -u odin -- python3 -m scripts.bootstrap_database")


def test_split_services_wait_for_the_schema_as_the_app_user():
    assert "/usr/sbin/runuser -u odin -- python3 -m scripts.wait_for_database_schema" in ENTRYPOINT
    assert "\n    python3 -m scripts.wait_for_database_schema" not in ENTRYPOINT


def test_restore_gate_refuses_a_data_directory_owned_by_another_user(tmp_path, monkeypatch):
    import os
    import pytest
    from core import postgres_restore_gate as gate
    data = tmp_path / "data"
    data.mkdir(mode=0o755)
    gate.assert_postgres_restore_ready(str(data))  # owned by this process: accepted
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)  # e.g. root reading odin's /data
    if os.getuid() == 0:
        pytest.skip("running as root: every directory is root-acceptable")
    with pytest.raises(gate.RestoreConsumerBlocked):
        gate.assert_postgres_restore_ready(str(data))
