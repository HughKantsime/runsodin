from core.interfaces.timelapse_media import require_timelapse_media_provider
from core.timelapse_media_errors import RetirementUnavailable,EditUnavailable,NoCompletedReceiptYet
"""O.D.I.N. — Camera & Timelapse Routes

Timelapse list/video/delete, camera list/toggle/stream/WebRTC.
"""

from core.rbac import should_filter_org, require_action

# Domain: printers
# Depends on: core, organizations
# Owns tables: timelapses (shared with archives)

from pathlib import Path
from typing import Optional
import logging
import math
import os
import subprocess

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Query, status, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.db import get_db
from core.dependencies import get_current_user, log_audit, validate_access_token
from core.rbac import require_role, check_org_access, has_permission, token_scope_granted, handler_enforces_token_scope
from modules.printers.models import Printer
from modules.archives.models import Timelapse
from core.timelapse_access import assert_timelapse_access,timelapse_visibility,visible_printer_name,inherit_timelapse_owner
from modules.printers.routes import get_camera_url, sync_go2rtc_config
from modules.printers.go2rtc_access import go2rtc_api_url, go2rtc_client_kwargs

log = logging.getLogger("odin.api")
router = APIRouter()
TIMELAPSE_ROOT="/data/timelapses"


def _check_printer_org(current_user: dict, printer_id: int, db) -> None:
    """Verify the caller has org access to the printer behind a timelapse/camera."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer or (not check_org_access(current_user, printer.org_id) and not printer.shared):
        raise HTTPException(status_code=404, detail="Not found")


def _authorize_media(current_user: dict) -> None:
    """Apply the same role and scoped-token policy after optional media-token validation."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not has_permission(current_user["role"], "viewer"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    scopes = current_user.get("_token_scopes", [])
    if scopes or current_user.get("_auth_kind") == "user_api_token":
        if not token_scope_granted(scopes, "read:printers"):
            raise HTTPException(status_code=403, detail="Insufficient token scope")


def _finite_edit_number(value) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


# ====================================================================
# Timelapses
# ====================================================================

@router.get("/timelapses", tags=["Timelapses"])
def list_timelapses(
    printer_id: Optional[int] = None,
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(default=50, le=200),
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role("viewer", scope="read:printers")),
):
    """List timelapse recordings with optional filters."""
    condition,parameters=timelapse_visibility(current_user)
    q=db.query(Timelapse).filter(text(condition)).params(**parameters).order_by(Timelapse.created_at.desc(),Timelapse.id.desc())
    if printer_id:
        q = q.filter(Timelapse.printer_id == printer_id)
    if status_filter:
        q = q.filter(Timelapse.status == status_filter)
    total = q.count()
    items = q.offset(offset).limit(limit).all()
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "timelapses": [
            {
                "id": t.id,
                "printer_id": t.printer_id,
                "printer_name": visible_printer_name(db,t,current_user),
                "print_job_id": t.print_job_id,
                "filename": t.filename,
                "frame_count": t.frame_count,
                "duration_seconds": t.duration_seconds,
                "file_size_mb": t.file_size_mb,
                "status": t.status,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "completed_at": t.completed_at.isoformat() if t.completed_at else None,
            }
            for t in items
        ],
    }


@router.get("/timelapses/{timelapse_id}", tags=["Timelapses"])
def get_timelapse(
    timelapse_id: int,
    response: Response,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role("viewer", scope="read:printers")),
):
    """Get a single timelapse by ID."""
    t = db.query(Timelapse).filter(Timelapse.id == timelapse_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Timelapse not found")
    assert_timelapse_access(db,t,current_user)
    if t.ownership_operation_id:response.headers["ETag"]='"'+t.ownership_operation_id+'"'
    return {
        "id": t.id,
        "printer_id": t.printer_id,
        "printer_name": visible_printer_name(db,t,current_user),
        "print_job_id": t.print_job_id,
        "filename": t.filename,
        "frame_count": t.frame_count,
        "duration_seconds": t.duration_seconds,
        "file_size_mb": t.file_size_mb,
        "status": t.status,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "completed_at": t.completed_at.isoformat() if t.completed_at else None,
    }


def _read_lifetime(request,db,principal,tid,*,query_allowed=False,attachment=False):
    from modules.printers.timelapse_request_authority import selected_evidence
    from core.timelapse_media_lock import MediaGuardUnavailable
    if len(request.headers.getlist('range'))>1:raise HTTPException(416,'Multiple byte ranges refused')
    expected=request.query_params.get('version')
    if len(request.query_params.getlist('version'))>1:raise HTTPException(400,'Invalid version precondition')
    if expected is not None:
        import re
        if not re.fullmatch(r'"[A-Za-z0-9_-]{1,64}"',expected):raise HTTPException(400,'Invalid version precondition')
        expected=expected[1:-1]
    try:
        provider=require_timelapse_media_provider()
        evidence=selected_evidence(request,db,principal,provider=provider,purpose='read',query_allowed=query_allowed)
        engine=db.get_bind();db.rollback();db.close()
        return provider.admitted_response(engine,TIMELAPSE_ROOT,tid,evidence,range_header=request.headers.get('range'),head=request.method=='HEAD',filename=f'odin_timelapse_{tid}.mp4' if attachment else None,expected_nonce=expected)
    except (RetirementUnavailable,MediaGuardUnavailable) as error:raise HTTPException(503,detail='Timelapse admission refused: '+getattr(error,'reason','writer_active')) from None

@router.api_route("/timelapses/{timelapse_id}/video",methods=['GET','HEAD'],tags=['Timelapses'],dependencies=[Depends(handler_enforces_token_scope('read:printers'))])
def get_timelapse_video(timelapse_id:int,request:Request,token:Optional[str]=None,db:Session=Depends(get_db),current_user:dict=Depends(get_current_user)):
    return _read_lifetime(request,db,current_user,timelapse_id,query_allowed=True,attachment=True)

@router.delete("/timelapses/{timelapse_id}",status_code=204,tags=['Timelapses'])
def delete_timelapse(timelapse_id:int,request:Request,db:Session=Depends(get_db),current_user:dict=Depends(require_role('admin',scope='write:printers'))):
    import re
    from modules.printers.timelapse_request_authority import selected_evidence
    from core.timelapse_request_budget import VerificationBudget
    value=request.headers.get('if-match')
    if value is None:raise HTTPException(428,'Exact lifetime If-Match required')
    if len(request.headers.getlist('if-match'))!=1:raise HTTPException(400,'Invalid lifetime If-Match')
    if not re.fullmatch(r'"[A-Za-z0-9_-]{1,64}"',value):raise HTTPException(400,'Invalid lifetime If-Match')
    nonce=value[1:-1]
    try:
        provider=require_timelapse_media_provider()
        evidence=selected_evidence(request,db,current_user,provider=provider,purpose='retire')
        engine=db.get_bind();db.rollback();db.close();budget=VerificationBudget()
        try:
            provider.sealed_lifetime_receipt(engine,evidence,operation_id=nonce,original_id=timelapse_id)
            return Response(status_code=204)
        except NoCompletedReceiptYet:pass
        with Session(engine) as admission:
            provider.authorize_retirement(admission,evidence,timelapse_id,nonce)
        provider.prepare_request_lifetime_retirement(engine,TIMELAPSE_ROOT,timelapse_id,evidence,expected_nonce=nonce,budget=budget)
        provider.retire_sealed_lifetime(engine,TIMELAPSE_ROOT,timelapse_id,evidence,expected_nonce=nonce,budget=budget)
        provider.sealed_lifetime_receipt(engine,evidence,operation_id=nonce,original_id=timelapse_id)
        return Response(status_code=204)
    except RetirementUnavailable as error:raise HTTPException(503,detail='Timelapse retirement refused: '+error.reason) from None


def _check_ffmpeg() -> bool:
    """Check if ffmpeg is available."""
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)  # noqa: S603 S607
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _get_timelapse_path(t: Timelapse) -> Path:
    """Resolve and validate timelapse video path."""
    video_path = Path(os.path.realpath(Path("/data/timelapses") / t.filename))
    if not str(video_path).startswith("/data/timelapses/"):
        raise HTTPException(status_code=404, detail="Not found")
    if not video_path.is_file():
        raise HTTPException(status_code=404, detail="Video file not found")
    return video_path


@router.api_route("/timelapses/{timelapse_id}/stream",methods=['GET','HEAD'],tags=['Timelapses'],dependencies=[Depends(handler_enforces_token_scope('read:printers'))])
def stream_timelapse(timelapse_id:int,request:Request,token:Optional[str]=None,db:Session=Depends(get_db),current_user:dict=Depends(get_current_user)):
    return _read_lifetime(request,db,current_user,timelapse_id,query_allowed=True)

@router.api_route("/timelapses/{timelapse_id}/download",methods=['GET','HEAD'],tags=['Timelapses'],dependencies=[Depends(handler_enforces_token_scope('read:printers'))])
def download_timelapse(timelapse_id:int,request:Request,db:Session=Depends(get_db),current_user:dict=Depends(get_current_user)):
    return _read_lifetime(request,db,current_user,timelapse_id,attachment=True)


def _edit_contention(error):
    from sqlalchemy.exc import DBAPIError
    if not isinstance(error,DBAPIError):return False
    code=getattr(error.orig,'sqlstate',None)
    sqlite=getattr(error.orig,'sqlite_errorcode',None)
    return code in ('55P03','40P01','57014') or (type(sqlite) is int and sqlite & 255 in (5,6))


def _edit_request(tid,request,body,db,principal,kind):
    import re
    from core.timelapse_recipes import make_recipe
    from modules.printers.timelapse_request_authority import selected_evidence
    from core.timelapse_media_lock import MediaGuardUnavailable
    value=request.headers.get('if-match')
    if value is None:raise HTTPException(428,'Exact recording If-Match required')
    if len(request.headers.getlist('if-match'))!=1 or not re.fullmatch(r'"[A-Za-z0-9_-]{1,64}"',value):raise HTTPException(400,'Invalid recording If-Match')
    identifier=body.get('operation_id')
    if type(identifier) is not str or not re.fullmatch('[0-9a-f]{32}',identifier):raise HTTPException(400,'Invalid edit operation ID')
    try:
        recipe=make_recipe(kind,start_seconds=body.get('start_seconds',0),end_seconds=body.get('end_seconds')) if kind=='trim' else make_recipe(kind,multiplier=body.get('multiplier'))
    except ValueError:raise HTTPException(400,'Invalid edit recipe') from None
    try:
        provider=require_timelapse_media_provider()
        evidence=selected_evidence(request,db,principal,provider=provider,purpose='edit')
        engine=db.get_bind();db.rollback();db.close()
        receipt=provider.execute_edit(engine,TIMELAPSE_ROOT,tid,evidence,identifier,nonce=value[1:-1],recipe=recipe)
        return Response(content=__import__('json').dumps(receipt),media_type='application/json',headers={'ETag':'"'+receipt['result_nonce']+'"'})
    except (EditUnavailable,RetirementUnavailable,MediaGuardUnavailable,OSError,ValueError):raise HTTPException(503,'Timelapse edit could not be confirmed') from None
    except Exception as error:
        if _edit_contention(error):raise HTTPException(503,'Timelapse edit is busy; retry the same operation') from None
        raise

@router.post('/timelapses/{timelapse_id}/trim',tags=['Timelapses'])
def trim_timelapse(timelapse_id:int,request:Request,body:dict=Body(...),db:Session=Depends(get_db),current_user:dict=Depends(require_role('operator',scope='write:printers'))):
    return _edit_request(timelapse_id,request,body,db,current_user,'trim')

VALID_SPEED_MULTIPLIERS={.5,1,1.5,2,4,8}

@router.post('/timelapses/{timelapse_id}/speed',tags=['Timelapses'])
def speed_timelapse(timelapse_id:int,request:Request,body:dict=Body(...),db:Session=Depends(get_db),current_user:dict=Depends(require_role('operator',scope='write:printers'))):
    return _edit_request(timelapse_id,request,body,db,current_user,'speed')

@router.get('/timelapses/{timelapse_id}/edits/{operation_id}',tags=['Timelapses'])
def edit_receipt(timelapse_id:int,operation_id:str,request:Request,db:Session=Depends(get_db),current_user:dict=Depends(require_role('operator',scope='write:printers'))):
    from modules.printers.timelapse_request_authority import selected_evidence
    from core.timelapse_media_lock import MediaGuardUnavailable
    try:
        provider=require_timelapse_media_provider()
        evidence=selected_evidence(request,db,current_user,provider=provider,purpose='edit')
        engine=db.get_bind();db.rollback();db.close()
        return provider.execute_edit(engine,TIMELAPSE_ROOT,timelapse_id,evidence,operation_id,lookup=True)
    except (EditUnavailable,RetirementUnavailable,MediaGuardUnavailable,OSError,ValueError):raise HTTPException(503,'Timelapse edit could not be confirmed') from None
    except Exception as error:
        if _edit_contention(error):raise HTTPException(503,'Timelapse edit is busy; retry the same operation') from None
        raise


# ====================================================================
# Cameras
# ====================================================================

@router.get("/cameras", tags=["Cameras"])
def list_cameras(db: Session = Depends(get_db), current_user: dict = Depends(require_role("viewer", scope="read:printers"))):
    """List printers with active camera streams.

    Returns cameras based on DB state (printers with camera URLs).
    go2rtc streams are synced lazily — if no streams exist yet, we
    trigger a config sync but don't block the response on it.
    """
    from core.rbac import get_org_scope
    org = get_org_scope(current_user)
    pq = db.query(Printer).filter(Printer.is_active.is_(True), Printer.camera_enabled.is_(True))
    if should_filter_org(current_user, org):
        pq = pq.filter((Printer.org_id == org) | (Printer.org_id == None) | (Printer.shared == True))
    printers = pq.all()

    # Build camera list from DB — don't gate on go2rtc being live
    cameras = []
    for p in printers:
        url = get_camera_url(p)
        if url:
            cameras.append({"id": p.id, "name": p.name, "has_camera": True, "display_order": p.display_order or 0, "camera_enabled": bool(p.camera_enabled)})

    # Ensure go2rtc config is synced (no-op if already up to date)
    if cameras:
        sync_go2rtc_config(db)

    return sorted(cameras, key=lambda x: x.get("display_order", 0))


@router.patch("/cameras/{printer_id}/toggle", tags=["Cameras"])
def toggle_camera(printer_id: int, current_user: dict = Depends(require_action("printers.edit", scope="write:printers")), db: Session = Depends(get_db)):
    """Toggle camera on/off for a printer."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")
    # Changing the camera is a change to the printer: org ownership only. Sharing grants viewing.
    if not check_org_access(current_user, printer.org_id):
        raise HTTPException(status_code=404, detail="Printer not found")

    # Toggle the camera_enabled flag
    # NULL streams nowhere (go2rtc and /cameras select TRUE only), so it is "off".
    new_state = not bool(printer.camera_enabled)
    db.execute(text("UPDATE printers SET camera_enabled = :enabled WHERE id = :id"),
               {"enabled": new_state, "id": printer_id})
    db.commit()

    return {"id": printer_id, "camera_enabled": new_state}


@router.get("/cameras/{printer_id}/stream", tags=["Cameras"])
def get_camera_stream(printer_id: int, db: Session = Depends(get_db), current_user: dict = Depends(require_role("viewer", scope="read:printers"))):
    """Get go2rtc stream info for a printer camera."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")
    if not check_org_access(current_user, printer.org_id) and not printer.shared:
        raise HTTPException(status_code=404, detail="Printer not found")

    if not printer.is_active or not printer.camera_enabled:
        raise HTTPException(status_code=404, detail="Camera not found")
    camera_url = get_camera_url(printer)
    if not camera_url:
        raise HTTPException(status_code=404, detail="Camera not found")

    stream_name = f"printer_{printer_id}"

    return {
        "printer_id": printer_id,
        "printer_name": printer.name,
        "stream_name": stream_name,
        "webrtc_url": f"/api/cameras/{printer_id}/webrtc",
    }


@router.post("/cameras/{printer_id}/webrtc", tags=["Cameras"])
async def camera_webrtc(printer_id: int, request: Request, db: Session = Depends(get_db), current_user: dict = Depends(require_role("viewer", scope="read:printers"))):
    """Proxy WebRTC signaling to go2rtc."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")
    if not check_org_access(current_user, printer.org_id) and not printer.shared:
        raise HTTPException(status_code=404, detail="Printer not found")

    if not printer.is_active or not printer.camera_enabled:
        raise HTTPException(status_code=404, detail="Camera not found")
    camera_url = get_camera_url(printer)
    if not camera_url:
        raise HTTPException(status_code=404, detail="Camera not found")

    stream_name = f"printer_{printer_id}"
    body = await request.body()

    # Ensure go2rtc has this stream configured before proxying.
    # Only sync if the stream is genuinely missing — never restart just
    # because go2rtc is temporarily unreachable (e.g. mid-restart).
    try:
        async with httpx.AsyncClient(**go2rtc_client_kwargs()) as client:
            streams_resp = await client.get(
                f"{go2rtc_api_url()}/api/streams", timeout=2.0,
            )
            if streams_resp.status_code == 200:
                streams = streams_resp.json()
                if stream_name not in streams:
                    sync_go2rtc_config(db)
    except Exception:
        # go2rtc not reachable — write config but don't force a restart
        # (it may already be restarting). The client will retry.
        sync_go2rtc_config(db)

    try:
        async with httpx.AsyncClient(**go2rtc_client_kwargs()) as client:
            resp = await client.post(
                f"{go2rtc_api_url()}/api/webrtc?src={stream_name}",
                content=body,
                headers={"Content-Type": request.headers.get("content-type", "application/sdp")},
                timeout=5.0,
            )
        if resp.status_code >= 400:
            raise HTTPException(status_code=502, detail=f"go2rtc returned {resp.status_code}")
        return Response(content=resp.content, media_type=resp.headers.get("content-type", "application/sdp"))
    except httpx.ConnectError:
        raise HTTPException(status_code=502, detail="go2rtc is not reachable")
    except (httpx.TimeoutException, httpx.RemoteProtocolError):
        raise HTTPException(status_code=504, detail="go2rtc not ready — try again")


# ====================================================================
# OBS overlay camera (public route, token required)
# ====================================================================



def overlay_camera_principal(db: Session, token: Optional[str], printer: Optional[Printer]) -> dict:
    """Authorize an overlay camera stream from a scoped API token.

    OBS browser sources cannot log in or send headers, so the copied overlay
    URL carries a per-user scoped API token (``?token=odin_...``, revocable
    and expirable in Settings > API Tokens). The token's user must be able to
    see the printer, exactly as for /cameras/{id}/webrtc.
    """
    from core.dependencies import resolve_user_api_token
    principal = resolve_user_api_token(token, db) if token else None
    if not principal:
        raise HTTPException(status_code=401, detail="A camera token is required")
    # Only a camera-only token (sole scope read:cameras) opens the relay: broader
    # tokens (read:printers, read, admin, ...) never belong in an OBS URL.
    from core.api_token_policy import is_camera_only
    if not is_camera_only(principal.get("_token_scopes") or []):
        raise HTTPException(status_code=403, detail="Overlay cameras need a camera-only (read:cameras) token")
    if printer is None or not (check_org_access(principal, printer.org_id) or printer.shared):
        raise HTTPException(status_code=404, detail="Printer not found")
    if not printer.is_active or not printer.camera_enabled or not get_camera_url(printer):
        raise HTTPException(status_code=404, detail="Camera not found")
    return principal


# A stream re-checks its token, user and camera this often, and ends on refusal.
OVERLAY_REVALIDATE_SECONDS = 30
OVERLAY_MAX_STREAMS_PER_TOKEN = 3
OVERLAY_MAX_STREAMS_PER_USER_PRINTER = 3   # one user's share of a printer's camera
OVERLAY_MAX_STREAMS_PER_PRINTER = 8        # small global ceiling per printer (per API worker)
# These caps are kept per API worker process (in memory): with N uvicorn workers a
# printer can serve up to N x the ceiling (and a token N x its cap). Accepted: the
# caps bound one worker's resources, and each stream still holds one go2rtc reader.
from modules.printers.mjpeg_relay import STREAMS as _overlay_streams


def overlay_stream_count() -> int:
    return len(_overlay_streams)


def _overlay_check(bind, token, printer_id) -> dict:
    """Blocking authorization (bcrypt, DB) in its own session. Returns who is watching
    and whether they may change the printer (owner/org operators and admins)."""
    with Session(bind=bind) as db:
        printer = db.query(Printer).filter(Printer.id == printer_id).first()
        principal = overlay_camera_principal(db, token, printer)
        privileged = (principal.get("role") in ("admin", "operator")
                      and check_org_access(principal, printer.org_id))
        return {"token_id": principal.get("_token_id"), "user_id": principal.get("id"), "privileged": bool(privileged)}


def _overlay_claim(viewer, printer_id):
    from modules.printers.mjpeg_relay import claim_stream
    return claim_stream(_overlay_streams, viewer, printer_id,
                        per_token=OVERLAY_MAX_STREAMS_PER_TOKEN,
                        per_user=OVERLAY_MAX_STREAMS_PER_USER_PRINTER,
                        per_printer=OVERLAY_MAX_STREAMS_PER_PRINTER,
                        label="overlay camera")


def _overlay_release(entry):
    from modules.printers.mjpeg_relay import release_stream
    release_stream(_overlay_streams, entry)


@router.get("/overlay/{printer_id}/camera", tags=["Overlay"])
async def overlay_camera_stream(printer_id: int, token: Optional[str] = Query(None), db: Session = Depends(get_db)):
    """Camera-only OBS token relay; recheck authority without holding a session."""
    from modules.printers.mjpeg_relay import open_mjpeg
    bind = db.get_bind()
    db.close()

    def sync():
        with Session(bind=bind) as session:
            sync_go2rtc_config(session)

    return await open_mjpeg(
        printer_id, check=lambda: _overlay_check(bind, token, printer_id),
        sync=sync, claim=_overlay_claim, release=_overlay_release,
        revalidate_seconds=OVERLAY_REVALIDATE_SECONDS,
    )
