from __future__ import annotations

from pathlib import Path
import pytest

from playwright.sync_api import Page, expect

from .conftest import BASE_URL, PERSONAS


FIXTURE = Path(__file__).parents[1] / "fixtures" / "release_gate" / "candidate-calibration-cube.3mf"


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
    page = role_page_factory("admin")
    for path in ("/products", "/orders"):
        _goto(page, path)
        expect(page.get_by_role("heading", name="Pro Feature", exact=True)).to_be_visible(timeout=20_000)

    _goto(page, "/settings")
    expect(page.get_by_role("heading", name="Settings", exact=True)).to_be_visible(timeout=20_000)
    expect(page.get_by_role("heading", name="Community Edition", exact=True)).to_be_visible()
    page.get_by_role("button", name="Access", exact=True).click()
    expect(page.get_by_text("Users & Groups", exact=True)).to_have_count(0)
    expect(page.get_by_role("heading", name="Community Edition", exact=True)).to_be_visible()


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
    assert FIXTURE.is_file()
    page = role_page_factory("admin")
    _goto(page, "/upload")
    page.locator("#file-upload").set_input_files(str(FIXTURE))
    expect(page.get_by_text("Added to Model Library", exact=True)).to_be_visible(timeout=30_000)
    expect(page.get_by_role("heading", name="ODIN Candidate Upload Cube", exact=True)).to_be_visible()
    page.get_by_role("button", name="Quick Print", exact=True).click()
    expect(page.get_by_role("button", name="Queued!", exact=True)).to_be_visible(timeout=20_000)
    _goto(page, "/jobs")
    row = page.get_by_role("row").filter(has_text="ODIN Candidate Upload Cube")
    expect(row).to_be_visible(timeout=20_000)
    expect(row).to_contain_text("pending")


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


@pytest.mark.parametrize('mobile', [False, True])
def test_compiled_order_confirmation_persists_bounded_corrections(role_page_factory, admin, mobile):
    """Compiled UI and real HTTP/database; only browser license facts substituted.

    This Community candidate does not certify Pro license validation. The
    explicit feature-facts substitution exposes the orders UI, while every
    product/order/confirmation request executes against the isolated server.
    """
    product = admin.post(f'{BASE_URL}/api/products', json={'name':f'Synthetic manual product {mobile}'}, timeout=10)
    assert product.status_code == 200
    label = f'Synthetic compiled production {mobile}'
    order = admin.post(f'{BASE_URL}/api/orders',json={'order_number':label,
        'items':[{'product_id':product.json()['id'],'quantity':3}]},timeout=10)
    assert order.status_code == 200
    order_id = order.json()['id']
    page = role_page_factory('admin',mobile=mobile)
    def browser_feature_facts(route):
        response = route.fetch()
        data = response.json()
        data['tier'] = 'pro'
        data['features'] = list(set(data.get('features',[])) | {'orders','products'})
        route.fulfill(response=response,json=data)
    page.route('**/api/license',browser_feature_facts)
    _goto(page,'/orders')
    page.get_by_role('button',name=label,exact=True).click()
    quantity = page.get_by_label('Produced quantity',exact=True)
    expect(quantity).to_be_visible()
    quantity.fill('4')
    expect(page.get_by_role('button',name='Confirm production',exact=True)).to_be_disabled()
    quantity.fill('3')
    with page.expect_response(lambda response: response.url.endswith('/confirm-production')) as receipt:
        page.get_by_role('button',name='Confirm production',exact=True).click()
    assert receipt.value.status == 200
    assert receipt.value.json()['status'] == 'fulfilled'
    expect(page.get_by_text('Production quantity confirmed',exact=True)).to_be_visible()
    quantity = page.get_by_label('Produced quantity',exact=True)
    expect(quantity).to_have_value('3')
    quantity.fill('0')
    with page.expect_response(lambda response: response.url.endswith('/confirm-production')) as receipt:
        page.get_by_role('button',name='Confirm production',exact=True).click()
    assert receipt.value.status == 200
    assert receipt.value.json()['items'][0]['fulfilled_quantity'] == 0
    expect(page.get_by_label('Produced quantity',exact=True)).to_have_value('0')
    persisted = admin.get(f'{BASE_URL}/api/orders/{order_id}',timeout=10).json()
    assert persisted['status']=='in_progress' and persisted['items'][0]['production_mode']=='historical'
    assert persisted['items'][0]['fulfilled_quantity']==0
    dimensions = page.evaluate('() => ({scroll:document.documentElement.scrollWidth,client:document.documentElement.clientWidth})')
    assert dimensions['scroll']<=dimensions['client']+1


@pytest.mark.parametrize('mobile',[False,True])
def test_compiled_order_edit_keeps_zero_and_downloads_invoice(role_page_factory,admin,mobile,tmp_path):
    from pypdf import PdfReader
    label=f'Synthetic compiled zero finances {mobile}'
    product=admin.post(f'{BASE_URL}/api/products',json={'name':label,'price':0},timeout=10)
    assert product.status_code==200
    created=admin.post(f'{BASE_URL}/api/orders',json={'order_number':label,'revenue':0,
        'platform_fees':0,'payment_fees':0,'shipping_charged':0,
        'items':[{'product_id':product.json()['id'],'quantity':2,'unit_price':0}]},timeout=10)
    assert created.status_code==200
    identifier=created.json()['id']
    page=role_page_factory('admin',mobile=mobile)
    def feature_facts(route):
        response=route.fetch();data=response.json();data['tier']='pro'
        data['features']=list(set(data.get('features',[]))|{'orders','products'})
        route.fulfill(response=response,json=data)
    page.route('**/api/license',feature_facts)
    _goto(page,'/orders')
    page.get_by_placeholder('Search by order number or customer...').fill(label)
    page.locator('button[title="Edit Order"]:visible').first.click()
    expect(page.get_by_role('button',name='Save Changes',exact=True)).to_be_visible()
    page.get_by_role('button',name='Save Changes',exact=True).click()
    expect(page.get_by_text('Order updated',exact=True)).to_be_visible()
    saved=admin.get(f'{BASE_URL}/api/orders/{identifier}',timeout=10).json()
    assert all(saved[key]==0 for key in ('revenue','platform_fees','payment_fees','shipping_charged'))
    assert saved['items'][0]['unit_price']==0 and saved['items'][0]['subtotal']==0
    page.get_by_role('button',name=label,exact=True).click()
    with page.expect_download() as receipt:
        page.get_by_role('button',name='Invoice',exact=True).click()
    download=receipt.value
    # The browser API client chooses its own local filename; server header tested separately.
    assert download.failure() is None
    path=tmp_path/'synthetic-invoice.pdf';download.save_as(path)
    text='\n'.join(p.extract_text() for p in PdfReader(path).pages)
    assert 'Invoice Total:' in text and '$0.00' in text
    assert 'Profit:' not in text and 'Platform Fees' not in text
    assert page.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth+1')


@pytest.mark.parametrize('mobile',[False,True])
def test_compiled_consumable_adjustment_persists_and_shortage_is_visible(role_page_factory,admin,mobile):
    import re
    label=f'Synthetic compiled inventory {mobile}'
    created=admin.post(f'{BASE_URL}/api/consumables',json={'name':label,'current_stock':3,'min_stock':2},timeout=10)
    assert created.status_code==200
    identifier=created.json()['id']
    page=role_page_factory('admin',mobile=mobile)
    def feature_facts(route):
        response=route.fetch();data=response.json();data['tier']='pro'
        data['features']=list(set(data.get('features',[]))|{'products'})
        route.fulfill(response=response,json=data)
    page.route('**/api/license',feature_facts)
    _goto(page,'/consumables')
    page.get_by_placeholder('Search name or SKU...').fill(label)
    page.get_by_title('Adjust Stock').click()
    page.locator('select:has(option[value="deduct"])').select_option('deduct')
    page.get_by_role('spinbutton').fill('2')
    page.get_by_role('button',name='Adjust',exact=True).click()
    expect(page.get_by_role('button',name='Adjust',exact=True)).not_to_be_visible()
    saved=admin.get(f'{BASE_URL}/api/consumables/{identifier}',timeout=10).json()
    assert saved['current_stock']==1 and saved['is_low_stock']
    assert [r['quantity_used'] for r in saved['usage_history']]==[2]
    expect(page.locator('tr').filter(has_text=label).get_by_text('1',exact=True)).to_be_visible()
    page.get_by_title('Adjust Stock').click()
    page.locator('select:has(option[value="deduct"])').select_option('deduct')
    page.get_by_role('spinbutton').fill('2')
    page.get_by_role('button',name='Adjust',exact=True).click()
    expect(page.get_by_text(re.compile('Adjust stock failed'))).to_be_visible()
    saved=admin.get(f'{BASE_URL}/api/consumables/{identifier}',timeout=10).json()
    assert saved['current_stock']==1 and len(saved['usage_history'])==1
    assert page.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth+1')


def test_compiled_report_schedule_persistence_and_run_now_failure(role_page_factory,admin):
    import re
    page=role_page_factory('admin')
    _goto(page,'/settings')
    page.get_by_role('button',name='System',exact=True).click()
    expect(page.get_by_role('heading',name='Scheduled Reports',exact=True)).to_be_visible()
    page.get_by_role('button',name='New Schedule',exact=True).click()
    page.locator('#rs-name').fill('Synthetic compiled report')
    page.locator('#rs-type').select_option('job_summary')
    page.locator('#rs-recipients').fill('synthetic@example.invalid')
    page.get_by_role('button',name='Create Schedule',exact=True).click()
    expect(page.get_by_text('Synthetic compiled report',exact=True)).to_be_visible()
    records=admin.get(f'{BASE_URL}/api/report-schedules',timeout=10)
    assert records.status_code==200
    record=next(r for r in records.json() if r['name']=='Synthetic compiled report')
    assert record['report_type']=='job_summary' and record['recipients']==['synthetic@example.invalid']
    page.get_by_role('button',name='Pause schedule',exact=True).click()
    expect(page.get_by_role('button',name='Activate schedule',exact=True)).to_be_visible()
    records=admin.get(f'{BASE_URL}/api/report-schedules',timeout=10).json()
    assert next(r for r in records if r['id']==record['id'])['is_active'] is False
    # This owned candidate has no configured SMTP transport. Failure must be visible, never sent.
    page.get_by_role('button',name='Run "Synthetic compiled report" now',exact=True).click()
    expect(page.get_by_text(re.compile('Failed to send report:'))).to_be_visible()
    expect(page.get_by_text('Report sent',exact=True)).to_have_count(0)
    page.get_by_role('button',name='Delete schedule',exact=True).click()
    expect(page.get_by_text('Synthetic compiled report',exact=True)).to_have_count(0)
    assert all(r['id']!=record['id'] for r in admin.get(f'{BASE_URL}/api/report-schedules',timeout=10).json())


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
def test_compiled_maintenance_template_creation_is_persisted(role_page_factory,admin,mobile):
    page=role_page_factory('admin')
    if mobile:page.set_viewport_size({'width':390,'height':844})
    def feature_facts(route):
        response=route.fetch();data=response.json()
        data['tier']='pro';data['features']=list(set(data.get('features',[]))|{'maintenance'})
        route.fulfill(response=response,json=data)
    page.route('**/api/license',feature_facts)
    _goto(page,'/maintenance')
    page.get_by_role('button',name='Task Templates',exact=True).click()
    page.get_by_role('button',name='Add Task',exact=True).click()
    label='Synthetic maintenance '+('mobile' if mobile else 'desktop')
    page.get_by_placeholder('e.g. Nozzle Change').fill(label)
    page.get_by_placeholder('e.g. 90').fill('30')
    page.get_by_role('button',name='Create Task',exact=True).click()
    expect(page.get_by_text(label,exact=True)).to_be_visible()
    response=admin.get(f'{BASE_URL}/api/maintenance/tasks',timeout=10)
    assert response.status_code==200
    row=next(r for r in response.json() if r['name']==label)
    assert row['interval_days']==30 and row['estimated_cost']==0
    page.reload(wait_until='domcontentloaded')
    page.get_by_role('button',name='Task Templates',exact=True).click()
    expect(page.get_by_text(label,exact=True)).to_be_visible()

@pytest.mark.parametrize('mobile',[False,True])
def test_compiled_product_zero_edit_persists_fields(role_page_factory,admin,mobile):
    """Real product CRUD and compiled UI; browser license presentation only."""
    label=f'Synthetic compiled free product {mobile}'
    created=admin.post(f'{BASE_URL}/api/products',json={'name':label,'sku':f'FREE-{mobile}','price':0,'description':'Synthetic preserved description','components':[]},timeout=10)
    assert created.status_code==200
    identifier=created.json()['id']
    page=role_page_factory('admin',mobile=mobile)
    def feature_facts(route):
        response=route.fetch();data=response.json()
        data['features']=list(set(data.get('features',[]))|{'products'})
        route.fulfill(response=response,json=data)
    page.route('**/api/license',feature_facts)
    _goto(page,'/products')
    if mobile:
        card=page.locator('div.p-4').filter(has=page.get_by_role('button',name=label,exact=True)).last
        expect(card.get_by_text('Price: $0.00',exact=True)).to_be_visible()
        card.locator('button').nth(1).click()
    else:
        row=page.locator('tr').filter(has_text=label)
        expect(row.get_by_text('$0.00',exact=True)).to_be_visible()
        row.get_by_title('Edit',exact=True).click()
    expect(page.get_by_placeholder('15.00')).to_have_value('0')
    with page.expect_response(lambda r:r.url.endswith(f'/api/products/{identifier}') and r.request.method=='PATCH') as completed:
        page.get_by_role('button',name='Save Changes',exact=True).click()
    assert completed.value.status==200
    expect(page.get_by_role('button',name='Save Changes',exact=True)).not_to_be_visible()
    saved=admin.get(f'{BASE_URL}/api/products/{identifier}',timeout=10).json()
    assert saved['price']==0 and saved['sku']==f'FREE-{mobile}' and saved['description']=='Synthetic preserved description'
    assert page.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth+1')
    deleted=admin.delete(f'{BASE_URL}/api/products/{identifier}',timeout=10)
    assert deleted.status_code==204
    assert admin.get(f'{BASE_URL}/api/products/{identifier}',timeout=10).status_code==404

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
def test_compiled_energy_zero_save_and_load_retry(admin,role_page_factory,mobile):
    """Real rate API/DB and compiled UI; license and measured-energy inputs substituted."""
    import os,re
    assert admin.put(f'{BASE_URL}/api/settings/energy-rate',json={'energy_cost_per_kwh':0},timeout=10).status_code==200
    page=role_page_factory('admin',mobile=mobile)
    def feature_facts(route):
        response=route.fetch();data=response.json();data['tier']='pro'
        data['features']=list(set(data.get('features',[]))|{'analytics'})
        route.fulfill(response=response,json=data)
    page.route('**/api/license',feature_facts)
    page.route(re.compile(r'/api/print-jobs\?'),lambda route:route.fulfill(json=[{'id':999,'energy_kwh':2,'completed_at':'2026-10-11T12:00:00Z'}]))
    failure={'active':False}
    def rate_read(route):
        if route.request.method=='GET' and failure['active']:
            route.fulfill(status=503,json={'detail':'Synthetic rate unavailable'})
        else:route.continue_()
    page.route('**/api/settings/energy-rate',rate_read)
    try:
        _goto(page,'/analytics')
        expect(page.get_by_role('heading',name='Energy Consumption',exact=True)).to_be_visible(timeout=20_000)
        widget=page.get_by_role('heading',name='Energy Consumption',exact=True).locator('..').locator('..').locator('..')
        expect(widget.get_by_role('button',name='$0/kWh',exact=True)).to_be_visible()
        expect(widget.get_by_text('$0.00',exact=True)).to_be_visible()
        page.get_by_role('button',name='$0/kWh',exact=True).click()
        page.get_by_role('spinbutton',name='Energy rate per kWh').fill('0.25')
        page.get_by_role('button',name='Save',exact=True).click()
        expect(page.get_by_role('button',name='$0.25/kWh',exact=True)).to_be_visible()
        assert admin.get(f'{BASE_URL}/api/settings/energy-rate',timeout=10).json()['energy_cost_per_kwh']==0.25
        expect(widget.get_by_text('$0.50',exact=True)).to_be_visible()
        failure['active']=True
        page.reload(wait_until='domcontentloaded')
        expect(widget.get_by_role('alert')).to_contain_text('Energy rate unavailable')
        expect(widget.get_by_text('--',exact=True)).to_be_visible()
        expect(widget.get_by_text('$0.50',exact=True)).to_have_count(0)
        failure['active']=False
        widget.get_by_role('button',name='Retry',exact=True).click()
        expect(page.get_by_role('button',name='$0.25/kWh',exact=True)).to_be_visible()
        expect(widget.get_by_text('$0.50',exact=True)).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth+1')
        widget.screenshot(path=str(Path(os.environ['ODIN_CANDIDATE_ARTIFACT_DIR'])/f'energy-widget-{mobile}.png'))
    finally:
        assert admin.put(f'{BASE_URL}/api/settings/energy-rate',json={'energy_cost_per_kwh':0.12},timeout=10).status_code==200

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
    printer={'id':900,'name':'Synthetic detail printer','gcode_state':'RUNNING','nozzle_temp':210,'nozzle_target_temp':220,'bed_temp':60,'bed_target_temp':60,'fan_speed':0,'mc_percent':45,'mc_remaining_time':75,'gcode_file':'Synthetic detail job','layer_num':12,'total_layer_num':100,'filament_slots':[]}
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
