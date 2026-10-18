from __future__ import annotations

from pathlib import Path
import pytest

from playwright.sync_api import Page, expect

from .conftest import BASE_URL, PERSONAS


FIXTURE = Path(__file__).parents[1] / "fixtures" / "release_gate" / "candidate-calibration-cube.3mf"
# A real sliced Bambu A1 package (plate 1 G-code), shared with the cert-B live evidence.
SLICED_FIXTURE = Path(__file__).parents[1] / "fixtures" / "certification" / "cert-B" / "a1-acceptance-coupon.gcode.3mf"
SLICED_PROJECT = "a1-acceptance-coupon.gcode"


def _goto(page: Page, path: str) -> None:
    page.goto(f"{BASE_URL}{path}", wait_until="domcontentloaded", timeout=20_000)


def test_login_feedback_valid_login_and_logout(page: Page):
    _goto(page, "/login")
    page.locator("#login-username").fill(PERSONAS["admin"][0])
    page.locator("#login-password").fill("definitely-wrong")
    page.get_by_role("button", name="Sign In", exact=True).click()
    expect(page.get_by_text("Invalid credentials", exact=True)).to_be_visible()

    page.locator("#login-password").fill(PERSONAS["admin"][1])
    page.get_by_role("button", name="Sign In", exact=True).click()
    expect(page.get_by_role("heading", name="Dashboard", exact=True)).to_be_visible(timeout=20_000)
    page.get_by_role("button", name="Logout", exact=True).click()
    expect(page.get_by_role("heading", name="Confirm Logout", exact=True)).to_be_visible()
    page.get_by_role("button", name="Logout", exact=True).last.click()
    page.wait_for_url(f"{BASE_URL}/login", timeout=20_000)


def test_admin_operational_navigation_and_seeded_content(role_page_factory):
    page = role_page_factory("admin")
    checks = (
        ("/printers", "Printers", "Synthetic / No Hardware"),
        ("/jobs", "Jobs", "ODIN Candidate Cube"),
        ("/models", "Models", "ODIN Candidate Calibration Cube"),
        ("/spools", "Spools", "ODIN Candidate"),
        ("/upload", "Upload Print File", "Drop .3mf file here"),
    )
    for path, heading, content in checks:
        _goto(page, path)
        expect(page.get_by_role("heading", name=heading, exact=True)).to_be_visible(timeout=20_000)
        expect(page.get_by_text(content, exact=False).first).to_be_visible(timeout=20_000)


def test_community_gates_and_access_tab_behavior(role_page_factory):
    """The exact unlicensed image is Community: Pro pages and Settings tabs are locked.

    2.0 (F20-20.1-04) opens Access on every tier: users, MFA, tokens, sessions, the IP
    allowlist and organizations are free; SSO and custom permissions show a license notice.
    Pro behaviour itself is proved by CV02 contracts (docs/TRUSTED_VALIDATION_SCOPE.md).
    """
    page = role_page_factory("admin")
    for path in ("/products", "/orders", "/consumables", "/maintenance", "/analytics"):
        _goto(page, path)
        expect(page.get_by_role("heading", name="Pro Feature", exact=True)).to_be_visible(timeout=20_000)

    _goto(page, "/settings")
    expect(page.get_by_role("heading", name="Settings", exact=True)).to_be_visible(timeout=20_000)
    expect(page.get_by_role("heading", name="Community Edition", exact=True)).to_be_visible()
    for locked in ("Integrations", "Branding"):
        page.get_by_role("button", name=locked, exact=True).click()
        expect(page.get_by_role("heading", name="Community Edition", exact=True)).to_be_visible()
        expect(page.get_by_role("heading", name="Interface Mode", exact=True)).to_be_visible()
    expect(page.get_by_role("heading", name="White-Label Branding")).to_have_count(0)

    page.get_by_role("button", name="Access", exact=True).click()
    expect(page.get_by_role("button", name="Users & Groups", exact=True)).to_be_visible()
    expect(page.get_by_role("heading", name="Community Edition", exact=True)).to_have_count(0)
    page.get_by_role("button", name="Authentication (OIDC & MFA)", exact=True).click()
    expect(page.get_by_text("Single sign-on (OIDC) requires an O.D.I.N. Pro license.", exact=True)).to_be_visible()
    page.get_by_role("button", name="Permissions", exact=True).click()
    expect(page.get_by_text("Custom role permissions requires an O.D.I.N. Pro license.", exact=True)).to_be_visible()


def test_operator_navigation_and_admin_route_enforcement(role_page_factory):
    page = role_page_factory("operator")
    _goto(page, "/jobs")
    expect(page.get_by_role("heading", name="Jobs", exact=True)).to_be_visible(timeout=20_000)
    expect(page.get_by_role("button", name="New Job", exact=True)).to_be_visible()
    _goto(page, "/settings")
    page.wait_for_url(BASE_URL + "/", timeout=20_000)
    expect(page.get_by_role("heading", name="Dashboard", exact=True)).to_be_visible()


def test_viewer_read_only_controls_and_admin_route_enforcement(role_page_factory):
    page = role_page_factory("viewer")
    _goto(page, "/jobs")
    expect(page.get_by_role("heading", name="Jobs", exact=True)).to_be_visible(timeout=20_000)
    expect(page.get_by_role("button", name="New Job", exact=True)).to_have_count(0)
    _goto(page, "/printers")
    expect(page.get_by_text("Synthetic / No Hardware", exact=False).first).to_be_visible(timeout=20_000)
    expect(page.get_by_role("button", name="Add Printer", exact=True)).to_have_count(0)
    _goto(page, "/settings")
    page.wait_for_url(BASE_URL + "/", timeout=20_000)


def test_mobile_navigation_and_horizontal_layout(role_page_factory):
    page = role_page_factory("admin", mobile=True)
    page.get_by_role("button", name="Open menu", exact=True).click()
    drawer = page.get_by_label("Main navigation")
    expect(drawer.get_by_role("button", name="Close menu", exact=True)).to_be_visible()
    drawer.get_by_role("link", name="Printers", exact=True).click()
    expect(page.get_by_role("heading", name="Printers", exact=True)).to_be_visible(timeout=20_000)
    dimensions = page.evaluate(
        "() => ({scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth})"
    )
    assert dimensions["scroll"] <= dimensions["client"] + 1, dimensions


def test_theme_persists_and_keyboard_skip_link_works(role_page_factory):
    page = role_page_factory("admin")
    toggle = page.get_by_role("button", name="Switch to light mode", exact=True)
    if toggle.count() == 0:
        toggle = page.get_by_role("button", name="Switch to dark mode", exact=True)
    original_label = toggle.get_attribute("aria-label")
    toggle.click()
    changed_label = "Switch to dark mode" if original_label == "Switch to light mode" else "Switch to light mode"
    expect(page.get_by_role("button", name=changed_label, exact=True)).to_be_visible()
    page.reload(wait_until="domcontentloaded")
    expect(page.get_by_role("button", name=changed_label, exact=True)).to_be_visible(timeout=20_000)

    page.keyboard.press("Tab")
    expect(page.get_by_role("link", name="Skip to content", exact=True)).to_be_focused()
    page.keyboard.press("Enter")
    expect(page.locator("#main-content")).to_be_focused()


def test_real_3mf_upload_reaches_model_library(role_page_factory):
    """Upload reaches the library; Quick Print queues only a dispatchable (sliced) file.

    2.0 file-bound jobs carry a validated plate (core.print_payload.validate_plate): an
    unsliced 3MF has no plate G-code, so Quick Print is offered disabled with the reason.
    """
    assert FIXTURE.is_file() and SLICED_FIXTURE.is_file()
    page = role_page_factory("admin")
    _goto(page, "/upload")
    page.locator("#file-upload").set_input_files(str(FIXTURE))
    expect(page.get_by_text("Added to Model Library", exact=True)).to_be_visible(timeout=30_000)
    expect(page.get_by_role("heading", name="ODIN Candidate Upload Cube", exact=True)).to_be_visible()
    expect(page.get_by_text("This file is not sliced. Print time and filament usage are estimates.", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Quick Print", exact=True)).to_be_disabled()
    expect(page.get_by_text("This 3MF has no sliced plate (Metadata/plate_1.gcode).", exact=False)).to_be_visible()
    expect(page.get_by_role("button", name="Queued!", exact=True)).to_have_count(0)

    page.get_by_role("button", name="Upload Another", exact=True).click()
    page.locator("#file-upload").set_input_files(str(SLICED_FIXTURE))
    expect(page.get_by_text("Added to Model Library", exact=True)).to_be_visible(timeout=30_000)
    expect(page.get_by_role("heading", name=SLICED_PROJECT, exact=True)).to_be_visible()
    page.get_by_role("button", name="Quick Print", exact=True).click()
    expect(page.get_by_role("button", name="Queued!", exact=True)).to_be_visible(timeout=20_000)
    _goto(page, "/jobs")
    row = page.get_by_role("row").filter(has_text=SLICED_PROJECT)
    expect(row).to_be_visible(timeout=20_000)
    expect(row).to_contain_text("pending")
    expect(page.get_by_role("row").filter(has_text="ODIN Candidate Upload Cube")).to_have_count(0)


def test_seeded_vision_detection_state(role_page_factory):
    page = role_page_factory("admin")
    _goto(page, "/detections")
    expect(page.get_by_role("heading", name="Vigil AI Detections", exact=True)).to_be_visible(timeout=20_000)
    expect(page.get_by_text("1 detection", exact=True)).to_be_visible(timeout=20_000)
    expect(page.get_by_text("Spaghetti", exact=True).first).to_be_visible()


def test_compiled_token_permission_selection_and_real_revoke(role_page_factory):
    """License presentation is substituted; token HTTP and authentication are real."""
    import requests
    page=role_page_factory('admin')
    page.route('**/api/license',lambda route:route.fulfill(json={
        'tier':'pro','features':[],'max_users':100,'max_printers':100,
        'licensee':'Synthetic UI presentation only'}))
    _goto(page,'/settings')
    page.get_by_role('button',name='Access',exact=True).click()
    page.get_by_role('button',name='Tokens & Sessions',exact=True).click()
    expect(page.get_by_role('heading',name='API Tokens',exact=True)).to_be_visible()
    page.get_by_role('button',name='New Token',exact=True).click()
    page.get_by_label('Token name',exact=True).fill('Synthetic browser token')
    create=page.get_by_role('button',name='Create Token',exact=True)
    expect(create).to_be_disabled()
    scope=page.get_by_role('button',name='Read Printers',exact=True)
    scope.click();expect(scope).to_have_attribute('aria-pressed','true')
    expect(create).to_be_enabled()
    create.click()
    expect(page.get_by_text("Token created. Copy it now. It won't be shown again.",exact=True)).to_be_visible()
    raw=page.get_by_role('button',name='Copy API token',exact=True).locator('..').locator('code').text_content().strip()
    opaque=raw.startswith('odin_')
    assert opaque
    # Never leave the one-time credential visible in an evidence screenshot.
    page.get_by_role('button',name='Dismiss',exact=True).click()
    headers={'X-API-Key':raw}
    response=requests.get(BASE_URL+'/api/printers',headers=headers,timeout=10)
    assert response.status_code==200
    response=requests.post(BASE_URL+'/api/models',headers=headers,json={'name':'Synthetic forbidden write'},timeout=10)
    assert response.status_code==403
    page.once('dialog',lambda dialog:dialog.accept())
    page.get_by_role('button',name='Revoke token Synthetic browser token',exact=True).click()
    expect(page.get_by_role('button',name='Revoke token Synthetic browser token',exact=True)).to_have_count(0)
    response=requests.get(BASE_URL+'/api/printers',headers=headers,timeout=10)
    assert response.status_code==401


def test_compiled_community_report_schedule_refusal_is_visible(role_page_factory,admin):
    """Scheduled reports (email_notifications) are Pro: the compiled UI shows the license refusal."""
    before=admin.get(f'{BASE_URL}/api/report-schedules',timeout=10)
    assert before.status_code==200
    page=role_page_factory('admin')
    _goto(page,'/settings')
    page.get_by_role('button',name='System',exact=True).click()
    expect(page.get_by_role('heading',name='Scheduled Reports',exact=True)).to_be_visible()
    page.get_by_role('button',name='New Schedule',exact=True).click()
    page.locator('#rs-name').fill('Synthetic compiled report')
    page.locator('#rs-type').select_option('job_summary')
    page.locator('#rs-recipients').fill('synthetic@example.invalid')
    page.get_by_role('button',name='Create Schedule',exact=True).click()
    expect(page.get_by_role('status').filter(has_text="Create schedule failed: Feature 'email_notifications' requires Pro")).to_contain_text('Current tier: community')
    expect(page.get_by_text('Synthetic compiled report',exact=True)).to_have_count(0)
    assert admin.get(f'{BASE_URL}/api/report-schedules',timeout=10).json()==before.json()


@pytest.mark.parametrize('mobile',[False,True])
def test_compiled_pricing_configuration_persists_zero_and_reports_invalid_divisor(role_page_factory,admin,mobile):
    import re
    page=role_page_factory('admin')
    if mobile:page.set_viewport_size({'width':390,'height':844})
    initialized=admin.put(f'{BASE_URL}/api/pricing-config',json={'hourly_rate':27},timeout=10)
    assert initialized.status_code==200
    _goto(page,'/calculator')
    expect(page.get_by_role('heading',name='Pricing Calculator',exact=True)).to_be_visible()
    def field(label):
        return page.locator('label').filter(has_text=re.compile('^'+re.escape(label)+'$')).locator('..').get_by_role('spinbutton')
    expect(field('Hourly Rate')).to_have_value('27')
    field('Hourly Rate').fill('0')
    with page.expect_response(lambda r:r.url.endswith('/api/pricing-config') and r.request.method=='PUT') as completed:
        page.get_by_role('button',name='Save Config',exact=True).click()
    assert completed.value.status==200
    saved=admin.get(f'{BASE_URL}/api/pricing-config',timeout=10).json()
    assert saved['hourly_rate']==0
    page.reload(wait_until='domcontentloaded')
    expect(field('Hourly Rate')).to_have_value('0')
    field('Spool Weight').fill('0')
    page.get_by_role('button',name='Save Config',exact=True).click()
    expect(page.get_by_text(re.compile('Save pricing config failed:'))).to_be_visible()
    assert admin.get(f'{BASE_URL}/api/pricing-config',timeout=10).json()==saved


@pytest.mark.parametrize('mobile',[False,True])
def test_compiled_printer_telemetry_panel_reads_authorized_history(role_page_factory,mobile):
    import re
    page=role_page_factory('viewer',mobile=mobile)
    _goto(page,'/printers')
    with page.expect_response(lambda r:'/telemetry?hours=24' in r.url) as first:
        page.get_by_role('button',name='Print telemetry',exact=True).click()
    assert first.value.status==200
    expect(page.get_by_role('heading',name='Print Telemetry',exact=True)).to_be_visible()
    expect(page.get_by_text(re.compile('^No telemetry data'))).to_be_visible()
    with page.expect_response(lambda r:'/telemetry?hours=6' in r.url) as narrowed:
        page.get_by_role('button',name='6h',exact=True).click()
    assert narrowed.value.status==200 and narrowed.value.json()==[]
    assert page.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth+1')

@pytest.mark.parametrize('mobile', [False, True])
def test_compiled_nozzle_install_retire_and_viewer_controls(role_page_factory, admin, mobile):
    listing = admin.get(f'{BASE_URL}/api/printers', timeout=10).json()
    printers = listing if isinstance(listing, list) else listing['items']
    printer = next(row for row in printers if row['name'] == 'ODIN Candidate Gate Printer')
    identifier = printer['id']
    page = role_page_factory('operator', mobile=mobile)
    _goto(page, '/printers')
    page.get_by_role('button', name='Nozzle lifecycle', exact=True).click()
    page.get_by_role('button', name='Track Nozzle', exact=True).click()
    page.get_by_placeholder('Notes (optional)').fill('Synthetic compiled nozzle acceptance')
    page.get_by_role('button', name='Install', exact=True).click()
    expect(page.get_by_role('button', name='Retire', exact=True)).to_be_visible()
    current = admin.get(f'{BASE_URL}/api/printers/{identifier}/nozzle', timeout=10).json()
    assert current['notes'] == 'Synthetic compiled nozzle acceptance'
    assert current['print_hours_accumulated'] == 0 and current['print_count'] == 0
    viewer = role_page_factory('viewer', mobile=mobile)
    _goto(viewer, '/printers')
    viewer.get_by_role('button', name='Nozzle lifecycle', exact=True).click()
    expect(viewer.get_by_text('hardened_steel', exact=True)).to_be_visible()
    expect(viewer.get_by_role('button', name='Retire', exact=True)).to_have_count(0)
    expect(viewer.get_by_role('button', name='Replace', exact=True)).to_have_count(0)
    page.once('dialog', lambda dialog: dialog.accept())
    page.get_by_role('button', name='Retire', exact=True).click()
    expect(page.get_by_text('No nozzle tracked', exact=True)).to_be_visible()
    assert admin.get(f'{BASE_URL}/api/printers/{identifier}/nozzle', timeout=10).json() is None
    history = admin.get(f'{BASE_URL}/api/printers/{identifier}/nozzle/history', timeout=10).json()
    assert next(row for row in history if row['id'] == current['id'])['removed_at'] is not None
    assert page.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth+1')

@pytest.mark.parametrize('mobile', [False, True])
def test_compiled_printer_heartbeat_timezone_and_future_display(role_page_factory, mobile):
    from datetime import datetime, timedelta, timezone
    page = role_page_factory('viewer', mobile=mobile)
    heartbeat = {'value': (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()}
    def synthetic_timestamp(route):
        response = route.fetch()
        data = response.json()
        rows = data if isinstance(data, list) else data['items']
        for row in rows:
            row['last_seen'] = heartbeat['value']
        route.fulfill(response=response, json=data)
    # Substitute only heartbeat presentation, never command a real printer.
    import re
    page.route(re.compile(r'/api/printers(?:\?|$)'), synthetic_timestamp)
    _goto(page, '/printers')
    expect(page.get_by_test_id('printer-status')).to_have_text('Online')
    heartbeat['value'] = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    page.reload(wait_until='domcontentloaded')
    expect(page.get_by_test_id('printer-status')).to_have_text('Offline')
    assert page.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth+1')
    page.unroute_all(behavior='wait')

@pytest.mark.parametrize('mobile', [False, True])
def test_compiled_hms_viewer_controls_and_visible_history_failure(role_page_factory, mobile):
    import re
    page = role_page_factory('viewer', mobile=mobile)
    mode = {'fail': False}
    def synthetic_history(route):
        response = route.fetch()
        assert response.status == 200
        if mode['fail']:
            route.fulfill(status=503, json={'detail': 'Synthetic history unavailable'})
        else:
            route.fulfill(response=response, json={'entries': [{'id': 1, 'code': 'SYNTHETIC', 'message': 'Synthetic history evidence', 'severity': 'error', 'occurred_at': '2026-10-11T12:00:00Z'}], 'frequency': {'SYNTHETIC': 1}, 'total': 1})
    # Historical presentation fixture only; never issue a clear command.
    page.route(re.compile(r'/api/printers/\d+/hms-history\?'), synthetic_history)
    _goto(page, '/printers')
    page.get_by_role('button', name='HMS error history', exact=True).click()
    expect(page.get_by_text('Top Recurring (1 total)', exact=True)).to_be_visible()
    expect(page.get_by_role('button', name='Clear Errors', exact=True)).to_have_count(0)
    mode['fail'] = True
    page.get_by_role('button', name='7d', exact=True).click()
    expect(page.get_by_role('alert')).to_contain_text('Could not load HMS history')
    expect(page.get_by_text('No HMS errors in the last 7 days', exact=True)).to_have_count(0)
    mode['fail'] = False
    page.get_by_role('button', name='Retry', exact=True).click()
    expect(page.get_by_text('Top Recurring (1 total)', exact=True)).to_be_visible()
    assert page.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth+1')
    page.unroute_all(behavior='wait')

@pytest.mark.parametrize('role', ['operator', 'viewer'])
def test_compiled_audit_direct_route_denies_non_admin(role_page_factory, role):
    page = role_page_factory(role)
    _goto(page, '/audit')
    page.wait_for_url(BASE_URL + '/', timeout=20_000)
    expect(page.get_by_role('heading', name='Dashboard', exact=True)).to_be_visible()

@pytest.mark.parametrize('mobile', [False, True])
def test_compiled_ams_units_humidity_label_and_request_recovery(role_page_factory, mobile):
    from datetime import datetime, timedelta, timezone
    import re
    page = role_page_factory('viewer', mobile=mobile)
    mode = {'fail': False}
    def historical_presentation(route):
        response = route.fetch()
        assert response.status == 200
        if mode['fail']:
            route.fulfill(status=503, json={'detail': 'Synthetic AMS history unavailable'})
            return
        now = datetime.now(timezone.utc)
        units = {str(unit): [{'time':(now-timedelta(hours=2)).isoformat(),'temperature':20+unit*10,'humidity':2+unit}, {'time':(now-timedelta(minutes=30)).isoformat(),'temperature':24+unit*10,'humidity':3+unit}] for unit in (0,1)}
        route.fulfill(response=response, json={'printer_id':1,'hours':24,'units':units,'humidity_unit':'level'})
    # Historical presentation only. Actual typed storage acceptance runs separately.
    page.route(re.compile(r'/api/printers/\d+/ams/environment\?'), historical_presentation)
    _goto(page, '/printers')
    page.get_by_role('button', name='AMS environment data', exact=True).click()
    select = page.get_by_role('combobox', name='AMS unit', exact=True)
    expect(select).to_have_value('0')
    expect(page.get_by_text('Humidity level (1–5)', exact=True)).to_be_visible()
    expect(page.get_by_text('Humidity (%)', exact=True)).to_have_count(0)
    expect(page.locator('.recharts-line-curve')).to_have_count(2)
    select.select_option('1')
    expect(select).to_have_value('1')
    expect(page.locator('.recharts-line-curve')).to_have_count(2)
    mode['fail'] = True
    page.get_by_role('button', name='6h', exact=True).click()
    expect(page.get_by_role('alert')).to_contain_text('Could not load AMS environment data')
    expect(page.get_by_text('No AMS environment data available', exact=True)).to_have_count(0)
    mode['fail'] = False
    page.get_by_role('button', name='Retry', exact=True).click()
    expect(select).to_have_value('1')
    expect(page.locator('.recharts-line-curve')).to_have_count(2)
    assert page.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth+1')
    import os
    artifact_dir = Path(os.environ['ODIN_CANDIDATE_ARTIFACT_DIR'])
    page.wait_for_timeout(1600)  # Finish the chart's presentation animation before visual QA.
    panel = page.get_by_role('heading', name='AMS Environment', exact=True).locator('..').locator('..')
    if mobile:
        # The scroll viewport itself must stop above fixed navigation, so its
        # final content remains reachable without being covered by the tabs.
        main_box = page.locator('#main-content').bounding_box()
        nav_box = page.locator('nav.fixed').filter(has=page.get_by_role('link', name='Dashboard', exact=True)).bounding_box()
        assert main_box and nav_box
        assert main_box['y'] + main_box['height'] <= nav_box['y'] + 1
        page.locator('#main-content').evaluate('(element) => { element.scrollTop = element.scrollHeight }')
    panel.screenshot(path=artifact_dir / f'compiled-ams-mobile-{mobile}.png')
    page.unroute_all(behavior='wait')


@pytest.mark.parametrize('mobile',[False,True])
def test_compiled_camera_pip_navigation_and_access_cleanup(role_page_factory,mobile):
    """Actual auth/bootstrap/App; synthetic camera list, signaling and browser media."""
    import os
    page=role_page_factory('admin',mobile=mobile)
    me=page.request.get(BASE_URL+'/api/auth/me')
    assert me.status==200 and isinstance(me.json().get('id'),int) and me.json()['id']>0
    page.add_init_script("""(() => {
      window._cameraPeers=[];
      window.RTCPeerConnection=class {
        constructor(){this.closed=false;this.iceConnectionState='new';window._cameraPeers.push(this)}
        addTransceiver(){}
        async createOffer(){return {type:'offer',sdp:'synthetic-offer'}}
        async setLocalDescription(){}
        async setRemoteDescription(){
          if(this.closed)return;
          const canvas=document.createElement('canvas');canvas.width=32;canvas.height=32;
          const paint=()=>{const c=canvas.getContext('2d');c.fillStyle='#00aa44';c.fillRect(0,0,32,32)};
          paint();this.stream=canvas.captureStream(10);this.timer=setInterval(paint,100);
          this.ontrack?.({streams:[this.stream]});
        }
        close(){this.closed=true;clearInterval(this.timer);this.stream?.getTracks().forEach(t=>t.stop())}
      };
    })();""")
    state={'removed':False,'denied':False}
    def camera_list(route):
        if state['denied']:route.fulfill(status=403,json={'detail':'Synthetic access revoked'})
        else:route.fulfill(json=[] if state['removed'] else [{'id':900,'name':'Synthetic camera','camera_enabled':True,'has_camera':True,'display_order':0}])
    page.route('**/api/cameras',camera_list)
    page.route('**/api/cameras/900/webrtc',lambda route:route.fulfill(status=200,content_type='application/sdp',body='synthetic-answer'))
    page.route('**/api/printers/900/vision',lambda route:route.fulfill(json={'enabled':False}))
    _goto(page,'/cameras')
    expect(page.get_by_role('link',name='Synthetic camera',exact=True)).to_be_visible(timeout=20_000)
    page.get_by_title('Picture-in-Picture',exact=True).click()
    player=page.get_by_role('region',name='Picture-in-picture camera')
    expect(player).to_be_visible();expect(player.get_by_text('LIVE',exact=True)).to_be_visible()
    assert player.locator('video').evaluate('(video)=>video.srcObject?.getVideoTracks().length===1')
    page.wait_for_function("""()=>{const video=document.querySelector('[aria-label="Camera stream"]');return video?.readyState>=2&&video.videoWidth===32}""",timeout=15_000)
    if mobile:
        assert player.evaluate('(node)=>node.getBoundingClientRect().bottom<=document.querySelector("nav.fixed").getBoundingClientRect().top')
    assert page.evaluate('window._cameraPeers.filter(peer=>!peer.closed).length')==2
    # Use the rendered router link, preserving the same document and provider.
    page.locator('nav.fixed a[href="/jobs"]' if mobile else 'a[href="/jobs"]:visible').first.click()
    expect(page.get_by_role('heading',name='Jobs',exact=True)).to_be_visible(timeout=20_000)
    expect(player).to_be_visible();assert page.evaluate('window._cameraPeers.filter(peer=>!peer.closed).length')==1
    player.get_by_role('button',name='Resize camera player').click()
    assert player.evaluate('(node)=>{const r=node.getBoundingClientRect();return r.x>=0&&r.y>=0&&r.right<=innerWidth&&r.bottom<=innerHeight}')
    player.screenshot(path=str(Path(os.environ['ODIN_CANDIDATE_ARTIFACT_DIR'])/f'camera-player-{mobile}.png'))
    state['denied']=True
    # The periodic list recheck is 30 seconds; advance browser timers naturally.
    expect(player).to_have_count(0,timeout=40_000)
    assert page.evaluate('window._cameraPeers.filter(peer=>!peer.closed).length')==0
    page.unroute_all(behavior='wait')


def test_compiled_tv_failure_retry_and_fleet_counts(role_page_factory):
    page=role_page_factory('admin')
    state={'failed':True}
    printers=[{'id':i,'name':f'TV synthetic {i}','gcode_state':'IDLE','camera_enabled':False,'last_seen':None} for i in range(1,14)]
    printers[0].update(gcode_state='RUNNING',mc_percent=45,mc_remaining_time=75,gcode_file='Synthetic TV job')
    printers[-1].update(gcode_state='PAUSE',hms_errors=[{'code':'Synthetic HMS'}])
    def printer_list(route):
        if state['failed']:route.fulfill(status=503,json={'detail':'Synthetic unavailable'})
        else:route.fulfill(json=printers)
    page.route('**/api/printers?*',printer_list)
    page.route('**/api/alerts/summary',lambda route:route.fulfill(status=503,json={'detail':'Synthetic unavailable'}))
    _goto(page,'/tv')
    expect(page.get_by_role('alert')).to_contain_text('Printer data unavailable',timeout=20_000)
    expect(page.get_by_text('Alert status unavailable',exact=True)).to_be_visible(timeout=20_000)
    expect(page.get_by_text('No active alerts',exact=True)).to_have_count(0)
    expect(page.get_by_text('0/0 Printing',exact=True)).to_have_count(0)
    state['failed']=False
    page.get_by_role('button',name='Retry printer data',exact=True).click()
    expect(page.get_by_text('2/13 Printing',exact=True)).to_be_visible(timeout=20_000)
    expect(page.get_by_text('1 Error',exact=True)).to_be_visible()
    expect(page.get_by_test_id('tv-printer-card')).to_have_count(12)
    expect(page.get_by_test_id('tv-printer-job')).to_have_text('Synthetic TV job')
    expect(page.get_by_text('ETA 1h 15m',exact=True)).to_be_visible()
    page.keyboard.press('Escape')
    expect(page.get_by_role('heading',name='Dashboard',exact=True)).to_be_visible(timeout=20_000)
    page.unroute_all(behavior='wait')


@pytest.mark.parametrize('mobile',[False,True])
def test_compiled_camera_detail_load_retry_and_fullscreen_failure(role_page_factory,mobile):
    page=role_page_factory('admin')
    if mobile:page.set_viewport_size({'width':390,'height':844})
    state={'failed':True}
    from datetime import datetime,timezone
    printer={'last_seen':datetime.now(timezone.utc).isoformat(),'id':900,'name':'Synthetic detail printer','gcode_state':'RUNNING','nozzle_temp':210,'nozzle_target_temp':220,'bed_temp':60,'bed_target_temp':60,'fan_speed':0,'mc_percent':45,'mc_remaining_time':75,'gcode_file':'Synthetic detail job','layer_num':12,'total_layer_num':100,'filament_slots':[]}
    def printer_list(route):
        if state['failed']:route.fulfill(status=503,json={'detail':'Synthetic unavailable'})
        else:route.fulfill(json=[printer])
    page.route('**/api/printers?*',printer_list)
    page.route('**/api/cameras/900/webrtc',lambda route:route.fulfill(status=503,json={'detail':'Synthetic unavailable'}))
    page.route('**/api/printers/900/vision',lambda route:route.fulfill(json={'enabled':False}))
    _goto(page,'/cameras/900')
    expect(page.get_by_role('alert')).to_contain_text('Printer data unavailable',timeout=20_000)
    expect(page.get_by_text('Printer not found',exact=True)).to_have_count(0)
    state['failed']=False
    page.get_by_role('button',name='Retry printer data',exact=True).click()
    expect(page.get_by_role('heading',name='Synthetic detail printer',exact=True)).to_be_visible(timeout=20_000)
    expect(page.get_by_text('210°/220°C',exact=True)).to_be_visible()
    expect(page.get_by_text('0%',exact=True)).to_be_visible()
    expect(page.get_by_text('Synthetic detail job',exact=True)).to_be_visible()
    expect(page.get_by_text('Layer 12/100',exact=True)).to_be_visible()
    page.evaluate("()=>{HTMLElement.prototype.requestFullscreen=()=>Promise.reject(new Error('Synthetic unsupported'))}")
    page.get_by_title('Toggle fullscreen',exact=True).click()
    expect(page.get_by_text('Fullscreen unavailable',exact=True)).to_be_visible()
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    page.unroute_all(behavior='wait')


@pytest.mark.parametrize('mobile',[False,True])
def test_archive_history_recovery_and_real_csv_download(role_page_factory,mobile):
    import re
    page=role_page_factory('admin',mobile=mobile)
    state={'unavailable':True}
    def archive_history(route):
        if state['unavailable']:
            route.fulfill(status=503,json={'detail':'Synthetic history unavailable'})
        else:
            route.continue_()
    page.route(re.compile(r'/api/archives\?'),archive_history)
    _goto(page,'/archives')
    alert=page.get_by_role('alert')
    expect(alert).to_contain_text('Archive history unavailable',timeout=20_000)
    expect(page.get_by_text('No archived prints yet',exact=True)).to_have_count(0)
    state['unavailable']=False
    page.get_by_role('button',name='Retry',exact=True).click()
    expect(page.get_by_text('No archived prints yet',exact=True)).to_be_visible(timeout=20_000)
    _goto(page,'/print-log?q=Synthetic+missing&status=completed')
    expect(page.get_by_role('heading',name='Print Log',exact=True)).to_be_visible()
    expected=page.request.get(BASE_URL+'/api/archives/log/export?status=completed&search=Synthetic+missing')
    assert expected.status==200
    assert expected.headers['content-type'].split(';')[0]=='text/csv'
    assert expected.body().startswith(b'Print Name,Printer,User,Status,Started,Completed,Duration (s),Filament (g),Cost,Tags')
    with page.expect_download(timeout=10_000) as delivered:
        page.get_by_role('button',name='Export CSV',exact=True).click()
    download=delivered.value
    assert download.suggested_filename=='print_log.csv'
    path=download.path()
    assert path and Path(path).read_bytes()==expected.body()
    page.route('**/api/archives/log/export?*',lambda route:route.fulfill(status=403,json={'detail':'Synthetic export denied'}))
    downloads=[]
    page.on('download',lambda event:downloads.append(event))
    page.get_by_role('button',name='Export CSV',exact=True).click()
    expect(page.get_by_text('Export failed: Synthetic export denied',exact=True)).to_be_visible(timeout=10_000)
    assert downloads==[]


@pytest.mark.parametrize('mobile', [False, True])
def test_compiled_model_material_search_and_favorite_persist(role_page_factory, admin, mobile):
    label = f'Synthetic material search {mobile}'
    created = admin.post(f'{BASE_URL}/api/models', json={'name':label, 'default_filament_type':'PETG'}, timeout=10)
    assert created.status_code == 201
    identifier = created.json()['id']
    try:
        page = role_page_factory('admin', mobile=mobile)
        _goto(page, '/models?q=petg')
        card = page.locator('div').filter(has=page.get_by_role('heading', name=label, exact=True)).filter(has=page.get_by_role('button', name='Schedule print job', exact=True)).last
        expect(page.get_by_role('heading', name=label, exact=True)).to_be_visible()
        card.get_by_role('button', name='Add to favorites', exact=True).click()
        expect(card.get_by_role('button', name='Remove from favorites', exact=True)).to_be_visible()
        assert admin.get(f'{BASE_URL}/api/models/{identifier}', timeout=10).json()['is_favorite'] is True
        page.reload(wait_until='domcontentloaded')
        expect(page.get_by_role('heading', name=label, exact=True)).to_be_visible()
        expect(page.get_by_placeholder('Search models...')).to_have_value('petg')
        card = page.locator('div').filter(has=page.get_by_role('heading', name=label, exact=True)).filter(has=page.get_by_role('button', name='Schedule print job', exact=True)).last
        card.get_by_role('button', name='Remove from favorites', exact=True).click()
        expect(card.get_by_role('button', name='Add to favorites', exact=True)).to_be_visible()
        assert admin.get(f'{BASE_URL}/api/models/{identifier}', timeout=10).json()['is_favorite'] is False
        assert page.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth+1')
    finally:
        assert admin.delete(f'{BASE_URL}/api/models/{identifier}', timeout=10).status_code == 204


@pytest.mark.parametrize('mobile', [False, True])
def test_compiled_profile_import_download_and_unavailable_retry(role_page_factory, admin, mobile, tmp_path):
    import json,re
    label = f'Synthetic_Profile_{mobile}'
    content = json.dumps({'name':label,'type':'filament','filament_type':['PETG'],'filament_flow_ratio':['0.98']})
    page = role_page_factory('admin', mobile=mobile)
    identifiers = []
    try:
        _goto(page, '/profiles')
        expect(page.get_by_role('heading',name='Profiles',exact=True)).to_be_visible()
        page.locator('input[type=file]').set_input_files({'name':f'{label}.json','mimeType':'application/json','buffer':content.encode()})
        expect(page.get_by_text('Imported 1 profile(s)',exact=True)).to_be_visible()
        page.get_by_placeholder('Search profiles...').fill(label)
        expect(page.get_by_text(label,exact=True)).to_be_visible()
        saved = admin.get(f'{BASE_URL}/api/profiles',params={'search':label},timeout=10).json()['profiles']
        identifiers = [row['id'] for row in saved]
        assert len(identifiers)==1
        detail = admin.get(f'{BASE_URL}/api/profiles/{identifiers[0]}',timeout=10).json()
        assert detail['raw_content']==content and detail['filament_type']=='PETG'
        card = page.locator('div').filter(has=page.get_by_text(label,exact=True)).filter(has=page.get_by_title('Download')).last
        with page.expect_download() as event:
            card.get_by_title('Download').click()
        download = event.value
        assert download.failure() is None and download.suggested_filename==f'{label}.json'
        target = tmp_path/f'{label}.json';download.save_as(target)
        assert target.read_text()==content
        matcher = re.compile(r'/api/profiles(?:\?.*)?$')
        page.route(matcher,lambda route:route.fulfill(status=503,json={'detail':'Synthetic profile list unavailable'}))
        _goto(page,'/profiles')
        expect(page.get_by_text('Unable to load profiles',exact=True)).to_be_visible(timeout=20_000)
        expect(page.get_by_text('No profiles yet',exact=True)).to_have_count(0)
        page.unroute(matcher)
        page.get_by_role('button',name='Retry',exact=True).click()
        expect(page.get_by_text(label,exact=True)).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth+1')
    finally:
        if not identifiers:
            identifiers = [row['id'] for row in admin.get(f'{BASE_URL}/api/profiles',params={'search':label},timeout=10).json()['profiles']]
        for identifier in identifiers:
            assert admin.delete(f'{BASE_URL}/api/profiles/{identifier}',timeout=10).status_code==204


@pytest.mark.parametrize('mobile',[False,True])
def test_compiled_generic_upload_persists_variant_and_previews_real_payload(role_page_factory,admin,mobile):
    page=role_page_factory('admin',mobile=mobile)
    _goto(page,'/upload')
    with page.expect_response(lambda response:response.url.endswith('/api/print-files/upload') and response.request.method=='POST',timeout=20_000) as uploaded:
        page.locator('#file-upload').set_input_files(FIXTURE)
    response=uploaded.value
    assert response.status==200
    body=response.json();file_id=body['id'];model_id=body['model_id']
    expect(page.get_by_role('heading',name=body['project_name'],exact=True)).to_be_visible()
    model=admin.get(f'{BASE_URL}/api/models/{model_id}',timeout=10)
    assert model.status_code==200
    preview=admin.get(f'{BASE_URL}/api/files/{file_id}/preview-model',timeout=10)
    assert preview.status_code==200 and preview.content==FIXTURE.read_bytes()
    variants=admin.get(f'{BASE_URL}/api/models/{model_id}/variants',timeout=10)
    assert variants.status_code==200 and file_id in {row['id'] for row in variants.json()['variants']}
    page.get_by_role('button',name='View in Library',exact=True).click()
    expect(page.get_by_role('heading',name=model.json()['name'],exact=True)).to_be_visible()
    page.reload(wait_until='domcontentloaded')
    expect(page.get_by_role('heading',name=model.json()['name'],exact=True)).to_be_visible()
    assert page.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth+1')


@pytest.mark.parametrize('mobile',[False,True])
def test_compiled_cfg_history_diff_and_restore_persist_without_printer_commands(role_page_factory,admin,tmp_path,mobile):
    label=f'SyntheticHistoryCfg{mobile}'
    original='[printer]\r\nkinematics: cartesian\r\nmax_velocity: 180\r\n'
    updated='[printer]\nkinematics: cartesian\nmax_velocity: 220\n'
    page=role_page_factory('admin',mobile=mobile)
    identifier=None
    apply_requests=[]
    page.on('request',lambda request:apply_requests.append(request.url) if '/api/profiles/' in request.url and request.url.endswith('/apply') else None)
    try:
        _goto(page,'/profiles')
        page.locator('input[type=file]').set_input_files({'name':label+'.cfg','mimeType':'text/plain','buffer':original.encode()})
        expect(page.get_by_text('Imported 1 profile(s)',exact=True)).to_be_visible()
        page.get_by_placeholder('Search profiles...').fill(label)
        expect(page.get_by_text(label,exact=True)).to_be_visible()
        saved=admin.get(f'{BASE_URL}/api/profiles',params={'search':label},timeout=10).json()['profiles']
        assert len(saved)==1
        identifier=saved[0]['id']
        assert admin.get(f'{BASE_URL}/api/profiles/{identifier}',timeout=10).json()['raw_content']==original
        card=page.locator('div').filter(has=page.get_by_text(label,exact=True)).filter(has=page.get_by_title('Profile history')).last
        expect(card.get_by_title('Apply to printer')).to_have_count(0)
        card.get_by_title('Profile history').click()
        dialog=page.get_by_role('dialog',name=f'History: {label}')
        expect(dialog.get_by_label('Current profile content')).to_have_value(original.replace('\r\n','\n'))
        expect(dialog.get_by_text('No saved revisions yet.',exact=False)).to_be_visible()
        dialog.get_by_label('Current profile content').fill(updated)
        dialog.get_by_label('Change notes').fill('Synthetic browser tuning')
        with page.expect_response(lambda response:response.url.endswith(f'/api/profiles/{identifier}/revisions') and response.request.method=='POST') as written:
            dialog.get_by_role('button',name='Save revision',exact=True).click()
        assert written.value.status==200 and written.value.json()['revision_number']==2
        expect(dialog.get_by_text('Revision 2 saved',exact=True)).to_be_visible()
        assert admin.get(f'{BASE_URL}/api/profiles/{identifier}',timeout=10).json()['raw_content']==updated
        history=admin.get(f'{BASE_URL}/api/profiles/{identifier}/revisions',timeout=10).json()
        assert [row['revision_number'] for row in history]==[2,1]
        dialog.get_by_role('button',name='Compare revision 1',exact=True).click()
        comparison=dialog.get_by_role('region',name='Revision comparison').locator('pre')
        expect(comparison).to_contain_text('-max_velocity: 180')
        expect(comparison).to_contain_text('+max_velocity: 220')
        dialog.get_by_role('button',name='Restore revision 1',exact=True).click()
        page.get_by_role('button',name='Restore content',exact=True).click()
        expect(dialog.get_by_text('Revision 3 saved from restored content',exact=True)).to_be_visible()
        expect(dialog.get_by_label('Current profile content')).to_have_value(original.replace('\r\n','\n'))
        assert admin.get(f'{BASE_URL}/api/profiles/{identifier}/export',timeout=10).content==original.encode()
        assert [row['revision_number'] for row in admin.get(f'{BASE_URL}/api/profiles/{identifier}/revisions',timeout=10).json()]==[3,2,1]
        page.screenshot(path=str(tmp_path/f'profile-history-{mobile}.png'))
        assert page.evaluate('document.documentElement.scrollWidth<=document.documentElement.clientWidth+1')
        page.reload(wait_until='domcontentloaded')
        page.get_by_placeholder('Search profiles...').fill(label)
        card=page.locator('div').filter(has=page.get_by_text(label,exact=True)).filter(has=page.get_by_title('Profile history')).last
        with page.expect_download() as event:card.get_by_title('Download').click()
        download=event.value
        target=tmp_path/(label+'.cfg');download.save_as(target)
        assert download.failure() is None and download.suggested_filename==label+'.cfg'
        assert target.read_bytes()==original.encode()
        assert apply_requests==[]
    finally:
        if identifier is not None:assert admin.delete(f'{BASE_URL}/api/profiles/{identifier}',timeout=10).status_code==204


def test_compiled_alert_delete_failure_retry_and_persisted_badge(role_page_factory, admin):
    """Owned synthetic alert; real delete/persistence/count, injected initial refusal."""
    page = role_page_factory('admin', mobile=True)
    response = admin.get(BASE_URL+'/api/alerts', timeout=10)
    assert response.status_code == 200
    alert = next(row for row in response.json() if row['title']=='ODIN Candidate Gate Alert')
    alert_id = alert['id']
    before = admin.get(BASE_URL+'/api/alerts/unread-count', timeout=10).json()['unread_count']
    _goto(page, '/alerts')
    expect(page.get_by_text('ODIN Candidate Gate Alert', exact=True)).to_be_visible()
    page.get_by_role('button',name='Info',exact=True).click()
    expect(page.get_by_text('ODIN Candidate Gate Alert',exact=True)).to_have_count(0)
    page.get_by_role('button',name='Warning',exact=True).click()
    expect(page.get_by_text('ODIN Candidate Gate Alert',exact=True)).to_be_visible()
    endpoint = f'**/api/alerts/{alert_id}'
    page.route(endpoint, lambda route: route.fulfill(status=503, content_type='application/json', body='{"detail":"Synthetic refusal"}'))
    page.get_by_role('button', name='Delete alert', exact=True).click()
    expect(page.get_by_text('Failed to delete alert', exact=True)).to_be_visible()
    expect(page.get_by_text('ODIN Candidate Gate Alert', exact=True)).to_be_visible()
    assert any(row['id']==alert_id for row in admin.get(BASE_URL+'/api/alerts', timeout=10).json())
    page.unroute(endpoint)
    page.get_by_role('button', name='Delete alert', exact=True).click()
    expect(page.get_by_text('ODIN Candidate Gate Alert', exact=True)).to_have_count(0)
    assert not any(row['id']==alert_id for row in admin.get(BASE_URL+'/api/alerts', timeout=10).json())
    assert admin.get(BASE_URL+'/api/alerts/unread-count', timeout=10).json()['unread_count']==before-1
    if before-1:
        expect(page.get_by_role('button', name=f'Notifications, {before-1} unread', exact=True)).to_be_visible()
    else:
        expect(page.get_by_role('button', name='Notifications', exact=True)).to_be_visible()
    page.reload(wait_until='domcontentloaded')
    expect(page.get_by_text('ODIN Candidate Gate Alert', exact=True)).to_have_count(0)
    desktop = role_page_factory('admin')
    _goto(desktop, '/alerts')
    expect(desktop.get_by_text('ODIN Candidate Gate Alert',exact=True)).to_have_count(0)


@pytest.mark.parametrize('mobile',[False,True])
def test_compiled_welcome_unconfirmed_feedback_synthetic_response(role_page_factory,admin,mobile):
    """Built UI feedback only: create/license responses substituted, no mail/account mutation."""
    page=role_page_factory('admin',mobile=mobile)
    originals=admin.get(BASE_URL+'/api/users',timeout=10).json()
    message='Account created, but welcome email delivery was not confirmed. Check email settings, then use Send password reset link for this user.'
    state={'created':False,'posts':0,'reads':0}
    name='Synthetic compiled welcome '+str(mobile)
    def license_facts(route):
        response=route.fetch();data=response.json()
        data['features']=list(set(data.get('features',[]))|{'user_groups'})
        data['max_users']=9999
        data['tier']='pro'  # Licensed UI feed only; backend license unchanged.
        route.fulfill(response=response,json=data)
    def users(route):
        if route.request.method=='POST':
            state['posts']+=1
            submitted=route.request.post_data_json
            assert submitted['send_welcome_email'] is True and submitted['username']==name
            state['created']=True
            route.fulfill(status=200,json={'status':'created','welcome_email':'unconfirmed','message':message})
        else:
            state['reads']+=1
            rows=list(originals)
            if state['created']:rows.append({'id':9900,'username':name,'email':'compiled@example.invalid','role':'viewer','is_active':True})
            route.fulfill(status=200,json=rows)
    page.route('**/api/license',license_facts)
    page.route('**/api/users',users)
    _goto(page,'/settings')
    page.get_by_role('button',name='Access',exact=True).click()
    expect(page.get_by_role('heading',name='Admin',exact=True)).to_be_visible()
    page.get_by_role('button',name='Add User',exact=True).click()
    form=page.locator('form').filter(has=page.get_by_role('button',name='Create User',exact=True))
    form.locator('input[type=text]').fill(name)
    form.locator('input[type=email]').fill('compiled@example.invalid')
    form.get_by_label('Send welcome email with auto-generated password').check()
    form.get_by_role('button',name='Create User',exact=True).click()
    expect(page.get_by_text(message,exact=True)).to_be_visible()
    expect(page.get_by_role('heading',name='New User',exact=True)).to_have_count(0)
    expect(page.get_by_text(name,exact=True)).to_be_visible()
    assert state['posts']==1 and state['reads']>=2
    assert admin.get(BASE_URL+'/api/users',timeout=10).json()==originals


@pytest.mark.parametrize('mobile',[False,True])
def test_compiled_admin_mfa_recovery_real_account(role_page_factory,admin,mobile):
    """Real owned account recovery; licensed UI feed substituted only."""
    import requests,pyotp
    username,password=PERSONAS['viewer']
    target=next(row for row in admin.get(BASE_URL+'/api/users',timeout=10).json() if row['username']==username)
    assert target['mfa_enabled'] is False
    viewer=requests.Session()
    try:
        assert viewer.post(BASE_URL+'/api/auth/login',data={'username':username,'password':password},timeout=10).status_code==200
        setup=viewer.post(BASE_URL+'/api/auth/mfa/setup',timeout=10)
        assert setup.status_code==200
        secret=setup.json()['secret']
        assert viewer.post(BASE_URL+'/api/auth/mfa/confirm',json={'code':pyotp.TOTP(secret).now()},timeout=10).status_code==200
        page=role_page_factory('admin',mobile=mobile)
        def licensed_ui(route):
            response=route.fetch();data=response.json()
            data['features']=list(set(data.get('features',[]))|{'user_groups'})
            data['tier']='pro';data['max_users']=9999
            route.fulfill(response=response,json=data)
        page.route('**/api/license',licensed_ui)
        deletes=[]
        page.on('request',lambda request:deletes.append(True) if request.method=='DELETE' and request.url.endswith('/mfa') else None)
        _goto(page,'/settings')
        page.get_by_role('button',name='Access',exact=True).click()
        button=page.get_by_role('button',name='Reset MFA for '+username,exact=True)
        expect(button).to_be_visible()
        button.click()
        expect(page.get_by_role('heading',name='Reset MFA',exact=True)).to_be_visible()
        assert deletes==[]
        page.get_by_role('button',name='Cancel',exact=True).click()
        assert deletes==[]
        assert next(row for row in admin.get(BASE_URL+'/api/users',timeout=10).json() if row['id']==target['id'])['mfa_enabled'] is True
        button.click()
        page.get_by_role('button',name='Disable MFA',exact=True).click()
        expect(page.get_by_text('MFA disabled. The user can enroll again in their account settings.',exact=True)).to_be_visible()
        expect(page.get_by_role('heading',name='Reset MFA',exact=True)).to_have_count(0)
        expect(button).to_have_count(0)
        assert deletes==[True]
        assert next(row for row in admin.get(BASE_URL+'/api/users',timeout=10).json() if row['id']==target['id'])['mfa_enabled'] is False
    finally:
        viewer.close()
        current=next(row for row in admin.get(BASE_URL+'/api/users',timeout=10).json() if row['id']==target['id'])
        if current['mfa_enabled']:
            assert admin.delete(BASE_URL+'/api/admin/users/'+str(target['id'])+'/mfa',timeout=10).status_code==200
        assert next(row for row in admin.get(BASE_URL+'/api/users',timeout=10).json() if row['id']==target['id'])['mfa_enabled'] is False


@pytest.mark.parametrize('mobile',[False,True])
def test_compiled_mfa_status_unknown_and_retry(role_page_factory,admin,mobile):
    """Built status UI with synthetic status/license feeds; no MFA mutation."""
    page=role_page_factory('admin',mobile=mobile)
    original=admin.get(BASE_URL+'/api/users',timeout=10).json()
    state={'reads':0,'mutations':0}
    def license_ui(route):
        response=route.fetch();data=response.json()
        data['tier']='pro';data['max_users']=9999
        data['features']=list(set(data.get('features',[]))|{'user_groups'})
        route.fulfill(response=response,json=data)
    def status(route):
        state['reads']+=1
        if state['reads']==1:route.fulfill(status=503,json={'detail':'Synthetic unavailable'})
        elif state['reads']==2:route.fulfill(status=200,json={'mfa_enabled':'false'})
        else:route.fulfill(status=200,json={'mfa_enabled':True})
    page.route('**/api/license',license_ui)
    page.route('**/api/auth/mfa/status',status)
    page.on('request',lambda request:state.update(mutations=state['mutations']+1) if '/api/auth/mfa' in request.url and request.method in ('POST','DELETE') else None)
    _goto(page,'/settings')
    page.get_by_role('button',name='Access',exact=True).click()
    page.get_by_role('button',name='Authentication (OIDC & MFA)',exact=True).click()
    message=page.get_by_text('Unable to load MFA status. Retry before making changes.',exact=True)
    expect(message).to_be_visible()
    expect(page.get_by_role('button',name='Set Up Two-Factor Authentication',exact=True)).to_have_count(0)
    expect(page.get_by_role('button',name='Disable MFA',exact=True)).to_have_count(0)
    with page.expect_response(lambda response: response.url.endswith('/api/auth/mfa/status')):
        page.get_by_role('button',name='Retry MFA status',exact=True).click()
    expect(message).to_be_visible()
    assert state['reads']==2
    expect(page.get_by_role('button',name='Set Up Two-Factor Authentication',exact=True)).to_have_count(0)
    page.get_by_role('button',name='Retry MFA status',exact=True).click()
    expect(page.get_by_text('Your account is protected with two-factor authentication.',exact=True)).to_be_visible()
    expect(message).to_have_count(0)
    assert state['reads']==3 and state['mutations']==0
    assert admin.get(BASE_URL+'/api/users',timeout=10).json()==original


@pytest.mark.parametrize('mobile',[False,True])
def test_compiled_required_mfa_password_enrollment_real_account(page,admin,mobile):
    """Actual owned HTTP enrollment and subsequent challenge, no auth response substitutes."""
    import pyotp
    username,password=PERSONAS['viewer']
    rows=admin.get(BASE_URL+'/api/users',timeout=10).json()
    target=next(row for row in rows if row['username']==username)
    assert target['mfa_enabled'] is False
    before=admin.get(BASE_URL+'/api/config/require-mfa',timeout=10).json()['require_mfa']
    try:
        assert admin.put(BASE_URL+'/api/config/require-mfa',json={'require_mfa':True},timeout=10).status_code==200
        if mobile:page.set_viewport_size({'width':390,'height':844})
        _goto(page,'/login')
        page.locator('#login-username').fill(username)
        page.locator('#login-password').fill(password)
        page.get_by_role('button',name='Sign In',exact=True).click()
        expect(page.get_by_role('heading',name='Set up two-factor authentication',exact=True)).to_be_visible()
        expect(page.get_by_alt_text('Authenticator setup QR code')).to_be_visible()
        assert page.request.get(BASE_URL+'/api/auth/me').status==401
        secret=page.locator('section[aria-label="Complete sign-in"] code').text_content()
        valid_secret=isinstance(secret,str) and len(secret)==32
        assert valid_secret
        page.locator('#continuation-code').fill(pyotp.TOTP(secret).now())
        page.get_by_role('button',name='Verify and sign in',exact=True).click()
        expect(page.get_by_role('heading',name='Dashboard',exact=True)).to_be_visible(timeout=20000)
        user=page.request.get(BASE_URL+'/api/auth/me').json()
        correct_user=user.get('username')==username
        assert correct_user
        dimensions=page.evaluate('() => ({scroll:document.documentElement.scrollWidth,client:document.documentElement.clientWidth})')
        assert dimensions['scroll']<=dimensions['client']+1
        page.context.clear_cookies()
        _goto(page,'/login')
        page.locator('#login-username').fill(username)
        page.locator('#login-password').fill(password)
        page.get_by_role('button',name='Sign In',exact=True).click()
        expect(page.get_by_role('heading',name='Two-factor authentication',exact=True)).to_be_visible()
        assert page.request.get(BASE_URL+'/api/auth/me').status==401
        page.locator('#continuation-code').fill(pyotp.TOTP(secret).now())
        page.get_by_role('button',name='Verify and sign in',exact=True).click()
        expect(page.get_by_role('heading',name='Dashboard',exact=True)).to_be_visible(timeout=20000)
    finally:
        assert admin.put(BASE_URL+'/api/config/require-mfa',json={'require_mfa':before},timeout=10).status_code==200
        assert admin.delete(BASE_URL+f'/api/admin/users/{target["id"]}/mfa',timeout=10).status_code==200

@pytest.mark.parametrize('mobile',[False,True])
def test_compiled_sso_continuation_real_restricted_authorities(page,admin,mobile):
    """SSO exchange transport substituted; real login grants/enrollment/challenge/cookies."""
    import requests,pyotp
    username,password=PERSONAS['viewer']
    target=next(row for row in admin.get(BASE_URL+'/api/users',timeout=10).json() if row['username']==username)
    before=admin.get(BASE_URL+'/api/config/require-mfa',timeout=10).json()['require_mfa']
    try:
        assert admin.put(BASE_URL+'/api/config/require-mfa',json={'require_mfa':True},timeout=10).status_code==200
        if mobile:page.set_viewport_size({'width':390,'height':844})
        with requests.Session() as client:
            grant=client.post(BASE_URL+'/api/auth/login',data={'username':username,'password':password},timeout=10)
        assert grant.status_code==200 and grant.json().get('mfa_setup_required') is True
        current={'response':grant.json(),'exchanges':0}
        def exchange(route):
            current['exchanges']+=1
            route.fulfill(status=200,json=current['response'])
        page.route('**/api/auth/oidc/exchange',exchange)
        # 2.0 delivers the one-time code in the URL fragment, never to a server (OIDCBootstrap).
        _goto(page,'/login#oidc_code=synthetic-owned-code')
        expect(page.get_by_alt_text('Authenticator setup QR code')).to_be_visible()
        assert page.request.get(BASE_URL+'/api/auth/me').status==401
        assert current['exchanges']==1
        assert page.evaluate('location.search')=='' and page.evaluate('location.hash')==''
        secret=page.locator('section[aria-label="Complete sign-in"] code').text_content()
        valid_secret=isinstance(secret,str) and len(secret)==32
        assert valid_secret
        page.locator('#continuation-code').fill(pyotp.TOTP(secret).now())
        page.get_by_role('button',name='Verify and sign in',exact=True).click()
        expect(page.get_by_role('heading',name='Dashboard',exact=True)).to_be_visible(timeout=20000)
        page.context.clear_cookies()
        with requests.Session() as client:
            challenge=client.post(BASE_URL+'/api/auth/login',data={'username':username,'password':password},timeout=10)
        assert challenge.status_code==200 and challenge.json().get('mfa_required') is True
        current['response']=challenge.json()
        _goto(page,'/login#oidc_code=synthetic-owned-challenge')
        expect(page.get_by_role('heading',name='Two-factor authentication',exact=True)).to_be_visible()
        assert page.request.get(BASE_URL+'/api/auth/me').status==401
        assert current['exchanges']==2
        page.locator('#continuation-code').fill(pyotp.TOTP(secret).now())
        page.get_by_role('button',name='Verify and sign in',exact=True).click()
        expect(page.get_by_role('heading',name='Dashboard',exact=True)).to_be_visible(timeout=20000)
        actual_user=page.request.get(BASE_URL+'/api/auth/me').json().get('username')==username
        assert actual_user
        assert page.evaluate("localStorage.getItem('access_token')===null && sessionStorage.getItem('mfa_token')===null")
    finally:
        assert admin.put(BASE_URL+'/api/config/require-mfa',json={'require_mfa':before},timeout=10).status_code==200
        assert admin.delete(BASE_URL+f'/api/admin/users/{target["id"]}/mfa',timeout=10).status_code==200


@pytest.mark.parametrize('mobile', [False, True])
def test_compiled_retention_known_policy_validation_and_persistence(role_page_factory, admin, mobile):
    """Real owned configuration API; no cleanup execution or production changes."""
    original = admin.get(BASE_URL + '/api/config/retention', timeout=10).json()
    page = role_page_factory('admin', mobile=mobile)
    try:
        _goto(page, '/settings')
        page.get_by_role('button', name='System', exact=True).click()
        field = page.get_by_label('Completed jobs', exact=True)
        expect(field).to_have_value(str(original['completed_jobs_days']))
        field.fill('')
        page.get_by_role('button', name='Save Policy', exact=True).click()
        expect(page.get_by_role('alert').filter(has_text='nonnegative whole')).to_be_visible()
        assert admin.get(BASE_URL + '/api/config/retention', timeout=10).json() == original
        field.fill('0')
        expected = {**original, 'completed_jobs_days': 0}
        page.get_by_role('button', name='Save Policy', exact=True).click()
        expect(page.get_by_role('status').filter(has_text='Retention policy saved')).to_be_visible()
        assert admin.get(BASE_URL + '/api/config/retention', timeout=10).json() == expected
        page.reload(wait_until='domcontentloaded')
        page.get_by_role('button', name='System', exact=True).click()
        expect(page.get_by_label('Completed jobs', exact=True)).to_have_value('0')
        page.get_by_label('Completed jobs', exact=True).fill('45')
        expect(page.get_by_role('button', name='Run Cleanup Now', exact=True)).to_be_disabled()
    finally:
        assert admin.put(BASE_URL + '/api/config/retention', json=original, timeout=10).status_code == 200
        assert admin.get(BASE_URL + '/api/config/retention', timeout=10).json() == original


@pytest.mark.parametrize('mobile', [False, True])
def test_compiled_allowlist_ipv6_host_persistence_owned_api(role_page_factory, admin, mobile):
    """Community Access tab (free in 2.0); real CIDR persistence and lock-out guard."""
    original = admin.get(BASE_URL + '/api/config/ip-allowlist', timeout=10).json()
    page = role_page_factory('admin', mobile=mobile)
    try:
        _goto(page, '/settings')
        page.get_by_role('button', name='Access', exact=True).click()
        page.get_by_role('button', name='Quotas & Restrictions', exact=True).click()
        page.get_by_label('Enable IP allowlisting', exact=True).check()
        page.get_by_role('textbox', name='IP address or CIDR', exact=True).fill('2001:db8:abcd::1')
        page.get_by_role('button', name='Add', exact=True).click()
        expect(page.get_by_text('2001:db8:abcd::1/128', exact=True)).to_be_visible()
        # 2.0 lock-out guard: a list that does not cover the caller's address (as the server
        # sees it) is refused with that address and nothing is persisted.
        page.get_by_role('button', name='Save', exact=True).click()
        refusal = page.get_by_role('alert').filter(has_text='saving it would lock you out')
        expect(refusal).to_be_visible()
        assert admin.get(BASE_URL + '/api/config/ip-allowlist', timeout=10).json() == original
        import re
        caller = re.search(r'This server sees your address as (\S+), which', refusal.text_content()).group(1)
        page.get_by_role('textbox', name='IP address or CIDR', exact=True).fill(caller)
        page.get_by_role('button', name='Add', exact=True).click()
        page.get_by_role('button', name='Save', exact=True).click()
        expect(page.get_by_role('status').filter(has_text='IP allowlist saved')).to_be_visible()
        saved = admin.get(BASE_URL + '/api/config/ip-allowlist', timeout=10).json()
        assert saved['enabled'] is True and '2001:db8:abcd::1/128' in saved['cidrs']
        assert any(cidr.split('/')[0] == caller for cidr in saved['cidrs'])
    finally:
        assert admin.put(BASE_URL + '/api/config/ip-allowlist', json=original, timeout=10).status_code == 200
        assert admin.get(BASE_URL + '/api/config/ip-allowlist', timeout=10).json() == original
