"""Review fix O11: the single-container SQLite restore coordinator and bootstrap run as odin.

They ran as root (only the PostgreSQL path used runuser), so a restore or migration
created root-owned state under /data until the later chown. Every database step of the
entrypoint now runs as the app user, after /data is handed to it."""
import re
from pathlib import Path

ENTRYPOINT = (Path(__file__).resolve().parents[2] / "docker" / "entrypoint.sh").read_text()


def _db_section():
    start = ENTRYPOINT.index("rollback_restore_on_startup_error() {")
    return ENTRYPOINT[start:ENTRYPOINT.index("trap - ERR\n\nchown")]


def test_every_restore_coordinator_and_bootstrap_call_runs_as_odin():
    section = _db_section()
    calls = re.findall(r"^[^#\n]*python3 -m (?:modules\.system\.restore_coordinator|scripts\.bootstrap_database)[^\n]*$",
                       section, re.M)
    assert calls, section
    for call in calls:
        assert "/usr/sbin/runuser -u odin -- python3 -m" in call, call


def test_data_is_handed_to_odin_before_the_database_steps():
    before = ENTRYPOINT[:ENTRYPOINT.index('if [ "${ODIN_DB_BOOTSTRAP_OWNER:-1}" = "1" ]')]
    assert "chown -R odin:odin /data" in before
