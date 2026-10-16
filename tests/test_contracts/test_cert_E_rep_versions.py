"""Cert-E (rep): advertised version strings for 10.7/10.8/10.9 against CHANGELOG.md.

A version is confirmed only when the matching CHANGELOG section names the
feature. Tests asserting absence document a claim_mismatch (they pass while
the changelog cannot confirm the advertised version).
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHANGELOG = (ROOT / 'CHANGELOG.md').read_text()
SITE_CHANGELOG = ROOT.parent / 'odin-site/src/data/changelog.json'


def section(version):
    match = re.search(r'^## \[' + re.escape(version) + r'\][^\n]*\n(.*?)(?=^## \[)', CHANGELOG, re.S | re.M)
    return match.group(1) if match else None


def test_v1_3_16_adds_usage_reports_to_pro_tier():
    body = section('1.3.16')
    assert body is not None and '`usage_reports` feature added to Pro license tier' in body


def test_v1_3_19_adds_report_runner_daemon():
    body = section('1.3.19')
    assert body is not None and re.search(r'Report runner daemon\*\* \(`report_runner\.py`\)', body)


def test_v1_3_47_adds_run_now_button_and_endpoint():
    body = section('1.3.47')
    assert body is not None and 'Scheduled Reports "Run Now"' in body and 'Run Now' in body and 'report-schedules/{id}/run' in body


@pytest.mark.parametrize('version', ['1.3.0', '1.3.1', '1.3.2', '1.3.3', '1.3.4'])
def test_v1_3_0_cannot_be_confirmed_from_changelog(version):
    """Documents the mismatch: CHANGELOG jumps from 1.0.8 to 1.3.5."""
    assert section(version) is None
    if SITE_CHANGELOG.exists():
        assert f'"version": "{version}"' not in SITE_CHANGELOG.read_text()
