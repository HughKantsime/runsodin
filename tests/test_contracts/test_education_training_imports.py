"""The split public facades and their helper modules must import in either order."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('module', [
    'core.license_device_key', 'core.license_info', 'core.license_trial_message', 'core.license_tiers',
    'modules.organizations.education_dispatch_policy',
    'modules.organizations.education_observation_policy',
    'modules.organizations.education_recovery_policy',
    'modules.organizations.education_submission_graph',
    'modules.organizations.education_upload_storage',
])
def test_helper_can_be_imported_before_its_public_facade(module):
    backend = Path(__file__).resolve().parents[2] / 'backend'
    result = subprocess.run([sys.executable, '-c', f'import {module}'],
                            cwd=backend, env={**os.environ, 'PYTHONPATH': str(backend)},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
