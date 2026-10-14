"""Raw payload authority snapshot and statement-time assignment comparison."""
from sqlalchemy import text
from core.dispatch_payload_selection import SELECTED_PRINT_FILE_ID_SQL, SELECTED_PRINT_FILE_JOIN_SQL
from core.dispatch_identity import ownership_refusal

SNAPSHOT_SQL = f"""SELECT j.model_id,j.print_file_id,j.charged_to_org_id,
 m.id AS model_row_id,m.org_id AS model_org_id,m.print_file_id AS primary_id,
 primary_file.id AS primary_row_id,primary_file.org_id AS primary_org_id,
 primary_file.model_id AS primary_model_id,primary_file.stored_path AS primary_path,
 ({SELECTED_PRINT_FILE_ID_SQL}) AS selected_id,
 pf.id AS selected_row_id,pf.org_id AS selected_org_id,pf.stored_path AS selected_path,
 EXISTS(SELECT 1 FROM education_submissions es WHERE es.model_id=m.id
   OR es.print_file_id=pf.id OR es.print_file_id=primary_file.id) AS protected_submission,
 EXISTS(SELECT 1 FROM education_upload_operations eo WHERE eo.final_path IS NOT NULL
   AND (eo.final_path=pf.stored_path OR eo.final_path=primary_file.stored_path)) AS protected_operation
 FROM jobs j LEFT JOIN education_submissions s ON s.job_id=j.id
 LEFT JOIN models m ON m.id=j.model_id
 LEFT JOIN print_files primary_file ON primary_file.id=m.print_file_id
 LEFT JOIN print_files pf ON {SELECTED_PRINT_FILE_JOIN_SQL}
 WHERE j.id=:id AND s.id IS NULL"""

FIELDS = {
 'model_id':'INTEGER','print_file_id':'INTEGER','charged_to_org_id':'INTEGER',
 'model_row_id':'INTEGER','model_org_id':'INTEGER','primary_id':'INTEGER',
 'primary_row_id':'INTEGER','primary_org_id':'INTEGER','primary_model_id':'INTEGER',
 'primary_path':'TEXT','selected_id':'INTEGER','selected_row_id':'INTEGER',
 'selected_org_id':'INTEGER','selected_path':'TEXT',
 'protected_submission':'BOOLEAN','protected_operation':'BOOLEAN',
}


def capture(executor, job_id):
    row = executor.execute(text(SNAPSHOT_SQL), {'id':job_id}).mappings().first()
    if row is None: return None
    snapshot = dict(row)
    for name, kind in FIELDS.items():
        value = snapshot[name]
        if kind == 'INTEGER' and value is not None and (type(value) is not int or value <= 0): return None
        if kind == 'TEXT' and value is not None and not isinstance(value,str): return None
        if kind == 'BOOLEAN':
            if type(value) is bool: pass
            elif type(value) is int and value in (0,1): snapshot[name]=bool(value)
            else: return None
    if snapshot['protected_submission'] or snapshot['protected_operation']: return None
    if snapshot['model_id'] is not None and snapshot['model_row_id'] is None: return None
    if snapshot['print_file_id'] is not None and snapshot['selected_row_id'] is None: return None
    if snapshot['primary_id'] is not None and (snapshot['primary_row_id'] is None or snapshot['primary_model_id'] != snapshot['model_id']): return None
    if snapshot['selected_id'] is not None and snapshot['selected_row_id'] is None: return None
    for owner in (snapshot['selected_org_id'],snapshot['primary_org_id']):
        if ownership_refusal({'charged_to_org_id':snapshot['charged_to_org_id'],
                             'model_org_id':snapshot['model_org_id'],'print_file_org_id':owner}, {'org_id':None}): return None
    return snapshot


def assignment_guard(snapshot):
    conditions=[]
    parameters={}
    for name,kind in FIELDS.items():
        parameter='payload_'+name
        conditions.append(f'(payload.{name}=:{parameter} OR (payload.{name} IS NULL AND CAST(:{parameter} AS {kind}) IS NULL))')
        parameters[parameter]=snapshot[name]
    return 'EXISTS (SELECT 1 FROM ('+SNAPSHOT_SQL+') payload WHERE '+' AND '.join(conditions)+')', parameters
