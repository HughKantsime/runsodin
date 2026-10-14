"""Cold-process historical package import must not evaluate newer027 source."""
import subprocess
import sys


def test_historical_profile_import_does_not_evaluate_power027_helper():
    program = """
import importlib.abc, sys
class BlockNewer(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'core.power_target_operation_schema':
            raise RuntimeError('newer027 helper evaluated before historical selection')
sys.meta_path.insert(0, BlockNewer())
import core.schema
from core.schema.pre026_restore_contract import INVENTORY, INVENTORY026
assert 'power_target_operations' not in INVENTORY['tables']
assert 'power_target_operations' not in INVENTORY026['tables']
assert 'core.power_target_operation_schema' not in sys.modules
"""
    result = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr



def test_current_power_operations_can_be_imported_in_a_fresh_process():
    result = subprocess.run([sys.executable, "-c", "import core.power_target_operations"], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr



def test_current_literal_power_inventory_matches_reviewed_schema_exactly():
    from core.schema.bootstrap import POWER_OPERATION_COLUMNS, RAW_REQUIRED_COLUMNS
    from core.power_target_operation_schema import COLUMNS, TABLE
    assert POWER_OPERATION_COLUMNS == COLUMNS
    assert RAW_REQUIRED_COLUMNS[TABLE] == frozenset(COLUMNS)
