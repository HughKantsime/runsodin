"""Claims follow-up: code gaps the docs agent reported, checked through the real app.

Every probe runs ``core.app.create_app()`` in its own subprocess with a freshly
bootstrapped SQLite database and a signed throwaway license (the cert-G
harness). Requests are real HTTP with a real password-login JWT. Only the
outermost boundaries are replaced: the scheduler's wall clock is pinned, and
``smtplib.SMTP`` records instead of connecting.

* timeline.move (F04-4.8, docs features/timeline.md): the configured action
  permission decides who may drag a job, not a fixed Operator floor.
* Saved scheduler blackout hours (docs features/scheduler.md "Blackout Hours"):
  Settings > General saves them through PUT /api/config; the next scheduler run
  must honour them.
* Pro gates in ``license_manager.TIERS`` (sso, permissions, email_notifications)
  are enforced by the server: configuration writes are refused below Pro, reads
  stay available after a downgrade, and alert email stops like webhooks do.
"""
import pytest

from tests.test_contracts.test_cert_G_harness import run_probe

CLOCK = r'''
from datetime import datetime, timezone, timedelta
NOON = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
class FixedClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOON if tz is None else NOON.astimezone(tz)
from modules.jobs import scheduler as SCHED
SCHED.datetime = FixedClock
'''

TIMELINE_PROBE = r'''
install_license("pro")
seed_user("gap-admin", "admin")
seed_user("gap-operator", "operator")
seed_user("gap-viewer", "viewer")
sql("INSERT INTO printers(id,name,model,machine_type,is_active,api_type) VALUES (21,'Gap-21','Bambu Lab P1S','P1S',1,'bambu')")
sql("INSERT INTO jobs(id,item_name,status,quantity,duration_hours,priority,created_at) VALUES (31,'Gap job','pending',1,1,3,'2026-10-01 00:00:00')")
admin, operator, viewer = login("gap-admin"), login("gap-operator"), login("gap-viewer")

def move(c, hour):
    r = c.patch("/api/jobs/31/move", json={"printer_id": 21, "scheduled_start": f"2026-11-02T{hour:02d}:00:00Z"})
    return r.status_code

def set_move_roles(roles):
    perms = body(admin.get("/api/permissions"))
    perms["action_access"]["timeline.move"] = roles
    return admin.put("/api/permissions", json=perms).status_code

OUT["defaults"] = {"operator": move(operator, 1), "viewer": move(viewer, 2), "admin": move(admin, 3)}
OUT["restrict_status"] = set_move_roles(["admin"])
OUT["restricted"] = {"operator": move(operator, 4), "admin": move(admin, 5)}
OUT["widen_status"] = set_move_roles(["admin", "operator", "viewer"])
OUT["widened"] = {"viewer": move(viewer, 6)}
OUT["final_start"] = sql("SELECT scheduled_start FROM jobs WHERE id=31")[0][0]
'''

BLACKOUT_PROBE = CLOCK + r'''
install_license("pro")
admin_id = seed_user("gap-admin", "admin")
admin = login("gap-admin")
sql("INSERT INTO printers(id,name,model,machine_type,is_active,api_type) VALUES (21,'Gap-21','Bambu Lab P1S','P1S',1,'bambu')")
sql("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES (21,1,'PLA')")
sql("INSERT INTO jobs(id,item_name,status,quantity,duration_hours,priority,hold,charged_to_user_id,target_type,created_at) "
    "VALUES (41,'Blackout job','pending',1,1,3,0,:u,'specific','2026-10-01 00:00:00')", u=admin_id)
OUT["save"] = admin.put("/api/config", json={"blackout_start": "08:00", "blackout_end": "20:00"}).status_code
OUT["config_after_save"] = body(admin.get("/api/config"))
run = admin.post("/api/scheduler/run")
OUT["run"] = run.status_code
OUT["scheduled"] = sql("SELECT status, printer_id, scheduled_start FROM jobs WHERE id=41")[0]
'''

GATE_PROBE = r'''
import smtplib
SENT = []
class _SMTP:
    def __init__(self, host, port=0, *a, **k): SENT.append(("connect", host))
    def starttls(self, *a, **k): pass
    def login(self, *a, **k): pass
    def send_message(self, msg, *a, **k): SENT.append(("send", msg["To"]))
    def sendmail(self, frm, to, msg, *a, **k): SENT.append(("send", to))
    def quit(self): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
smtplib.SMTP = _SMTP
smtplib.SMTP_SSL = _SMTP
import os, threading, time
tier = os.environ["CLAIMS_TIER"]
admin_id = seed_user("gap-admin", "admin")
sql("UPDATE users SET email='gap-admin@example.test' WHERE id=:i", i=admin_id)
# Configuration an install might already hold from a Pro period (a downgrade keeps it).
sql("INSERT INTO system_config(key, value) VALUES ('smtp_config', :v)",
    v='{"enabled": true, "host": "smtp.example.test", "port": 587, "username": "", "password": "", "from_address": "odin@example.test", "use_tls": false}')
install_license(None if tier == "community" else tier)
a = login("gap-admin")
def call(method, path, **kw):
    r = getattr(a, method)(path, **kw)
    return {"status": r.status_code, "body": body(r) if r.status_code >= 400 else None}
F = OUT["features"] = {}
perms = body(a.get("/api/permissions"))
F["permissions_read"] = {"status": a.get("/api/permissions").status_code, "body": None}
F["permissions_write"] = call("put", "/api/permissions", json=perms)
F["permissions_reset"] = call("post", "/api/permissions/reset")
F["sso_read"] = call("get", "/api/admin/oidc")
F["sso_write"] = call("put", "/api/admin/oidc", json={"display_name": "Gap SSO"})
F["oidc_public"] = call("get", "/api/auth/oidc/config")
F["smtp_read"] = call("get", "/api/smtp-config")
F["smtp_write"] = call("put", "/api/smtp-config", json={"enabled": True, "host": "smtp.example.test", "port": 587,
                                                         "from_address": "odin@example.test", "use_tls": False})
F["smtp_test"] = call("post", "/api/alerts/test-email")
time.sleep(0.5)
OUT["test_email_sent"] = [s for s in SENT if s[0] == "send"]
SENT.clear()
# Alert email delivery through both dispatch paths (channels + dispatcher), real code, SMTP boundary recorded.
from modules.notifications.channels import send_email
send_email(admin_id, "print_failed", "Gap alert", "message")
from core.db import SessionLocal
from modules.notifications.alert_dispatcher import _deliver_email
s = SessionLocal()
try:
    _deliver_email(s, admin_id, "Gap alert 2", "message", "info")
finally:
    s.close()
time.sleep(0.5)
OUT["alert_email_sent"] = [x for x in SENT if x[0] == "send"]
'''

TIERS = ("community", "pro")


@pytest.fixture(scope="module")
def timeline(tmp_path_factory):
    return run_probe("claims-timeline", TIMELINE_PROBE, tmp_path_factory.mktemp("claims-timeline"))


@pytest.fixture(scope="module")
def blackout(tmp_path_factory):
    root = tmp_path_factory.mktemp("claims-blackout")
    return run_probe("claims-blackout", BLACKOUT_PROBE, root, {"ENV_FILE_PATH": str(root / "data.env")})


@pytest.fixture(scope="module")
def gates(tmp_path_factory):
    root = tmp_path_factory.mktemp("claims-gates")
    return {t: run_probe(f"claims-gates-{t}", GATE_PROBE, root, {"CLAIMS_TIER": t}) for t in TIERS}


# ---------- timeline.move is the configured action permission ----------

def test_timeline_move_default_permission_is_operator_and_admin(timeline):
    assert timeline["defaults"] == {"operator": 200, "viewer": 403, "admin": 200}


def test_timeline_move_removed_from_operator_is_refused_by_the_server(timeline):
    assert timeline["restrict_status"] == 200
    assert timeline["restricted"] == {"operator": 403, "admin": 200}


def test_timeline_move_granted_to_viewer_is_accepted_by_the_server(timeline):
    assert timeline["widen_status"] == 200
    assert timeline["widened"] == {"viewer": 200}
    assert str(timeline["final_start"]).startswith("2026-11-02 06:00")


def test_no_network_during_timeline_probe(timeline):
    assert timeline["_net_attempts"] == []


# ---------- saved blackout hours drive the scheduler ----------

def test_saved_blackout_hours_are_read_back(blackout):
    assert blackout["save"] == 200
    assert blackout["config_after_save"]["blackout_start"] == "08:00"
    assert blackout["config_after_save"]["blackout_end"] == "20:00"


def test_scheduler_run_honours_saved_blackout_hours(blackout):
    """Clock 12:00 UTC: default 22:30-05:30 would start the job now; saved 08:00-20:00 pushes it to 20:00."""
    assert blackout["run"] == 200
    status, printer, start = blackout["scheduled"]
    assert (status, printer) == ("scheduled", 21)
    assert str(start).startswith("2026-10-12 20:00"), start


# ---------- Pro gates from license_manager.TIERS ----------

GATED_WRITES = {"permissions_write": "permissions", "permissions_reset": "permissions",
                "sso_write": "sso", "smtp_write": "email_notifications", "smtp_test": "email_notifications"}


@pytest.mark.parametrize("call_name", sorted(GATED_WRITES))
def test_pro_configuration_writes_are_refused_on_community(gates, call_name):
    c = gates["community"]["features"][call_name]
    assert c["status"] == 403, c
    assert c["body"]["detail"] == (f"Feature '{GATED_WRITES[call_name]}' requires Pro or Education or Enterprise. "
                                   "Current tier: community")


@pytest.mark.parametrize("call_name", sorted(GATED_WRITES))
def test_pro_configuration_writes_are_allowed_on_pro(gates, call_name):
    assert 200 <= gates["pro"]["features"][call_name]["status"] < 300, gates["pro"]["features"][call_name]


@pytest.mark.parametrize("call_name", ["permissions_read", "sso_read", "oidc_public", "smtp_read"])
def test_reads_stay_available_after_a_downgrade(gates, call_name):
    for t in TIERS:
        assert 200 <= gates[t]["features"][call_name]["status"] < 300, (t, gates[t]["features"][call_name])


def test_alert_email_is_not_delivered_below_pro(gates):
    assert gates["community"]["alert_email_sent"] == []
    assert gates["community"]["test_email_sent"] == []


def test_alert_email_is_delivered_on_pro(gates):
    assert gates["pro"]["alert_email_sent"] == [["send", "gap-admin@example.test"]] * 2
    assert gates["pro"]["test_email_sent"] == [["send", "gap-admin@example.test"]]


def test_no_network_during_gate_probes(gates):
    assert all(gates[t]["_net_attempts"] == [] for t in TIERS)
