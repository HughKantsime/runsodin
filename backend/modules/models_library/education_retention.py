"""Library visibility is independent of retained Education model/file history."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from core.db import get_db
from core.rbac import require_action, check_org_access
from modules.models_library.models import Model

router = APIRouter(prefix='/models', tags=['Models'])


def assert_model_deletable(db, model_id):
    if db.execute(text('SELECT 1 FROM education_submissions WHERE model_id=:id'), {'id': model_id}).first():
        raise HTTPException(409, 'Model is used by Education history; hide it instead')


def visible_models(db):
    return db.query(Model).filter(text('models.hidden_from_library IS FALSE OR EXISTS ('
        'SELECT 1 FROM education_submissions es WHERE es.model_id=models.id AND es.archived_at IS NULL)'))


@router.post('/{model_id}/hide')
def hide_model(model_id: int, current_user=Depends(require_action('models.edit', scope='write:models')), db=Depends(get_db)):
    model = db.get(Model, model_id)
    if not model or not check_org_access(current_user, model.org_id):
        raise HTTPException(404, 'Model not found')
    db.execute(text('UPDATE models SET hidden_from_library=TRUE WHERE id=:id'), {'id': model_id})
    live = db.execute(text('SELECT 1 FROM education_submissions WHERE model_id=:id AND archived_at IS NULL'), {'id': model_id}).first()
    db.commit()
    return {'id': model_id, 'hidden': not bool(live), 'requested_hidden': True}
