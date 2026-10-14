"""Exact committed upload graph capture/recheck for explicit AMS approval."""
import re
from dataclasses import dataclass
from sqlalchemy import text
from modules.organizations.education_review_service import _reviewable_submission,_assert_review_state,assert_file_preview_allowed


class EducationPayloadUnavailable(ValueError):pass


@dataclass(frozen=True,repr=False)
class EducationPayloadGraph:
    submission_id:int
    org_id:int
    job_id:int
    model_id:int
    file_id:int
    creator_id:int
    operation_id:str
    revision:int
    stored_path:str
    sha256:str
    storage_bytes:int
    job_file_id:int|None
    job_hash:str|None


def capture_payload_graph(db,*,submission_id,revision,principal):
    """Authorize and capture only; no file IO. Caller closes before staging."""
    try:
        row=_reviewable_submission(db,submission_id=submission_id,principal=principal)
        _assert_review_state(row,revision)
        assert_file_preview_allowed(db,file_id=row.print_file_id,principal=principal)
        graph=db.execute(text('SELECT s.model_id,s.operation_id,s.submitted_by,j.model_id AS job_model,'
            'j.print_file_id AS job_file,j.payload_sha256 AS job_hash,m.print_file_id AS model_file,m.org_id AS model_owner,'
            'pf.model_id AS file_model,pf.created_by,pf.org_id AS file_owner,pf.stored_path,pf.file_hash,pf.storage_bytes,pf.blob_state,'
            'o.org_id AS operation_owner,o.user_id AS operation_user,o.state AS operation_state,o.final_path,o.expected_hash,o.expected_bytes '
            'FROM education_submissions s JOIN jobs j ON j.id=s.job_id JOIN models m ON m.id=s.model_id '
            'JOIN print_files pf ON pf.id=s.print_file_id JOIN education_upload_operations o ON o.operation_id=s.operation_id '
            'WHERE s.id=:id'),{'id':submission_id}).mappings().one()
        if (graph['job_model']!=graph['model_id'] or graph['file_model']!=graph['model_id']
                or graph['model_file']!=row.print_file_id or graph['model_owner']!=row.org_id
                or graph['file_owner']!=row.org_id or graph['operation_owner']!=row.org_id
                or graph['created_by']!=graph['submitted_by'] or graph['operation_user']!=graph['submitted_by']
                or graph['operation_state']!='committed' or graph['blob_state']!='present'
                or graph['stored_path']!=graph['final_path'] or type(graph['stored_path']) is not str
                or type(graph['file_hash']) is not str or not re.fullmatch('[0-9a-f]{64}',graph['file_hash'])
                or graph['expected_hash']!=graph['file_hash']
                or type(graph['storage_bytes']) is not int or not 0<graph['storage_bytes']<=64*1024*1024
                or graph['expected_bytes']!=graph['storage_bytes']
                or graph['job_file'] not in (None,row.print_file_id)
                or graph['job_hash'] not in (None,graph['file_hash'])):raise ValueError()
        return EducationPayloadGraph(int(row.id),int(row.org_id),int(row.job_id),int(graph['model_id']),
            int(row.print_file_id),int(graph['submitted_by']),graph['operation_id'],revision,
            graph['stored_path'],graph['file_hash'],graph['storage_bytes'],graph['job_file'],graph['job_hash'])
    except Exception:raise EducationPayloadUnavailable('Submission payload authority unavailable') from None


def lock_and_recheck_payload_graph(db,*,captured,principal):
    """Call after Education parent locks, before printer locks. No file IO."""
    try:
        if type(captured) is not EducationPayloadGraph:raise ValueError()
        postgres=db.get_bind().dialect.name=='postgresql'
        params={'model':captured.model_id,'file':captured.file_id,'org':captured.org_id,
                'operation':captured.operation_id,'creator':captured.creator_id}
        operation_where='operation_id=:operation AND org_id=:org AND user_id=:creator'
        operation_lock=('SELECT 1 FROM education_upload_operations WHERE '+operation_where+' FOR UPDATE NOWAIT' if postgres
                        else 'UPDATE education_upload_operations SET updated_at=updated_at WHERE '+operation_where+' RETURNING 1')
        if db.execute(text(operation_lock),params).first() is None:raise ValueError()
        for table,column,key in (('models','name','model'),('print_files','filename','file')):
            query=(f'SELECT 1 FROM {table} WHERE id=:{key} AND org_id=:org FOR UPDATE NOWAIT' if postgres
                   else f'UPDATE {table} SET {column}={column} WHERE id=:{key} AND org_id=:org RETURNING 1')
            if db.execute(text(query),params).first() is None:raise ValueError()
        current=capture_payload_graph(db,submission_id=captured.submission_id,revision=captured.revision,principal=principal)
        if current!=captured:raise ValueError()
        return current
    except Exception:raise EducationPayloadUnavailable('Submission payload changed; reload required') from None


@dataclass(frozen=True,repr=False)
class PreparedAMSApproval:
    graph: EducationPayloadGraph
    verified_payload: dict
