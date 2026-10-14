"""Legacy frame audience uses historical provenance, not today's printer owner.

Unknown-owner captures remain platform-maintenance-only until the immutable
capture registry exists. These execute actual file reads and refusals rather
than require an obsolete current-printer source-code predicate.
"""
import asyncio
import pytest
from fastapi import HTTPException
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_vision_training_acceptance import _owned_frame_route,_export_frame


@pytest.mark.parametrize('user',[
    {'id':3,'role':'admin','group_id':1},
    {'id':5,'role':'admin','group_id':2},
    {'id':4,'role':'viewer','group_id':None},
])
def test_tenant_or_unassigned_viewer_cannot_read_unknown_owner_image(review_db,tmp_path,user):
    root=tmp_path/'frames';_export_frame(review_db,root)
    with pytest.raises(HTTPException) as error:
        asyncio.run(_owned_frame_route(root)(9,'frame.jpg',current_user=user,db=review_db))
    assert error.value.status_code==404


def test_platform_maintenance_returns_exact_owned_jpeg_bytes(review_db,tmp_path):
    root=tmp_path/'frames';_export_frame(review_db,root)
    response=asyncio.run(_owned_frame_route(root)(9,'frame.jpg',current_user={'id':3,'role':'admin','group_id':None},db=review_db))
    assert response.media_type=='image/jpeg'
    assert response.body==(root/'9'/'frame.jpg').read_bytes()
