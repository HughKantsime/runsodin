"""Integer flag columns work on both dialects (PostgreSQL native probe + SQLite here).

vision_models.is_active, the vision_settings flags and printer_profiles.is_shared/is_default
are INTEGER 0/1 (no schema change under the post-027 rules). PostgreSQL refused
"vision_models.is_active IS TRUE" (the Vigil AI registry never loaded), "IS NOT TRUE" on
model retirement, and booleans bound into printer_profiles' integer columns. The same probe
(ops/database_parity/integer_flags_native.py) runs against an owned PostgreSQL through
ops/database_parity/run_integer_flags.py; this test runs it on SQLite and guards the SQL.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INTEGER_FLAGS = {
    "vision_models": ("is_active",),
    "vision_settings": ("enabled", "spaghetti_enabled", "first_layer_enabled", "detachment_enabled",
                        "build_plate_empty_enabled", "auto_pause", "collect_training_data"),
    "printer_profiles": ("is_shared", "is_default"),
}


def test_integer_flag_probe_passes_on_sqlite(tmp_path):
    env = {**os.environ, "INTF_SQLITE_PATH": str(tmp_path / "odin.db"), "INTF_BACKEND": str(ROOT / "backend"),
           "PYTHONPATH": f"{ROOT / 'backend'}:{ROOT}", "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": str(tmp_path)}
    env.pop("DATABASE_URL", None)
    result = subprocess.run([sys.executable, str(ROOT / "ops" / "database_parity" / "integer_flags_native.py")],
                            cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)
    assert result.returncode == 0 and "integer-flags-native-pass" in result.stdout, result.stdout[-3000:] + result.stderr[-3000:]


def test_no_boolean_literal_comparison_on_integer_flag_columns():
    """Statements naming an integer-flag table never compare its flags with TRUE/FALSE."""
    offenders = []
    for path in (ROOT / "backend").rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if "/schema/preserved" in rel or rel.endswith("pre026_restore_contract.py"):
            continue
        source = path.read_text()
        for table, columns in INTEGER_FLAGS.items():
            if table not in source:
                continue
            for column in columns:
                for match in re.finditer(rf"\b{column}\s+IS\s+(NOT\s+)?(TRUE|FALSE)\b|\b{column}\s*=\s*(TRUE|FALSE)\b",
                                         source, re.I):
                    window = source[max(0, match.start() - 400):match.end()]
                    if table in window:
                        offenders.append(f"{rel}: {match.group(0)} ({table})")
    orm = (ROOT / "backend" / "modules" / "vision" / "routes" / "vision_models.py").read_text()
    if re.search(r"is_active\.is_(not)?\((True|False)\)", orm):
        offenders.append("vision_models.py: VisionModel.is_active compared with a Python boolean")
    assert offenders == []
