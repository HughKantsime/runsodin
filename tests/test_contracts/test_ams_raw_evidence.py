"""Raw tray evidence never substitutes cached or inferred material identity."""
import pytest
from modules.printers.telemetry.bambu.ams_evidence import parse_raw_ams_facts


def packet(trays, unit='0'):
    return {'ams': {'ams': [{'id':unit, 'tray':trays}]}}


def test_partial_packet_exposes_only_reported_tray():
    facts = parse_raw_ams_facts(packet([{'id':'2', 'tray_type':'petg'}], unit='1'))
    assert len(facts) == 1
    assert (facts[0].slot_number, facts[0].unit_id, facts[0].tray_id) == (7,1,2)
    assert facts[0].material == 'PETG'
    assert facts[0].mechanically_ready is None


@pytest.mark.parametrize('raw', [{}, {'gcode_state':'IDLE'}, {'ams':{'ams':[]}}])
def test_heartbeat_or_empty_packet_has_no_tray_evidence(raw):
    assert parse_raw_ams_facts(raw) == ()


@pytest.mark.parametrize('value,state', [('', 'explicit_empty'), (None,'invalid'),
    (True,'invalid'), (45,'invalid'), ('x'*33,'invalid'), ('PETG\n','invalid'), ('\n','invalid'),
    ('new-unknown','unknown'), ('PETG_CF','reported')])
def test_material_fields_are_explicit_and_strict(value,state):
    fact = parse_raw_ams_facts(packet([{'id':0,'tray_type':value}]))[0]
    assert fact.material_state == state
    assert fact.material == ('PETG-CF' if state=='reported' else None)


def test_missing_material_never_adopts_cached_material():
    fact = parse_raw_ams_facts(packet([{'id':0}]))[0]
    assert fact.material_state == 'unknown'
    assert fact.material is None


@pytest.mark.parametrize('raw', [packet([{'id':0},{'id':'0'}]),
    packet([{'id':True}]), packet([{'id':4}]), packet([{'id':0}], unit=4),
    {'ams':None}, {'ams':{}}, {'ams':{'ams':'bad'}},
    {'ams':{'ams':[{'id':0}]}},
    {'ams':{'ams':[{'id':True,'tray':[]}]}},
    {'ams':{'ams':[{'id':0,'tray':[]},{'id':'0','tray':[]}]}}, packet([{'id':0}]*5)])
def test_malformed_batch_refuses_without_echoing_input(raw):
    with pytest.raises(ValueError, match='^AMS packet (shape|identity) unavailable$'):
        parse_raw_ams_facts(raw)


def test_input_does_not_get_modified_or_retained():
    raw=packet([{'id':0,'tray_type':'PETG'}])
    facts=parse_raw_ams_facts(raw)
    raw['ams']['ams'][0]['tray'][0]['tray_type']='PLA'
    assert facts[0].material == 'PETG'


def h2d_packet():
    """Captured H2D shape: two four-tray AMS units plus an AMS HT (unit 128)."""
    return {'ams': {'ams': [
        {'id': '0', 'tray': [{'id': str(t), 'tray_type': m} for t, m in enumerate(('PLA', 'PETG', 'PLA', 'PLA'))]},
        {'id': '1', 'tray': [{'id': str(t), 'tray_type': 'PLA'} for t in range(4)]},
        {'id': '128', 'tray': [{'id': '0', 'tray_type': 'PLA-S'}]},
    ]}}


def test_h2d_packet_with_ams_ht_yields_four_tray_facts_only():
    facts = parse_raw_ams_facts(h2d_packet())
    assert [f.slot_number for f in facts] == list(range(1, 9))
    assert (facts[0].slot_number, facts[0].material, facts[0].material_state) == (1, 'PLA', 'reported')


@pytest.mark.parametrize('unit', ['4', '127', '136'])
def test_unknown_unit_ids_still_refuse(unit):
    raw = h2d_packet(); raw['ams']['ams'][2]['id'] = unit
    with pytest.raises(ValueError, match='^AMS packet identity unavailable$'):
        parse_raw_ams_facts(raw)


def test_ams_ht_with_a_second_tray_or_duplicate_unit_refuses():
    raw = h2d_packet(); raw['ams']['ams'][2]['tray'].append({'id': '1', 'tray_type': 'PLA'})
    with pytest.raises(ValueError, match='^AMS packet identity unavailable$'):
        parse_raw_ams_facts(raw)
    raw = h2d_packet(); raw['ams']['ams'].append({'id': '128', 'tray': [{'id': '0'}]})
    with pytest.raises(ValueError, match='^AMS packet identity unavailable$'):
        parse_raw_ams_facts(raw)
