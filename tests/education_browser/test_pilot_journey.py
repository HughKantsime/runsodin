"""Actual compiled Education UI and HTTP/JWT routes against a disposable app."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import pytest
import requests
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def pilot(tmp_path):
    assert (ROOT / 'frontend/dist/index.html').exists(), 'Build current frontend before the Education browser gate'
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    url = f'http://127.0.0.1:{port}'
    env = {k:v for k,v in os.environ.items() if not k.startswith(('ODIN_', 'DATABASE_', 'LICENSE_')) and k not in ('ENCRYPTION_KEY','API_KEY','JWT_SECRET_KEY')}
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
                    yield url, browser
                finally:
                    browser.close()
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)


def login(browser, url, username):
    context = browser.new_context()
    page = context.new_page()
    page.goto(url+'/login')
    page.locator('#login-username').fill(username)
    page.locator('#login-password').fill('Synthetic-Browser-Only-2026!')
    page.get_by_role('button',name='Sign In',exact=True).click()
    expect(page.locator('#main-content')).to_be_visible(timeout=15000)
    page.goto(url+'/education')
    expect(page.get_by_role('heading',name='Education',exact=True)).to_be_visible(timeout=15000)
    return context, page


def test_student_upload_teacher_approval_operator_schedule_and_isolation(pilot):
    from tests.test_contracts.test_education_submission_upload import _sliced_3mf
    url, browser = pilot
    student_context, student = login(browser,url,'student')
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
    teacher_context, teacher = login(browser,url,'manager')
    teacher.get_by_role('button',name='Review queue',exact=True).click()
    # Select the smallest rendered card containing this heading and its review action.
    review = teacher.get_by_role('heading',name=item_name,exact=True).locator('xpath=ancestor::div[contains(@class,"rounded")][1]')
    review.get_by_role('button',name='Review',exact=True).click()
    teacher.get_by_label('Authorized printer').select_option('9')
    expect(teacher.get_by_role('button',name='Approve and queue',exact=True)).to_be_enabled()
    with teacher.expect_response(lambda r: f'/submissions/{submission["id"]}/approve' in r.url) as approved:
        teacher.get_by_role('button',name='Approve and queue',exact=True).click()
    assert approved.value.status==200, approved.value.text()
    operator_context, operator = login(browser,url,'admin')
    scheduled = operator.request.post(url+'/api/scheduler/run')
    assert scheduled.status==200, scheduled.text()
    assert scheduled.json()['scheduled']==1
    student.get_by_role('button',name='Refresh',exact=True).click()
    expect(student.get_by_text('scheduled',exact=True)).to_be_visible()
    denied = student.request.post(url+'/api/scheduler/run')
    assert denied.status==403
    other_context, other = login(browser,url,'student-two')
    expect(other.get_by_role('heading',name=item_name,exact=True)).to_have_count(0)
    listing = other.request.get(url+'/api/education/submissions')
    assert listing.status==200
    assert submission['id'] not in [item['id'] for item in listing.json()['items']]
    for context in (student_context,teacher_context,operator_context,other_context):
        context.close()


def test_invalid_upload_error_and_teacher_return_visible_to_student(pilot):
    from tests.test_contracts.test_education_submission_upload import _sliced_3mf
    url, browser = pilot
    student_context, student = login(browser,url,'student')
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
    teacher_context, teacher=login(browser,url,'manager')
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
