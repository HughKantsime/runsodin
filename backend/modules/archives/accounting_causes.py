"""Customer explanations for the complete accounting-reason vocabulary."""
DEFAULT = ('ODIN could not tell which spool fed this print', 'Settle this print')
# Explicit even for fallback text: the source-census test rejects uncatalogued reasons.
REASONS = frozenset('''ambiguous_legacy_file ambiguous_start_spool archive_graph_changed
archive_ownership_unresolved attempt_already_accounted attempt_linkage_changed attempt_usage_without_archive
conflicting_material_quantities contradictory_start_identity ended_while_unconfirmed external_accounting
first_seen_mid_print foreign_file foreign_job_printer foreign_spool incomplete_mapping invalid_captured_spool
invalid_mapping invalid_material_metadata invalid_material_quantity invalid_measured_quantity invalid_start_snapshot
inventory_shortfall job_usage_unattributed legacy_usage_unattributed material_source_bound_exceeded
measured_material_distribution_unknown missing_explicit_file missing_file missing_material_mapping missing_material_plan
missing_or_invalid_start_snapshot missing_physical_print missing_scheduled_job missing_spool multiple_trays_observed
no_bound_spool non_successful_attempt_quantity_unconfirmed non_successful_material_distribution_unknown nothing_deducted
project_import spool_change_pending spool_identity_changed spool_owner_changed tray_evidence_incomplete
unknown_material_quantity unknown_spool_balance operator_settled operator_settled_no_deduction operator_accepted_existing
missing_price missing_net_weight invalid_price invalid_net_weight'''.split())
CAUSES = dict.fromkeys(REASONS, DEFAULT)
for reasons, title, next_step in [
    ('no_bound_spool', 'No spool was bound to the slot this print used', 'Bind spools on the Printers page; settle this print'),
    ('spool_change_pending', 'The spool in the slot changed and was not confirmed', 'Confirm the spool on the Printers page; ODIN then deducts by itself'),
    ('unknown_spool_balance', 'The spool has no known balance', 'Weigh the spool, or settle to record the use'),
    ('non_successful_attempt_quantity_unconfirmed non_successful_material_distribution_unknown unknown_material_quantity',
     'The print did not finish and the printer reported no amount', 'Settle with the grams used'),
    ('first_seen_mid_print ended_while_unconfirmed', 'ODIN did not see the whole print', 'Settle with the grams used'),
    ('attempt_usage_without_archive job_usage_unattributed legacy_usage_unattributed',
     'Filament was already deducted another way', 'Settle: keep the existing deduction'),
    ('inventory_shortfall', 'The spool had less filament than the print used', 'Weigh the spool'),
]:
    for reason in reasons.split():
        CAUSES[reason] = (title, next_step)


def describe(reason, ownership_source=None):
    title, next_step = CAUSES.get(reason, DEFAULT)
    if reason == 'archive_ownership_unresolved':
        if ownership_source in ('missing_job_file', 'source_graph_changed'):
            title, next_step = 'The print was started at the printer, not from an ODIN job', 'Settle this print'
        else:
            title, next_step = "The print's job, file or organisation records disagree", 'An administrator must review it'
    return dict(code=reason, title=title, next_step=next_step)
