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
