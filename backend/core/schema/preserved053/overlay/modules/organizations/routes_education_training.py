"""Training API: Education credentials plus explicit feature and matrix gates."""
from uuid import UUID
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from core.db import get_db
from core.rbac import require_action
from license_manager import feature_gate
from modules.organizations.education_access import require_education_principal
from modules.organizations.education_training_schemas import TrainingDefinition, TrainingUpdate, QuizAttempt, SignoffCommand
from modules.organizations import education_training_service as service

router = APIRouter(prefix='/education', tags=['Education training'], dependencies=[Depends(feature_gate('training_signoff'))])


def _write(db, call, *args, **kwargs):
    try:
        return call(db, *args, **kwargs)
    except Exception:
        db.rollback()
        raise


@router.get('/cost-centers/{center_id}/trainings', dependencies=[Depends(require_action('education.training_read'))])
async def list_trainings(center_id: int, principal: dict = Depends(require_education_principal()), db: Session = Depends(get_db)):
    return service.list_trainings(db, principal, center_id)


@router.post('/cost-centers/{center_id}/trainings', dependencies=[Depends(require_action('education.training_manage'))])
async def create_training(center_id: int, body: TrainingDefinition, principal: dict = Depends(require_education_principal(write=True)), db: Session = Depends(get_db)):
    return _write(db, service.save_training, principal, body, center_id=center_id)


@router.put('/trainings/{training_id}', dependencies=[Depends(require_action('education.training_manage'))])
async def update_training(training_id: UUID, body: TrainingUpdate, principal: dict = Depends(require_education_principal(write=True)), db: Session = Depends(get_db)):
    return _write(db, service.save_training, principal, body, training_id=str(training_id))


@router.get('/trainings/{training_id}/records', dependencies=[Depends(require_action('education.training_read'))])
async def training_roster(training_id: UUID, principal: dict = Depends(require_education_principal()), db: Session = Depends(get_db)):
    return service.roster(db, principal, str(training_id))


@router.post('/trainings/{training_id}/quiz', dependencies=[Depends(require_action('education.training_take'))])
async def take_quiz(training_id: UUID, body: QuizAttempt, principal: dict = Depends(require_education_principal(write=True)), db: Session = Depends(get_db)):
    return _write(db, service.assess, principal, str(training_id), body, kind='quiz')


@router.post('/trainings/{training_id}/students/{user_id}/signoff', dependencies=[Depends(require_action('education.training_manage'))])
async def signoff(training_id: UUID, user_id: int, body: SignoffCommand, principal: dict = Depends(require_education_principal(write=True)), db: Session = Depends(get_db)):
    return _write(db, service.assess, principal, str(training_id), body, kind='signoff', user_id=user_id)


@router.post('/trainings/{training_id}/students/{user_id}/revoke', dependencies=[Depends(require_action('education.training_manage'))])
async def revoke(training_id: UUID, user_id: int, body: SignoffCommand, principal: dict = Depends(require_education_principal(write=True)), db: Session = Depends(get_db)):
    return _write(db, service.assess, principal, str(training_id), body, kind='revoke', user_id=user_id)
