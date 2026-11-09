"""Shared chargeback aggregation SQL."""

# Recorded filament per job from its print attempts (measured when the printer
# reported it, otherwise the file estimate; failed attempts count). Attempts with
# no recorded grams are counted, not treated as zero.
ARCHIVE_GRAMS_BY_JOB_SQL = (
    "SELECT job_id, SUM(filament_used_grams) AS grams, "
    "SUM(CASE WHEN filament_used_grams IS NULL THEN 1 ELSE 0 END) AS unmeasured "
    "FROM print_archives WHERE job_id IS NOT NULL GROUP BY job_id"
)
