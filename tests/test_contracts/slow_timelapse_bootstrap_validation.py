"""Run explicitly with pytest on this file for the full-size SQLite history."""
import pytest

from tests.test_contracts.test_education_classes_migration import native_engine
from tests.test_contracts.test_timelapse_bootstrap_validation import check_history_scale


@pytest.mark.slow
@pytest.mark.parametrize('native_engine', ['sqlite'], indirect=True)
def test_publication_history_scale_120k(native_engine):
    check_history_scale(native_engine, (120000,))
