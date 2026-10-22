"""Canonical persisted payload selection for jobs joined as j, m and s.

Explicit pointers never fall back when missing or linked to a different model.
Education's authoritative graph cannot hide a contradictory explicit job file.
"""
SELECTED_PRINT_FILE_ID_SQL = """CASE
    WHEN s.id IS NOT NULL THEN s.print_file_id
    WHEN j.print_file_id IS NOT NULL THEN j.print_file_id
    WHEN m.print_file_id IS NOT NULL THEN m.print_file_id
    ELSE (SELECT candidate.id FROM print_files candidate
          WHERE candidate.model_id=m.id ORDER BY candidate.id DESC LIMIT 1)
END"""
SELECTED_PRINT_FILE_JOIN_SQL = (
    "pf.id=(" + SELECTED_PRINT_FILE_ID_SQL + ") AND pf.model_id=m.id "
    "AND (s.id IS NULL OR j.print_file_id IS NULL OR j.print_file_id=s.print_file_id)"
)
