"""Verified first-review claims; no expansion of durable evidence or enum policy."""
import pytest
from sqlalchemy import inspect, text

from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_six_materials import SIX
from tests.test_contracts.test_six_materials_catalogue import seed


@pytest.mark.parametrize('material', SIX)
def test_initial_slots_accept_exact_labels_and_enum_values(material):
    from modules.printers.schemas import PrinterCreate
    for value in (material, material.replace('-', '_')):
        request = PrinterCreate(name='Fixture', initial_slots=[dict(slot_number=1, filament_type=value)])
        assert request.initial_slots[0].filament_type.value == material.replace('-', '_')


@pytest.mark.parametrize('material', SIX)
def test_job_and_model_inputs_accept_six_enum_values(material):
    from modules.jobs.schemas import JobCreate, JobUpdate
    from modules.models_library.schemas import ModelCreate, ModelUpdate, ColorRequirement
    value = material.replace('-', '_')
    assert JobCreate(item_name='Fixture', filament_type=value).filament_type.value == value
    assert JobUpdate(filament_type=value).filament_type.value == value
    assert ModelCreate(name='Fixture', default_filament_type=value).default_filament_type.value == value
    assert ModelUpdate(default_filament_type=value).default_filament_type.value == value
    assert ColorRequirement(color='Blue', filament_type=value).filament_type.value == value


@pytest.mark.parametrize('material', SIX)
def test_library_spelling_does_not_change_material_matches(material):
    from core.print_payload import material_match
    stored = material.replace('-', '_')
    assert material_match(stored, material) is True
    assert material_match(material, stored) is True


def test_backfill_has_timestamp_and_valid_audit_chain(review_db):
    from core.audit_integrity import verify
    from modules.inventory.catalogue import backfill_catalogue_materials
    seed(review_db)
    connection = review_db.connection()
    columns = inspect(connection).get_columns('audit_logs')
    assert {c['name'] for c in columns if not c['nullable'] and c['default'] is None} == {'id', 'action'}
    assert next(c for c in columns if c['name'] == 'timestamp')['default'] is not None
    backfill_catalogue_materials(connection)
    backfill_catalogue_materials(connection)
    rows = connection.execute(text("SELECT timestamp,chain_seq,row_hash FROM audit_logs "
        "WHERE action='filament.catalogue_material_recognized'")).all()
    assert len(rows) == 6
    assert all(timestamp and seq > 0 and len(digest) == 64 for timestamp, seq, digest in rows)
    assert verify(connection)['ok'] is True


@pytest.mark.parametrize('material', SIX)
def test_unknown_material_input_stays_closed_on_create(material):
    from pydantic import ValidationError
    from modules.printers.schemas import FilamentSlotCreate
    with pytest.raises(ValidationError):
        FilamentSlotCreate(slot_number=1, filament_type=material + '-unknown')


def test_presets_store_all_six_using_the_job_enum_spelling(review_db):
    from modules.jobs.routes.presets import CreatePresetRequest, create_preset
    from modules.jobs.models import PrintPreset
    for material in SIX:
        value = material.replace('-', '_')
        result = create_preset(CreatePresetRequest(name=material, filament_type=value),
            current_user=dict(id=3, role='admin', group_id=1), db=review_db)
        assert review_db.get(PrintPreset, result['id']).filament_type.value == value


def test_explicit_historical_contracts_predate_catalogue_rows():
    from core.schema.pre026_restore_contract import PRE026_RESTORE025, PRE027_RESTORE026, inventory_for
    for contract in (PRE026_RESTORE025, PRE027_RESTORE026):
        columns = inventory_for(contract)['columns']['filament_library']
        assert 'catalogue_code' not in columns and 'catalogue_type' not in columns


@pytest.mark.parametrize('change', ['new-material', 'missing-stage-one'])
def test_frozen_vocabulary_pin_requires_an_explicit_decision(monkeypatch, change):
    from core import material_identity
    from tests.test_contracts.test_ams_evidence_schema import test_frozen_schema_vocabulary_excludes_only_stage_one_materials
    changed = material_identity._MATERIALS | {'FUTURE-POLYMER'} if change == 'new-material' else material_identity._MATERIALS - {'PET-CF'}
    monkeypatch.setattr(material_identity, '_MATERIALS', changed)
    with pytest.raises(AssertionError):
        test_frozen_schema_vocabulary_excludes_only_stage_one_materials()


def test_release_note_is_under_added_and_states_education_limit():
    from pathlib import Path
    changelog = (Path(__file__).resolve().parents[2] / 'CHANGELOG.md').read_text()
    # The note belongs to release 2.3.0: read that release's Added section, not the first one in the
    # file (entries added under [Unreleased] for the next release come before it).
    release = changelog.split('\n## [2.3.0]', 1)[1].split('\n## [', 1)[0]
    added = release.split('### Added\n', 1)[1].split('\n### ', 1)[0]
    note = next(line for line in added.splitlines() if line.startswith('- Six more Bambu materials'))
    assert 'Education AMS reads do not yet accept these six materials' in note
