"""Initial schema baseline

Revision ID: 001_initial
Revises:
Create Date: 2026-02-14

This migration represents the CURRENT database schema as of v1.3.23.
It was generated from the SQLAlchemy models in models.py and should be
stamped (not run) on existing databases:

    alembic stamp head

NOTE: The following tables are created via raw SQL in docker/entrypoint.sh
and are NOT tracked by SQLAlchemy models or Alembic:

    - users
    - print_jobs
    - print_files
    - oidc_config
    - webhooks
    - vision_detections
    - vision_settings
    - vision_models
    - api_tokens
    - active_sessions
    - token_blacklist
    - quota_usage
    - model_revisions
    - report_schedules
    - timelapses (also has a SQLAlchemy model — dual-managed)

These tables must be migrated separately or brought under Alembic control
in a future migration.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- printers ---
    op.create_table(
        "printers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(100), unique=True, nullable=False),
        sa.Column("nickname", sa.String(100), nullable=True),
        sa.Column("model", sa.String(100)),
        sa.Column("slot_count", sa.Integer(), default=4),
        sa.Column("is_active", sa.Boolean(), default=True),
        sa.Column("display_order", sa.Integer(), default=0),
        sa.Column("camera_url", sa.String(), nullable=True),
        sa.Column("camera_enabled", sa.Boolean(), default=True),
        sa.Column("api_type", sa.String(50)),
        sa.Column("api_host", sa.String(255)),
        sa.Column("api_key", sa.String(255)),
        sa.Column("is_favorite", sa.Boolean(), default=False),
        sa.Column("last_seen", sa.DateTime(), nullable=True),
        sa.Column("bed_temp", sa.Float(), nullable=True),
        sa.Column("bed_target_temp", sa.Float(), nullable=True),
        sa.Column("nozzle_temp", sa.Float(), nullable=True),
        sa.Column("nozzle_target_temp", sa.Float(), nullable=True),
        sa.Column("gcode_state", sa.String(20), nullable=True),
        sa.Column("print_stage", sa.String(50), nullable=True),
        sa.Column("hms_errors", sa.Text(), nullable=True),
        sa.Column("lights_on", sa.Boolean(), nullable=True),
        sa.Column("lights_toggled_at", sa.DateTime(), nullable=True),
        sa.Column("nozzle_type", sa.String(20), nullable=True),
        sa.Column("nozzle_diameter", sa.Float(), nullable=True),
        sa.Column("fan_speed", sa.Integer(), nullable=True),
        sa.Column("plug_type", sa.String(20), nullable=True),
        sa.Column("plug_host", sa.String(255), nullable=True),
        sa.Column("plug_entity_id", sa.String(255), nullable=True),
        sa.Column("plug_auth_token", sa.Text(), nullable=True),
        sa.Column("plug_auto_on", sa.Boolean(), default=True),
        sa.Column("plug_auto_off", sa.Boolean(), default=True),
        sa.Column("plug_cooldown_minutes", sa.Integer(), default=5),
        sa.Column("plug_power_state", sa.Boolean(), nullable=True),
        sa.Column("plug_energy_kwh", sa.Float(), default=0),
        sa.Column("total_print_hours", sa.Float(), default=0),
        sa.Column("total_print_count", sa.Integer(), default=0),
        sa.Column("hours_since_maintenance", sa.Float(), default=0),
        sa.Column("prints_since_maintenance", sa.Integer(), default=0),
        sa.Column("last_error_code", sa.String(50), nullable=True),
        sa.Column("last_error_message", sa.Text(), nullable=True),
        sa.Column("last_error_at", sa.DateTime(), nullable=True),
        sa.Column("camera_discovered", sa.Boolean(), default=False),
        sa.Column("tags", sa.JSON(), default=list),
        sa.Column("timelapse_enabled", sa.Boolean(), default=False),
        sa.Column("org_id", sa.Integer(), nullable=True),
        sa.Column("shared", sa.Boolean(), default=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )

    # --- filament_library ---
    op.create_table(
        "filament_library",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("brand", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("material", sa.String(), default="PLA"),
        sa.Column("color_hex", sa.String(6)),
        sa.Column("cost_per_gram", sa.Float(), nullable=True),
        sa.Column("is_custom", sa.Boolean(), default=False),
        sa.Column("created_at", sa.DateTime()),
    )

    # --- spools ---
    op.create_table(
        "spools",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("filament_id", sa.Integer(), sa.ForeignKey("filament_library.id"), nullable=False),
        sa.Column("qr_code", sa.String(50), unique=True),
        sa.Column("rfid_tag", sa.String(32), unique=True, nullable=True),
        sa.Column("color_hex", sa.String(6), nullable=True),
        sa.Column("initial_weight_g", sa.Float(), default=1000.0),
        sa.Column("remaining_weight_g", sa.Float(), default=1000.0),
        sa.Column("spool_weight_g", sa.Float(), default=250.0),
        sa.Column("price", sa.Float()),
        sa.Column("purchase_date", sa.DateTime()),
        sa.Column("vendor", sa.String(100)),
        sa.Column("lot_number", sa.String(50)),
        sa.Column("status", sa.Enum("active", "empty", "archived", name="spoolstatus"), default="active"),
        sa.Column("location_printer_id", sa.Integer(), sa.ForeignKey("printers.id"), nullable=True),
        sa.Column("location_slot", sa.Integer(), nullable=True),
        sa.Column("storage_location", sa.String(100)),
        sa.Column("notes", sa.Text()),
        sa.Column("org_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_spools_qr_code", "spools", ["qr_code"], unique=True)
    op.create_index("ix_spools_rfid_tag", "spools", ["rfid_tag"], unique=True)

    # --- filament_slots ---
    op.create_table(
        "filament_slots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("printer_id", sa.Integer(), sa.ForeignKey("printers.id"), nullable=False),
        sa.Column("slot_number", sa.Integer(), nullable=False),
        sa.Column("filament_type", sa.Enum(
            "empty", "PLA", "PETG", "ABS", "ASA", "TPU", "PA", "PC", "PVA", "OTHER",
            "PLA_SUPPORT", "PLA_CF", "PETG_CF", "NYLON_CF", "NYLON_GF",
            "PC_ABS", "PC_CF", "SUPPORT", "HIPS", "PPS", "PPS_CF",
            name="filamenttype",
        ), default="empty"),
        sa.Column("color", sa.String(50)),
        sa.Column("color_hex", sa.String(7)),
        sa.Column("spoolman_spool_id", sa.Integer()),
        sa.Column("assigned_spool_id", sa.Integer(), sa.ForeignKey("spools.id"), nullable=True),
        sa.Column("spool_confirmed", sa.Boolean(), default=False),
        sa.Column("loaded_at", sa.DateTime(), server_default=sa.func.now()),
    )

    # --- models ---
    op.create_table(
        "models",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("build_time_hours", sa.Float()),
        sa.Column("default_filament_type", sa.Enum(
            "empty", "PLA", "PETG", "ABS", "ASA", "TPU", "PA", "PC", "PVA", "OTHER",
            "PLA_SUPPORT", "PLA_CF", "PETG_CF", "NYLON_CF", "NYLON_GF",
            "PC_ABS", "PC_CF", "SUPPORT", "HIPS", "PPS", "PPS_CF",
            name="filamenttype",
        ), default="PLA"),
        sa.Column("color_requirements", sa.JSON(), default=dict),
        sa.Column("category", sa.String(100)),
        sa.Column("thumbnail_url", sa.String(500)),
        sa.Column("thumbnail_b64", sa.Text()),
        sa.Column("print_file_id", sa.Integer()),
        sa.Column("notes", sa.Text()),
        sa.Column("cost_per_item", sa.Float()),
        sa.Column("units_per_bed", sa.Integer(), default=1),
        sa.Column("quantity_per_bed", sa.Integer(), default=1),
        sa.Column("markup_percent", sa.Float(), default=300),
        sa.Column("is_favorite", sa.Boolean(), default=False),
        sa.Column("org_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )

    # --- products ---
    op.create_table(
        "products",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("sku", sa.String(50), nullable=True),
        sa.Column("price", sa.Float(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )

    # --- orders ---
    op.create_table(
        "orders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_number", sa.String(100), nullable=True),
        sa.Column("platform", sa.String(50), nullable=True),
        sa.Column("customer_name", sa.String(200), nullable=True),
        sa.Column("customer_email", sa.String(200), nullable=True),
        sa.Column("status", sa.Enum(
            "pending", "in_progress", "partial", "fulfilled", "shipped", "cancelled",
            name="orderstatus",
        ), default="pending"),
        sa.Column("revenue", sa.Float(), nullable=True),
        sa.Column("platform_fees", sa.Float(), nullable=True),
        sa.Column("payment_fees", sa.Float(), nullable=True),
        sa.Column("shipping_charged", sa.Float(), nullable=True),
        sa.Column("shipping_cost", sa.Float(), nullable=True),
        sa.Column("labor_minutes", sa.Integer(), default=0),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("order_date", sa.DateTime(), nullable=True),
        sa.Column("shipped_date", sa.DateTime(), nullable=True),
        sa.Column("tracking_number", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )

    # --- order_items ---
    op.create_table(
        "order_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("quantity", sa.Integer(), default=1),
        sa.Column("unit_price", sa.Float(), nullable=True),
        sa.Column("fulfilled_quantity", sa.Integer(), default=0),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )

    # --- jobs ---
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("model_id", sa.Integer(), sa.ForeignKey("models.id")),
        sa.Column("model_revision_id", sa.Integer(), nullable=True),
        sa.Column("item_name", sa.String(200), nullable=False),
        sa.Column("quantity", sa.Integer(), default=1),
        sa.Column("status", sa.Enum(
            "pending", "scheduled", "printing", "completed", "failed", "cancelled",
            name="jobstatus",
        ), default="pending"),
        sa.Column("priority", sa.Integer(), default=3),
        sa.Column("printer_id", sa.Integer(), sa.ForeignKey("printers.id")),
        sa.Column("scheduled_start", sa.DateTime()),
        sa.Column("scheduled_end", sa.DateTime()),
        sa.Column("actual_start", sa.DateTime()),
        sa.Column("actual_end", sa.DateTime()),
        sa.Column("duration_hours", sa.Float()),
        sa.Column("colors_required", sa.String(500)),
        sa.Column("filament_type", sa.Enum(
            "empty", "PLA", "PETG", "ABS", "ASA", "TPU", "PA", "PC", "PVA", "OTHER",
            "PLA_SUPPORT", "PLA_CF", "PETG_CF", "NYLON_CF", "NYLON_GF",
            "PC_ABS", "PC_CF", "SUPPORT", "HIPS", "PPS", "PPS_CF",
            name="filamenttype",
        )),
        sa.Column("match_score", sa.Integer()),
        sa.Column("is_locked", sa.Boolean(), default=False),
        sa.Column("hold", sa.Boolean(), default=False),
        sa.Column("notes", sa.Text()),
        sa.Column("estimated_cost", sa.Float(), nullable=True),
        sa.Column("suggested_price", sa.Float(), nullable=True),
        sa.Column("order_item_id", sa.Integer(), sa.ForeignKey("order_items.id"), nullable=True),
        sa.Column("quantity_on_bed", sa.Integer(), default=1),
        sa.Column("due_date", sa.DateTime(), nullable=True),
        sa.Column("submitted_by", sa.Integer(), nullable=True),
        sa.Column("approved_by", sa.Integer(), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("rejected_reason", sa.Text(), nullable=True),
        sa.Column("fail_reason", sa.String(100), nullable=True),
        sa.Column("fail_notes", sa.Text(), nullable=True),
        sa.Column("charged_to_user_id", sa.Integer(), nullable=True),
        sa.Column("charged_to_org_id", sa.Integer(), nullable=True),
        sa.Column("is_favorite", sa.Boolean(), default=False),
        sa.Column("required_tags", sa.JSON(), default=list),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )

    # --- product_components ---
    op.create_table(
        "product_components",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("model_id", sa.Integer(), sa.ForeignKey("models.id"), nullable=False),
        sa.Column("quantity_needed", sa.Integer(), default=1),
        sa.Column("notes", sa.Text(), nullable=True),
    )

    # --- scheduler_runs ---
    op.create_table(
        "scheduler_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("total_jobs", sa.Integer(), default=0),
        sa.Column("scheduled_count", sa.Integer(), default=0),
        sa.Column("skipped_count", sa.Integer(), default=0),
        sa.Column("setup_blocks", sa.Integer(), default=0),
        sa.Column("avg_match_score", sa.Float()),
        sa.Column("avg_job_duration", sa.Float()),
        sa.Column("notes", sa.Text()),
    )

    # --- audit_logs ---
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("timestamp", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("action", sa.String(50), nullable=False),
        sa.Column("entity_type", sa.String(50)),
        sa.Column("entity_id", sa.Integer()),
        sa.Column("details", sa.JSON()),
        sa.Column("ip_address", sa.String(45)),
    )

    # --- spool_usage ---
    op.create_table(
        "spool_usage",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("spool_id", sa.Integer(), sa.ForeignKey("spools.id"), nullable=False),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id"), nullable=True),
        sa.Column("weight_used_g", sa.Float(), nullable=False),
        sa.Column("used_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("notes", sa.String(255)),
    )

    # --- drying_logs ---
    op.create_table(
        "drying_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("spool_id", sa.Integer(), sa.ForeignKey("spools.id"), nullable=False),
        sa.Column("dried_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("duration_hours", sa.Float(), nullable=False),
        sa.Column("temp_c", sa.Float(), nullable=True),
        sa.Column("method", sa.String(50), default="dryer"),
        sa.Column("notes", sa.Text(), nullable=True),
    )

    # --- timelapses ---
    op.create_table(
        "timelapses",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("printer_id", sa.Integer(), sa.ForeignKey("printers.id"), nullable=False),
        sa.Column("print_job_id", sa.Integer(), nullable=True),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("frame_count", sa.Integer(), default=0),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("file_size_mb", sa.Float(), nullable=True),
        sa.Column("status", sa.String(20), default="capturing"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
    )

    # --- print_presets ---
    op.create_table(
        "print_presets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(200), unique=True, nullable=False),
        sa.Column("model_id", sa.Integer(), sa.ForeignKey("models.id"), nullable=True),
        sa.Column("item_name", sa.String(200)),
        sa.Column("quantity", sa.Integer(), default=1),
        sa.Column("priority", sa.Integer(), default=3),
        sa.Column("duration_hours", sa.Float(), nullable=True),
        sa.Column("colors_required", sa.String(500), nullable=True),
        sa.Column("filament_type", sa.Enum(
            "empty", "PLA", "PETG", "ABS", "ASA", "TPU", "PA", "PC", "PVA", "OTHER",
            "PLA_SUPPORT", "PLA_CF", "PETG_CF", "NYLON_CF", "NYLON_GF",
            "PC_ABS", "PC_CF", "SUPPORT", "HIPS", "PPS", "PPS_CF",
            name="filamenttype",
        ), nullable=True),
        sa.Column("required_tags", sa.JSON(), default=list),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )

    # --- maintenance_tasks ---
    op.create_table(
        "maintenance_tasks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("printer_model_filter", sa.String(100), nullable=True),
        sa.Column("interval_print_hours", sa.Float(), nullable=True),
        sa.Column("interval_days", sa.Integer(), nullable=True),
        sa.Column("estimated_cost", sa.Float(), default=0),
        sa.Column("estimated_downtime_min", sa.Integer(), default=30),
        sa.Column("is_active", sa.Boolean(), default=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )

    # --- maintenance_logs ---
    op.create_table(
        "maintenance_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("printer_id", sa.Integer(), sa.ForeignKey("printers.id"), nullable=False),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("maintenance_tasks.id"), nullable=True),
        sa.Column("task_name", sa.String(200), nullable=False),
        sa.Column("performed_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("performed_by", sa.String(100), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("cost", sa.Float(), default=0),
        sa.Column("downtime_minutes", sa.Integer(), default=0),
        sa.Column("print_hours_at_service", sa.Float(), default=0),
    )

    # --- nozzle_lifecycle ---
    op.create_table(
        "nozzle_lifecycle",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("printer_id", sa.Integer(), sa.ForeignKey("printers.id"), nullable=False),
        sa.Column("nozzle_type", sa.String(20), nullable=True),
        sa.Column("nozzle_diameter", sa.Float(), nullable=True),
        sa.Column("installed_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("removed_at", sa.DateTime(), nullable=True),
        sa.Column("print_hours_accumulated", sa.Float(), default=0),
        sa.Column("print_count", sa.Integer(), default=0),
        sa.Column("notes", sa.Text(), nullable=True),
    )

    # --- consumables ---
    op.create_table(
        "consumables",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("sku", sa.String(50), nullable=True),
        sa.Column("unit", sa.String(20), default="piece"),
        sa.Column("cost_per_unit", sa.Float(), default=0),
        sa.Column("current_stock", sa.Float(), default=0),
        sa.Column("min_stock", sa.Float(), default=0),
        sa.Column("vendor", sa.String(200), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), default="active"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )

    # --- product_consumables ---
    op.create_table(
        "product_consumables",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("consumable_id", sa.Integer(), sa.ForeignKey("consumables.id"), nullable=False),
        sa.Column("quantity_per_product", sa.Float(), default=1),
        sa.Column("notes", sa.Text(), nullable=True),
    )

    # --- consumable_usage ---
    op.create_table(
        "consumable_usage",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("consumable_id", sa.Integer(), sa.ForeignKey("consumables.id"), nullable=False),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id"), nullable=True),
        sa.Column("quantity_used", sa.Float(), nullable=False),
        sa.Column("used_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("notes", sa.String(255), nullable=True),
    )

    # --- system_config ---
    op.create_table(
        "system_config",
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )

    # --- alerts ---
    op.create_table(
        "alerts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("alert_type", sa.Enum(
            "print_complete", "print_failed", "printer_error", "spool_low",
            "maintenance_overdue", "job_submitted", "job_approved", "job_rejected",
            "spaghetti_detected", "first_layer_issue", "detachment_detected",
            name="alerttype",
        ), nullable=False),
        sa.Column("severity", sa.Enum("info", "warning", "critical", name="alertseverity"), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("is_read", sa.Boolean(), default=False),
        sa.Column("is_dismissed", sa.Boolean(), default=False),
        sa.Column("printer_id", sa.Integer(), sa.ForeignKey("printers.id"), nullable=True),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id"), nullable=True),
        sa.Column("spool_id", sa.Integer(), sa.ForeignKey("spools.id"), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_alerts_user_id", "alerts", ["user_id"])
    op.create_index("ix_alerts_is_read", "alerts", ["is_read"])
    op.create_index("ix_alerts_created_at", "alerts", ["created_at"])

    # --- alert_preferences ---
    op.create_table(
        "alert_preferences",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("alert_type", sa.Enum(
            "print_complete", "print_failed", "printer_error", "spool_low",
            "maintenance_overdue", "job_submitted", "job_approved", "job_rejected",
            "spaghetti_detected", "first_layer_issue", "detachment_detected",
            name="alerttype",
        ), nullable=False),
        sa.Column("in_app", sa.Boolean(), default=True),
        sa.Column("browser_push", sa.Boolean(), default=False),
        sa.Column("email", sa.Boolean(), default=False),
        sa.Column("threshold_value", sa.Float(), nullable=True),
    )
    op.create_index("ix_alert_preferences_user_id", "alert_preferences", ["user_id"])

    # --- push_subscriptions ---
    op.create_table(
        "push_subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("p256dh_key", sa.Text(), nullable=False),
        sa.Column("auth_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_push_subscriptions_user_id", "push_subscriptions", ["user_id"])


def downgrade() -> None:
    # Drop in reverse dependency order
    op.drop_table("push_subscriptions")
    op.drop_table("alert_preferences")
    op.drop_table("alerts")
    op.drop_table("system_config")
    op.drop_table("consumable_usage")
    op.drop_table("product_consumables")
    op.drop_table("consumables")
    op.drop_table("nozzle_lifecycle")
    op.drop_table("maintenance_logs")
    op.drop_table("maintenance_tasks")
    op.drop_table("print_presets")
    op.drop_table("timelapses")
    op.drop_table("drying_logs")
    op.drop_table("spool_usage")
    op.drop_table("audit_logs")
    op.drop_table("scheduler_runs")
    op.drop_table("product_components")
    op.drop_table("jobs")
    op.drop_table("order_items")
    op.drop_table("orders")
    op.drop_table("products")
    op.drop_table("models")
    op.drop_table("filament_slots")
    op.drop_table("spools")
    op.drop_table("filament_library")
    op.drop_table("printers")
