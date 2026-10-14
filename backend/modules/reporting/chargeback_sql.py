"""Shared chargeback aggregation SQL."""

# Measured filament per job from its print attempts (failed attempts consume
# filament too). Prints without a measurement are counted, not treated as zero.
ARCHIVE_GRAMS_BY_JOB_SQL = (
    "SELECT job_id, SUM(filament_used_grams) AS grams, "
    "SUM(CASE WHEN filament_used_grams IS NULL THEN 1 ELSE 0 END) AS unmeasured "
    "FROM print_archives WHERE job_id IS NOT NULL GROUP BY job_id"
)
