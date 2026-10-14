"""Authenticated model computed-value contract, owned isolated models."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_api_token_delegation_behavior import token_client, review_db


@pytest.mark.parametrize('cost,markup,hours,bed,per_hour', [(5,0,2,0,0),(0,50,2,0,0),(5,50,2,2.5,1.25),(None,50,2,None,None),(5,None,2,None,None),(5,50,0,2.5,None),(0,50,None,0,None)])
def test_model_response_preserves_known_zero_value(token_client, review_db, cost, markup, hours, bed, per_hour):
    from core.auth import create_access_token
    from modules.models_library.models import Model
    from modules.models_library.routes import models_crud
    client, _ = token_client
    client.app.include_router(models_crud.router)
    model = Model(name='Synthetic known zero value',org_id=1,cost_per_item=cost,markup_percent=markup,build_time_hours=2,units_per_bed=1)
    review_db.add(model)
    review_db.flush()
    review_db.execute(text('UPDATE models SET cost_per_item=:cost,markup_percent=:markup,build_time_hours=:hours WHERE id=:id'), dict(cost=cost,markup=markup,hours=hours,id=model.id))
    review_db.commit()
    response = client.get(f'/models/{model.id}', headers={'Authorization':'Bearer '+create_access_token(data={'sub':'admin'})})
    assert response.status_code == 200
    assert response.json()['value_per_bed']==bed
    assert response.json()['value_per_hour']==per_hour
