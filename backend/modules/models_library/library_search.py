"""Server-side search and filters for the model library (models list and print-files list).

Database columns only: models.name, print_files.filename / original_filename / project_name /
filaments_json / printer_model / plate_count, and completed ``jobs`` rows. The per-plate cache is a
file cache and is never read here.

Every file-based condition is evaluated against files the caller may see, inside the SQL: the
organization fence, the controlled-part audience (``file_visibility``) and Education ownership
(Education-owned files are never matched by their file facts). "Printed OK on printer X" also
requires the printer to be visible to the caller and the job to pass ``job_visibility`` and the
organization fence. Nothing here replaces the callers' own visibility predicates: it only narrows.
"""
from typing import Optional

from fastapi import HTTPException, Query
from sqlalchemy import text

from core.rbac import check_org_access, get_org_scope, should_filter_org

_ESCAPE = " ESCAPE '\\'"
_NOT_EDUCATION = (
    "NOT EXISTS (SELECT 1 FROM education_submissions es WHERE es.print_file_id={a}.id) "
    "AND NOT EXISTS (SELECT 1 FROM education_upload_operations eo "
    "WHERE eo.final_path IS NOT NULL AND eo.final_path={a}.stored_path)"
)


def _contains(value: str) -> str:
    escaped = value.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return "%" + escaped + "%"


class LibraryFilters:
    """Query parameters shared by the models list, the priced models list and the print-files list."""

    def __init__(
        self,
        q: Optional[str] = Query(None, max_length=100, description="Model name, file name or project name"),
        material: Optional[str] = Query(None, max_length=40, description="Filament type, exact (e.g. PETG)"),
        printer_model: Optional[str] = Query(None, max_length=80, description="Sliced-for printer model contains"),
        plates_min: Optional[int] = Query(None, ge=1, le=9999),
        plates_max: Optional[int] = Query(None, ge=1, le=9999),
        printed_on_printer: Optional[int] = Query(None, ge=1, description="A completed job of the file ran on this printer"),
    ):
        if plates_min is not None and plates_max is not None and plates_min > plates_max:
            raise HTTPException(status_code=422, detail="plates_min cannot be greater than plates_max")
        self.q = (q or "").strip() or None
        self.material = (material or "").strip() or None
        self.printer_model = (printer_model or "").strip() or None
        self.plates_min = plates_min
        self.plates_max = plates_max
        self.printed_on_printer = printed_on_printer

    @property
    def active(self) -> bool:
        return any(v is not None for v in (self.q, self.material, self.printer_model, self.plates_min,
                                           self.plates_max, self.printed_on_printer))

    @property
    def has_facets(self) -> bool:
        return any(v is not None for v in (self.material, self.printer_model, self.plates_min,
                                           self.plates_max, self.printed_on_printer))


def _printer_visible(db, actor, printer_id) -> bool:
    row = db.execute(text("SELECT org_id, shared FROM printers WHERE id=:id"), {"id": printer_id}).first()
    return row is not None and (check_org_access(actor, row.org_id) or bool(row.shared))


def _name_clause(alias: str) -> str:
    columns = ("filename", "original_filename", "project_name")
    return "(" + " OR ".join(f"lower({alias}.{c}) LIKE :lib_q{_ESCAPE}" for c in columns) + ")"


def _facet_conditions(db, actor, filters, alias, effective_org, filter_org):
    """Conditions that must all hold for ONE file (alias is the print_files alias in the caller's SQL)."""
    if alias not in ("f", "print_files"):
        raise ValueError("Untrusted file alias")
    conditions, params = [], {}
    if filters.material:
        value = filters.material.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params["lib_mat_a"] = f'%"type": "{value}"%'
        params["lib_mat_b"] = f'%"type":"{value}"%'
        column = f"lower(CAST({alias}.filaments_json AS TEXT))"
        conditions.append(f"({column} LIKE :lib_mat_a{_ESCAPE} OR {column} LIKE :lib_mat_b{_ESCAPE})")
    if filters.printer_model:
        params["lib_pm"] = _contains(filters.printer_model)
        conditions.append(f"lower({alias}.printer_model) LIKE :lib_pm{_ESCAPE}")
    if filters.plates_min is not None:
        params["lib_pmin"] = filters.plates_min
        conditions.append(f"COALESCE({alias}.plate_count, 1) >= :lib_pmin")
    if filters.plates_max is not None:
        params["lib_pmax"] = filters.plates_max
        conditions.append(f"COALESCE({alias}.plate_count, 1) <= :lib_pmax")
    if filters.printed_on_printer is not None:
        if not _printer_visible(db, actor, filters.printed_on_printer):
            conditions.append("1=0")
        else:
            from core.controlled_parts import job_visibility
            visible, values = job_visibility(actor, "j")
            org = ""
            if filter_org:
                if effective_org is None:
                    org = " AND j.charged_to_org_id IS NULL"
                else:
                    org = " AND j.charged_to_org_id=:lib_job_org"
                    params["lib_job_org"] = effective_org
            params["lib_printer"] = filters.printed_on_printer
            params.update(values)
            conditions.append(
                f"EXISTS (SELECT 1 FROM jobs j WHERE j.print_file_id={alias}.id AND j.printer_id=:lib_printer "
                f"AND j.status='completed'{org} AND ({visible}) "
                "AND NOT EXISTS (SELECT 1 FROM education_submissions jes WHERE jes.job_id=j.id))")
    return conditions, params


def model_clause(db, actor, filters, effective_org, filter_org):
    """(sql, params) narrowing the models query, or None when no filter is set."""
    if filters is None or not filters.active:
        return None
    from core.controlled_parts import file_visibility
    visible, values = file_visibility(actor, "f.id")
    params = dict(values)
    base = "(f.model_id=models.id OR f.id=models.print_file_id) AND (" + visible + ") AND " + _NOT_EDUCATION.format(a="f")
    if filter_org:
        if effective_org is None:
            base += " AND f.org_id IS NULL"
        else:
            base += " AND (f.org_id=:lib_org OR f.org_id IS NULL)"
            params["lib_org"] = effective_org
    parts = []
    if filters.q:
        params["lib_q"] = _contains(filters.q)
        parts.append(f"(lower(models.name) LIKE :lib_q{_ESCAPE} OR EXISTS (SELECT 1 FROM print_files f WHERE {base} AND {_name_clause('f')}))")
    if filters.has_facets:
        conditions, facet_params = _facet_conditions(db, actor, filters, "f", effective_org, filter_org)
        params.update(facet_params)
        parts.append("EXISTS (SELECT 1 FROM print_files f WHERE " + base + " AND " + " AND ".join(conditions) + ")")
    return "(" + " AND ".join(parts) + ")", params


def apply_model_filters(query, db, actor, filters, effective_org):
    clause = model_clause(db, actor, filters, effective_org, should_filter_org(actor, effective_org))
    if clause is None:
        return query
    return query.filter(text(clause[0])).params(**clause[1])


def file_clause(db, actor, filters):
    """(' AND (...)', params) to append to the print-files list predicate (alias print_files)."""
    if filters is None or not filters.active:
        return "", {}
    effective_org = get_org_scope(actor)
    filter_org = should_filter_org(actor, effective_org)
    params, parts = {}, []
    if filters.q:
        params["lib_q"] = _contains(filters.q)
        parts.append(_name_clause("print_files"))
    if filters.has_facets:
        conditions, facet_params = _facet_conditions(db, actor, filters, "print_files", effective_org, filter_org)
        params.update(facet_params)
        parts.extend(conditions)
    parts.append(_NOT_EDUCATION.format(a="print_files"))
    return " AND (" + " AND ".join(parts) + ")", params
