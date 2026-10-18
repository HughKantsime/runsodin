"""Qualified-source checks whose subject is the preserved029 bundle, not the live tree.

The 025/026 restore contract qualifies the exact sources that build its stage. Since
post-027 migrations, those sources run only from core/schema/preserved029/bundle, so the
tests asserting "these sources are the qualified ones" run against that bundle
(test_preserved029_contract_target.py), as 48f215e did for the frozen 027 profile.
"""
from pathlib import Path

import pytest

BUNDLE = Path(__file__).resolve().parents[2] / "backend/core/schema/preserved029/bundle"


def running_against_bundle() -> bool:
    import core
    return BUNDLE in Path(core.__file__).resolve().parents


# Deselected (not skipped) outside the bundle by tests/test_contracts/conftest.py, so the
# live-tree run reports no skips; test_preserved029_contract_target.py runs and counts them.
bundle_only = pytest.mark.preserved029_bundle
