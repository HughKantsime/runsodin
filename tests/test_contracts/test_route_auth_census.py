"""Every API route authenticates unless it is on the reviewed public allowlist (F11-11.3-02).

The perimeter admits any odin_-prefixed X-API-Key and leaves validation to the route, so a
route without an authenticating dependency is reachable with a fabricated token.
"""
import json,os,subprocess,sys,textwrap
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
PUBLIC={
    # Authentication flows (credential or one-time-code carriers of their own)
    'POST /api/auth/biometric-refresh','GET /api/auth/capabilities','POST /api/auth/forgot-password',
    'POST /api/auth/login','POST /api/auth/mfa/enrollment/cancel','POST /api/auth/mfa/enrollment/confirm',
    'POST /api/auth/mfa/enrollment/setup','POST /api/auth/mfa/verify','GET /api/auth/oidc/callback',
    'GET /api/auth/oidc/config','POST /api/auth/oidc/exchange','GET /api/auth/oidc/login','POST /api/auth/reset-password',
    # First-run setup (gated by setup phase)
    'POST /api/setup/admin','GET /api/setup/status','POST /api/setup/test-printer',
    # Pre-login presentation and health
    'GET /api/branding','GET /api/permissions','GET /api/license','GET /api/settings/education-mode',
    'GET /api/settings/language','GET /api/push/vapid-key','GET /api/bambu/filament-types','GET /api/version',
    'GET /api/health','GET /api/health/ready','GET /health','GET /health/ready','GET /api/system/license-server-status',
    'GET /api/docs','GET /api/redoc',
    # Public OBS overlay (no stored camera URL, see test_overlay_never_exposes_stored_camera_url)
    'GET /api/overlay/{printer_id}',
    # Internal HTTP-camera relay for go2rtc/ffmpeg: loopback peers without forwarding
    # headers plus a per-boot per-printer HMAC token (modules/printers/camera_relay.py)
    'GET /internal/camera-source/{printer_id}/{token}',
    # Overlay MJPEG relay: no session; the handler requires a camera-only (sole scope read:cameras) API
    # token in ?token= and the token user's printer access (test_cert_B_camera_fixes.py)
    'GET /api/overlay/{printer_id}/camera',
    # SPA fallback
    'GET /{path:path}',
}
PROBE=textwrap.dedent('''
import json,os
from pathlib import Path
from sqlalchemy import create_engine
from core.schema import bootstrap_database
bootstrap_database(create_engine(os.environ['DATABASE_URL']),Path('.'))
from core.app import create_app
from core.dependencies import get_current_user
from fastapi.routing import APIRoute
def reaches(dep,seen):
    for d in dep.dependencies:
        if d.call is get_current_user:return True
        if id(d) in seen:continue
        seen.add(id(d))
        if reaches(d,seen):return True
    return False
routes=sorted({m+' '+r.path.replace('/api/v1/','/api/') for r in create_app().routes if isinstance(r,APIRoute) and not reaches(r.dependant,set()) for m in r.methods if m!='HEAD'})
print(json.dumps(routes))
''')


def test_only_reviewed_public_routes_skip_authentication(tmp_path):
    env={**os.environ,'DATABASE_URL':f'sqlite:///{tmp_path}/census.db','JWT_SECRET_KEY':'census-test-key-only','PYTHONPATH':str(ROOT/'backend')}
    result=subprocess.run([sys.executable,'-c',PROBE],cwd=ROOT/'backend',env=env,capture_output=True,text=True,timeout=180)
    assert result.returncode==0,result.stderr[-3000:]
    unauthenticated=set(json.loads(result.stdout.strip().splitlines()[-1]))
    assert unauthenticated-PUBLIC==set(),'Routes reachable without authentication: '+', '.join(sorted(unauthenticated-PUBLIC))
