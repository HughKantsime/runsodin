"""Transport ordering acceptance; isolated adapters, no device/network claim."""
from types import SimpleNamespace
import pytest
from modules.printers import dispatch

@pytest.mark.parametrize('protocol', ['legacy', 'v2'])
@pytest.mark.parametrize('verified', [True, False, None, 'truthy', 'error', 'no_guard', 'upload_failed'])
def test_explicit_mapping_rechecks_after_upload_before_start(monkeypatch, protocol, verified):
    events = []
    sent_options = []
    def uploaded(*args, **kwargs):
        events.append('upload')
        return verified != 'upload_failed'
    def checked():
        events.append('fresh_selected_tray_read')
        if verified == 'error':
            raise RuntimeError('synthetic read failure')
        return verified
    def started(*args, **kwargs):
        events.append('start')
        assert kwargs.get('ams_mapping') == [0]
        sent_options.append(kwargs)
        return True
    monkeypatch.setattr(dispatch, '_ws', lambda *args: None)
    monkeypatch.setattr(dispatch.time, 'sleep', lambda *args: None)
    if protocol == 'legacy':
        from modules.printers.adapters import bambu
        fake = SimpleNamespace(upload_file=uploaded, connect=lambda: True, start_print=started,
                               disconnect=lambda: events.append('cleanup'))
        monkeypatch.setattr(bambu, 'BambuPrinter', lambda **kwargs: fake)
        operation = dispatch._dispatch_bambu_legacy
    else:
        from modules.printers.telemetry.bambu import ftp_upload, session, broker_policy
        monkeypatch.setattr(ftp_upload, 'upload_file', uploaded)
        monkeypatch.setattr(session, 'run_command', started)
        monkeypatch.setattr(broker_policy, 'resolve_bambu_broker_config', lambda *a, **k:
                            SimpleNamespace(host='192.0.2.1', port=8883, use_tls=True))
        operation = dispatch._dispatch_bambu_v2
    result = operation(53, 'synthetic.3mf', 'synthetic.3mf',
                       {'ip': '192.0.2.1', 'serial': 'SYNTHETIC', 'access_code': ''},
                       ams_mapping=[0], before_start=None if verified == 'no_guard' else checked)
    assert result[0] is (verified is True)
    if verified == 'no_guard':
        assert events == []
    elif verified == 'upload_failed':
        assert events[0] == 'upload'
        assert 'fresh_selected_tray_read' not in events
    else:
        assert events[:2] == ['upload', 'fresh_selected_tray_read']
    assert ('start' in events) is (verified is True)
    assert all('before_start' not in options for options in sent_options)
    if protocol == 'legacy' and verified != 'no_guard':
        assert events[-1] == 'cleanup'
