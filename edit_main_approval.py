#!/usr/bin/env python3
"""
Edit main.py to add job approval workflow.
Run on server: python3 edit_main_approval.py

Changes:
1. Add import for dispatch_alert
2. Add RejectJobRequest schema (inline, avoids editing schemas.py separately)
3. Modify create_job to check approval setting + user role
4. Add approve/reject/resubmit endpoints
5. Add system config endpoint for require_job_approval
"""

import sys

MAIN_PY = "/opt/printfarm-scheduler/backend/main.py"

with open(MAIN_PY, "r") as f:
    content = f.read()

changes = 0

# ============================================================
# 1. Modify create_job to accept current_user and check approval
# ============================================================

old_create_job = '''@app.post("/api/jobs", response_model=JobResponse, status_code=status.HTTP_201_CREATED, tags=["Jobs"])
def create_job(job: JobCreate, db: Session = Depends(get_db)):
    """Create a new print job."""
    # Calculate cost if model is linked
    estimated_cost, suggested_price, _ = (None, None, None)
    if job.model_id:
        estimated_cost, suggested_price, _ = calculate_job_cost(db, model_id=job.model_id)
    
    db_job = Job(
        item_name=job.item_name,
        model_id=job.model_id,
        quantity=job.quantity,
        priority=job.priority,
        duration_hours=job.duration_hours,
        colors_required=job.colors_required,
        filament_type=job.filament_type,
        notes=job.notes,
        hold=job.hold,
        status=JobStatus.PENDING,
        estimated_cost=estimated_cost,
        suggested_price=suggested_price
    )
    db.add(db_job)
    db.commit()
    db.refresh(db_job)
    return db_job'''

new_create_job = '''@app.post("/api/jobs", response_model=JobResponse, status_code=status.HTTP_201_CREATED, tags=["Jobs"])
def create_job(job: JobCreate, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    """Create a new print job. If approval is required and user is a viewer, job is created as 'submitted'."""
    # Calculate cost if model is linked
    estimated_cost, suggested_price, _ = (None, None, None)
    if job.model_id:
        estimated_cost, suggested_price, _ = calculate_job_cost(db, model_id=job.model_id)
    
    # Check if approval workflow is enabled
    approval_required = False
    approval_config = db.query(SystemConfig).filter(SystemConfig.key == "require_job_approval").first()
    if approval_config and approval_config.value in (True, "true", "True", "1"):
        approval_required = True
    
    # Determine initial status
    initial_status = JobStatus.PENDING
    submitted_by = None
    if approval_required and current_user and current_user.get("role") == "viewer":
        initial_status = "submitted"
        submitted_by = current_user.get("id")
    
    db_job = Job(
        item_name=job.item_name,
        model_id=job.model_id,
        quantity=job.quantity,
        priority=job.priority,
        duration_hours=job.duration_hours,
        colors_required=job.colors_required,
        filament_type=job.filament_type,
        notes=job.notes,
        hold=job.hold,
        status=initial_status,
        estimated_cost=estimated_cost,
        suggested_price=suggested_price,
        submitted_by=submitted_by
    )
    db.add(db_job)
    db.commit()
    db.refresh(db_job)
    
    # If submitted for approval, notify approvers
    if initial_status == "submitted":
        try:
            from alert_dispatcher import dispatch_alert
            dispatch_alert(
                db=db,
                alert_type=AlertType.JOB_SUBMITTED,
                severity=AlertSeverity.INFO,
                title=f"Job awaiting approval: {job.item_name or 'Untitled'}",
                message=f"{current_user.get('display_name') or current_user.get('username', 'A user')} submitted a print job",
                job_id=db_job.id
            )
        except Exception as e:
            logger.warning(f"Failed to dispatch job_submitted alert: {e}")
    
    return db_job'''

if old_create_job in content:
    content = content.replace(old_create_job, new_create_job)
    changes += 1
    print("✓ Modified create_job to support approval workflow")
else:
    print("✗ Could not find create_job - may have been modified already")
    print("  Looking for the function signature...")
    if "def create_job(job: JobCreate, db: Session = Depends(get_db)):" in content:
        print("  Found old signature but body doesn't match exactly.")
        print("  Please apply manually from the design doc.")
    elif "submitted_by=submitted_by" in content:
        print("  Already appears to have approval logic.")
    else:
        print("  MANUAL EDIT NEEDED for create_job endpoint.")

# ============================================================
# 2. Add approval endpoints after reset_job endpoint
# ============================================================

insertion_marker = '''# ============== Scheduler =============='''

approval_endpoints = '''# ============== Job Approval Workflow (v0.18.0) ==============

class _RejectJobRequest(BaseModel):
    """Inline schema for reject endpoint."""
    reason: str

@app.post("/api/jobs/{job_id}/approve", tags=["Jobs"])
def approve_job(job_id: int, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    """Approve a submitted job. Moves it to pending status for scheduling."""
    if not current_user or current_user.get("role") not in ("operator", "admin"):
        raise HTTPException(status_code=403, detail="Only operators and admins can approve jobs")
    
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    if job.status != "submitted":
        raise HTTPException(status_code=400, detail="Job is not in submitted status")
    
    job.status = JobStatus.PENDING
    job.approved_by = current_user["id"]
    job.approved_at = datetime.utcnow()
    db.commit()
    db.refresh(job)
    
    # Notify the student who submitted
    if job.submitted_by:
        try:
            from alert_dispatcher import dispatch_alert
            dispatch_alert(
                db=db,
                alert_type=AlertType.JOB_APPROVED,
                severity=AlertSeverity.INFO,
                title=f"Job approved: {job.item_name or 'Untitled'}",
                message=f"Approved by {current_user.get('display_name') or current_user.get('username', 'an approver')}",
                job_id=job.id
            )
        except Exception as e:
            logger.warning(f"Failed to dispatch job_approved alert: {e}")
    
    return {"status": "approved", "job_id": job.id}


@app.post("/api/jobs/{job_id}/reject", tags=["Jobs"])
def reject_job(job_id: int, body: _RejectJobRequest, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    """Reject a submitted job with a required reason."""
    if not current_user or current_user.get("role") not in ("operator", "admin"):
        raise HTTPException(status_code=403, detail="Only operators and admins can reject jobs")
    
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    if job.status != "submitted":
        raise HTTPException(status_code=400, detail="Job is not in submitted status")
    
    if not body.reason or not body.reason.strip():
        raise HTTPException(status_code=400, detail="Rejection reason is required")
    
    job.status = "rejected"
    job.approved_by = current_user["id"]
    job.rejected_reason = body.reason.strip()
    db.commit()
    db.refresh(job)
    
    # Notify the student who submitted
    if job.submitted_by:
        try:
            from alert_dispatcher import dispatch_alert
            dispatch_alert(
                db=db,
                alert_type=AlertType.JOB_REJECTED,
                severity=AlertSeverity.WARNING,
                title=f"Job rejected: {job.item_name or 'Untitled'}",
                message=f"Reason: {body.reason.strip()}",
                job_id=job.id
            )
        except Exception as e:
            logger.warning(f"Failed to dispatch job_rejected alert: {e}")
    
    return {"status": "rejected", "job_id": job.id, "reason": body.reason.strip()}


@app.post("/api/jobs/{job_id}/resubmit", tags=["Jobs"])
def resubmit_job(job_id: int, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    """Resubmit a rejected job for approval again."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    if job.status != "rejected":
        raise HTTPException(status_code=400, detail="Only rejected jobs can be resubmitted")
    
    if job.submitted_by != current_user["id"]:
        raise HTTPException(status_code=403, detail="Only the original submitter can resubmit")
    
    job.status = "submitted"
    job.rejected_reason = None
    job.approved_by = None
    job.approved_at = None
    db.commit()
    db.refresh(job)
    
    # Re-notify approvers
    try:
        from alert_dispatcher import dispatch_alert
        dispatch_alert(
            db=db,
            alert_type=AlertType.JOB_SUBMITTED,
            severity=AlertSeverity.INFO,
            title=f"Job resubmitted: {job.item_name or 'Untitled'}",
            message=f"{current_user.get('display_name') or current_user.get('username', 'A user')} resubmitted a previously rejected job",
            job_id=job.id
        )
    except Exception as e:
        logger.warning(f"Failed to dispatch job_submitted alert: {e}")
    
    return {"status": "resubmitted", "job_id": job.id}


@app.get("/api/config/require-job-approval", tags=["Config"])
def get_approval_setting(db: Session = Depends(get_db)):
    """Get the current job approval requirement setting."""
    config = db.query(SystemConfig).filter(SystemConfig.key == "require_job_approval").first()
    enabled = False
    if config and config.value in (True, "true", "True", "1"):
        enabled = True
    return {"require_job_approval": enabled}


@app.put("/api/config/require-job-approval", tags=["Config"])
def set_approval_setting(body: dict, db: Session = Depends(get_db), current_user: dict = Depends(require_role("admin"))):
    """Toggle the job approval requirement. Admin only."""
    enabled = body.get("enabled", False)
    config = db.query(SystemConfig).filter(SystemConfig.key == "require_job_approval").first()
    if config:
        config.value = "true" if enabled else "false"
    else:
        config = SystemConfig(key="require_job_approval", value="true" if enabled else "false")
        db.add(config)
    db.commit()
    return {"require_job_approval": enabled}


# ============== Scheduler =============='''

if insertion_marker in content:
    content = content.replace(insertion_marker, approval_endpoints)
    changes += 1
    print("✓ Added approval endpoints (approve, reject, resubmit, config)")
else:
    print("✗ Could not find insertion marker '# ============== Scheduler =='")
    print("  MANUAL INSERT NEEDED - paste endpoints before the scheduler section")

# ============================================================
# 3. Write the file
# ============================================================

if changes > 0:
    with open(MAIN_PY, "w") as f:
        f.write(content)
    print(f"\n✅ Applied {changes} changes to main.py")
else:
    print("\n⚠ No changes applied. Check the errors above.")
    sys.exit(1)
