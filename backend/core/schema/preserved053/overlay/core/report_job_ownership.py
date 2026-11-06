"""Read-only retained job ownership proof for reporting.

Closed SQL fragments use the caller's jobs alias ``j``. No identity allocation,
write locks, current-user ownership inference, or legacy adoption occurs here.
"""


def proved_job_owner_sql():
    owner = "ci.owner_scope=CASE WHEN j.charged_to_org_id IS NULL THEN 'global' ELSE 'org' END AND ci.owner_key=COALESCE(j.charged_to_org_id,0)"
    return """EXISTS (SELECT 1 FROM cost_identities ci
        WHERE ci.identity_uid=j.economic_uid AND ci.resource_kind='job'
        AND ci.resource_id=j.id AND """ + owner + """ ) AND (
        (j.order_item_id IS NULL
         AND NOT EXISTS (SELECT 1 FROM cost_job_bindings b WHERE b.job_uid=j.economic_uid)
         AND NOT EXISTS (SELECT 1 FROM cost_order_memberships n WHERE n.child_uid=j.economic_uid))
        OR EXISTS (SELECT 1 FROM order_items i JOIN orders o ON o.id=i.order_id
          JOIN cost_identities ic ON ic.identity_uid=i.economic_uid AND ic.resource_kind='item' AND ic.resource_id=i.id
          JOIN cost_identities oc ON oc.identity_uid=o.economic_uid AND oc.resource_kind='order' AND oc.resource_id=o.id
          WHERE i.id=j.order_item_id
          AND ((o.org_id=j.charged_to_org_id) OR (o.org_id IS NULL AND j.charged_to_org_id IS NULL))
          AND ic.owner_scope=oc.owner_scope AND ic.owner_key=oc.owner_key
          AND oc.owner_scope=CASE WHEN j.charged_to_org_id IS NULL THEN 'global' ELSE 'org' END
          AND oc.owner_key=COALESCE(j.charged_to_org_id,0)
          AND EXISTS (SELECT 1 FROM cost_job_bindings b WHERE b.job_uid=j.economic_uid
            AND b.order_uid=o.economic_uid AND b.item_uid=i.economic_uid
            AND b.owner_scope=oc.owner_scope AND b.owner_key=oc.owner_key)
          AND EXISTS (SELECT 1 FROM cost_order_memberships n WHERE n.child_uid=j.economic_uid
            AND n.child_kind='job' AND n.order_uid=o.economic_uid
            AND n.owner_scope=oc.owner_scope AND n.owner_key=oc.owner_key)
          AND EXISTS (SELECT 1 FROM cost_order_memberships n WHERE n.child_uid=i.economic_uid
            AND n.child_kind='item' AND n.order_uid=o.economic_uid
            AND n.owner_scope=oc.owner_scope AND n.owner_key=oc.owner_key)
          AND NOT EXISTS (SELECT 1 FROM cost_order_memberships n WHERE n.child_uid=j.economic_uid AND n.child_kind<>'job')
          AND NOT EXISTS (SELECT 1 FROM cost_order_memberships n WHERE n.child_uid=i.economic_uid AND n.child_kind<>'item')
        ))"""


def proved_education_job_sql():
    # Markers include associated files/models even if their submission is orphaned.
    associated = """(af.id=j.print_file_id OR af.job_id=j.id
      OR (j.model_id IS NOT NULL AND af.model_id=j.model_id)
      OR af.id=(SELECT m.print_file_id FROM models m WHERE m.id=j.model_id)
      OR EXISTS (SELECT 1 FROM print_files seed WHERE seed.stored_path=af.stored_path
        AND seed.stored_path IS NOT NULL AND (seed.id=j.print_file_id OR seed.job_id=j.id
          OR (j.model_id IS NOT NULL AND seed.model_id=j.model_id)
          OR seed.id=(SELECT m.print_file_id FROM models m WHERE m.id=j.model_id))))"""
    marked = """EXISTS (SELECT 1 FROM education_submissions s WHERE s.job_id=j.id
      OR s.print_file_id=j.print_file_id OR s.model_id=j.model_id
      OR s.print_file_id=(SELECT m.print_file_id FROM models m WHERE m.id=j.model_id))
      OR EXISTS (SELECT 1 FROM education_upload_operations x JOIN print_files f
        ON x.final_path=f.stored_path AND x.final_path IS NOT NULL
        WHERE f.id=j.print_file_id OR f.id=(SELECT m.print_file_id FROM models m WHERE m.id=j.model_id))"""
    marked += " OR EXISTS (SELECT 1 FROM print_files af WHERE " + associated + """
      AND (EXISTS (SELECT 1 FROM education_submissions marker
            WHERE marker.print_file_id=af.id OR (af.model_id IS NOT NULL AND marker.model_id=af.model_id)
              OR EXISTS (SELECT 1 FROM models alias_model WHERE alias_model.id=marker.model_id AND alias_model.print_file_id=af.id))
        OR EXISTS (SELECT 1 FROM education_upload_operations marker
            WHERE marker.final_path=af.stored_path AND marker.final_path IS NOT NULL)))"""
    graph = """EXISTS (SELECT 1 FROM education_submissions s
      JOIN education_cost_centers c ON c.id=s.cost_center_id AND c.org_id=s.org_id
      JOIN print_files f ON f.id=s.print_file_id AND f.org_id=s.org_id AND f.created_by=s.submitted_by
      JOIN models m ON m.id=s.model_id AND m.org_id=s.org_id AND m.print_file_id=f.id
      JOIN education_upload_operations x ON x.operation_id=s.operation_id
        AND x.org_id=s.org_id AND x.user_id=s.submitted_by
        AND x.final_path=f.stored_path AND x.state='committed'
      WHERE s.job_id=j.id AND s.org_id=j.charged_to_org_id
        AND s.submitted_by=j.submitted_by AND j.model_id=m.id AND j.print_file_id=f.id
        AND NOT EXISTS (SELECT 1 FROM education_submissions other WHERE other.id<>s.id
          AND (other.job_id=j.id OR other.print_file_id=f.id OR other.model_id=m.id))
        AND NOT EXISTS (SELECT 1 FROM education_upload_operations other
          WHERE other.final_path=f.stored_path AND other.final_path IS NOT NULL
          AND other.operation_id<>x.operation_id)
        AND NOT EXISTS (SELECT 1 FROM print_files af WHERE """ + associated + """
          AND (EXISTS (SELECT 1 FROM education_submissions marker
                WHERE marker.id<>s.id AND (marker.print_file_id=af.id
                  OR (af.model_id IS NOT NULL AND marker.model_id=af.model_id)
                  OR EXISTS (SELECT 1 FROM models alias_model WHERE alias_model.id=marker.model_id AND alias_model.print_file_id=af.id)))
            OR EXISTS (SELECT 1 FROM education_upload_operations marker
                WHERE marker.final_path=af.stored_path AND marker.final_path IS NOT NULL
                  AND marker.operation_id<>x.operation_id))))"""
    return "(NOT (" + marked + ") OR " + graph + ")"
