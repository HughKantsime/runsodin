"""Historical/imported dangling aliases must not grant private content access."""
import pytest
from fastapi import HTTPException
from sqlalchemy import text
from tests.test_contracts.test_valuation_content_visibility import db,visible


def test_missing_current_model_is_not_generic_content_authority(db):
    db.execute(text('DELETE FROM models WHERE id=3'))
    with pytest.raises(HTTPException) as error:
        visible(db)
    assert error.value.status_code==404
