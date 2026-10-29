"""Synthetic telemetry values through actual AMS display projections, no printer access."""
import pytest
from modules.printers.telemetry.bambu.raw import BambuPrintSection
from modules.printers.telemetry.bambu.status_view import ams_slots_from_section
from modules.printers.monitors.mqtt_telemetry import parse_h2d_external_spools


def test_ams_display_retains_material_color_rfid_and_zero_remaining():
    section = BambuPrintSection.model_validate({'ams': {'ams': [{'id':'0','tray': [
        {'id':'0','tray_type':'PETG','tray_color':'12AB3480','remain':0,'tag_uid':'synthetic-tag','tray_sub_brands':'Synthetic PETG'},
        {'id':'1'},
    ]}]}})
    slots = ams_slots_from_section(section)
    assert len(slots) == 2
    loaded, empty = slots
    assert (loaded.slot_number,loaded.filament_type,loaded.color,loaded.color_hex,loaded.remaining_percent,loaded.rfid_tag,loaded.sub_brand,loaded.empty) == (1,'PETG','12AB3480','12AB34',0,'synthetic-tag','Synthetic PETG',False)
    assert empty.empty is True and empty.filament_type == '' and empty.rfid_tag == ''


@pytest.mark.parametrize('payload', [None,{}, {'ams':None}])
def test_no_ams_does_not_fabricate_slots(payload):
    section = None if payload is None else BambuPrintSection.model_validate(payload)
    assert ams_slots_from_section(section) == []


def test_external_spool_sides_material_color_and_zero_are_not_swapped():
    result = parse_h2d_external_spools({'vt_tray':[
        {'tray_type':'PLA','tray_color':'12AB34','remain':0},
        {'tray_type':'PETG','tray_color':'#AB1234','remain':70},
    ]})
    assert result == {'left':{'material':'PLA','color':'#12AB34','remain_percent':0},'right':{'material':'PETG','color':'#AB1234','remain_percent':70}}


def test_single_external_spool_is_left_and_unknown_remaining_is_preserved():
    assert parse_h2d_external_spools({'vt_tray':{'tray_type':'PLA','remain':None}}) == {'left':{'material':'PLA','color':'','remain_percent':None}}


def test_empty_left_external_spool_does_not_shift_right_to_left():
    assert parse_h2d_external_spools({'vt_tray':[{}, {'tray_type':'PETG','remain':20}]}) == {'right':{'material':'PETG','color':'','remain_percent':20}}


@pytest.mark.parametrize('payload',[None,[],{}, {'vt_tray':None},{'vt_tray':[]},{'vt_tray':[None,{}]}])
def test_absent_external_spools_are_not_fabricated(payload):
    assert parse_h2d_external_spools(payload) is None


@pytest.mark.parametrize('tray,expected', [({'id':'0','tray_type':'PLA'},None),({'id':'0','tray_type':'PLA','remain':None},None),({'id':'0','tray_type':'PLA','remain':0},0)])
def test_both_ams_projections_distinguish_unknown_from_measured_zero(tray, expected):
    # spool-tray-identity spec §1: measured percentages require a real identity.
    tray = {'tag_uid':'synthetic-tag', **tray}
    from modules.printers.adapters.bambu import BambuPrinter
    section=BambuPrintSection.model_validate({'ams':{'ams':[{'id':'0','tray':[tray]}]}})
    assert ams_slots_from_section(section)[0].remaining_percent is expected
    legacy=BambuPrinter('127.0.0.1','synthetic-serial','')
    legacy._parse_ams({'ams':[{'id':'0','tray':[tray]}]})
    assert legacy._status.ams_slots[0].remaining_percent is expected


@pytest.mark.parametrize('value', [True,False,12.5,'bad','12.5',float('inf'),float('-inf'),float('nan')])
def test_legacy_malformed_remaining_is_unknown_without_truncation_or_crash(value):
    from modules.printers.adapters.bambu import BambuPrinter
    legacy=BambuPrinter('127.0.0.1','synthetic-serial','')
    legacy._parse_ams({'ams':[{'id':'0','tray':[{'id':'0','tray_type':'PLA','tag_uid':'synthetic-tag','remain':value}]}]})
    assert legacy._status.ams_slots[0].remaining_percent is None


@pytest.mark.parametrize('value', [True,False])
def test_typed_decoder_does_not_turn_boolean_into_measured_percentage(value):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        BambuPrintSection.model_validate({'ams':{'ams':[{'id':'0','tray':[{'id':'0','tray_type':'PLA','tag_uid':'synthetic-tag','remain':value}]}]}})


@pytest.mark.parametrize('value,expected', [(0,0),('0',0),('70',70),(-1,None),(None,None)])
def test_legacy_remaining_valid_integer_compatibility(value, expected):
    # Spec §1 replaces the legacy -1 sentinel with unknown (None).
    from modules.printers.adapters.bambu import BambuPrinter
    legacy=BambuPrinter('127.0.0.1','synthetic-serial','')
    legacy._parse_ams({'ams':[{'id':'0','tray':[{'id':'0','tray_type':'PLA','tag_uid':'synthetic-tag','remain':value}]}]})
    assert legacy._status.ams_slots[0].remaining_percent==expected


def test_sparse_reordered_ams_projections_use_physical_unit_and_tray_ids():
    from modules.printers.adapters.bambu import BambuPrinter
    units=[{'id':'2','tray':[{'id':'2','tray_type':'PLA','remain':25}]}, {'id':'0','tray':[{'id':'3','tray_type':'PETG','remain':50}]}]
    section=BambuPrintSection.model_validate({'ams':{'ams':units}})
    assert [slot.slot_number for slot in ams_slots_from_section(section)]==[11,4]
    legacy=BambuPrinter('127.0.0.1','synthetic-serial','')
    legacy._parse_ams({'ams':units})
    assert [slot.slot_number for slot in legacy._status.ams_slots]==[11,4]


@pytest.mark.parametrize('bad_id', ['bad', '-1', '4'])
def test_projections_omit_invalid_identity_preserving_known_physical_slot(bad_id):
    from modules.printers.adapters.bambu import BambuPrinter
    units=[{'id':'0','tray':[{'id':'3','tray_type':'PLA'}, {'id':bad_id,'tray_type':'PETG'}]}]
    section=BambuPrintSection.model_validate({'ams':{'ams':units}})
    assert [slot.slot_number for slot in ams_slots_from_section(section)]==[4]
    legacy=BambuPrinter('127.0.0.1','synthetic-serial','')
    legacy._parse_ams({'ams':units})
    assert [slot.slot_number for slot in legacy._status.ams_slots]==[4]


def test_projections_omit_duplicate_physical_destinations_without_order_winner():
    from modules.printers.adapters.bambu import BambuPrinter
    units=[{'id':'0','tray':[{'id':'0','tray_type':'PLA'},{'id':'0','tray_type':'PETG'},{'id':'3','tray_type':'ABS'}]}]
    section=BambuPrintSection.model_validate({'ams':{'ams':units}})
    assert [slot.slot_number for slot in ams_slots_from_section(section)]==[4]
    legacy=BambuPrinter('127.0.0.1','synthetic-serial','')
    legacy._parse_ams({'ams':units})
    assert [slot.slot_number for slot in legacy._status.ams_slots]==[4]


def test_legacy_missing_unit_or_tray_identity_never_uses_array_position():
    from modules.printers.adapters.bambu import BambuPrinter
    legacy=BambuPrinter('127.0.0.1','synthetic-serial','')
    legacy._parse_ams({'ams':[{'tray':[{'id':'0','tray_type':'PLA'}]},{'id':'0','tray':[{'tray_type':'PETG'}]}]})
    assert legacy._status.ams_slots==[]


@pytest.mark.parametrize('identity', [{}, {'tag_uid':'00000000'}, {'tray_uuid':'00000000'}])
@pytest.mark.parametrize('remaining', [0, 70, -1])
def test_tagless_remaining_is_unknown_in_both_projections(identity, remaining):
    from modules.printers.adapters.bambu import BambuPrinter
    tray = {'id':'0','tray_type':'PLA','remain':remaining, **identity}
    units = [{'id':'0','tray':[tray]}]
    section = BambuPrintSection.model_validate({'ams':{'ams':units}})
    assert ams_slots_from_section(section)[0].remaining_percent is None
    legacy = BambuPrinter('127.0.0.1','synthetic-serial','')
    legacy._parse_ams({'ams':units})
    assert legacy._status.ams_slots[0].remaining_percent is None
