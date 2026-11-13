"""Library search and filters (models list, priced models list, print-files list): each filter,
the shared visibility rules, and that the plate cache is never read."""
from pathlib import Path

import pytest
from sqlalchemy import text

from tests.test_contracts.test_api_token_delegation_behavior import token_client, review_db  # noqa: F401
from tests.test_contracts.test_cert_C_models import library, sliced_3mf, _upload  # noqa: F401
from tests.test_contracts.test_home_assistant_status import ha_client  # noqa: F401
from tests.test_contracts.test_plate_cache_routes import env, mark_controlled  # noqa: F401

BACKEND = Path(__file__).resolve().parents[2] / 'backend'
LISTS = ('/models', '/models-with-pricing')


@pytest.fixture
def lib(env):
    row = env.db.execute(text('SELECT model_id, org_id FROM print_files WHERE id=:id'), {'id': env.file_id}).one()
    env.db.execute(text('UPDATE print_files SET plate_count=3 WHERE id=:id'), {'id': env.file_id})
    env.db.commit()
    env.model_id, env.org_id = row.model_id, row.org_id
    return env


def model_ids(env, path='/models', **params):
    response = env.client.get(path, params=params, headers=env.issue(['read:models']))
    assert response.status_code == 200, response.text
    return [m['id'] for m in response.json()]


def found(env, **params):
    results = {path: env.model_id in model_ids(env, path, **params) for path in LISTS}
    assert len(set(results.values())) == 1, results   # both model lists agree
    return results['/models']


def add_second_file(env):
    env.db.execute(text(
        "INSERT INTO print_files(filename, model_id, org_id, stored_path, filaments_json, printer_model, plate_count, blob_state) "
        "VALUES('second.3mf', :m, :o, '/nonexistent/second.3mf', :f, 'Prusa MK4', 1, 'present')"),
        {'m': env.model_id, 'o': env.org_id, 'f': '[{"slot": 1, "type": "ABS", "color": "#000000", "used_grams": 1.0}]'})
    env.db.commit()


# ------------------------------------------------------------------ q

def test_q_matches_model_name_file_name_and_project_name(lib):
    assert found(lib, q='bracket') and found(lib, q='BRACKET')
    assert not found(lib, q='flange')
    lib.db.execute(text("UPDATE print_files SET project_name='Flange Assembly' WHERE id=:id"), {'id': lib.file_id})
    assert found(lib, q='flange')
    lib.db.execute(text("UPDATE print_files SET filename='Spare-Widget.3mf', original_filename='Spare-Widget.3mf' WHERE id=:id"), {'id': lib.file_id})
    lib.db.commit()
    assert found(lib, q='spare-widget')


def test_q_wildcards_are_literal(lib):
    assert not found(lib, q='%')
    assert not found(lib, q='_')
    assert not found(lib, q='c_rt')


def test_blank_filters_change_nothing(lib):
    assert found(lib) and found(lib, q='  ', material=' ')


# ------------------------------------------------------------------ facets

def test_material_is_an_exact_filament_type(lib):
    assert found(lib, material='petg') and found(lib, material='PLA')
    assert not found(lib, material='abs') and not found(lib, material='pet')


def test_printer_model_contains(lib):
    assert found(lib, printer_model='a1') and found(lib, printer_model='Bambu Lab')
    assert not found(lib, printer_model='x1c')


def test_plate_count_min_and_max(lib):
    assert found(lib, plates_min=3) and found(lib, plates_max=3) and found(lib, plates_min=2, plates_max=3)
    assert not found(lib, plates_min=4) and not found(lib, plates_max=2)


def test_facets_must_hold_on_the_same_file(lib):
    add_second_file(lib)
    assert found(lib, material='abs') and found(lib, material='abs', printer_model='mk4')
    assert not found(lib, material='abs', printer_model='a1')
    assert not found(lib, material='abs', plates_min=3)


def test_printed_ok_on_printer(lib):
    assert not found(lib, printed_on_printer=9)
    lib.db.execute(text("INSERT INTO jobs(id,item_name,status,print_file_id,model_id,charged_to_org_id,printer_id) "
                        "VALUES(701,'Done','completed',:f,:m,:o,9)"), {'f': lib.file_id, 'm': lib.model_id, 'o': lib.org_id})
    lib.db.commit()
    assert found(lib, printed_on_printer=9)
    assert not found(lib, printed_on_printer=99999)          # unknown printer
    lib.db.execute(text("UPDATE jobs SET status='failed' WHERE id=701"))
    lib.db.commit()
    assert not found(lib, printed_on_printer=9)              # only completed counts


def test_printed_ok_needs_a_visible_printer(lib, monkeypatch):
    lib.db.execute(text("INSERT INTO jobs(id,item_name,status,print_file_id,model_id,charged_to_org_id,printer_id) "
                        "VALUES(702,'Done','completed',:f,:m,:o,9)"), {'f': lib.file_id, 'm': lib.model_id, 'o': lib.org_id})
    lib.db.execute(text('UPDATE printers SET shared=0 WHERE id=9'))
    lib.db.commit()
    assert found(lib, printed_on_printer=9)
    # A printer the caller may not see (another organization's, not shared) never matches.
    import modules.models_library.library_search as search
    asked = []
    monkeypatch.setattr(search, 'check_org_access', lambda actor, org_id: asked.append(org_id) or False)
    assert not found(lib, printed_on_printer=9) and asked


def test_parameters_are_validated(lib):
    for params in ({'plates_min': 0}, {'plates_max': 0}, {'printed_on_printer': 0}, {'q': 'x' * 101}, {'plates_min': 5, 'plates_max': 2}):
        for path in LISTS + ('/print-files',):
            assert lib.client.get(path, params=params, headers=lib.issue(['read:models'])).status_code == 422


# ------------------------------------------------------------------ visibility

def test_other_organizations_file_is_never_matched(lib):
    lib.db.execute(text("INSERT INTO print_files(filename, model_id, org_id, stored_path, filaments_json, printer_model, plate_count, blob_state) "
                        "VALUES('foreign-secret.3mf', :m, :o, '/nonexistent/foreign.3mf', '[{\"type\": \"ASA\"}]', 'Foreign X', 9, 'present')"),
                       {'m': lib.model_id, 'o': (lib.org_id or 0) + 1000})
    lib.db.commit()
    assert not found(lib, q='foreign-secret') and not found(lib, material='asa')
    assert not found(lib, printer_model='foreign x') and not found(lib, plates_min=9)


def test_controlled_file_is_not_matched_outside_the_audience(lib):
    mark_controlled(lib, lib.file_id, members=(1,))          # token user 3 is not a member
    for params in ({}, {'q': 'bracket'}, {'material': 'petg'}, {'printer_model': 'a1'}, {'plates_min': 3}):
        assert not found(lib, **params), params
        listed = lib.client.get('/print-files', params={**params, 'include_scheduled': True}, headers=lib.issue(['read:models']))
        assert listed.status_code == 200 and lib.file_id not in [f['id'] for f in listed.json()], params
    lib.db.execute(text('INSERT INTO controlled_audience_members(audience_id,user_id) VALUES(1,3)'))
    lib.db.commit()
    for params in ({'q': 'bracket'}, {'material': 'petg'}, {'printer_model': 'a1'}, {'plates_min': 3}):
        assert found(lib, **params), params
        listed = lib.client.get('/print-files', params={**params, 'include_scheduled': True}, headers=lib.issue(['read:models']))
        assert lib.file_id in [f['id'] for f in listed.json()], params


def test_controlled_completed_job_does_not_prove_printed_on(lib):
    lib.db.execute(text("INSERT INTO jobs(id,item_name,status,print_file_id,model_id,charged_to_org_id,printer_id) "
                        "VALUES(703,'Done','completed',:f,:m,:o,9)"), {'f': lib.file_id, 'm': lib.model_id, 'o': lib.org_id})
    lib.db.commit()
    mark_controlled(lib, lib.file_id, members=(1,))
    assert not found(lib, printed_on_printer=9)


def test_education_owned_file_facts_are_not_searchable(lib):
    stored = '/nonexistent/edu-secret.3mf'
    lib.db.execute(text("UPDATE print_files SET filename='edu-secret.3mf', model_id=:m, org_id=:o, stored_path=:p WHERE id=11"),
                   {'m': lib.model_id, 'o': lib.org_id, 'p': stored})
    lib.db.execute(text("UPDATE education_upload_operations SET final_path=:p WHERE operation_id='upload-1'"), {'p': stored})
    lib.db.commit()
    assert not found(lib, q='edu-secret')


def test_print_files_list_filters(lib):
    def listed(**params):
        response = lib.client.get('/print-files', params={**params, 'include_scheduled': True}, headers=lib.issue(['read:models']))
        assert response.status_code == 200, response.text
        return [f['id'] for f in response.json()]
    assert lib.file_id in listed() and lib.file_id in listed(q='bracket', material='petg', plates_min=3)
    assert lib.file_id not in listed(q='nothing-like-this') and lib.file_id not in listed(material='abs')


def test_search_never_reads_the_plate_cache():
    source = (BACKEND / 'modules' / 'models_library' / 'library_search.py').read_text()
    assert 'plate_cache' not in source and 'facts.json' not in source
