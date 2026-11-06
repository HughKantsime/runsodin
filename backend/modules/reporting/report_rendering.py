"""Compatibility exports for the shared report rendering implementation."""
from modules.reporting.report_runner import get_smtp_config, send_report_email
from modules.reporting.report_generators import (
    _wrap_report_html, generate_fleet_utilization, generate_job_summary, generate_filament_consumption, generate_failure_analysis, generate_chargeback_summary,
)
