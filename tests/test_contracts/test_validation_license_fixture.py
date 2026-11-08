"""Validation licensing changes only the owned process and restores Community."""
import os
from pathlib import Path

import pytest
from fastapi import HTTPException

from ops.release_gate.license_fixture import signed_tier


@pytest.mark.parametrize('tier,feature', [('pro', 'csv_export'), ('education', 'user_groups')])
def test_validation_license_is_verified_and_restores_on_failure(tier_license, tier, feature):
    import license_manager as lm
    tier_license(None)
    original = (lm.ODIN_PUBLIC_KEY, lm.LICENSE_DIR, lm.get_license())
    with pytest.raises(HTTPException):
        lm.require_feature(feature)
    with pytest.raises(RuntimeError, match='synthetic failure'):
        with signed_tier(tier) as info:
            path = Path(os.environ['ODIN_LICENSE_FILE'])
            assert info.valid and info.tier == tier
            assert lm.load_license().valid
            lm.require_feature(feature)
            raise RuntimeError('synthetic failure')
    assert not path.exists()
    assert (lm.ODIN_PUBLIC_KEY, lm.LICENSE_DIR, lm.get_license()) == original
    assert 'ODIN_LICENSE_FILE' not in os.environ
    with pytest.raises(HTTPException):
        lm.require_feature(feature)
