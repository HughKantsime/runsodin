"""Existing generic report rendering, moved verbatim from report_runner."""
from html import escape
from sqlalchemy import text
from core.db_compat import sql
from modules.reporting.chargeback_sql import ARCHIVE_GRAMS_BY_JOB_SQL

def _wrap_report_html(title, content):
    """Wrap report content in branded email template."""
    return f"""
    <div style="font-family: system-ui, sans-serif; background: #1a1917; color: #e5e4e1; padding: 24px; border-radius: 8px;">
        <h2 style="margin: 0 0 16px; color: #fbbf24;">📊 {title}</h2>
        {content}
        <p style="color: #555; margin-top: 20px; font-size: 11px;">— O.D.I.N. (Orchestrated Dispatch &amp; Inventory Network)</p>
    </div>
    """


def generate_fleet_utilization(session, filters):
    """Printer count, utilization %, hours."""
    total = session.execute(text("SELECT COUNT(*) FROM printers")).scalar() or 0
    active = session.execute(text("SELECT COUNT(*) FROM printers WHERE is_active IS TRUE")).scalar() or 0
    from core.printer_utils import PRINTING_STATES, state_sql_in
    printing_sql, printing_params = state_sql_in("gcode_state", PRINTING_STATES, "state")
    printing = session.execute(text(f"SELECT COUNT(*) FROM printers WHERE {printing_sql}"), printing_params).scalar() or 0  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fragment: state_sql_in() over the PRINTING_STATES/IDLE_STATES module constants; states are bound
    total_hours = session.execute(text(
        "SELECT COALESCE(SUM(total_print_hours), 0) FROM printers"
    )).scalar() or 0

    utilization = round((printing / active * 100) if active > 0 else 0, 1)

    content = f"""
    <table style="width: 100%; border-collapse: collapse; background: #33312d; border-radius: 4px;">
        <tr style="border-bottom: 1px solid #47453d;">
            <td style="padding: 10px; color: #8a8679; font-size: 12px;">TOTAL PRINTERS</td>
            <td style="padding: 10px; font-size: 16px; font-weight: bold;">{total}</td>
        </tr>
        <tr style="border-bottom: 1px solid #47453d;">
            <td style="padding: 10px; color: #8a8679; font-size: 12px;">ACTIVE</td>
            <td style="padding: 10px; font-size: 16px; font-weight: bold;">{active}</td>
        </tr>
        <tr style="border-bottom: 1px solid #47453d;">
            <td style="padding: 10px; color: #8a8679; font-size: 12px;">CURRENTLY PRINTING</td>
            <td style="padding: 10px; font-size: 16px; font-weight: bold;">{printing}</td>
        </tr>
        <tr style="border-bottom: 1px solid #47453d;">
            <td style="padding: 10px; color: #8a8679; font-size: 12px;">UTILIZATION</td>
            <td style="padding: 10px; font-size: 16px; font-weight: bold;">{utilization}%</td>
        </tr>
        <tr>
            <td style="padding: 10px; color: #8a8679; font-size: 12px;">TOTAL PRINT HOURS</td>
            <td style="padding: 10px; font-size: 16px; font-weight: bold;">{round(total_hours, 1)}</td>
        </tr>
    </table>
    """
    return _wrap_report_html("Fleet Utilization Report", content)


def generate_job_summary(session, filters):
    """Jobs by status in period."""
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
    rows = session.execute(text(f"""
        SELECT status, COUNT(*) as cnt
        FROM jobs
        WHERE created_at >= {sql.now_offset('-7 days')}
        GROUP BY status ORDER BY cnt DESC
    """)).fetchall()

    table_rows = ""
    total = 0
    for r in rows:
        table_rows += f"""
        <tr style="border-bottom: 1px solid #333;">
            <td style="padding: 8px; font-size: 14px;">{escape(str(r[0]))}</td>
            <td style="padding: 8px; font-size: 14px; text-align: right;">{r[1]}</td>
        </tr>"""
        total += r[1]

    content = f"""
    <p style="color: #888; margin-bottom: 12px;">Last 7 days — {total} total jobs</p>
    <table style="width: 100%; border-collapse: collapse; background: #33312d; border-radius: 4px;">
        <thead><tr style="border-bottom: 2px solid #47453d;">
            <th style="padding: 8px; text-align: left; color: #8a8679; font-size: 12px;">STATUS</th>
            <th style="padding: 8px; text-align: right; color: #8a8679; font-size: 12px;">COUNT</th>
        </tr></thead>
        <tbody>{table_rows}</tbody>
    </table>
    """
    return _wrap_report_html("Job Summary Report", content)


def generate_filament_consumption(session, filters):
    """Usage by material type."""
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
    rows = session.execute(text(f"""
        SELECT fl.material, COALESCE(SUM(su.weight_used_g), 0) as total_g
        FROM spool_usage su
        JOIN spools s ON su.spool_id = s.id
        JOIN filament_library fl ON s.filament_id = fl.id
        WHERE su.used_at >= {sql.now_offset('-30 days')}
        GROUP BY fl.material ORDER BY total_g DESC
    """)).fetchall()

    table_rows = ""
    for r in rows:
        table_rows += f"""
        <tr style="border-bottom: 1px solid #333;">
            <td style="padding: 8px; font-size: 14px;">{escape(str(r[0] or 'Unknown'))}</td>
            <td style="padding: 8px; font-size: 14px; text-align: right;">{round(r[1], 1)}g</td>
        </tr>"""

    content = f"""
    <p style="color: #888; margin-bottom: 12px;">Last 30 days filament consumption</p>
    <table style="width: 100%; border-collapse: collapse; background: #33312d; border-radius: 4px;">
        <thead><tr style="border-bottom: 2px solid #47453d;">
            <th style="padding: 8px; text-align: left; color: #8a8679; font-size: 12px;">MATERIAL</th>
            <th style="padding: 8px; text-align: right; color: #8a8679; font-size: 12px;">USED</th>
        </tr></thead>
        <tbody>{table_rows if table_rows else '<tr><td style="padding: 8px; color: #666;" colspan="2">No usage recorded</td></tr>'}</tbody>
    </table>
    """
    return _wrap_report_html("Filament Consumption Report", content)


def generate_failure_analysis(session, filters):
    """Detection counts, failure reasons."""
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
    rows = session.execute(text(f"""
        SELECT detection_type, COUNT(*) as cnt, AVG(confidence) as avg_conf
        FROM vision_detections
        WHERE created_at >= {sql.now_offset('-30 days')} AND (status IS NULL OR status != 'training')
        GROUP BY detection_type ORDER BY cnt DESC
    """)).fetchall()

    # Also get job failure reasons
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
    fail_rows = session.execute(text(f"""
        SELECT fail_reason, COUNT(*) as cnt
        FROM jobs
        WHERE status = 'failed' AND fail_reason IS NOT NULL
          AND created_at >= {sql.now_offset('-30 days')}
        GROUP BY fail_reason ORDER BY cnt DESC
    """)).fetchall()

    detect_rows_html = ""
    for r in rows:
        detect_rows_html += f"""
        <tr style="border-bottom: 1px solid #333;">
            <td style="padding: 8px; font-size: 14px;">{escape(str(r[0]))}</td>
            <td style="padding: 8px; font-size: 14px; text-align: right;">{r[1]}</td>
            <td style="padding: 8px; font-size: 14px; text-align: right;">{round(r[2] * 100, 1)}%</td>
        </tr>"""

    fail_rows_html = ""
    for r in fail_rows:
        fail_rows_html += f"""
        <tr style="border-bottom: 1px solid #333;">
            <td style="padding: 8px; font-size: 14px;">{escape(str(r[0]))}</td>
            <td style="padding: 8px; font-size: 14px; text-align: right;">{r[1]}</td>
        </tr>"""

    content = f"""
    <p style="color: #888; margin-bottom: 12px;">Last 30 days</p>
    <h3 style="color: #fbbf24; font-size: 14px; margin: 16px 0 8px;">AI Detections</h3>
    <table style="width: 100%; border-collapse: collapse; background: #33312d; border-radius: 4px;">
        <thead><tr style="border-bottom: 2px solid #47453d;">
            <th style="padding: 8px; text-align: left; color: #8a8679; font-size: 12px;">TYPE</th>
            <th style="padding: 8px; text-align: right; color: #8a8679; font-size: 12px;">COUNT</th>
            <th style="padding: 8px; text-align: right; color: #8a8679; font-size: 12px;">AVG CONF</th>
        </tr></thead>
        <tbody>{detect_rows_html if detect_rows_html else '<tr><td style="padding: 8px; color: #666;" colspan="3">No detections</td></tr>'}</tbody>
    </table>
    <h3 style="color: #fbbf24; font-size: 14px; margin: 16px 0 8px;">Job Failure Reasons</h3>
    <table style="width: 100%; border-collapse: collapse; background: #33312d; border-radius: 4px;">
        <thead><tr style="border-bottom: 2px solid #47453d;">
            <th style="padding: 8px; text-align: left; color: #8a8679; font-size: 12px;">REASON</th>
            <th style="padding: 8px; text-align: right; color: #8a8679; font-size: 12px;">COUNT</th>
        </tr></thead>
        <tbody>{fail_rows_html if fail_rows_html else '<tr><td style="padding: 8px; color: #666;" colspan="2">No failures</td></tr>'}</tbody>
    </table>
    """
    return _wrap_report_html("Failure Analysis Report", content)


def generate_chargeback_summary(session, filters):
    """Reuse query pattern from /api/reports/chargebacks.

    Rows are per user and charged organization so each cost carries that
    organization's configured finance currency (no currency is presumed and
    none is converted). Jobs without an estimated cost are counted and labeled
    as unpriced; they are never valued as 0.
    """
    from core.finance_currency import read_currency
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
    rows = session.execute(text(f"""
        SELECT u.username, COUNT(j.id) as job_count,
               SUM(j.estimated_cost) as total_cost,
               COALESCE(SUM(j.duration_hours), 0) as total_hours,
               COALESCE(SUM(pa.grams), 0) as total_grams,
               SUM(CASE WHEN j.estimated_cost IS NULL THEN 1 ELSE 0 END) as unpriced_jobs,
               j.charged_to_org_id as org_id
        FROM jobs j
        LEFT JOIN users u ON j.charged_to_user_id = u.id
        LEFT JOIN ({ARCHIVE_GRAMS_BY_JOB_SQL}) pa ON pa.job_id = j.id
        WHERE j.charged_to_user_id IS NOT NULL
          AND j.status IN ('completed', 'failed')
          AND j.created_at >= {sql.now_offset('-30 days')}
        GROUP BY j.charged_to_user_id, u.username, j.charged_to_org_id
        ORDER BY total_cost DESC
    """)).fetchall()

    currencies = {}

    def _currency(org_id):
        if type(org_id) is not int or org_id <= 0:
            return None
        if org_id not in currencies:
            currencies[org_id] = read_currency(session, owner_scope='org', owner_key=org_id)
        return currencies[org_id]

    def _cost_cell(total, unpriced, org_id):
        if total is None:
            return "Unknown"
        currency = _currency(org_id)
        amount = f"{currency} {total:.2f}" if currency else f"{total:.2f}"
        return amount + (f" (+{int(unpriced)} unpriced)" if unpriced else "")

    table_rows = ""
    for r in rows:
        table_rows += f"""
        <tr style="border-bottom: 1px solid #333;">
            <td style="padding: 8px; font-size: 14px;">{escape(str(r[0] or 'Unknown'))}</td>
            <td style="padding: 8px; font-size: 14px; text-align: right;">{r[1]}</td>
            <td style="padding: 8px; font-size: 14px; text-align: right;">{escape(_cost_cell(r[2], r[5], r[6]))}</td>
            <td style="padding: 8px; font-size: 14px; text-align: right;">{round(r[3], 1)}h</td>
            <td style="padding: 8px; font-size: 14px; text-align: right;">{round(r[4])}g</td>
        </tr>"""

    content = f"""
    <p style="color: #888; margin-bottom: 12px;">Last 30 days chargebacks by user. Costs are job estimates in the charged organization's configured currency (no currency shown when none is configured); unpriced jobs are counted, not valued.</p>
    <table style="width: 100%; border-collapse: collapse; background: #33312d; border-radius: 4px;">
        <thead><tr style="border-bottom: 2px solid #47453d;">
            <th style="padding: 8px; text-align: left; color: #8a8679; font-size: 12px;">USER</th>
            <th style="padding: 8px; text-align: right; color: #8a8679; font-size: 12px;">JOBS</th>
            <th style="padding: 8px; text-align: right; color: #8a8679; font-size: 12px;">COST</th>
            <th style="padding: 8px; text-align: right; color: #8a8679; font-size: 12px;">HOURS</th>
            <th style="padding: 8px; text-align: right; color: #8a8679; font-size: 12px;">GRAMS</th>
        </tr></thead>
        <tbody>{table_rows if table_rows else '<tr><td style="padding: 8px; color: #666;" colspan="5">No chargebacks</td></tr>'}</tbody>
    </table>
    """
    return _wrap_report_html("Chargeback Summary Report", content)


