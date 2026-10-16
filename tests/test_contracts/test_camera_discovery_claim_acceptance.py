"""Owned camera discovery/registration assertions, no hardware or playable-video claim."""
from contextlib import contextmanager
from types import SimpleNamespace
import pytest
from sqlalchemy import text
from cryptography.fernet import Fernet
from tests.test_contracts.test_printer_offline_behavior import offline_store, review_db, v2_offline_monitor


@pytest.fixture
def captured_config(offline_store, monkeypatch):
    from core import db_utils
    from core.db_utils import _ConnectionAdapter
    from modules.printers import route_utils
    monkeypatch.setenv('ENCRYPTION_KEY', Fernet.generate_key().decode())
    configs=[]
    @contextmanager
    def database():
        connection=offline_store.get_bind().raw_connection()
        try:yield _ConnectionAdapter(connection,False)
        finally:connection.close()
    monkeypatch.setattr(db_utils,'get_db',database)
    from modules.notifications import printer_health
    monkeypatch.setattr(printer_health,'get_db',database)
    monkeypatch.setattr(route_utils,'_go2rtc_restart_with_lock',lambda value:configs.append(value))
    return configs


@pytest.mark.parametrize('model',['X1C','H2D'])
def test_bambu_actual_mqtt_camera_presence_generates_config_from_encrypted_credentials(v2_offline_monitor,offline_store,captured_config,model):
    from core import crypto
    from modules.printers.telemetry.events import BambuReportEvent
    from modules.printers.telemetry.bambu.raw import BambuPrintSection
    monitor,emitters,_=v2_offline_monitor
    monitor._camera_discovered=False
    with offline_store.get_bind().begin() as conn:
        conn.execute(text('UPDATE printers SET api_type=\'bambu\',model=:model,api_host=\'127.0.0.1\',api_key=:key,camera_enabled=TRUE,camera_url=NULL WHERE id=199'),
                     {'model':model,'key':crypto.encrypt('owned-serial|owned-fixture-code')})
    report=BambuPrintSection.model_validate({'command':'push_status','ipcam':{'rtsp_url':'rtsp://192.0.2.17/owned'}})
    emitters[0](BambuReportEvent('monitor-199',2000,report))
    assert captured_config[-1]['streams']['printer_199']=='rtsps://bblp:owned-fixture-code@127.0.0.1:322/streaming/live/1'
    assert offline_store.execute(text('SELECT camera_url FROM printers WHERE id=199')).scalar_one() is None
    emitters[0](BambuReportEvent('monitor-199',2001,report))
    assert len(captured_config)==1


def test_moonraker_camera_list_resolves_relative_stream_and_snapshot(monkeypatch):
    from modules.printers.adapters.moonraker import MoonrakerPrinter
    printer=MoonrakerPrinter('127.0.0.1');calls=[]
    def get(path):
        calls.append(path)
        return {'result':{'webcams':[{'stream_url':'/webcam/?action=stream','snapshot_url':'/webcam/?action=snapshot'}]}}
    monkeypatch.setattr(printer,'_get',get)
    printer._discover_webcam()
    assert calls==['/server/webcams/list']
    assert printer.get_webcam_urls()=={'stream_url':printer.base_url+'/webcam/?action=stream','snapshot_url':printer.base_url+'/webcam/?action=snapshot'}


@pytest.mark.parametrize('modern,status',[(True,200),(False,200),(False,404)])
def test_prusalink_modern_camera_or_bounded_authenticated_snapshot_probe(monkeypatch,modern,status):
    import requests
    from modules.printers.adapters.prusalink import PrusaLinkPrinter
    # LAN address: the device destination policy blocks loopback printers.
    printer=PrusaLinkPrinter('192.0.2.10',api_key='owned-fixture-key');calls=[]
    monkeypatch.setattr(printer,'_get',lambda path:[{'id':'owned-camera'}] if modern else [])
    def head(url,**kwargs):calls.append((url,kwargs));return SimpleNamespace(status_code=status)
    monkeypatch.setattr(requests,'head',head)
    result=printer.get_webcam_url()
    if modern:
        assert result==printer.base_url+'/api/v1/cameras/snap' and not calls
    else:
        assert calls==[(printer.base_url+'/webcam/?action=snapshot',{'headers':{'X-Api-Key':'owned-fixture-key'},'timeout':5})]
        assert result==(calls[0][0] if status==200 else None)


@pytest.mark.parametrize('statuses',[(200,),(404,200),(404,404)])
def test_elegoo_port8080_candidates_are_bounded_and_only_success_is_selected(monkeypatch,statuses):
    import requests
    from modules.printers.adapters.elegoo import ElegooPrinter
    printer=ElegooPrinter('127.0.0.1');calls=[]
    def head(url,**kwargs):calls.append((url,kwargs));return SimpleNamespace(status_code=statuses[len(calls)-1])
    monkeypatch.setattr(requests,'head',head)
    result=printer.get_webcam_url()
    expected=['http://127.0.0.1:8080/?action=stream','http://127.0.0.1:8080/webcam/?action=stream'][:len(statuses)]
    assert calls==[(url,{'timeout':3}) for url in expected]
    assert result==(expected[-1] if statuses[-1]==200 else None)


@pytest.mark.parametrize('protocol',['moonraker','prusalink','elegoo'])
def test_claim_nonbambu_discovery_reaches_current_go2rtc_stream(offline_store,captured_config,monkeypatch,protocol):
    from modules.printers.monitors import moonraker_monitor,prusalink_monitor,elegoo_monitor
    url='http://192.168.50.20:8080/?action=stream'
    with offline_store.get_bind().begin() as conn:
        conn.execute(text('UPDATE printers SET api_type=:protocol,api_host=\'192.168.50.20\',camera_enabled=TRUE,camera_url=NULL WHERE id=199'),{'protocol':protocol})
    if protocol=='moonraker':
        monitor=moonraker_monitor.MoonrakerMonitor(199,'Owned camera','192.168.50.20')
        monkeypatch.setattr(monitor.printer,'get_webcam_urls',lambda:{'stream_url':url})
    elif protocol=='prusalink':
        monitor=prusalink_monitor.PrusaLinkMonitorThread(199,'Owned camera','192.168.50.20')
        monkeypatch.setattr(monitor.client,'get_webcam_url',lambda:url)
    else:
        monitor=elegoo_monitor.ElegooMonitorThread(199,'Owned camera','192.168.50.20')
        monkeypatch.setattr(monitor.client,'get_webcam_url',lambda:url)
    monitor._discover_and_save_camera()
    assert captured_config[-1]['streams'].get('printer_199')=='ffmpeg:'+url+'#video=h264'


def test_explicit_mjpeg_registration_uses_h264_prefix(offline_store,captured_config):
    from core import crypto
    from modules.printers.route_utils import sync_go2rtc_config_raw
    url='http://127.0.0.1:8080/?action=stream'
    with offline_store.get_bind().begin() as conn:
        conn.execute(text('UPDATE printers SET api_type=\'elegoo\',camera_enabled=TRUE,camera_url=:url WHERE id=199'),{'url':crypto.encrypt(url)})
    sync_go2rtc_config_raw()
    assert captured_config[-1]['streams']['printer_199']=='ffmpeg:'+url+'#video=h264'
