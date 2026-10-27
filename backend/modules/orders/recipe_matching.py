"""Recipe comparisons; legacy revisions remain immutable in storage."""
from fastapi import HTTPException

FIELDS = (('file', ('print_file_id','payload_sha256')), ('plate', ('plate_index',)),
          ('model', ('model_id',)), ('printer', ('printer_id',)),
          ('printer configuration', ('configuration_key',)))


def assert_payload_matches(actual, expected):
    differences = [label for label, keys in FIELDS if any(actual.get(k) != expected.get(k) for k in keys)]
    if differences:
        raise HTTPException(409, 'Job '+', '.join(differences)+' does not match this recipe revision')


def resolve_legacy_recipe(db, owner, spec):
    from modules.orders import batch_production as batches
    from core.recipe_configuration import REFUSAL
    if not spec.get('reference_job_id') or not spec.get('reference_claim_token'):
        raise HTTPException(409, REFUSAL)
    evidence = batches.job_evidence(db, owner, spec['reference_job_id'])
    legacy_payload = {key:evidence[key] for _,keys in FIELDS for key in keys}
    if (evidence['claim_token'] != spec['reference_claim_token']
            or evidence['observation_uid'] != spec.get('reference_observation_uid')
            or legacy_payload != spec['payload']):
        raise HTTPException(409, 'Legacy recipe reference evidence differs. Qualify a new recipe revision.')
    return {**spec, 'version':2, 'payload':batches.recipe_payload(evidence)}


def resolve_for_evidence(db, owner, spec, evidence):
    # Do not acquire a different printer's legacy-backfill lock. Callers have
    # already locked the inspected job and its printer, in admission order.
    if spec['payload']['printer_id'] != evidence['printer_id']:
        raise HTTPException(409, 'Job printer does not match this recipe revision')
    return resolve_legacy_recipe(db, owner, spec) if spec.get('version') == 1 else spec
