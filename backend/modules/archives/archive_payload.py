"""Archive response fields shared by list, detail and comparison."""
from .accounting_causes import describe
from .settlement_data import annotate_settled

def archive_row_to_dict(r, db=None, user=None):
    """Convert a raw archive row to a response dict with printer display name."""
    d = dict(r._mapping)
    d["printer_display"] = (
        d.get("printer_nickname") or d.get("printer_name")
        or f"Printer {d.get('printer_id')}"
    )
    import json
    d['consumption'] = json.loads(d.get('consumption_json') or '[]')
    # Parse tags from comma-separated string
    raw_tags = d.get("tags") or ""
    d["tags"] = [t.strip() for t in raw_tags.split(",") if t.strip()] if raw_tags else []
    if d.get("accounting_state") in ("unresolved", "reconciliation_required"):
        d["accounting_cause"] = describe(d.get("accounting_reason"), d.get("ownership_source"))
    if db is not None:
        annotate_settled(db, d, user)
    return d

