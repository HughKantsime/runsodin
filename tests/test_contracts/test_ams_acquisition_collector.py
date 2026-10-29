import pytest
from modules.printers.telemetry.bambu.ams_acquisition_collector import RawAMSAcquisitionCollector


@pytest.fixture
def owned():
    clock=[100.0]
    c=RawAMSAcquisitionCollector(topic='device/synthetic/report',configuration_key='a'*64,
        owner_org_id=1,selected_slots=[1],subscribe_mid=7,clock=lambda:clock[0])
    return c,clock


def report(c,packet=None,**kw):
    return c.observe(incarnation=kw.get('incarnation',c.incarnation),topic=kw.get('topic','device/synthetic/report'),
        retained=kw.get('retained',False),packet=packet if packet is not None else
        {'ams':{'ams':[{'id':0,'tray':[{'id':0,'tray_type':'PETG'}]}]}})


def snap(c,**kw):
    return c.snapshot(configuration_key=kw.get('config','a'*64),owner_org_id=kw.get('owner',1))


def ready(c):assert c.acknowledge_subscription(c.incarnation,7,[0])


def test_matching_suback_and_explicit_report_required(owned):
    c,_=owned
    assert not report(c)
    assert not c.acknowledge_subscription(c.incarnation,8,[0])
    assert not report(c)
    ready(c)
    assert not report(c,{'gcode_state':'IDLE'})
    assert snap(c) is None
    assert report(c)
    assert snap(c).trays[0].material=='PETG'


@pytest.mark.parametrize('retained',[True,None,0,'false'])
def test_retained_or_unknown_refuses_and_cannot_revive(owned,retained):
    c,_=owned;ready(c);assert report(c)
    assert not report(c,retained=retained)
    assert snap(c) is None
    assert not report(c)


@pytest.mark.parametrize('packet',[
    {'ams':None},{'ams':{'ams':[{'id':0,'tray':[{'id':0}]}]}},
    {'ams':{'ams':[{'id':0,'tray':[{'id':0,'tray_type':''}]}]}},
    {'ams':{'ams':[{'id':0,'tray':[{'id':0,'tray_type':True}]}]}},
])
def test_new_invalid_observation_supersedes_old_success(owned,packet):
    c,_=owned;ready(c);assert report(c)
    assert not report(c,packet);assert snap(c) is None;assert not report(c)


def test_reads_and_heartbeats_do_not_renew_first_receipt(owned):
    c,clock=owned;ready(c);report(c);first=snap(c)
    clock[0]=200;assert not report(c,{'gcode_state':'IDLE'})
    assert snap(c) is first
    assert first.received_monotonic==100
    clock[0]=220;assert snap(c) is None


@pytest.mark.parametrize('change',[{'config':'b'*64},{'owner':2},{'owner':True}])
def test_config_or_owner_change_retires(owned,change):
    c,_=owned;ready(c);report(c)
    assert snap(c,**change) is None;assert not report(c)


def test_old_incarnation_wrong_topic_and_retirement(owned):
    c,_=owned;ready(c)
    assert not report(c,incarnation='old')
    assert report(c)
    assert not report(c,topic='device/other/report')
    assert snap(c) is None
    ready_result=c.acknowledge_subscription(c.incarnation,7,[0])
    assert not ready_result


def test_reordered_monotonic_receipt_refuses(owned):
    c,clock=owned;ready(c);report(c);clock[0]=99
    assert not report(c);assert snap(c) is None


def test_concurrent_retirement_cannot_revive(owned):
    from concurrent.futures import ThreadPoolExecutor
    c,_=owned;ready(c)
    with ThreadPoolExecutor(2) as pool:
        a=pool.submit(report,c);b=pool.submit(c.retire);a.result();b.result()
    assert snap(c) is None;assert not report(c)


def test_waiting_is_distinct_from_invalidated(owned):
    c,_=owned;ready(c)
    assert not report(c,{'gcode_state':'IDLE'})
    assert snap(c) is None and not c.retired
    report(c,{'ams':None})
    assert snap(c) is None and c.retired


@pytest.mark.parametrize('value',[float('nan'),float('inf'),True,'100'])
def test_invalid_clock_cannot_produce_fresh_snapshot(owned,value):
    c,clock=owned;ready(c);assert report(c)
    clock[0]=value
    assert snap(c) is None and c.retired


@pytest.mark.parametrize('codes',[[0,0],[0,1],[],[True],[128]])
def test_single_successful_suback_is_required(owned,codes):
    c,_=owned
    assert not c.acknowledge_subscription(c.incarnation,7,codes)
    assert c.retired and not report(c)


@pytest.mark.parametrize('topic',['device/+/report','device/#/report','device/a\x00b/report'])
def test_topic_must_be_literal(topic):
    with pytest.raises(ValueError):
        RawAMSAcquisitionCollector(topic=topic,configuration_key='a'*64,owner_org_id=1,
            selected_slots=[1],subscribe_mid=1)


def test_boolean_mid_cannot_match_one():
    c=RawAMSAcquisitionCollector(topic='device/synthetic/report',configuration_key='a'*64,
        owner_org_id=1,selected_slots=[1],subscribe_mid=1)
    assert not c.acknowledge_subscription(c.incarnation,True,[0])
    assert c.acknowledge_subscription(c.incarnation,1,[0])


@pytest.mark.parametrize('mid',[0,65536,True])
def test_invalid_packet_id_refused(mid):
    with pytest.raises(ValueError):
        RawAMSAcquisitionCollector(topic='device/synthetic/report',configuration_key='a'*64,
            owner_org_id=1,selected_slots=[1],subscribe_mid=mid)


@pytest.mark.parametrize('status,expected', [
    ({'gcode_state': 'IDLE', 'print_error': 0}, True),
    ({'gcode_state': 'FINISH', 'print_error': 0}, True),
    ({'gcode_state': 'IDLE'}, False),
    ({'print_error': 0}, False),
    ({'gcode_state': 'IDLE', 'print_error': False}, False),
    ({'gcode_state': 'RUNNING', 'print_error': 0}, False),
    ({'gcode_state': 'IDLE', 'print_error': 1}, False),
])
def test_same_packet_idle_and_no_error_required(owned, status, expected):
    c, _ = owned; ready(c)
    assert report(c, {**status, 'ams': {'ams': [{'id': 0, 'tray': [{'id': 0, 'tray_type': 'PETG'}]}]}})
    assert snap(c).idle_without_error is expected


def test_preceding_idle_heartbeat_cannot_qualify_material_only_report(owned):
    c, _ = owned; ready(c)
    assert not report(c, {'gcode_state': 'IDLE', 'print_error': 0})
    assert report(c)
    assert snap(c).idle_without_error is False


@pytest.mark.parametrize('later', [{'gcode_state': 'RUNNING'}, {'print_error': 1}])
def test_newer_busy_or_error_invalidates_idle_without_renewing_material_receipt(owned, later):
    c, clock = owned; ready(c)
    assert report(c, {'gcode_state': 'IDLE', 'print_error': 0,
                      'ams': {'ams': [{'id': 0, 'tray': [{'id': 0, 'tray_type': 'PETG'}]}]}})
    first = snap(c); assert first.idle_without_error is True
    clock[0] = 101
    assert not report(c, later)
    current = snap(c)
    assert current.idle_without_error is False
    assert current.packet_id == first.packet_id
    assert current.received_monotonic == first.received_monotonic
    assert current.trays == first.trays


@pytest.mark.parametrize('later', [{'gcode_state':'RUNNING','print_error':0},{'gcode_state':'FAILED','print_error':1}])
def test_cancelled_zero_error_same_packet_material_readiness_is_revoked(owned,later):
    c,_=owned;ready(c)
    packet={'gcode_state':'FAILED','print_error':0,'ams':{'ams':[{'id':0,'tray':[{'id':0,'tray_type':'PETG'}]}]}}
    assert report(c,packet)
    assert snap(c).idle_without_error is True
    assert not report(c,later)
    assert snap(c).idle_without_error is False


def test_diagnostic_status_is_safe_and_tracks_later_busy_packet(owned):
    c, _ = owned
    ready(c)
    packet = {'ams': {'ams': [{'id': 0, 'tray': [{'id': 0, 'tray_type': 'PETG'}]}]},
              'gcode_state': 'IDLE', 'print_error': 0, 'hms': []}
    assert report(c, packet)
    assert snap(c).idle_without_error
    assert not report(c, {'gcode_state': 'RUNNING', 'print_error': 42,
                          'hms': [{'attr': 1, 'code': 2}, {'attr': 'private-token', 'code': 3}]})
    snapshot = snap(c)
    assert snapshot.idle_without_error is False
    assert (snapshot.gcode_state, snapshot.print_error, snapshot.active_hms_codes) == (
        'RUNNING', 42, ('00000001_00000002',))
    from modules.printers.telemetry.bambu.ams_acquisition_collector import safe_status_fields
    assert safe_status_fields({'gcode_state': 'private-serial', 'print_error': 'private-token'}) == dict(
        gcode_state=None, print_error=None, active_hms_codes=None)


@pytest.mark.parametrize('tray', [{'id':0}, {'id':0,'tray_type':''}])
def test_trusted_diagnostic_read_preserves_addressed_unreported_slot(tray):
    c=RawAMSAcquisitionCollector(topic='device/synthetic/report',configuration_key='a'*64,
        owner_org_id=1,selected_slots=[1],subscribe_mid=7,allow_unreported=True)
    ready(c)
    assert report(c, {'ams':{'ams':[{'id':0,'tray':[tray]}]}})
    fact=snap(c).trays[0]
    assert fact.slot_number==1 and fact.material is None
    assert fact.material_state in ('unknown','explicit_empty')


def test_h2d_report_with_ams_ht_acquires_the_selected_tray(owned):
    # Prod H2D (2026-10-29): units 0 and 1 plus AMS HT 128 in every AMS packet.
    # Before the fix the HT unit retired the collector, so dispatch refused ams_read_failed.
    c,_=owned;ready(c)
    packet={'gcode_state':'IDLE','print_error':0,'ams':{'ams':[
        {'id':'0','tray':[{'id':'0','tray_type':'PLA'},{'id':'1','tray_type':'PETG'}]},
        {'id':'1','tray':[{'id':'0','tray_type':'PLA'}]},
        {'id':'128','tray':[{'id':'0','tray_type':'PLA-S'}]}]}}
    assert report(c,packet)
    assert not c.retired
    assert [t.slot_number for t in snap(c).trays]==[1] and snap(c).trays[0].material=='PLA'
