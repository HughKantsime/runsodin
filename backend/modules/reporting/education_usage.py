"""Education report from one retained-owner-qualified, bounded query snapshot."""
from __future__ import annotations
import json
import math
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session
from core.base import JobStatus
from core.db_compat import as_utc_datetime
from core.rbac import check_org_access
from core.report_job_ownership import proved_job_owner_sql, proved_education_job_sql

MAX_REPORT_JOBS=65536


def _number(value):
    if isinstance(value,bool) or not isinstance(value,(int,float)):return None
    result=float(value)
    return result if math.isfinite(result) and result>=0 else None


def _hours(start,end):
    try:
        result=(as_utc_datetime(end)-as_utc_datetime(start)).total_seconds()/3600
        return result if math.isfinite(result) and result>0 else None
    except (TypeError,ValueError,OverflowError):return None


def _bucket():
    return dict(total_jobs_submitted=0,total_jobs_approved=0,total_jobs_rejected=0,
                total_jobs_completed=0,total_jobs_failed=0,total_print_hours=0.0,
                estimated_print_hours=0.0,estimated_filament_grams=0.0,
                unknown_actual_hours_jobs=0,unknown_estimated_hours_jobs=0,
                unknown_filament_estimate_jobs=0,accuracy_samples=0,
                unknown_accuracy_jobs=0,unproven_history_jobs=0,_accuracy_sum=0.0,
                total_cost=0.0,unknown_cost_jobs=0,_cost_samples=0)


def _accumulate(bucket,key,value):
    if bucket[key] is None:return
    total=bucket[key]+value
    if math.isfinite(total):bucket[key]=total
    else:
        bucket[key]=None
        bucket[key+'_unavailable_reason']='numeric_overflow'


_COMPLETED=JobStatus.COMPLETED.value
_FAILED=JobStatus.FAILED.value


def _contribution(row):
    """Derive one row's report facts once; every bucket the row joins applies the same facts.

    Returns (approved, rejected, completed, failed, proved, cost, actual, estimated, accuracy, grams)
    where a None value fact means "unknown" for that fact and False means "not applicable".
    """
    approved=int(row['approved_by'] is not None and row['rejected_reason'] is None)
    rejected=int(row['rejected_reason'] is not None)
    status=row['status']
    completed=status==_COMPLETED
    failed=status==_FAILED
    if not row['owner_proved']:
        return (approved,rejected,completed,failed,False,False,False,False,False,False)
    cost=_number(row['estimated_cost']) if completed or failed else False
    if not completed:
        return (approved,rejected,completed,failed,True,cost,False,False,False,False)
    actual=_hours(row['actual_start'],row['actual_end'])
    estimate=_hours(row['scheduled_start'],row['scheduled_end'])
    quantity=_number(row['quantity'])
    duration=_number(row['duration_hours'])
    if duration is None:duration=_number(row['model_hours'])
    if duration is None or quantity is None or quantity<=0:estimated=None
    elif math.isfinite(duration*quantity):estimated=duration*quantity
    else:estimated=None
    accuracy=None if actual is None or estimate is None else min(actual,estimate)/max(actual,estimate)*100
    requirements=row['color_requirements']
    try:
        if isinstance(requirements,str):
            if len(requirements)>65536:raise ValueError('size')
            requirements=json.loads(requirements)
        if not isinstance(requirements,dict) or not requirements or quantity is None or quantity<=0:raise ValueError('unknown')
        values=[_number(v.get('grams')) if isinstance(v,dict) else None for v in requirements.values()]
        if any(v is None for v in values):raise ValueError('unknown')
        grams=sum(values)*quantity
        if not math.isfinite(grams):raise ValueError('overflow')
    except (ValueError,TypeError,RecursionError,OverflowError):grams=None
    return (approved,rejected,completed,failed,True,cost,actual,estimated,accuracy,grams)


def _add(bucket,facts):
    approved,rejected,completed,failed,proved,cost,actual,estimated,accuracy,grams=facts
    bucket['total_jobs_submitted']+=1
    bucket['total_jobs_approved']+=approved
    bucket['total_jobs_rejected']+=rejected
    bucket['total_jobs_completed']+=int(completed)
    bucket['total_jobs_failed']+=int(failed)
    if not proved:
        bucket['unproven_history_jobs']+=1
        return
    if cost is None:bucket['unknown_cost_jobs']+=1
    elif cost is not False:
        _accumulate(bucket,'total_cost',cost);bucket['_cost_samples']+=1
    if not completed:return
    if actual is None:bucket['unknown_actual_hours_jobs']+=1
    else:_accumulate(bucket,'total_print_hours',actual)
    if estimated is None:bucket['unknown_estimated_hours_jobs']+=1
    else:_accumulate(bucket,'estimated_print_hours',estimated)
    if accuracy is None:bucket['unknown_accuracy_jobs']+=1
    else:
        bucket['_accuracy_sum']+=accuracy
        bucket['accuracy_samples']+=1
    if grams is None:bucket['unknown_filament_estimate_jobs']+=1
    else:_accumulate(bucket,'estimated_filament_grams',grams)


def _finish(bucket,currency=None):
    result=dict(bucket)
    total=result['total_jobs_submitted']
    terminal=result['total_jobs_completed']+result['total_jobs_failed']
    result['approval_rate']=round(result['total_jobs_approved']/total*100,1) if total else 0
    result['success_rate']=round(result['total_jobs_completed']/terminal*100,1) if terminal else 0
    samples=result['accuracy_samples']
    result['accuracy']=round(result.pop('_accuracy_sum')/samples,1) if samples else None
    for key in ('total_print_hours','estimated_print_hours','estimated_filament_grams'):
        if result[key] is not None:result[key]=round(result[key],1)
    if not result.pop('_cost_samples'):result['total_cost']=None
    elif result['total_cost'] is not None:result['total_cost']=round(result['total_cost'],2)
    # Job estimates labeled in the viewer org's current currency (historical
    # estimates retain no currency of their own); None for cross-org views.
    result.update(currency=currency,cost_basis='job_estimate_current_currency',
                  accuracy_basis='retained_schedule_interval',filament_basis='model_estimate')
    return result


def report_currency(db,current_user):
    """Current finance currency of the viewer's org; None for platform/cross-org views."""
    from core.finance_currency import read_currency
    org=current_user.get('group_id')
    if type(org) is not int or org<=0:return None
    return read_currency(db,owner_scope='org',owner_key=org)


def build_education_usage_report(days:int,db:Session,current_user:dict)->dict:
    as_of=datetime.now(timezone.utc);cutoff=as_of-timedelta(days=days)
    org=current_user.get('group_id')
    platform=current_user.get('role')=='admin' and org is None
    valid=type(org) is int and org>0
    if not platform and not valid:rows=[]
    else:
        proof='('+proved_job_owner_sql()+') AND ('+proved_education_job_sql()+')'
        scope='' if platform else ' AND u.group_id=:org AND j.charged_to_org_id=:org AND ('+proof+')'
        # A scoped row is admitted only by the proof, so it is proved: evaluating the
        # correlated proof again in the select list would only repeat the same answer.
        proved='CASE WHEN '+proof+' THEN 1 ELSE 0 END' if platform else '1'
        rows=db.execute(text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fragments: proved_job_owner_sql()/proved_education_job_sql() constant builders, and a scope and an owner_proved column each chosen from a closed literal pair; values are bound
            '''SELECT j.id,j.status,j.approved_by,j.rejected_reason,j.quantity,
          j.duration_hours,j.estimated_cost,j.actual_start,j.actual_end,j.scheduled_start,j.scheduled_end,
          j.created_at,j.submitted_by,j.printer_id,j.model_id,u.username,u.email,u.role,
          m.build_time_hours AS model_hours,m.color_requirements,m.name AS model_name,
          m.org_id AS model_org,p.name AS printer_name,p.org_id AS printer_org,p.shared AS printer_shared,
          '''+proved+''' AS owner_proved
          FROM jobs j JOIN users u ON u.id=j.submitted_by
          LEFT JOIN models m ON m.id=j.model_id LEFT JOIN printers p ON p.id=j.printer_id
          WHERE j.created_at>=:cutoff AND j.created_at<:as_of'''+scope+'''
          ORDER BY j.id LIMIT :row_limit'''),
          {'org':org,'cutoff':cutoff,'as_of':as_of,'row_limit':MAX_REPORT_JOBS+1}).mappings().all()
        if len(rows)>MAX_REPORT_JOBS:raise HTTPException(413,'Usage report exceeds bounded job limit')
        if len({r['id'] for r in rows})!=len(rows):raise HTTPException(503,'Usage report identity could not be confirmed')
    users={};printers={};models={};daily={};summary=_bucket()
    access={}
    def org_access(org_id):
        # check_org_access depends on the resource org only through `is None` and ==.
        try:return access[org_id]
        except KeyError:
            allowed=access[org_id]=check_org_access(current_user,org_id)
            return allowed
        except TypeError:return check_org_access(current_user,org_id)
    for row in rows:
        row=dict(row)
        model_visible=bool(row['model_id'] is not None and row['model_name'] is not None
                           and org_access(row['model_org']))
        if not model_visible:
            row['model_hours']=None
            row['color_requirements']=None
        submitter=row['submitted_by']
        user=users.get(submitter)
        if user is None:
            user=users[submitter]=dict(_bucket(),user_id=submitter,username=row['username'],email=row['email'],role=row['role'],last_activity=None)
        created=as_utc_datetime(row['created_at'])
        activity=created.isoformat()
        if user['last_activity'] is None or activity>user['last_activity']:user['last_activity']=activity
        day=created.date().isoformat();daily[day]=daily.get(day,0)+1
        facts=_contribution(row)
        _add(summary,facts);_add(user,facts)
        # Unproven platform rows supply maintenance counts only, no resource IDs.
        for groups,kind in ((printers,'printer'),(models,'model')):
            visible=bool(row['owner_proved'] and row[kind+'_id'] is not None and row[kind+'_name'] is not None
                         and (org_access(row[kind+'_org']) or
                              (kind=='printer' and row['printer_shared'] in (True,1))))
            key=row[kind+'_id'] if visible else None
            group=groups.get(key)
            if group is None:
                group=groups[key]=dict(_bucket(),id=key,name=row[kind+'_name'] if visible else None,available=visible)
            _add(group,facts)
    currency=report_currency(db,current_user)
    result=_finish(summary,currency)
    result.update(total_users_active=len(users),total_jobs=result['total_jobs_submitted'],
                  rejection_rate=round(result['total_jobs_rejected']/result['total_jobs_submitted']*100,1) if result['total_jobs_submitted'] else 0)
    return dict(summary=result,users=[_finish(v,currency) for v in sorted(users.values(),key=lambda v:(-v['total_jobs_submitted'],v['user_id']))],
                daily_submissions=dict(sorted(daily.items())),by_printer=[_finish(v,currency) for v in printers.values()],
                by_model=[_finish(v,currency) for v in models.values()],days=days,window_basis='submission_created_at',
                as_of=as_of.isoformat(),history_basis='proved_retained_owner_or_platform_count_only')
