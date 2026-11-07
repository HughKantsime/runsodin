"""D2 HTTP boundary: license, permission matrix, Education scopes, live roster."""
from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session
from core.db import get_db
from core.rbac import require_action
from license_manager import feature_gate
from modules.organizations.education_access import require_education_principal
from modules.organizations import education_classes_service as service
from modules.organizations.education_classes_schemas import (
    ClassCreate, ClassUpdate, AssignmentCreate, AssignmentUpdate, SubmissionLink, Assessment,
)
from modules.organizations.education_assignment_submissions import link_submission, assess
from modules.organizations.education_classes_portfolio import portfolio, thumbnail

def private_response(response: Response):
    # Also prevents the generic Idempotency-Key middleware from retaining grades
    # or replaying a response after a roster/license change.
    response.headers['Cache-Control'] = 'private, no-store'


router = APIRouter(prefix='/education/classes', tags=['Education classes'],
                   dependencies=[Depends(feature_gate('classes')), Depends(private_response)])
read = require_education_principal()
write = require_education_principal(write=True)


@router.get('', dependencies=[Depends(require_action('education.classes_read'))])
def get_classes(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
                principal=Depends(read), db: Session = Depends(get_db)):
    return service.list_classes(db, principal, offset=offset, limit=limit)


@router.post('', status_code=201, dependencies=[Depends(require_action('education.classes_manage'))])
def post_class(body: ClassCreate, principal=Depends(write), db: Session = Depends(get_db)):
    return service.create_class(db, principal, body)


@router.get('/portfolio', dependencies=[Depends(require_action('education.classes_read'))])
def get_portfolio(offset: int = Query(0, ge=0), limit: int = Query(30, ge=1, le=100),
                  principal=Depends(read), db: Session = Depends(get_db)):
    return portfolio(db, principal, offset=offset, limit=limit)


@router.get('/portfolio/{submission_id}/thumbnail', dependencies=[Depends(require_action('education.classes_read'))])
def get_thumbnail(submission_id: int, principal=Depends(read), db: Session = Depends(get_db)):
    data = thumbnail(db, principal, submission_id)
    return Response(data, media_type='image/png', headers={'Cache-Control': 'private, no-store',
                                                        'X-Content-Type-Options': 'nosniff'})


@router.put('/{class_id}', dependencies=[Depends(require_action('education.classes_manage'))])
def put_class(class_id: str, body: ClassUpdate, principal=Depends(write), db: Session = Depends(get_db)):
    return service.update_class(db, principal, class_id, body)


@router.get('/{class_id}/roster', dependencies=[Depends(require_action('education.classes_read'))])
def get_roster(class_id: str, principal=Depends(read), db: Session = Depends(get_db)):
    return service.roster(db, principal, class_id)


@router.get('/{class_id}/assignments', dependencies=[Depends(require_action('education.classes_read'))])
def get_assignments(class_id: str, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
                    principal=Depends(read), db: Session = Depends(get_db)):
    return service.assignments(db, principal, class_id, offset=offset, limit=limit)


@router.post('/{class_id}/assignments', status_code=201, dependencies=[Depends(require_action('education.classes_manage'))])
def post_assignment(class_id: str, body: AssignmentCreate, principal=Depends(write), db: Session = Depends(get_db)):
    return service.save_assignment(db, principal, class_id, body)


@router.put('/{class_id}/assignments/{assignment_id}', dependencies=[Depends(require_action('education.classes_manage'))])
def put_assignment(class_id: str, assignment_id: str, body: AssignmentUpdate,
                   principal=Depends(write), db: Session = Depends(get_db)):
    return service.save_assignment(db, principal, class_id, body, assignment_id)


@router.get('/assignments/{assignment_id}/submissions', dependencies=[Depends(require_action('education.classes_read'))])
def get_assignment_submissions(assignment_id: str, offset: int = Query(0, ge=0), limit: int = Query(30, ge=1, le=100),
                               principal=Depends(read), db: Session = Depends(get_db)):
    return portfolio(db, principal, assignment_id=assignment_id, offset=offset, limit=limit)


@router.post('/assignments/{assignment_id}/submissions', dependencies=[Depends(require_action('education.classes_submit'))])
def post_assignment_submission(assignment_id: str, body: SubmissionLink, principal=Depends(write), db: Session = Depends(get_db)):
    return link_submission(db, principal, assignment_id, body)


@router.put('/assignments/{assignment_id}/submissions/{submission_id}', dependencies=[Depends(require_action('education.classes_assess'))])
def put_assessment(assignment_id: str, submission_id: int, body: Assessment,
                   principal=Depends(write), db: Session = Depends(get_db)):
    return assess(db, principal, assignment_id, submission_id, body)
