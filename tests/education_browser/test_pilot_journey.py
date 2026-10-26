"""Actual compiled Education UI and HTTP/JWT routes against a disposable app."""
import os
import secrets
from pathlib import Path
import socket
import subprocess
import sys
import time

import pytest
import requests
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]


def _hdiutil(args, cleanup=None, attempts=3):
    """hdiutil intermittently fails on a busy host; retry briefly, then fail with its error."""
    for attempt in range(attempts):
        result = subprocess.run(['hdiutil', *args], capture_output=True, text=True)
        if result.returncode == 0:
            return
        if cleanup is not None:
            cleanup.unlink(missing_ok=True)
        if attempt + 1 < attempts:
            time.sleep(2 * (attempt + 1))
    raise subprocess.CalledProcessError(result.returncode, result.args, result.stdout, result.stderr)


@pytest.fixture
def upload_volume(tmp_path):
    root = tmp_path / 'upload-volume'
    root.mkdir()
    if sys.platform != 'darwin':
        yield root
        return
    # Use a real bounded filesystem, so host-wide fullness cannot silently
    # turn every browser journey into the storage-reserve rejection path.
    image = tmp_path / 'browser-uploads.sparseimage'
    _hdiutil(['create', '-size', '4g', '-type', 'SPARSE', '-fs', 'APFS',
              '-volname', 'ODIN-Browser-Test', str(image)], cleanup=image)
    _hdiutil(['attach', str(image), '-mountpoint', str(root), '-nobrowse', '-noverify'])
    try:
        yield root
    finally:
        subprocess.run(['hdiutil', 'detach', str(root)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture
def pilot(tmp_path, request, upload_volume):
    assert (ROOT / 'frontend/dist/index.html').exists(), 'Build current frontend before the Education browser gate'
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    url = f'http://127.0.0.1:{port}'
    env = {k:v for k,v in os.environ.items() if not k.startswith(('ODIN_', 'DATABASE_', 'LICENSE_')) and k not in ('ENCRYPTION_KEY','API_KEY','JWT_SECRET_KEY')}
    password = secrets.token_urlsafe(24)
    env['ODIN_EDU_TEST_PASSWORD'] = password
    env['ODIN_EDU_TEST_MIN_FREE_GIB'] = str(getattr(request, 'param', 1))
    env['ODIN_EDU_TEST_UPLOAD_ROOT'] = str(upload_volume)
    with (tmp_path / 'app.log').open('w') as log:
        process = subprocess.Popen([sys.executable, str(Path(__file__).with_name('app_fixture.py')), str(tmp_path / 'app'), str(port)], cwd=tmp_path, env=env, stdout=log, stderr=log)
        try:
            deadline = time.monotonic()+20
            while True:
                assert process.poll() is None, f'Disposable app startup failed; private diagnostic log: {tmp_path / "app.log"}'
                try:
                    if requests.get(url+'/health', timeout=1).status_code == 200:
                        break
                except requests.RequestException:
                    pass
                assert time.monotonic()<deadline, 'Disposable app did not become healthy'
                time.sleep(.1)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    yield url, browser, password
                finally:
                    browser.close()
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)


def login(browser, url, username, password):
    context = browser.new_context()
    page = context.new_page()
    page.goto(url+'/login')
    page.locator('#login-username').fill(username)
    page.locator('#login-password').fill(password)
    page.get_by_role('button',name='Sign In',exact=True).click()
    expect(page.locator('#main-content')).to_be_visible(timeout=15000)
    page.goto(url+'/education')
    heading = 'Education administration' if username == 'superadmin' else 'Education'
    expect(page.get_by_role('heading',name=heading,exact=True)).to_be_visible(timeout=15000)
    return context, page


def test_student_upload_teacher_approval_operator_schedule_and_isolation(pilot):
    from tests.test_contracts.test_education_submission_upload import _sliced_3mf
    url, browser, password = pilot
    student_context, student = login(browser,url,'student',password)
    student.get_by_role('button',name='Submit print',exact=True).first.click()
    student.locator('#education-file').set_input_files({'name':'browser-pilot.3mf','mimeType':'application/octet-stream','buffer':_sliced_3mf()})
    with student.expect_response(lambda r: r.url.endswith('/api/education/submissions') and r.request.method=='POST') as uploaded:
        student.get_by_role('button',name='Submit for review',exact=True).click()
    response = uploaded.value
    assert response.status==201, response.text()
    submission = response.json()
    listing = student.request.get(url+'/api/education/submissions')
    assert listing.status==200
    item_name = next(item['item_name'] for item in listing.json()['items'] if item['id']==submission['id'])
    expect(student.get_by_role('heading',name=item_name,exact=True)).to_be_visible()
    teacher_context, teacher = login(browser,url,'manager',password)
    teacher.get_by_role('button',name='Review queue',exact=True).click()
    # Select the smallest rendered card containing this heading and its review action.
    review = teacher.get_by_role('heading',name=item_name,exact=True).locator('xpath=ancestor::div[contains(@class,"rounded")][1]')
    review.get_by_role('button',name='Review',exact=True).click()
    teacher.get_by_label('Authorized printer').select_option('9')
    # 2.0: a Bambu approval names its filament supply; the printer default is the pre-AMS workflow.
    teacher.get_by_label('Filament supply').select_option('default')
    expect(teacher.get_by_role('button',name='Approve and queue',exact=True)).to_be_enabled()
    with teacher.expect_response(lambda r: f'/submissions/{submission["id"]}/approve' in r.url) as approved:
        teacher.get_by_role('button',name='Approve and queue',exact=True).click()
    assert approved.value.status==200, approved.value.text()
    operator_context, operator = login(browser,url,'admin',password)
    scheduled = operator.request.post(url+'/api/scheduler/run')
    assert scheduled.status==200, scheduled.text()
    assert scheduled.json()['scheduled']==1
    student.get_by_role('button',name='Refresh',exact=True).click()
    expect(student.get_by_text('scheduled',exact=True)).to_be_visible()
    denied = student.request.post(url+'/api/scheduler/run')
    assert denied.status==403
    other_context, other = login(browser,url,'student-two',password)
    expect(other.get_by_role('heading',name=item_name,exact=True)).to_have_count(0)
    listing = other.request.get(url+'/api/education/submissions')
    assert listing.status==200
    assert submission['id'] not in [item['id'] for item in listing.json()['items']]
    for context in (student_context,teacher_context,operator_context,other_context):
        context.close()


def test_invalid_upload_error_and_teacher_return_visible_to_student(pilot):
    from tests.test_contracts.test_education_submission_upload import _sliced_3mf
    url, browser, password = pilot
    student_context, student = login(browser,url,'student',password)
    student.get_by_role('button',name='Submit print',exact=True).first.click()
    student.locator('#education-file').set_input_files({'name':'invalid.3mf','mimeType':'application/octet-stream','buffer':b'not-a-sliced-archive'})
    with student.expect_response(lambda r: r.url.endswith('/api/education/submissions') and r.request.method=='POST') as invalid:
        student.get_by_role('button',name='Submit for review',exact=True).click()
    assert invalid.value.status in (400,422), invalid.value.text()
    error_text = invalid.value.json().get('detail')
    assert isinstance(error_text,str) and error_text
    expect(student.get_by_text(error_text,exact=True)).to_be_visible()
    listing=student.request.get(url+'/api/education/submissions')
    assert listing.status==200 and listing.json()['items']==[]
    student.locator('#education-file').set_input_files({'name':'return-pilot.3mf','mimeType':'application/octet-stream','buffer':_sliced_3mf()})
    with student.expect_response(lambda r: r.url.endswith('/api/education/submissions') and r.request.method=='POST') as uploaded:
        student.get_by_role('button',name='Submit for review',exact=True).click()
    assert uploaded.value.status==201, uploaded.value.text()
    submission=uploaded.value.json()
    teacher_context, teacher=login(browser,url,'manager',password)
    teacher.get_by_role('button',name='Review queue',exact=True).click()
    teacher.get_by_role('button',name='Review',exact=True).click()
    teacher.get_by_label('Reason',exact=True).fill('Please use the assigned material before resubmitting.')
    with teacher.expect_response(lambda r: f'/submissions/{submission["id"]}/reject' in r.url) as rejected:
        teacher.get_by_role('button',name='Reject submission',exact=True).click()
    assert rejected.value.status==200, rejected.value.text()
    student.get_by_role('button',name='Refresh',exact=True).click()
    expect(student.get_by_text('rejected',exact=True)).to_be_visible()
    expect(student.get_by_text('Please use the assigned material before resubmitting.',exact=True)).to_be_visible()
    student_context.close()
    teacher_context.close()


@pytest.mark.parametrize('pilot', [1000000], indirect=True)
def test_storage_reserve_denial_is_visible_and_creates_no_submission(pilot):
    from tests.test_contracts.test_education_submission_upload import _sliced_3mf
    url, browser, password = pilot
    context, student = login(browser, url, 'student', password)
    try:
        student.get_by_role('button', name='Submit print', exact=True).first.click()
        student.locator('#education-file').set_input_files({
            'name': 'storage-pilot.3mf', 'mimeType': 'application/octet-stream',
            'buffer': _sliced_3mf(),
        })
        with student.expect_response(lambda r: r.url.endswith('/api/education/submissions') and r.request.method == 'POST') as denied:
            student.get_by_role('button', name='Submit for review', exact=True).click()
        assert denied.value.status == 507
        detail = denied.value.json()['detail']
        assert 'headroom' in detail.lower()
        expect(student.get_by_text(detail, exact=True)).to_be_visible()
        listing = student.request.get(url + '/api/education/submissions')
        assert listing.status == 200 and listing.json()['items'] == []
    finally:
        context.close()


def test_guided_troubleshooting_server_report_and_safe_printer_failure(pilot):
    url,browser,password=pilot
    context,page=login(browser,url,'superadmin',password)
    try:
        page.goto(url+'/settings')
        page.get_by_role('button',name='System',exact=True).click()
        expect(page.get_by_role('heading',name='Troubleshooting',exact=True)).to_be_visible()
        page.get_by_role('button',name='Check server',exact=True).click()
        expect(page.get_by_text('Database query succeeded.',exact=True)).to_be_visible()
        # Saved synthetic printer has a deliberately blocked loopback address.
        assert page.request.get(url+'/api/printers').status==200
        page.locator('#diagnostic-printer').select_option('99')
        page.get_by_role('button',name='Check printer',exact=True).click()
        expect(page.get_by_text('Saved printer address could not be safely resolved.',exact=True)).to_be_visible()
        page.locator('#diagnostic-problem').fill('Transfer failed on our test printer')
        page.get_by_role('button',name='Preview support report',exact=True).click()
        preview=page.get_by_label('Support report preview')
        expect(preview).to_contain_text('Transfer failed on our test printer')
        expect(preview).to_contain_text('physical_print_verified')
        with page.expect_download() as downloaded:
            page.get_by_role('button',name='Download report',exact=True).click()
        import json
        report=json.loads(Path(downloaded.value.path()).read_text())
        assert report['physical_print_verified'] is False
        assert 'recent_logs' not in report
        assert report['printer_checks']['checks'][0]['status']=='fail'
        expect(page.get_by_role('link',name='Open email draft')).to_have_attribute('href',__import__('re').compile('^mailto:support@runsodin.com'))
        page.screenshot(path=str(ROOT/'artifacts/edu-workflow-check/troubleshooting-browser.png'),full_page=True)
    finally: context.close()


def test_submit_dialog_keyboard_and_critical_accessibility(pilot):
    url, browser, password = pilot
    context, page = login(browser, url, 'student', password)
    page.get_by_role('button', name='Submit print', exact=True).first.click()
    expect(page.locator('#education-file')).to_be_visible()
    page.evaluate((ROOT / 'frontend/node_modules/axe-core/axe.min.js').read_text())
    results = page.evaluate("""async () => await axe.run(document.querySelector('[role=dialog]') || document.querySelector('#main-content'), {runOnly:{type:'tag',values:['wcag2a','wcag2aa']}})""")
    blocking = [{ 'id':v['id'], 'impact':v['impact'], 'elements':[n['target'] for n in v['nodes']] }
        for v in results['violations'] if v['impact'] in ('critical','serious')]
    assert blocking == [], blocking
    page.keyboard.press('Escape')
    # The accessible cancellation action must always remain keyboard reachable.
    if page.locator('#education-file').is_visible():
        page.get_by_role('button', name='Cancel', exact=True).focus()
        page.keyboard.press('Enter')
    expect(page.locator('#education-file')).not_to_be_visible()
    context.close()


def test_bounded_malformed_archive_http_corpus_leaves_no_submissions(pilot):
    import io, zipfile, struct, uuid
    from tests.test_contracts.test_education_submission_upload import _sliced_3mf
    url, browser, password = pilot
    context, page = login(browser,url,'student',password)
    valid = _sliced_3mf()
    duplicate = io.BytesIO()
    with zipfile.ZipFile(duplicate,'w') as target, zipfile.ZipFile(io.BytesIO(valid)) as source:
        for item in source.infolist(): target.writestr(item.filename,source.read(item))
        # The duplicate member is the malformation under test; assert zipfile's warning here
        # instead of letting it reach the run log (where it names a print file).
        with pytest.warns(UserWarning, match='Duplicate name'):
            target.writestr('Metadata/plate_1.gcode','G28')
    expanded = bytearray(valid)
    start = expanded.find(b'PK\x01\x02')
    struct.pack_into('<I',expanded,start+24,600*1024**2)
    corpus = [b'',b'not a zip',valid[:30],valid[:-22],duplicate.getvalue(),bytes(expanded)]
    control=context.request.post(url+'/api/education/submissions', multipart={
        'cost_center_id':'7','submission_token':str(uuid.uuid4()),
        'file':{'name':'control.3mf','mimeType':'application/octet-stream','buffer':valid}})
    assert control.status==201, control.text()
    control_id=control.json()['id']
    for payload in corpus:
        started=time.monotonic()
        response=context.request.post(url+'/api/education/submissions', multipart={
            'cost_center_id':'7','submission_token':str(uuid.uuid4()),
            'file':{'name':'malformed.3mf','mimeType':'application/octet-stream','buffer':payload}})
        assert response.status in (400,422), response.text()
        assert time.monotonic()-started < 5
        listing=context.request.get(url+'/api/education/submissions')
        assert listing.status==200
        assert [item['id'] for item in listing.json()['items']]==[control_id]
    context.close()
