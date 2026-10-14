"""Explicit mapping must validate selected material, not merely slot existence."""
import json
import pytest
from fastapi import HTTPException
from core.print_payload import validate_mapping


@pytest.mark.parametrize('loaded', ['PLA', 'empty', 'Unknown', 'OTHER'])
def test_selected_tray_must_have_source_material(loaded):
    with pytest.raises(HTTPException):
        validate_mapping(
            [{'source_slot': 0, 'target_slot': 1}],
            json.dumps([{'slot':1, 'type':'PETG', 'color':'#000000'}]),
            {1: loaded}, 'bambu')


@pytest.mark.parametrize('source,target', [('PETG','petg'), ('PETG_CF','PETG-CF'), ('PLA','PLA')])
def test_matching_material_preserves_explicit_protocol_index(source,target):
    assert validate_mapping([{'source_slot':0,'target_slot':4}],
        json.dumps([{'slot':1,'type':source}]), {4:target}, 'bambu') == [3]


@pytest.mark.parametrize('source,target', [('PETG','PETG-CF'), ('PLA','PLA-SILK'), ('Unknown','Unknown'), (None,'PETG'), ('PETG',True)])
def test_variants_and_unknown_material_are_not_substituted(source,target):
    with pytest.raises(HTTPException):
        validate_mapping([{'source_slot':0,'target_slot':1}],
            json.dumps([{'slot':1,'type':source}]), {1:target}, 'bambu')
