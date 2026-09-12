"""EDU privacy regressions for erasure and shared-browser state."""

import inspect
from pathlib import Path

from modules.organizations import routes_sessions


def test_erasure_response_and_audit_do_not_reintroduce_former_identity():
    source = inspect.getsource(routes_sessions.erase_user_data)
    post_update = source.split("UPDATE users SET", 1)[1]
    assert "was: {user.username}" not in post_update
    assert 'f"User {user.username}' not in post_update
    assert '"anonymized_user_id"' in post_update
    assert '"actor_user_id"' in post_update


def test_frontend_has_no_persistent_identity_or_permission_writes():
    frontend = Path(__file__).resolve().parents[2] / "frontend/src"
    offenders = []
    for path in frontend.rglob("*"):
        if path.suffix not in {".ts", ".tsx", ".js", ".jsx"} or ".test." in path.name:
            continue
        source = path.read_text(encoding="utf-8")
        for key in ("odin_user", "rbac_permissions", "access_token", "refresh_token"):
            if f"localStorage.setItem('{key}'" in source or f'localStorage.setItem("{key}"' in source:
                offenders.append(f"{path.relative_to(frontend)} writes {key}")
    assert offenders == []
